"""Frozen evaluation template for RL pipeline generation.

The RL agent does NOT build pipelines from scratch.  It starts with a frozen
template that handles data loading, train/test splitting, and evaluation.
The agent only places operators in the "RL zone" between the split outputs
and the evaluation metric.

Template structure::

    dorian.io.dataset  (frozen, single output: full DataFrame)
        │
        ├──► project_columns (Snippet instance 1: df[features])
        │       ↑ columns
        │    dorian.io.state[dataset.features]
        │
        └──► project_columns (Snippet instance 2: df[target])
                ↑ columns
             dorian.io.state[dataset.target]
        │                    │
        ▼ pos 0              ▼ pos 1
    train_test_split   (frozen)
        |    |    |    |
      X_tr X_te y_tr y_te
        |              |         <- RL zone: agent places transformers/estimators here
        |              |
    [RL-generated pipeline section]
        |              |
    accuracy_score     (frozen)

The ``build_eval_template`` function returns:
- A DAG with the frozen nodes and edges pre-wired
- Node IDs for the RL zone entry/exit points (free ports)
- A frozenset of frozen node IDs that the environment must not modify

The metric is resolved from the KB based on the dataset's task type.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet  # noqa: F401 – Parameter used for state nodes


# ---------------------------------------------------------------------------
# Projection snippet code
# ---------------------------------------------------------------------------

_PROJECT_COLUMNS_SNIPPET = '''\
def foo(df, columns=None):
    """Project a DataFrame to a subset of columns.

    Two instances live in the template: one for features (returns a
    2D DataFrame), one for target (returns a 1D Series — sklearn
    classifiers / metrics expect ``y`` to be 1-dimensional, so a
    single target column collapses to a Series even though state
    returns a list ``["target_col"]``).

    ``columns`` is resolved at expansion time from the session's
    dataset profile via ``dorian.io.state[dataset.features]`` or
    ``dorian.io.state[dataset.target]``.
    """
    if columns is None:
        return df
    if isinstance(columns, str):
        return df[columns]
    cols = list(columns)
    if len(cols) == 1:
        return df[cols[0]]
    return df[cols]
'''


# ---------------------------------------------------------------------------
# Metric resolution
# ---------------------------------------------------------------------------
#
# Metric FQNs come from the KB (``get_metrics_for_task``) so adding a
# new metric to a task — F1, ROC-AUC, MAE, RMSE — is a KB edit, not a
# code edit. The legacy ``_FALLBACK_METRIC`` is the floor when the KB
# has no entries for the task or when a process boots without a KB
# snapshot loaded (test paths).

_FALLBACK_METRIC = "sklearn.metrics.accuracy_score"

# Multi-metric defaults used when the KB snapshot isn't loadable
# (RL trainer / FLAML seeder containers may run without the
# snapshot volume mounted). Mirrors what
# ``get_metrics_for_task`` returns once the snapshot is present.
_FALLBACK_TASK_METRICS: dict[str, list[str]] = {
    "Classification": [
        "sklearn.metrics.accuracy_score",
        "sklearn.metrics.f1_score",
        "sklearn.metrics.precision_score",
        "sklearn.metrics.recall_score",
    ],
    "Binary Classification": [
        "sklearn.metrics.accuracy_score",
        "sklearn.metrics.f1_score",
        "sklearn.metrics.precision_score",
        "sklearn.metrics.recall_score",
        "sklearn.metrics.roc_auc_score",
    ],
    "Multiclass Classification": [
        "sklearn.metrics.accuracy_score",
        "sklearn.metrics.f1_score",
    ],
    "Regression": [
        "sklearn.metrics.r2_score",
        "sklearn.metrics.mean_absolute_error",
        "sklearn.metrics.mean_squared_error",
    ],
}

# Per-metric (input_specs, output_specs) used when wiring fanout from
# y_test / y_pred to each metric node. Most sklearn metrics share the
# (y_true@0, y_pred@1) signature; the few exceptions (silhouette
# wants X@0 + labels@1) live here. Unknown metrics fall through to the
# (y_true, y_pred) pair, which is correct for every classification /
# regression metric in the catalogue.
_METRIC_IO: dict[str, tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]] = {
    "sklearn.metrics.silhouette_score": (
        [("X", 0, "features"), ("labels", 1, "labels")],
        [("score", 0, "score")],
    ),
}
_DEFAULT_METRIC_IO: tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]] = (
    [("y_true", 0, "labels"), ("y_pred", 1, "predictions")],
    [("score", 0, "score")],
)

# Default kwargs per metric — sklearn's f1 / precision / recall default
# to ``average="binary"`` which fails on multiclass targets. ``weighted``
# averaging works for both binary AND multiclass without forcing the
# user to know upfront. Mirrors ``dorian/evaluation/dag_builder.py:
# TASK_METRIC_KWARGS`` so the in-DAG metric runs match the post-exec
# eval procedure's behaviour.
_METRIC_DEFAULT_KWARGS: dict[str, dict[str, str]] = {
    "sklearn.metrics.f1_score": {"average": "weighted"},
    "sklearn.metrics.precision_score": {"average": "weighted"},
    "sklearn.metrics.recall_score": {"average": "weighted"},
}


def _resolve_metrics_for_task(task: str | None) -> list[str]:
    """Return the metric FQN list for a task — KB first, fallback last.

    Multi-metric is the canonical shape: a Classification template
    spawns one metric node per FQN returned here (accuracy + F1 +
    ROC-AUC, all parallel) so a single pipeline run produces every
    score at once. The user's evaluation-procedure selection can
    override this list explicitly via ``ResolvedProcedure.config["metrics"]``;
    unset → KB default → ``_FALLBACK_METRIC``.

    The KB's ``metrics_by_task`` bucket holds both data-quality
    metrics (``LabelCompleteness`` — bare name, used by the
    debugger's pathway thresholds) and model-evaluation metrics
    (``sklearn.metrics.accuracy_score`` — dotted FQN, what the
    evaluation harness needs). Filter to the dotted shape: the
    eval harness only consumes callable sklearn metrics, never DQ
    score thresholds.
    """
    if not task:
        return [_FALLBACK_METRIC]
    try:
        from dorian.knowledge.queries import get_metrics_for_task
        kb_metrics = list(get_metrics_for_task(task))
    except Exception:
        kb_metrics = []
    eval_metrics = [m for m in kb_metrics if "." in m]
    if eval_metrics:
        return eval_metrics
    # Fallback: the snapshot isn't loadable in this process (seeder /
    # trainer containers may not mount kb_snapshot). Use the
    # task-default mapping so the template still produces multi-metric.
    return _FALLBACK_TASK_METRICS.get(task, [_FALLBACK_METRIC])


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

class EvalTemplate:
    """Immutable evaluation template with RL zone entry/exit points.

    Attributes
    ----------
    dag : DAG
        The pre-built DAG with frozen nodes.
    frozen_nodes : frozenset[str]
        Node IDs that the RL agent must not modify.
    frozen_edges : frozenset[tuple[str, str]]
        Edge (source, dest) pairs that the RL agent must not modify.
    rl_entry_ports : list[tuple[str, PortInfo]]
        Free output ports from train_test_split that feed into the RL zone.
        Each entry is ``(node_id, (port_name, port_position, dtype))``.
    rl_exit_target : dict
        Info about the metric nodes that the RL zone must feed into.
        ``{"node_ids": [str], "input_port": {...}}`` — every metric's
        ``y_pred`` slot is wired from the same source. ``node_id`` is
        kept as a single-string alias for backwards compatibility (set
        to the first metric's id) but new callers should fan out to
        ``node_ids``.
    task : str | None
        The data-science task.
    procedure : ResolvedProcedure | None
        The resolved evaluation procedure that drove template construction.
    metric_fqn : str
        Backwards-compat alias for the first metric in the template.
    metric_fqns : tuple[str, ...]
        All metric FQNs the template emits in parallel.
    """

    def __init__(
        self,
        dag: DAG,
        frozen_nodes: frozenset[str],
        frozen_edges: frozenset[tuple[str, str]],
        rl_entry_ports: list[tuple[str, tuple[str, int, str]]],
        rl_exit_target: dict[str, Any],
        task: str | None,
        metric_fqns: tuple[str, ...],
        procedure: Any = None,
    ):
        self.dag = dag
        self.frozen_nodes = frozen_nodes
        self.frozen_edges = frozen_edges
        self.rl_entry_ports = rl_entry_ports
        self.rl_exit_target = rl_exit_target
        self.task = task
        self.procedure = procedure
        self.metric_fqns = tuple(metric_fqns)
        # Backwards-compat: callers reading ``.metric_fqn`` (singular)
        # get the first metric. New callers should iterate metric_fqns.
        self.metric_fqn = metric_fqns[0] if metric_fqns else _FALLBACK_METRIC


def build_eval_template(
    procedure: Any = None,
    *,
    task: str | None = None,
    test_size: float = 0.2,
    random_state: int = 42,
) -> EvalTemplate:
    """Build a frozen evaluation template DAG for any pipeline body.

    Parameters
    ----------
    procedure : ResolvedProcedure | None
        Resolved evaluation procedure (from session meta via
        ``dorian.evaluation.resolver.resolve_eval_procedure``). Carries
        the procedure type (holdout / kfold / custom), task, metrics,
        and procedure-specific config (k, custom code, …).
    task : str or None
        Backwards-compat parameter — when ``procedure`` is None, build
        a default holdout template for this task with KB-resolved
        metrics.
    test_size, random_state
        Holdout split parameters. Overridden by ``procedure.config``
        when present.

    Returns
    -------
    EvalTemplate
        DAG with frozen nodes, RL zone entry / exit, and all metric
        nodes. The pipeline body wires ``y_pred`` once and the
        template fans out to every metric node so a single run
        emits every score in parallel.
    """
    # Resolve effective procedure / task / metrics.
    proc_type = "holdout"
    proc_task = task
    proc_metrics: list[str] | None = None
    proc_config: dict = {}
    if procedure is not None:
        proc_type = getattr(procedure, "type", proc_type) or "holdout"
        proc_config = getattr(procedure, "config", None) or {}
        proc_task = proc_config.get("task") or proc_task or getattr(procedure, "task", None)
        cfg_metrics = proc_config.get("metrics")
        if isinstance(cfg_metrics, (list, tuple)) and cfg_metrics:
            proc_metrics = list(cfg_metrics)
        cfg_test_size = proc_config.get("test_size")
        if isinstance(cfg_test_size, (int, float)):
            test_size = float(cfg_test_size)
        cfg_random_state = proc_config.get("random_state")
        if isinstance(cfg_random_state, int):
            random_state = cfg_random_state
    if proc_metrics is None:
        proc_metrics = _resolve_metrics_for_task(proc_task)

    if proc_type == "custom":
        # Custom procedure: replace metric nodes with a single
        # Snippet node that runs the user-supplied code on
        # (y_test, y_pred) and emits a dict of metric scores. The
        # dataset / projection / split backbone is identical to
        # holdout; only the scoring tail changes.
        return _build_custom_template(
            proc_task, proc_config,
            test_size=test_size, random_state=random_state,
            procedure=procedure,
        )
    if proc_type == "kfold":
        # K-fold splits + scores happen post-execution (via
        # ``dorian/evaluation/dag_builder.py:_build_kfold_dag``)
        # because the pipeline body's fit→predict cycle has to
        # repeat per fold and that's a graph-rebuild, not a
        # template tweak. The in-DAG template for kfold drops the
        # metric nodes entirely (single-split scoring is misleading
        # for a CV procedure) and lets the post-exec path own the
        # full evaluation. The pipeline body still terminates at a
        # predictions output that the post-exec evaluator picks up
        # via ``_evaluate_pipeline_sync``.
        return _build_kfold_template(
            proc_task,
            test_size=test_size, random_state=random_state,
            procedure=procedure,
        )
    # holdout (default + explicit)
    return _build_holdout_template(
        proc_task, proc_metrics,
        test_size=test_size, random_state=random_state,
        procedure=procedure,
    )


def _build_holdout_template(
    task: str | None,
    metric_fqns: list[str],
    *,
    test_size: float,
    random_state: int,
    procedure: Any,
) -> EvalTemplate:
    """Holdout-style template: dataset → projections → split → N metrics."""
    dag = DAG()
    frozen_nodes: set[str] = set()
    frozen_edge_pairs: set[tuple[str, str]] = set()

    # ── 1. Dataset loader (dorian.io.dataset) ──────────────────────────
    # Single output: the full DataFrame.  Column slicing is done by
    # explicit projection snippets below — never by multi-output hack.
    dataset_id = _nid()
    dag.nodes[dataset_id] = Operator(
        name="dorian.io.dataset",
        language="python",
    )
    frozen_nodes.add(dataset_id)

    # ── 2. Feature / target projection ────────────────────────────────
    # Two projection snippets slice the DataFrame into X (features) and
    # y (target) so train_test_split receives them as separate inputs.
    # Column names come from dorian.io.state which resolves at expansion
    # time from the session's dataset profile.

    # -- project_features: df[columns] → X ---
    proj_features_id = _nid()
    dag.nodes[proj_features_id] = Snippet(
        name="project_columns",
        code=_PROJECT_COLUMNS_SNIPPET,
        language="python",
    )
    frozen_nodes.add(proj_features_id)

    # Compact state parameter — expands to resolved column list at expansion time
    state_features_id = _nid()
    dag.nodes[state_features_id] = Parameter(
        name="dorian.io.state", dtype="state", value="dataset.features",
    )
    frozen_nodes.add(state_features_id)

    # dataset → project_features (pos 0: the DataFrame)
    dag.edges.append(Edge(
        source=dataset_id, destination=proj_features_id,
        position=0, output=0,
    ))
    frozen_edge_pairs.add((dataset_id, proj_features_id))
    # state(features) → project_features (kwarg "columns")
    dag.edges.append(Edge(
        source=state_features_id, destination=proj_features_id,
        position="columns", output=0,
    ))
    frozen_edge_pairs.add((state_features_id, proj_features_id))

    # -- project_target: df[columns] → y ---
    proj_target_id = _nid()
    dag.nodes[proj_target_id] = Snippet(
        name="project_columns",
        code=_PROJECT_COLUMNS_SNIPPET,
        language="python",
    )
    frozen_nodes.add(proj_target_id)

    # Compact state parameter — expands to resolved column list at expansion time
    state_target_id = _nid()
    dag.nodes[state_target_id] = Parameter(
        name="dorian.io.state", dtype="state", value="dataset.target",
    )
    frozen_nodes.add(state_target_id)

    # dataset → project_target (pos 0: the DataFrame)
    dag.edges.append(Edge(
        source=dataset_id, destination=proj_target_id,
        position=0, output=0,
    ))
    frozen_edge_pairs.add((dataset_id, proj_target_id))
    # state(target) → project_target (kwarg "columns")
    dag.edges.append(Edge(
        source=state_target_id, destination=proj_target_id,
        position="columns", output=0,
    ))
    frozen_edge_pairs.add((state_target_id, proj_target_id))

    # ── 3. train_test_split ────────────────────────────────────────────
    split_id = _nid()
    dag.nodes[split_id] = Operator(
        name="sklearn.model_selection.train_test_split",
        language="python",
    )
    frozen_nodes.add(split_id)

    # Split parameters
    ts_param_id = _nid()
    dag.nodes[ts_param_id] = Parameter(
        name="test_size", dtype="float", value=str(test_size),
    )
    frozen_nodes.add(ts_param_id)
    dag.edges.append(Edge(
        source=ts_param_id, destination=split_id,
        position="test_size", output=0,
    ))
    frozen_edge_pairs.add((ts_param_id, split_id))

    rs_param_id = _nid()
    dag.nodes[rs_param_id] = Parameter(
        name="random_state", dtype="int", value=str(random_state),
    )
    frozen_nodes.add(rs_param_id)
    dag.edges.append(Edge(
        source=rs_param_id, destination=split_id,
        position="random_state", output=0,
    ))
    frozen_edge_pairs.add((rs_param_id, split_id))

    # Projection outputs → split: X (features) at pos 0, y (target) at pos 1
    dag.edges.append(Edge(
        source=proj_features_id, destination=split_id,
        position=0, output=0,
    ))
    frozen_edge_pairs.add((proj_features_id, split_id))
    dag.edges.append(Edge(
        source=proj_target_id, destination=split_id,
        position=1, output=0,
    ))
    frozen_edge_pairs.add((proj_target_id, split_id))

    # ── 4. Metric nodes (N parallel) ──────────────────────────────────
    # One Operator per metric FQN. Each reads (y_test, y_pred) and emits
    # a scalar score. The pipeline body wires ``y_pred`` once via
    # ``rl_exit_target`` and the template's exit-fanout pass connects
    # that single source to every metric's predictions slot.
    metric_ids: list[str] = []
    for fqn in metric_fqns:
        metric_id = _nid()
        dag.nodes[metric_id] = Operator(name=fqn, language="python")
        frozen_nodes.add(metric_id)
        # Look up the metric's I/O signature; default is the standard
        # (y_true@0, y_pred@1) pair used by every classification /
        # regression metric in the catalogue.
        in_specs, _ = _METRIC_IO.get(fqn, _DEFAULT_METRIC_IO)
        # Wire y_test → metric (the ``labels``-typed input slot — usually
        # y_true@0). silhouette_score routes y_test to ``labels@1`` and
        # X_test to features@0; that wiring isn't a labels-only metric
        # so we skip it here (silhouette templates need a different
        # backbone — clustering doesn't have a y_pred to fan out anyway).
        for port_name, port_pos, port_dtype in in_specs:
            if port_dtype == "labels":
                dag.edges.append(Edge(
                    source=split_id, destination=metric_id,
                    position=port_pos, output=3,  # y_test
                ))
                frozen_edge_pairs.add((split_id, metric_id))
                break
        # Default kwargs (e.g. ``average=weighted`` for f1/precision/
        # recall — needed for multiclass datasets, harmless on binary).
        for kw_name, kw_value in _METRIC_DEFAULT_KWARGS.get(fqn, {}).items():
            kw_id = _nid()
            dag.nodes[kw_id] = Parameter(
                name=kw_name, dtype="str", value=str(kw_value),
            )
            frozen_nodes.add(kw_id)
            dag.edges.append(Edge(
                source=kw_id, destination=metric_id,
                position=kw_name, output=0,
            ))
            frozen_edge_pairs.add((kw_id, metric_id))
        metric_ids.append(metric_id)

    # ── RL zone entry ports ───────────────────────────────────────────
    rl_entry_ports = [
        (split_id, ("X_train", 0, "features")),
        (split_id, ("X_test", 1, "features")),
        (split_id, ("y_train", 2, "labels")),
    ]

    # RL exit target: every metric's y_pred slot reads from the same
    # source the pipeline body produces. Callers fan out by iterating
    # ``node_ids``; ``node_id`` is the legacy single-target alias.
    primary_metric_id = metric_ids[0] if metric_ids else None
    primary_pred_port = _DEFAULT_METRIC_IO[0][1]  # ("y_pred", 1, "predictions")
    rl_exit_target = {
        "node_id": primary_metric_id,
        "node_ids": list(metric_ids),
        "input_port": {
            "name": primary_pred_port[0],
            "position": primary_pred_port[1],
            "dtype": primary_pred_port[2],
        },
    }

    return EvalTemplate(
        dag=dag,
        frozen_nodes=frozenset(frozen_nodes),
        frozen_edges=frozenset(frozen_edge_pairs),
        rl_entry_ports=rl_entry_ports,
        rl_exit_target=rl_exit_target,
        task=task,
        metric_fqns=tuple(metric_fqns),
        procedure=procedure,
    )


def _build_custom_template(
    task: str | None,
    proc_config: dict,
    *,
    test_size: float,
    random_state: int,
    procedure: Any,
) -> EvalTemplate:
    """Custom evaluation procedure: user code wraps (y_test, y_pred).

    Same backbone as the holdout template (dataset → projections →
    split → RL zone) but the metric fanout becomes a single Snippet
    node that runs ``proc_config["code"]`` against ``(y_test,
    y_pred)`` and returns a dict ``{metric_name: score}``. The
    user's code keeps full control over what the metric set means —
    the template just guarantees it gets the held-out predictions
    paired with their true labels.

    The Snippet is treated by the executor as a single output port
    that resolves to a scalar (the first metric value, by
    convention) for backwards compat with single-metric callers;
    the full dict surfaces via ``ExecutorResult.node_outputs`` and
    the post-exec ``_evaluate_pipeline_sync`` path.
    """
    # Reuse the holdout backbone — same dataset / projection / split
    # frozen sub-graph — but pass an empty metric list so the holdout
    # builder doesn't spawn the standard metric nodes. We then attach
    # the custom Snippet ourselves.
    tpl = _build_holdout_template(
        task, [], test_size=test_size, random_state=random_state,
        procedure=procedure,
    )
    code = (proc_config or {}).get("code") or ""
    if not code:
        # No code supplied — degrade to holdout default. The user
        # selected "custom" without filling in the code box; the
        # post-exec path will short-circuit to ``_build_none_dag``.
        return _build_holdout_template(
            task, _resolve_metrics_for_task(task),
            test_size=test_size, random_state=random_state,
            procedure=procedure,
        )

    dag = tpl.dag
    splitter_id = next(nid for nid, _ in tpl.rl_entry_ports)
    custom_id = _nid()
    # Wrap the user's code in a callable Snippet. We trust ``code`` to
    # define ``foo(y_true, y_pred, X_test=None)`` and return either a
    # dict ``{metric: score}`` or a scalar (which we wrap into
    # ``{"score": value}``). The executor's ``_resolve_snippet`` rejects
    # missing ``foo`` so syntax errors surface immediately at run time.
    snippet_code = (
        "def foo(y_true, y_pred, X_test=None):\n"
        "    _ns = {}\n"
        "    exec(_USER_CODE_, _ns)\n"
        "    if 'foo' in _ns:\n"
        "        out = _ns['foo'](y_true, y_pred, X_test=X_test) if 'X_test' in _ns['foo'].__code__.co_varnames else _ns['foo'](y_true, y_pred)\n"
        "    elif 'evaluate' in _ns:\n"
        "        out = _ns['evaluate'](y_true, y_pred)\n"
        "    else:\n"
        "        out = None\n"
        "    if isinstance(out, dict):\n"
        "        return out\n"
        "    return {'score': out}\n"
    ).replace("_USER_CODE_", repr(code))
    new_nodes = dict(dag.nodes)
    new_edges = list(dag.edges)
    new_nodes[custom_id] = Snippet(
        name="custom_eval", code=snippet_code, language="python",
    )
    frozen_nodes = set(tpl.frozen_nodes) | {custom_id}
    frozen_edges = set(tpl.frozen_edges)
    # y_test (split out=3) → custom.y_true (pos 0)
    new_edges.append(Edge(
        source=splitter_id, destination=custom_id, position=0, output=3,
    ))
    frozen_edges.add((splitter_id, custom_id))
    # X_test (split out=1) → custom.X_test (pos 2) — kwarg is optional
    new_edges.append(Edge(
        source=splitter_id, destination=custom_id, position=2, output=1,
    ))
    frozen_edges.add((splitter_id, custom_id))
    # The pipeline body fans y_pred to custom.y_pred (pos 1) instead
    # of to N metric nodes — a SINGLE exit target.
    rl_exit_target = {
        "node_id": custom_id,
        "node_ids": [custom_id],
        "input_port": {"name": "y_pred", "position": 1, "dtype": "predictions"},
    }
    return EvalTemplate(
        dag=DAG(nodes=new_nodes, edges=new_edges),
        frozen_nodes=frozenset(frozen_nodes),
        frozen_edges=frozenset(frozen_edges),
        rl_entry_ports=tpl.rl_entry_ports,
        rl_exit_target=rl_exit_target,
        task=task,
        metric_fqns=("custom_eval",),
        procedure=procedure,
    )


def _build_kfold_template(
    task: str | None,
    *,
    test_size: float,
    random_state: int,
    procedure: Any,
) -> EvalTemplate:
    """K-fold CV template — degrades to holdout-shape in-DAG.

    The actual k-fold evaluation runs in the post-execution path
    (``dorian/evaluation/dag_builder.py:_build_kfold_dag``) because
    each fold needs its own train→fit→predict cycle, which is a
    pipeline-body rebuild rather than a template tweak. The
    in-DAG template still computes the standard multi-metric set
    on a single train/test split — useful as a smoke check that
    the body executes; the post-exec path produces the
    fold-averaged scores the user actually cares about. Both sets
    show up in the canvas evaluation panel.
    """
    return _build_holdout_template(
        task, _resolve_metrics_for_task(task),
        test_size=test_size, random_state=random_state,
        procedure=procedure,
    )


def _nid() -> str:
    """Generate a short unique node ID."""
    return uuid4().hex[:12]


def build_eval_template_for_session(
    session_meta: dict | None = None,
    *,
    task: str | None = None,
    test_size: float = 0.2,
    random_state: int = 42,
) -> EvalTemplate:
    """Build the evaluation template for a session — sidebar-driven.

    Reads ``selectedEvaluationProcedureName`` /
    ``selectedEvaluationProcedureId`` from *session_meta* and resolves
    them via :func:`dorian.evaluation.resolver.resolve_eval_procedure`,
    then hands the resolved procedure to :func:`build_eval_template`.

    The same path the canvas, RL agent, FLAML extractor, and
    cross-product runner all converge on — when the user picks
    "K-fold (k=5) with f1_macro + roc_auc" in the sidebar, every
    consumer downstream sees that procedure shape from the same
    template builder.

    Falls back to a holdout template with KB-default metrics for
    the supplied *task* when no procedure is selected (or when the
    session_meta is empty / missing).
    """
    from dorian.evaluation.resolver import resolve_eval_procedure
    meta = session_meta or {}
    if not isinstance(meta, dict):
        meta = {}
    procedure = resolve_eval_procedure(meta)
    # Carry the session's task into the procedure if not already set —
    # the resolver doesn't always populate ``config.task``.
    task_info = meta.get("selectedDataScienceTask") or {}
    session_task = (
        task_info.get("name") if isinstance(task_info, dict) else None
    ) or task
    if session_task and procedure.config.get("task") is None:
        # ``ResolvedProcedure`` is frozen — clone with the task injected.
        from dorian.evaluation.resolver import ResolvedProcedure
        procedure = ResolvedProcedure(
            name=procedure.name,
            type=procedure.type,
            config={**procedure.config, "task": session_task},
        )
    return build_eval_template(
        procedure=procedure,
        task=session_task,
        test_size=test_size,
        random_state=random_state,
    )


def fan_out_exit_target(dag: DAG, exit_target: dict, source_node: str, source_output: int = 1) -> None:
    """Wire the pipeline body's y_pred output to EVERY metric in the
    template's exit target, in place on *dag*.

    Replaces the legacy single-target rl_exit_target wiring. Callers
    that previously did ``Edge(body, exit_target['node_id'], pos=
    input_port.position, output=1)`` should switch to this helper so
    multi-metric templates fan out automatically.
    """
    port = exit_target.get("input_port") or {}
    pos = port.get("position", 1)
    metric_ids = exit_target.get("node_ids") or [exit_target.get("node_id")]
    for mid in metric_ids:
        if not mid:
            continue
        dag.edges.append(Edge(
            source=source_node, destination=mid,
            position=pos, output=source_output,
        ))
