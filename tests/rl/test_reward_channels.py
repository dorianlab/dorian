"""Integrity tests for the reward-shaping side channels.

Covers the three-way composition (percentile + ranking objectives +
cache affinity) and the invariants we promised in ``rl/env/reward.py``:

* Cold-start safe: all three channels return 0.0 when their data
  source is empty / unavailable, so the env's base reward
  structure is untouched.
* Ordering preserved: the sum of all three channel caps is below
  ``VALIDATOR_CREDIT_CAP_DEFAULT`` — a pipeline that bounces off
  the validator but happens to be cache-adjacent + leaderboard-
  topping still cannot out-score a pipeline that passed validation
  and partial-executed.
* Non-uniform: distinct DAGs never collapse onto identical rewards
  once channels are active (the degeneracy the user flagged).
"""
from __future__ import annotations

from dorian.dag import DAG, Edge, Operator, Parameter

from rl.env.partial_credit import (
    PARTIAL_CREDIT_CAP_DEFAULT,
    VALIDATOR_CREDIT_CAP_DEFAULT,
)
from rl.env.reward import (
    AFFINITY_BONUS_CAP_DEFAULT,
    LeaderboardSnapshot,
    PERCENTILE_BONUS_CAP_DEFAULT,
    RANKING_OBJECTIVE_BONUS_CAP_DEFAULT,
    RewardChannels,
    affinity_bonus,
    compose_bonuses,
    percentile_bonus,
    ranking_objective_bonus,
)


def _trivial_dag() -> DAG:
    dag = DAG()
    dag.nodes["a"] = Operator(name="a", language="python", tasks=[])
    dag.nodes["b"] = Operator(name="b", language="python", tasks=[])
    dag.edges.append(Edge(source="a", destination="b", position=0, output=0))
    return dag


# ---------------------------------------------------------------------------
# Per-channel sanity
# ---------------------------------------------------------------------------

def test_percentile_cold_start_is_zero():
    """Empty / sparse leaderboard — no gradient to build from yet."""
    empty = LeaderboardSnapshot()
    assert percentile_bonus(0.8, "d1", empty) == 0.0
    sparse = LeaderboardSnapshot(
        metric_values_by_dataset={"d1": [0.5, 0.6]},
        min_samples_for_percentile=5,
    )
    assert percentile_bonus(0.8, "d1", sparse) == 0.0


def test_percentile_ranks_in_series():
    """Ascending sorted leaderboard — a metric at the top of the
    distribution gets near-full cap; bottom gets 0."""
    snap = LeaderboardSnapshot(
        metric_values_by_dataset={"d1": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]},
        min_samples_for_percentile=5,
    )
    top = percentile_bonus(0.85, "d1", snap)
    bottom = percentile_bonus(0.05, "d1", snap)
    middle = percentile_bonus(0.45, "d1", snap)
    assert 0.0 <= bottom < middle < top <= PERCENTILE_BONUS_CAP_DEFAULT + 1e-9


def test_ranking_objective_averages_scorers():
    """Average of two [0, 1] scorers — scaled by cap, skips errors."""
    dag = _trivial_dag()
    scorers = (lambda d: 1.0, lambda d: 0.5)
    b = ranking_objective_bonus(dag, scorers)
    assert abs(b - RANKING_OBJECTIVE_BONUS_CAP_DEFAULT * 0.75) < 1e-9

    def broken(_dag):
        raise RuntimeError("ranker blew up")

    # Broken scorer is skipped, not fatal.
    b2 = ranking_objective_bonus(dag, (broken, lambda d: 1.0))
    assert abs(b2 - RANKING_OBJECTIVE_BONUS_CAP_DEFAULT) < 1e-9


def test_ranking_objective_empty_returns_zero():
    assert ranking_objective_bonus(_trivial_dag(), ()) == 0.0


