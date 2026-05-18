"""Swappable policy cores for the RL agent.

Every module exports a class implementing the ``Policy`` protocol
from ``rl.policy.base``. Ablation harnesses select one:

    from rl.policy import MemoryPolicy, HedgePolicy, HybridPolicy

    # A: thesis-A8 analogue -- pure retrieval, no training
    policy = MemoryPolicy(seed=42)

    # B: no-regret exponential-weights, zero gradients
    policy = HedgePolicy(seed=42, eta=0.1)

    # F: memory-exploit + Hedge-explore (production recommendation)
    policy = HybridPolicy(epsilon=0.1, seed=42)

See internal design note for the architecture trade-offs and
the composition-matrix section for which orthogonal layers
(cache-affinity nudges, failure-aware masking, UX isolation, ...)
stack with which cores.
"""

from .base import (
    ActionCandidate,
    Observation,
    Policy,
    Transition,
    cosine_similarity,
    masked_indices,
    pick_with_weights,
)
from .hedge_policy import HedgePolicy
from .hybrid_policy import HybridPolicy
from .memory_policy import MemoryPolicy

__all__ = [
    "ActionCandidate",
    "HedgePolicy",
    "HybridPolicy",
    "MemoryPolicy",
    "Observation",
    "Policy",
    "Transition",
    "cosine_similarity",
    "masked_indices",
    "pick_with_weights",
]
