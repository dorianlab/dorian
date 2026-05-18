"""Policy core interface.

Every policy architecture from internal design note section 2
lives in its own module (memory_policy, hedge_policy, ...) and
implements ``Policy`` below. Ablation harnesses swap modules along
the "policy core" axis without touching the env, encoder, memory,
dispatch, or isolation layers.

Contract:

    policy = MemoryPolicy(...)
    for episode in range(N):
        trajectory = []
        obs = env.reset()
        while not done:
            candidates, mask = env.available_actions()
            action_id = policy.select(obs, candidates, mask)
            next_obs, reward, done, info = env.step(action_id)
            trajectory.append(Transition(obs, action_id, reward,
                                          next_obs, done))
            obs = next_obs
        policy.update(trajectory)

Notes:

* ``select`` is called per decision step. It must respect the
  ``mask`` (invalid actions set to False) and return an
  ``action_id`` that corresponds to one masked-True candidate.
* ``update`` is called once per episode with the full trajectory.
  Policies that are online per-step (UCB, Hedge with per-step
  updates) can also process the per-step view inside the same
  method -- the contract doesn't prescribe a granularity.
* The observation carries a dataset embedding so cross-dataset
  transfer is possible; policies that don't use it may ignore it.
* The observation optionally carries a live ``ExperimentGraph``
  handle so a policy can query cache-affinity for each candidate.
  Policies may pass ``experiment_graph=None``; the interface
  stays uniform.

No training-time hyperparameters live on the interface. Policies
that need them accept them in their own constructors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Observation:
    """What the policy sees at a decision step.

    The fields are the superset of what any current policy reads;
    individual policies only touch what they need. Adding a field
    to this dataclass is a non-breaking change (policies ignore
    unknown fields by default).
    """

    #: Current partial pipeline DAG as a JSON string. Rust parsers
    #: + pyo3 bindings accept this directly.
    dag_json: str
    #: Compact dataset fingerprint (thesis section 4.4). Fixed
    #: length across the run; used by memory-based policies for
    #: kNN retrieval and by Hedge's context bucketing.
    dataset_embedding: tuple[float, ...]
    #: 0-indexed step count within the current episode.
    step_idx: int
    #: Remaining step budget the env will allow. `-1` = unbounded.
    remaining_budget: int
    #: Optional extra context (task type, user flags). Policies
    #: should not rely on keys they didn't negotiate with the env.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionCandidate:
    """One candidate action a policy can pick from on this step.

    The env builds the full candidate list by enumerating masked
    (op, input_binding, output_routing) triples — but from the
    policy's perspective each candidate is a single discrete unit
    with a stable id and a bit of context.
    """

    #: Integer id from the persistent action map.
    #: Stable across episodes and across catalog changes within a
    #: catalog version.
    action_id: int
    #: Operator key (FQN for atomic, "composite::<hash>" for mined).
    op_key: str
    #: Compact per-action features. v1: (family_onehot | task_tag |
    #: is_new_flag | arity). Policies that don't consume feature
    #: vectors ignore this.
    features: tuple[float, ...] = ()


@dataclass(frozen=True)
class Transition:
    """One step in a trajectory."""

    obs: Observation
    action_id: int
    reward: float
    next_obs: Observation | None
    terminal: bool


# ---------------------------------------------------------------------------
# Policy protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Policy(Protocol):
    """Abstract contract for a policy core.

    The `@runtime_checkable` marker lets tests assert
    `isinstance(policy, Policy)` for duck-type validation without
    requiring every implementation to inherit explicitly.
    """

    def select(
        self,
        obs: Observation,
        candidates: Sequence[ActionCandidate],
        mask: Sequence[bool],
    ) -> int:
        """Pick one action_id from the masked-True candidates.

        Must be deterministic given its internal RNG state — for
        reproducibility across ablation runs, each policy accepts
        a ``seed`` in its constructor and threads it through.
        """
        ...

    def update(self, trajectory: Sequence[Transition]) -> dict[str, float]:
        """Process a completed trajectory.

        Returns a dict of observability metrics (e.g.
        ``{"entropy": 1.3, "max_weight": 42.0}``) for dashboard
        consumption. An empty dict is valid.
        """
        ...


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def masked_indices(mask: Sequence[bool]) -> list[int]:
    """Indices of True entries in a mask."""
    return [i for i, m in enumerate(mask) if m]


def pick_with_weights(
    rng,
    weights: Sequence[float],
    indices: Sequence[int],
) -> int:
    """Weighted sample from ``indices`` using ``weights[indices]``.

    Gracefully falls back to uniform when all weights are zero,
    matching the thesis's "at least one valid action" safeguard.
    """
    if not indices:
        raise ValueError("no candidate indices to pick from")
    total = sum(weights[i] for i in indices)
    if total <= 0 or not (total == total):  # total!=total handles NaN
        # Degenerate distribution -> uniform.
        return rng.choice(indices)
    r = rng.random() * total
    cum = 0.0
    for i in indices:
        cum += weights[i]
        if r < cum:
            return i
    return indices[-1]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Unit-safe cosine similarity over equal-length vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
