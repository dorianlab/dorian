"""
rl/policy/persistence.py
------------------------
Save / load learned policy state across RL trainer container restarts.

The training loop accumulates per-action statistics (MemoryPolicy) and
multiplicative-weights (HedgePolicy) over hundreds of episodes — ~2%
valid-pipeline rate only emerges after several batches of warm-up.
Losing that on every deploy is the dominant cold-start tax, so persist
it to disk and restore on startup.

What's persisted:

  * ``MemoryPolicy._stats`` — ``{action_id: [ActionEpisodeStat, ...]}``
  * ``HedgePolicy._log_weights`` — ``{action_id: float}``

What is NOT persisted (recreated fresh on load):

  * ``random.Random`` instances — replaying the same seed from a
    different trajectory point is worse than fresh randomness.
  * ``threading.Lock`` — must be current-process local.
  * Action space / catalog references — caller binds these at init.

Format: a single pickle file keyed by policy class name so mismatched
types are rejected cleanly. Atomic write via ``.tmp`` + ``os.replace``.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def save_policy(policy: Any, path: Path) -> bool:
    """Persist *policy*'s learned state to *path*. Returns True on success.

    Never raises — a persistence failure must not crash the trainer;
    worst case the next restart loses a batch or two of learning.
    """
    try:
        payload = {"type": type(policy).__name__, "state": _extract(policy)}
    except Exception:
        _log.exception("policy state extraction failed; skipping save")
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        return True
    except Exception:
        _log.exception("policy state write failed")
        return False


def load_policy_into(policy: Any, path: Path) -> bool:
    """Load state from *path* into the live *policy*. Returns True if applied.

    Returns False (no-op) when the file is missing, unreadable, or the
    serialised policy type doesn't match — a type mismatch typically
    means config changed (hybrid → memory-only, etc.) and reusing the
    old state would degrade behaviour.
    """
    if not path.exists():
        return False
    try:
        with path.open("rb") as f:
            payload = pickle.load(f)
    except Exception:
        _log.exception("policy state read failed at %s", path)
        return False

    expected = type(policy).__name__
    if payload.get("type") != expected:
        _log.info(
            "policy state on disk is %s but runtime policy is %s — ignoring",
            payload.get("type"), expected,
        )
        return False

    try:
        _apply(policy, payload.get("state") or {})
    except Exception:
        _log.exception("policy state apply failed")
        return False
    return True


# ---------------------------------------------------------------------------
# Per-policy extract / apply — one branch per policy kind. Keeping the
# branches explicit (rather than a generic __getstate__) means new policy
# types opt in deliberately and old snapshots don't silently pollute new
# fields.
# ---------------------------------------------------------------------------

def _extract(policy: Any) -> dict:
    from rl.policy.hybrid_policy import HybridPolicy
    from rl.policy.memory_policy import MemoryPolicy
    from rl.policy.hedge_policy import HedgePolicy

    if isinstance(policy, HybridPolicy):
        return {
            "memory": _extract(policy.memory),
            "hedge": _extract(policy.hedge),
        }
    if isinstance(policy, MemoryPolicy):
        return {"stats": dict(policy._stats)}
    if isinstance(policy, HedgePolicy):
        return {"log_weights": dict(policy._log_weights)}
    return {}


def _apply(policy: Any, state: dict) -> None:
    from rl.policy.hybrid_policy import HybridPolicy
    from rl.policy.memory_policy import MemoryPolicy
    from rl.policy.hedge_policy import HedgePolicy

    if isinstance(policy, HybridPolicy):
        _apply(policy.memory, state.get("memory") or {})
        _apply(policy.hedge, state.get("hedge") or {})
        return
    if isinstance(policy, MemoryPolicy):
        stats = state.get("stats") or {}
        # Don't replace the dict — MemoryPolicy may hold a reference
        # elsewhere (e.g. the warm-start routine captured the
        # instance). Mutate in place under the policy's own lock.
        with policy._stats_lock:
            policy._stats.clear()
            policy._stats.update(stats)
        return
    if isinstance(policy, HedgePolicy):
        weights = state.get("log_weights") or {}
        with policy._weights_lock:
            policy._log_weights.clear()
            policy._log_weights.update(weights)
        return
