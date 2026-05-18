"""Architecture F -- memory-exploit + Hedge-explore hybrid.

One policy per step; internally dispatches:

    if rng < epsilon:             # explore
        action = HedgePolicy.select(...)
    else:                         # exploit
        action = MemoryPolicy.select(...)

`update()` feeds the trajectory to BOTH inner policies (they have
distinct state; their stats do not conflict). This keeps the
recommendation from internal design note section 6.F:
"memory as exploit + Hedge as explore".

Composition: the inner policies satisfy the ``Policy`` protocol.
This class does too. Ablation harnesses can swap Hybrid for
Memory or Hedge without caller changes.

Note on cache-affinity nudges: the two inner policies read
``obs.extras["cache_affinity_per_action"]`` independently.
Setting it once on the obs handles both. See
internal design note section 6b for the composition matrix
detailing which layers stack with which cores.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence

from .base import ActionCandidate, Observation, Policy, Transition
from .hedge_policy import HedgePolicy
from .memory_policy import MemoryPolicy


@dataclass
class HybridPolicy:
    """Memory-exploit + Hedge-explore.

    Parameters
    ----------
    epsilon:
        Explore probability. Thesis action-prior coefficient sits
        at 0.1; we reuse that scale.
    memory:
        Concrete MemoryPolicy instance. Defaults to fresh.
    hedge:
        Concrete HedgePolicy instance. Defaults to fresh.
    seed:
        RNG seed for the ε-coin. Inner policies have their own
        RNGs — do not share seeds, so each inner policy's sampling
        is independent of this coin.
    """

    epsilon: float = 0.1
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    hedge: HedgePolicy = field(default_factory=HedgePolicy)
    seed: int = 0
    _coin: random.Random = field(init=False)
    _last_branch: str = field(default="exploit", init=False)

    def __post_init__(self) -> None:
        self._coin = random.Random(self.seed)

    # ------------------------------------------------------------------
    # Policy protocol
    # ------------------------------------------------------------------

    def select(
        self,
        obs: Observation,
        candidates: Sequence[ActionCandidate],
        mask: Sequence[bool],
    ) -> int:
        if self._coin.random() < self.epsilon:
            self._last_branch = "explore"
            return self.hedge.select(obs, candidates, mask)
        self._last_branch = "exploit"
        return self.memory.select(obs, candidates, mask)

    def update(
        self,
        trajectory: Sequence[Transition],
    ) -> dict[str, float]:
        mem_stats = self.memory.update(trajectory)
        hedge_stats = self.hedge.update(trajectory)
        return {
            **{f"memory.{k}": v for k, v in mem_stats.items()},
            **{f"hedge.{k}": v for k, v in hedge_stats.items()},
            "last_branch_explore": 1.0 if self._last_branch == "explore" else 0.0,
        }

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def last_branch(self) -> str:
        """Which inner policy handled the most recent `select`.
        Either ``"explore"`` or ``"exploit"``."""
        return self._last_branch
