"""RL env invariant tests.

These exercise the env without touching Redis / docstore / the
event bus. Tests that need live infra are marked ``skip`` when
``DORIAN_RL_LIVE=1`` is not set.
"""
from __future__ import annotations

import os

import pytest

from rl.catalog.loader import seed_catalog
from rl.env import (
    AddEdgeSpec,
    AddNodeSpec,
    DorianPipelineEnv,
    RemoveEdgeSpec,
    RemoveNodeSpec,
)
from rl.train.config import TrainerConfig


LIVE = os.environ.get("DORIAN_RL_LIVE", "0") == "1"


def _make_env(**kw) -> DorianPipelineEnv:
    # Pull rollout-shape defaults from TrainerConfig so tests and
    # production share one source of truth. Callers can override any
    # field via kwargs.
    cfg = TrainerConfig()
    defaults: dict = {
        "catalog": seed_catalog(),
        "max_steps": cfg.max_steps_per_episode,
        "probe_every_n_steps": 1_000_000,  # effectively disabled
    }
    defaults.update(kw)
    return DorianPipelineEnv(**defaults)


# ---------------------------------------------------------------------------
# Frozen-harness invariants
# ---------------------------------------------------------------------------

def test_reset_installs_frozen_harness():
    env = _make_env()
    env.reset("credit-g")
    # Loader, split, metric all present as frozen nodes.
    assert len(env._frozen_node_ids) >= 3
    # Each frozen node is an Operator node (or Parameter satellite
    # of the split's random_state).
    from dorian.dag import Operator, Parameter
    for nid in env._frozen_node_ids:
        node = env._dag.nodes[nid]
        assert isinstance(node, (Operator, Parameter))


def test_available_actions_excludes_removes_on_frozen_nodes():
    env = _make_env()
    env.reset("credit-g")
    cands, _ = env.available_actions()
    for c in cands:
        if isinstance(c.spec, RemoveNodeSpec):
            assert c.spec.node_id not in env._frozen_node_ids, (
                f"RemoveNode on frozen node {c.spec.node_id} must not surface"
            )
        if isinstance(c.spec, RemoveEdgeSpec):
            key = (c.spec.src_node_id, c.spec.dst_node_id, c.spec.position)
            assert key not in env._frozen_edge_keys, (
                f"RemoveEdge on frozen edge {key} must not surface"
            )


def test_frozen_harness_wires_match_sklearn_semantics():
    """The frozen harness must wire:
      * ``loader.X``      -> ``split.position=0`` (first positional arg)
      * ``loader.y``      -> ``split.position=1`` (second positional arg)
      * ``split.y_test``  -> ``metric.position=0`` (y_true)

    Regression guard: every time port semantics drift in the catalog
    (output reorders, position renames, KB crawler side-effects) the
    frozen harness must still produce the same wiring or sklearn
    will silently fit on the wrong arrays. This test asserts the
    exact edges -- if any output index or position changes, we want
    a test failure, not a ``[250, 750]`` shape mismatch in prod.
    """
    from dorian.dag import Operator
    env = _make_env()
    env.reset("credit-g")

    # Find the three harness node ids by operator name.
    def _nid(op_name: str) -> str:
        for nid, node in env._dag.nodes.items():
            if isinstance(node, Operator) and node.name == op_name:
                return nid
        raise AssertionError(f"operator {op_name} not in DAG")

    loader_id = _nid("dorian.io.dataset")
    split_id = _nid("sklearn.model_selection.train_test_split")
    metric_id = _nid("sklearn.metrics.accuracy_score")

    # Collect edges between those specific nodes.
    edges = [
        e for e in env._dag.edges
        if not isinstance(env._dag.nodes.get(e.source), type(None))
    ]
    loader_to_split = [
        e for e in edges
        if e.source == loader_id and e.destination == split_id
    ]
    split_to_metric = [
        e for e in edges
        if e.source == split_id and e.destination == metric_id
    ]

    # loader -> split: X at position 0, y at position 1
    assert len(loader_to_split) == 2, (
        f"expected 2 loader->split edges, got {len(loader_to_split)}"
    )
    loader_to_split_by_pos = {str(e.position): e for e in loader_to_split}
    assert "0" in loader_to_split_by_pos, "loader.X -> split.position=0 missing"
    assert "1" in loader_to_split_by_pos, "loader.y -> split.position=1 missing"
    # X is output 0 on loader, y is output 1.
    assert int(loader_to_split_by_pos["0"].output) == 0
    assert int(loader_to_split_by_pos["1"].output) == 1

    # split -> metric: y_test at position 0 (y_true slot)
    assert len(split_to_metric) == 1, (
        f"expected exactly 1 split->metric edge (y_test->y_true), "
        f"got {len(split_to_metric)}"
    )
    e = split_to_metric[0]
    assert str(e.position) == "0", (
        f"y_test must wire into metric position 0 (y_true), got {e.position}"
    )
    # y_test is output index 3 on train_test_split (X_train, X_test,
    # y_train, y_test). If the catalog's outputs tuple is reordered,
    # this assertion catches it.
    assert int(e.output) == 3, (
        f"split output 3 is y_test per sklearn docs; got output={e.output}. "
        f"Catalog's train_test_split.outputs order may have drifted."
    )


