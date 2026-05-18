"""Smoke tests for the trainer-side channel assembly.

Infrastructure-free: no Postgres, no docstore, no pyo3. These verify
the graceful-degradation contract + the built-in ranking-scorer
variants' shapes.
"""
from __future__ import annotations

from dorian.dag import DAG, Edge, Operator, Parameter

from rl.train.reward_channels import (
    build_reward_channels,
    get_ranking_scorers,
    guardrail_presence_scorer,
    load_leaderboard_snapshot,
    parametrisation_scorer,
    set_ranking_scorers,
    simplicity_scorer,
)


def test_leaderboard_snapshot_empty_dataset_ids_skips_postgres():
    """Zero datasets → empty snapshot, no Postgres call attempted."""
    snap = load_leaderboard_snapshot([])
    assert snap.metric_values_by_dataset == {}


def test_leaderboard_snapshot_graceful_on_missing_postgres(monkeypatch):
    """Postgres pool unavailable → warning + empty snapshot, never
    crashes the trainer."""
    async def _boom(_):
        raise RuntimeError("pg pool unreachable")

    import rl.train.reward_channels as rc
    monkeypatch.setattr(rc, "_fetch_evaluations", _boom)
    snap = load_leaderboard_snapshot(["credit-g"])
    assert snap.metric_values_by_dataset == {}


def test_build_reward_channels_disabled_returns_none():
    """Ablation switch — disabled returns None, env falls back to
    base reward structure."""
    assert build_reward_channels(["credit-g"], enabled=False) is None


def test_simplicity_scorer_prefers_small_graphs():
    small = DAG()
    for i in range(3):
        small.nodes[f"n{i}"] = Operator(name=f"n{i}", language="python", tasks=[])
    big = DAG()
    for i in range(30):
        big.nodes[f"n{i}"] = Operator(name=f"n{i}", language="python", tasks=[])
    assert simplicity_scorer(small) == 1.0
    assert simplicity_scorer(big) == 0.0
    # Monotonic in between.
    mid = DAG()
    for i in range(10):
        mid.nodes[f"n{i}"] = Operator(name=f"n{i}", language="python", tasks=[])
    assert 0.0 < simplicity_scorer(mid) < 1.0


def test_parametrisation_scorer_reflects_param_ratio():
    dag = DAG()
    for i in range(10):
        dag.nodes[f"op{i}"] = Operator(name=f"op{i}", language="python", tasks=[])
    # No params — score 0.
    assert parametrisation_scorer(dag) == 0.0
    # One param per op — ratio 1.0 ≥ 0.3 cap, score 1.0.
    for i in range(10):
        dag.nodes[f"p{i}"] = Parameter(name=f"p{i}", dtype="int", value="1")
    assert parametrisation_scorer(dag) == 1.0


def test_guardrail_presence_scorer_detects_trust_guardrails_operators():
    dag = DAG()
    dag.nodes["m"] = Operator(
        name="sklearn.ensemble.RandomForestClassifier",
        language="python", tasks=[],
    )
    assert guardrail_presence_scorer(dag) == 0.0
    dag.nodes["g"] = Operator(
        name="trust_guardrails.UnitaryToxicBert",
        language="python", tasks=[],
    )
    assert guardrail_presence_scorer(dag) == 1.0


def test_ranking_scorers_are_swappable():
    """Trainer code can replace the built-in variants — the channel
    infrastructure stays identical so downstream code doesn't
    branch on which scorers are active."""
    original = get_ranking_scorers()
    try:
        set_ranking_scorers((lambda d: 0.5,))
        assert len(get_ranking_scorers()) == 1
    finally:
        set_ranking_scorers(original)
