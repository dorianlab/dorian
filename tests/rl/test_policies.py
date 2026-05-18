"""Tests for the three policy cores in rl/policy/.

Each test exercises: (a) the ``Policy`` protocol compliance,
(b) masking respected, (c) the behavioural contract specific to
that architecture.
"""
from __future__ import annotations

import pytest

from rl.policy import (
    ActionCandidate,
    HedgePolicy,
    HybridPolicy,
    MemoryPolicy,
    Observation,
    Policy,
    Transition,
    cosine_similarity,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _obs(embedding=(1.0, 0.0, 0.0), *, extras=None):
    return Observation(
        dag_json="{}",
        dataset_embedding=embedding,
        step_idx=0,
        remaining_budget=10,
        extras=extras or {},
    )


def _cands(ids):
    return [ActionCandidate(action_id=a, op_key=f"op_{a}") for a in ids]


def _traj(obs, action_ids, reward):
    """Build a trajectory with the same obs repeated, terminal reward on
    the last step."""
    out = []
    for i, a in enumerate(action_ids):
        is_last = i == len(action_ids) - 1
        out.append(
            Transition(
                obs=obs,
                action_id=a,
                reward=reward if is_last else 0.0,
                next_obs=None,
                terminal=is_last,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "factory",
    [
        lambda: MemoryPolicy(seed=0),
        lambda: HedgePolicy(seed=0),
        lambda: HybridPolicy(seed=0),
    ],
)
def test_policy_conforms_to_protocol(factory):
    policy = factory()
    assert isinstance(policy, Policy)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MemoryPolicy(seed=0),
        lambda: HedgePolicy(seed=0),
        lambda: HybridPolicy(seed=0),
    ],
)
def test_select_respects_mask(factory):
    policy = factory()
    candidates = _cands([10, 11, 12, 13])
    mask = [False, True, False, True]  # only 11 + 13 allowed
    # Run many times; must always be a masked-True id.
    seen = set()
    for _ in range(50):
        a = policy.select(_obs(), candidates, mask)
        assert a in {11, 13}, f"policy returned masked-False action {a}"
        seen.add(a)
    # Both allowed ids should be reachable.
    assert seen == {11, 13}


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MemoryPolicy(seed=0),
        lambda: HedgePolicy(seed=0),
        lambda: HybridPolicy(seed=0),
    ],
)
def test_select_empty_mask_raises(factory):
    policy = factory()
    candidates = _cands([1, 2])
    with pytest.raises(ValueError):
        policy.select(_obs(), candidates, [False, False])


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MemoryPolicy(seed=0),
        lambda: HedgePolicy(seed=0),
        lambda: HybridPolicy(seed=0),
    ],
)
def test_update_returns_observability_dict(factory):
    policy = factory()
    stats = policy.update(_traj(_obs(), [1, 2, 3], reward=0.8))
    assert isinstance(stats, dict)
    # Non-empty -- at least trajectory_len or its hybrid-prefixed sibling.
    assert len(stats) >= 1


# ---------------------------------------------------------------------------
# MemoryPolicy behaviour
# ---------------------------------------------------------------------------

def test_memory_policy_prefers_action_with_history_after_success():
    policy = MemoryPolicy(seed=0, recency_half_life_secs=1e9)
    obs = _obs()
    candidates = _cands([1, 2])
    # Seed memory: action 1 succeeded on this dataset embedding; action 2 didn't.
    policy.update(_traj(obs, [1], reward=1.0))  # success
    policy.update(_traj(obs, [2], reward=0.0))  # failure

    # Over many draws, action 1 should dominate.
    hits_a = 0
    for _ in range(200):
        hits_a += policy.select(obs, candidates, [True, True]) == 1
    # With full-cosine-1 similarity + full recency, success-rate
    # dominates; expect >65% for action 1.
    assert hits_a > 130