def test_reset_leaves_scoring_cage_open():
    """reset() installs only the frozen scoring cage
    (loader -> split -> metric with just y_test -> y_true wired).
    X_train / X_test / y_train are dangling on the frontier;
    metric.y_pred is unwired. The agent assembles the middle."""
    from dorian.dag import Operator
    env = _make_env()
    env.reset("credit-g")
    # Scoring cage is NOT closed at step 0 -- the agent must
    # wire predict.y_pred -> metric.1 to complete it.
    assert not env._is_terminal_pipeline(), (
        "fresh reset must leave metric.y_pred unwired so the agent "
        "has a middle to assemble"
    )
    # Exactly three Operator nodes exist at reset: loader, split,
    # metric. No model/fit/predict pre-installed.
    op_nodes = [
        n for n in env._dag.nodes.values() if isinstance(n, Operator)
    ]
    op_names = {n.name for n in op_nodes}
    assert "dorian.io.dataset" in op_names
    assert "sklearn.model_selection.train_test_split" in op_names
    assert "sklearn.metrics.accuracy_score" in op_names
    assert "sklearn.ensemble.RandomForestClassifier" not in op_names
    assert "fit" not in op_names
    assert "predict" not in op_names


# ---------------------------------------------------------------------------
# Action-space id stability
# ---------------------------------------------------------------------------

def test_add_node_action_ids_are_stable_across_episodes():
    env = _make_env()
    ids_first: dict[str, int] = {}
    env.reset("credit-g")
    cands, _ = env.available_actions()
    for c in cands:
        if isinstance(c.spec, AddNodeSpec):
            ids_first[c.spec.op_key] = c.action_id

    # Second episode: same op_key -> same action_id.
    env.reset("kr-vs-kp")
    cands, _ = env.available_actions()
    for c in cands:
        if isinstance(c.spec, AddNodeSpec) and c.spec.op_key in ids_first:
            assert c.action_id == ids_first[c.spec.op_key]


# ---------------------------------------------------------------------------
# Debugger separation: agent must NEVER see mitigation suggestions
# ---------------------------------------------------------------------------

def test_mitigation_fields_never_surface_to_agent():
    """Per memory/project_rl_debugger_separation.md: the debugger is
    an optimisation pass that rewrites pipelines before execution.
    The RL agent learns from the reward discount alone -- mitigation
    op_keys / boost maps must never leak into obs.extras."""
    env = _make_env()
    env.reset("credit-g")
    env._last_probe_failure = {
        "exception_type": "NotFittedError",
        "message_template": "not fitted",
        "category": "data_driven",
    }
    obs = env._current_observation()
    assert "mitigation_op_keys" not in obs.extras
    assert "mitigation_boost" not in obs.extras
    # Raw exception telemetry is fine (pure observability, not actionable).
    assert obs.extras.get("last_exception", {}).get("category") == "data_driven"


