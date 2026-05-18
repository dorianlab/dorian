"""Mitigation rewrite that auto-injects ``random_state`` Parameter
nodes into operators whose seed handle is declared-but-unwired.

Why this exists: ``engine/cache`` (commit 0d7914f) forces cache
Bypass for any operator with ``random_state_param_name`` declared
but no Parameter node wired to that handle. An unseeded stochastic
op is effectively non-deterministic -- its results vary per run --
so two firings with matching non-seed params MUST NOT share a cache
entry.

The rewrite closes the gap by adding the missing seed Parameter
before the cache is consulted, making the firing genuinely
reproducible and therefore cache-eligible. Called during the same
expansion pipeline that handles dataset / state / compound
expansion (before the Dask / Rust scheduler sees the graph).

Default seed: 42. An RL ε-greedy exploration mode may override this
per episode to produce empirically-diverse structurally-identical
pipelines; that knob lives in the RL trainer, not here.

See internal design note section 6 for the full rationale.
"""
from __future__ import annotations

from typing import Iterable
from uuid import uuid4

from dorian.dag import DAG, Edge, Operator, Parameter

# Mirrors ``classify_random_state_param_builtin`` in
# engine/graph/src/dem.rs. Keep the two lists in lockstep -- the Rust
# side computes the detection set for the cache gate; this list
# drives the mitigation at expansion time.
#
# Future: source both from the KB so drift is impossible.
_SKLEARN_NEEDS_SEED: frozenset[str] = frozenset(
    (
        "sklearn.model_selection.train_test_split",
        "sklearn.model_selection.KFold",
        "sklearn.model_selection.StratifiedKFold",
        "sklearn.model_selection.ShuffleSplit",
        "sklearn.model_selection.StratifiedShuffleSplit",
        "sklearn.ensemble.RandomForestClassifier",
        "sklearn.ensemble.RandomForestRegressor",
        "sklearn.ensemble.ExtraTreesClassifier",
        "sklearn.ensemble.ExtraTreesRegressor",
        "sklearn.ensemble.GradientBoostingClassifier",
        "sklearn.ensemble.GradientBoostingRegressor",
        "sklearn.ensemble.AdaBoostClassifier",
        "sklearn.ensemble.AdaBoostRegressor",
        "sklearn.ensemble.RandomTreesEmbedding",
        "sklearn.tree.DecisionTreeClassifier",
        "sklearn.tree.DecisionTreeRegressor",
        "sklearn.tree.ExtraTreeClassifier",
        "sklearn.tree.ExtraTreeRegressor",
        "sklearn.linear_model.SGDClassifier",
        "sklearn.linear_model.SGDRegressor",
        "sklearn.linear_model.LogisticRegression",
        "sklearn.linear_model.Perceptron",
        "sklearn.linear_model.PassiveAggressiveClassifier",
        "sklearn.linear_model.PassiveAggressiveRegressor",
        "sklearn.neural_network.MLPClassifier",
        "sklearn.neural_network.MLPRegressor",
        "sklearn.cluster.KMeans",
        "sklearn.cluster.MiniBatchKMeans",
        "sklearn.cluster.SpectralClustering",
        "sklearn.decomposition.PCA",
        "sklearn.decomposition.TruncatedSVD",
        "sklearn.decomposition.FastICA",
        "sklearn.decomposition.KernelPCA",
        "sklearn.kernel_approximation.Nystroem",
        "sklearn.kernel_approximation.RBFSampler",
        "sklearn.manifold.TSNE",
        "sklearn.random_projection.GaussianRandomProjection",
        "sklearn.random_projection.SparseRandomProjection",
        "sklearn.mixture.GaussianMixture",
        "sklearn.mixture.BayesianGaussianMixture",
        "sklearn.svm.SVC",
        "sklearn.svm.NuSVC",
        "sklearn.svm.LinearSVC",
        "sklearn.svm.SVR",
        "sklearn.svm.NuSVR",
        "sklearn.svm.LinearSVR",
    )
)

SEED_PARAM_NAME: str = "random_state"
DEFAULT_SEED_VALUE: int = 42


def _operator_needs_seed(op: Operator) -> bool:
    return op.name in _SKLEARN_NEEDS_SEED


def _seed_handles_wired_to(dag: DAG, node_id: str) -> set[str]:
    """Return the keyword handles currently wired from Parameter nodes
    into ``node_id``. Used to detect whether ``random_state`` is
    already set so the rewrite is idempotent."""
    handles: set[str] = set()
    for edge in dag.edges:
        if edge.destination != node_id:
            continue
        src = dag.nodes.get(edge.source)
        if not isinstance(src, Parameter):
            continue
        # Keyword positions are str; positional are int.
        if isinstance(edge.position, str):
            handles.add(edge.position)
    return handles


def find_missing_seed_nodes(dag: DAG) -> list[str]:
    """Return node IDs where ``random_state`` is declared but unwired.

    Python-side mirror of ``cache::detect_missing_random_state``.
    Both paths must agree on which nodes need seeding; the Rust side
    drives the cache gate and this function drives the rewrite.
    """
    missing: list[str] = []
    for nid, node in dag.nodes.items():
        if not isinstance(node, Operator):
            continue
        if not _operator_needs_seed(node):
            continue
        if SEED_PARAM_NAME in _seed_handles_wired_to(dag, nid):
            continue
        missing.append(nid)
    return sorted(missing)


def inject_default_seeds(
    dag: DAG,
    *,
    seed_value: int = DEFAULT_SEED_VALUE,
    targets: Iterable[str] | None = None,
) -> list[str]:
    """Mutating rewrite: add a ``Parameter(random_state, int,
    <seed_value>)`` node + edge for every target that lacks a seed.

    Parameters
    ----------
    dag:
        DAG to mutate in place. Nodes and edges are appended; no
        existing nodes or edges are changed.
    seed_value:
        Integer seed to hard-code into the injected Parameter. RL
        ε-greedy exploration may pass a sampled value.
    targets:
        Optional explicit node-id list. Defaults to
        ``find_missing_seed_nodes(dag)`` so callers can simply invoke
        this at expansion time.

    Returns
    -------
    list[str]:
        The target node IDs that were actually seeded. Callers log
        this for observability -- silent seeding is a correctness
        trap.
    """
    if targets is None:
        target_ids = find_missing_seed_nodes(dag)
    else:
        target_ids = list(targets)
    seeded: list[str] = []
    for node_id in target_ids:
        if node_id not in dag.nodes:
            continue  # stale target; ignore
        param_node_id = f"_seed_{node_id}_{uuid4().hex[:8]}"
        dag.nodes[param_node_id] = Parameter(
            name=SEED_PARAM_NAME,
            dtype="int",
            value=str(seed_value),
        )
        dag.edges.append(
            Edge(
                source=param_node_id,
                destination=node_id,
                position=SEED_PARAM_NAME,
                output=0,
            )
        )
        seeded.append(node_id)
    return seeded
