"""Architecture A -- pure memory + action priors.

No trainable weights. Decisions come from:

    π(a|s) ∝ mask(a|s) × prior(a|s) × (1 + ε_cache × cache_affinity(s ∪ a))

where

    prior(a|s) = Σ_i cos(z_D, z_{D_i}) × succ_rate_i(a) × ρ(Δt_i)

summed over past episodes with their dataset embeddings z_{D_i}
and per-action success rates. A new action (or a new dataset with
no neighbours) falls back to a uniform prior + the cache-affinity
nudge.

Adaptation under operator drift: instantaneous. A new op shows up
in `candidates`, has no memory entries yet, and gets uniform-prior
weight; as the first few rollouts populate its success stats, the
prior sharpens.

Training cost: zero. `update()` is an O(|trajectory|) insert into
the per-action success tracker.

See internal design note section 2.A for the rationale.
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from .base import (
    ActionCandidate,
    Observation,
    Policy,
    Transition,
    cosine_similarity,
    masked_indices,
    pick_with_weights,
)


@dataclass
class _ActionEpisodeStat:
    """Per (action_id, dataset-neighbour) success record.

    ``n_success`` is a float (not an int) because
    ``credit_partial_success`` adds a fractional success when an
    AI-Debugger auto-mitigation rewrites a failing parent into a
    working child — the parent's actions get half a success, not a
    whole one.
    """

    dataset_embedding: tuple[float, ...]
    n_total: int = 0
    n_success: float = 0.0
    last_episode_ts: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.n_total == 0:
            return 0.0
        return self.n_success / self.n_total


@dataclass
class MemoryPolicy:
    """Pure memory-based policy. Architecture A from the critique.

    Parameters
    ----------
    seed:
        RNG seed for reproducibility.
    epsilon_cache:
        Weight on the cache_affinity nudge. Thesis equivalent
        (memory prior) uses ε=0.1; cache affinity uses the same
        scale by default.
    recency_half_life_secs:
        Half-life for the recency decay ρ(Δt). Default 3600s
        (1 hour) — experiences within the last hour count at full
        weight; older entries decay.
    success_threshold:
        Reward value at or above which a transition counts as
        "success" in the success-rate tally.
    """

    seed: int = 0
    epsilon_cache: float = 0.1
    recency_half_life_secs: float = 3600.0
    success_threshold: float = 0.5
    # Prior returned for action_ids that have no history yet. The old
    # default was 1.0 (encourage exploration of unseen actions), but
    # that makes warm-started actions — which typically score
    # 0.25 + Σ(cos · success_rate) ≈ 1.5–3.0 — barely stand out above
    # the unseen baseline. Lowering to 0.25 preserves nonzero weight
    # on unseen actions (they stay explorable) while letting the
    # warm-start bias actually express itself in the weighted draw.
    unseen_action_prior: float = 0.25
    # --- state (private) ---
    _stats: dict[int, list[_ActionEpisodeStat]] = field(default_factory=dict)
    _rng: random.Random = field(init=False)
    # Parallel rollouts share this policy; ``update`` and
    # ``credit_synthetic_trajectory`` both mutate ``_stats``. A single
    # OS-level lock serialises those mutations without blocking
    # ``select()``, which reads under the assumption that dict mutation
    # is atomic at the slot level (safe in CPython for the single-key
    # reads the prior path does).
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

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
            raise ValueError("MemoryPolicy.select called with empty mask")
        weights = [0.0] * len(candidates)
        now = time.time()
        for idx in indices:
            cand = candidates[idx]
            weights[idx] = self._prior(cand.action_id, obs, now) + 1e-6
            aff = self._read_cache_affinity(obs, cand)
            weights[idx] *= 1.0 + self.epsilon_cache * aff
            # Deterministic suggestion boost. ``suggestion_weight``
            # is populated by the mask for edges whose ports share a
            # semantic identity (e.g. y_pred→y_pred). The policy
            # multiplies it in so the weighted draw prefers
            # semantically-matched candidates without losing access
            # to the rest of the action space.
            weights[idx] *= max(1.0, getattr(cand, "suggestion_weight", 1.0))
        chosen = pick_with_weights(self._rng, weights, indices)
        return candidates[chosen].action_id

    def update(
        self,
        trajectory: Sequence[Transition],
    ) -> dict[str, float]:
        if not trajectory:
            return {"trajectory_len": 0.0}
        terminal_reward = trajectory[-1].reward
        success = terminal_reward >= self.success_threshold
        now = time.time()
        with self._stats_lock:
            for step in trajectory:
                stats_for_action = self._stats.setdefault(step.action_id, [])
                entry = self._match_dataset_entry(
                    stats_for_action, step.obs.dataset_embedding
                )
                if entry is None:
                    entry = _ActionEpisodeStat(
                        dataset_embedding=step.obs.dataset_embedding,
                        n_total=0,
                        n_success=0,
                        last_episode_ts=now,
                    )
                    stats_for_action.append(entry)
                entry.n_total += 1
                if success:
                    entry.n_success += 1
                entry.last_episode_ts = now
        return {
            "trajectory_len": float(len(trajectory)),
            "terminal_reward": float(terminal_reward),
            "n_distinct_actions_seen": float(
                len({s.action_id for s in trajectory})
            ),
        }

    def credit_synthetic_trajectory(
        self,
        action_ids: Sequence[int],
        dataset_embedding: tuple[float, ...],
        *,
        ts: float | None = None,
        repetitions: int = 20,
    ) -> None:
        """Credit a sequence of action_ids as a prior successful
        trajectory. Used for warm-starting from curated / LLM /
        BK-Tree-seeded pipelines.

        ``repetitions`` defaults to 20 because a single credit per
        action is diluted quickly under sustained failure: every
        failed rollout bumps ``n_total`` without bumping
        ``n_success``, so ``success_rate = 3 / (3 + N_failures)``
        drops below 1% after a few thousand losing episodes — the
        warm-start bias bleeds away before the policy ever lands a
        success to anchor itself. 20 credits give the prior enough
        headroom to survive ~5k–10k failures while still being
        overridable by real successes (which will push the rate
        upward when they arrive).
        """
        now = ts if ts is not None else time.time()
        with self._stats_lock:
            for aid in action_ids:
                stats_for_action = self._stats.setdefault(aid, [])
                entry = self._match_dataset_entry(
                    stats_for_action, dataset_embedding
                )
                if entry is None:
                    entry = _ActionEpisodeStat(
                        dataset_embedding=dataset_embedding,
                        n_total=0,
                        n_success=0,
                        last_episode_ts=now,
                    )
                    stats_for_action.append(entry)
                entry.n_total += repetitions
                entry.n_success += repetitions
                entry.last_episode_ts = now

    def credit_partial_success(
        self,
        action_ids: Sequence[int],
        dataset_embedding: tuple[float, ...],
        *,
        factor: float = 0.5,
        ts: float | None = None,
    ) -> None:
        """Retroactively credit a partial success on a set of actions.

        Used after the AI Debugger's auto-mitigation rewrites a failing
        pipeline into a working one (see
        ``dorian/event/handlers/rl_error_mitigation.py``). The original
        failure update already ran on the parent's trajectory; this
        method nudges the per-action success counts upward to reflect
        that the parent's choices were "almost right" — the failure
        was a localised bug that a downstream rewrite resolved.

        Semantics — for each ``action_id``:

          * If the action already has a stats entry for the given
            dataset neighbourhood, increment ``n_success`` by
            ``factor`` (a fractional success). ``n_total`` is NOT
            incremented (the parent's failure already counted it once).
          * If there's no entry yet, do nothing — the policy hasn't
            seen this action under this dataset and the regular
            ``update()`` path will create the entry on the next
            episode. Skipping prevents creating a "phantom" success-
            without-trial that would dominate ``success_rate``.

        ``factor`` ∈ (0, 1]. Default 0.5 — a half-success — is a
        defensible midpoint between "the action was wrong" (the
        original failure update) and "the action was right" (a clean
        success). Tune per ablation.
        """
        if factor <= 0.0:
            return
        now = ts if ts is not None else time.time()
        with self._stats_lock:
            for aid in action_ids:
                stats_for_action = self._stats.get(aid)
                if not stats_for_action:
                    continue
                entry = self._match_dataset_entry(
                    stats_for_action, dataset_embedding
                )
                if entry is None:
                    continue
                entry.n_success += factor
                entry.last_episode_ts = now

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prior(
        self,
        action_id: int,
        obs: Observation,
        now: float,
    ) -> float:
        entries = self._stats.get(action_id)
        if not entries:
            # No history -> small nonzero prior so every action stays
            # explorable, but low enough that warm-started actions
            # clearly stand out. See ``unseen_action_prior``.
            return self.unseen_action_prior
        total = 0.0
        for entry in entries:
            cos = cosine_similarity(
                obs.dataset_embedding, entry.dataset_embedding
            )
            if cos <= 0.0:
                continue
            dt = max(0.0, now - entry.last_episode_ts)
            recency = math.pow(0.5, dt / max(self.recency_half_life_secs, 1.0))
            total += cos * entry.success_rate * recency
        # Small floor keeps never-succeeded actions alive for
        # exploration; the cache-affinity nudge provides another
        # small bias on top.
        return 0.25 + total

    @staticmethod
    def _match_dataset_entry(
        entries: list[_ActionEpisodeStat],
        embedding: tuple[float, ...],
        *,
        threshold: float = 0.999,
    ) -> _ActionEpisodeStat | None:
        """Find the entry whose stored embedding is (nearly)
        identical to ``embedding``. Exact-match by cosine > 0.999
        keeps separate dataset-contexts distinct; closer clustering
        is left to future dataset-bucket hashing."""
        for e in entries:
            if cosine_similarity(e.dataset_embedding, embedding) >= threshold:
                return e
        return None

    @staticmethod
    def _read_cache_affinity(
        obs: Observation, cand: ActionCandidate
    ) -> float:
        """Read a precomputed per-action cache-affinity scalar from
        ``obs.extras``. Envs that haven't wired this in get 0.0 and
        the nudge turns into a no-op."""
        aff_map = obs.extras.get("cache_affinity_per_action")
        if isinstance(aff_map, dict):
            return float(aff_map.get(cand.action_id, 0.0))
        return 0.0

    # ------------------------------------------------------------------
    # Introspection helpers (observability / tests)
    # ------------------------------------------------------------------

    def entries_for(self, action_id: int) -> list[_ActionEpisodeStat]:
        """Read-only view of the per-action stats list."""
        return list(self._stats.get(action_id, ()))

    def memory_size(self) -> int:
        return sum(len(v) for v in self._stats.values())
