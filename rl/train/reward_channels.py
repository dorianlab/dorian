"""Trainer-side assembly of the reward-shaping channels.

The env (``rl/env/reward.py``) defines the *shape* of the bonuses
(bounded, additive, degradation-safe). This module fills them in with
live data from Dorian's persistence layer:

* **Postgres ``evaluations``** → ``LeaderboardSnapshot``. Same-dataset
  percentile ranks the terminal metric against prior runs.
* **pyo3 ``ExperimentGraph``** → ``affinity`` wrapper. Already
  constructed in ``rl/train/loop.py``; we thread it through.
* **Ranking-objective scorers** → built-in safe variants by default.
  User-defined ``RankingObjective`` Snippets in the docstore are NOT executed
  here — arbitrary-code execution from the RL trainer is a security
  boundary we refuse to cross silently. When a sandboxed executor is
  available, register it via ``set_ranking_scorers``.

All loaders graceful-degrade: Postgres unavailable / docstore unavailable /
ExperimentGraph disabled → empty channel, 0 bonus. The trainer keeps
running; only the side-channel signal narrows.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Iterable

from dorian.dag import DAG, Operator, Parameter

from rl.env.reward import (
    LeaderboardSnapshot,
    RankingScorer,
    RewardChannels,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leaderboard snapshot (Postgres)
# ---------------------------------------------------------------------------

async def _fetch_evaluations(dataset_ids: Iterable[str]) -> list[dict]:
    """Fetch leaderboard evaluations from Postgres.

    The RL trainer is a SEPARATE process from the FastAPI backend
    that lazily initialises the ExperimentStore singleton during
    lifespan. In our worker we have to do it ourselves on first
    use — guarded so repeat episodes don't re-init.
    """
    try:
        from dorian.experiment.store import (
            get_experiment_store,
            init_experiment_store,
        )
        try:
            store = await get_experiment_store()
        except RuntimeError:
            store = await init_experiment_store()
        return await store.get_evaluations_for_datasets(list(dataset_ids))
    except Exception as exc:  # pragma: no cover -- infra optional
        _log.warning("leaderboard snapshot fetch failed: %s", exc)
        return []


def load_leaderboard_snapshot(
    dataset_ids: Iterable[str],
    *,
    metric_name: str = "terminal_reward",
    min_samples: int = 5,
) -> LeaderboardSnapshot:
    """Return a ``LeaderboardSnapshot`` built from Postgres ``evaluations``.

    Filters to ``metric_name`` so the percentile compares like-for-like
    (terminal_reward vs terminal_reward, not accuracy vs AUC). Ascending
    sort is required by the ``bisect`` lookup in ``percentile_bonus``.
    """
    dataset_ids = list(dataset_ids)
    if not dataset_ids:
        return LeaderboardSnapshot()
    rows: list[dict] = []
    try:
        rows = asyncio.run(_fetch_evaluations(dataset_ids))
    except RuntimeError as exc:
        # ``asyncio.run`` raises when called from inside a running
        # event loop; fall back to ``run_until_complete`` on the
        # active loop. Any OTHER RuntimeError (pool unreachable,
        # etc.) degrades to an empty snapshot — never crashes the
        # trainer.
        msg = str(exc).lower()
        if "running event loop" in msg or "cannot be called" in msg:
            try:
                loop = asyncio.get_event_loop()
                rows = loop.run_until_complete(_fetch_evaluations(dataset_ids))
            except Exception as exc2:
                _log.warning("leaderboard snapshot load failed: %s", exc2)
        else:
            _log.warning("leaderboard snapshot load failed: %s", exc)
    except Exception as exc:  # pragma: no cover
        _log.warning("leaderboard snapshot load failed: %s", exc)
    by_dataset: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get("metric_name") != metric_name:
            continue
        v = row.get("metric_value")
        if v is None:
            continue
        try:
            by_dataset[row["dataset_id"]].append(float(v))
        except (TypeError, ValueError):
            continue
    for dsid in by_dataset:
        by_dataset[dsid].sort()
    return LeaderboardSnapshot(
        metric_values_by_dataset=dict(by_dataset),
        min_samples_for_percentile=min_samples,
    )


# ---------------------------------------------------------------------------
# Built-in ranking-objective variants (safe, deterministic)
# ---------------------------------------------------------------------------
#
# These stand in for the user-defined ``RankingObjective`` Snippets in
# docstore. Each returns a float in [0, 1] given a DAG. Evaluation is
# pure-Python, no external calls. When a sandboxed Snippet executor is
# wired up, replace via ``set_ranking_scorers``.


def simplicity_scorer(dag: DAG) -> float:
    """Prefers smaller graphs (fewer nodes / less wiring) — a weak
    prior toward parsimonious pipelines. 1.0 for a 3-node graph,
    decays toward 0 past ~30 nodes."""
    n = len(dag.nodes)
    if n <= 3:
        return 1.0
    if n >= 30:
        return 0.0
    return max(0.0, 1.0 - (n - 3) / 27.0)


def parametrisation_scorer(dag: DAG) -> float:
    """Prefers pipelines with hyperparameter wiring — unparameterised
    pipelines rely on defaults, which is a weaker optimisation target.
    Capped at 0.3 operator-to-parameter ratio (beyond that is probably
    parameter sprawl, not tuning)."""
    ops = sum(1 for n in dag.nodes.values() if isinstance(n, Operator))
    params = sum(1 for n in dag.nodes.values() if isinstance(n, Parameter))
    if ops == 0:
        return 0.0
    ratio = params / ops
    return max(0.0, min(1.0, ratio / 0.3))


def guardrail_presence_scorer(dag: DAG) -> float:
    """Prefers pipelines that include at least one guardrail operator.
    Aligns the agent with the TRUST-AI compliance objective without
    enforcing it — the agent can still ship un-guardrailed pipelines,
    they just earn slightly less reward."""
    for node in dag.nodes.values():
        if not isinstance(node, Operator):
            continue
        name = (node.name or "").lower()
        if "guard" in name or "trust_guardrails" in name:
            return 1.0
    return 0.0


_DEFAULT_SCORERS: tuple[RankingScorer, ...] = (
    simplicity_scorer,
    parametrisation_scorer,
    guardrail_presence_scorer,
)

_active_scorers: tuple[RankingScorer, ...] = _DEFAULT_SCORERS


def set_ranking_scorers(scorers: tuple[RankingScorer, ...]) -> None:
    """Swap the process-wide ranking-scorer tuple. Call before
    starting the trainer to override the built-in variants with
    e.g. docstore-sourced objectives executed via a sandboxed
    evaluator."""
    global _active_scorers
    _active_scorers = tuple(scorers)


def get_ranking_scorers() -> tuple[RankingScorer, ...]:
    return _active_scorers


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------

def build_reward_channels(
    dataset_ids: Iterable[str],
    *,
    experiment_graph=None,
    enabled: bool = True,
) -> RewardChannels | None:
    """Assemble all three channels into a ``RewardChannels`` bundle.

    ``None`` return signals "shaping disabled" — the env falls back
    to its base reward structure (still non-uniform thanks to
    partial-credit + baseline-structural, but the additional signal
    channels are off).
    """
    if not enabled:
        return None
    snapshot = load_leaderboard_snapshot(dataset_ids)
    return RewardChannels(
        leaderboard=snapshot,
        ranking_scorers=get_ranking_scorers(),
        experiment_graph=experiment_graph,
    )


__all__ = [
    "build_reward_channels",
    "get_ranking_scorers",
    "guardrail_presence_scorer",
    "load_leaderboard_snapshot",
    "parametrisation_scorer",
    "set_ranking_scorers",
    "simplicity_scorer",
]
