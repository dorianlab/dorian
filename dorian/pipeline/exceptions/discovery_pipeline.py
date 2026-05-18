"""Runtime glue: observe → buffer → template → LLM.

Bridges ``matching.match`` outcomes to the two discovery paths
(deterministic templating, LLM fallback) so the user-facing code
has ONE entry point and the routing decisions happen in one place.

Flow:

  1. Caller runs ``match(sig, registry, raw_message=msg)``.
  2. Caller hands the result plus the raw message + traceback to
     ``observe_match(result, raw_message, raw_traceback, registry,
     buffer, llm_agent=None)``.
  3. observe_match:
     - Always: ``registry.touch(result.pattern.signature_hash)`` on
       a hit — observation count feeds the promotion policy.
     - On ``via="bucket"``: append the raw message's
       canonicalised form to the bucket's sample buffer. If the
       buffer reaches ``MIN_SAMPLES_FOR_PROMOTION`` AND no leaf
       has been mined yet, call ``templating.propose_leaf(samples)``.
       Success → register a ``status="proposed", scope="leaf"``
       pattern with the bucket's mitigations carried over.
       Refusal (returns None) → escalate to the LLM fallback if
       one is configured.
     - On ``via="miss"``: escalate directly to LLM fallback if
       configured. No buffer accumulation — miss is rare enough
       that we don't want to sit on it waiting for a quorum.
     - On ``via="hash"`` / ``via="leaf_regex"``: nothing else to
       do; the pattern is already serving.

``observe_match`` never raises into the caller — the discovery
orchestration is best-effort optimisation. A logging hook
(``on_event``) surfaces every interesting state change for
telemetry / audit / debugging.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .matching import MatchResult
from .pattern_discovery import (
    DiscoveryMode,
    DiscoveryProposal,
    DiscoveryRequest,
    PatternDiscoveryAgent,
    discover,
    proposed_pattern_from,
)
from .registry import (
    ExceptionPattern,
    ExceptionRegistry,
    MitigationRef,
)
from .templating import MIN_SAMPLES_FOR_PROMOTION, propose_leaf
from .traceback_signature import TracebackSignature, canonicalise_message


# ---------------------------------------------------------------------------
# Sample buffer
# ---------------------------------------------------------------------------

# A practical cap keeps memory bounded if a bucket fires on a noisy
# long-tail distribution that never converges to a template. Well
# above MIN_SAMPLES_FOR_PROMOTION; the templating LCS is O(n²) in
# the token count per-sample but only O(n) in sample count.
_SAMPLE_BUFFER_MAX = 32


@dataclass
class SampleBuffer:
    """Per-bucket sample buffer for deterministic template mining.

    Keyed on bucket ``signature_hash`` (which is the synthetic
    ``"bucket:{type}:{prefix}"`` form). Stores canonicalised
    message templates, not raw messages — the templating module
    works on the canonical shape so we avoid pushing
    instance-specific noise into the LCS.

    ``mined`` flips to True once the first leaf proposal has been
    registered for this bucket; subsequent observations still
    touch the bucket but don't re-trigger templating. (A future
    promotion policy may reset ``mined`` if the proposed leaf is
    demoted, to allow a fresh attempt.)
    """

    samples: list[str] = field(default_factory=list)
    mined: bool = False

    def add(self, canonical_message: str) -> None:
        # De-dup exact repeats — they can't add to LCS stability
        # and bias the centroid pick toward the most-repeated form
        # when what we want is representative diversity.
        if canonical_message in self.samples:
            return
        if len(self.samples) >= _SAMPLE_BUFFER_MAX:
            return
        self.samples.append(canonical_message)


class BufferRegistry:
    """Collection of per-bucket sample buffers. One process-local
    instance is sufficient; callers that need persistence wrap a
    Redis / docstore layer around the same interface.

    Lookups default to an empty buffer so callers don't need to
    pre-provision. The buffer's hash key is the bucket's synthetic
    signature_hash so misses and leaf hits never share a buffer.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, SampleBuffer] = defaultdict(SampleBuffer)

    def for_bucket(self, bucket_hash: str) -> SampleBuffer:
        return self._buffers[bucket_hash]

    def __len__(self) -> int:
        return len(self._buffers)


