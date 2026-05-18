"""Entry point for the rl-trainer service.

Run stand-alone:
    uv run python -m rl.train.main

Or in the compose stack as the ``rl-trainer`` service. Config
comes from env vars (see ``rl.train.config.TrainerConfig``).
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
from pathlib import Path

from .config import TrainerConfig
from .loop import train


def _format_report(r) -> str:
    tag = "ok " if r.valid_pipeline else "    "
    return (
        f"[ep {r.episode:>4d}] {tag} ds={r.dataset_id:<14} "
        f"steps={r.steps:>3d} "
        f"reward={r.terminal_reward:>7.3f} "
        f"wall={r.wall_clock_secs:>6.2f}s "
        f"class={r.final_class_hash[:8]}"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("rl.train")
    cfg = TrainerConfig.from_env()
    log.info("trainer config: %s", cfg)

    # Continuous mode: when DORIAN_RL_CONTINUOUS=1 the trainer keeps
    # looping ``train(cfg)`` batches so the agent runs as a persistent
    # background process (the v1 GenerationScheduler behaviour it's
    # replacing). Policy + ActionSpace are constructed ONCE and reused
    # across every batch so memory / hedge weights accumulate (the
    # warm-start also runs only once, not once per batch).
    import os as _os
    continuous = _os.environ.get("DORIAN_RL_CONTINUOUS", "").lower() in (
        "1", "true", "yes", "on",
    )

    # Build persistent policy + action space once. The first ``train()``
    # call will warm-start the policy; subsequent calls skip re-warming
    # because the policy + action_space are passed through.
    from rl.env.action_space import ActionSpace
    from .loop import _make_policy, _warm_start
    from rl.catalog.loader import seed_catalog_with_guards
    from rl.env.datasets import load_public_datasets, CC18_SUBSET
    from rl.policy.persistence import load_policy_into, save_policy
    persistent_action_space = ActionSpace()
    persistent_policy = _make_policy(cfg)
    _warm_start(
        cfg, persistent_policy, persistent_action_space,
        seed_catalog_with_guards(),
        load_public_datasets() or CC18_SUBSET,
    )

    # Restore any state saved by a previous trainer run so learned
    # per-action stats / hedge weights carry across restarts. Load
    # happens AFTER warm-start so that disk state (which has already
    # seen real rollouts) overwrites the cold warm-start seed values
    # for keys it covers; keys the saved state doesn't cover keep
    # their warm-start priors.
    policy_state_path = Path(_os.environ.get(
        "DORIAN_RL_POLICY_STATE_PATH",
        "/app/volumes/rl_policy/state.pickle",
    ))
    if load_policy_into(persistent_policy, policy_state_path):
        log.info("policy state restored from %s", policy_state_path)
    else:
        log.info(
            "policy state cold-start (no prior snapshot at %s)",
            policy_state_path,
        )

    # Persist on every graceful exit path. ``atexit`` covers normal
    # termination; SIGTERM (podman stop) and SIGINT (Ctrl-C) need
    # explicit handlers because atexit doesn't fire on signals that
    # kill the process without a clean Python exit.
    def _save_and_exit(signum=None, _frame=None) -> None:
        ok = save_policy(persistent_policy, policy_state_path)
        log.info("policy state saved on exit (ok=%s)", ok)
        if signum is not None:
            # Re-raise so the default handler tears down properly.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    atexit.register(_save_and_exit)
    signal.signal(signal.SIGTERM, _save_and_exit)
    signal.signal(signal.SIGINT, _save_and_exit)

    # Cross-product + AutoML engines run as a separate Rust binary
    # (``engine/dorian-engines``) — see docker-compose.yml service
    # ``engines``. They write to the same evaluations table this
    # trainer reads from. RL itself will migrate into the same Rust
    # binary in a follow-up port, at which point this Python entry
    # point retires entirely.

    # Cold-start epsilon decay. The ``HybridPolicy`` default of
    # epsilon=0.1 means 90% of action selections go through
    # MemoryPolicy — which falls back to a thin prior when history
    # is sparse, so cold starts spend most rollouts re-sampling
    # near-uniform garbage. Start higher (heavy Hedge exploration,
    # where multiplicative weights actually adapt fast) and decay
    # linearly to the config value over the first N episodes so the
    # policy transitions cleanly into the exploit regime once its
    # memory has enough per-action mass to be useful.
    #
    # Disable the schedule by setting ``DORIAN_RL_EPSILON_START`` to
    # the same value as ``DORIAN_RL_HYBRID_EPSILON`` (or 0, which
    # also disables).
    epsilon_start = float(_os.environ.get("DORIAN_RL_EPSILON_START", "0.5"))
    epsilon_end = cfg.hybrid_epsilon
    epsilon_decay_episodes = int(_os.environ.get(
        "DORIAN_RL_EPSILON_DECAY_EPISODES", "1000"
    ))

    def _scheduled_epsilon(episodes_so_far: int) -> float:
        if epsilon_decay_episodes <= 0 or epsilon_start <= epsilon_end:
            return epsilon_end
        progress = min(1.0, episodes_so_far / epsilon_decay_episodes)
        return epsilon_start + (epsilon_end - epsilon_start) * progress

    total_episodes = 0
    total_successes = 0
    batch = 0
    while True:
        batch += 1
        # Apply scheduled epsilon for this batch. HybridPolicy exposes
        # ``epsilon`` as a plain dataclass field; mutation between
        # batches is safe because worker rollout threads are quiesced
        # at this point.
        if hasattr(persistent_policy, "epsilon"):
            new_eps = _scheduled_epsilon(total_episodes)
            if abs(new_eps - persistent_policy.epsilon) > 1e-6:
                log.info(
                    "epsilon schedule: %.3f → %.3f (%d/%d episodes)",
                    persistent_policy.epsilon, new_eps,
                    total_episodes, epsilon_decay_episodes,
                )
                persistent_policy.epsilon = new_eps
        # Apply any partial-credit attempts whose child pipeline
        # succeeded since the last batch. Idempotent — each attempt
        # is flipped to ``credited=True`` after application. See
        # ``rl/train/partial_credit.py`` for the full contract.
        try:
            import asyncio as _asyncio
            from rl.train.partial_credit import drain_partial_credits
            _credited = _asyncio.run(drain_partial_credits(persistent_policy))
            if _credited:
                log.info("partial-credit drain: %d attempts applied", _credited)
        except Exception as _exc:
            log.warning("partial-credit drain skipped: %s", _exc)

        log.info("starting batch %d (%d episodes)", batch, cfg.n_episodes)
        batch_successes = 0
        for report in train(
            cfg,
            policy=persistent_policy,
            action_space=persistent_action_space,
        ):
            total_episodes += 1
            log.info(_format_report(report))
            if report.valid_pipeline:
                batch_successes += 1
                total_successes += 1
        log.info(
            "batch %d done: %d/%d valid  (total %d/%d across %d batches)",
            batch, batch_successes, cfg.n_episodes,
            total_successes, total_episodes, batch,
        )
        # Snapshot after every batch so a crash / OOM loses at most
        # one batch of learning, not everything since startup.
        if save_policy(persistent_policy, policy_state_path):
            log.debug("policy state snapshot written to %s", policy_state_path)

        # Merge any pipelines that landed in Postgres since the last
        # batch (FLAML seeder, user submissions, etc.) into the live
        # BK-Tree so warm-start neighbourhood queries pick them up
        # without a trainer restart.
        try:
            from rl.train.persistence import refresh_bk_tree_sync
            added = refresh_bk_tree_sync()
            if added:
                log.info(
                    "BK-Tree refresh: %d new priors merged from Postgres",
                    added,
                )
        except Exception:
            log.exception("BK-Tree refresh failed")

        if not continuous:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
