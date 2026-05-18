"""Pre-episode prior sources for the RL trainer.

The RL trainer ships with static warm-start priors (``rl/train/priors.py``)
loaded once at startup from ``llm_priors.json`` + the Postgres BK-Tree
seeds. This package adds a second prior channel: **per-episode
dataset-aware recommendations**.

Before each episode, the env computes a :class:`DatasetProfile` from
the actual CSV (not the heuristic ``feature_mix`` tag), hands it to
a :class:`PriorSource`, and receives back a list of
:class:`PriorRecommendation` objects naming catalog op_keys the
agent should prefer for that dataset. The mask folds those into
``ActionCandidate.suggestion_weight`` — same channel the semantic-name
matcher uses, so the policy sees a single multiplier per candidate.

Modularity invariants:

  * The trainer works without any LLM configured. ``NullPriorSource``
    is the default; it returns an empty list, suggestion_weight stays
    at its semantic-match baseline, and the system runs as before.
  * Two interchangeable backends ship:
      - :class:`OpenAIChatPriorSource` — submits the DatasetProfile to
        an OpenAI-compatible chat endpoint via
        ``dorian.llm.factory.spawn("rl-prior")`` and parses a strict
        JSON response. Cached by profile hash.
      - :class:`MCPPriorSource` — reads recommendations from an
        in-memory queue populated by MCP tools (``rl_dataset_profile``
        + ``rl_prior_recommend``), so Claude Code and other MCP
        clients can inject priors without the trainer making outbound
        LLM calls. Mirrors the extraction-tool pattern.
  * Backend selection is env-driven (``DORIAN_RL_PRIOR_BACKEND``) so
    compose profiles can flip between null / openai / mcp without
    touching code.
"""
from __future__ import annotations

import os

from .base import (
    DatasetProfile,
    NullPriorSource,
    PriorRecommendation,
    PriorSource,
)
from .profile import compute_dataset_profile


def build_prior_source(backend: str | None = None) -> PriorSource:
    """Construct the configured prior source. Default is Null (no
    priors), so trainer runs without any external dependency."""
    backend = (backend or os.environ.get("DORIAN_RL_PRIOR_BACKEND") or "null").lower()
    if backend == "openai":
        from .openai_chat import OpenAIChatPriorSource
        return OpenAIChatPriorSource()
    if backend == "mcp":
        from .mcp_source import get_shared_mcp_source
        return get_shared_mcp_source()
    return NullPriorSource()


__all__ = [
    "DatasetProfile",
    "NullPriorSource",
    "PriorRecommendation",
    "PriorSource",
    "build_prior_source",
    "compute_dataset_profile",
]
