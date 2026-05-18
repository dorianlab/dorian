"""
dorian/event/handlers/risk_debugger.py
---------------------------------------
Core AI Debugger event handlers — risk identification, mitigation discovery,
suggestion rendering, canvas scope management, and debounced analysis.
"""

import asyncio
import json
from collections import defaultdict
from uuid import uuid4

from backend.cache import cached_read_csv
from backend.events import Event, aemit
from backend.envs import aioredis
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.knowledge.sources.mitigations import MITIGATION_CATALOG, render_description

from .risk_kb import (
    _short_name,
    _has_rewrite_rule,
    _kb_risks_for_operator,
    _kb_principles_for_risk,
    _kb_checks_for_risk,
    _kb_mitigations_with_descriptions,
    _kb_direct_alternatives,
)
from .risk_pathways import (
    _extract_pipeline_from_event,
    _extract_operators,
    _debug_pipeline,
)


# ═══════════════════════════════════════════��═══════════════════════════════
# Core risk chain
# ═══════════════════════════════════════════════════════════════════════════

async def identify_risks(event: Event):
    """Query the KB for *potential* risks that an operator ``might_introduce``.

    Emits ``PotentialRiskIdentified`` for each risk found.  These are *not*
    yet validated on data — they represent structural KB knowledge about the
    operator.
    """
    uid, session = event.data["uid"], event.data["session"]
    operator = event.data.get("operator")
    if not operator:
        return

    risks = await _kb_risks_for_operator(operator)

    for record in risks:
        await aemit(Event("PotentialRiskIdentified", data={
            "uid": uid,
            "session": session,
            "operator": operator,
            "risk": record["risk_name"],
            "status": "potential",
        }))


async def identify_mitigations(event: Event):
    """For a given risk, query the KB for mitigations (``might_mitigate``)
    and discover direct alternatives via the ``performs`` pathway.

    Uses ``_kb_mitigations_with_descriptions`` to fetch mitigations AND their
    description templates in a single Neo4j round-trip (batched Cypher).
    Results are cached by risk name so that the same risk across multiple
    operators (e.g. Overfitting on every sklearn estimator) costs only one
    query total.  The Python ``MITIGATION_CATALOG`` serves as a synchronous
    fallback when the KB returns no descriptions.

    Emits ``MitigationActionsIdentified`` with enriched action list including
    templated descriptions.
    """
    uid, session = event.data["uid"], event.data["session"]
    operator = event.data.get("operator", "")
    risk = event.data["risk"]
    status = event.data.get("status", "potential")

    ctx = defaultdict(str, operator=operator, risk=risk)

    # 1. Standard mitigations from KB — single batched query returns
    #    mitigations + descriptions together, cached across operators.
    mitigation_rows = await _kb_mitigations_with_descriptions(risk)
    actions: list[dict] = []

    # Track Direct Alternative description from the batch (avoid extra query)
    da_short_template, da_long_template = "", ""

    for row in mitigation_rows:
        name = row["name"]
        if name == "Direct Alternative":
            da_short_template = row.get("short", "")
            da_long_template = row.get("long", "")
            continue  # handled below with alternatives list

        # Prefer KB descriptions (canonical), fall back to Python catalog
        kb_short, kb_long = row.get("short", ""), row.get("long", "")
        short = (kb_short or "").format_map(ctx) if kb_short else render_description(
            name, operator=operator, risk=risk,
        )
        long = (kb_long or "").format_map(ctx) if kb_long else render_description(
            name, operator=operator, risk=risk, long=True,
        )

        actions.append({"name": name, "short": short, "long": long})

    # 2. Direct alternatives — KB pathway: same task, no risk link
    task_name, alternatives = await _kb_direct_alternatives(operator, risk)
    if alternatives:
        alt_display = ", ".join(_short_name(a) for a in alternatives[:5])
        da_ctx = defaultdict(
            str, operator=_short_name(operator), risk=risk,
            task=task_name, alternatives=alt_display,
        )
        # Use descriptions from the batched query; fall back to catalog
        short = (da_short_template or "").format_map(da_ctx) if da_short_template else render_description(
            "Direct Alternative", operator=_short_name(operator), risk=risk,
        )
        long = (da_long_template or "").format_map(da_ctx) if da_long_template else render_description(
            "Direct Alternative", operator=_short_name(operator), risk=risk,
            task=task_name, alternatives=alt_display, long=True,
        )
        actions.append({
            "name": "Direct Alternative",
            "short": short,
            "long": long,
            "alternatives": alternatives,
            "task": task_name,
        })

    if actions:
        await aemit(Event("MitigationActionsIdentified", data={
            "uid": uid,
            "session": session,
            "operator": operator,
            "risk": risk,
            "status": status,
            "actions": actions,
            "pipeline_label": event.data.get("pipeline_label", ""),
            "pipeline_id": event.data.get("pipeline_id", ""),
            "check_message": event.data.get("check_message", ""),
        }))


