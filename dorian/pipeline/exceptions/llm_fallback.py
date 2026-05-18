"""Deprecated alias -- use ``pattern_discovery`` instead.

Earlier drafts framed the LLM path as a "fallback" to regex. The
v2 design (see (internal design note; not in public repo) + the MCP-interface
doc) treats pattern discovery as a pluggable multi-strategy
surface. This module re-exports the v2 types under the old names
so in-flight callers keep working; new code should import from
``pattern_discovery`` directly.
"""
from __future__ import annotations

from .pattern_discovery import (
    DiscoveryProposal as LlmFallbackResponse,
    DiscoveryRequest as LlmFallbackRequest,
    McpLlmAgent as LlmFallbackWorker,
    proposed_pattern_from,
)

__all__ = [
    "LlmFallbackRequest",
    "LlmFallbackResponse",
    "LlmFallbackWorker",
    "proposed_pattern_from",
]
