"""
dorian/pipeline/dag_analysis.py
-------------------------------
DAG parsing and validation — pure functions with no side effects.

Extracted from ``execution.py`` to keep the execution module focused on
orchestration.  All function signatures are identical to the originals.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from dorian.dag import DAG, Edge, Group, IOMapping, Operator, Parameter, Snippet


# ---------------------------------------------------------------------------
# Pipeline deserialisation
# ---------------------------------------------------------------------------

_DTYPE_NORMALIZE = {"string": "str", "integer": "int", "boolean": "bool", "number": "float"}

# Operator names that are known single-word identifiers but are NOT parameter nodes.
# These are method shortcuts handled by operator_resolver, plus common builtins.
_KNOWN_OPERATOR_NAMES = frozenset({
    "fit", "predict", "transform", "fit_transform", "fit_predict",
    "score", "predict_proba", "predict_log_proba", "decision_function",
    "inverse_transform", "partial_fit",
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "sum", "min", "max", "abs", "round",
})


def _flatten_groups(dag: DAG) -> DAG:
    """Flatten all :class:`Group` nodes into their constituent sub-DAG.

    For each Group:
    1. Extract children as top-level DAG nodes (Operator/Snippet/Parameter)
    2. Add internal edges to the DAG's edge list
    3. Rewire external edges via the Group's ``io_map``
    4. Remove the Group node itself

    After flattening, only Operator, Snippet, and Parameter nodes remain.
    """
    from dorian.dag import _class_of  # noqa: deferred to avoid circular

    groups_to_flatten = [
        (nid, n) for nid, n in dag.nodes.items() if isinstance(n, Group)
    ]
    if not groups_to_flatten:
        return dag

    nodes = dict(dag.nodes)
    edges = list(dag.edges)

    for group_id, group in groups_to_flatten:
        # 1. Extract children into top-level nodes
        for child_id, child_dict in group.children.items():
            ct = child_dict.get("class_type", "")
            if ct == "Operator":
                nodes[child_id] = Operator.from_dict(child_dict)
            elif ct == "Parameter":
                nodes[child_id] = Parameter.from_dict(child_dict)
            elif ct == "Snippet":
                nodes[child_id] = Snippet.from_dict(child_dict)
            else:
                # Unknown child type — skip
                continue

        # 2. Add internal edges
        edges.extend(group.internal_edges)

        # 3. Rewire external edges using io_map
        new_edges = []
        for edge in edges:
            if edge.destination == group_id:
                handle_key = str(edge.position)
                mapping = group.io_map.get(handle_key)
                if mapping and mapping.direction == "input":
                    new_edges.append(Edge(
                        source=edge.source,
                        destination=mapping.internal_node_id,
                        position=mapping.internal_handle,
                        output=edge.output,
                    ))
                else:
                    # No mapping — try to pass through to init node
                    init_id = f"{group_id}_cx_init"
                    if init_id in nodes or init_id in group.children:
                        new_edges.append(Edge(
                            source=edge.source,
                            destination=init_id,
                            position=edge.position,
                            output=edge.output,
                        ))
                    # else: drop the edge (orphaned)
            elif edge.source == group_id:
                handle_key = str(edge.output)
                mapping = group.io_map.get(handle_key)
                if mapping and mapping.direction == "output":
                    new_edges.append(Edge(
                        source=mapping.internal_node_id,
                        destination=edge.destination,
                        position=edge.position,
                        output=mapping.internal_handle,
                    ))
                else:
                    # Fallback: route from last internal child (terminal method node).
                    # We cannot keep the original edge — the group node is about to
                    # be deleted, which would leave an orphaned source reference.
                    last_child_id = list(group.children.keys())[-1] if group.children else None
                    if last_child_id:
                        new_edges.append(Edge(
                            source=last_child_id,
                            destination=edge.destination,
                            position=edge.position,
                            output=0,
                        ))
                    # else: drop — empty group has no valid source
            else:
                new_edges.append(edge)
        edges = new_edges

        # 4. Remove the Group node
        del nodes[group_id]

    return DAG(nodes=nodes, edges=edges)


def _parse_pipeline(pipeline_data: dict, *, flatten_groups: bool = True) -> DAG:
    """Convert the pipeline dict stored in session meta into a DAG instance.

    Parameters
    ----------
    flatten_groups:
        When True (default), Group nodes are expanded into their constituent
        sub-DAGs.  Set to False for rewrite operations that should preserve
        the canvas-level structure (Groups remain as single nodes).
    """
    if not pipeline_data:
        raise ValueError("pipeline_data is empty or None")
    raw = pipeline_data.get("pipeline") or pipeline_data
    if isinstance(raw, str):
        raw = json.loads(raw)

    def _norm_dtype(raw_dtype: str) -> str:
        return _DTYPE_NORMALIZE.get(raw_dtype, raw_dtype)

    def _resolve_node_fields(nd: dict) -> tuple:
        """Return (node_type, name, dtype, value, code, language, tasks) from nd.

        Handles both the flat seeder/docstore format and nested React-Flow
        format where extra fields live under a "data" sub-dict.
        """
        sub = nd.get("data") if isinstance(nd.get("data"), dict) else {}

        # Type: top-level wins; fall back to class_type (docstore/RL format);
        # then sub-dict; normalise case variants.
        raw_type = (nd.get("type") or nd.get("class_type") or sub.get("type") or sub.get("backendType") or "").strip()
        if raw_type.lower() in {"parameter", "param"}:
            node_type = "Parameter"
        elif raw_type.lower() == "snippet":
            node_type = "Snippet"
        elif raw_type.lower() in {"operator", "visualizer", ""}:
            node_type = "Operator"
        else:
            node_type = raw_type  # preserve unexpected values for the heuristic

        name = nd.get("name") or sub.get("name") or nd.get("label") or sub.get("label") or ""

        # dtype: top-level wins; fall back to sub-dict "dtype"; then for
        # Parameter nodes also accept sub-dict "type" (the frontend stores
        # the param dtype, e.g. "env"/"eval", as data.type — not data.dtype).
        _dtype_raw = nd.get("dtype") or sub.get("dtype")
        if not _dtype_raw and node_type == "Parameter":
            _cand = sub.get("type", "")
            if _cand.lower() in {"int", "float", "string", "str", "bool", "eval", "env", "state", "list", "categorical"}:
                _dtype_raw = _cand
        dtype = _norm_dtype(_dtype_raw or "str")

        # Value: check flat field first, then React-Flow data sub-dict, then meta.
        value = nd.get("value")
        if value is None:
            value = sub.get("value")
        if value is None:
            meta_sub = nd.get("meta") if isinstance(nd.get("meta"), dict) else {}
            v = meta_sub.get("value")
            value = v if v is not None else meta_sub.get("default")

        code = nd.get("code") or sub.get("code") or "def foo(*a, **kw): pass"
        language = nd.get("language") or sub.get("language") or "python"
        tasks = nd.get("tasks") or sub.get("tasks") or []

        return node_type, name, dtype, value, code, language, tasks

    nodes: Dict[str, Any] = {}
    for nid, nd in (raw.get("nodes") or {}).items():
        # Detect Group nodes first (sent by backend via state/group-created)
        raw_type = (nd.get("type") or nd.get("class_type") or "").strip().lower()
        if raw_type == "group" or nd.get("class_type") == "Group":
            # Group nodes carry their full structure in the dict;
            # flatten data sub-dict if present (React-Flow nesting)
            group_data = nd.get("data", nd) if isinstance(nd.get("data"), dict) else nd
            nodes[nid] = Group.from_dict(group_data)
            continue

        node_type, name, dtype, value, code, language, tasks = _resolve_node_fields(nd)
        eff_name = name or nid

        if node_type == "Parameter":
            nodes[nid] = Parameter(
                name=eff_name,
                dtype=dtype,
                value=json.dumps(value) if isinstance(value, (dict, list)) else (str(value) if value is not None else ""),
            )
        elif node_type == "Snippet":
            nodes[nid] = Snippet(name=eff_name, code=code, language=language)
        else:
            # node_type is "Operator" or unrecognised frontend type.
            if value is not None:
                # Explicit value present → treat as Parameter regardless of type label.
                nodes[nid] = Parameter(name=eff_name, dtype=dtype, value=str(value))
            elif "." not in eff_name and eff_name not in _KNOWN_OPERATOR_NAMES:
                # Heuristic: simple identifier that is not a known operator/method →
                # almost certainly a hyperparameter node whose type was not preserved
                # by the frontend (e.g. React-Flow stores nodeType, not "Parameter").
                nodes[nid] = Parameter(name=eff_name, dtype=dtype, value="")
            else:
                nodes[nid] = Operator(name=eff_name, language=language, tasks=tasks)

    # Edge.__post_init__ normalises position/output types (str→int when
    # numeric, keyword strings preserved), so no manual coercion needed here.
    edges = [
        Edge(
            source=e["source"],
            destination=e["destination"],
            position=e.get("position", 0),
            output=e.get("output", 0),
        )
        for e in (raw.get("edges") or [])
    ]

    dag = DAG(nodes=nodes, edges=edges)

    # ---- Flatten Group nodes into their constituent sub-DAGs ----
    if flatten_groups:
        dag = _flatten_groups(dag)

    return dag


def _validate_pipeline(pipeline: DAG) -> List[str]:
    """Pre-flight checks on the parsed DAG.  Returns a list of error strings (empty = valid).

    Checks:
      1. **Dangling edges** — edges referencing node IDs that do not exist.
      2. **Cycle detection** — a DAG with cycles cannot be topologically sorted;
         Dask would deadlock.  Uses iterative DFS with colouring.
      3. **Method nodes missing data inputs** — after compound expansion,
         every ``fit`` / ``predict`` / ``transform`` node should have at
         least one non-chain data input (position > 0). A method node
         that has only its instance-chain edge and no data feed is a
         wiring bug that would otherwise manifest at execution time as
         ``method() missing 1 required positional argument: 'X'`` or
         ``missing ... 'y'``. Catching it here turns a silent runtime
         crash into an actionable pipeline-validation error.
    """
    errors: List[str] = []
    node_ids = set(pipeline.nodes.keys())

    # 1. Dangling edges
    for i, e in enumerate(pipeline.edges):
        if e.source not in node_ids:
            errors.append(f"Edge {i}: source '{e.source}' does not exist in nodes")
        if e.destination not in node_ids:
            errors.append(f"Edge {i}: destination '{e.destination}' does not exist in nodes")

    if errors:
        return errors  # cycle detection meaningless with broken edges

    # 2. Cycle detection (iterative DFS with WHITE/GRAY/BLACK colouring)
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {nid: WHITE for nid in node_ids}

    # Build adjacency list
    adj: Dict[str, List[str]] = {nid: [] for nid in node_ids}
    for e in pipeline.edges:
        adj[e.source].append(e.destination)

    for start in node_ids:
        if colour[start] != WHITE:
            continue
        stack = [(start, False)]  # (node, children_processed)
        while stack:
            node, processed = stack.pop()
            if processed:
                colour[node] = BLACK
                continue
            if colour[node] == GRAY:
                # Already visiting — push the finalisation marker
                colour[node] = BLACK
                continue
            colour[node] = GRAY
            stack.append((node, True))  # come back to finalise
            for child in adj[node]:
                if colour[child] == GRAY:
                    errors.append(f"Cycle detected involving node '{child}'")
                    return errors  # one cycle is enough to reject
                if colour[child] == WHITE:
                    stack.append((child, False))

    # 3. Method nodes missing required data inputs.
    #
    # Runs only against compound-expanded sub-DAGs (nodes whose id
    # carries the ``_cx_`` tag). Each method node has an instance chain
    # edge at position 0 from the previous method in the chain; the
    # actual payload (X, y, X_test, …) arrives at position >= 1. If a
    # fit / predict / transform / fit_transform node has zero non-chain
    # data edges, the method will raise "missing 1 required positional
    # argument" at execution. Flag it here.
    _DATA_METHODS = frozenset({
        "fit", "predict", "transform", "fit_transform",
        "predict_proba", "predict_log_proba", "decision_function",
    })
    for nid, node in pipeline.nodes.items():
        if "_cx_" not in nid:
            continue
        if not isinstance(node, Operator):
            continue
        if node.name not in _DATA_METHODS:
            continue
        # Collect incoming edges to this method node.
        data_edges = [
            e for e in pipeline.edges
            if e.destination == nid
            and not isinstance(pipeline.nodes.get(e.source), Parameter)
        ]
        # Chain edge is position 0. Data edges use position >= 1.
        non_chain = [
            e for e in data_edges
            if e.position not in (0, "0", None)
        ]
        if not non_chain:
            errors.append(
                f"Method node '{nid}' ({node.name!r}) has no data input — "
                f"this would raise \"{node.name}() missing required positional "
                f"argument\" at execution. Check upstream wiring."
            )

    return errors


def _sink_nodes(pipeline: DAG) -> List[str]:
    """Return node IDs that are never used as sources (terminal / output nodes)."""
    sources = {e.source for e in pipeline.edges}
    destinations = {e.destination for e in pipeline.edges}
    sinks = destinations - sources
    return list(sinks) if sinks else list(pipeline.nodes.keys())


# ---------------------------------------------------------------------------
# Shadow engine helpers (Phase 1.7)
# ---------------------------------------------------------------------------

def _node_to_shadow_dict(node) -> dict:
    """Convert a Python DAG node to a JSON-serialisable dict for the Rust engine.

    Serde discriminator is ``class_type`` with Pascal-case variant names,
    matching the ``#[serde(tag = "class_type")]`` tag on engine::graph::Node.
    """
    if isinstance(node, Operator):
        return {
            "class_type": "Operator",
            "name": node.name,
            "language": getattr(node, "language", "python"),
        }
    elif isinstance(node, Snippet):
        return {
            "class_type": "Snippet",
            "name": getattr(node, "name", "snippet"),
            "code": getattr(node, "code", ""),
            "language": getattr(node, "language", "python"),
        }
    elif isinstance(node, Parameter):
        return {
            "class_type": "Parameter",
            "name": node.name,
            "dtype": getattr(node, "dtype", "string"),
            "value": str(getattr(node, "value", "")),
        }
    elif isinstance(node, Group):
        return {"class_type": "Group", "name": getattr(node, "name", "group")}
    else:
        return {"class_type": "Operator", "name": str(node)}


def _compute_graph_depth(pipeline: DAG) -> int:
    """Compute the execution depth (number of levels) of the pipeline DAG.

    Uses the same level-assignment algorithm as the Rust engine:
    roots start at level 0, each node is max(predecessor levels) + 1.
    """
    if not pipeline.nodes:
        return 0

    # Build adjacency: dest → list of source nodes
    predecessors: Dict[str, List[str]] = {nid: [] for nid in pipeline.nodes}
    for e in pipeline.edges:
        if e.destination in predecessors and e.source in pipeline.nodes:
            predecessors[e.destination].append(e.source)

    levels: Dict[str, int] = {}
    visiting: set = set()  # cycle detection

    def _level(nid: str) -> int:
        if nid in levels:
            return levels[nid]
        if nid in visiting:
            # Cycle detected — treat as root to break infinite recursion.
            levels[nid] = 0
            return 0
        visiting.add(nid)
        preds = predecessors.get(nid, [])
        if not preds:
            levels[nid] = 0
        else:
            levels[nid] = max(_level(p) for p in preds) + 1
        visiting.discard(nid)
        return levels[nid]

    for nid in pipeline.nodes:
        _level(nid)

    return max(levels.values(), default=-1) + 1
