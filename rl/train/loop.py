"""Rollout loop. No SB3 / torch dependency -- just the Policy
protocol + the env + ExperimentGraph commit.

Invariant: analytical reads (affinity/match/plan) can happen per
step on the partial pipeline; mutating commit happens once per
episode at the terminal step. See
(internal design note; not in public repo).

Parallelism: the loop runs each batch of episodes through a
``ThreadPoolExecutor`` sized by the ``ElasticScaler``. Each worker
owns its own ``DorianPipelineEnv`` instance but shares the
process-wide ``ActionSpace`` and ``Policy`` so action-id stats
accumulate consistently across workers. Policy mutation is serialised
via locks inside ``MemoryPolicy._stats_lock`` / ``HedgePolicy._weights_lock``.
Sequential mode (``parallelism_enabled=False``) preserves the
legacy single-worker behaviour for ablations.

Warm-start: on trainer init we credit each curated / BK-Tree prior
as a synthetic successful trajectory, so the policy's action-prior
over known-good shapes starts non-uniform before the first organic
rollout.
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from rl.catalog.loader import seed_catalog_with_guards
from rl.env import DorianPipelineEnv
from rl.env.action_space import ActionSpace
from rl.env.datasets import CC18_SUBSET, load_public_datasets
from rl.exec import ExperimentGraph
from rl.policy import (
    HedgePolicy,
    HybridPolicy,
    MemoryPolicy,
    Observation,
    Policy,
    Transition,
)
from rl.policy.base import ActionCandidate as PolicyActionCandidate

from .config import TrainerConfig
from .elastic_scaler import ElasticScaler

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Episode rollout
# ---------------------------------------------------------------------------


@dataclass
class EpisodeReport:
    episode: int
    dataset_id: str
    steps: int
    terminal_reward: float
    wall_clock_secs: float
    final_class_hash: str
    valid_pipeline: bool
    info_tail: dict = field(default_factory=dict)


def rollout_episode(
    env: DorianPipelineEnv,
    policy: Policy,
    dataset_id: str,
    *,
    episode_idx: int,
) -> tuple[list[Transition], EpisodeReport]:
    t0 = time.time()
    env_obs = env.reset(dataset_id)
    trajectory: list[Transition] = []
    last_info: dict = {}
    while not env.done:
        cands, mask = env.available_actions()
        if not cands:
            # No valid actions -- force terminate.
            break
        # Convert env candidates to policy-side candidates.
        policy_cands = [
            PolicyActionCandidate(
                action_id=c.action_id,
                op_key=_action_op_key(c),
            )
            for c in cands
        ]
        obs = Observation(
            dag_json=env_obs.dag_json,
            dataset_embedding=env_obs.dataset_embedding,
            step_idx=env_obs.step_idx,
            remaining_budget=env_obs.remaining_budget,
            extras=env_obs.extras,
        )
        action_id = policy.select(obs, policy_cands, mask)
        step = env.step(action_id)
        last_info = step.info
        # Shortcut attribution: when the env's auto-close fires on a
        # commit step, inject a synthetic Transition PER wire it
        # resolved, so the policy's prior for each AddEdge action_id
        # accumulates organically. Without these injections the
        # shortcut would silently do the work and the policy would
        # never learn those edges — the "training wheels never come
        # off" failure mode. Synthetic transitions carry reward=0 so
        # the gradient sits on the commit step (which pays
        # ``len(wires) × auto_close_tax``, favouring unassisted
        # closures over time).
        auto_close = step.info.get("auto_close") if isinstance(step.info, dict) else None
        wires = getattr(auto_close, "wires", ()) if auto_close is not None else ()
        for wire in wires:
            trajectory.append(
                Transition(
                    obs=obs,
                    action_id=wire.action_id,
                    reward=0.0,
                    next_obs=None,
                    terminal=False,
                )
            )
        trajectory.append(
            Transition(
                obs=obs,
                action_id=action_id,
                reward=step.reward,
                next_obs=None,
                terminal=step.done,
            )
        )
        env_obs = step.obs

    terminal_reward = trajectory[-1].reward if trajectory else 0.0
    return trajectory, EpisodeReport(
        episode=episode_idx,
        dataset_id=dataset_id,
        steps=len(trajectory),
        terminal_reward=terminal_reward,
        wall_clock_secs=time.time() - t0,
        final_class_hash=env_obs.class_hash,
        valid_pipeline=env.pipeline_is_valid(),
        info_tail=dict(last_info),
    )


def _action_op_key(c) -> str:
    """Extract a human-readable op_key from an env ActionCandidate
    for observability. The AddNode candidates carry `op`; other
    primitives don't -- we fall back to the abstract form."""
    if c.op is not None:
        return c.op.op_key
    # Synthesise from the spec type for remove/add_edge cases.
    return type(c.spec).__name__