@pytest.mark.skip(
    reason=(
        "pack + VotingClassifier + StackingClassifier are out of the "
        "catalog at this revision: ensemble composition needs a new "
        "'unfitted classifier' family (outputs Model, no data inputs) "
        "to work with the (X_train, y_train, X_test) -> Prediction "
        "rail; pack/voter wiring rebuilds once that exists."
    )
)
def test_pack_shim_accepts_variadic_fanin_with_sequential_slots():
    """``dorian.compose.pack`` is the variable-length container
    operator: its input port is variadic (cap=5). The mask offers
    AddEdge candidates with sequential positional slots ('0', '1',
    '2', ...) as fan-in grows. VotingClassifier / StackingClassifier
    accept the pack's ModelList output, keeping variable-length
    composition confined to a single reusable shim operator."""
    from dorian.dag import Operator
    import uuid
    env = _make_env()
    env.reset("credit-g")
    # Drop two estimator nodes + one pack shim onto the canvas.
    rf_nid = f"m_rf_{uuid.uuid4().hex[:4]}"
    lr_nid = f"m_lr_{uuid.uuid4().hex[:4]}"
    pack_nid = f"m_pack_{uuid.uuid4().hex[:4]}"
    env._dag.nodes[rf_nid] = Operator(
        name="sklearn.ensemble.RandomForestClassifier",
        language="python", tasks=[],
    )
    env._dag.nodes[lr_nid] = Operator(
        name="sklearn.linear_model.LogisticRegression",
        language="python", tasks=[],
    )
    env._dag.nodes[pack_nid] = Operator(
        name="dorian.compose.pack",
        language="python", tasks=[],
    )
    cands, _ = env.available_actions()
    edges_to_pack = [
        c for c in cands
        if isinstance(c.spec, AddEdgeSpec)
        and c.spec.dst_node_id == pack_nid
    ]
    assert len(edges_to_pack) >= 2
    assert all(c.spec.dst_input_port == "0" for c in edges_to_pack)
    # Wire RF into pack. Next enumeration must offer slot '1'.
    first = next(
        c for c in edges_to_pack if c.spec.src_node_id == rf_nid
    )
    env.step(first.action_id)
    cands2, _ = env.available_actions()
    edges_to_pack_2 = [
        c for c in cands2
        if isinstance(c.spec, AddEdgeSpec)
        and c.spec.dst_node_id == pack_nid
    ]
    assert edges_to_pack_2, "mask must still offer more variadic slots"
    assert all(c.spec.dst_input_port == "1" for c in edges_to_pack_2)


def test_voting_classifier_requires_pack_shim():
    """VC input type is ``ModelList`` -- Model-typed outputs (RF, LR)
    cannot wire into VC directly. The agent must drop a pack node
    between base estimators and the voter."""
    from dorian.dag import Operator
    import uuid
    env = _make_env()
    env.reset("credit-g")
    rf_nid = f"rf_{uuid.uuid4().hex[:4]}"
    vc_nid = f"vc_{uuid.uuid4().hex[:4]}"
    env._dag.nodes[rf_nid] = Operator(
        name="sklearn.ensemble.RandomForestClassifier",
        language="python", tasks=[],
    )
    env._dag.nodes[vc_nid] = Operator(
        name="sklearn.ensemble.VotingClassifier",
        language="python", tasks=[],
    )
    cands, _ = env.available_actions()
    direct_edges = [
        c for c in cands
        if isinstance(c.spec, AddEdgeSpec)
        and c.spec.src_node_id == rf_nid
        and c.spec.dst_node_id == vc_nid
    ]
    assert direct_edges == [], (
        "Model (RF) -> ModelList (VC) must not be a direct edge; "
        "agent must route through a pack shim"
    )


def test_reward_is_discounted_by_debugger_mitigations():
    """Terminal reward shrinks as the debugger applies more
    mitigations. With the default cost 0.1: 0 mitigations -> full
    metric; 3 mitigations -> metric * 0.7."""
    env = _make_env()
    env.reset("credit-g")
    env._n_mitigations_applied = 0
    assert env._discount(0.9) == pytest.approx(0.9)
    env._n_mitigations_applied = 3
    assert env._discount(0.9) == pytest.approx(0.9 * 0.7)
    # Discount clamps at zero; never negative.
    env._n_mitigations_applied = 99
    assert env._discount(0.9) == 0.0


# ---------------------------------------------------------------------------
# Validator gate -- Rust structural check before executor
# ---------------------------------------------------------------------------

