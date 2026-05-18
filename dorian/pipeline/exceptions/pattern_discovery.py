"""Pluggable pattern-discovery backends.

The LLM is not a "fallback" -- it is one of several discovery
strategies, selectable per deployment or per failure. Supported
strategies, composable at the orchestration layer:

  * ``REGEX_ONLY``           -- use the registered regex patterns;
                                miss on no match. Cheapest, fastest,
                                deterministic.
  * ``LLM_FALLBACK``          -- run regex first; on miss, consult
                                the MCP-backed agent. The thesis
                                default.
  * ``LLM_PRIMARY``           -- consult the agent first; fall
                                back to regex only if the agent
                                is unavailable. Use when the agent
                                quality significantly exceeds the
                                curated regex library.
  * ``REGEX_AND_LLM_PARALLEL`` -- run both; vote / union the
                                proposals. Use during evaluation
                                phases when comparing regex-only
                                vs agent-enriched proposals.

The backend is accessed through a narrow ``PatternDiscoveryAgent``
protocol. The production path binds it to an MCP server that in
turn speaks to any OpenAI-compatible model (self-hosted via
vLLM / Ollama / llama.cpp-server, cloud via OpenAI / Anthropic
compatibility endpoints, or a future Dorian-owned specialised
model). A second binding of the same protocol lets the agent
running in an interactive Claude-Code session drive the same
discovery flow -- so a human operator pairs with the agent to
develop and validate rewrite rules live, without touching
production LLM infrastructure.

See (internal design note; not in public repo) § "Pattern mining: regex first,
LLM fallback" and the sibling MCP-interface doc
((internal design note; not in public repo)) for the protocol shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .registry import ExceptionPattern, MitigationRef
from .traceback_signature import TracebackSignature


class DiscoveryMode(str, Enum):
    """Strategy selector. Callers pass one into ``discover``."""

    REGEX_ONLY = "regex_only"
    LLM_FALLBACK = "llm_fallback"
    LLM_PRIMARY = "llm_primary"
    REGEX_AND_LLM_PARALLEL = "regex_and_llm_parallel"


# ---------------------------------------------------------------------------
# Request / response shapes (stable across every backend)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveryRequest:
    """Input to the pattern-discovery agent."""

    signature: TracebackSignature
    raw_message: str
    raw_traceback: str
    pipeline_class_hash: str = ""
    tenant_id: str = ""
    # Additional context the MCP server can route to the agent.
    # Open field so future callers can attach whatever helps the
    # model (dataset profile, recent rollout history, etc.) without
    # bumping the contract.
    context: dict = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DiscoveryProposal:
    """Agent-proposed pattern. Status starts ``proposed`` and is
    promoted to ``live`` either by operator review or by
    observation-threshold policy (see ``promotion.py``'s
    canonical-form analogue; exception-pattern promotion is a
    follow-up)."""

    root_cause_summary: str
    message_regex: str
    message_template: str
    proposed_mitigations: tuple[MitigationRef, ...]
    confidence: float = 0.0
    # Echo of the request's signature so the caller can register
    # without re-hashing.
    signature: TracebackSignature | None = None


# ---------------------------------------------------------------------------
# Agent protocol -- uniform across backends
# ---------------------------------------------------------------------------

@runtime_checkable
class PatternDiscoveryAgent(Protocol):
    """The narrow contract every backend satisfies.

    Concrete bindings:
      * ``McpLlmAgent``     -- the production path. Speaks MCP to a
                               gateway that forwards to any
                               OpenAI-compatible backend.
      * ``InteractiveAgent`` -- the session-active path. Lets an
                               agent running in a Claude-Code (or
                               equivalent) session drive the same
                               discovery flow for live rule
                               development. Not shipped here; the
                               protocol + the MCP spec are what
                               makes it pluggable.
      * ``StubAgent``       -- test / offline path. Returns a
                               fixed proposal; useful for unit
                               tests exercising the orchestration
                               logic without network calls.
    """

    def propose(self, req: DiscoveryRequest) -> DiscoveryProposal | None:
        """Return a proposal or None on failure / unknown. Never
        raises into the caller -- the orchestration layer is
        responsible for graceful degradation."""
        ...


# ---------------------------------------------------------------------------
# Stub + McpLlm scaffolds
# ---------------------------------------------------------------------------

class StubDiscoveryAgent:
    """Offline agent. Returns a canned proposal if the signature's
    exception type is in ``known``, else ``None``. For tests + for
    running the whole discovery pipeline without any LLM wiring."""

    def __init__(
        self,
        known: dict[str, DiscoveryProposal] | None = None,
    ) -> None:
        self.known = known or {}

    def propose(self, req: DiscoveryRequest) -> DiscoveryProposal | None:
        base = self.known.get(req.signature.exception_type)
        if base is None:
            return None
        return DiscoveryProposal(
            root_cause_summary=base.root_cause_summary,
            message_regex=base.message_regex,
            message_template=base.message_template,
            proposed_mitigations=base.proposed_mitigations,
            confidence=base.confidence,
            signature=req.signature,
        )


class McpLlmAgent:
    """Production binding contract. Talks to an MCP server that
    exposes the pattern-discovery tool; the server in turn speaks
    to any OpenAI-compatible backend (self-hosted vLLM / Ollama /
    llama.cpp-server, cloud OpenAI / Anthropic via compat
    endpoints, Dorian-owned specialised models).

    This class is the CONTRACT stub -- the live network path is
    wired in a separate service (see
    (internal design note; not in public repo)). Tests swap in the stub
    agent; production wires the MCP transport in place.
    """

    def __init__(
        self,
        mcp_endpoint: str,
        model_id: str,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self.mcp_endpoint = mcp_endpoint
        self.model_id = model_id
        self.timeout_s = timeout_s

    def propose(self, req: DiscoveryRequest) -> DiscoveryProposal | None:
        raise NotImplementedError(
            "McpLlmAgent is a contract stub. The live MCP client "
            "binding is implemented in the background worker; "
            "see internal design notes for the tool "
            "schema and request/response shapes."
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def discover(
    req: DiscoveryRequest,
    *,
    mode: DiscoveryMode,
    regex_agent: PatternDiscoveryAgent | None = None,
    llm_agent: PatternDiscoveryAgent | None = None,
) -> list[DiscoveryProposal]:
    """Run the configured discovery strategy.

    Returns a (possibly empty) list of proposals. Callers register
    each proposal as a ``status="proposed"`` pattern in the
    registry; a promotion worker elevates them to ``live`` once
    thresholds are met.

    ``regex_agent`` is optional: most deployments use the in-
    process registry directly for regex matching via
    ``matching.match`` rather than going through the agent
    protocol. It's supported here for uniform composition in
    ``REGEX_AND_LLM_PARALLEL`` mode where both backends must expose
    the same interface.
    """
    if mode is DiscoveryMode.REGEX_ONLY:
        if regex_agent is None:
            return []
        p = regex_agent.propose(req)
        return [p] if p is not None else []

    if mode is DiscoveryMode.LLM_FALLBACK:
        if regex_agent is not None:
            p = regex_agent.propose(req)
            if p is not None:
                return [p]
        if llm_agent is None:
            return []
        p = llm_agent.propose(req)
        return [p] if p is not None else []

    if mode is DiscoveryMode.LLM_PRIMARY:
        if llm_agent is not None:
            p = llm_agent.propose(req)
            if p is not None:
                return [p]
        if regex_agent is None:
            return []
        p = regex_agent.propose(req)
        return [p] if p is not None else []

    if mode is DiscoveryMode.REGEX_AND_LLM_PARALLEL:
        out: list[DiscoveryProposal] = []
        if regex_agent is not None:
            r = regex_agent.propose(req)
            if r is not None:
                out.append(r)
        if llm_agent is not None:
            l = llm_agent.propose(req)
            if l is not None:
                out.append(l)
        return out

    raise ValueError(f"unknown discovery mode: {mode}")


# ---------------------------------------------------------------------------
# Registration helper (unchanged semantics from the earlier
# llm_fallback path; kept here so callers have one import point).
# ---------------------------------------------------------------------------

def proposed_pattern_from(
    req: DiscoveryRequest,
    resp: DiscoveryProposal,
) -> ExceptionPattern:
    """Build a ``status="proposed"`` ExceptionPattern from a
    discovery proposal."""
    return ExceptionPattern(
        signature_hash=req.signature.hash_hex(),
        exception_type=req.signature.exception_type,
        operator_fqn=req.signature.operator_fqn,
        site_library=req.signature.site_library,
        message_template=resp.message_template,
        user_frame_depth=req.signature.user_frame_depth,
        mitigations=resp.proposed_mitigations,
        source="llm_proposed",
        status="proposed",
        observations=1,
        message_regex=re.compile(resp.message_regex) if resp.message_regex else None,
    )


__all__ = [
    "DiscoveryMode",
    "DiscoveryProposal",
    "DiscoveryRequest",
    "McpLlmAgent",
    "PatternDiscoveryAgent",
    "StubDiscoveryAgent",
    "discover",
    "proposed_pattern_from",
]