# ---------------------------------------------------------------------------
# Policy + warm-start
# ---------------------------------------------------------------------------


def _make_policy(cfg: TrainerConfig) -> Policy:
    if cfg.policy_kind == "memory":
        return MemoryPolicy(seed=cfg.seed, epsilon_cache=cfg.memory_epsilon_cache)
    if cfg.policy_kind == "hedge":
        return HedgePolicy(seed=cfg.seed, eta=cfg.hedge_eta)
    if cfg.policy_kind == "hybrid":
        return HybridPolicy(
            seed=cfg.seed,
            epsilon=cfg.hybrid_epsilon,
            memory=MemoryPolicy(
                seed=cfg.seed, epsilon_cache=cfg.memory_epsilon_cache
            ),
            hedge=HedgePolicy(seed=cfg.seed + 1, eta=cfg.hedge_eta),
        )
    raise ValueError(f"unknown policy_kind: {cfg.policy_kind}")


def _warm_start(
    cfg: TrainerConfig,
    policy: Policy,
    action_space: ActionSpace,
    catalog: tuple,
    datasets,
) -> None:
    """Credit curated + BK-Tree priors as synthetic successes.

    Degrades silently on any failure — the trainer keeps running;
    the only loss is the warm-start signal. The user can disable
    either source via env vars."""
    if not cfg.warm_start_enabled:
        return

    from .priors import (
        load_bktree_priors,
        load_llm_priors,
        warm_start_policy,
    )

    # Dataset embeddings keyed by dataset name. MUST match what the
    # env produces at rollout time — otherwise warm-start credits
    # land at embedding A while organic rollouts key on embedding B
    # and the cosine similarity never lights up for the right
    # dataset. Profile-derived vectors (measured from the CSV via
    # ``compute_dataset_profile``) are authoritative; the legacy
    # ``DatasetRegistry.feature_embedding`` heuristic is the
    # graceful fallback for datasets that fail to profile.
    from rl.env.datasets import DatasetRegistry, load_dataset
    from rl.priors.profile import compute_dataset_profile
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os as _os
    reg = DatasetRegistry()
    dataset_embeddings: dict[str, tuple[float, ...]] = {}

    # Parallelise dataset profiling — each call independently reads
    # one CSV, runs sklearn / numpy metafeature extractors, and emits
    # a feature vector. The metafeature numpy / sklearn calls drop
    # the GIL on the heavy paths (impute / nanmean / KMeans), so a
    # ThreadPoolExecutor scales near-linearly up to the host's CPU
    # share for the trainer container (``cpus: 64`` in compose).
    # On a fresh trainer boot with 72 datasets this drops warm-start
    # from ~6 minutes serial to ~30 seconds on a 32-core host.
    pool_size = max(4, min(32, (_os.cpu_count() or 8)))

    def _profile_one(d):
        try:
            csv_path = load_dataset(d, reg)
            # Prefer the cached canonical profile from
            # ``expdb.datasets[].profile`` — same 51 metafeatures the
            # backend already computed on upload. Skips a 2-5s
            # CSV-read + sklearn / numpy metafeature pass per dataset
            # which dominated warm-start cost (≈6 minutes serial for
            # 72 datasets, even before the trainer touches the rl
            # rollout loop). Falls back to inline profiling when
            # ``catalogue_id`` is empty (e.g. CC18_SUBSET).
            profile = compute_dataset_profile(
                csv_path, name=d.name,
                dataset_id=getattr(d, "catalogue_id", "") or None,
            )
            return d.name, profile.feature_vector(), None
        except Exception as exc:
            try:
                return d.name, reg.feature_embedding(d), exc
            except Exception:
                return d.name, None, exc

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = [pool.submit(_profile_one, d) for d in datasets]
        for fut in as_completed(futures):
            name, vec, exc = fut.result()
            if vec is not None:
                dataset_embeddings[name] = vec
            if exc is not None:
                _log.warning(
                    "warm_start: profile for %s failed (%s) — fell back to legacy embedding",
                    name, exc,
                )

    catalog_by_op = {op.op_key: op for op in catalog}

    total_priors = 0
    total_credited = 0

    # Curated LLM-authored priors.
    try:
        priors_path = Path(cfg.warm_start_priors_path)
        if not priors_path.is_absolute():
            # Repo-relative; resolve against the cwd.
            priors_path = Path.cwd() / priors_path
        llm_priors = load_llm_priors(priors_path)
        counts = warm_start_policy(
            policy, llm_priors, action_space, dataset_embeddings,
            strength=cfg.warm_start_strength,
        )
        total_priors += counts["priors"]
        total_credited += counts["actions_credited"]
        _log.info(
            "warm_start[llm]: %d priors, %d actions credited",
            counts["priors"], counts["actions_credited"],
        )
    except Exception as exc:
        _log.warning("warm_start[llm] failed (%s) — continuing without", exc)

    # BK-Tree decomposition from Postgres.
    if cfg.warm_start_bktree_enabled:
        try:
            bk_priors = load_bktree_priors(
                catalog_by_op, limit=cfg.warm_start_bktree_limit,
            )
            counts = warm_start_policy(
                policy, bk_priors, action_space, dataset_embeddings,
                strength=cfg.warm_start_strength,
            )
            total_priors += counts["priors"]
            total_credited += counts["actions_credited"]
            _log.info(
                "warm_start[bktree]: %d priors, %d actions credited",
                counts["priors"], counts["actions_credited"],
            )
        except Exception as exc:
            _log.warning(
                "warm_start[bktree] failed (%s) — continuing without", exc
            )

    try:
        from backend.events import Event, emit
        emit(Event("WarmStartComplete", {
            "priors_loaded": total_priors,
            "actions_credited": total_credited,
            "action_space_size": len(action_space),
        }))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Env factory (shared vs. per-worker)
