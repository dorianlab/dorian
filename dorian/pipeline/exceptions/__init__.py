"""Exception-driven optimization pass.

Turns pipeline failures into structured mitigation suggestions via
a traceback-signature registry with pluggable pattern-discovery
backends (regex, LLM-via-MCP, interactive-agent-via-MCP).

Submodules:

  * ``traceback_signature`` -- ``TracebackSignature`` +
    ``extract(exc)``
  * ``registry``            -- ``ExceptionPattern`` +
    ``MemoryExceptionRegistry`` + seed library
  * ``matching``            -- 4-tier match (hash + leaf regex +
    bucket catchall + miss)
  * ``templating``          -- deterministic anti-unification mining:
    when a bucket serves N samples, distill a narrower leaf regex
    without LLM involvement
  * ``pattern_discovery``   -- pluggable discovery agents +
    ``DiscoveryMode`` orchestration (LLM fallback for true misses)
  * ``llm_fallback``        -- deprecated alias re-exporting
    ``pattern_discovery`` types under pre-v2 names

See (internal design note; not in public repo) for the end-to-end flow and
(internal design note; not in public repo) for the MCP tool surface.
"""

# Primary v2 types.
from .discovery_pipeline import (
    BufferRegistry,
    DiscoveryEvent,
    SampleBuffer,
    observe_match,
)
from .matching import MatchResult, match, match_many
from .pattern_discovery import (
    DiscoveryMode,
    DiscoveryProposal,
    DiscoveryRequest,
    McpLlmAgent,
    PatternDiscoveryAgent,
    StubDiscoveryAgent,
    discover,
    proposed_pattern_from,
)
from .registry import (
    ExceptionPattern,
    ExceptionRegistry,
    MemoryExceptionRegistry,
    MitigationRef,
    seed_patterns,
)
from .templating import (
    LeafProposal,
    MIN_SAMPLES_FOR_PROMOTION,
    propose_leaf,
    tokenise,
)
from .traceback_signature import (
    FrameSummary,
    TracebackSignature,
    canonicalise_message,
    extract,
)

# Deprecated aliases preserved from the v1 scaffold.
from .llm_fallback import (
    LlmFallbackRequest,
    LlmFallbackResponse,
    LlmFallbackWorker,
)

__all__ = [
    # v2 -- primary
    "BufferRegistry",
    "DiscoveryEvent",
    "DiscoveryMode",
    "DiscoveryProposal",
    "DiscoveryRequest",
    "ExceptionPattern",
    "ExceptionRegistry",
    "FrameSummary",
    "LeafProposal",
    "MIN_SAMPLES_FOR_PROMOTION",
    "MatchResult",
    "McpLlmAgent",
    "MemoryExceptionRegistry",
    "MitigationRef",
    "PatternDiscoveryAgent",
    "SampleBuffer",
    "StubDiscoveryAgent",
    "TracebackSignature",
    "canonicalise_message",
    "discover",
    "extract",
    "match",
    "match_many",
    "observe_match",
    "propose_leaf",
    "proposed_pattern_from",
    "seed_patterns",
    "tokenise",
    # v1 aliases (deprecated)
    "LlmFallbackRequest",
    "LlmFallbackResponse",
    "LlmFallbackWorker",
]