async def render_suggestion(event: Event):
    """Enrich with EU principles, available checks, and push each action to
    the frontend Redis stream as a suggestion card."""
    uid, session = event.data["uid"], event.data["session"]
    operator = event.data.get("operator", "")
    risk = event.data["risk"]
    actions = event.data["actions"]  # list[dict] with name, short, long, …
    status = event.data.get("status", "potential")

    # ── Applicability gate ──────────────────────────────��───────────────
    # Only emit if the target operator is currently on the canvas.
    # The "dataset" pseudo-operator (dataset-level checks) is exempt —
    # it has no canvas node but is gated elsewhere (empty pipeline → no
    # operators → _debug_pipeline returns early).
    if operator and operator != "dataset":
        is_on_canvas = await aioredis.sismember(
            RedisKeys.canvas_operators(session), operator,
        )
        if not is_on_canvas:
            return

    principles = await _kb_principles_for_risk(risk)
    checks = await _kb_checks_for_risk(risk)

    severity = "high" if status == "actionable" else "medium"

    stream = RedisKeys.stream(uid, session)
    pipe = aioredis.pipeline(transaction=False)

    pipeline_label = event.data.get("pipeline_label", "")
    pipeline_id = event.data.get("pipeline_id", "")
    check_message = event.data.get("check_message", "")

    for action in actions:
        has_rewrite = await _has_rewrite_rule(action["name"])
        message = {
            "event": "suggestion",
            "sid": str(uuid4()),
            "uid": uid,
            "session": session,
            "task": operator,
            "risk": risk,
            "action": action["name"],
            "description_short": action.get("short", ""),
            "description_long": action.get("long", ""),
            "alternatives": json.dumps(action.get("alternatives", [])),
            "principles": json.dumps(principles),
            "checks": json.dumps(checks),
            "severity": severity,
            "status": status,
            "source": "kb" if status == "potential" else "data_check",
            "pipeline_label": pipeline_label,
            "pipeline_id": pipeline_id,
            "check_message": check_message,
            "has_rewrite": str(has_rewrite).lower(),
        }
        pipe.xadd(stream, {str(k): str(v) for k, v in message.items()}, maxlen=STREAM_MAXLEN, approximate=True)

    await pipe.execute()


# ══════════════════════════════════════════════════════════════════���════════
# Canvas trigger — fires when user drops an operator on the canvas
# ══════════════════════════════════��═══════════════════════════════════���════

# Per-session debounce state for rapid node additions.
_pending_operators: dict[str, set[str]] = {}   # session -> set of operator FQNs
_pending_risk_tasks: dict[str, asyncio.Task] = {}  # session -> debounce task
_pending_uids: dict[str, str] = {}  # session -> uid (latest caller wins)

_RISK_DEBOUNCE_SECONDS = 0.3


