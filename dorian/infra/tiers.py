"""User tier model for priority-based resource allocation.

Each user has a tier that determines their queue priority, rate limits,
and resource allocation.  Tiers are stored in Redis and cached per-session.

Design:
    - ``free`` is the default tier (no special treatment).
    - Higher tiers get numerically lower (= higher priority) ZADD scores
      in the pipeline execution queue — they run first.
    - The tier system is additive: a higher tier never *loses* capabilities,
      it only gains faster queue advancement and larger quotas.

Usage::

    from dorian.infra.tiers import get_user_tier, tier_priority

    tier = await get_user_tier(uid)       # "free" | "standard" | "priority" | "enterprise"
    score = tier_priority(tier)           # -10 (free) ... -40 (enterprise)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class TierLevel(IntEnum):
    """Numeric tier levels — higher number = higher tier."""
    FREE = 0
    STANDARD = 1
    PRIORITY = 2
    ENTERPRISE = 3


@dataclass(frozen=True, slots=True)
class TierConfig:
    """Configuration for a single user tier."""

    name: str
    level: TierLevel
    queue_priority: float
    """ZADD score for the pipeline execution queue.

    Redis sorted sets pop lowest scores first (ZPOPMIN), so lower = higher
    priority.  We use negative values: -10 (free) to -40 (enterprise).
    """

    max_concurrent_pipelines: int
    """Maximum number of pipelines this user can run simultaneously."""

    rate_limit_multiplier: float
    """Multiplier applied to base rate limits (1.0 = standard)."""

    label: str
    """Human-readable label for the UI (e.g. 'Free', 'Priority')."""


# ── Tier definitions ──────────────────────────────────────────────────────────

TIERS: dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        level=TierLevel.FREE,
        queue_priority=-10.0,
        max_concurrent_pipelines=2,
        rate_limit_multiplier=1.0,
        label="Free",
    ),
    "standard": TierConfig(
        name="standard",
        level=TierLevel.STANDARD,
        queue_priority=-20.0,
        max_concurrent_pipelines=4,
        rate_limit_multiplier=1.5,
        label="Standard",
    ),
    "priority": TierConfig(
        name="priority",
        level=TierLevel.PRIORITY,
        queue_priority=-30.0,
        max_concurrent_pipelines=8,
        rate_limit_multiplier=2.0,
        label="Priority",
    ),
    "enterprise": TierConfig(
        name="enterprise",
        level=TierLevel.ENTERPRISE,
        queue_priority=-40.0,
        max_concurrent_pipelines=16,
        rate_limit_multiplier=3.0,
        label="Enterprise",
    ),
}

DEFAULT_TIER = "free"


def get_tier_config(tier_name: str) -> TierConfig:
    """Return the config for *tier_name*, falling back to ``free``."""
    return TIERS.get(tier_name, TIERS[DEFAULT_TIER])


def tier_priority(tier_name: str) -> float:
    """Return the ZADD queue priority score for *tier_name*."""
    return get_tier_config(tier_name).queue_priority


# ── Redis-backed tier storage ─────────────────────────────────────────────────

_TIER_KEY_PREFIX = "user:tier:"


async def get_user_tier(uid: str) -> str:
    """Look up the user's tier from Redis.  Returns ``"free"`` if unset."""
    try:
        from backend.envs import aioredis
        raw = await aioredis.get(f"{_TIER_KEY_PREFIX}{uid}")
        if raw and raw in TIERS:
            return raw
    except Exception:
        pass
    return DEFAULT_TIER


async def set_user_tier(uid: str, tier_name: str) -> None:
    """Set the user's tier in Redis.  Validates the tier name."""
    if tier_name not in TIERS:
        raise ValueError(f"Unknown tier {tier_name!r}. Available: {list(TIERS)}")
    from backend.envs import aioredis
    await aioredis.set(f"{_TIER_KEY_PREFIX}{uid}", tier_name)


def get_user_tier_sync(uid: str) -> str:
    """Synchronous tier lookup (for Dask workers / sync contexts)."""
    try:
        from backend.envs import redis
        raw = redis.get(f"{_TIER_KEY_PREFIX}{uid}")
        if raw and raw in TIERS:
            return raw
    except Exception:
        pass
    return DEFAULT_TIER
