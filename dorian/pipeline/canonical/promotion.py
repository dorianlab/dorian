"""Promotion / demotion decisions over the rewrite ledger.

A pipeline class is promoted to a canonical target when:

  1. ``hit_rate >= PROMOTION_HIT_RATE_THRESHOLD`` (default 0.95)
  2. ``observations >= PROMOTION_MIN_OBSERVATIONS`` (default 20)
  3. The dominant target class is itself stable (recursion
     guard -- if the target is itself a promotion candidate for
     another rewrite rule at similar hit rate, defer).

This module is pure-functional: it reads the ledger and returns
``PromotionDecision``s. The caller applies them to the registry.

See (internal design note; not in public repo) § "Promotion policy".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .ledger import RewriteLedger, SourceStats


PROMOTION_HIT_RATE_THRESHOLD: float = float(
    os.environ.get("DORIAN_CANONICAL_HIT_RATE", "0.95")
)
PROMOTION_MIN_OBSERVATIONS: int = int(
    os.environ.get("DORIAN_CANONICAL_MIN_OBS", "20")
)
# If the target class is also a high-hit-rate source for another
# rule, defer promotion -- either resolve the chain transitively
# (future work) or wait for the target to stabilise.
TARGET_INSTABILITY_THRESHOLD: float = float(
    os.environ.get("DORIAN_CANONICAL_TARGET_INSTABILITY", "0.05")
)


@dataclass(frozen=True)
class PromotionDecision:
    """A single promotion recommendation. The registry writer
    consumes a list of these.

    ``action`` is one of:
      * ``"promote"``  -- add / update the source → target mapping
      * ``"demote"``   -- remove an existing mapping
      * ``"defer"``    -- observed but not yet actionable (recorded
                          for observability; registry unchanged)
    """

    source_class_hash: str
    rule_id: str
    target_class_hash: str
    action: str
    hit_rate: float
    observations: int
    reason: str


def evaluate(
    ledger: RewriteLedger,
    *,
    hit_rate_threshold: float = PROMOTION_HIT_RATE_THRESHOLD,
    min_observations: int = PROMOTION_MIN_OBSERVATIONS,
    target_instability_threshold: float = TARGET_INSTABILITY_THRESHOLD,
) -> list[PromotionDecision]:
    """Walk the ledger; emit promotion decisions for each
    (source, rule) pair that meets or fails the thresholds.

    Thresholds are arguments (not globals reread at call time) so
    tests can parametrise without touching env.
    """
    decisions: list[PromotionDecision] = []
    # Pre-compute: which hashes appear as sources with observation
    # counts; used for the target-stability check.
    source_obs: dict[str, int] = {}
    source_dominant_ratio: dict[str, float] = {}
    for stats in ledger.all_source_stats():
        source_obs[stats.source_class_hash] = max(
            source_obs.get(stats.source_class_hash, 0), stats.observations
        )
        dom = stats.dominant_target()
        if dom is not None:
            rate = stats.hit_rate_for(dom[0])
            source_dominant_ratio[stats.source_class_hash] = max(
                source_dominant_ratio.get(stats.source_class_hash, 0.0),
                rate,
            )

    for stats in ledger.all_source_stats():
        dominant = stats.dominant_target()
        if dominant is None:
            decisions.append(
                PromotionDecision(
                    source_class_hash=stats.source_class_hash,
                    rule_id=stats.rule_id,
                    target_class_hash="",
                    action="defer",
                    hit_rate=0.0,
                    observations=stats.observations,
                    reason="no observations yet",
                )
            )
            continue
        target_hash, hits = dominant
        hit_rate = stats.hit_rate_for(target_hash)

        if stats.observations < min_observations:
            decisions.append(
                PromotionDecision(
                    source_class_hash=stats.source_class_hash,
                    rule_id=stats.rule_id,
                    target_class_hash=target_hash,
                    action="defer",
                    hit_rate=hit_rate,
                    observations=stats.observations,
                    reason=f"only {stats.observations} observations; need {min_observations}",
                )
            )
            continue

        if hit_rate < hit_rate_threshold:
            decisions.append(
                PromotionDecision(
                    source_class_hash=stats.source_class_hash,
                    rule_id=stats.rule_id,
                    target_class_hash=target_hash,
                    action="demote",
                    hit_rate=hit_rate,
                    observations=stats.observations,
                    reason=f"hit rate {hit_rate:.3f} < threshold {hit_rate_threshold}",
                )
            )
            continue

        # Recursion guard: if the target is itself a high-hit-rate
        # source for some other rule, defer.
        target_dom_rate = source_dominant_ratio.get(target_hash, 0.0)
        if target_dom_rate > (1.0 - target_instability_threshold):
            decisions.append(
                PromotionDecision(
                    source_class_hash=stats.source_class_hash,
                    rule_id=stats.rule_id,
                    target_class_hash=target_hash,
                    action="defer",
                    hit_rate=hit_rate,
                    observations=stats.observations,
                    reason=(
                        f"target {target_hash[:8]} is itself unstable "
                        f"(dominant rate {target_dom_rate:.3f}); "
                        "defer until target stabilises"
                    ),
                )
            )
            continue

        decisions.append(
            PromotionDecision(
                source_class_hash=stats.source_class_hash,
                rule_id=stats.rule_id,
                target_class_hash=target_hash,
                action="promote",
                hit_rate=hit_rate,
                observations=stats.observations,
                reason=(
                    f"hit rate {hit_rate:.3f} >= {hit_rate_threshold} "
                    f"over {stats.observations} observations"
                ),
            )
        )
    return decisions


def promotions(
    decisions: Iterable[PromotionDecision],
) -> list[PromotionDecision]:
    """Filter: only `promote` decisions."""
    return [d for d in decisions if d.action == "promote"]


def demotions(
    decisions: Iterable[PromotionDecision],
) -> list[PromotionDecision]:
    """Filter: only `demote` decisions."""
    return [d for d in decisions if d.action == "demote"]


__all__ = [
    "PROMOTION_HIT_RATE_THRESHOLD",
    "PROMOTION_MIN_OBSERVATIONS",
    "PromotionDecision",
    "TARGET_INSTABILITY_THRESHOLD",
    "demotions",
    "evaluate",
    "promotions",
]
