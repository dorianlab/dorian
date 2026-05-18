"""Recommendation-engine substitution against the canonical-form registry.

At recommendation time, for every candidate pipeline:

  1. Compute its structural class hash.
  2. Look up the hash in the canonical-form registry.
  3. If the hash matches a promoted source class, replace the
     candidate with the registry's canonical target pipeline.
  4. Otherwise return the candidate unchanged.

The registry is abstracted as ``CanonicalRegistry`` -- tests use
the in-memory ``DictCanonicalRegistry``; the docstore-backed
production implementation plugs in under the same protocol.

See (internal design note; not in public repo) § "Recommendation-engine
substitution".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from dorian.dag import DAG
from dorian.pipeline.canonical.class_hash import canonical_class_hash


@dataclass(frozen=True)
class CanonicalEntry:
    """One promoted canonical form."""

    source_class_hash: str
    target_class_hash: str
    rule_id: str
    canonical_pipeline: DAG
    hit_rate: float
    observations: int


@runtime_checkable
class CanonicalRegistry(Protocol):
    """Storage interface for the canonical-form registry."""

    def lookup(self, source_class_hash: str) -> CanonicalEntry | None:
        ...

    def register(self, entry: CanonicalEntry) -> None:
        ...

    def __contains__(self, source_class_hash: str) -> bool:
        ...


class DictCanonicalRegistry:
    """In-memory registry. Tests + local experimentation; the
    production path uses a docstore-backed implementation."""

    def __init__(self) -> None:
        self._entries: dict[str, CanonicalEntry] = {}

    def lookup(self, source_class_hash: str) -> CanonicalEntry | None:
        return self._entries.get(source_class_hash)

    def register(self, entry: CanonicalEntry) -> None:
        self._entries[entry.source_class_hash] = entry

    def __contains__(self, source_class_hash: str) -> bool:
        return source_class_hash in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubstitutionResult:
    """Outcome of a substitution attempt -- transparent for UI
    feedback ("this recommendation was replaced because ...")."""

    output_dag: DAG
    substituted: bool
    source_class_hash: str
    target_class_hash: str = ""
    rule_id: str = ""


def substitute(
    candidate: DAG,
    registry: CanonicalRegistry,
) -> SubstitutionResult:
    """Apply canonical-form substitution to a single candidate.

    Returns a ``SubstitutionResult`` so callers can surface the
    swap in the UI: "Recommended pipeline replaced with canonical
    form X (applied rewrite Y, which fires in Z% of observed
    cases)."
    """
    src = canonical_class_hash(candidate)
    entry = registry.lookup(src)
    if entry is None:
        return SubstitutionResult(
            output_dag=candidate,
            substituted=False,
            source_class_hash=src,
        )
    return SubstitutionResult(
        output_dag=entry.canonical_pipeline,
        substituted=True,
        source_class_hash=src,
        target_class_hash=entry.target_class_hash,
        rule_id=entry.rule_id,
    )


def substitute_many(
    candidates: list[DAG],
    registry: CanonicalRegistry,
) -> list[SubstitutionResult]:
    """Vectorised ``substitute`` over a list of candidates. Each
    element preserves its result object so callers can still show
    per-recommendation swap reasons in the UI."""
    return [substitute(c, registry) for c in candidates]


__all__ = [
    "CanonicalEntry",
    "CanonicalRegistry",
    "DictCanonicalRegistry",
    "SubstitutionResult",
    "substitute",
    "substitute_many",
]