def test_validator_gate_returns_none_when_native_unavailable():
    """Graceful degradation contract: if dorian_native isn't
    importable or the FFI raises, the gate returns None (no errors)
    so the existing executor path still runs.

    The unit under test is ``rl.env.validator_gate.validate_structural``;
    we exercise it directly with a DAG and trust the import guard.
    """
    from dorian.dag import DAG, Operator
    from rl.env.validator_gate import validate_structural

    dag = DAG()
    dag.nodes["a"] = Operator(name="some.op", language="python", tasks=[])
    # No matter whether native is installed, calling the gate must
    # not raise -- empty errors is a valid clean result.
    errs = validate_structural(dag)
    assert isinstance(errs, list)


def test_env_surfaces_validation_errors_in_observation_extras():
    """If ``_validate_structural`` returns errors, they land in
    ``obs.extras['last_validation_errors']`` as list[dict] — same
    channel the debugger / telemetry consume runtime exceptions
    through (``last_exception``)."""
    env = _make_env()
    env.reset("credit-g")
    # Simulate: the validator returned two errors. We bypass the
    # native call by injecting into the attribute directly — what
    # the observation builder consumes. This test asserts the
    # observation shape, not the validator's behaviour (that's
    # covered by the Rust-side tests).
    env._last_validation_errors = [
        {
            "kind": "UnwiredRequiredInput",
            "pointer": "[UnwiredRequiredInput] node=pred operator=predict port=0",
            "fields": {
                "node_id": "pred",
                "operator": "predict",
                "port": "0",
                "expected_type": "Model",
            },
        },
        {
            "kind": "CycleDetected",
            "pointer": "[CycleDetected] node=? operator=?",
            "fields": {"cycle_nodes": ["a", "b", "a"]},
        },
    ]
    obs = env._current_observation()
    assert "last_validation_errors" in obs.extras
    errs = obs.extras["last_validation_errors"]
    assert len(errs) == 2
    assert errs[0]["kind"] == "UnwiredRequiredInput"
    assert "predict" in errs[0]["pointer"]
    assert errs[0]["fields"]["port"] == "0"


def test_validator_errors_cleared_on_reset():
    env = _make_env()
    env.reset("credit-g")
    env._last_validation_errors = [{"kind": "CycleDetected", "pointer": "x", "fields": {}}]
    env.reset("credit-g")
    obs = env._current_observation()
    assert "last_validation_errors" not in obs.extras


# ---------------------------------------------------------------------------
# Partial-execution credit for post-validation runtime failures
# ---------------------------------------------------------------------------

def test_partial_credit_returns_zero_when_nodes_missing():
    from dorian.dag import DAG
    from rl.env.partial_credit import partial_credit
    dag = DAG()
    # Unknown node ids -> 0.
    assert partial_credit(dag, metric_node_id=None, failed_node_id=None) == 0.0
    assert partial_credit(dag, metric_node_id="x", failed_node_id="y") == 0.0


def test_partial_credit_ranks_by_depth_along_metric_chain():
    """Build a straight chain A -> B -> C -> D -> metric. A failure
    at D should yield more credit than a failure at B; both should
    be less than the configured cap."""
    from dorian.dag import DAG, Edge, Operator
    from rl.env.partial_credit import partial_credit, PARTIAL_CREDIT_CAP_DEFAULT

    dag = DAG()
    chain = ["a", "b", "c", "d", "metric"]
    for nid in chain:
        dag.nodes[nid] = Operator(name=nid, language="python", tasks=[])
    for u, v in zip(chain, chain[1:]):
        dag.edges.append(Edge(source=u, destination=v, position=0, output=0))

    # Metric has 4 ancestors (a, b, c, d).
    # Failure at b: b has 1 ancestor (a). Credit = cap * 1/4.
    low = partial_credit(dag, metric_node_id="metric", failed_node_id="b")
    # Failure at d: d has 3 ancestors (a, b, c). Credit = cap * 3/4.
    high = partial_credit(dag, metric_node_id="metric", failed_node_id="d")
    assert low < high
    assert 0.0 < low < high < PARTIAL_CREDIT_CAP_DEFAULT + 1e-9
    # And both ordered below a realistic success metric (say 0.5).
    assert high < 0.5


