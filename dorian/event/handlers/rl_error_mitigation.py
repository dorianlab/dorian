"""
dorian/event/handlers/rl_error_mitigation.py
--------------------------------------------
RL consumer for the Debugger's execution-error mitigation stream.

The AI Debugger already emits structured suggestions for failed nodes
(via ``dorian.event.handlers.execution_error_handler``).  End-users accept
them by clicking the suggestion card; this module does the same thing
automatically for pipelines produced by the RL generator, then resubmits
the rewritten DAG as a **child pipeline**.

Feedback loop
-------------
1. RL generator submits a pipeline → pipeline fails at some node.
2. Debugger handler matches the error against ``EXECUTION_ERROR_PATTERNS``,
   emits a suggestion, and indexes it by ``run_id`` in Redis.
3. This handler (subscribed to ``PipelineRunFailed``) only fires on
   synthetic RL sessions (``session`` starts with ``rl:``) and:
     a. Loads the parent DAG from ``doc_pipelines``.
     b. Replays the Debugger's recorded suggestion (Redis).
     c. Applies the rewrite (parameter change OR structural rewrite).
     d. Persists + resubmits the rewritten DAG as a child pipeline via
        ``persist_and_submit``.
4. A parent→child attempt row lands in the docstore
   ``rl_mitigation_attempts`` so that when the child completes the RL
   reward attribution loop (future work) can credit the mitigation.

Guards
------
* ``mitigation_depth`` in the submission payload caps retry chains at a
  small number (default 2) so a persistently-broken pattern cannot burn
  budget forever.
* The same suggestion applied to the same parent never fires twice — we
  key attempts on ``(parent_pipeline_id, signature)``.
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone

from backend.events import Event, aemit, aemit_bg
from backend.envs import aioredis
from dorian.dag import DAG
from dorian.pipeline.mitigation_apply import (
    apply_parameter_change_to_dag as _apply_parameter_change_to_dag,
    apply_structural_rewrite_to_dag as _apply_structural_rewrite_to_dag,
)


# Max number of auto-mitigation hops in a parent→child chain before we
# give up and leave the failure uncorrected.
_MAX_MITIGATION_DEPTH = 2


async def handle_rl_pipeline_run_failed(event: Event) -> None:
    """Auto-apply the Debugger's suggestion to a failing RL pipeline.

    Subscribes to ``PipelineRunFailed``. No-op unless the session looks
    like an RL generator session (``rl:*``). Short-circuits on missing
    suggestion, missing parent DAG, retry-depth exhaustion, or duplicate
    attempt.
    """
    session = event.data.get("session", "")
    # Engine-driven sessions are auto-mitigation candidates: RL trainer,
    # AutoML BO, and the cross-product crawler all submit pipelines via
    # synthetic sessions and benefit from the same Debugger-suggestion
    # → rewrite → resubmit feedback loop. User sessions skip the loop —
    # the user accepts the suggestion card explicitly via the UI.
    if not session or not session.split(":", 1)[0] in ("rl", "automl", "xproduct"):
        return

    run_id = event.data.get("run_id", "")
    parent_pipeline_id = event.data.get("pipeline_id", "")
    if not run_id or not parent_pipeline_id:
        return

    # ── 1. Retrieve the suggestion the Debugger emitted for this run ──
    try:
        raw = await aioredis.get(f"execution_error:run:{run_id}:suggestion")
    except Exception:
        raw = None
    if not raw:
        # Nothing the Debugger knows how to fix — surface for curation.
        await aemit_bg(Event("RLMitigationSkipped", {
            "source": "rl_error_mitigation",
            "reason": "no suggestion indexed for run",
            "run_id": run_id,
            "pipeline_id": parent_pipeline_id,
            "session": session,
        }))
        return

    suggestion = json.loads(raw) if isinstance(raw, (bytes, bytearray, str)) else raw
    if isinstance(suggestion, (bytes, bytearray)):
        suggestion = json.loads(suggestion.decode("utf-8"))

    fix_type = suggestion.get("fix_type", "")
    raw_operator = suggestion.get("operator", "")
    failing_node_id = suggestion.get("node_id", "")
    pattern_id = suggestion.get("pattern_id", "")
    mitigation_slug = suggestion.get("mitigation_slug") or ""

    # ── 2. Cap mitigation chain depth ─────────────────────────────────
    mitigation_depth = int(event.data.get("mitigation_depth", 0) or 0)
    if mitigation_depth >= _MAX_MITIGATION_DEPTH:
        await aemit_bg(Event("RLMitigationDepthExhausted", {
            "source": "rl_error_mitigation",
            "pipeline_id": parent_pipeline_id,
            "depth": mitigation_depth,
            "pattern_id": pattern_id,
        }))
        return

    # ── 3. Dedup: don't try the same fix on the same parent twice ────
    try:
        from backend.envs import expdb
        existing = await expdb.rl_mitigation_attempts.find_one({
            "parent_pipeline_id": parent_pipeline_id,
            "pattern_id": pattern_id,
        })
        if existing is not None:
            await aemit_bg(Event("RLMitigationSkipped", {
                "source": "rl_error_mitigation",
                "reason": "attempt already recorded for parent+pattern",
                "pipeline_id": parent_pipeline_id,
                "pattern_id": pattern_id,
            }))
            return
    except Exception:
        pass  # dedup is best-effort

    # ── 4. Load the parent DAG ─────────────────────────────────────────
    parent_dag = await _load_parent_dag(parent_pipeline_id)
    if parent_dag is None:
        await aemit_bg(Event("RLMitigationSkipped", {
            "source": "rl_error_mitigation",
            "reason": "parent DAG not found",
            "pipeline_id": parent_pipeline_id,
        }))
        return

    # ── 4b. Resolve the real operator FQN from the parent DAG ──────────
    # The execution error fires on a COMPOUND-EXPANDED sub-node
    # (e.g. ``5048bc185945_cx_fit_1``). The stored parent DAG is
    # pre-expansion, so its top-level node id is the ``_cx_`` prefix.
    # The rewrite compiler needs the operator's class FQN (e.g.
    # ``sklearn.linear_model.PassiveAggressiveClassifier``) to match — NOT
    # the node_id.
    operator_fqn = _resolve_operator_fqn(parent_dag, failing_node_id, raw_operator)
    if not operator_fqn:
        await aemit_bg(Event("RLMitigationSkipped", {
            "source": "rl_error_mitigation",
            "reason": "could not resolve operator FQN from parent DAG",
            "pipeline_id": parent_pipeline_id,
            "node_id": failing_node_id,
            "raw_operator": raw_operator,
        }))
        return

    # ── 5. Apply the mitigation ────────────────────────────────────────
    try:
        if fix_type == "parameter_change":
            child_dag = _apply_parameter_change_to_dag(
                parent_dag,
                operator_fqn=operator_fqn,
                param_name=suggestion.get("param_name", ""),
                fix_value=suggestion.get("fix_value", ""),
            )
        elif fix_type == "structural_rewrite":
            child_dag = await _apply_structural_rewrite_to_dag(
                parent_dag,
                operator_fqn=operator_fqn,
                mitigation_slug=mitigation_slug,
            )
        else:
            await aemit_bg(Event("RLMitigationSkipped", {
                "source": "rl_error_mitigation",
                "reason": f"unknown fix_type {fix_type!r}",
                "pipeline_id": parent_pipeline_id,
            }))
            return
    except Exception:
        await aemit_bg(Event("RLMitigationApplyFailed", {
            "source": "rl_error_mitigation",
            "pipeline_id": parent_pipeline_id,
            "pattern_id": pattern_id,
            "error": traceback.format_exc(),
        }))
        return

    if child_dag is None:
        await aemit_bg(Event("RLMitigationSkipped", {
            "source": "rl_error_mitigation",
            "reason": "mitigation produced no change",
            "pipeline_id": parent_pipeline_id,
            "pattern_id": pattern_id,
        }))
        return

    # ── 6. Persist and resubmit as a child pipeline ────────────────────
    dataset_id = event.data.get("dataset_id") or _dataset_from_session(session)
    task = event.data.get("task") or "Classification"

    from dorian.pipeline.generation.executor import persist_and_submit
    child_pipeline_id = await persist_and_submit(
        child_dag,
        dataset_id=dataset_id or "",
        task=task,
        session=session,
        source="rl_auto_mitigation",
    )

    # ── 7. Record the attempt for reward attribution ──────────────────
    # The trajectory of action_ids the parent took is captured here so
    # the trainer can apply ``MemoryPolicy.credit_partial_success`` to
    # it once the child completes successfully — see the drain in
    # ``rl/train/partial_credit.py`` for the consumer side.
    parent_action_ids = (event.data.get("rl_trajectory_action_ids") or [])
    parent_embedding = event.data.get("rl_dataset_embedding") or []
    try:
        from backend.envs import expdb
        await expdb.rl_mitigation_attempts.insert_one({
            "parent_pipeline_id": parent_pipeline_id,
            "child_pipeline_id": child_pipeline_id,
            "session": session,
            "dataset_id": dataset_id,
            "pattern_id": pattern_id,
            "mitigation_slug": mitigation_slug or None,
            "fix_type": fix_type,
            "operator": operator_fqn,
            "signature": suggestion.get("signature") or "",
            "depth": mitigation_depth + 1,
            "status": "submitted",
            "created_at": datetime.now(timezone.utc),
            # Captured so the trainer can credit the parent's actions
            # retroactively when the child succeeds (task #60).
            "parent_action_ids": [int(a) for a in parent_action_ids if isinstance(a, (int, float))],
            "parent_dataset_embedding": list(parent_embedding),
        })
    except Exception:
        pass

    await aemit(Event("RLMitigationApplied", {
        "source": "rl_error_mitigation",
        "parent_pipeline_id": parent_pipeline_id,
        "child_pipeline_id": child_pipeline_id,
        "pattern_id": pattern_id,
        "fix_type": fix_type,
        "mitigation_slug": mitigation_slug or None,
        "depth": mitigation_depth + 1,
        "session": session,
    }))


async def handle_rl_mitigation_child_completed(event: Event) -> None:
    """Flip a parent's mitigation attempt to ``child_succeeded`` /
    ``child_failed`` so the trainer can credit the parent's actions.

    Subscribes to ``PipelineRunCompleted`` AND ``PipelineRunFailed``
    on ``rl:`` sessions. Looks the run's pipeline up in
    ``rl_mitigation_attempts.child_pipeline_id``; if a row exists and
    its status is still ``submitted`` we update it. Anything else is
    a no-op (regular RL runs without a parent attempt).

    The trainer's :func:`rl.train.partial_credit.drain_partial_credits`
    consumes ``child_succeeded`` rows at batch start.
    """
    session = event.data.get("session", "")
    if not session.startswith("rl:"):
        return
    child_pipeline_id = event.data.get("pipeline_id", "")
    if not child_pipeline_id:
        return
    succeeded = event.type == "PipelineRunCompleted"
    new_status = "child_succeeded" if succeeded else "child_failed"
    try:
        from backend.envs import expdb
        await expdb.rl_mitigation_attempts.update_one(
            {"child_pipeline_id": child_pipeline_id, "status": "submitted"},
            {"$set": {"status": new_status, "completed_at": datetime.now(timezone.utc)}},
        )
    except Exception as exc:  # noqa: BLE001
        await aemit_bg(Event("RLMitigationStatusUpdateFailed", {
            "child_pipeline_id": child_pipeline_id,
            "error": str(exc),
        }))


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _resolve_operator_fqn(dag: DAG, node_id: str, fallback: str) -> str:
    """Return the operator class FQN for a failing node.

    The executor reports errors against compound-expanded sub-nodes whose
    ids look like ``{base}_cx_{method}_{idx}``. The parent DAG (pre-expansion)
    holds the real ``Operator`` node at the ``{base}`` id, with ``.name``
    set to the class FQN. We strip the ``_cx_`` suffix and look up the
    base node. If that fails and *fallback* contains a dotted path, use it.
    """
    from dorian.dag import Operator as _Operator

    base = node_id
    if "_cx_" in base:
        base = base[: base.index("_cx_")]

    node = dag.nodes.get(base)
    if isinstance(node, _Operator) and node.name:
        return node.name

    # Last resort: use the raw suggestion operator field if it looks dotted.
    if fallback and "." in fallback and not fallback.startswith("node_"):
        return fallback
    return ""


async def _load_parent_dag(pipeline_id: str) -> DAG | None:
    """Fetch the parent pipeline's DAG from the docstore by logical id."""
    try:
        from backend.envs import expdb
        doc = await expdb.pipelines.find_one(
            {"pipeline_id": pipeline_id},
            projection={"nodes": 1, "edges": 1},
        )
        if not doc:
            # Some upsert paths key by ``_id`` instead of ``pipeline_id``.
            doc = await expdb.pipelines.find_one(
                {"_id": pipeline_id},
                projection={"nodes": 1, "edges": 1},
            )
        if not doc:
            return None
        from dorian.pipeline.execution import _parse_pipeline
        return _parse_pipeline({"nodes": doc["nodes"], "edges": doc["edges"]})
    except Exception:
        return None


_RL_SESSION_DATASET_RE = re.compile(r"^rl:[^:]+:(?P<did>[A-Fa-f0-9]+)$")


def _dataset_from_session(session: str) -> str | None:
    """Extract the dataset id from an ``rl:round-N:<did>`` session name.

    Falls back to ``None`` when the session does not match the expected
    shape — the caller will emit a skip event then.
    """
    m = _RL_SESSION_DATASET_RE.match(session or "")
    return m.group("did") if m else None