async def _debounced_risk_analysis(session: str):
    """Wait for the debounce window, then analyse all collected operators."""
    try:
        await asyncio.sleep(_RISK_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # superseded by a newer call — discard silently

    # Atomically pop the pending batch.
    uid = _pending_uids.pop(session, None)
    operators = _pending_operators.pop(session, set())
    _pending_risk_tasks.pop(session, None)

    if not uid or not operators:
        return

    for op_name in operators:
        await aemit(Event("RiskAnalysisStarted", {"operator": op_name, "session": session}))
        await identify_risks(Event("TaskIdentified", data={
            "uid": uid,
            "session": session,
            "operator": op_name,
        }))


async def identify_operator_risks(event: Event):
    """Triggered by ``PipelineNodeAdded``; extract operator FQN and start the
    risk identification chain (potential risks only — no data validation).

    Rapid successive calls for the same session are debounced: operators are
    collected for 300 ms of quiet before a single batched analysis pass runs.
    """
    uid = event.data.get("uid")
    session = event.data.get("session")
    payload = event.data.get("payload", event.data)
    # Frontend sends "nodeName"; fall back to "name" for internal callers.
    op_name = payload.get("nodeName") or payload.get("name", "")

    # Skip non-FQN entries (Parameters, Snippets, custom nodes)
    if not op_name or "." not in op_name:
        return

    # Track operator in the canvas scope set used for suggestion gating.
    await aioredis.sadd(RedisKeys.canvas_operators(session), op_name)

    # ── Debounce: collect operator and (re)schedule the delayed analysis ─
    _pending_operators.setdefault(session, set()).add(op_name)
    _pending_uids[session] = uid  # latest uid wins (same user)

    existing = _pending_risk_tasks.get(session)
    if existing and not existing.done():
        existing.cancel()

    _pending_risk_tasks[session] = asyncio.create_task(
        _debounced_risk_analysis(session),
    )


# ══════════════════════════════════════════════��════════════════════════════
# Canvas removal / scope reset — fires when operators leave the canvas
# ═══════════════════════════════════��═════════════════════════════���═════════

async def handle_node_removed(event: Event):
    """Cancel any pending debounced risk analysis when the user removes
    an operator from the canvas.

    The redis I/O slice (SREM, suggestions/reset, TaskIdentified emit,
    CanvasScopeUpdated emit) ported to rust
    (``engine/backend/src/handlers/risk_scope.rs::handle_node_removed``).
    Python keeps subscribing to PipelineNodeRemoved only to clear the
    debounce state — the AI Debugger chain is still python and the
    debounced task lives in this process.

    The CSV-based revalidation (``_revalidate_data_checks``) now hangs
    off ``CanvasScopeUpdated`` (rust-emitted, with
    ``affected_operators`` in the payload) — see
    ``handle_canvas_scope_updated`` below.
    """
    session = event.data.get("session")
    op_name = event.data.get("nodeName", "")
    if not op_name or "." not in op_name:
        return
    pending_task = _pending_risk_tasks.pop(session, None)
    if pending_task and not pending_task.done():
        pending_task.cancel()
    _pending_operators.pop(session, None)
    _pending_uids.pop(session, None)


async def handle_canvas_scope_updated(event: Event):
    """Run CSV-backed revalidation on the new canvas scope.

    Subscribed to: ``CanvasScopeUpdated`` (emitted by the rust
    ``risk_scope::handle_node_removed`` handler). Carries
    ``affected_operators`` so revalidation is incremental.

    This handler intentionally stays python — it reads the dataset
    CSV, runs chi-squared / NaN counts / etc. via the toolbox checks.
    Those are "submitted compute jobs" by the migration policy; the
    orchestration (which operators to revalidate against) is rust.
    """
    uid = event.data.get("uid")
    session = event.data.get("session")
    affected = event.data.get("affected_operators")
    if not session:
        return
    affected_list = list(affected) if isinstance(affected, list) else None
    await _revalidate_data_checks(uid, session, affected_operators=affected_list)


async def _revalidate_data_checks(
    uid: str,
    session: str,
    affected_operators: list[str] | None = None,
) -> None:
    """Re-run data-validated checks for canvas operators.

    Shared helper called by ``handle_node_removed`` and potentially other
    scope-change handlers.  Loads session data and delegates to
    ``_debug_pipeline``.

    Parameters
    ----------
    affected_operators:
        When provided, only these operators are re-checked (incremental
        revalidation).  When ``None``, falls back to reading the full
        ``canvas_operators`` SET from Redis (original O(N*M) behaviour).

    Does nothing if the session has no dataset yet.
    """
    raw = await aioredis.get(RedisKeys.session_meta(session))
    if not raw:
        return
    meta = json.loads(raw)
    dataset = meta.get("dataset") or {}
    did = dataset.get("did", "")
    fpath = dataset.get("fpath")
    if not fpath:
        return

    try:
        df = cached_read_csv(fpath)
    except Exception as exc:
        await aemit(Event("RevalidateDataCheckReadFailed", {"fpath": fpath, "error": str(exc)}))
        return

    target_raw = await aioredis.get(RedisKeys.dataset_target_columns(did))
    target_cols: list[str] = json.loads(target_raw) if target_raw else []

    protected_raw = await aioredis.get(f"dataset:{did}:protected_attributes")
    protected_attrs: list[str] = json.loads(protected_raw) if protected_raw else []

    if affected_operators is not None:
        operators = affected_operators
    else:
        canvas_ops_raw = await aioredis.smembers(RedisKeys.canvas_operators(session))
        operators = [
            op.decode() if isinstance(op, bytes) else op
            for op in canvas_ops_raw
        ]

    if not operators:
        return  # No operators → no data checks to run

    await _debug_pipeline(
        uid, session, did,
        operators, "Current pipeline",
        df, target_cols, protected_attrs,
    )


async def handle_pipeline_composed(event: Event):
    """Reset AI Debugger scope when user creates a new empty pipeline.

    Triggered by ``PipelineComposed`` — emitted from the frontend when the
    user clicks "Compose" to start a pipeline from scratch.
    """
    uid = event.data.get("uid")
    session = event.data.get("session")

    # Clear the entire canvas operator set
    await aioredis.delete(RedisKeys.canvas_operators(session))

    # Emit reset event → frontend clears all suggestions
    stream = RedisKeys.stream(uid, session)
    await aioredis.xadd(stream, {"event": "suggestions/reset"}, maxlen=STREAM_MAXLEN, approximate=True)
    await aemit(Event("SuggestionsReset", {"session": session}))


async def sync_canvas_operators_from_pipeline(event: Event):
    """Populate the canvas operator set when a saved pipeline is loaded.

    Triggered by ``PipelineRetrieved`` — extracts operator FQNs from the
    pipeline JSON, replaces the canvas operator set, clears stale
    suggestions, and re-runs the potential-risk identification chain for
    every operator in the loaded pipeline.
    """
    session = event.data.get("session")
    uid = event.data.get("uid")
    value = event.data.get("value")

    # The event value structure varies — pipelineHistory dict, version dict,
    # or raw pipeline JSON string.
    pipeline_json = _extract_pipeline_from_event(value)
    operators = _extract_operators(pipeline_json)

    # Replace the canvas operator set
    key = RedisKeys.canvas_operators(session)
    await aioredis.delete(key)
    if operators:
        await aioredis.sadd(key, *operators)

    # Clear stale suggestions and re-identify risks for all operators
    stream = RedisKeys.stream(uid, session)
    await aioredis.xadd(stream, {"event": "suggestions/reset"}, maxlen=STREAM_MAXLEN, approximate=True)

    for op_name in operators:
        await identify_risks(Event("TaskIdentified", data={
            "uid": uid,
            "session": session,
            "operator": op_name,
        }))

    await aemit(Event("CanvasOperatorsSynced", {"count": len(operators), "session": session}))
