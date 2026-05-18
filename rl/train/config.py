"""Trainer configuration: dataset pool, hyperparams, policy
selector. One place to flip ablations."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrainerConfig:
    """End-to-end RL trainer configuration.

    All fields are overridable from env vars so the compose
    service reads its configuration from the environment without
    code changes.
    """

    # --- Datasets ---
    dataset_ids: tuple[str, ...] = ("credit-g", "kr-vs-kp", "mfeat-fourier")

    # --- Policy ablation selector ---
    # one of: "memory" | "hedge" | "hybrid"
    policy_kind: str = "hybrid"
    hybrid_epsilon: float = 0.1
    hedge_eta: float = 0.1
    memory_epsilon_cache: float = 0.1

    # --- Rollout shape ---
    n_episodes: int = 100
    # Floor analysis for 5 primitives + compound add_node expansion:
    # the frozen scoring cage has 3 model/fit/predict adds + 5 wires
    # (X_train->fit, y_train->fit, fit->predict, X_test->predict,
    # predict.y_pred->metric[1]) + commit ≈ 9 steps bare minimum.
    # Cold Memory/Hedge policies need 3-5× that for exploration +
    # mis-wire recovery + mitigation-triggered re-execution. 60
    # is the working-margin default; override via DORIAN_RL_MAX_STEPS.
    max_steps_per_episode: int = 60
    seed: int = 42

    # --- Commit / cache integration ---
    commit_to_experiment_graph: bool = True
    substitute_canonical_forms: bool = False  # Tier-D add-on

    # --- Env flags ---
    # Enables the ``ChangeParamValueSpec`` action primitive — the
    # cheapest expansion path for the pipeline library. The policy
    # can mutate any Parameter node to a KB-declared alternative
    # value and generate a structurally-identical-but-distinct
    # pipeline (instance_hash sees the change). Default ON now
    # that candidate values come from the KB (choices / low / high
    # / log_scale) rather than the old hardcoded 2×/1.5× toggle.
    param_tuning_enabled: bool = True
    auto_seed_on_commit: bool = True

    # --- Logging ---
    log_every_n_episodes: int = 5

    # --- Reward shaping channels (rl/env/reward.py) ---
    # Leaderboard percentile + ranking-objective + cache-affinity
    # bonuses. All three degrade gracefully to 0 bonus when their
    # data source is empty / unavailable — safe to leave on at
    # cold-start. Disable only if an ablation needs a clean base
    # reward signal.
    reward_channels_enabled: bool = True
    # How often to refresh the Postgres-backed leaderboard snapshot
    # (in episodes). Percentile bonus uses this cached snapshot;
    # refreshing every-episode thrashes Postgres, never-refreshing
    # ossifies the signal. One batch of log_every matches well.
    leaderboard_refresh_every_n_episodes: int = 25

    # --- Elastic parallelism (rl/train/elastic_scaler.py) ---
    # Parallelism ceiling for episode rollouts. 1 = sequential
    # (legacy behaviour). The scaler adjusts up to this cap based on
    # host health — never above. Set conservatively on shared hosts.
    parallelism_ceiling: int = 4
    parallelism_floor: int = 1
    # Fraction of host cores the trainer is allowed to occupy at peak.
    scaler_target_host_cpu_share: float = 0.30
    # Set False to run the old sequential loop (disables scaler).
    parallelism_enabled: bool = True

    # --- Warm-start priors (rl/train/priors.py) ---
    # Pre-credit action sequences from known-good pipelines so the
    # policy has non-uniform priors before its first organic success.
    # Two sources: a curated JSON file and the Postgres pipelines table.
    warm_start_enabled: bool = True
    warm_start_priors_path: str = "rl/train/llm_priors.json"
    warm_start_bktree_limit: int = 200
    warm_start_bktree_enabled: bool = True
    warm_start_strength: float = 1.0

    @classmethod
    def from_env(cls) -> "TrainerConfig":
        """Read overrides from environment variables."""
        def _get(key: str, default: str) -> str:
            return os.environ.get(key, default)

        def _get_int(key: str, default: int) -> int:
            try:
                return int(os.environ.get(key, str(default)))
            except ValueError:
                return default

        def _get_float(key: str, default: float) -> float:
            try:
                return float(os.environ.get(key, str(default)))
            except ValueError:
                return default

        def _get_bool(key: str, default: bool) -> bool:
            v = os.environ.get(key)
            if v is None:
                return default
            return v.lower() in ("1", "true", "yes", "on")

        dsets = os.environ.get(
            "DORIAN_RL_DATASETS", "credit-g,kr-vs-kp,mfeat-fourier"
        )
        return cls(
            dataset_ids=tuple(x.strip() for x in dsets.split(",") if x.strip()),
            policy_kind=_get("DORIAN_RL_POLICY", "hybrid"),
            hybrid_epsilon=_get_float("DORIAN_RL_HYBRID_EPSILON", 0.1),
            hedge_eta=_get_float("DORIAN_RL_HEDGE_ETA", 0.1),
            memory_epsilon_cache=_get_float("DORIAN_RL_MEMORY_EPS_CACHE", 0.1),
            n_episodes=_get_int("DORIAN_RL_EPISODES", 100),
            max_steps_per_episode=_get_int("DORIAN_RL_MAX_STEPS", 60),
            seed=_get_int("DORIAN_RL_SEED", 42),
            commit_to_experiment_graph=_get_bool("DORIAN_RL_COMMIT", True),
            substitute_canonical_forms=_get_bool("DORIAN_RL_CANONICAL_SUBST", False),
            param_tuning_enabled=_get_bool("DORIAN_RL_PARAM_TUNING", True),
            auto_seed_on_commit=_get_bool("DORIAN_RL_AUTO_SEED", True),
            log_every_n_episodes=_get_int("DORIAN_RL_LOG_EVERY", 5),
            reward_channels_enabled=_get_bool("DORIAN_RL_REWARD_CHANNELS", True),
            leaderboard_refresh_every_n_episodes=_get_int(
                "DORIAN_RL_LEADERBOARD_REFRESH_EVERY", 25
            ),
            parallelism_ceiling=_get_int("DORIAN_RL_PARALLELISM_CEILING", 4),
            parallelism_floor=_get_int("DORIAN_RL_PARALLELISM_FLOOR", 1),
            scaler_target_host_cpu_share=_get_float(
                "DORIAN_RL_SCALER_HOST_CPU_SHARE", 0.30
            ),
            parallelism_enabled=_get_bool("DORIAN_RL_PARALLELISM", True),
            warm_start_enabled=_get_bool("DORIAN_RL_WARM_START", True),
            warm_start_priors_path=_get(
                "DORIAN_RL_PRIORS_PATH", "rl/train/llm_priors.json"
            ),
            warm_start_bktree_limit=_get_int(
                "DORIAN_RL_WARM_START_BKTREE_LIMIT", 200
            ),
            warm_start_bktree_enabled=_get_bool(
                "DORIAN_RL_WARM_START_BKTREE", True
            ),
            warm_start_strength=_get_float("DORIAN_RL_WARM_START_STRENGTH", 1.0),
        )
