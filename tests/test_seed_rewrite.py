"""Tests for the ``random_state`` auto-injection rewrite."""
from __future__ import annotations

from dorian.dag import DAG, Edge, Operator, Parameter
from dorian.pipeline.seed_rewrite import (
    DEFAULT_SEED_VALUE,
    SEED_PARAM_NAME,
    find_missing_seed_nodes,
    inject_default_seeds,
)


def _dag_with_split_and_rf(*, seed_wired_to: set[str] | None = None) -> DAG:
    """Build a minimal DAG with a train_test_split + RandomForest
    classifier; optionally pre-wire random_state to some subset."""
    seed_wired_to = seed_wired_to or set()
    nodes: dict = {
        "fname": Parameter(name="fpath", dtype="str", value="a.csv"),
        "load": Operator(name="pandas.read_csv", language="python", tasks=[]),
        "split": Operator(
            name="sklearn.model_selection.train_test_split",
            language="python",
            tasks=[],
        ),
        "clf": Operator(
            name="sklearn.ensemble.RandomForestClassifier",
            language="python",
            tasks=[],
        ),
    }
    edges = [
        Edge(source="fname", destination="load", position=0, output=0),
        Edge(source="load", destination="split", position=0, output=0),
        Edge(source="split", destination="clf", position=0, output=0),
    ]
    for target in seed_wired_to:
        pid = f"rs_{target}"
        nodes[pid] = Parameter(name="random_state", dtype="int", value="7")
        edges.append(
            Edge(
                source=pid,
                destination=target,
                position="random_state",
                output=0,
            )
        )
    return DAG(nodes=nodes, edges=edges)


def test_find_missing_flags_split_and_clf():
    dag = _dag_with_split_and_rf()
    missing = find_missing_seed_nodes(dag)
    assert missing == ["clf", "split"]


def test_find_missing_ignores_already_seeded_nodes():
    dag = _dag_with_split_and_rf(seed_wired_to={"split"})
    missing = find_missing_seed_nodes(dag)
    assert missing == ["clf"]

    dag_full = _dag_with_split_and_rf(seed_wired_to={"split", "clf"})
    assert find_missing_seed_nodes(dag_full) == []


def test_inject_default_seeds_adds_parameter_and_edge():
    dag = _dag_with_split_and_rf()
    seeded = inject_default_seeds(dag)
    assert sorted(seeded) == ["clf", "split"]
    # Two new Parameter nodes, each wired via position="random_state".
    new_params = [
        (nid, node)
        for nid, node in dag.nodes.items()
        if isinstance(node, Parameter) and node.name == SEED_PARAM_NAME
    ]
    assert len(new_params) == 2
    assert all(p.value == str(DEFAULT_SEED_VALUE) for _, p in new_params)

    seed_edges = [
        e for e in dag.edges if e.position == SEED_PARAM_NAME
    ]
    assert len(seed_edges) == 2
    assert {e.destination for e in seed_edges} == {"clf", "split"}


def test_inject_default_seeds_is_idempotent():
    dag = _dag_with_split_and_rf()
    inject_default_seeds(dag)
    # Second call finds no missing targets and injects nothing.
    seeded_again = inject_default_seeds(dag)
    assert seeded_again == []


def test_inject_default_seeds_custom_value():
    dag = _dag_with_split_and_rf()
    inject_default_seeds(dag, seed_value=137)
    params = [
        node
        for node in dag.nodes.values()
        if isinstance(node, Parameter) and node.name == SEED_PARAM_NAME
    ]
    assert all(p.value == "137" for p in params)


def test_inject_default_seeds_explicit_targets():
    dag = _dag_with_split_and_rf()
    seeded = inject_default_seeds(dag, targets=["split"])
    assert seeded == ["split"]
    # `clf` remains unseeded because it wasn't in the target list.
    assert "clf" in find_missing_seed_nodes(dag)


def test_find_missing_skips_operators_outside_known_list():
    """Operators not in the random_state-needing set are ignored.
    StandardScaler doesn't use random_state; it must never be seeded."""
    nodes = {
        "scaler": Operator(
            name="sklearn.preprocessing.StandardScaler",
            language="python",
            tasks=[],
        ),
    }
    dag = DAG(nodes=nodes, edges=[])
    assert find_missing_seed_nodes(dag) == []