# ---------------------------------------------------------------------------
# Events — narrow telemetry surface for the orchestrator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveryEvent:
    """Structured event emitted from ``observe_match``. Callers can
    bind a single handler for telemetry + audit + debugging without
    needing to know the internal branch structure."""

    kind: str  # "bucket_sample_added" | "leaf_proposed_deterministic"
               # | "leaf_proposed_llm" | "discovery_escalated"
               # | "discovery_failed"
    signature_hash: str
    details: dict
    ts: float = field(default_factory=time.time)


EventHandler = Callable[[DiscoveryEvent], None]


def _noop_handler(_: DiscoveryEvent) -> None:
    pass


# ---------------------------------------------------------------------------
# The orchestrator entry point
# ---------------------------------------------------------------------------

def observe_match(
    result: MatchResult,
    *,
    raw_message: str,
    raw_traceback: str,
    registry: ExceptionRegistry,
    buffers: BufferRegistry,
    llm_agent: PatternDiscoveryAgent | None = None,
    on_event: EventHandler = _noop_handler,
) -> list[DiscoveryProposal]:
    """Turn a ``MatchResult`` into registry side-effects + (maybe) a
    discovery proposal.

    Returns the list of proposals that this call produced (empty
    when nothing new happened). Never raises — discovery is
    best-effort and the caller's primary response path (showing
    the matched pattern's mitigations to the user / RL) continues
    regardless of what this returns.
    """
    proposals: list[DiscoveryProposal] = []

    # Every hit is an observation.
    if result.matched and result.pattern is not None:
        try:
            registry.touch(result.pattern.signature_hash)
        except Exception:
            pass  # telemetry-only, never block the caller

    if result.via in ("hash", "leaf_regex"):
        return proposals

    if result.via == "bucket" and result.pattern is not None:
        bucket = result.pattern
        buf = buffers.for_bucket(bucket.signature_hash)
        canonical = canonicalise_message(raw_message)
        before = len(buf.samples)
        buf.add(canonical)
        after = len(buf.samples)
        if after > before:
            on_event(
                DiscoveryEvent(
                    kind="bucket_sample_added",
                    signature_hash=bucket.signature_hash,
                    details={"sample_count": after},
                )
            )

        # Templating eligibility: enough samples + haven't mined yet.
        if not buf.mined and len(buf.samples) >= MIN_SAMPLES_FOR_PROMOTION:
            proposal = propose_leaf(buf.samples)
            if proposal is not None:
                leaf_pattern = _proposed_leaf_from_bucket(
                    signature=result.signature,
                    bucket=bucket,
                    regex=proposal.regex,
                    template=proposal.template,
                )
                try:
                    registry.register(leaf_pattern)
                    buf.mined = True
                    proposals.append(
                        DiscoveryProposal(
                            root_cause_summary=(
                                "Deterministic template mined from "
                                f"{len(buf.samples)} bucket samples"
                            ),
                            message_regex=proposal.regex,
                            message_template=proposal.template,
                            proposed_mitigations=bucket.mitigations,
                            confidence=min(1.0, len(buf.samples) / 10.0),
                            signature=result.signature,
                        )
                    )
                    on_event(
                        DiscoveryEvent(
                            kind="leaf_proposed_deterministic",
                            signature_hash=leaf_pattern.signature_hash,
                            details={
                                "bucket_hash": bucket.signature_hash,
                                "sample_count": len(buf.samples),
                                "regex": proposal.regex,
                            },
                        )
                    )
                    return proposals
                except Exception:
                    pass  # fall through to LLM on registration failure
            else:
                # Templating refused — escalate to LLM if configured.
                on_event(
                    DiscoveryEvent(
                        kind="discovery_escalated",
                        signature_hash=bucket.signature_hash,
                        details={
                            "reason": "templating_refused",
                            "sample_count": len(buf.samples),
                        },
                    )
                )
                proposals.extend(
                    _run_llm_fallback(
                        signature=result.signature,
                        raw_message=raw_message,
                        raw_traceback=raw_traceback,
                        llm_agent=llm_agent,
                        registry=registry,
                        on_event=on_event,
                    )
                )
                # Mark mined regardless of LLM outcome so we don't
                # repeatedly escalate for the same bucket within one
                # session (promotion worker resets this later).
                buf.mined = True
        return proposals

    if result.via == "miss":
        proposals.extend(
            _run_llm_fallback(
                signature=result.signature,
                raw_message=raw_message,
                raw_traceback=raw_traceback,
                llm_agent=llm_agent,
                registry=registry,
                on_event=on_event,
            )
        )
        return proposals

    return proposals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proposed_leaf_from_bucket(
    *,
    signature: TracebackSignature,
    bucket: ExceptionPattern,
    regex: str,
    template: str,
) -> ExceptionPattern:
    """Build a ``status="proposed", scope="leaf"`` pattern from a
    templating proposal, carrying the bucket's mitigations.

    The leaf's ``signature_hash`` uses the CURRENT signature's
    5-field hash so subsequent hits on the same shape get an O(1)
    hit after promotion. ``site_library`` uses the concrete
    signature's site_library (not the bucket's prefix) — the leaf
    is narrower than the bucket by definition.
    """
    return ExceptionPattern(
        signature_hash=signature.hash_hex(),
        exception_type=signature.exception_type,
        operator_fqn=signature.operator_fqn,
        site_library=signature.site_library,
        message_template=template,
        user_frame_depth=signature.user_frame_depth,
        mitigations=bucket.mitigations,
        source="regex",
        status="proposed",
        scope="leaf",
        observations=1,
        last_seen_ts=time.time(),
        message_regex=re.compile(regex),
    )