def test_affinity_bonus_scales_with_graph_affinity():
    """Bonus = cap × affinity, clamped."""

    class _FakeGraph:
        def __init__(self, val):
            self._val = val

        def affinity(self, _json):
            return self._val

    dag = _trivial_dag()
    assert (
        abs(affinity_bonus(dag, _FakeGraph(0.5)) - AFFINITY_BONUS_CAP_DEFAULT * 0.5)
        < 1e-9
    )
    # Overflow is clamped so buggy graphs can't blow past the cap.
    assert (
        abs(affinity_bonus(dag, _FakeGraph(5.0)) - AFFINITY_BONUS_CAP_DEFAULT)
        < 1e-9
    )
    # Broken graph is graceful-degraded to 0.

    class _Broken:
        def affinity(self, _json):
            raise RuntimeError("native not built")

    assert affinity_bonus(dag, _Broken()) == 0.0


# ---------------------------------------------------------------------------
# Composition integrity
# ---------------------------------------------------------------------------

def test_compose_channels_are_additive_and_bounded():
    """All three channels active at their caps — the sum must stay
    below VALIDATOR_CREDIT_CAP_DEFAULT so the reward ordering
    (success > exec-partial > validator-partial > bonuses > floor)
    survives. Otherwise a failed-validator graph could out-score a
    successfully-partial-executed one just by being cache-adjacent."""
    dag = _trivial_dag()

    class _TopAffinity:
        def affinity(self, _j):
            return 1.0

    snap = LeaderboardSnapshot(
        metric_values_by_dataset={"d1": [0.1, 0.2, 0.3, 0.4, 0.5]},
        min_samples_for_percentile=5,
    )
    ch = RewardChannels(
        leaderboard=snap,
        ranking_scorers=(lambda d: 1.0,),
        experiment_graph=_TopAffinity(),
    )
    total = compose_bonuses(dag, metric_value=0.99, dataset_id="d1", channels=ch)
    expected = (
        PERCENTILE_BONUS_CAP_DEFAULT
        + RANKING_OBJECTIVE_BONUS_CAP_DEFAULT
        + AFFINITY_BONUS_CAP_DEFAULT
    )
    assert abs(total - expected) < 1e-9
    # Invariant — validator-partial cap dominates the sum of all
    # three side channels so the reward ordering cannot flip.
    assert total <= VALIDATOR_CREDIT_CAP_DEFAULT + 1e-9
    assert total < PARTIAL_CREDIT_CAP_DEFAULT


def test_compose_channels_none_returns_zero():
    """Stand-alone env use (no trainer) — compose returns 0 when
    channels are None, preserving the base reward structure."""
    assert compose_bonuses(
        _trivial_dag(), metric_value=0.8, dataset_id="d1", channels=None
    ) == 0.0


# ---------------------------------------------------------------------------
# End-to-end integrity: no uniform reward across distinct DAGs
# ---------------------------------------------------------------------------

def test_distinct_failing_graphs_get_distinct_rewards_with_channels():
    """The user's original complaint: every failing episode returned
    -0.201. With channels active, distinct graphs on the same
    failure path must map to distinct rewards — even when neither
    partial_credit nor validator_partial_credit can fire."""
    from rl.env.partial_credit import baseline_structural_credit

    class _AffinityA:
        def affinity(self, _j):
            return 0.2

    class _AffinityB:
        def affinity(self, _j):
            return 0.8

    # Two distinct graphs.
    a = DAG()
    a.nodes["only"] = Operator(name="only", language="python", tasks=[])
    b = DAG()
    for nid in ["p", "q", "metric"]:
        b.nodes[nid] = Operator(name=nid, language="python", tasks=[])
    b.edges.append(Edge(source="p", destination="q", position=0, output=0))
    b.edges.append(Edge(source="q", destination="metric", position=0, output=0))

    # Same failure path for both: no exec result, no validator hit.
    # Only baseline-structural credit + reward channels fire.
    base_a = -0.2 + baseline_structural_credit(a, metric_node_id=None)
    base_b = -0.2 + baseline_structural_credit(b, metric_node_id="metric")

    ch_a = RewardChannels(
        leaderboard=None,
        ranking_scorers=(),
        experiment_graph=_AffinityA(),
    )
    ch_b = RewardChannels(
        leaderboard=None,
        ranking_scorers=(),
        experiment_graph=_AffinityB(),
    )
    ra = base_a + compose_bonuses(a, metric_value=None, dataset_id=None, channels=ch_a)
    rb = base_b + compose_bonuses(b, metric_value=None, dataset_id=None, channels=ch_b)
    assert ra != rb
    # Both still below zero — this is a failure path, channels nudge
    # but don't resurrect the reward into positive territory.
    assert ra < 0.0 and rb < 0.0


