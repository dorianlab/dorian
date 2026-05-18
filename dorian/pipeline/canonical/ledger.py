"""In-memory rewrite-observation ledger + storage interface.

Records each time the AI Debugger applies a rewrite rule to a
pipeline. Aggregated into per-(source_class, rule_id) statistics
that the promotion worker reads to decide which source classes
have stable canonical forms.

The interface is deliberately thin so the docstore-backed live
implementation (follow-up) can drop in without touching call
sites. Tests run against the in-memory ``MemoryLedger``.

See (internal design note; not in public repo) for schema + design.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class RewriteObservation:
    """One observed rewrite firing."""

    rule_id: str
    source_class_hash: str
    target_class_hash: str
    pipeline_id: str = ""
    session_id: str = ""
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SourceStats:
    """Aggregated stats for a (source_class_hash, rule_id) pair."""

    source_class_hash: str
    rule_id: str
    observations: int
    target_hit_counts: dict[str, int]

    @property
    def total_target_hits(self) -> int:
        return sum(self.target_hit_counts.values())

    def dominant_target(self) -> tuple[str, int] | None:
        """(target_hash, count) of the most-frequently-produced
        target class, or None if no targets recorded."""
        if not self.target_hit_counts:
            return None
        return max(self.target_hit_counts.items(), key=lambda kv: kv[1])

    def hit_rate_for(self, target_hash: str) -> float:
        """Fraction of source-class observations that rewrote to
        ``target_hash``."""
        if self.observations == 0:
            return 0.0
        return self.target_hit_counts.get(target_hash, 0) / self.observations


@runtime_checkable
class RewriteLedger(Protocol):
    """Storage interface for rewrite observations."""

    def record(self, obs: RewriteObservation) -> None:
        ...

    def stats_for(
        self, source_class_hash: str, rule_id: str
    ) -> SourceStats:
        ...

    def all_source_stats(self) -> Iterable[SourceStats]:
        ...


# ---------------------------------------------------------------------------
# In-memory implementation (tests + dry-run)
# ---------------------------------------------------------------------------

class MemoryLedger:
    """All-in-memory RewriteLedger. Suitable for tests + local
    experimentation; production path uses a docstore-backed
    implementation under the same protocol."""

    def __init__(self) -> None:
        # {(src, rule): {target: count}} + a parallel observation count.
        self._target_counts: dict[tuple[str, str], dict[str, int]] = (
            defaultdict(lambda: defaultdict(int))
        )
        self._obs_counts: dict[tuple[str, str], int] = defaultdict(int)

    def record(self, obs: RewriteObservation) -> None:
        key = (obs.source_class_hash, obs.rule_id)
        self._obs_counts[key] += 1
        self._target_counts[key][obs.target_class_hash] += 1

    def stats_for(
        self, source_class_hash: str, rule_id: str
    ) -> SourceStats:
        key = (source_class_hash, rule_id)
        targets = dict(self._target_counts.get(key, {}))
        return SourceStats(
            source_class_hash=source_class_hash,
            rule_id=rule_id,
            observations=self._obs_counts.get(key, 0),
            target_hit_counts=targets,
        )

    def all_source_stats(self) -> Iterable[SourceStats]:
        for (src, rule), obs_count in self._obs_counts.items():
            targets = dict(self._target_counts.get((src, rule), {}))
            yield SourceStats(
                source_class_hash=src,
                rule_id=rule,
                observations=obs_count,
                target_hit_counts=targets,
            )

    def __len__(self) -> int:
        return sum(self._obs_counts.values())


__all__ = [
    "MemoryLedger",
    "RewriteLedger",
    "RewriteObservation",
    "SourceStats",
]
