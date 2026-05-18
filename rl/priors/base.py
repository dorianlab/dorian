"""Prior source interface + null default.

Prior sources recommend catalog op_keys the policy should bias toward
at the start of an episode, based on the :class:`DatasetProfile`.
Backends are swappable; the trainer never takes a hard dependency on
any of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .profile import DatasetProfile


@dataclass(frozen=True)
class PriorRecommendation:
    """One catalog op_key the source thinks the agent should prefer.

    ``weight`` is the mask-level suggestion_weight multiplier to
    apply to candidates matching this op_key. Default 5.0 puts the
    recommendation mid-way between ``1.0`` (no hint) and ``100.0``
    (the semantic-name match that fires deterministically). Prior
    sources can return stronger weights for confident calls.
    """

    op_key: str
    reason: str = ""
    weight: float = 5.0


class PriorSource(Protocol):
    """Protocol for pre-episode prior sources.

    Implementations must be idempotent + side-effect-free (the
    trainer may call ``recommend`` repeatedly on the same profile).
    Failures should degrade to ``[]`` rather than raise — the
    trainer must never break on a misconfigured backend.
    """

    def recommend(self, profile: DatasetProfile) -> list[PriorRecommendation]:
        ...


class NullPriorSource:
    """Default backend when nothing else is configured. Returns no
    recommendations; the mask falls back to its baseline behaviour
    (semantic-match + warm-start only). System works end-to-end
    without any LLM."""

    def recommend(self, profile: DatasetProfile) -> list[PriorRecommendation]:
        return []


__all__ = ["NullPriorSource", "PriorRecommendation", "PriorSource", "DatasetProfile"]
