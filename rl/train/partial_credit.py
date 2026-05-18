"""Drain pending partial-credit signals into the live policy.

When the AI Debugger's auto-mitigation loop turns a failing parent
pipeline into a working child (see
``dorian/event/handlers/rl_error_mitigation.py``), the parent's
trajectory was already absorbed by the trainer as a failure. The
child's success is a delayed signal that the parent's action choices
were "almost right" — the missing bit was a single localised bug
the rewrite resolved.

This module reconciles that delayed signal:

  1. ``rl_mitigation_attempts`` rows persist ``parent_action_ids``
     + ``parent_dataset_embedding`` at submission time.
  2. The trainer calls :func:`drain_partial_credits` at batch start.
  3. We pick up every attempt where ``status='child_succeeded'`` and
     ``credited`` is missing, call
     ``policy.credit_partial_success(action_ids, embedding, factor)``,
     and flip ``credited=True`` so each attempt is applied at most
     once across batches / restarts.

A ``status`` flip from ``submitted`` → ``child_succeeded`` happens
in ``handle_rl_mitigation_child_completed`` (subscribed to
``PipelineRunCompleted`` for ``rl:`` sessions). That side reads
the child's terminal reward; if it crossed the success threshold,
status becomes ``child_succeeded`` — otherwise ``child_failed``
(no credit). The trainer here only consumes ``child_succeeded``.
"""
from __future__ import annotations

import logging
from typing import Iterable

_log = logging.getLogger(__name__)


# Default factor — a half-success per action. Tunable via env var so
# ablations can sweep without touching code.
import os
DEFAULT_FACTOR = float(os.environ.get("DORIAN_RL_PARTIAL_CREDIT_FACTOR", "0.5"))


async def drain_partial_credits(policy, *, factor: float = DEFAULT_FACTOR) -> int:
    """Apply pending partial-credit attempts to ``policy`` in-place.

    Returns the number of attempts credited. Idempotent — flipping
    ``credited=True`` after each application means a re-drain is a
    no-op until a new attempt's child succeeds.

    The policy must expose ``credit_partial_success(action_ids,
    embedding, factor=...)``; ``MemoryPolicy`` and ``HybridPolicy``
    do (the hybrid forwards to its inner memory). Other policies
    (pure ``HedgePolicy``) are skipped silently.
    """
    if not hasattr(policy, "credit_partial_success") and not _has_inner_memory(policy):
        return 0

    try:
        from backend.envs import expdb
    except Exception:
        return 0

    cursor = expdb.rl_mitigation_attempts.find({
        "status": "child_succeeded",
        "credited": {"$exists": False},
    })
    rows = await cursor.to_list(length=1000)
    if not rows:
        return 0

    n_applied = 0
    for row in rows:
        action_ids = [int(a) for a in (row.get("parent_action_ids") or []) if a is not None]
        embedding = tuple(float(x) for x in (row.get("parent_dataset_embedding") or []))
        if not action_ids or not embedding:
            # Missing data — mark as credited so we don't keep
            # retrying. Caller already chose to log the gap upstream.
            await expdb.rl_mitigation_attempts.update_one(
                {"_id": row["_id"]}, {"$set": {"credited": True}}
            )
            continue
        target = policy if hasattr(policy, "credit_partial_success") else getattr(policy, "memory", None)
        if target is None:
            continue
        target.credit_partial_success(action_ids, embedding, factor=factor)
        await expdb.rl_mitigation_attempts.update_one(
            {"_id": row["_id"]}, {"$set": {"credited": True}}
        )
        n_applied += 1

    if n_applied:
        _log.info("partial-credit drain: applied %d attempts (factor=%.2f)", n_applied, factor)
    return n_applied


def _has_inner_memory(policy) -> bool:
    """``HybridPolicy`` composes a ``MemoryPolicy`` as ``self.memory``."""
    inner = getattr(policy, "memory", None)
    return inner is not None and hasattr(inner, "credit_partial_success")