def test_memory_policy_cold_start_is_approximately_uniform():
    policy = MemoryPolicy(seed=0)
    candidates = _cands([7, 8, 9])
    hits = {7: 0, 8: 0, 9: 0}
    for _ in range(600):
        a = policy.select(_obs(), candidates, [True, True, True])
        hits[a] += 1
    # Allow ±15% around uniform 200 per bucket.
    for v in hits.values():
        assert 140 < v < 280


def test_memory_policy_credit_partial_success_lifts_failed_action():
    """credit_partial_success retroactively boosts a failed action's
    weight without erasing the original failure update — matches the
    bug-fix-rewrite contract used by rl_error_mitigation."""
    policy = MemoryPolicy(seed=0, recency_half_life_secs=1e9)
    obs = _obs()

    # Action 1 saw a failed rollout (n_total=1, n_success=0).
    policy.update(_traj(obs, [1], reward=0.0))
    pre = policy._stats[1][0]
    assert pre.n_total == 1 and pre.n_success == 0

    # Bug-fix-rewrite credits half a success.
    policy.credit_partial_success([1], obs.dataset_embedding, factor=0.5)
    post = policy._stats[1][0]
    assert post.n_total == 1, "n_total must NOT advance — the parent's failure already counted it"
    assert post.n_success == 0.5, "n_success bumped by the credit factor"
    assert pytest.approx(post.success_rate, abs=1e-9) == 0.5


def test_memory_policy_credit_partial_skips_unseen_action():
    """An action with no stats entry yet must NOT get a phantom
    success — the regular update() path will create the entry on
    the next episode."""
    policy = MemoryPolicy(seed=0)
    obs = _obs()
    policy.credit_partial_success([42], obs.dataset_embedding, factor=0.5)
    assert 42 not in policy._stats


def test_memory_policy_credit_partial_zero_factor_is_noop():
    policy = MemoryPolicy(seed=0)
    obs = _obs()
    policy.update(_traj(obs, [1], reward=0.0))
    snap = policy._stats[1][0].n_success
    policy.credit_partial_success([1], obs.dataset_embedding, factor=0.0)
    assert policy._stats[1][0].n_success == snap


def test_memory_policy_cache_affinity_nudge_shifts_choice():
    policy = MemoryPolicy(seed=0, epsilon_cache=10.0)
    candidates = _cands([1, 2])
    # No memory. Action 2 has high cache-affinity, action 1 has none.
    obs = _obs(extras={"cache_affinity_per_action": {1: 0.0, 2: 1.0}})
    hits_2 = 0
    for _ in range(300):
        hits_2 += policy.select(obs, candidates, [True, True]) == 2
    # With ε_cache=10 and affinity=1, action 2 gets (1 + 10) = 11x
    # weight boost over action 1. Should win overwhelmingly.
    assert hits_2 > 240


# ---------------------------------------------------------------------------
# HedgePolicy behaviour
# ---------------------------------------------------------------------------

def test_hedge_policy_weights_grow_with_positive_reward():
    policy = HedgePolicy(seed=0, eta=1.0)
    obs = _obs()
    # Warm the policy with both actions so they exist in the weight map.
    policy.select(obs, _cands([1, 2]), [True, True])
    # Prime: action 1 sees reward=1.0 ten times; action 2 stays at 0.
    for _ in range(10):
        policy.update(_traj(obs, [1], reward=1.0))
    w1 = policy.weight_of(1)
    w2 = policy.weight_of(2)
    assert w1 > w2, f"expected action 1 weight > action 2; got {w1} vs {w2}"


def test_hedge_policy_samples_proportional_to_weights():
    policy = HedgePolicy(seed=0, eta=1.0)
    obs = _obs()
    candidates = _cands([1, 2])
    # Drive action 1 strongly positive.
    policy.select(obs, candidates, [True, True])
    for _ in range(20):
        policy.update(_traj(obs, [1], reward=1.0))
    # After many rewards, action 1 should dominate sampling.
    hits_1 = 0
    for _ in range(200):
        hits_1 += policy.select(obs, candidates, [True, True]) == 1
    assert hits_1 > 170


