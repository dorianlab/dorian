"""MCP-backed prior source.

External MCP clients (Claude Code, custom orchestrators) can inject
prior recommendations by calling ``rl_prior_recommend`` on the
Dorian MCP server (``dorian/mcp/server.py``). That tool routes the
recommendation into a process-local shared queue that this source
reads from at episode reset.

Why a shared queue instead of direct LLM calls: the RL trainer runs
as its own container. We don't want it making outbound network
calls to LLMs by default — too many security / rate-limit / latency
footguns. The MCP path inverts control: external clients poll the
trainer's profile via ``rl_dataset_profile``, think about it
however they want, and push recommendations back in. That's the
same shape as the mitigation-curation MCP flow already in the
project.

When the trainer runs WITHOUT an MCP client attached, no
recommendations arrive and the source degrades to empty — same
behaviour as :class:`NullPriorSource`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from .base import DatasetProfile, PriorRecommendation


@dataclass
class MCPPriorSource:
    """Process-local recommendation queue with per-dataset slots.

    The MCP server's ``rl_prior_recommend`` tool writes into
    ``_by_dataset`` keyed on dataset name. The trainer's env reads
    the latest value at ``reset()``. An entry is **one-shot by
    default** — consumed on read, so stale injections from a prior
    batch don't silently influence later episodes. Pass
    ``consume=False`` to retain the entry across multiple resets.
    """

    _by_dataset: OrderedDict[str, tuple[list[PriorRecommendation], bool]] = field(
        default_factory=OrderedDict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Also store the most recent profile per dataset so MCP clients
    # can read what the trainer observed without having to re-profile
    # the CSV themselves. Updated by the env on reset().
    _profiles: dict[str, DatasetProfile] = field(default_factory=dict)

    def inject(
        self,
        dataset_name: str,
        recs: list[PriorRecommendation],
        *,
        consume: bool = True,
    ) -> None:
        """External entry point — called by the MCP tool to register
        recommendations for the next episode on ``dataset_name``."""
        with self._lock:
            self._by_dataset[dataset_name] = (list(recs), consume)

    def publish_profile(
        self, dataset_name: str, profile: DatasetProfile
    ) -> None:
        """Called by the env on ``reset()`` so MCP clients querying
        ``rl_dataset_profile`` get the actual measured profile."""
        with self._lock:
            self._profiles[dataset_name] = profile

    def get_profile(self, dataset_name: str) -> DatasetProfile | None:
        with self._lock:
            return self._profiles.get(dataset_name)

    def recommend(self, profile: DatasetProfile) -> list[PriorRecommendation]:
        with self._lock:
            entry = self._by_dataset.get(profile.name)
            if entry is None:
                return []
            recs, consume = entry
            if consume:
                del self._by_dataset[profile.name]
            return list(recs)


# Module-level singleton so the MCP server tool and the trainer see
# the same queue. Both ends import ``get_shared_mcp_source`` to reach
# it — avoids passing an instance through the compose stack.
_SHARED: MCPPriorSource | None = None
_SHARED_LOCK = threading.Lock()


def get_shared_mcp_source() -> MCPPriorSource:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = MCPPriorSource()
        return _SHARED


__all__ = ["MCPPriorSource", "get_shared_mcp_source"]