def test_success_with_all_channels_beats_any_failure_combination():
    """Ordering invariant under full channel stacking: a successful
    pipeline at the bottom of the leaderboard must still beat a
    failing pipeline that happens to be cache-adjacent + ranking-
    objective-perfect."""
    from rl.env.partial_credit import (
        VALIDATOR_CREDIT_CAP_DEFAULT,
        baseline_structural_credit,
    )

    class _Affinity:
        def __init__(self, v):
            self._v = v

        def affinity(self, _j):
            return self._v

    success_dag = _trivial_dag()
    fail_dag = _trivial_dag()
    fail_dag.nodes["metric"] = Operator(name="metric", language="python", tasks=[])

    snap = LeaderboardSnapshot(
        metric_values_by_dataset={"d1": [0.5, 0.6, 0.7, 0.8, 0.9]},
        min_samples_for_percentile=5,
    )
    # Success at the low end of the leaderboard: 0.5 → 0th percentile.
    success_ch = RewardChannels(
        leaderboard=snap,
        ranking_scorers=(lambda d: 0.0,),
        experiment_graph=_Affinity(0.0),
    )
    # Failure at the top of every side channel.
    fail_ch = RewardChannels(
        leaderboard=snap,
        ranking_scorers=(lambda d: 1.0,),
        experiment_graph=_Affinity(1.0),
    )
    success_reward = 0.5 + compose_bonuses(
        success_dag, metric_value=0.5, dataset_id="d1", channels=success_ch
    )
    fail_reward = (
        -0.2
        + VALIDATOR_CREDIT_CAP_DEFAULT  # worst-case: validator partial maxed
        + baseline_structural_credit(fail_dag, metric_node_id="metric")
        + compose_bonuses(
            fail_dag, metric_value=None, dataset_id="d1", channels=fail_ch
        )
    )
    assert success_reward > fail_reward


# ---------------------------------------------------------------------------
# Env integration
# ---------------------------------------------------------------------------

def test_env_uses_reward_channels_on_evaluate_terminal_success_path():
    """When reward_channels are attached, the env adds the composed
    bonus to the success-path reward. No channels → no bonus, same
    number as before."""
    from rl.env.dorian_env import DorianPipelineEnv
    from rl.env.executor import ExecutorResult
    from rl.catalog.loader import seed_catalog_with_guards

    class _Aff:
        def affinity(self, _j):
            return 1.0

    env = DorianPipelineEnv(catalog=seed_catalog_with_guards())
    env.reset("credit-g")
    env._last_executor_result = ExecutorResult(
        success=True, metric_value=0.8, metric_node_id="m",
        wall_clock_secs=0.0,
    )
    # Simulate the success branch of _evaluate_terminal by calling
    # _compose_bonuses directly — this is the same helper the
    # terminal path uses, so any drift here reveals wire-up bugs.
    without = env._compose_bonuses(metric_value=0.8)
    assert without == 0.0
    env.reward_channels = RewardChannels(
        leaderboard=LeaderboardSnapshot(
            metric_values_by_dataset={"credit-g": [0.1, 0.2, 0.3, 0.4, 0.5]},
            min_samples_for_percentile=5,
        ),
        ranking_scorers=(lambda d: 1.0,),
        experiment_graph=_Aff(),
    )
    with_channels = env._compose_bonuses(metric_value=0.8)
    assert with_channels > 0.0
    assert with_channels <= (
        PERCENTILE_BONUS_CAP_DEFAULT
        + RANKING_OBJECTIVE_BONUS_CAP_DEFAULT
        + AFFINITY_BONUS_CAP_DEFAULT
        + 1e-9
    )
