"""
dorian/pipeline/transforms.py
-------------------------------
Pipeline-level DAG rewrite rules applied by the execution engine before
the Dask graph is built.

Design intent
-------------
Rules are expressed as ``RewriteRule`` + ``Apply`` so they can be
dispatched in two ways:

  Synchronous  (run_pipeline — background thread, no event loop):
      sync_apply(rule, dag, meta)

  Asynchronous (future error-handling / mitigation flows — event loop):
      await apply(rule, dag, meta)    # from dorian.pipeline.parser

Both paths share the same ``RewriteRule`` definitions and the same
``Apply.f`` functions, so mitigation rules written here can later be
composed with the full ``transform()`` pipeline without modification.

The ``meta`` dict is the session-context carrier.  Every ``Apply.f``
receives it as a third positional argument so rules can be context-aware
without accessing globals or singletons:

    def my_rule(dag, mapping, meta):
        session = meta["session"]
        ...

See also
--------
dorian/code/parsing/rule.py  – Apply, RewriteRule, Transformation types (canonical)
dorian/pipeline/parser.py    – match(), apply(), transform() (async)
"""
from __future__ import annotations

import importlib
import inspect
import json
from typing import Any, Dict, TypedDict
from uuid import uuid4

from dorian.infra.keys import RedisKeys
from dorian.code.parsing.rule import Add, Apply, Delete, RewriteRule
from dorian.pipeline.parser import match
from dorian.dag import DAG, Edge, Node, Operator, Parameter, Snippet
from backend.config import config
from backend.events import Event, emit


def _sync_redis():
    """Lazy sync Redis accessor.

    Import is deferred so that callers which only use the import-time
    machinery in this module (e.g. the MCP server's dry_run_rule path,
    which reaches ``sync_apply`` → ``transforms``) don't trigger the
    full ``backend.envs`` boot chain — that chain instantiates
    ``dask.distributed.LocalCluster``, which hangs or prints to stdout
    and corrupts MCP's JSON-RPC stdio channel.
    """
    from backend.envs import redis as _r
    return _r


# ---------------------------------------------------------------------------
# Context carrier type (replaces bare ``dict`` for meta dicts)
# ---------------------------------------------------------------------------

class DatasetMeta(TypedDict, total=False):
    """Context carried through dataset-expansion rewrite rules.

    Both fields are required by ``_expand_dataset``; ``total=False`` is used
    so callers that only know ``session`` can still be type-checked.
    """
    fpath: str
    loader: str


class SessionMeta(TypedDict, total=False):
    """Minimal session context used as the ``meta`` dict in rewrite rules."""
    session: str


# ---------------------------------------------------------------------------
# MIME type → concrete pandas loader
# ---------------------------------------------------------------------------

_pipe_cfg = config.pipeline
_MIME_TO_LOADER: Dict[str, str] = dict(_pipe_cfg.mime_to_loader)
_DEFAULT_LOADER: str = str(_pipe_cfg.default_loader)


# ---------------------------------------------------------------------------
# Synchronous dispatch helper
# ---------------------------------------------------------------------------

def sync_apply(rule: RewriteRule, dag: DAG, meta: dict) -> DAG:
    """Apply a RewriteRule to a DAG synchronously.

    Mirrors the recursion of ``dorian.pipeline.parser.apply()`` but without
    the async machinery.  Use this from synchronous contexts (e.g.
    ``run_pipeline`` inside a background thread).

    Async callers — error handlers, mitigation flows — should use
    ``await apply(rule, dag, meta)`` from ``dorian.pipeline.parser`` so
    both contexts share the same ``RewriteRule`` instance.

    ``processed`` tracks candidates already visited so that an expansion
    function that returns the DAG unchanged (e.g. for ``Function``-interface
    operators) does not cause an infinite re-match loop.
    """
    processed: list = []
    is_matched, candidate = match(rule.pattern, dag, processed)
    while is_matched:
        processed.append(candidate)   # prevent re-match on unchanged return
        current_mapping = dict(candidate)
        for tf in rule.transformations:
            if isinstance(tf, Add):
                # Named nodes: create UUIDs, extend mapping for subsequent transformations
                _nodes = {}
                if isinstance(tf.nodes, dict):
                    for local_id, node in tf.nodes.items():
                        uid = str(uuid4())
                        _nodes[uid] = node
                        current_mapping[local_id] = uid
                elif tf.nodes:
                    for node in tf.nodes:
                        _nodes[str(uuid4())] = node
                _edges = []
                if tf.edges:
                    for e in tf.edges:
                        if isinstance(e, Edge):
                            _edges.append(Edge(
                                source=current_mapping.get(e.source, e.source),
                                destination=current_mapping.get(e.destination, e.destination),
                                position=e.position,
                                output=e.output,
                            ))
                        else:
                            _edges.append(Edge(
                                source=current_mapping.get(e[0], e[0]),
                                destination=current_mapping.get(e[1], e[1]),
                            ))
                dag = DAG(nodes=dict(dag.nodes, **_nodes), edges=dag.edges + _edges)
            elif isinstance(tf, Delete):
                mapped = [current_mapping[n] for n in (tf.nodes or [])]
                _nodes = {k: v for k, v in dag.nodes.items() if k not in mapped}
                foo = lambda x: (current_mapping[x[0]], current_mapping[x[1]])
                to_remove = list(map(foo, tf.edges)) if tf.edges else []
                _edges = [
                    e for e in dag.edges
                    if (e.source, e.destination) not in to_remove
                    and e.source not in mapped
                    and e.destination not in mapped
                ]
                dag = DAG(nodes=_nodes, edges=_edges)
            elif isinstance(tf, Apply):
                dag = tf.f(dag, current_mapping, meta)
        is_matched, candidate = match(rule.pattern, dag, processed)
    return dag


# ---------------------------------------------------------------------------
# Dataset reference expansion
# ---------------------------------------------------------------------------

_SPLIT_XY_SNIPPET = '''\
def foo(df, features=None, target=None):
    """Split a DataFrame into (X, y) using session feature/target columns.

    Injected by ``DATASET_EXPANSION_RULE``. ``features`` and ``target`` are
    passed as kwargs from Parameter nodes seeded from session dataset meta.
    Falls back to "all-but-last-column is X, last is y" when either is empty
    so pipelines against un-profiled datasets still execute.
    """
    import pandas as pd
    if isinstance(features, str):
        features = [features]
    if isinstance(target, (list, tuple)):
        target = target[0] if target else None
    if not target:
        target = df.columns[-1]
    if not features:
        features = [c for c in df.columns if c != target]
    X = df[list(features)]
    y = df[target]
    return X, y
'''