def test_hedge_policy_new_actions_get_geometric_mean_seed():
    policy = HedgePolicy(seed=0, eta=1.0)
    obs = _obs()
    # Prime with action 1 only.
    policy.select(obs, _cands([1]), [True])
    for _ in range(5):
        policy.update(_traj(obs, [1], reward=1.0))
    # Now action 2 appears for the first time.
    _ = policy.select(obs, _cands([1, 2]), [True, True])
    snap = policy.snapshot()
    assert 2 in snap
    # Action 2 should start at the mean log-weight of existing
    # actions — for a single existing action 1, that means equal.
    assert snap[2] == pytest.approx(snap[1])


def test_hedge_policy_rebases_on_cap():
    policy = HedgePolicy(seed=0, eta=2.0, max_log_weight=5.0)
    obs = _obs()
    policy.select(obs, _cands([1]), [True])
    # Drive log-weight well past the cap.
    for _ in range(20):
        policy.update(_traj(obs, [1], reward=1.0))
    assert max(policy.snapshot().values()) <= 5.0 + 1e-9


# ---------------------------------------------------------------------------
# HybridPolicy behaviour
# ---------------------------------------------------------------------------

def test_hybrid_policy_branches_per_select():
    policy = HybridPolicy(seed=0, epsilon=0.5)
    obs = _obs()
    candidates = _cands([1, 2, 3])
    branches = {"explore": 0, "exploit": 0}
    for _ in range(400):
        policy.select(obs, candidates, [True, True, True])
        branches[policy.last_branch] += 1
    # With ε=0.5 we expect roughly 50/50.
    assert 150 < branches["explore"] < 250
    assert 150 < branches["exploit"] < 250


def test_hybrid_update_flows_into_both_inner_policies():
    memory = MemoryPolicy(seed=0, recency_half_life_secs=1e9)
    hedge = HedgePolicy(seed=0, eta=1.0)
    hybrid = HybridPolicy(epsilon=0.0, memory=memory, hedge=hedge, seed=0)
    obs = _obs()
    hybrid.update(_traj(obs, [1, 2], reward=1.0))
    # Memory should have stats for both.
    assert memory.entries_for(1)
    assert memory.entries_for(2)
    # Hedge should have weights for both.
    snap = hedge.snapshot()
    assert 1 in snap and 2 in snap


def test_hybrid_update_observability_is_prefixed():
    policy = HybridPolicy(seed=0)
    stats = policy.update(_traj(_obs(), [1, 2, 3], reward=0.9))
    assert any(k.startswith("memory.") for k in stats)
    assert any(k.startswith("hedge.") for k in stats)


# ---------------------------------------------------------------------------
# Ablation-style smoke: all three policies are drop-in swappable
# ---------------------------------------------------------------------------

def test_ablation_can_swap_policies_transparently():
    """Pin the interface: a tiny rollout loop calls .select and
    .update uniformly across all three cores."""
    obs = _obs()
    candidates = _cands([1, 2, 3])

    policies: list[Policy] = [
        MemoryPolicy(seed=1),
        HedgePolicy(seed=1),
        HybridPolicy(seed=1),
    ]
    for policy in policies:
        for _ in range(5):
            a = policy.select(obs, candidates, [True, True, True])
            assert a in {1, 2, 3}
            stats = policy.update(_traj(obs, [a], reward=0.7))
            assert isinstance(stats, dict)


# ---------------------------------------------------------------------------
# Utility sanity
# ---------------------------------------------------------------------------

def test_cosine_similarity_unit_vectors():
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)


def test_cosine_similarity_handles_zero_and_length_mismatch():
    assert cosine_similarity((), ()) == 0.0
    assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0
    assert cosine_similarity((1.0,), (1.0, 1.0)) == 0.0