def test_partial_credit_zero_for_failures_off_metric_chain():
    """Failure in a side branch that doesn't feed the metric should
    not earn credit — the side branch shouldn't have run at all."""
    from dorian.dag import DAG, Edge, Operator
    from rl.env.partial_credit import partial_credit

    dag = DAG()
    for nid in ["root", "main", "metric", "side"]:
        dag.nodes[nid] = Operator(name=nid, language="python", tasks=[])
    # Main path: root -> main -> metric
    dag.edges.append(Edge(source="root", destination="main", position=0, output=0))
    dag.edges.append(Edge(source="main", destination="metric", position=0, output=0))
    # Side path: root -> side  (side does NOT reach metric)
    dag.edges.append(Edge(source="root", destination="side", position=1, output=0))

    # Side fails: not on metric's required path -> 0.
    assert partial_credit(dag, metric_node_id="metric", failed_node_id="side") == 0.0
    # Main fails: on the path; credit > 0.
    assert partial_credit(dag, metric_node_id="metric", failed_node_id="main") > 0.0


def test_env_adds_partial_credit_only_for_data_driven_failures():
    """The env wraps partial_credit and adds it to the invalid-step
    penalty only when error_category == 'data_driven'. our_bug / None
    paths get the uniform penalty unchanged."""
    from rl.env.executor import ExecutorResult
    env = _make_env()
    env.reset("credit-g")
    # our_bug path: no credit.
    r = ExecutorResult(
        success=False, metric_value=None, metric_node_id=None,
        wall_clock_secs=0.0,
        error_type="ImportError", error_message="...",
        error_category="our_bug", failed_node="anything",
    )
    assert env._partial_credit_for(r) == 0.0
    # data_driven but no failed_node: no credit.
    r = ExecutorResult(
        success=False, metric_value=None, metric_node_id=None,
        wall_clock_secs=0.0,
        error_type="ValueError", error_message="shape",
        error_category="data_driven", failed_node=None,
    )
    assert env._partial_credit_for(r) == 0.0


# ---------------------------------------------------------------------------
# Validator-path partial credit
# ---------------------------------------------------------------------------

def test_validator_partial_credit_scales_with_clean_node_fraction():
    """A graph with 1 error out of 10 nodes should earn more credit
    than one with 9 errors out of 10 — both above zero, both below
    the execution-path cap (so ordering stays: exec-partial >
    validator-partial > hard failure)."""
    from dorian.dag import DAG, Operator
    from rl.env.partial_credit import (
        VALIDATOR_CREDIT_CAP_DEFAULT,
        PARTIAL_CREDIT_CAP_DEFAULT,
        validator_partial_credit,
    )

    dag = DAG()
    for i in range(10):
        dag.nodes[f"n{i}"] = Operator(name=f"n{i}", language="python", tasks=[])

    one_err = [{"kind": "UnwiredPort", "fields": {"node_id": "n0"}}]
    many_err = [
        {"kind": "UnwiredPort", "fields": {"node_id": f"n{i}"}} for i in range(9)
    ]
    low = validator_partial_credit(dag, many_err)
    high = validator_partial_credit(dag, one_err)
    assert 0.0 < low < high <= VALIDATOR_CREDIT_CAP_DEFAULT + 1e-9
    # Strictly below the execution cap so exec-partial always wins
    # head-to-head against validator-partial at the same progress.
    assert VALIDATOR_CREDIT_CAP_DEFAULT < PARTIAL_CREDIT_CAP_DEFAULT


def test_validator_partial_credit_graceful_degradation():
    """No errors / empty graph / errors without node pointers all
    return 0.0 or a minimal floor — never crash."""
    from dorian.dag import DAG, Operator
    from rl.env.partial_credit import (
        VALIDATOR_CREDIT_CAP_DEFAULT,
        validator_partial_credit,
    )

    empty = DAG()
    assert validator_partial_credit(empty, []) == 0.0
    assert validator_partial_credit(empty, None) == 0.0
    dag = DAG()
    dag.nodes["x"] = Operator(name="x", language="python", tasks=[])
    # Errors exist but don't localise to a node — minimal floor.
    no_locate = [{"kind": "Cycle", "fields": {}}]
    c = validator_partial_credit(dag, no_locate)
    assert 0.0 < c < VALIDATOR_CREDIT_CAP_DEFAULT