def _expand_dataset(dag: DAG, mapping: Dict[str, str], meta: Dict[str, Any]) -> DAG:
    """Replace a ``dorian.io.dataset`` node with a full X/y loader sub-chain.

    Expanded sub-chain::

        Parameter(fpath) ─┐
                          ├─► loader (pandas.read_csv) ─► split_xy (Snippet)
        Parameter(features) ─► split_xy
        Parameter(target)   ─► split_xy

    The trailing snippet returns a 2-tuple ``(X, y)`` so downstream consumers
    (``train_test_split``, feature transformers, …) can wire their inputs by
    output port: ``output=0`` → X, ``output=1`` → y.

    Expected ``meta`` keys:
        fpath   (str)           – absolute path to the CSV / Excel / … file
        loader  (str)           – dotted operator name, e.g. ``'pandas.read_csv'``
        features (list[str] | None) – feature column names from session meta
        target   (str | list[str] | None) – target column name from session meta

    Outgoing edges of the matched node are remapped by ``position``: edges
    destined for position ``0`` (the "X" input) are rewired from snippet
    output ``0``; edges destined for position ``1`` (the "y" input) are
    rewired from snippet output ``1``. Any other outgoing position is wired
    from output ``0`` (X) as a safe default.
    """
    nid      = mapping["n"]
    fpath    = meta["fpath"]
    loader   = meta["loader"]
    features = meta.get("features") or []
    target   = meta.get("target") or ""

    outgoing = [
        (e.destination, e.position, e.output)
        for e in dag.edges if e.source == nid
    ]

    # Decide whether to inject the ``split_xy`` snippet. It is required only
    # when a downstream node expects a separate y channel from the dataset —
    # indicated by an outgoing edge using ``output=1`` (the y port). Legacy
    # pipelines (auto-sklearn trial configs, printout-only previews) wire
    # everything from ``output=0`` and bring their own X/y split (e.g. a
    # bespoke ``auto_select`` snippet); for those we keep the loader chain
    # as a single DataFrame source so their downstream graphs are unchanged.
    def _wants_y(out) -> bool:
        if out is None:
            return False
        try:
            return int(out) == 1
        except (ValueError, TypeError):
            return False

    needs_split = any(_wants_y(out) for _dst, _pos, out in outgoing)

    fpath_id  = f"fpath_{nid}"
    loader_id = f"loader_{nid}"

    new_nodes = {k: v for k, v in dag.nodes.items() if k != nid}
    new_nodes[fpath_id]  = Parameter(name="fpath", dtype="str", value=fpath)
    new_nodes[loader_id] = Operator(name=loader, language="python")
    new_edges = [e for e in dag.edges if e.source != nid and e.destination != nid]
    new_edges.append(Edge(fpath_id, loader_id, position=0))

    if not needs_split:
        # Legacy single-DataFrame fan-out: outgoing edges keep their original
        # ``position``/``output`` and originate from the loader directly.
        for dst, pos, out in outgoing:
            new_edges.append(Edge(loader_id, dst, position=pos, output=out))
        return DAG(nodes=new_nodes, edges=new_edges)

    # Feature / target split fan-out: loader → split_xy → (X, y)
    split_id    = f"split_xy_{nid}"
    features_id = f"features_{nid}"
    target_id   = f"target_{nid}"

    new_nodes[features_id] = Parameter(
        name="features", dtype="eval", value=repr(list(features)),
    )
    # ``target`` may be a single column or a list — pass through with repr so
    # the ``eval`` dtype reconstructs whichever shape was stored.
    new_nodes[target_id] = Parameter(
        name="target", dtype="eval", value=repr(target),
    )
    new_nodes[split_id] = Snippet(
        name="split_xy", code=_SPLIT_XY_SNIPPET, language="python",
    )

    # loader → split_xy (positional arg 0: the DataFrame)
    new_edges.append(Edge(loader_id, split_id, position=0))
    # features / target → split_xy (keyword args)
    new_edges.append(Edge(features_id, split_id, position="features"))
    new_edges.append(Edge(target_id,   split_id, position="target"))

    # Rewire outgoing edges by their declared output port: the X channel
    # (output 0) stays X, the y channel (output 1) is routed from the
    # snippet's second return value. Non-numeric output ports (if any) are
    # passed through unchanged on the X path as a safe default.
    for dst, pos, out in outgoing:
        try:
            out_int = int(out) if out is not None else 0
        except (ValueError, TypeError):
            out_int = 0
        if out_int == 1:
            new_edges.append(Edge(split_id, dst, position=pos, output=1))
        else:
            new_edges.append(Edge(split_id, dst, position=pos, output=0))

    return DAG(nodes=new_nodes, edges=new_edges)


DATASET_EXPANSION_RULE = RewriteRule(
    pattern=DAG(
        nodes={"n": Node(type="Operator", text=r"dorian\.io\.dataset")},
        edges=[],
    ),
    description="expand dorian.io.dataset to a concrete file-loader sub-chain",
    transformations=[Apply(f=_expand_dataset)],
)


# ---------------------------------------------------------------------------
# Public entry point (synchronous)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Compound operator expansion
# ---------------------------------------------------------------------------

def _fit_arity(class_dotted: str, fit_method_name: str) -> int:
    """Required positional data args for *fit_method_name*, excluding *self*.

    Uses Python introspection on the actual library class.  Falls back to 1
    (the common case: ``fit(X)``) if the class cannot be imported or the
    method signature cannot be read.

    This is the only piece derived from Python introspection; every other
    piece of knowledge (method names, sequence order, operator→interface
    binding) comes from the KB.
    """
    try:
        module_path, cls_name = class_dotted.rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
        sig = inspect.signature(getattr(cls, fit_method_name))
        return sum(
            1
            for n, p in sig.parameters.items()
            if n != "self"
            and p.default is inspect.Parameter.empty
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            )
        )
    except Exception:
        return 1


