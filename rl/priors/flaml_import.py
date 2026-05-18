"""
rl/priors/flaml_import.py
-------------------------
Convert sklearn-Pipeline objects (e.g. from FLAML) into Dorian DAGs by
code-generating a Python script that instantiates the pipeline and
feeding it through Dorian's own source-to-DAG extractor.

Why via code + extractor (rather than direct DAG construction)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Dorian extractor (``dorian.code.parsing.parser.parse``) is the
authoritative path from a hand-written Python pipeline to a Dorian
DAG: tree-sitter → AST → rule-driven rewrites that already cover
method chains (``fit``, ``transform``, ``predict``), constructor
hyperparameters, tuple unpacking (``train_test_split``), dataset
IO, and the rest of the pandas/sklearn patterns Dorian cares about.
Parallel DAG construction would silently diverge from that rule set
and lose future improvements. Any FLAML pipeline that the extractor
can't handle is a gap in the rule library to fix there, not to
paper over here.

A deliberate side effect of going through the extractor: the
resulting DAGs use the same operator FQNs, same port conventions,
and same hyperparameter dtypes as extractions from user code, so
the RL trainer's BK-Tree similarity metric can compare them on the
same footing.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from dorian.dag import DAG

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def sklearn_pipeline_to_dag(pipeline: Any) -> DAG:
    """Convert a sklearn ``Pipeline`` (or bare estimator) into a Dorian DAG.

    Goes through the shared code extractor, so the conversion uses
    the same rules as any hand-written Python pipeline Dorian
    already handles. Returns the ``final_dag`` (post-rewrite) — the
    form the RL trainer's BK-Tree and the rest of the engine
    consume.
    """
    code = sklearn_pipeline_to_code(pipeline)
    return extract_dag_from_code(code)


def sklearn_pipeline_to_code(pipeline: Any) -> str:
    """Emit a Python source string that reconstructs *pipeline* and
    drives it through ``fit`` / ``predict`` against a placeholder CSV.

    Output layout the extractor recognises:

        import pandas as pd
        from <module> import <Class>
        ...

        df = pd.read_csv(fpath)
        X = df.iloc[:, :-1]
        y = df.iloc[:, -1]

        step0 = <Class0>(<kwargs0>)
        X0 = step0.fit_transform(X, y)
        step1 = <Class1>(<kwargs1>)
        X1 = step1.fit_transform(X0, y)
        ...
        clf = <ClassN>(<kwargsN>)
        clf.fit(Xn, y)
        y_pred = clf.predict(Xn)

    Pipelines whose final step is not an estimator (pure transformer
    chain) skip the ``fit``/``predict`` tail; the extractor handles
    that shape the same way it handles transformer-only user code.
    """
    steps = _extract_steps(pipeline)
    if not steps:
        raise ValueError("pipeline has no steps")

    imports: dict[str, set[str]] = {"pandas as pd": set()}
    lines: list[str] = [
        "df = pd.read_csv(fpath)",
        "X = df.iloc[:, :-1]",
        "y = df.iloc[:, -1]",
        "",
    ]

    final_idx = len(steps) - 1
    prev_var = "X"
    for i, (_step_name, step) in enumerate(steps):
        if isinstance(step, str):
            # ``"passthrough"`` / ``"drop"`` sentinels — skip but
            # keep the variable alias moving so downstream sees it.
            continue

        cls_name = type(step).__name__
        module = type(step).__module__
        _add_import(imports, module, cls_name)

        hyper = ", ".join(
            f"{k}={_render_value(v, imports)}"
            for k, v in _hyperparams_to_emit(step)
        )
        var = "clf" if i == final_idx else f"step{i}"
        out = f"X{i+1}"

        lines.append(f"{var} = {cls_name}({hyper})")
        if i == final_idx and _is_estimator(step):
            lines.append(f"fitted = {var}.fit({prev_var}, y)")
            lines.append(f"y_pred = fitted.predict({prev_var})")
            _add_import(imports, "sklearn.metrics", "accuracy_score")
            lines.append("score = accuracy_score(y, y_pred)")
        else:
            lines.append(f"{out} = {var}.fit_transform({prev_var}, y)")
            prev_var = out

    header = _format_imports(imports)
    return header + "\n".join(lines) + "\n"


def extract_dag_from_code(code: str) -> DAG:
    """Run the Dorian extractor on *code* and return the final DAG.

    ``dorian.code.parsing.parser.parse`` internally calls
    ``asyncio.run(transform(...))``. Callers that already have a
    running event loop (the seeder's ``_main_async``, any service
    that imports ``backend.envs`` and thus boots a Dask client
    loop) hit ``RuntimeError: asyncio.run() cannot be called from
    a running event loop``. Isolate the extractor on a dedicated
    worker thread with no inherited loop so this function stays
    callable from either context.

    Surfaces any extractor failure as ``RuntimeError`` so the
    seeder can record the offending pipeline in its manifest for
    subsequent rule-authoring.
    """
    try:
        from dorian.code.parsing.parser import parse
    except Exception as exc:
        raise RuntimeError(f"parser unavailable: {exc}") from exc

    import threading

    result: list[DAG] = []
    error: list[BaseException] = []

    def _worker() -> None:
        try:
            _, final = parse(code, "python")
        except BaseException as exc:  # noqa: BLE001 — propagated to caller
            error.append(exc)
            return
        result.append(final)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if error:
        raise RuntimeError(f"extractor failed: {error[0]}") from error[0]
    return _merge_into_eval_template(
        _collapse_method_shortcuts(_replace_read_csv_with_dataset(result[0]))
    )


def _merge_into_eval_template(dag):
    """Drop FLAML's dataset/projection/metric noise and graft the
    classifier (+ implicit encoders) into the canonical RL frozen
    evaluation template.

    The RL agent's frozen template (see
    ``dorian.pipeline.generation.eval_template.build_eval_template``)
    provides the dataset/projection/split/metric backbone with
    state-resolved column projections (``dorian.io.state[dataset.
    features]`` / ``dataset.target``). The same template is the
    canonical evaluation harness for cross-product runs and the
    user-facing canvas — using it from the FLAML extractor unifies
    the "how do we evaluate a model" question across all three
    consumers. When the UI sidebar grows multi-metric / k-fold /
    custom evaluation procedures, swapping the template propagates
    everywhere without touching any pipeline body.

    What FLAML contributes:
      * The classifier (with hyperparameters).
      * An ``OrdinalEncoder`` between ``split.X`` and the classifier
        (FLAML's ``TabularPredictor`` runs implicit categorical
        encoding before invoking the underlying estimator).
      * A label-encoder Snippet on the y path BEFORE the split, so
        y_train and y_test share one categorical mapping.

    Wiring into the RL zone:
        split.X_train (out=0) ─▶ OrdinalEncoder.X_train  (pos=0)
        split.X_test  (out=1) ─▶ OrdinalEncoder.X_test   (pos=1)
        OrdinalEncoder.X_train_t (out=0) ─▶ classifier.X_train (pos=0)
        OrdinalEncoder.X_test_t  (out=1) ─▶ classifier.X_test  (pos=2)
        split.y_train (out=2) ─▶ classifier.y_train (pos=1)
        classifier (out=1) ─▶ metric.y_pred (pos=1)
        split.y_test  (out=3) ─▶ metric.y_true (pos=0, frozen)

    Decoupling the pipeline body from the evaluation procedure: the
    classifier subgraph copied here knows nothing about the metric,
    the split fraction, or which task it serves — those are all
    properties of the eval template. A future refactor that wires
    ``build_eval_template`` to a session's ``EvaluationProcedure``
    selection will swap holdout for k-fold (or custom) and add
    multi-metric scoring without changing this function.
    """
    from dorian.dag import DAG, Operator, Parameter, Snippet, Edge
    from dorian.pipeline.generation.eval_template import build_eval_template
    from uuid import uuid4

    # Locate the classifier + the y-encoding insertion point in the
    # incoming FLAML DAG. We only need the classifier and its
    # parameter satellites — the rest (read_csv replacement,
    # subscripts, accuracy_score) gets dropped in favour of the
    # eval template's canonical projections.
    nodes = dict(dag.nodes)
    edges = list(dag.edges)
    estimator_id = None
    estimator_task = "Classification"
    for nid, n in nodes.items():
        if not isinstance(n, Operator):
            continue
        if not any(n.name.startswith(p) for p in (
            "lightgbm.", "xgboost.", "catboost.", "sklearn.",
        )):
            continue
        if "Classifier" in n.name:
            estimator_id = nid
            estimator_task = "Classification"
            break
        if "Regressor" in n.name:
            estimator_id = nid
            estimator_task = "Regression"
            break
    if estimator_id is None:
        return dag

    # ── Build canonical eval template ─────────────────────────────
    tpl = build_eval_template(task=estimator_task)
    template_dag = tpl.dag
    splitter_id = next(
        nid for nid, _ in tpl.rl_entry_ports
    )
    proj_target_id = None
    # The y projection feeds split.y at position 1.
    for e in template_dag.edges:
        if e.destination == splitter_id and e.position == 1:
            proj_target_id = e.source
            break
    if proj_target_id is None:
        return dag

    # ── Copy estimator + its parameter satellites into template ───
    estimator_node = nodes[estimator_id]
    new_nodes = dict(template_dag.nodes)
    new_edges: list[Edge] = list(template_dag.edges)

    new_estimator_id = f"clf_{uuid4().hex[:6]}"
    new_nodes[new_estimator_id] = Operator(
        name=estimator_node.name, language=estimator_node.language,
    )
    # Hyperparameter satellites: every Parameter feeding the original
    # classifier becomes a fresh Parameter feeding the template-side copy.
    for e in edges:
        if e.destination != estimator_id:
            continue
        src = nodes.get(e.source)
        if not isinstance(src, Parameter):
            continue
        new_param_id = f"p_{src.name}_{uuid4().hex[:6]}"
        new_nodes[new_param_id] = Parameter(
            name=src.name, dtype=src.dtype, value=src.value,
        )
        new_edges.append(Edge(
            new_param_id, new_estimator_id,
            position=e.position, output=0,
        ))

    # ── Find the X projection feeding the splitter ──────────────────
    proj_features_id = None
    for e in new_edges:
        if e.destination == splitter_id and e.position == 0:
            proj_features_id = e.source
            break

    # ── Insert OrdinalEncoder + label_encoder BEFORE the split ─────
    # Encoders pre-split keeps the canonical 2-X-input contract simple:
    # one fit_transform call on full X, one Categorical pass on full y,
    # then split. Both train and test land in the same encoded space by
    # construction — no fit-on-train + transform-on-test plumbing
    # required, and no canvas-style transform_extra duplicates needed
    # in the inline compound expansion.
    encoder_id = f"ordinal_encoder_{uuid4().hex[:6]}"
    p_handle_id = f"p_handle_unknown_{uuid4().hex[:6]}"
    p_unknown_id = f"p_unknown_value_{uuid4().hex[:6]}"
    new_nodes[encoder_id] = Operator(
        name="sklearn.preprocessing.OrdinalEncoder", language="python",
    )
    new_nodes[p_handle_id] = Parameter(
        name="handle_unknown", dtype="str", value="use_encoded_value",
    )
    new_nodes[p_unknown_id] = Parameter(
        name="unknown_value", dtype="int", value="-1",
    )
    new_edges.append(Edge(p_handle_id, encoder_id, position="handle_unknown"))
    new_edges.append(Edge(p_unknown_id, encoder_id, position="unknown_value"))

    label_enc_id = f"label_encoder_{uuid4().hex[:6]}"
    new_nodes[label_enc_id] = Snippet(
        name="label_encoder",
        code=(
            "def foo(y):\n"
            "    import pandas as pd\n"
            "    return pd.Categorical(y).codes\n"
        ),
        language="python",
    )

    # Re-route proj_features→split through encoder, proj_target→split
    # through label_encoder. The template's frozen-edge convention is
    # for RL-agent rule enforcement; here we're constructing the DAG
    # so we modify edges freely.
    rewired_edges: list[Edge] = []
    for e in new_edges:
        if (e.source == proj_features_id and e.destination == splitter_id):
            rewired_edges.append(Edge(
                source=encoder_id, destination=splitter_id,
                position=e.position, output=0,
            ))
        elif (e.source == proj_target_id and e.destination == splitter_id):
            rewired_edges.append(Edge(
                source=label_enc_id, destination=splitter_id,
                position=e.position, output=0,
            ))
        else:
            rewired_edges.append(e)
    if proj_features_id is not None:
        rewired_edges.append(Edge(
            source=proj_features_id, destination=encoder_id,
            position=0, output=0,
        ))
    rewired_edges.append(Edge(
        source=proj_target_id, destination=label_enc_id,
        position=0, output=0,
    ))
    new_edges = rewired_edges

    # ── Wire classifier from split outputs (canonical Sklearn Estimator
    # X_train/y_train/X_test slots) ───────────────────────────────
    new_edges.append(Edge(splitter_id, new_estimator_id, position=0, output=0))
    new_edges.append(Edge(splitter_id, new_estimator_id, position=1, output=2))
    new_edges.append(Edge(splitter_id, new_estimator_id, position=2, output=1))

    # ── Wire classifier output to EVERY metric's y_pred slot ──────
    # Multi-metric templates (Classification → accuracy + f1 + roc_auc,
    # Regression → r2 + mae + rmse) fan out from the same predictions
    # source. Single-metric templates degrade to one fan-out edge.
    exit_target = tpl.rl_exit_target
    pred_pos = exit_target["input_port"]["position"]
    for metric_id in (exit_target.get("node_ids") or [exit_target.get("node_id")]):
        if not metric_id:
            continue
        new_edges.append(Edge(
            source=new_estimator_id, destination=metric_id,
            position=pred_pos, output=1,
        ))

    return DAG(nodes=new_nodes, edges=new_edges)


def _inject_implicit_preprocessing(dag):
    """Splice ``OrdinalEncoder`` (X), ``pd.Categorical(y).codes`` Snippet
    (y), and ``train_test_split`` between the dataset and the classifier
    so every extracted FLAML pipeline evaluates on a held-out test set.

    FLAML's ``TabularPredictor`` runs implicit categorical-encoding +
    holds out a CV split before invoking the underlying estimator —
    that's why the same script that succeeds inside FLAML blows up
    standalone with dtype / class errors AND, when those are fixed,
    scores on the training set (giving meaningless near-perfect
    accuracy). Both bugs are baked into the extractor here, by DAG
    post-processing rather than emitting more Python source — the
    parser's tuple-unpacking + identifier-rewire path was too brittle
    for cases like ``X_train, X_test, y_train, y_test =
    train_test_split(X, y)``.

    Resulting DAG shape:

        X_subscript ─▶ OrdinalEncoder ─▶ split (X@0)
        y_subscript ─▶ label_encoder ─▶ split (y@1)

        split.X_train (out=0) ─▶ classifier (X_train @ pos 0)
        split.X_test  (out=1) ─▶ classifier (X_test  @ pos 2)
        split.y_train (out=2) ─▶ classifier (y_train @ pos 1)
        split.y_test  (out=3) ─▶ accuracy_score (y_true @ pos 0)

        classifier (out=1) ─▶ accuracy_score (y_pred @ pos 1)

    The split outputs follow the ``sklearn.model_selection.train_test_split``
    annotation in ``annotations.kb``: ``[X_train@0, X_test@1, y_train@2,
    y_test@3]``. Encoders sit BEFORE the split so train and test share
    a single categorical mapping by construction.
    """
    from dorian.dag import DAG, Operator, Parameter, Snippet, Edge
    from uuid import uuid4

    nodes = dict(dag.nodes)
    edges = list(dag.edges)

    # Locate the classifier and the metric — required anchors. Skip
    # otherwise (RL-trainer / canvas pipelines have their own shape).
    estimator_id = None
    for nid, n in nodes.items():
        if not isinstance(n, Operator):
            continue
        if not any(n.name.startswith(p) for p in (
            "lightgbm.", "xgboost.", "catboost.", "sklearn.",
        )):
            continue
        if "Classifier" not in n.name and "Regressor" not in n.name:
            continue
        estimator_id = nid
        break
    if estimator_id is None:
        return dag

    metric_id = next(
        (nid for nid, n in nodes.items()
         if isinstance(n, Operator)
         and n.name in (
             "sklearn.metrics.accuracy_score",
             "sklearn.metrics.f1_score",
             "sklearn.metrics.r2_score",
             "sklearn.metrics.mean_squared_error",
         )),
        None,
    )

    # X-source and y-source: incoming non-Parameter edges to the
    # classifier at integer positions. Position 0 = X (training X
    # in the extractor's pre-split shape), position 1 = y.
    x_source = y_source = None
    estimator_data_edges = [
        e for e in edges
        if e.destination == estimator_id
        and not isinstance(nodes.get(e.source), Parameter)
    ]
    for e in estimator_data_edges:
        try:
            pos = int(e.position)
        except (TypeError, ValueError):
            continue
        if pos == 0 and x_source is None:
            x_source = e.source
        elif pos == 1 and y_source is None:
            y_source = e.source
    if x_source is None or y_source is None:
        return dag

    new_nodes = dict(nodes)
    new_edges: list[Edge] = []

    # ── Encoder (X) and label encoder (y) ──────────────────────────
    encoder_id = f"ordinal_encoder_{uuid4().hex[:6]}"
    p_handle_id = f"p_handle_unknown_{uuid4().hex[:6]}"
    p_unknown_id = f"p_unknown_value_{uuid4().hex[:6]}"
    label_enc_id = f"label_encoder_{uuid4().hex[:6]}"
    new_nodes[encoder_id] = Operator(
        name="sklearn.preprocessing.OrdinalEncoder", language="python",
    )
    new_nodes[p_handle_id] = Parameter(
        name="handle_unknown", dtype="str", value="use_encoded_value",
    )
    new_nodes[p_unknown_id] = Parameter(
        name="unknown_value", dtype="int", value="-1",
    )
    new_nodes[label_enc_id] = Snippet(
        name="label_encoder",
        code=(
            "def foo(y):\n"
            "    import pandas as pd\n"
            "    return pd.Categorical(y).codes\n"
        ),
        language="python",
    )

    # ── Splitter ──────────────────────────────────────────────────
    splitter_id = f"train_test_split_{uuid4().hex[:6]}"
    p_test_size_id = f"p_test_size_{uuid4().hex[:6]}"
    p_random_state_id = f"p_random_state_{uuid4().hex[:6]}"
    new_nodes[splitter_id] = Operator(
        name="sklearn.model_selection.train_test_split", language="python",
    )
    new_nodes[p_test_size_id] = Parameter(
        name="test_size", dtype="float", value="0.25",
    )
    new_nodes[p_random_state_id] = Parameter(
        name="random_state", dtype="int", value="42",
    )

    # ── Drop every original edge that points at the classifier or
    # metric and that originates from x_source / y_source / classifier
    # itself — we'll re-attach them via the split outputs.
    drop_set: set[tuple] = set()
    classifier_metric_edge = None  # classifier → metric (y_pred path)
    for e in edges:
        if e.destination == estimator_id and e.source in (x_source, y_source):
            drop_set.add(id(e))
        elif metric_id is not None and e.destination == metric_id and e.source == y_source:
            drop_set.add(id(e))
        elif metric_id is not None and e.source == estimator_id and e.destination == metric_id:
            classifier_metric_edge = e

    for e in edges:
        if id(e) in drop_set:
            continue
        new_edges.append(e)

    # ── Wire encoders ─────────────────────────────────────────────
    new_edges.append(Edge(x_source, encoder_id, position=0, output=0))
    new_edges.append(Edge(p_handle_id, encoder_id, position="handle_unknown"))
    new_edges.append(Edge(p_unknown_id, encoder_id, position="unknown_value"))
    new_edges.append(Edge(y_source, label_enc_id, position=0, output=0))

    # ── Wire splitter ─────────────────────────────────────────────
    # train_test_split(X, y, test_size=, random_state=)
    new_edges.append(Edge(encoder_id, splitter_id, position=0, output=0))
    new_edges.append(Edge(label_enc_id, splitter_id, position=1, output=0))
    new_edges.append(Edge(p_test_size_id, splitter_id, position="test_size"))
    new_edges.append(Edge(p_random_state_id, splitter_id, position="random_state"))

    # ── Wire classifier from split outputs ────────────────────────
    # Per annotations.kb: train_test_split outputs are
    # [X_train@0, X_test@1, y_train@2, y_test@3]. The classifier's
    # canonical Sklearn Estimator slots: X_train@0, y_train@1,
    # X_test@2.
    new_edges.append(Edge(splitter_id, estimator_id, position=0, output=0))
    new_edges.append(Edge(splitter_id, estimator_id, position=1, output=2))
    new_edges.append(Edge(splitter_id, estimator_id, position=2, output=1))

    # ── Wire metric from split's y_test + classifier's y_pred ─────
    if metric_id is not None:
        new_edges.append(Edge(splitter_id, metric_id, position=0, output=3))
        # The classifier→metric edge already exists in new_edges (we
        # didn't drop it). It carries y_pred from the predict-extra
        # output of the compound expansion.

    return DAG(nodes=new_nodes, edges=new_edges)


def _collapse_method_shortcuts(dag):
    """Collapse ``<Class> ─self→ fit ─self→ predict`` chains into a
    single class Operator.

    The Dorian convention for canvas / trial-config pipelines is that
    a sklearn estimator lives as a SINGLE class-named ``Operator``
    node — compound expansion at runtime turns it into the
    ``__init__ → fit → predict`` chain itself. The FLAML extractor,
    parsing ``clf = LGBM(...); fitted = clf.fit(X, y); pred =
    fitted.predict(X)``, instead emits THREE nodes — the class plus
    method-shortcut ``fit`` / ``predict`` — and downstream consumers
    wire from the predict node's output. That's a redundant
    pre-expansion of work the runtime will redo, and worse: when a
    mitigation rewrite (label encoder, ordinal encoder, …) needs to
    splice upstream of the classifier, it has to know which of the
    three nodes is "the classifier" — the class node carries the
    parameters but doesn't see the data, the fit node sees the
    training data but isn't the operator, the predict node sees the
    test data but isn't the operator either. Collapsing back to one
    class node makes mitigation logic match how it works for
    canvas / trial-config pipelines.

    Strategy: find every ``Operator(name="fit"|"predict"|…)`` node
    whose ``self`` edge resolves (transitively through method
    shortcuts) to a class operator with a dotted name. Move the
    method's data edges onto the class node (fit's X@1, y@2 →
    class@0, @1; predict's X_test@1 → class@2), reroute the
    method's outgoing edges from the class node, and delete the
    method shortcut. The rewrite is idempotent — no method
    shortcuts left after one pass means subsequent passes are
    no-ops. Trial-config / canvas pipelines (which never had the
    shortcuts to begin with) pass through unchanged.
    """
    from dorian.dag import DAG, Operator, Parameter, Edge

    method_shortcuts = {"fit", "predict", "transform", "fit_transform",
                        "fit_predict", "predict_proba", "decision_function",
                        "score", "score_samples", "inverse_transform"}

    nodes = dict(dag.nodes)
    edges = list(dag.edges)

    def _find_self_source(method_id: str) -> str | None:
        """Walk the chain back through method shortcuts to the
        underlying class operator. Returns the class operator's id,
        or ``None`` when there's no chain (or the chain doesn't end
        at a dotted-name class)."""
        cursor = method_id
        for _ in range(8):  # bounded — chains are short
            chain_edge = next(
                (e for e in edges
                 if e.destination == cursor and (
                     e.position == "self" or e.position == 0
                 ) and not isinstance(nodes.get(e.source), Parameter)),
                None,
            )
            if chain_edge is None:
                return None
            src_node = nodes.get(chain_edge.source)
            if not isinstance(src_node, Operator):
                return None
            if "." in src_node.name:
                return chain_edge.source
            if src_node.name in method_shortcuts:
                cursor = chain_edge.source
                continue
            return None
        return None

    # Map each method shortcut node to the class it should fold into.
    # Process methods in chain order: ``fit`` first (its data goes to
    # class slots 0/1), then ``predict`` (slot 2 for X_test).
    method_order = ["fit", "fit_transform", "transform",
                    "predict", "predict_proba", "decision_function",
                    "fit_predict", "score", "score_samples",
                    "inverse_transform"]
    method_nodes: list[tuple[str, str, str]] = []  # (method_id, class_id, method_name)
    for nid, node in nodes.items():
        if not isinstance(node, Operator):
            continue
        if node.name not in method_shortcuts:
            continue
        class_id = _find_self_source(nid)
        if class_id is None:
            continue
        method_nodes.append((nid, class_id, node.name))

    if not method_nodes:
        return dag

    # Stable ordering for slot allocation per class.
    method_nodes.sort(key=lambda t: (
        method_order.index(t[2]) if t[2] in method_order else 99,
        t[0],
    ))

    # Track next free positional slot per class (0 = X, 1 = y, 2 = X_test).
    next_slot: dict[str, int] = {}
    new_edges: list[Edge] = []
    drop_methods = {mid for mid, _, _ in method_nodes}
    # Edges to keep: everything except chain self-edges to dropped methods
    # and edges whose source/destination is a dropped method (handled below).
    for e in edges:
        if e.source in drop_methods or e.destination in drop_methods:
            continue
        new_edges.append(e)

    # Re-route each method's data edges onto its class.
    for mid, class_id, mname in method_nodes:
        slot_base = next_slot.setdefault(class_id, 0)
        # Method's incoming non-self, non-chain data edges, sorted by
        # original position (numeric first, then strings).
        method_in = []
        for e in edges:
            if e.destination != mid:
                continue
            if e.position == "self" or e.position == 0:
                continue
            if isinstance(nodes.get(e.source), Parameter):
                continue
            method_in.append(e)
        method_in.sort(key=lambda e: (
            (0, int(e.position)) if isinstance(e.position, int)
            else (1, str(e.position))
        ))
        for e in method_in:
            new_edges.append(Edge(
                source=e.source,
                destination=class_id,
                position=slot_base,
                output=e.output,
            ))
            slot_base += 1
        next_slot[class_id] = slot_base

        # Method's outgoing edges → originate from class instead.
        # Skip edges whose destination is ALSO a dropped method —
        # those are intra-chain self-edges (e.g. ``fit → predict
        # pos=self``) that have no meaning once both methods are
        # collapsed; the chain reconstitutes at compound-expansion
        # time.
        for e in edges:
            if e.source != mid:
                continue
            if e.destination in drop_methods:
                continue
            new_edges.append(Edge(
                source=class_id,
                destination=e.destination,
                position=e.position,
                output=e.output,
            ))

    # Drop the method-shortcut nodes themselves.
    new_nodes = {nid: n for nid, n in nodes.items() if nid not in drop_methods}
    return DAG(nodes=new_nodes, edges=new_edges)


def _replace_read_csv_with_dataset(dag):
    """Post-process: collapse ``pandas.read_csv(fpath)`` into a single
    ``dorian.io.dataset`` Operator.

    The FLAML extractor's emitted code starts with ``df =
    pd.read_csv(fpath)``. The tree-sitter parser correctly produces a
    ``pandas.read_csv`` Operator with a free-identifier ``fpath`` node
    feeding it, but neither the canvas nor the dataset-expansion
    pipeline knows how to render or execute that pair — the canvas
    shows the identifier as an unnamed numeric box and the executor
    has no resolver for the bare identifier. ``dorian.io.dataset`` is
    the canonical Dorian operator for "load a CSV into X / y"; it
    gets expanded at runtime by ``DATASET_EXPANSION_RULE`` (which
    injects the ``fpath`` parameter from session meta — the pipeline
    body itself has NO fpath, so the same DAG runs on any dataset).
    Adding an ``fpath`` Parameter here would (a) bake a single
    dataset's path into the pipeline (defeating the point of having
    one DAG run cross-product against every dataset), and (b)
    duplicate the parameter the expansion rule injects, breaking
    idempotence on re-expansion.

    The post-processing here is a pure DAG rewrite: find a
    ``pandas.read_csv`` Operator whose only data input is an
    identifier-typed node with text ``fpath``, drop both, install
    one ``dorian.io.dataset`` Operator, rewire all of read_csv's
    outgoing edges from the new operator. No-op when the pattern
    doesn't match.
    """
    from dorian.dag import DAG, Operator, Parameter, Edge, Node
    nodes = dict(dag.nodes)
    edges = list(dag.edges)

    read_csv_id = next(
        (nid for nid, n in nodes.items()
         if isinstance(n, Operator) and n.name == "pandas.read_csv"),
        None,
    )
    if read_csv_id is None:
        return dag

    # The fpath identifier is the read_csv's only non-Parameter incoming.
    incoming_data = [
        e for e in edges
        if e.destination == read_csv_id
        and not isinstance(nodes.get(e.source), Parameter)
    ]
    if len(incoming_data) != 1:
        return dag
    fpath_edge = incoming_data[0]
    fpath_node = nodes.get(fpath_edge.source)
    fpath_text = getattr(fpath_node, "text", None) or getattr(fpath_node, "name", None)
    if fpath_text != "fpath":
        return dag

    dataset_id = f"dataset_{read_csv_id}"
    new_nodes = {
        nid: n for nid, n in nodes.items()
        if nid not in (read_csv_id, fpath_edge.source)
    }
    new_nodes[dataset_id] = Operator(name="dorian.io.dataset", language="python")
    new_edges: list[Edge] = []
    for e in edges:
        if e.destination == read_csv_id or e.source == fpath_edge.source:
            continue
        if e.source == read_csv_id:
            new_edges.append(Edge(
                source=dataset_id,
                destination=e.destination,
                position=e.position,
                output=e.output,
            ))
        else:
            new_edges.append(e)
    return DAG(nodes=new_nodes, edges=new_edges)


# ---------------------------------------------------------------------------
# Helpers: step extraction
# ---------------------------------------------------------------------------

def _extract_steps(pipeline: Any) -> list[tuple[str, Any]]:
    """Return a list of (step_name, estimator) pairs.

    Accepts ``sklearn.pipeline.Pipeline`` (uses ``.steps``) or a bare
    estimator (wraps as a single-step list).
    """
    steps = getattr(pipeline, "steps", None)
    if steps is not None:
        return list(steps)
    return [(type(pipeline).__name__.lower(), pipeline)]


def _is_estimator(step: Any) -> bool:
    """True when *step* is a predictor (has ``predict``) and not just a transformer."""
    return hasattr(step, "predict")


# ---------------------------------------------------------------------------
# Helpers: hyperparameter emission
# ---------------------------------------------------------------------------

def _hyperparams_to_emit(estimator: Any) -> Iterable[tuple[str, Any]]:
    """Yield (name, value) pairs for hyperparameters that differ from default.

    Matches the behaviour of sklearn's own ``__repr__`` (emit only
    non-default args) so emitted code round-trips the pipeline
    faithfully without the noise of every default parameter.
    """
    try:
        defaults = type(estimator)()
    except Exception:
        # Estimator has required positional args; dump everything so
        # the reconstruction is at least complete.
        yield from estimator.get_params(deep=False).items()
        return

    current = estimator.get_params(deep=False)
    baseline = defaults.get_params(deep=False)
    for k, v in current.items():
        if k not in baseline:
            yield k, v
            continue
        if _roughly_equal(v, baseline[k]):
            continue
        yield k, v


def _roughly_equal(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        if a == b:
            return bool(a == b)
    except Exception:
        pass
    try:
        return repr(a) == repr(b)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers: value → Python source
# ---------------------------------------------------------------------------

_SAFE_REPR_TYPES: tuple[type, ...] = (bool, int, float, str)


def _render_value(v: Any, imports: dict[str, set[str]]) -> str:
    """Render *v* as valid Python source, registering any required imports.

    Falls through to ``repr(v)`` for objects whose ``__repr__`` is
    already valid Python (the common sklearn case: nested estimators
    print as ``DecisionTreeClassifier(max_depth=3)``). The extractor
    resolves the class name against the imports, so the rendered
    code becomes another parse tree the existing rules can walk.
    """
    if v is None:
        return "None"
    if isinstance(v, _SAFE_REPR_TYPES):
        return repr(v)
    if isinstance(v, (list, tuple)):
        inner = ", ".join(_render_value(x, imports) for x in v)
        return f"[{inner}]" if isinstance(v, list) else f"({inner},)" if len(v) == 1 else f"({inner})"
    if isinstance(v, dict):
        inner = ", ".join(
            f"{_render_value(k, imports)}: {_render_value(x, imports)}"
            for k, x in v.items()
        )
        return "{" + inner + "}"
    # Nested estimator — pull its module into the imports and reuse
    # its repr. sklearn estimator reprs are valid Python.
    module = getattr(type(v), "__module__", None)
    name = getattr(type(v), "__name__", None)
    if module and name:
        _add_import(imports, module, name)
        return repr(v)
    # Unknown object — fall back to repr. May produce code the
    # extractor can't parse; the seeder catches that and records
    # it in the manifest for follow-up.
    return repr(v)


# ---------------------------------------------------------------------------
# Helpers: import block
# ---------------------------------------------------------------------------

def _add_import(imports: dict[str, set[str]], module: str, name: str) -> None:
    """Register ``from <module> import <name>`` in *imports*.

    Consolidates names from the same module into a single import
    line so the emitted code stays readable.
    """
    if module in ("builtins", "__main__"):
        return
    # sklearn sometimes exposes a top-level alias (e.g.
    # ``sklearn.linear_model.LogisticRegression``) whose canonical
    # module is an internal submodule (``sklearn.linear_model._logistic``).
    # Normalise to the public submodule where available so the
    # emitted import survives across sklearn versions.
    module = _public_module(module, name)
    imports.setdefault(module, set()).add(name)


_PRIVATE_PREFIX = re.compile(r"^(sklearn\.[a-z_]+)\._.*$")


def _public_module(module: str, _name: str) -> str:
    m = _PRIVATE_PREFIX.match(module)
    if m:
        return m.group(1)
    return module


def _format_imports(imports: dict[str, set[str]]) -> str:
    out: list[str] = []
    # Pandas first — it's the IO primitive the extractor scans for.
    if "pandas as pd" in imports:
        out.append("import pandas as pd")
    for mod, names in sorted(imports.items()):
        if mod == "pandas as pd":
            continue
        joined = ", ".join(sorted(names))
        out.append(f"from {mod} import {joined}")
    return "\n".join(out) + "\n\n"