# ---------------------------------------------------------------------------


def _make_env(
    cfg: TrainerConfig,
    catalog,
    datasets,
    action_space,
    *,
    prior_source=None,
) -> DorianPipelineEnv:
    """Build one env instance. Each worker owns its own env (the DAG
    state is per-episode), but they SHARE the action_space + the
    prior source so id assignments and MCP-injected recommendations
    accumulate consistently across workers."""
    return DorianPipelineEnv(
        catalog=catalog,
        datasets=datasets,
        action_space=action_space,
        max_steps=cfg.max_steps_per_episode,
        param_tuning_enabled=cfg.param_tuning_enabled,
        auto_seed_on_commit=cfg.auto_seed_on_commit,
        prior_source=prior_source,
    )


# ---------------------------------------------------------------------------
# Main training loop
# ---------------------------------------------------------------------------


def train(
    cfg: TrainerConfig | None = None,
    *,
    policy: Policy | None = None,
    action_space: ActionSpace | None = None,
) -> Iterable[EpisodeReport]:
    """Generator over episode reports. Yields after each episode
    so callers can tee to logs + dashboards.

    Batching: the outer loop runs ``cfg.n_episodes`` total. With
    parallelism > 1, episodes within a batch run concurrently and
    are yielded in completion order (not submission order). Between
    batches the ``ElasticScaler`` resamples host health and may
    resize the next batch.

    When a ``policy`` + ``action_space`` pair is passed in, the
    trainer reuses them across calls so cross-batch learning
    accumulates. Continuous-mode callers (``rl.train.main``) build
    the policy once and thread it through every ``train()`` call;
    fresh calls get a fresh policy with a one-shot warm-start."""
    cfg = cfg or TrainerConfig.from_env()
    rng = random.Random(cfg.seed)
    # Discover datasets.
    discovered = load_public_datasets() or CC18_SUBSET
    catalog = seed_catalog_with_guards()

    # ONE ActionSpace for the whole trainer so workers' id assignments
    # agree. Must be constructed before envs + policies observe it.
    if action_space is None:
        action_space = ActionSpace()

    # If ``cfg.dataset_ids`` is the literal "all", sample from every
    # discovered dataset. Otherwise keep the user-supplied whitelist.
    dataset_ids: tuple[str, ...]
    if len(cfg.dataset_ids) == 1 and cfg.dataset_ids[0].lower() == "all":
        dataset_ids = tuple(d.name for d in discovered)
    else:
        have = {d.name for d in discovered}
        dataset_ids = tuple(n for n in cfg.dataset_ids if n in have) or tuple(
            d.name for d in discovered
        )

    if policy is None:
        policy = _make_policy(cfg)
        _warm_start(cfg, policy, action_space, catalog, discovered)

    eg: ExperimentGraph | None = None
    if cfg.commit_to_experiment_graph:
        try:
            eg = ExperimentGraph()
        except ImportError as exc:
            _log.warning(
                "ExperimentGraph disabled (%s). Run with "
                "DORIAN_RL_COMMIT=false to silence.", exc,
            )
            eg = None

    from rl.train.reward_channels import build_reward_channels
    reward_channels = build_reward_channels(
        dataset_ids,
        experiment_graph=eg,
        enabled=cfg.reward_channels_enabled,
    )

    # Scaler and env pool.
    scaler = ElasticScaler(
        min_parallelism=cfg.parallelism_floor,
        max_parallelism=cfg.parallelism_ceiling,
        target_host_cpu_share=cfg.scaler_target_host_cpu_share,
    )

    # Build the configured prior source ONCE and share it across all
    # workers. Default backend is Null (no LLM / MCP dependency);
    # swap via DORIAN_RL_PRIOR_BACKEND=openai|mcp for active priors.
    # OpenAIChatPriorSource needs the catalog's op_key list upfront
    # so its prompt can constrain the model's output to valid ops.
    # Pre-load debugger rewrite docs + exception patterns into
    # process-local caches. Both subsystems would otherwise try to
    # ``asyncio.run`` during a rollout — guaranteed to collide with
    # the env's active event loop. One-shot init sidesteps the
    # contention entirely.
    try:
        from rl.env.debugger import prime_rewrite_cache
        prime_rewrite_cache()
    except Exception as _exc:
        _log.info("debugger cache prime skipped (%s)", _exc)

    from rl.priors import build_prior_source
    prior_source = build_prior_source()
    _catalog_op_keys = tuple(op.op_key for op in catalog)
    if hasattr(prior_source, "set_catalog"):
        try:
            prior_source.set_catalog(_catalog_op_keys)
        except Exception:
            pass

    if not cfg.parallelism_enabled:
        # Sequential legacy path.
        env = _make_env(cfg, catalog, discovered, action_space, prior_source=prior_source)
        env.reward_channels = reward_channels
        yield from _sequential_train(
            cfg, env, policy, rng, dataset_ids, eg, reward_channels,
        )
        return

    # Parallel path: one env per worker slot.
    pool_size = max(1, cfg.parallelism_ceiling)
    envs: list[DorianPipelineEnv] = []
    for _ in range(pool_size):
        e = _make_env(cfg, catalog, discovered, action_space, prior_source=prior_source)
        e.reward_channels = reward_channels
        envs.append(e)

    episode_counter = 0
    executor = ThreadPoolExecutor(
        max_workers=pool_size, thread_name_prefix="rl-rollout"
    )
    try:
        while episode_counter < cfg.n_episodes:
            batch_size = min(
                scaler.current_parallelism,
                cfg.n_episodes - episode_counter,
            )

            # Refresh leaderboard snapshot at batch boundary (matches
            # the old every-25-episodes cadence for the common config).
            if (
                episode_counter > 0
                and episode_counter % cfg.leaderboard_refresh_every_n_episodes
                < batch_size
            ):
                reward_channels = build_reward_channels(
                    dataset_ids,
                    experiment_graph=eg,
                    enabled=cfg.reward_channels_enabled,
                )
                for e in envs:
                    e.reward_channels = reward_channels

            futures: dict[Future, DorianPipelineEnv] = {}
            for slot in range(batch_size):
                dataset_id = rng.choice(dataset_ids)
                env = envs[slot]
                ep_idx = episode_counter + slot
                fut = executor.submit(
                    rollout_episode, env, policy, dataset_id,
                    episode_idx=ep_idx,
                )
                futures[fut] = env

            # Yield in completion order, mapping each future back to
            # its worker env so final_dag / final_dag_json read from
            # the right instance.
            for fut in as_completed(futures):
                slot_env = futures[fut]
                try:
                    trajectory, report = fut.result()
                except Exception as exc:
                    _log.warning("rollout failed: %s", exc, exc_info=True)
                    continue
                scaler.record_episode_outcome(
                    timed_out=_looks_timed_out(report)
                )
                if (
                    eg is not None
                    and report.valid_pipeline
                    and slot_env.final_dag is not None
                ):
                    try:
                        eg.commit_episode(
                            slot_env.final_dag_json,
                            terminal_reward=report.terminal_reward,
                            artifact="feature",
                            compute_secs=report.wall_clock_secs,
                        )
                    except Exception as exc:
                        _log.warning("ExperimentGraph commit failed: %s", exc)
                if report.valid_pipeline and slot_env.final_dag is not None:
                    try:
                        from rl.train.persistence import commit_rl_pipeline_sync
                        commit_rl_pipeline_sync(
                            dag_json=slot_env.final_dag_json,
                            class_hash=report.final_class_hash,
                            dataset_id=report.dataset_id,
                            terminal_reward=report.terminal_reward,
                            wall_clock_secs=report.wall_clock_secs,
                            policy_kind=cfg.policy_kind,
                            episode=report.episode,
                        )
                    except Exception as exc:  # pragma: no cover
                        _log.warning("rl-v2 leaderboard persist failed: %s", exc)
                # Policy update — serialised by the policy's internal lock.
                stats = policy.update(trajectory)
                if report.episode % cfg.log_every_n_episodes == 0:
                    report.info_tail["policy_stats"] = stats
                    if eg is not None:
                        report.info_tail["experiment_graph_size"] = len(eg)
                    report.info_tail["scaler_parallelism"] = scaler.current_parallelism
                yield report

            episode_counter += batch_size

            # Batch boundary: let the scaler resample + decide.
            scaler.decide()
    finally:
        executor.shutdown(wait=True)