def _get_init_param_names(class_dotted: str) -> frozenset[str]:
    """Return the parameter names accepted by a class's ``__init__``.

    Traverses the MRO so inherited params (e.g. ``device`` from
    ``Guardrail.__init__``, ``token`` from ``HFNLPGuardrail.__init__``)
    are included.  Returns an empty set on import/introspection failure.
    """
    try:
        module_path, cls_name = class_dotted.rsplit(".", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
        sig = inspect.signature(cls.__init__)
        return frozenset(n for n in sig.parameters if n != "self")
    except Exception:
        return frozenset()


_PASSTHROUGH_SNIPPET_CODE = '''def foo(instance, data, **kwargs):
    """Passthrough guardrail: validate data then return it unchanged.

    1. Extract text from LLM pipeline data (format-aware)
    2. Resolve ontological arguments from the instance's class hierarchy
    3. Auto-coerce kwargs to the method's type-hinted types
    4. Call instance.validate(...)
    5. Check results — raise on failure
    6. Return original data (passthrough)
    """
    import inspect as _inspect

    # -- Extract text from various LLM pipeline data formats --
    if isinstance(data, list) and data and isinstance(data[0], dict):
        texts = [str(m.get("content", "")) for m in data]
    elif hasattr(data, "choices"):
        texts = [str(c.message.content) for c in data.choices]
    elif isinstance(data, str):
        texts = [data]
    else:
        texts = [str(data)]

    # -- Resolve dataset_risk from class attribute --
    categories = getattr(instance, "guardrail_category", [])
    risk_from_class = categories[0] if categories else None

    # -- Build validate kwargs from incoming kwargs + instance introspection --
    validate_kwargs = {}
    try:
        hints = _inspect.get_annotations(type(instance).validate)
    except Exception:
        hints = {}

    # guardrail_type: coerce string → enum via type hint
    gt = kwargs.get("guardrail_type")
    if gt is not None:
        gt_hint = hints.get("guardrail_type")
        if gt_hint and isinstance(gt, str) and callable(gt_hint):
            try:
                gt = gt_hint(gt)
            except Exception:
                pass
        validate_kwargs["guardrail_type"] = gt

    # dataset_risk: prefer kwargs, fallback to class attribute
    dr = kwargs.get("dataset_risk")
    if dr is not None:
        dr_hint = hints.get("dataset_risk")
        if dr_hint and isinstance(dr, str) and callable(dr_hint):
            try:
                dr = dr_hint(dr)
            except Exception:
                pass
        validate_kwargs["dataset_risk"] = dr
    elif risk_from_class is not None:
        validate_kwargs["dataset_risk"] = risk_from_class

    # Pass through remaining kwargs (e.g. threshold override)
    for k, v in kwargs.items():
        if k in ("guardrail_type", "dataset_risk"):
            continue
        hint = hints.get(k)
        if hint and isinstance(v, str) and callable(hint):
            try:
                v = hint(v)
            except Exception:
                pass
        validate_kwargs[k] = v

    # -- Call validate --
    results = instance.validate(texts, **validate_kwargs)

    # -- Check results (duck-typed, no imports) --
    for r in results:
        if hasattr(r, "outcome"):
            val = getattr(r.outcome, "value", str(r.outcome))
            if val.lower() not in ("pass", "passed", "ok", "safe"):
                msg = getattr(r, "message", str(r))
                raise RuntimeError(f"Guardrail failed: {msg}")

    # -- Passthrough: return original data --
    return data
'''


class CompoundExpansionError(RuntimeError):
    """Surgical error: tells the runner WHICH node + operator failed, why.

    Raised in place of bare ``KeyError`` / ``RuntimeError`` so the
    error envelope reaching ``execution.run_pipeline`` carries enough
    context for the SPA to mark a specific node failed rather than
    blanket-failing every node in the run. The ``__str__`` form is
    user-readable; the structured fields are surfaced via
    ``PipelineExpansionFailed`` event payload.
    """

    def __init__(
        self,
        *,
        node_id: str,
        operator: str | None = None,
        reason: str = "compound_expansion",
        detail: str = "",
        cause: BaseException | None = None,
    ):
        op_label = operator or "<unknown>"
        msg = (
            f"compound expansion failed for node '{node_id}' "
            f"(operator '{op_label}'): {reason}"
        )
        if detail:
            msg += f" — {detail}"
        if cause is not None:
            msg += f" (cause: {type(cause).__name__}: {cause})"
        super().__init__(msg)
        self.node_id = node_id
        self.operator = op_label
        self.reason = reason
        self.detail = detail
        self.cause = cause


def _expand_compound_operator(
    dag: DAG, mapping: Dict[str, str], meta: Dict[str, Any]
) -> DAG:
    """Expand a single class-interface operator into its internal method sub-DAG.

    Given an ``Operator(name="sklearn.X.Y")`` with interface ``Sklearn Transformer``
    (method sequence ``[__init__, fit, transform]``), produces::

        __init__ → fit → transform_0
                       → transform_1
                       → …

    where the number of ``transform`` (or ``predict``) nodes equals the
    number of outgoing data ports on the original compound node.

    Parameter routing is KB-driven when declarations exist: each Parameter
    edge is routed to the method specified by its ``method`` field in the KB.
    Falls back to routing all params to ``__init__`` for operators without KB
    parameter declarations.

    For interfaces with the ``passthrough`` attribute (e.g. ``Guardrail``),
    the infer method is replaced by a generated Snippet that calls the method,
    checks results, and returns the original data unchanged.

    Returns the DAG unchanged for:
    - non-``Operator`` nodes (type guard)
    - method shortcuts without ``"."`` (``fit``, ``transform``, ``predict``, …)
    - operators with no incoming data edges (already-expanded ``__init__`` nodes)
    - operators with no KB interface entry (logs warning)
    - ``Function`` interface operators (no expansion needed)
    """
    # Deferred imports to avoid circular dependencies at module load time.
    from dorian.knowledge.queries import (
        get_operator_interface, get_method_sequence,
        get_operator_parameters, get_interface_attributes,
        get_all_interface_methods, get_method_io, get_interface_io,
    )

    nid = mapping["n"]
    node = dag.nodes[nid]

    if not isinstance(node, Operator):
        return dag

    # Guard 1: method shortcut nodes (fit, transform, chat.send, …) are sub-DAG
    # nodes created by a prior expansion — they must not be re-expanded.
    # Check KB-declared methods first, fall back to "no dot" heuristic.
    if node.name in get_all_interface_methods() or "." not in node.name:
        return dag

    # Guard 2: nodes already produced by a prior expansion round carry the
    # "_cx_" tag in their ID (e.g. "<uuid>_cx_init").  Re-expanding them
    # would loop infinitely because the init node keeps the original name.
    if "_cx_" in nid:
        return dag

    # Split incoming edges now so we can apply guards below.
    param_edges = [
        e
        for e in dag.edges
        if e.destination == nid and isinstance(dag.nodes.get(e.source), Parameter)
    ]
    data_edges = sorted(
        [
            e
            for e in dag.edges
            if e.destination == nid
            and not isinstance(dag.nodes.get(e.source), Parameter)
        ],
        key=lambda e: e.position,
    )

    interface = get_operator_interface(node.name)
    if interface is None:
        emit(Event("CompoundExpansionSkipped", {
            "source": "transforms._expand_compound_operator",
            "operator": node.name,
            "reason": "No interface found in KB — add 'X is a Function' or 'X is a <ClassInterface>'",
        }))
        return dag

    methods_raw = get_method_sequence(interface)
    methods = list(dict.fromkeys(methods_raw))

    if len(methods) < 2:
        # Function interface (len=0) — direct callable, no expansion.
        # A class with a degenerate 1-method chain (len=1) is almost
        # certainly a KB seeding gap: the ``calls`` edges are missing.
        # If the Python object IS a class (inspect.isclass), treating
        # it as a Function would hand the class to Dask and crash at
        # runtime with "_cached_init() takes 0 positional arguments".
        # Emit a visible skip reason so we can grep the operator name
        # out of observability and fix its KB declaration.
        if isinstance(node, Operator):
            try:
                from dorian.pipeline.operator_resolver import _resolve_dotted
                import inspect
                resolved = _resolve_dotted(node.name)
                is_class = inspect.isclass(resolved)
            except Exception:
                is_class = False
            if is_class:
                emit(Event("CompoundExpansionSkipped", {
                    "source": "transforms._expand_compound_operator",
                    "operator": node.name,
                    "interface": interface,
                    "methods": methods,
                    "reason": (
                        f"class operator but method chain has only "
                        f"{len(methods)} step(s); add ``calls`` "
                        f"edges to the KB interface for this operator"
                    ),
                }))
        return dag

    # Check for passthrough attribute (e.g. Guardrail interface)
    is_passthrough = "passthrough" in get_interface_attributes(interface)

    out_edges = [e for e in dag.edges if e.source == nid]

    # Guard 3 removed: frontend no longer creates sub-DAG nodes —
    # Group nodes are flattened by _flatten_groups() before this rule runs.
    # Legacy pipelines without Groups are handled by the normal expansion below.

    prefix = f"{nid}_cx"

    # --- KB-driven parameter routing ---
    # Build a map: param_name → method_name (None means __init__)
    kb_params = get_operator_parameters(node.name)
    param_method_map: Dict[str, str | None] = {}
    if kb_params:
        for p in kb_params:
            param_method_map[p["name"]] = p.get("method")

    # --- Passthrough mode (e.g. Guardrail: __init__ + passthrough Snippet) ---
    if is_passthrough:
        init_id = f"{prefix}_init"
        snippet_id = f"{prefix}_passthrough"

        new_nodes = {k: v for k, v in dag.nodes.items() if k != nid}
        new_nodes[init_id] = Operator(name=node.name, language=node.language)
        new_nodes[snippet_id] = Snippet(
            name=f"{node.name}__passthrough",
            code=_PASSTHROUGH_SNIPPET_CODE,
            language="python",
        )

        new_edges = [
            e for e in dag.edges if e.source != nid and e.destination != nid
        ]

        # Route parameters: introspect __init__ to determine which params belong
        # there vs the snippet.  The KB method map alone is unreliable because
        # `;` chaining in the KB grammar can create disjoint nodes for the same
        # method name (e.g. the `validate` node in the `calls` chain is a
        # different node from the standalone `validate` that declares params).
        # Introspection on the actual Python class is authoritative.
        init_param_names = _get_init_param_names(node.name)

        for e in param_edges:
            param_node = dag.nodes.get(e.source)
            pname = param_node.name if isinstance(param_node, Parameter) else None

            # KB says explicitly it's a non-__init__ method param → snippet
            target_method = param_method_map.get(pname) if pname else None
            if target_method and target_method != "__init__":
                new_edges.append(
                    Edge(e.source, snippet_id, position=e.position, output=e.output)
                )
            # Introspection confirms it's an __init__ param → __init__
            elif pname and pname in init_param_names:
                new_edges.append(
                    Edge(e.source, init_id, position=e.position, output=e.output)
                )
            # Unknown param (rewrite-injected runtime param like guardrail_type,
            # dataset_risk) → snippet, which accepts **kwargs and forwards them
            elif pname:
                new_edges.append(
                    Edge(e.source, snippet_id, position=e.position, output=e.output)
                )
            else:
                # Fallback: __init__ (defensive)
                new_edges.append(
                    Edge(e.source, init_id, position=e.position, output=e.output)
                )

        # __init__ → snippet (instance at position 0)
        new_edges.append(Edge(init_id, snippet_id, position=0, output=0))

        # Data edges → snippet (data at position 1)
        # Passthrough takes one data input — use the first data edge.
        if data_edges:
            new_edges.append(
                Edge(data_edges[0].source, snippet_id, position=1, output=data_edges[0].output)
            )

        # Rewire outgoing edges from snippet
        for e in out_edges:
            new_edges.append(
                Edge(snippet_id, e.destination, position=e.position, output=0)
            )

        return DAG(nodes=new_nodes, edges=new_edges)

    # --- Generic KB-driven N-method expansion ---
    #
    # Expands a class-interface operator into a strictly linear method chain:
    #
    #     __init__ → fit → transform   (Sklearn Transformer)
    #     __init__ → fit → predict     (Sklearn Estimator)
    #     __init__ → ... → method_N    (any N-method chain)
    #
    # Each method is a single node.  Data edges are routed to the method that
    # the KB says consumes them (name-based match via per-method I/O).  The
    # evaluation pipeline — "fit on train, apply on test" — is the caller's
    # responsibility: the eval procedure is expected to declare an interface
    # input (e.g. ``X_test``) and route it through the ``transform.X`` /
    # ``predict.X`` method-local port.  Compound expansion does NOT invent
    # extra terminal-method copies to absorb that pattern.
    #
    # Historical note: earlier revisions spawned a second terminal node
    # (``transform_extra``/``predict_extra``) for every interface input NOT
    # consumed by any method.  That workaround existed because the KB
    # incorrectly declared ``transform.X`` / ``predict.X`` as consuming the
    # same interface input as ``fit.X``, so a single data edge fanned out
    # to both methods.  Fixing the KB (see ``interfaces.py``) made the
    # workaround unnecessary — this function now trusts the KB.

    method_io = get_method_io(interface)
    interface_inputs, interface_outputs = get_interface_io(interface)

    # 1. Create one Operator node per method in the chain.
    method_ids: Dict[str, str] = {}
    extra_method_ids: list[tuple[str, str]] = []  # (input_name, mid)
    new_nodes = {k: v for k, v in dag.nodes.items() if k != nid}
    for i, method_name in enumerate(methods):
        if method_name == "__init__":
            mid = f"{prefix}_init"
            new_nodes[mid] = Operator(name=node.name, language=node.language)
        else:
            mid = f"{prefix}_{method_name.replace('.', '_')}_{i}"
            new_nodes[mid] = Operator(name=method_name, language=node.language)
        method_ids[method_name] = mid

    # 1b. Canvas-style 2-X-input transformers (X_train / X_test).
    #
    # The interface may declare a second feature-flow input
    # (``X_test``) that no method in the chain consumes — Sklearn
    # Transformer chains only one ``transform`` call, but the canvas
    # node carries two X handles so users can wire X_test through.
    # For every "orphan" interface input (declared but no method
    # consumer by name), spawn a duplicate of the chain's terminal
    # method to consume it. The new method shares ``self`` with the
    # original terminal so both calls run on the same fitted
    # instance.
    if method_io and len(methods) >= 2:
        consumed_names = {
            inp.get("name", "")
            for _, (m_inputs, _) in method_io.items()
            for inp in m_inputs
            if inp.get("name")
        }
        # Build a set of external positions that actually have an
        # incoming data edge — only spawn a terminal duplicate when
        # the orphan interface input is *connected* to upstream
        # data. Otherwise the duplicate has no data edge to wire
        # against and the validator (and runtime) reject it with
        # ``transform_extra_X_test has no data input``. This is the
        # difference between a canvas pipeline that explicitly wires
        # X_test through every transformer and a FLAML-style
        # pipeline whose preprocessing only touches X (a single
        # transform call is enough).
        bound_positions: set = set()
        for de in data_edges:
            bound_positions.add(de.position)
            try:
                bound_positions.add(int(de.position))
            except (TypeError, ValueError):
                pass
        terminal_method = methods[-1]
        terminal_mid = method_ids[terminal_method]
        # When the chain ends in ``predict`` the original ``predict``
        # node receives the orphan ``X_test`` edge directly via the
        # routing override below — spawning a separate
        # ``predict_extra_X_test`` would leave both copies competing
        # for self with one of them ending up data-less, tripping
        # post-expansion validation. Estimator chains thus skip the
        # extra-method synthesis entirely.
        if terminal_method == "predict":
            pass
        else:
            for inp in interface_inputs:
                inp_name = inp.get("name", "")
                if not inp_name or inp_name in consumed_names:
                    continue
                inp_pos = inp.get("position")
                if inp_pos is None or inp_pos not in bound_positions:
                    continue
                # ``X_test`` (or any orphan feature-flow input) gets a
                # duplicate terminal method. The new node mirrors the
                # terminal's operator name so the resolver dispatches
                # ``instance.<method>(...)`` exactly the same way.
                extra_mid = f"{prefix}_{terminal_method.replace('.', '_')}_extra_{inp_name}"
                new_nodes[extra_mid] = Operator(
                    name=terminal_method, language=node.language,
                )
                extra_method_ids.append((inp_name, extra_mid))

    new_edges = [
        e for e in dag.edges if e.source != nid and e.destination != nid
    ]

    # 2. Chain methods linearly: each method's output 0 → next method's input 0
    #    (instance / self passing).
    for i in range(len(methods) - 1):
        src_mid = method_ids[methods[i]]
        dst_mid = method_ids[methods[i + 1]]
        new_edges.append(Edge(src_mid, dst_mid, position=0, output=0))

    # 2b. Wire ``self`` into each orphan-input terminal duplicate
    # from the original terminal's instance output (output=0). The
    # X_test consumer sees the same fitted instance as the X_train
    # transform.
    if extra_method_ids:
        terminal_mid = method_ids[methods[-1]]
        for _, extra_mid in extra_method_ids:
            new_edges.append(Edge(terminal_mid, extra_mid, position=0, output=0))

    # 3. Route data edges using KB method I/O declarations (name-based).
    if method_io:
        _route_data_edges_by_kb(
            data_edges, interface_inputs, method_io,
            method_ids, methods, new_edges,
            extra_method_ids=extra_method_ids,
        )
    else:
        # Fallback for interfaces without per-method I/O (shouldn't happen
        # if KB is complete — log a warning).
        emit(Event("CompoundExpansionFallback", {
            "source": "transforms._expand_compound_operator",
            "operator": node.name, "interface": interface,
            "reason": "No per-method I/O in KB — using introspection fallback",
        }))
        _route_data_edges_by_introspection(
            data_edges, node.name, methods, method_ids, new_edges,
        )

    # 4. Route parameter edges using KB method field.
    #
    # Contract: the KB requires every class-style interface to begin
    # its method chain with ``__init__`` (see ``interfaces.kb`` —
    # ``<Interface> calls <__init__-uuid>; <__init__-uuid> calls
    # <next>``). Constructor-style routing is the default sink for
    # any parameter whose KB ``method`` field is unset. If
    # ``__init__`` isn't in the method chain here, the operator's KB
    # declaration is broken — surface it surgically so the SPA can
    # mark THIS node failed (not every node in the run) and the
    # error names the offending operator instead of leaking a bare
    # ``KeyError('__init__')``. Don't paper over the KB issue with
    # ``methods[0]`` — that hides the real problem.
    if "__init__" not in method_ids:
        raise CompoundExpansionError(
            node_id=nid, operator=node.name,
            reason="kb_missing_init",
            detail=(
                f"interface '{interface}' method chain {methods!r} has no "
                f"``__init__`` anchor — fix the ``calls`` chain in the "
                f"KB source (interfaces.kb / sklearn.kb / llm.kb / "
                f"guardrails.kb) so the first method is ``__init__``."
            ),
        )
    init_method_id = method_ids["__init__"]
    for e in param_edges:
        param_node = dag.nodes.get(e.source)
        pname = param_node.name if isinstance(param_node, Parameter) else None
        target_method = param_method_map.get(pname) if pname else None
        if target_method and target_method in method_ids:
            new_edges.append(
                Edge(e.source, method_ids[target_method], position=e.position, output=e.output)
            )
        else:
            new_edges.append(
                Edge(e.source, init_method_id, position=e.position, output=e.output)
            )

    # 5. Rewire outgoing edges from the terminal method.
    _route_outgoing_edges(
        out_edges, method_io, method_ids, methods, new_edges,
        interface_outputs=interface_outputs,
        extra_method_ids=extra_method_ids,
    )

    return DAG(nodes=new_nodes, edges=new_edges)


# ---------------------------------------------------------------------------
# Data-edge routing helpers
# ---------------------------------------------------------------------------

def _route_data_edges_by_kb(
    data_edges: list[Edge],
    interface_inputs: list[dict],
    method_io: dict[str, tuple[list[dict], list[dict]]],
    method_ids: Dict[str, str],
    methods: list[str],
    new_edges: list[Edge],
    extra_method_ids: list[tuple[str, str]] | None = None,
) -> None:
    """Route external data edges to methods using per-method I/O from the KB.

    For each external data edge, match its position against the interface-level
    input declarations to find the input name, then look up which method
    consumes that input and at what internal position.

    ``extra_method_ids`` carries ``(interface_input_name, terminal_dup_id)``
    pairs for canvas-style 2-X-input transformers. When an external edge
    targets one of those orphan interface inputs, we route it to the
    terminal duplicate instead of the fallback collision-prone path.
    """
    # Map: external position → input name
    # Positions may arrive as strings from KB or ints from edges — normalise
    # to both forms so lookups succeed regardless of type.
    #
    # The canvas UI also uses descriptive handle aliases like ``X_train`` for
    # position 0's ``X`` slot (paired with ``X_test`` at position 1). Without
    # the alias, an edge wired ``train_test_split→imputer`` with
    # ``position='X_train'`` falls through to the catch-all fallback (which
    # routes everything to ``fit`` and starves ``transform``). Map the alias
    # to the canonical name so canvas pipelines bind the same way as
    # integer-position pipelines.
    ext_pos_to_name: Dict[int | str, str] = {}
    for inp in interface_inputs:
        pos = inp.get("position", 0)
        name = inp.get("name", "")
        ext_pos_to_name[pos] = name
        # Also store the int-coerced key so Edge.position (int) matches
        try:
            ext_pos_to_name[int(pos)] = name
        except (ValueError, TypeError):
            pass
        if name:
            ext_pos_to_name[name] = name
    # Canvas/KB convention: the canvas labels handles with a ``_train``
    # suffix on training-side inputs that the KB declares without the
    # suffix (``X``, ``y``). The corresponding ``_test`` slot is already
    # canonical (``X_test``). Add the suffix-stripped alias so a canvas
    # edge with ``position='X_train'`` resolves to ``X``.
    canonical = set(ext_pos_to_name.values())
    for canon in ("X", "y"):
        train_alias = f"{canon}_train"
        if canon in canonical and train_alias not in ext_pos_to_name:
            ext_pos_to_name[train_alias] = canon

    # Map: input name → (method_name, internal_position) for the FIRST method
    # that consumes it.  (An input may appear in multiple methods — e.g. X goes
    # to both fit and predict — so we collect all targets.)
    name_to_targets: Dict[str, list[tuple[str, int | str]]] = {}
    for method_name, (m_inputs, _) in method_io.items():
        for m_inp in m_inputs:
            inp_name = m_inp.get("name", "")
            int_pos = m_inp.get("position", 1)
            name_to_targets.setdefault(inp_name, []).append((method_name, int_pos))

    # Estimator-specific re-routing: when the chain ends in ``predict``
    # AND the interface declares an ``X_test`` orphan input, the
    # original ``predict`` node's internal X is conceptually the *test*
    # X — it should receive the ``X_test`` edge directly, NOT the
    # train-side X edge that ``fit`` also consumes. Without this
    # re-routing, ``name_to_targets["X"]`` fans the train-X edge into
    # both fit and predict (and another consumer of "X" through
    # ``X_test`` falling to the orphan-extra), causing fit to receive
    # five positional args at runtime instead of three.
    #
    # Transformer chains (terminal ``transform``/``fit_transform``)
    # keep all their X routing — they legitimately apply the same
    # train-X to every method in the chain.
    if methods and methods[-1] == "predict":
        x_test_in_interface = any(
            inp.get("name") == "X_test" for inp in interface_inputs
        )
        predict_x_pos = None
        if "predict" in method_io:
            for inp in method_io["predict"][0]:
                if inp.get("name") == "X":
                    predict_x_pos = inp.get("position", 1)
                    break
        if x_test_in_interface and predict_x_pos is not None:
            # Strip predict from the train-X target list.
            for name in list(name_to_targets.keys()):
                name_to_targets[name] = [
                    (m, p) for m, p in name_to_targets[name] if m != "predict"
                ]
            # Route the X_test edge directly into predict's internal
            # X port, replacing whatever orphan-extra path it would
            # have taken.
            name_to_targets.setdefault("X_test", []).append(
                ("predict", predict_x_pos),
            )

    # Index the terminal duplicates by orphan-input name for quick lookup.
    extra_by_name: Dict[str, str] = {
        nm: mid for nm, mid in (extra_method_ids or [])
    }

    for data_e in data_edges:
        inp_name = ext_pos_to_name.get(data_e.position)
        targets = name_to_targets.get(inp_name, []) if inp_name else []

        if targets:
            # Route to every method that consumes this input.
            for method_name, int_pos in targets:
                if method_name in method_ids:
                    new_edges.append(
                        Edge(data_e.source, method_ids[method_name],
                             position=int_pos, output=data_e.output)
                    )
        elif inp_name and inp_name in extra_by_name:
            # Orphan interface input — route to the terminal duplicate at
            # position 1 (the X slot; ``self`` is already wired at 0).
            new_edges.append(
                Edge(data_e.source, extra_by_name[inp_name],
                     position=1, output=data_e.output)
            )
        else:
            # Fallback: route to the first non-init method at its original position.
            fallback_mid = method_ids[methods[1]]
            new_edges.append(
                Edge(data_e.source, fallback_mid,
                     position=data_e.position, output=data_e.output)
            )


def _route_data_edges_by_introspection(
    data_edges: list[Edge],
    operator_name: str,
    methods: list[str],
    method_ids: Dict[str, str],
    new_edges: list[Edge],
) -> None:
    """Fallback routing when per-method I/O is not declared in the KB.

    Uses Python introspection (``_fit_arity``) to guess how many data edges
    the second method (fit) consumes; remaining edges go to the last method.
    """
    fit_method = methods[1]
    infer_method = methods[-1]
    fit_id = method_ids[fit_method]
    infer_mid = method_ids[infer_method]

    fit_n = _fit_arity(operator_name, fit_method)
    fit_data_edges = data_edges[:fit_n]
    infer_data_edges = data_edges[fit_n:] if fit_n < len(data_edges) else data_edges

    # Data → fit (positions 1, 2, …)
    for slot, e in enumerate(fit_data_edges, start=1):
        new_edges.append(Edge(e.source, fit_id, position=slot, output=e.output))

    # Data → infer (position 1 each)
    for data_e in infer_data_edges:
        new_edges.append(Edge(data_e.source, infer_mid, position=1, output=data_e.output))


def _route_outgoing_edges(
    out_edges: list[Edge],
    method_io: dict[str, tuple[list[dict], list[dict]]],
    method_ids: Dict[str, str],
    methods: list[str],
    new_edges: list[Edge],
    interface_outputs: list[dict] | None = None,
    extra_method_ids: list[tuple[str, str]] | None = None,
) -> None:
    """Rewire outgoing edges from the compound node to producing methods.

    Each outgoing edge's ``output`` field specifies an interface-level output
    port index.  Per-method I/O tells us which method produces that output.

    Method shortcuts return ``(instance, result)`` — output 0 is the instance
    (used for chain continuation) and output 1 is the method's actual return
    value.  Outgoing edges therefore use ``output=1`` to carry the data result
    to downstream consumers.

    ``extra_method_ids`` lets the caller redirect orphan interface outputs
    (declared on the interface but produced by no method in the chain — e.g.
    ``X_test_transformed`` for canvas-style 2-X-input transformers) to the
    terminal duplicate that consumes the matching orphan input.
    """
    terminal_mid = method_ids[methods[-1]]

    # Build a name → terminal-duplicate map for orphan-output redirection.
    # Convention: if the interface declares ``X_transformed`` and
    # ``X_test_transformed`` as outputs, the X_test duplicate produces the
    # second one. The pairing is positional — orphan inputs and orphan
    # outputs at the same chain index correspond.
    extra_output_by_pos: Dict[int, str] = {}
    if extra_method_ids and interface_outputs:
        # Outputs not produced by any chained method are owned by extras.
        produced_pos: set[int] = set()
        if method_io:
            pos = 0
            for method_name in methods:
                if method_name in method_io:
                    _, m_outputs = method_io[method_name]
                    for _ in m_outputs:
                        produced_pos.add(pos)
                        pos += 1
        # Orphan interface output positions, in declared order, take
        # ownership by terminal duplicates in the same order they were
        # spawned.
        orphan_positions: list[int] = []
        for o in interface_outputs:
            pos = o.get("position")
            if pos is None:
                continue
            try:
                pos_i = int(pos)
            except (TypeError, ValueError):
                continue
            if pos_i not in produced_pos:
                orphan_positions.append(pos_i)
        for pos_i, (_, mid) in zip(orphan_positions, extra_method_ids):
            extra_output_by_pos[pos_i] = mid

    if method_io:
        # Map interface-level output position → producing method node ID.
        # Outputs are assigned positions in method-chain order: the first
        # method with outputs gets position 0, 1, …; the next method
        # continues the sequence.
        output_pos_to_mid: Dict[int, str] = {}
        pos = 0
        for method_name in methods:
            if method_name in method_io:
                _, m_outputs = method_io[method_name]
                for _ in m_outputs:
                    output_pos_to_mid[pos] = method_ids[method_name]
                    pos += 1

        for e in out_edges:
            try:
                out_idx = int(e.output)
            except (TypeError, ValueError):
                out_idx = 0
            # Orphan output: route to the terminal duplicate that handles it.
            src_mid = extra_output_by_pos.get(
                out_idx,
                output_pos_to_mid.get(out_idx, terminal_mid),
            )
            # output=1: method result (port 0 is instance, port 1 is data)
            new_edges.append(
                Edge(src_mid, e.destination, position=e.position, output=1)
            )
    else:
        for e in out_edges:
            # output=1: method result for method shortcut nodes
            new_edges.append(
                Edge(terminal_mid, e.destination, position=e.position, output=1)
            )


COMPOUND_OPERATOR_EXPANSION_RULE = RewriteRule(
    pattern=DAG(nodes={"n": Node(type="Operator", text=r".*")}, edges=[]),
    description=(
        "expand class-interface operators into their internal method sub-DAG "
        "(e.g. StandardScaler → __init__ → fit → transform × N)"
    ),
    transformations=[Apply(f=_expand_compound_operator)],
)


def expand_compound_operators(pipeline: DAG, session: str) -> DAG:
    """Expand all class-interface operators before the Dask graph is built.

    ``Function``-interface operators (``train_test_split``, etc.) pass through
    unchanged.  Operators with no KB entry are skipped with a warning.
    No-op if the pipeline contains no class-interface operators.

    Called from ``run_pipeline`` after ``expand_dataset_refs`` so the resolver
    only ever sees concrete method nodes.

    Set ``DORIAN_USE_RUST_EXPAND_COMPOUND=1`` to route through the
    rust port for the common path (non-passthrough class operators
    with full per-method KB I/O). Passthrough interfaces, KB-seeding
    gaps, and introspection-fallback paths are then resolved by the
    python ``sync_apply`` pass on the rust output.
    """
    import os as _os
    if _os.environ.get(
        "DORIAN_USE_RUST_EXPAND_COMPOUND", ""
    ).lower() in ("1", "true", "yes", "on"):
        try:
            pipeline = _expand_compound_operators_rust(pipeline)
        except Exception as exc:  # noqa: BLE001
            try:
                emit(Event("ExpandCompoundRustFallback", {"error": str(exc)}))
            except Exception:
                pass
    return sync_apply(COMPOUND_OPERATOR_EXPANSION_RULE, pipeline, {"session": session})


def _expand_compound_operators_rust(pipeline: DAG) -> DAG:
    """Pre-resolve KB look-ups for every rust-eligible compound op
    and run the rust pure-mutation port on the batch.

    Eligibility (mirrors python guards 1+2 + the non-passthrough
    branch with KB method I/O):
      * is an Operator
      * not a method-shortcut (name in get_all_interface_methods()
        or no ``.``)
      * id does not contain ``_cx_``
      * has a KB interface
      * method sequence has ≥ 2 entries
      * not passthrough
      * KB method I/O is declared (``method_io`` non-empty)

    Operators failing any check are left in the graph for the python
    rule to handle on the second pass.
    """
    import json as _json
    import dorian_native  # type: ignore
    from dorian.knowledge.queries import (
        get_operator_interface,
        get_method_sequence,
        get_operator_parameters,
        get_interface_attributes,
        get_all_interface_methods,
        get_method_io,
    )

    iface_methods = get_all_interface_methods()
    records: list[dict] = []

    for nid, node in pipeline.nodes.items():
        if not isinstance(node, Operator):
            continue
        if node.name in iface_methods or "." not in node.name:
            continue
        if "_cx_" in nid:
            continue
        interface = get_operator_interface(node.name)
        if interface is None:
            continue
        methods_raw = get_method_sequence(interface)
        methods = list(dict.fromkeys(methods_raw))
        if len(methods) < 2:
            continue
        if "passthrough" in get_interface_attributes(interface):
            continue
        method_io = get_method_io(interface)
        if not method_io:
            continue

        from dorian.knowledge.queries import get_interface_io
        interface_inputs, _ = get_interface_io(interface)

        kb_params_raw = get_operator_parameters(node.name) or []
        kb_params = [
            (p["name"], p.get("method") or "__init__")
            for p in kb_params_raw
            if p.get("name")
        ]

        # Normalise KB positions to ints. KB returns strings.
        def _coerce_pos(p):
            try:
                return int(p)
            except (TypeError, ValueError):
                return 0

        records.append({
            "node_id": nid,
            "methods": methods,
            "kb_params": kb_params,
            "interface_inputs": [
                [inp.get("name", ""), _coerce_pos(inp.get("position", 0))]
                for inp in interface_inputs
            ],
            "method_io": [
                {
                    "method": method,
                    "inputs": [
                        [io.get("name", ""), _coerce_pos(io.get("position", 1))]
                        for io in inputs
                    ],
                    "outputs": [
                        [io.get("name", ""), _coerce_pos(io.get("position", 0))]
                        for io in outputs
                    ],
                }
                for method, (inputs, outputs) in method_io.items()
            ],
        })

    if not records:
        return pipeline

    expanded = dorian_native.expand_compound_operators(
        _json.dumps(pipeline.to_json_dict()),
        _json.dumps(records),
    )
    return DAG.from_json_dict(_json.loads(expanded))


def expand_dataset_refs(pipeline: DAG, session: str) -> DAG:
    """Expand any ``dorian.io.dataset`` nodes using the session's dataset meta.

    Resolves ``fpath`` and MIME type from Redis (sync client — safe in a
    Dask background thread), builds the ``meta`` dict, then delegates to
    ``sync_apply(DATASET_EXPANSION_RULE, ...)``.

    Async callers should build ``meta`` the same way and call::

        await apply(DATASET_EXPANSION_RULE, pipeline, meta)

    so both code paths reuse the same rule.
    """
    raw_meta = _sync_redis().get(RedisKeys.session_meta(session))
    if not raw_meta:
        return pipeline

    session_meta = json.loads(raw_meta)
    dataset = (session_meta.get("dataset") or {})
    fpath   = dataset.get("fpath") or ""
    loader  = _MIME_TO_LOADER.get(dataset.get("mime", ""), _DEFAULT_LOADER)

    if not fpath:
        return pipeline

    # Feature / target columns for the X/y split injected by the expansion.
    # Prefer dataset meta (synthetic RL sessions seed them there); fall back
    # to standalone Redis keys written by the upload flow; finally to None
    # (snippet uses "all-but-last" heuristic).
    features = dataset.get("features") or []
    target = dataset.get("target") or dataset.get("targets") or ""

    did = dataset.get("did") or ""
    if did and not features:
        try:
            raw = _sync_redis().get(RedisKeys.dataset_feature_columns(did))
            if raw:
                features = json.loads(raw)
        except Exception:
            pass
    if did and not target:
        try:
            raw = _sync_redis().get(RedisKeys.dataset_target_columns(did))
            if raw:
                loaded = json.loads(raw)
                target = loaded[0] if isinstance(loaded, list) and loaded else loaded
        except Exception:
            pass

    meta = {
        "fpath": fpath,
        "loader": loader,
        "features": features,
        "target": target,
    }

    # Rust expand path (task #72). Default off until soak'd in prod;
    # set ``DORIAN_USE_RUST_EXPAND_DATASET=1`` to opt in. Falls back
    # to the python sync_apply path on any rust error so a malformed
    # pipeline JSON can't take down the whole expansion chain.
    import os as _os
    if _os.environ.get("DORIAN_USE_RUST_EXPAND_DATASET", "").lower() in ("1", "true", "yes", "on"):
        try:
            import dorian_native  # type: ignore
            expanded_json = dorian_native.expand_dataset_refs(
                json.dumps(pipeline.to_json_dict()),
                json.dumps(meta),
            )
            return DAG.from_json_dict(json.loads(expanded_json))
        except Exception as exc:  # noqa: BLE001
            from backend.events import Event, emit
            try:
                emit(Event("ExpandDatasetRustFallback", {"error": str(exc)}))
            except Exception:
                pass

    return sync_apply(DATASET_EXPANSION_RULE, pipeline, meta)


# ---------------------------------------------------------------------------
# Categorical encoding injection
# ---------------------------------------------------------------------------

_ENCODING_OPS = frozenset({
    "sklearn.preprocessing.OrdinalEncoder",
    "sklearn.preprocessing.LabelEncoder",
    "sklearn.preprocessing.OneHotEncoder",
})


def _has_encoding_operator(dag: DAG) -> bool:
    """Check if the DAG already contains an encoding operator."""
    for node in dag.nodes.values():
        if isinstance(node, Operator) and node.name in _ENCODING_OPS:
            return True
    return False


def _needs_encoding(session: str) -> bool:
    """Check if the session's dataset has categorical features.

    Reads the profile from session meta (stored by check_data / DataProfiled).
    Returns True if NumberOfCategoricalFeatures > 0.
    """
    raw = _sync_redis().get(RedisKeys.session_meta(session))
    if not raw:
        return False
    meta = json.loads(raw)
    profile = (meta.get("dataset") or {}).get("profile", {})
    n_cat = profile.get("NumberOfCategoricalFeatures", 0)
    try:
        return n_cat is not None and float(n_cat) > 0
    except (TypeError, ValueError):
        return False


def _insert_encoder(
    dag: DAG, mapping: Dict[str, str], meta: Dict[str, Any]
) -> DAG:
    """Insert ``OrdinalEncoder`` upstream of ``train_test_split`` on the X path.

    The matched node is ``train_test_split`` (pattern node ``"n"``).
    We interpose an ``OrdinalEncoder`` operator between the X data source
    (position-0 incoming non-Parameter edge) and the split node.

    Guards
    ------
    * Returns the DAG unchanged if an encoding operator is already present.
    * Returns the DAG unchanged if the dataset has no categorical features
      (unless ``meta["force_encoding"]`` is set — used by the reactive handler
      when a ``MetafeatureError`` proves categoricals exist).
    * Returns the DAG unchanged if no suitable X edge is found.
    """
    nid = mapping["n"]

    if _has_encoding_operator(dag):
        return dag

    session = meta.get("session", "")
    if not meta.get("force_encoding") and not _needs_encoding(session):
        return dag

    # Find the X data edge (position 0 going into train_test_split, non-Parameter)
    x_edge = None
    for e in dag.edges:
        if (
            e.destination == nid
            and e.position == 0
            and not isinstance(dag.nodes.get(e.source), Parameter)
        ):
            x_edge = e
            break

    if x_edge is None:
        return dag

    encoder_id = f"encoder_{nid}"
    p_handle_id = f"p_handle_unknown_{nid}"
    p_unkval_id = f"p_unknown_value_{nid}"

    new_nodes = dict(dag.nodes)
    new_nodes[encoder_id] = Operator(
        name="sklearn.preprocessing.OrdinalEncoder", language="python"
    )
    new_nodes[p_handle_id] = Parameter(
        name="handle_unknown", dtype="str", value="use_encoded_value"
    )
    new_nodes[p_unkval_id] = Parameter(
        name="unknown_value", dtype="eval", value="-1"
    )

    new_edges = [e for e in dag.edges if e != x_edge]
    # X source → encoder (data in)
    new_edges.append(
        Edge(x_edge.source, encoder_id, position=0, output=x_edge.output)
    )
    # encoder → train_test_split (takes the place of the original X edge)
    new_edges.append(Edge(encoder_id, nid, position=0))
    # parameters → encoder
    new_edges.append(Edge(p_handle_id, encoder_id, position="handle_unknown"))
    new_edges.append(Edge(p_unkval_id, encoder_id, position="unknown_value"))

    emit(Event("OrdinalEncoderInserted", {"source": "transforms._insert_ordinal_encoder", "node": nid}))
    return DAG(nodes=new_nodes, edges=new_edges)


CATEGORICAL_ENCODING_RULE = RewriteRule(
    pattern=DAG(
        nodes={
            "n": Node(
                type="Operator",
                text=r"sklearn\.model_selection\.train_test_split",
            )
        },
        edges=[],
    ),
    description=(
        "insert OrdinalEncoder upstream of train_test_split "
        "when categorical features are detected in the dataset"
    ),
    transformations=[Apply(f=_insert_encoder)],
)


def expand_categorical_encoding(pipeline: DAG, session: str) -> DAG:
    """Insert ``OrdinalEncoder`` if the dataset has categorical features.

    Called from ``run_pipeline`` after ``expand_dataset_refs`` and **before**
    ``expand_compound_operators``, so the compound rule can subsequently
    expand the OrdinalEncoder into its ``__init__ → fit → transform`` sub-DAG.

    Set ``DORIAN_USE_RUST_EXPAND_CATEGORICAL=1`` to route through the
    rust port; the python ``sync_apply`` path stays as the fallback.
    """
    import os as _os
    if _os.environ.get(
        "DORIAN_USE_RUST_EXPAND_CATEGORICAL", ""
    ).lower() in ("1", "true", "yes", "on"):
        try:
            import json as _json
            import dorian_native  # type: ignore
            should_insert = _needs_encoding(session)
            expanded = dorian_native.expand_categorical_encoding(
                _json.dumps(pipeline.to_json_dict()),
                should_insert,
            )
            new_dag = DAG.from_json_dict(_json.loads(expanded))
            if should_insert:
                # Mirror the python rule's emit so observability stays
                # parity. The rust path inserts at most one encoder
                # across all train_test_split matches; emit once when
                # the encoder actually landed.
                if any(
                    isinstance(n, Operator)
                    and n.name == "sklearn.preprocessing.OrdinalEncoder"
                    for n in new_dag.nodes.values()
                ) and not _has_encoding_operator(pipeline):
                    emit(Event(
                        "OrdinalEncoderInserted",
                        {"source": "transforms.expand_categorical_encoding (rust)"},
                    ))
            return new_dag
        except Exception as exc:  # noqa: BLE001
            try:
                emit(Event("ExpandCategoricalRustFallback", {"error": str(exc)}))
            except Exception:
                pass

    return sync_apply(
        CATEGORICAL_ENCODING_RULE, pipeline, {"session": session}
    )
