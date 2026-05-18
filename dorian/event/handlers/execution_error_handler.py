"""
dorian/event/handlers/execution_error_handler.py
--------------------------------------------------
Handles ``NodeExecutionFailed`` events by matching error messages against
known patterns from the execution error KB
(``dorian.knowledge.sources.execution_errors``).

When a match is found the handler emits a suggestion to the frontend that
offers to fix the offending parameter value.  The suggestion flows through
the existing suggestion card UI; when accepted, ``apply_mitigation`` in
``risk_checks.py`` handles the ``fix_type: "parameter_change"`` branch to
rewrite the DAG.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from backend.events import Event, aemit, aemit_bg
from backend.envs import aioredis
from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
from dorian.knowledge.execution_errors import (
    EXECUTION_ERROR_PATTERNS,
    ExecutionErrorPattern,
)


def _is_rl(uid: str, session: str) -> bool:
    """True when the event originated from the RL generator.

    RL runs use ``uid='system'`` and ``session='rl:round-N:did...'``.
    They drive their own auto-mitigation loop (``rl_error_mitigation``)
    and don't consume user-facing suggestion cards, so a number of
    expensive side-effects can be short-circuited or demoted to
    fire-and-forget for them.
    """
    return uid == "system" and isinstance(session, str) and session.startswith("rl:")


# Redis key template for the per-run suggestion index. Used by the RL
# auto-mitigation loop to look up which suggestion (if any) was emitted for
# a given failing run.
_RUN_SUGGESTION_KEY = "execution_error:run:{run_id}:suggestion"
_RUN_SUGGESTION_TTL = 3600  # 1h — suggestion is only useful during the retry window


def _error_signature(operator_fqn: str, error_msg: str) -> str:
    """Stable 16-char fingerprint of *(operator, first error line)*.

    Used both to key the docstore corpus (so duplicates aggregate per-operator
    error category) and to let the RL mitigation loop look up suggestions by
    their natural grouping.
    """
    first_line = (error_msg or "").strip().splitlines()[0] if error_msg else ""
    blob = f"{operator_fqn}|{first_line}".encode("utf-8", errors="replace")
    return hashlib.sha1(blob).hexdigest()[:16]


async def _persist_error_instance(
    *,
    signature: str,
    operator_fqn: str,
    error_msg: str,
    trace: str,
    node_id: str,
    run_id: str,
    session: str,
    pattern_id: str | None,
    fix_type: str | None,
    mitigation_slug: str | None,
) -> None:
    """Append this failure to the data-driven ``execution_error_instances``
    corpus. The Debugger uses this corpus to:

      * curate new mitigations for unmatched errors (pattern_id=None),
      * measure mitigation success rate per signature, and
      * give the RL agent an invalid-action prior derived from real failures.

    Writes are fire-and-forget; a persistence failure must never block
    suggestion emission.
    """
    try:
        from backend.envs import expdb
        # Extract the dataset id up front so the error-learning query
        # path can use an exact-match index instead of a regex scan on
        # the session string. RL sessions are ``rl:round-N:{did[:8]}``;
        # user sessions don't carry the dataset id so we leave it None.
        dataset_id: str | None = None
        if isinstance(session, str) and session.startswith("rl:"):
            parts = session.split(":")
            if len(parts) >= 3:
                dataset_id = parts[2]
        doc = {
            "signature": signature,
            "operator": operator_fqn,
            "node_id": node_id,
            "run_id": run_id,
            "session": session,
            "dataset_id": dataset_id,
            "pattern_id": pattern_id,
            "fix_type": fix_type,
            "mitigation_slug": mitigation_slug,
            "error_first_line": (error_msg or "").strip().splitlines()[0][:500]
                if error_msg else "",
            "error_preview": (error_msg or "")[:2000],
            "trace_tail": (trace or "")[-2000:],
            "created_at": datetime.now(timezone.utc),
        }
        await expdb.execution_error_instances.insert_one(doc)
    except Exception as exc:
        await aemit(Event("ExecutionErrorPersistFailed", data={
            "source": "execution_error_handler._persist_error_instance",
            "signature": signature,
            "operator": operator_fqn,
            "error": str(exc)[:300],
        }))


async def handle_node_execution_failed(event: Event) -> None:
    """Match execution errors against known patterns and emit fix suggestions.

    Subscribes to ``NodeExecutionFailed`` (emitted from
    ``dorian.pipeline.run_state._instrument``).

    Flow:
      1. Extract error message + node context from event payload.
      2. Iterate ``EXECUTION_ERROR_PATTERNS`` for a regex match.
      3. Verify operator affinity (if the pattern restricts to certain FQNs).
      4. Compute the concrete fix value from regex capture groups.
      5. Emit a suggestion card to the frontend Redis stream.
    """
    uid = event.data.get("uid", "")
    session = event.data.get("session", "")
    run_id = event.data.get("run_id", "")
    node_id = event.data.get("node_id", "")
    error_msg = event.data.get("error", "")
    trace = event.data.get("trace", "")

    if not error_msg or not session:
        return

    # Resolve operator FQN from the node_id convention:
    # node_id format: "node_{operator_short}_{index}" or full FQN embedded.
    # We also check the trace for dotted names.
    operator_fqn = _extract_operator_fqn(node_id, trace)
    is_rl = _is_rl(uid, session)

    # Every failure — matched or not — feeds the data-driven corpus the
    # Debugger grows over time. Unmatched instances become curation seeds;
    # matched ones let us measure mitigation success rate per signature.
    # For RL runs: fire-and-forget so we don't block this handler's return
    # on downstream ExecutionErrorCaptured subscribers (observability,
    # corpus indexers). The event still reaches them via the bg lane.
    signature = _error_signature(operator_fqn or node_id, error_msg)
    _emit_chain = aemit_bg if is_rl else aemit
    await _emit_chain(Event("ExecutionErrorCaptured", data={
        "source": "execution_error_handler",
        "signature": signature,
        "operator": operator_fqn,
        "node_id": node_id,
        "run_id": run_id,
        "session": session,
        "error_preview": (error_msg or "")[:300],
    }))

    for pattern_entry in EXECUTION_ERROR_PATTERNS:
        match = pattern_entry.pattern.search(error_msg)
        if not match:
            # Also try matching against the full traceback
            match = pattern_entry.pattern.search(trace) if trace else None
        if not match:
            continue

        # Operator affinity check
        if pattern_entry.operators and operator_fqn:
            if not _operator_matches(operator_fqn, pattern_entry.operators):
                continue

        # Compute the fix value (parameter_change only — structural
        # rewrites don't carry a single value).
        fix_value = (
            pattern_entry.compute_fix(match, error_msg)
            if pattern_entry.fix_type == "parameter_change"
            else ""
        )

        # Build description with template substitution. ``sample`` is the
        # capture group used by the structural-encoder pattern; others use
        # ``requested`` / ``n_features`` etc. Missing keys degrade to "".
        template_ctx = defaultdict(str, **match.groupdict(), fix_value=fix_value)
        description_short = pattern_entry.description_short.format_map(template_ctx)
        description_long = pattern_entry.description_long.format_map(template_ctx)

        suggestion_id = str(uuid4())
        # Skip the WS-facing suggestion-card emission for RL runs. The
        # Redis stream ``{uid}:{session}:stream`` for a system/rl:... session
        # has no subscriber — nobody reads it. The RL auto-mitigation loop
        # consumes the Redis suggestion index written below, not the card.
        if not is_rl:
            await _emit_execution_error_suggestion(
                suggestion_id=suggestion_id,
                uid=uid,
                session=session,
                run_id=run_id,
                node_id=node_id,
                operator=operator_fqn or node_id,
                pattern_entry=pattern_entry,
                fix_value=fix_value,
                description_short=description_short,
                description_long=description_long,
                error_msg=error_msg,
            )

        # Persist the corpus row (matched branch).
        await _persist_error_instance(
            signature=signature,
            operator_fqn=operator_fqn,
            error_msg=error_msg,
            trace=trace,
            node_id=node_id,
            run_id=run_id,
            session=session,
            pattern_id=pattern_entry.id,
            fix_type=pattern_entry.fix_type,
            mitigation_slug=pattern_entry.mitigation_slug,
        )

        # Index the suggestion by run_id so the RL auto-mitigation loop
        # (and any future retry flow) can replay it without scanning the
        # stream.
        try:
            await aioredis.set(
                _RUN_SUGGESTION_KEY.format(run_id=run_id),
                json.dumps({
                    "suggestion_id": suggestion_id,
                    "pattern_id": pattern_entry.id,
                    "operator": operator_fqn or node_id,
                    "node_id": node_id,
                    "fix_type": pattern_entry.fix_type,
                    "mitigation_slug": pattern_entry.mitigation_slug,
                    "param_name": pattern_entry.param_name,
                    "fix_value": fix_value,
                    "risk": pattern_entry.risk_name,
                    "description_short": description_short,
                }),
                ex=_RUN_SUGGESTION_TTL,
            )
        except Exception:
            pass  # index is a best-effort accelerator, not source of truth

        # RL: fire-and-forget to avoid serialising the handler on the
        # fan-out of ExecutionErrorMatched subscribers.
        await _emit_chain(Event("ExecutionErrorMatched", data={
            "source": "execution_error_handler",
            "pattern_id": pattern_entry.id,
            "node_id": node_id,
            "operator": operator_fqn or node_id,
            "fix_type": pattern_entry.fix_type,
            "param_name": pattern_entry.param_name,
            "fix_value": fix_value,
            "mitigation_slug": pattern_entry.mitigation_slug,
            "signature": signature,
            "suggestion_id": suggestion_id,
            "run_id": run_id,
        }))

        # First match wins — don't emit multiple suggestions for the same error
        return

    # No pattern matched — still persist to the corpus so curators can add
    # a new pattern for recurring signatures.
    await _persist_error_instance(
        signature=signature,
        operator_fqn=operator_fqn,
        error_msg=error_msg,
        trace=trace,
        node_id=node_id,
        run_id=run_id,
        session=session,
        pattern_id=None,
        fix_type=None,
        mitigation_slug=None,
    )
    await _emit_chain(Event("ExecutionErrorUnmatched", data={
        "source": "execution_error_handler",
        "node_id": node_id,
        "operator": operator_fqn or "",
        "signature": signature,
        "error_preview": error_msg[:200],
        "run_id": run_id,
    }))


def _extract_operator_fqn(node_id: str, trace: str) -> str:
    """Best-effort extraction of the operator FQN from node_id or traceback.

    Node IDs in Dorian follow the pattern ``node_{short_name}_{index}``
    where ``short_name`` may be an abbreviated version of the FQN.
    The compound-expanded sub-nodes follow ``node_{short}_{index}_cx_{method}_{sub_idx}``.

    We try:
      1. Look for a dotted FQN in the node_id itself.
      2. Fall back to scanning the traceback for sklearn/pandas-style imports.
    """
    # Direct dotted path in node_id (rare but possible)
    if "." in node_id and not node_id.startswith("node_"):
        return node_id

    # Strip compound expansion suffixes to get base operator name
    base = node_id
    if "_cx_" in base:
        base = base[:base.index("_cx_")]

    # Try to reconstruct from known patterns in the traceback
    if trace:
        # Look for sklearn/pandas/trust_guardrails import paths
        fqn_match = re.search(
            r"(?:sklearn|pandas|trust_guardrails|openrouter)"
            r"(?:\.\w+)+",
            trace,
        )
        if fqn_match:
            return fqn_match.group(0)

    return ""


def _operator_matches(fqn: str, allowed: frozenset[str]) -> bool:
    """Check if the operator FQN matches any entry in the allowed set.

    Supports both exact match and prefix match (e.g. ``sklearn.decomposition``
    matches ``sklearn.decomposition.PCA``).
    """
    if fqn in allowed:
        return True
    for a in allowed:
        if fqn.startswith(a) or a.startswith(fqn):
            return True
    return False


async def _emit_execution_error_suggestion(
    *,
    suggestion_id: str,
    uid: str,
    session: str,
    run_id: str,
    node_id: str,
    operator: str,
    pattern_entry: ExecutionErrorPattern,
    fix_value: str,
    description_short: str,
    description_long: str,
    error_msg: str,
) -> None:
    """Push a suggestion card to the frontend Redis stream.

    The payload is identical for end-users (HITL click to apply) and the RL
    auto-mitigation loop — the only difference is who consumes it. Both
    read ``fix_type`` to pick the rewrite path:

      * ``parameter_change`` — update an existing Parameter node's value
        (handled by ``_apply_parameter_change`` in ``risk_checks.py``).
      * ``structural_rewrite`` — compile and apply the named mitigation
        rewrite from the ``rewrites`` docstore collection
        (handled by ``_apply_structural_execution_rewrite`` in ``risk_checks.py``).
    """
    stream = RedisKeys.stream(uid, session)

    # Human-facing action label depends on the fix type.
    if pattern_entry.fix_type == "structural_rewrite":
        slug = pattern_entry.mitigation_slug or ""
        action_label = f"Apply: {slug.replace('-', ' ').title() or 'Structural Rewrite'}"
    elif pattern_entry.fix_type == "manual":
        # No automatic fix — the suggestion is informational. Label it
        # as "Review" so the UI's "Apply" button renders as a review/ack
        # rather than an executable action.
        action_label = "Review: check upstream wiring"
    else:
        action_label = f"Fix: {pattern_entry.param_name}"

    message = {
        "event": "suggestion",
        "sid": suggestion_id,
        "uid": uid,
        "session": session,
        "task": operator,
        "risk": pattern_entry.risk_name,
        "action": action_label,
        "description_short": description_short,
        "description_long": description_long,
        "alternatives": json.dumps([]),
        "principles": json.dumps([]),
        "checks": json.dumps([]),
        "severity": "high",
        "status": "actionable",
        "source": "execution_error",
        "pipeline_label": "",
        "pipeline_id": "",
        "check_message": error_msg[:500],
        "has_rewrite": "true",
        # Execution error–specific metadata for apply_mitigation
        "fix_type": pattern_entry.fix_type,
        "fix_param_name": pattern_entry.param_name,
        "fix_param_value": fix_value,
        "fix_node_id": node_id,
        "fix_pattern_id": pattern_entry.id,
        "fix_mitigation_slug": pattern_entry.mitigation_slug or "",
        "run_id": run_id,
    }

    await aioredis.xadd(
        stream,
        {str(k): str(v) for k, v in message.items()},
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
