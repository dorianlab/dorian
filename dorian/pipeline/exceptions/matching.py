"""Signature → pattern matching.

Four-tier lookup, descending in precision:

  1. **Hash match** -- exact ``TracebackSignature.hash_hex()`` match
     against a registered pattern. O(1).
  2. **Leaf regex match** -- walk live patterns with
     ``scope="leaf"`` whose ``exception_type`` + exact
     ``site_library`` match, and try each pattern's
     ``message_regex`` against the raw message. Handles
     instance-specific messages that canonicalise differently
     (the canonicaliser is conservative; the regex is permissive).
  3. **Bucket catchall** -- walk live patterns with
     ``scope="bucket"``. Matches on ``exception_type`` + a
     ``site_library`` PREFIX (so ``"pandas."`` catches any
     ``pandas.core.*`` submodule). The broadest tier: supplies a
     best-guess mitigation bundle for any library-level error
     family that didn't land on a leaf. Rationale: "we don't have
     a precise template yet for this error, but the general
     mitigation bundle for pandas-KeyError is a better starting
     point than silence."
  4. **Miss** -- novel exception. Callers emit a
     ``NovelExceptionObserved`` event and route through
     ``pattern_discovery.discover(..., mode=LLM_FALLBACK)`` which
     mines a proper leaf template (and fills the bucket gap over
     time via anti-unification; see Step 4 / ``templating.py``).

The tiers run in order; the first match wins. A bucket hit is
authoritative: the UI can render the mitigations immediately while
a background job mines a leaf template asynchronously. The bucket
tier means the USER-facing flow NEVER blocks on LLM availability
for common library error families.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .registry import ExceptionPattern, ExceptionRegistry, MitigationRef
from .traceback_signature import TracebackSignature


@dataclass(frozen=True)
class MatchResult:
    """Outcome of a match attempt."""

    signature: TracebackSignature
    pattern: ExceptionPattern | None
    via: str  # "hash" | "leaf_regex" | "bucket" | "miss"

    @property
    def matched(self) -> bool:
        return self.pattern is not None

    def mitigations_ranked(self) -> list[MitigationRef]:
        """Mitigations for the matched pattern, sorted by descending
        weight. Empty list on miss."""
        if self.pattern is None:
            return []
        return sorted(
            self.pattern.mitigations, key=lambda m: m.weight, reverse=True
        )


def match(
    signature: TracebackSignature,
    registry: ExceptionRegistry,
    *,
    raw_message: str | None = None,
) -> MatchResult:
    """Look up a signature in the registry across all four tiers.

    ``raw_message`` (the original exception message before
    canonicalisation) enables the leaf-regex tier; callers without
    that context pass ``None`` and leaf-regex is skipped (hash and
    bucket still fire).
    """
    # Tier 1: hash match. Note: bucket patterns use a synthetic
    # ``bucket:{type}:{prefix}`` hash so this never collides with a
    # concrete-signature hash.
    hit = registry.get(signature.hash_hex())
    if hit is not None and hit.status == "live" and hit.scope == "leaf":
        return MatchResult(signature=signature, pattern=hit, via="hash")

    # Cache the live-pattern walk so tier 2 + tier 3 share one pass.
    live_patterns = list(registry.all_live())

    # Tier 2: leaf regex match -- walk leaf patterns with strict
    # site_library equality.
    if raw_message:
        for pat in live_patterns:
            if pat.scope != "leaf":
                continue
            if pat.exception_type != signature.exception_type:
                continue
            if pat.site_library and pat.site_library != signature.site_library:
                continue
            if pat.message_regex is None:
                continue
            if pat.message_regex.search(raw_message):
                return MatchResult(
                    signature=signature, pattern=pat, via="leaf_regex"
                )

    # Tier 3: bucket catchall -- walk bucket patterns with site_library
    # as a PREFIX. Optional message_regex still applies when present;
    # its absence means "any message from this library family."
    for pat in live_patterns:
        if pat.scope != "bucket":
            continue
        if pat.exception_type != signature.exception_type:
            continue
        if pat.site_library and not signature.site_library.startswith(
            pat.site_library
        ):
            continue
        if pat.message_regex is not None and raw_message:
            if not pat.message_regex.search(raw_message):
                continue
        return MatchResult(signature=signature, pattern=pat, via="bucket")

    return MatchResult(signature=signature, pattern=None, via="miss")


def match_many(
    signatures: Iterable[TracebackSignature],
    registry: ExceptionRegistry,
) -> list[MatchResult]:
    """Vectorised ``match`` -- one lookup per signature."""
    return [match(sig, registry) for sig in signatures]


__all__ = ["MatchResult", "match", "match_many"]