def _looks_timed_out(report: EpisodeReport) -> bool:
    """Heuristic: the episode's info_tail has ``error_type=="TimeoutError"``
    or the rollout wall-clock exceeded the executor's wall-cap. Surfaces
    self-starvation to the scaler."""
    info = report.info_tail or {}
    if info.get("error_type") == "TimeoutError":
        return True
    return False


def _sequential_train(
    cfg: TrainerConfig,
    env: DorianPipelineEnv,
    policy: Policy,
    rng: random.Random,
    dataset_ids: tuple[str, ...],
    eg,
    reward_channels,
) -> Iterator[EpisodeReport]:
    """Legacy single-worker path. Preserved for ablation / debug."""
    from rl.train.reward_channels import build_reward_channels
    for episode in range(cfg.n_episodes):
        if (
            env.reward_channels is not None
            and episode > 0
            and episode % cfg.leaderboard_refresh_every_n_episodes == 0
        ):
            env.reward_channels = build_reward_channels(
                dataset_ids,
                experiment_graph=eg,
                enabled=cfg.reward_channels_enabled,
            )
        dataset_id = rng.choice(dataset_ids)
        trajectory, report = rollout_episode(
            env, policy, dataset_id, episode_idx=episode
        )
        if eg is not None and report.valid_pipeline and env.final_dag is not None:
            eg.commit_episode(
                env.final_dag_json,
                terminal_reward=report.terminal_reward,
                artifact="feature",
                compute_secs=report.wall_clock_secs,
            )
        if report.valid_pipeline and env.final_dag is not None:
            try:
                import asyncio as _asyncio
                from rl.train.persistence import commit_rl_pipeline
                _asyncio.run(commit_rl_pipeline(
                    dag_json=env.final_dag_json,
                    class_hash=report.final_class_hash,
                    dataset_id=report.dataset_id,
                    terminal_reward=report.terminal_reward,
                    wall_clock_secs=report.wall_clock_secs,
                    policy_kind=cfg.policy_kind,
                    episode=report.episode,
                ))
            except Exception as exc:  # pragma: no cover
                _log.warning("rl-v2 leaderboard persist failed: %s", exc)
        stats = policy.update(trajectory)
        if episode % cfg.log_every_n_episodes == 0:
            report.info_tail["policy_stats"] = stats
            if eg is not None:
                report.info_tail["experiment_graph_size"] = len(eg)
        yield report