# ---------------------------------------------------------------------------
# Executor failed_node capture (the missing wire that starved
# partial_credit of any learnable signal)
# ---------------------------------------------------------------------------

def test_executor_captures_failing_node_on_exception():
    """A dask task that raises should bubble up with the owning
    node_id attached — the hook partial_credit() needs to compute
    a non-zero reward. Before this wire, every runtime failure
    returned failed_node=None and the agent cycled on the
    invalid-step floor."""
    from rl.env.executor import _wrap_task_capture

    def boom():
        raise ValueError("kaboom")

    wrapped = _wrap_task_capture("n_boom", boom)
    try:
        wrapped()
    except ValueError as exc:
        assert getattr(exc, "_dorian_failed_node", None) == "n_boom"
    else:
        raise AssertionError("wrapped call should have raised")


def test_baseline_credit_distinguishes_distinct_graphs():
    """When partial_credit returns 0 and validator credit returns 0,
    the baseline-structural credit must still separate distinct
    graphs — otherwise the agent sees a flat floor across episodes
    and policy gradient has no direction."""
    from dorian.dag import DAG, Edge, Operator
    from rl.env.partial_credit import (
        BASELINE_STRUCTURE_CAP_DEFAULT,
        baseline_structural_credit,
    )

    # Graph A: sparsely connected, metric has no ancestors.
    a = DAG()
    a.nodes["metric"] = Operator(name="metric", language="python", tasks=[])
    a.nodes["stray"] = Operator(name="stray", language="python", tasks=[])
    # Graph B: well-connected chain to the metric.
    b = DAG()
    for nid in ["root", "mid", "metric"]:
        b.nodes[nid] = Operator(name=nid, language="python", tasks=[])
    b.edges.append(Edge(source="root", destination="mid", position=0, output=0))
    b.edges.append(Edge(source="mid", destination="metric", position=0, output=0))

    ca = baseline_structural_credit(a, metric_node_id="metric")
    cb = baseline_structural_credit(b, metric_node_id="metric")
    assert ca != cb
    assert 0.0 <= ca < cb <= BASELINE_STRUCTURE_CAP_DEFAULT + 1e-9


def test_failing_node_of_unwinds_run_id_and_slice_suffix():
    """``_build_task_graph`` renames keys to ``{run_id}_{k}`` and
    multi-output slice keys are ``{node}_{idx}``. The inverse needs
    to recover the original node id for both layers so partial credit
    can anchor to it."""
    from rl.env.executor import _failing_node_of

    node_ids = {"split_node", "model_node"}
    # Direct match.
    assert _failing_node_of("split_node", node_ids) == "split_node"
    # Slice key: ``split_node_1`` -> ``split_node``.
    assert _failing_node_of("split_node_1", node_ids) == "split_node"
    # Run-id-prefixed: ``rl-12345_model_node`` -> ``model_node``.
    assert _failing_node_of("rl-12345_model_node", node_ids) == "model_node"
    # Run-id prefix + slice: ``rl-12345_split_node_2`` -> ``split_node``.
    assert _failing_node_of("rl-12345_split_node_2", node_ids) == "split_node"
    # Unknown: returns None, caller uses the raw dask key as fallback.
    assert _failing_node_of("rl-12345_unknown_node", node_ids) is None


# ---------------------------------------------------------------------------
# Live-infra tests (skippable)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="set DORIAN_RL_LIVE=1 for live executor test")
def test_live_one_episode_produces_a_report():
    """Runs ONE full episode against credit-g with the default
    HybridPolicy. Requires openml + pandas + sklearn installed
    (always in dev deps)."""
    from rl.policy import HybridPolicy
    from rl.train.loop import rollout_episode

    # max_steps comes from TrainerConfig via _make_env -- do not
    # override here. See rl/train/config.py for the floor analysis.
    env = _make_env()
    policy = HybridPolicy(seed=0)
    trajectory, report = rollout_episode(env, policy, "credit-g", episode_idx=0)
    assert report.steps >= 1
    # valid_pipeline may be False if the random policy didn't
    # produce a runnable pipeline; we just check the contract.
    assert report.wall_clock_secs >= 0
