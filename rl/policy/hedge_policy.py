"""Architecture B -- Hedge / multiplicative weights.

The classical Hedge algorithm (Freund & Schapire, 1997) over the
persistent action id space. Per step:

    w[a]  ← w[a] · exp(η · r(a))        (only for the actually chosen a)
    π(a|s) ∝ mask(a|s) · w[a]

No gradients, no epochs, no replay buffer. One O(|candidates|)
weighted sample per decision. One O(|trajectory|) scalar update
per episode. Classical no-regret guarantee against any adversary
generating the rewards.

When a new action enters the catalog, its weight is seeded to the
current geometric mean of existing weights — neither an
overconfident start nor an extinction sentence. Old actions are
kept at their last weight even after they vanish from the
catalog (so if they return, they come back with their accumulated
history).

Training cost: zero gradient steps. Adaptation to drift:
instantaneous via the geometric-mean seed.

See internal design note section 2.B for the rationale and
the composition matrix for how this stacks with cache-affinity
nudges + memory priors (both orthogonal; see ``HybridPolicy``).
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field
from typing import Sequence

from .base import (
    ActionCandidate,
    Observation,
    Policy,
    Transition,
    masked_indices,
    pick_with_weights,
)


@dataclass
class HedgePolicy:
    """Multiplicative-weights policy.

    Parameters
    ----------
    seed:
        RNG seed for reproducibility.
    eta:
        Learning rate. Classical Hedge sets η = sqrt(8 ln |A| / T)
        but we default to a constant 0.1 for online operation
        where T is unknown; callers may schedule η.
    max_log_weight:
        Safety cap on log(w[a]) to prevent numerical blowup on
        sustained high rewards. When a log-weight hits the cap,
        all log-weights are rebased by subtracting their max (the
        Hedge distribution is scale-invariant).
    cache_affinity_scale:
        Weight on a per-action cache-affinity nudge read from
        ``obs.extras["cache_affinity_per_action"]``. Defaults to
        0.0 (no nudge) — the composition matrix has HybridPolicy
        or an outer env-level nudge do this explicitly so the
        pure-Hedge baseline stays pure.
    """

    seed: int = 0
    eta: float = 0.1
    max_log_weight: float = 20.0
    cache_affinity_scale: float = 0.0
    # state
    _log_weights: dict[int, float] = field(default_factory=dict)
    _rng: random.Random = field(init=False)
    # Serialises ``_log_weights`` mutation across parallel rollouts —
    # both ``select`` (which seeds new actions) and ``update`` (which
    # adjusts weights) mutate the dict.
    _weights_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------
    # Policy protocol
    # ------------------------------------------------------------------

    def select(
        self,
        obs: Observation,
        candidates: Sequence[ActionCandidate],
        mask: Sequence[bool],
    ) -> int:
        indices = masked_indices(mask)
        if not indices:
            raise ValueError("HedgePolicy.select called with empty mask")
        with self._weights_lock:
            seed_lw = self._geometric_mean_log_weight()
            for i in indices:
                a = candidates[i].action_id
                if a not in self._log_weights:
                    self._log_weights[a] = seed_lw
            log_scores = []
            for i in indices:
                a = candidates[i].action_id
                lw = self._log_weights[a]
                aff = self._read_cache_affinity(obs, candidates[i])
                lw += self.cache_affinity_scale * aff
                # Deterministic suggestion boost: semantic-name
                # matches on AddEdge carry a multiplier that the
                # policy folds into the log-weight as ``log(mult)``.
                # 1.0 (baseline) → no change; 3.0 → +log(3) ≈ 1.1.
                sugg = getattr(candidates[i], "suggestion_weight", 1.0)
                if sugg > 1.0:
                    lw += math.log(sugg)
                log_scores.append(lw)
        max_lw = max(log_scores)
        weights = [0.0] * len(candidates)
        for i, lw in zip(indices, log_scores):
            weights[i] = math.exp(lw - max_lw)
        chosen_idx = pick_with_weights(self._rng, weights, indices)
        return candidates[chosen_idx].action_id

    def update(
        self,
        trajectory: Sequence[Transition],
    ) -> dict[str, float]:
        if not trajectory:
            return {"trajectory_len": 0.0}
        # Classical Hedge assigns the terminal reward to every
        # action in the trajectory. Credit assignment is crude but
        # matches the thesis's "late reward" setting.
        terminal = trajectory[-1].reward
        with self._weights_lock:
            for step in trajectory:
                a = step.action_id
                lw = self._log_weights.get(a, self._geometric_mean_log_weight())
                lw += self.eta * terminal
                self._log_weights[a] = lw
            self._rebase_if_exceeds_cap()
            return {
                "trajectory_len": float(len(trajectory)),
                "terminal_reward": float(terminal),
                "max_log_weight": float(
                    max(self._log_weights.values()) if self._log_weights else 0.0
                ),
                "min_log_weight": float(
                    min(self._log_weights.values()) if self._log_weights else 0.0
                ),
                "n_actions_seen": float(len(self._log_weights)),
            }

    def credit_synthetic_trajectory(
        self,
        action_ids: Sequence[int],
        *,
        strength: float = 1.0,
    ) -> None:
        """Bias the weight distribution toward ``action_ids`` as if a
        prior successful trajectory had landed on each. Uses the same
        multiplicative-weights update (``lw += eta * strength``)
        that organic rollouts use, so warm-started weights are on the
        same scale as learned ones and degrade under the same rebase.
        """
        if not action_ids:
            return
        with self._weights_lock:
            for aid in action_ids:
                lw = self._log_weights.get(aid, self._geometric_mean_log_weight())
                lw += self.eta * strength
                self._log_weights[aid] = lw
            self._rebase_if_exceeds_cap()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _geometric_mean_log_weight(self) -> float:
        """Log-weight seed for never-seen actions. The arithmetic
        mean of log-weights, which corresponds to the geometric
        mean in weight space — neutral relative to the current
        population."""
        if not self._log_weights:
            return 0.0
        return sum(self._log_weights.values()) / len(self._log_weights)

    def _rebase_if_exceeds_cap(self) -> None:
        if not self._log_weights:
            return
        peak = max(self._log_weights.values())
        if peak <= self.max_log_weight:
            return
        # Subtract peak from every entry — this preserves the
        # softmax distribution exactly.
        for k in self._log_weights:
            self._log_weights[k] -= peak

    @staticmethod
    def _read_cache_affinity(
        obs: Observation, cand: ActionCandidate
    ) -> float:
        aff_map = obs.extras.get("cache_affinity_per_action")
        if isinstance(aff_map, dict):
            return float(aff_map.get(cand.action_id, 0.0))
        return 0.0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def weight_of(self, action_id: int) -> float:
        """Normalised softmax weight for a given action under the
        current state. Useful for tests + dashboards."""
        if action_id not in self._log_weights:
            return 0.0
        max_lw = max(self._log_weights.values())
        return math.exp(self._log_weights[action_id] - max_lw)

    def snapshot(self) -> dict[int, float]:
        """Copy of the current log-weight map (read-only view)."""
        return dict(self._log_weights)
