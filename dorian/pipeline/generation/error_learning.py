"""Error-pattern-aware priors for the RL action selector.

The ``execution_error_instances`` docstore collection is the RL agent's
experience buffer for failure modes.  Every failed pipeline run writes
a row with ``{operator, error_signature, pattern_id, session, ...}``.
The agent reads those rows to build a per-dataset failure-frequency
prior over the operator catalog: operators that have repeatedly failed
on this dataset get down-weighted or hard-masked at action selection.

This is what makes the error corpus first-class in learning — not just
a backwards-looking list of what broke, but a forward-looking signal
that shapes the next episode's action space.

Public API
----------

``get_failure_priors(dataset_id, window_s) -> dict[op_fqn, FailureStat]``
    Query the error corpus for one dataset and summarise per operator.

``invalid_ops_for_dataset(dataset_id, catalog_ops, ...) -> set[str]``
    Derive the hard-mask set for use in ``PipelineGenEnv.action_masks``.

Both functions are async — docstore reads use motor.  Callers typically
invoke them once per episode ``reset`` and cache the result in the
environment state.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Iterable
import re


@dataclass(slots=True)
class FailureStat:
    """Per-operator failure summary on a single dataset."""
    operator: str
    failures: int
    distinct_signatures: int
    last_error_preview: str


_DEFAULT_WINDOW_S = 86_400        # 24 hours — recent enough to reflect current code, KB, data
_DEFAULT_MIN_FAILURES = 3         # ignore one-off failures — they may be flaky
_DEFAULT_MAX_DISTINCT_FIXES = 1   # after the N-th distinct failure signature, assume the op is broken on this data

# Budget for the failure-corpus query. The RL batch path and the
# recommendation fast-path both call into this module during latency-
# sensitive flows (session seed, batch start). 500ms is enough for a
# well-indexed docstore query to return, and short enough that a slow
# query fails open instead of stalling the session view.
_QUERY_TIMEOUT_S = 0.5

# In-process TTL cache: a given dataset's failure stats change slowly
# (only when a new pipeline runs and writes to the corpus). Caching
# for 60s means repeat session seeds within the same minute hit
# memory instead of the docstore. Value is (deadline_monotonic, result).
_CACHE_TTL_S = 60.0
_stats_cache: dict[tuple, tuple[float, dict[str, "FailureStat"]]] = {}


def _session_pattern_for_dataset(dataset_id: str) -> re.Pattern:
    """Regex that matches RL session strings for a given dataset.

    The scheduler forms sessions as ``rl:round-{N}:{did[:8]}``. We
    match the 8-char prefix to catch failures from any round of RL
    generation that targeted this dataset.
    """
    prefix = dataset_id[:8] if dataset_id else ""
    if not prefix:
        return re.compile(r"^rl:round-\d+:")
    return re.compile(rf"^rl:round-\d+:{re.escape(prefix)}")


async def _query_failures(
    dataset_id: str,
    window_s: float,
    only_operators: tuple[str, ...] | None,
) -> dict[str, FailureStat]:
    """Run the actual docstore query without caching / timeout.

    Uses the exact-match ``dataset_id`` field (populated by
    ``_persist_error_instance``) so the compound index on
    ``(dataset_id, created_at, operator)`` serves the query with
    an index-only scan. Falls back to a session-regex match for
    legacy rows that were written before the dataset_id field was
    added — those rows should be uncommon once the index and writer
    are both deployed, and the regex stays only as belt-and-braces.
    """
    try:
        from backend.envs import expdb
        from datetime import datetime, timezone, timedelta
    except Exception:
        return {}

    since = datetime.now(timezone.utc) - timedelta(seconds=window_s)
    # Primary path: exact dataset_id match on the indexed field.
    did_prefix = dataset_id[:8] if dataset_id else ""
    query: dict = {
        "$or": [
            {"dataset_id": did_prefix},
            # Legacy fallback: rows written before dataset_id denorm.
            {"session": {"$regex": _session_pattern_for_dataset(dataset_id).pattern}},
        ],
        "created_at": {"$gte": since},
    }
    if only_operators is not None:
        if not only_operators:
            return {}
        query["operator"] = {"$in": list(only_operators)}

    stats: dict[str, dict] = {}
    try:
        async for doc in expdb.execution_error_instances.find(
            query,
            projection={"operator": 1, "signature": 1, "error_first_line": 1},
        ):
            op = doc.get("operator") or ""
            if not op:
                continue
            entry = stats.setdefault(op, {"failures": 0, "signatures": set(), "preview": ""})
            entry["failures"] += 1
            sig = doc.get("signature")
            if sig:
                entry["signatures"].add(sig)
            preview = doc.get("error_first_line") or ""
            if preview:
                entry["preview"] = preview[:200]
    except Exception:
        return {}

    return {
        op: FailureStat(
            operator=op,
            failures=entry["failures"],
            distinct_signatures=len(entry["signatures"]),
            last_error_preview=entry["preview"],
        )
        for op, entry in stats.items()
    }


async def get_failure_priors(
    dataset_id: str,
    window_s: float = _DEFAULT_WINDOW_S,
    *,
    only_operators: Iterable[str] | None = None,
) -> dict[str, FailureStat]:
    """Summarise recent failures per operator for one dataset.

    Runs the docstore query behind:
      * a 60s in-process TTL cache keyed by (dataset_id, window_s,
        operator-set), so repeat calls from the same session-seed
        burst (DataProfiled + DataScienceTaskSelected +
        EvaluationProcedureSelected all enter within the same second)
        hit memory after the first call.
      * a 500ms timeout (``_QUERY_TIMEOUT_S``), because the
        user-facing recommendation path calls into this during
        session seeding. A slow/unindexed corpus must never stall
        the session view — fail open instead.

    Returns an empty dict on docstore unavailable, timeout, or no
    matching rows. The RL agent / recommendation filter then
    proceeds without error-based masking, which is the safe
    default.
    """
    ops_tuple: tuple[str, ...] | None = (
        tuple(sorted(only_operators)) if only_operators is not None else None
    )
    cache_key = (dataset_id, window_s, ops_tuple)

    # Cache hit?
    now = time.monotonic()
    cached = _stats_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    # Cache miss — query docstore with a hard timeout.
    try:
        result = await asyncio.wait_for(
            _query_failures(dataset_id, window_s, ops_tuple),
            timeout=_QUERY_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, Exception):
        # Fail open: cache an empty result for a SHORT period to avoid
        # hammering a slow docstore over and over during the same burst,
        # but short enough that the next real user interaction gets a
        # fresh attempt once load subsides.
        _stats_cache[cache_key] = (now + 5.0, {})
        return {}

    _stats_cache[cache_key] = (now + _CACHE_TTL_S, result)
    return result


async def invalid_ops_for_dataset(
    dataset_id: str,
    catalog_ops: Iterable[str],
    *,
    window_s: float = _DEFAULT_WINDOW_S,
    min_failures: int = _DEFAULT_MIN_FAILURES,
    max_distinct_fixes: int = _DEFAULT_MAX_DISTINCT_FIXES,
) -> tuple[set[str], dict[str, FailureStat]]:
    """Derive the hard-mask set of operators to avoid on this dataset.

    An operator is masked when:
      * it has at least ``min_failures`` recorded failures on this
        dataset within the window, AND
      * those failures share few enough distinct signatures that the
        agent has already seen the same kind of break repeatedly
        (``distinct_signatures > max_distinct_fixes`` keeps an op
        that fails in many different ways unmasked — it might still
        succeed on a different code path; only converging-failure ops
        are masked).

    The threshold lets the operator stay in the action space long
    enough for mitigation attempts to try and fix the pattern (the
    ``rl_error_mitigation`` loop inserts a rewrite and resubmits). If
    the same error persists past ``min_failures`` attempts, the
    mitigation isn't helping for this data — remove the op entirely
    so the agent explores alternatives.

    Returns ``(masked_set, all_stats)``. The full stats are returned
    alongside the mask so observability events can report not just
    "what was masked" but "why".
    """
    stats = await get_failure_priors(
        dataset_id, window_s=window_s, only_operators=catalog_ops,
    )
    masked: set[str] = set()
    for op, s in stats.items():
        if s.failures >= min_failures and s.distinct_signatures <= max_distinct_fixes:
            masked.add(op)
    return masked, stats