def _run_llm_fallback(
    *,
    signature: TracebackSignature,
    raw_message: str,
    raw_traceback: str,
    llm_agent: PatternDiscoveryAgent | None,
    registry: ExceptionRegistry,
    on_event: EventHandler,
) -> list[DiscoveryProposal]:
    """Invoke the LLM discovery backend; register its proposal as
    ``status="proposed"``. Returns the proposal(s) produced."""
    if llm_agent is None:
        on_event(
            DiscoveryEvent(
                kind="discovery_failed",
                signature_hash=signature.hash_hex(),
                details={"reason": "no_llm_agent_configured"},
            )
        )
        return []

    req = DiscoveryRequest(
        signature=signature,
        raw_message=raw_message,
        raw_traceback=raw_traceback,
    )
    try:
        proposals = discover(req, mode=DiscoveryMode.LLM_PRIMARY, llm_agent=llm_agent)
    except Exception as exc:
        on_event(
            DiscoveryEvent(
                kind="discovery_failed",
                signature_hash=signature.hash_hex(),
                details={"reason": "agent_raised", "error": str(exc)},
            )
        )
        return []
    for p in proposals:
        try:
            pattern = proposed_pattern_from(req, p)
            registry.register(pattern)
            on_event(
                DiscoveryEvent(
                    kind="leaf_proposed_llm",
                    signature_hash=pattern.signature_hash,
                    details={
                        "source": "llm_proposed",
                        "confidence": p.confidence,
                    },
                )
            )
        except Exception:
            pass  # individual registration failure shouldn't drop the rest
    return list(proposals)


__all__ = [
    "BufferRegistry",
    "DiscoveryEvent",
    "EventHandler",
    "SampleBuffer",
    "observe_match",
]
