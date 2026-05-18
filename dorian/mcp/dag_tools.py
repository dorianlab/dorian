"""
dorian.mcp.dag_tools
---------------------
DAG inspection, diffing, validation, and dry-run rewrite tools.

These tools let LLM agents understand pipeline structure, compare before/after
states, and test rewrites without modifying the active pipeline.
"""
from __future__ import annotations

import json
from typing import Any

from dorian.dag import DAG, Edge, Node, Operator, Parameter, Snippet, match
from dorian.pipeline.transforms import sync_apply


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_dag(pipeline_json: str | dict) -> DAG:
    """Parse a pipeline JSON (string or dict) into a DAG object."""
    if isinstance(pipeline_json, str):
        pipeline_json = json.loads(pipeline_json)
    return DAG.from_json_dict(pipeline_json)


def _node_summary(nid: str, node: Any) -> dict:
    """Produce a human-readable summary of a node."""
    if isinstance(node, Operator):
        return {"id": nid, "kind": "Operator", "name": node.name, "language": node.language}
    if isinstance(node, Parameter):
        return {"id": nid, "kind": "Parameter", "name": node.name, "dtype": node.dtype, "value": node.value}
    if isinstance(node, Snippet):
        return {"id": nid, "kind": "Snippet", "name": node.name, "language": node.language}
    if isinstance(node, Node):
        return {"id": nid, "kind": "Node", "type": node.type, "text": node.text, "language": node.language}
    return {"id": nid, "kind": type(node).__name__, "repr": repr(node)}


def _edge_summary(edge: Edge) -> dict:
    """Produce a human-readable summary of an edge."""
    return {
        "source": edge.source,
        "destination": edge.destination,
        "position": edge.position,
        "output": edge.output,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tool implementations
# ═══════════════════════════════════════════════════════════════════════════

def dag_inspect(pipeline_json: str | dict) -> dict:
    """Parse and describe a pipeline DAG structure.

    Parameters
    ----------
    pipeline_json : str | dict
        The pipeline as a JSON string or dictionary (the format produced
        by ``DAG.to_json_dict()``).

    Returns
    -------
    dict
        A structured summary with node and edge details, statistics, and
        topological information.
    """
    try:
        dag = _parse_dag(pipeline_json)
    except Exception as e:
        return {"error": f"Failed to parse DAG: {e}"}

    nodes = [_node_summary(nid, node) for nid, node in dag.nodes.items()]
    edges = [_edge_summary(e) for e in dag.edges]

    # Compute basic statistics
    operators = [n for n in nodes if n["kind"] == "Operator"]
    parameters = [n for n in nodes if n["kind"] == "Parameter"]

    # Find root nodes (no incoming data edges)
    all_destinations = {e.destination for e in dag.edges}
    roots = [nid for nid in dag.nodes if nid not in all_destinations]

    # Find leaf nodes (no outgoing edges)
    all_sources = {e.source for e in dag.edges}
    leaves = [nid for nid in dag.nodes if nid not in all_sources]

    return {
        "node_count": len(dag.nodes),
        "edge_count": len(dag.edges),
        "operator_count": len(operators),
        "parameter_count": len(parameters),
        "root_nodes": roots,
        "leaf_nodes": leaves,
        "nodes": nodes,
        "edges": edges,
    }


def dag_diff(before_json: str | dict, after_json: str | dict) -> dict:
    """Diff two DAG states and return the structural changes.

    Parameters
    ----------
    before_json : str | dict
        The original pipeline DAG.
    after_json : str | dict
        The modified pipeline DAG.

    Returns
    -------
    dict
        ``{"added_nodes": [...], "removed_nodes": [...], "modified_nodes": [...],
          "added_edges": [...], "removed_edges": [...], "summary": "..."}``
    """
    try:
        before = _parse_dag(before_json)
        after = _parse_dag(after_json)
    except Exception as e:
        return {"error": f"Failed to parse DAG(s): {e}"}

    # Node diff
    before_ids = set(before.nodes.keys())
    after_ids = set(after.nodes.keys())

    added_ids = after_ids - before_ids
    removed_ids = before_ids - after_ids
    common_ids = before_ids & after_ids

    added_nodes = [_node_summary(nid, after.nodes[nid]) for nid in sorted(added_ids)]
    removed_nodes = [_node_summary(nid, before.nodes[nid]) for nid in sorted(removed_ids)]

    modified_nodes = []
    for nid in sorted(common_ids):
        b_node = before.nodes[nid]
        a_node = after.nodes[nid]
        if repr(b_node) != repr(a_node):
            modified_nodes.append({
                "id": nid,
                "before": _node_summary(nid, b_node),
                "after": _node_summary(nid, a_node),
            })

    # Edge diff
    before_edges = {(e.source, e.destination, e.position, e.output) for e in before.edges}
    after_edges = {(e.source, e.destination, e.position, e.output) for e in after.edges}

    added_edges = [
        {"source": s, "destination": d, "position": p, "output": o}
        for s, d, p, o in sorted(after_edges - before_edges)
    ]
    removed_edges = [
        {"source": s, "destination": d, "position": p, "output": o}
        for s, d, p, o in sorted(before_edges - after_edges)
    ]

    # Summary
    changes = []
    if added_nodes:
        changes.append(f"+{len(added_nodes)} nodes")
    if removed_nodes:
        changes.append(f"-{len(removed_nodes)} nodes")
    if modified_nodes:
        changes.append(f"~{len(modified_nodes)} modified nodes")
    if added_edges:
        changes.append(f"+{len(added_edges)} edges")
    if removed_edges:
        changes.append(f"-{len(removed_edges)} edges")

    summary = ", ".join(changes) if changes else "No changes"

    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "summary": summary,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Semantic diff — match nodes by name, not ID
# ═══════════════════════════════════════════════════════════════════════════


def _extract_operator_name(node_dict: dict) -> str | None:
    """Extract the canonical operator name from a node dict.

    Handles both clean ground-truth names (``sklearn.datasets.make_classification``)
    and messy auto-extracted names (``scaler.fit_transform.scaler.fit_transform``,
    or raw ``call`` nodes whose ``text`` contains the function name).
    """
    ct = node_dict.get("class_type", node_dict.get("type", ""))
    name = node_dict.get("name", "")

    if ct == "Operator" and name:
        # Auto-extracted method operators have doubled names like
        # "scaler.fit_transform.scaler.fit_transform" — take the last segment
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == parts[len(parts) // 2]:
            # Doubled: take second half
            name = ".".join(parts[len(parts) // 2:])
        return name

    # Raw Node (type=call) — try to extract the function name from text
    if ct in ("Node", "") and node_dict.get("type") == "call":
        text = node_dict.get("text", "")
        # e.g. "make_classification(    n_samples=500, ...)"
        # extract the function name before the first "("
        if "(" in text:
            func_name = text.split("(")[0].strip()
            return func_name

    return None


def _short_op_name(name: str) -> str:
    """Return the last segment of a dotted operator name for fuzzy matching.

    ``sklearn.datasets.make_classification`` → ``make_classification``
    ``make_classification`` → ``make_classification``
    """
    return name.rsplit(".", 1)[-1] if name else name


def _extract_param_key(
    node_id: str,
    node_dict: dict,
    edges: list[dict],
    node_lookup: dict[str, dict],
) -> tuple[str, str] | None:
    """Return ``(param_name, short_operator_name)`` for a parameter node.

    Uses the short (last segment) operator name so that
    ``(n_samples, make_classification)`` matches across DAGs regardless
    of whether the operator is fully qualified or not.
    """
    param_name = node_dict.get("name", "")
    if not param_name:
        return None

    # Find the operator this parameter connects to
    for edge in edges:
        if edge.get("source") == node_id:
            dest_id = edge.get("destination", "")
            dest_node = node_lookup.get(dest_id, {})
            op_name = _extract_operator_name(dest_node)
            if op_name:
                return (param_name, _short_op_name(op_name))

    return None


def semantic_dag_diff(
    auto_dag_json: str | dict,
    expected_dag_json: str | dict,
) -> dict:
    """Diff two DAGs by matching nodes semantically (by name) rather than by ID.

    Designed for comparing an auto-extracted DAG (numeric IDs, raw AST nodes)
    against a curated ground truth DAG (semantic IDs, clean operators).

    Parameters
    ----------
    auto_dag_json:
        The auto-extracted DAG (may contain raw Node objects).
    expected_dag_json:
        The curated ground truth DAG (clean Operators + Parameters).

    Returns
    -------
    dict with keys: missing_operators, extra_operators, extra_ast_nodes,
    missing_parameters, extra_parameters, missing_edges, extra_edges,
    matched_nodes, summary.
    """
    if isinstance(auto_dag_json, str):
        auto_dag_json = json.loads(auto_dag_json)
    if isinstance(expected_dag_json, str):
        expected_dag_json = json.loads(expected_dag_json)

    auto_nodes = auto_dag_json.get("nodes", {})
    auto_edges = auto_dag_json.get("edges", [])
    exp_nodes = expected_dag_json.get("nodes", {})
    exp_edges = expected_dag_json.get("edges", [])

    # ── Step 1: Build operator-name maps ──────────────────────
    # auto: name → [(node_id, node_dict), ...]
    auto_ops: dict[str, list[tuple[str, dict]]] = {}
    auto_other: list[tuple[str, dict]] = []  # non-operator, non-parameter nodes

    for nid, node in auto_nodes.items():
        ct = node.get("class_type", node.get("type", ""))
        if ct == "Parameter":
            continue  # handled separately
        op_name = _extract_operator_name(node)
        if op_name:
            auto_ops.setdefault(op_name, []).append((nid, node))
        else:
            auto_other.append((nid, node))

    exp_ops: dict[str, list[tuple[str, dict]]] = {}
    for nid, node in exp_nodes.items():
        ct = node.get("class_type", node.get("type", ""))
        if ct == "Parameter":
            continue
        op_name = _extract_operator_name(node)
        if op_name:
            exp_ops.setdefault(op_name, []).append((nid, node))

    # ── Step 2: Match operators ───────────────────────────────
    matched_nodes: dict[str, str] = {}  # exp_id → auto_id
    used_auto_ids: set[str] = set()

    # Exact name match first
    for name, exp_list in exp_ops.items():
        auto_list = auto_ops.get(name, [])
        for i, (exp_id, _) in enumerate(exp_list):
            if i < len(auto_list):
                auto_id, _ = auto_list[i]
                matched_nodes[exp_id] = auto_id
                used_auto_ids.add(auto_id)

    # Fuzzy match for remaining: check if expected name is a substring of auto
    # or vice versa (e.g., "fit_transform" in "scaler.fit_transform")
    unmatched_exp = [
        (name, exp_id, node)
        for name, entries in exp_ops.items()
        for exp_id, node in entries
        if exp_id not in matched_nodes
    ]
    unmatched_auto = [
        (name, auto_id, node)
        for name, entries in auto_ops.items()
        for auto_id, node in entries
        if auto_id not in used_auto_ids
    ]

    for exp_name, exp_id, _ in unmatched_exp:
        best_match = None
        for auto_name, auto_id, _ in unmatched_auto:
            if auto_id in used_auto_ids:
                continue
            # Check substring match in either direction
            if exp_name in auto_name or auto_name in exp_name:
                best_match = auto_id
                break
            # Check if the auto call text contains the expected name
            auto_node = auto_nodes.get(auto_id, {})
            text = auto_node.get("text", "")
            if exp_name.split(".")[-1] in text:
                best_match = auto_id
                break
        if best_match:
            matched_nodes[exp_id] = best_match
            used_auto_ids.add(best_match)

    # ── Step 3: Identify missing/extra operators ──────────────
    missing_operators = []
    for name, entries in exp_ops.items():
        for exp_id, node in entries:
            if exp_id not in matched_nodes:
                missing_operators.append({"name": name, "expected_id": exp_id})

    extra_operators = []
    for name, entries in auto_ops.items():
        for auto_id, node in entries:
            if auto_id not in used_auto_ids:
                extra_operators.append({
                    "id": auto_id,
                    "name": name,
                    "class_type": node.get("class_type", node.get("type", "?")),
                })

    # Extra AST nodes (raw Node objects that aren't operators/parameters)
    extra_ast_nodes = []
    for auto_id, node in auto_other:
        ntype = node.get("type", "?")
        text = (node.get("text", "") or "")[:80]
        extra_ast_nodes.append({"id": auto_id, "type": ntype, "text": text})

    # ── Step 4: Match parameters ──────────────────────────────
    auto_params: dict[tuple[str, str], tuple[str, dict]] = {}
    for nid, node in auto_nodes.items():
        ct = node.get("class_type", node.get("type", ""))
        if ct != "Parameter":
            continue
        key = _extract_param_key(nid, node, auto_edges, auto_nodes)
        if key:
            auto_params[key] = (nid, node)

    exp_params: dict[tuple[str, str], tuple[str, dict]] = {}
    for nid, node in exp_nodes.items():
        ct = node.get("class_type", node.get("type", ""))
        if ct != "Parameter":
            continue
        key = _extract_param_key(nid, node, exp_edges, exp_nodes)
        if key:
            exp_params[key] = (nid, node)

    # Match params and build mapping
    used_auto_param_ids: set[str] = set()
    for key, (exp_id, _) in exp_params.items():
        if key in auto_params:
            auto_id, _ = auto_params[key]
            matched_nodes[exp_id] = auto_id
            used_auto_param_ids.add(auto_id)

    missing_parameters = []
    for key, (exp_id, node) in exp_params.items():
        if exp_id not in matched_nodes:
            missing_parameters.append({
                "name": key[0],
                "operator": key[1],
            })

    extra_parameters = []
    for key, (auto_id, node) in auto_params.items():
        if auto_id not in used_auto_param_ids:
            extra_parameters.append({
                "id": auto_id,
                "name": key[0],
                "operator": key[1],
            })

    # ── Step 5: Compare edges via node mapping ────────────────
    # Two-pass approach:
    #   Pass 1: check edge existence by (src, dst) pair
    #   Pass 2: for matched edges, check position/output accuracy
    reverse_map = {v: k for k, v in matched_nodes.items()}

    def _name_for(nid: str, nodes: dict) -> str:
        node = nodes.get(nid, {})
        return _extract_operator_name(node) or node.get("name", nid)

    # Build expected edges as list of dicts with exp IDs
    exp_edge_list = [
        {
            "src": e["source"], "dst": e["destination"],
            "position": e.get("position"), "output": e.get("output"),
        }
        for e in exp_edges
    ]

    # Map auto edges to expected ID space
    auto_edge_list = []
    unmapped_auto_edges = []
    for e in auto_edges:
        src = e.get("source", "")
        dst = e.get("destination", "")
        mapped_src = reverse_map.get(src)
        mapped_dst = reverse_map.get(dst)
        if mapped_src and mapped_dst:
            auto_edge_list.append({
                "src": mapped_src, "dst": mapped_dst,
                "position": e.get("position"), "output": e.get("output"),
            })
        else:
            unmapped_auto_edges.append({
                "source": src,
                "destination": dst,
                "position": e.get("position"),
                "output": e.get("output"),
            })

    # Pass 1: match by (src, dst) pair
    exp_by_pair: dict[tuple, list[dict]] = {}
    for e in exp_edge_list:
        exp_by_pair.setdefault((e["src"], e["dst"]), []).append(e)

    auto_by_pair: dict[tuple, list[dict]] = {}
    for e in auto_edge_list:
        auto_by_pair.setdefault((e["src"], e["dst"]), []).append(e)

    all_pairs = set(exp_by_pair.keys()) | set(auto_by_pair.keys())

    missing_edges = []       # in expected, not in auto (by src,dst pair)
    extra_edges = []         # in auto, not in expected (by src,dst pair)
    wrong_metadata_edges = []  # same src,dst but different position/output

    for pair in all_pairs:
        exp_list = exp_by_pair.get(pair, [])
        auto_list = auto_by_pair.get(pair, [])
        src_name = _name_for(pair[0], exp_nodes)
        dst_name = _name_for(pair[1], exp_nodes)

        if not auto_list:
            # Edge exists in expected but not in auto
            for e in exp_list:
                missing_edges.append({
                    "from": src_name, "to": dst_name,
                    "position": e["position"], "output": e["output"],
                })
        elif not exp_list:
            # Edge exists in auto but not in expected
            for e in auto_list:
                extra_edges.append({
                    "from": src_name, "to": dst_name,
                    "position": e["position"], "output": e["output"],
                })
        else:
            # Both have this edge — check metadata
            # Match by index (for multi-edges between same pair)
            for i, exp_e in enumerate(exp_list):
                if i < len(auto_list):
                    auto_e = auto_list[i]
                    if (exp_e["position"] != auto_e["position"]
                            or exp_e["output"] != auto_e["output"]):
                        wrong_metadata_edges.append({
                            "from": src_name, "to": dst_name,
                            "expected_position": exp_e["position"],
                            "actual_position": auto_e["position"],
                            "expected_output": exp_e["output"],
                            "actual_output": auto_e["output"],
                        })
                else:
                    # More expected edges than auto for this pair
                    missing_edges.append({
                        "from": src_name, "to": dst_name,
                        "position": exp_e["position"], "output": exp_e["output"],
                    })
            # More auto edges than expected for this pair
            for j in range(len(exp_list), len(auto_list)):
                extra_edges.append({
                    "from": src_name, "to": dst_name,
                    "position": auto_list[j]["position"],
                    "output": auto_list[j]["output"],
                })

    # ── Summary ───────────────────────────────────────────────
    parts = []
    if missing_operators:
        parts.append(f"{len(missing_operators)} missing operators")
    if extra_operators:
        parts.append(f"{len(extra_operators)} extra operators")
    if extra_ast_nodes:
        parts.append(f"{len(extra_ast_nodes)} extra AST nodes")
    if missing_parameters:
        parts.append(f"{len(missing_parameters)} missing parameters")
    if extra_parameters:
        parts.append(f"{len(extra_parameters)} extra parameters")
    if missing_edges:
        parts.append(f"{len(missing_edges)} missing edges")
    if extra_edges:
        parts.append(f"{len(extra_edges)} extra edges")
    if unmapped_auto_edges:
        parts.append(f"{len(unmapped_auto_edges)} unmapped edges")

    return {
        "missing_operators": missing_operators,
        "extra_operators": extra_operators,
        "extra_ast_nodes": extra_ast_nodes,
        "missing_parameters": missing_parameters,
        "extra_parameters": extra_parameters,
        "missing_edges": missing_edges,
        "extra_edges": extra_edges,
        "wrong_metadata_edges": wrong_metadata_edges,
        "unmapped_auto_edges": unmapped_auto_edges,
        "matched_nodes": {k: v for k, v in matched_nodes.items()},
        "summary": ", ".join(parts) if parts else "No differences",
    }


def format_semantic_diff(diff: dict) -> str:
    """Format a ``semantic_dag_diff`` result as readable text for the LLM prompt."""
    if diff.get("summary") == "No differences":
        return ""

    lines = ["## Extraction Errors (compared to expected pipeline)"]

    if diff.get("missing_operators"):
        names = ", ".join(op["name"] for op in diff["missing_operators"])
        lines.append(f"Missing operators: {names}")

    if diff.get("extra_operators"):
        entries = [f"{op['name']} [{op['id']}]" for op in diff["extra_operators"]]
        lines.append(f"Extra operators (should not exist): {', '.join(entries)}")

    if diff.get("extra_ast_nodes"):
        entries = [
            f'{n["type"]} "{n["text"]}"' for n in diff["extra_ast_nodes"]
        ]
        lines.append(f"Extra AST nodes (should be deleted): {', '.join(entries)}")

    if diff.get("missing_parameters"):
        entries = [f'{p["name"]} (on {p["operator"]})' for p in diff["missing_parameters"]]
        lines.append(f"Missing parameters: {', '.join(entries)}")

    if diff.get("extra_parameters"):
        entries = [f'{p["name"]} (on {p["operator"]}) [{p["id"]}]' for p in diff["extra_parameters"]]
        lines.append(f"Extra parameters: {', '.join(entries)}")

    if diff.get("missing_edges"):
        entries = [
            f'{e["from"]} → {e["to"]} (pos={e["position"]}, out={e["output"]})'
            for e in diff["missing_edges"]
        ]
        lines.append(f"Missing edges: {', '.join(entries)}")

    if diff.get("extra_edges"):
        entries = [
            f'{e["from"]} → {e["to"]} (pos={e["position"]}, out={e["output"]})'
            for e in diff["extra_edges"]
        ]
        lines.append(f"Extra edges: {', '.join(entries)}")

    lines.append(f"Summary: {diff['summary']}")
    return "\n".join(lines)


def semantic_diff_score(diff: dict) -> int:
    """Weighted error score from a ``semantic_dag_diff`` result.

    Lower is better (0 = perfect match). Weights reflect severity:
    operators matter most, edges least. Position/output metadata is
    intentionally ignored — revisit once the gate is proven useful.
    """
    return (
        len(diff.get("missing_operators", [])) * 10
        + len(diff.get("extra_operators", [])) * 10
        + len(diff.get("extra_ast_nodes", [])) * 3
        + len(diff.get("missing_parameters", [])) * 5
        + len(diff.get("extra_parameters", [])) * 5
        + len(diff.get("missing_edges", [])) * 2
        + len(diff.get("extra_edges", [])) * 2
    )


def graph_edit_distance(
    before_json: str | dict, after_json: str | dict, *, beam_limit: int = 50_000
) -> int | None:
    """Graph edit distance between two pipeline DAGs.

    Prefers the Rust-native A* implementation (``dorian_native.graph_edit_distance``)
    and falls back to ``fast_distance`` (operator-set symmetric difference) if the
    native module is unavailable. Returns ``None`` only if both paths fail.

    Lower is better; used as a similarity / convergence signal in the LLM
    rule-suggestion self-correction loop, and as a secondary gate alongside
    :func:`semantic_diff_score`.
    """
    try:
        import dorian_native  # type: ignore
    except ImportError:
        dorian_native = None  # type: ignore

    def _as_json_str(x: str | dict) -> str:
        if isinstance(x, str):
            return x
        import json as _json
        return _json.dumps(x)

    if dorian_native is not None:
        try:
            return int(
                dorian_native.graph_edit_distance(
                    _as_json_str(before_json),
                    _as_json_str(after_json),
                    beam_limit,
                )
            )
        except Exception:
            try:
                return int(
                    dorian_native.fast_distance(
                        _as_json_str(before_json), _as_json_str(after_json)
                    )
                )
            except Exception:
                pass

    # Python fallback — operator-set symmetric difference
    def _operators(dag_src: str | dict) -> set[str]:
        if isinstance(dag_src, str):
            import json as _json
            dag_src = _json.loads(dag_src)
        names: set[str] = set()
        for nid, node in (dag_src.get("nodes") or {}).items():
            t = (node or {}).get("type")
            if t in ("Operator", "Snippet") and node.get("text"):
                names.add(node["text"])
        return names

    try:
        a, b = _operators(before_json), _operators(after_json)
        return len(a ^ b)
    except Exception:
        return None


def graph_edit_path(
    before_json: str | dict, after_json: str | dict, *, max_ops: int = 200
) -> dict:
    """Minimum-signal edit path turning ``before`` into ``after``.

    Returns ``{ops: [...], truncated: bool, strategy: str}``.

    Prefers ``dorian_native.graph_edit_path`` (Rust, O(|V|+|E|) structural
    diff) when available; falls back to the in-module Python implementation
    otherwise. The NP-hard exact A*-with-path-reconstruction is a
    follow-up (see (internal design note; not in public repo) §7); the structural
    diff path covers the common case where user edits preserve node IDs.
    """
    # Try the native fast path first — same JSON contract on both sides.
    try:
        import dorian_native  # type: ignore
        import json as _json

        def _as_str(x: str | dict) -> str:
            return x if isinstance(x, str) else _json.dumps(x)

        raw = dorian_native.graph_edit_path(
            _as_str(before_json), _as_str(after_json), max_ops,
        )
        return _json.loads(raw)
    except (ImportError, AttributeError):
        pass
    except Exception:
        # Any native failure (parse, internal panic) falls through to Python.
        pass

    def _parse(x: str | dict) -> dict:
        if isinstance(x, str):
            import json as _json
            return _json.loads(x)
        return x

    g1 = _parse(before_json) or {}
    g2 = _parse(after_json) or {}
    n1 = g1.get("nodes") or {}
    n2 = g2.get("nodes") or {}
    e1 = g1.get("edges") or []
    e2 = g2.get("edges") or []

    # Strategy selection: shared IDs ⇒ structural diff
    shared = set(n1) & set(n2)
    both_nonempty = bool(n1) and bool(n2)
    use_id_diff = both_nonempty and (len(shared) / max(len(n1), len(n2)) >= 0.25)

    ops: list[dict] = []

    def _attr(n: dict, k: str) -> str | None:
        return n.get(k) if n else None

    def _edge_key(e: dict) -> tuple:
        return (
            str(e.get("source", "")),
            str(e.get("destination", "")),
            str(e.get("position", "")),
            int(e.get("output", 0) or 0),
        )

    def _push(op: dict):
        if len(ops) < max_ops:
            ops.append(op)

    if use_id_diff:
        for nid in sorted(set(n1) - set(n2)):
            _push({"kind": "DeleteNode", "id": nid})
        for nid in sorted(set(n2) - set(n1)):
            node = n2[nid]
            _push({
                "kind": "InsertNode",
                "id": nid,
                "type": _attr(node, "type"),
                "text": _attr(node, "text"),
                "language": _attr(node, "language"),
            })
        for nid in sorted(shared):
            a, b = n1[nid], n2[nid]
            diffs = {k: _attr(b, k) for k in ("type", "text", "language")
                     if _attr(a, k) != _attr(b, k)}
            if diffs:
                _push({"kind": "RenameNode", "id": nid, **diffs})

        keys1 = {_edge_key(e): e for e in e1}
        keys2 = {_edge_key(e): e for e in e2}
        for k in sorted(keys1.keys() - keys2.keys()):
            s, d, p, o = k
            _push({"kind": "DeleteEdge", "source": s, "destination": d,
                   "position": p, "output": o})
        for k in sorted(keys2.keys() - keys1.keys()):
            s, d, p, o = k
            _push({"kind": "InsertEdge", "source": s, "destination": d,
                   "position": p, "output": o})

        strategy = "id_diff"
    else:
        def _fp(node: dict) -> tuple:
            return (_attr(node, "type"), _attr(node, "text"), _attr(node, "language"))

        fps1: dict[tuple, list[str]] = {}
        fps2: dict[tuple, list[str]] = {}
        for nid, nd in n1.items():
            fps1.setdefault(_fp(nd), []).append(nid)
        for nid, nd in n2.items():
            fps2.setdefault(_fp(nd), []).append(nid)

        for fp, ids in fps1.items():
            extra = max(0, len(ids) - len(fps2.get(fp, [])))
            for nid in ids[:extra]:
                _push({"kind": "DeleteNode", "id": nid})
        for fp, ids in fps2.items():
            extra = max(0, len(ids) - len(fps1.get(fp, [])))
            for nid in ids[:extra]:
                t, text, lang = fp
                _push({"kind": "InsertNode", "id": nid,
                       "type": t, "text": text, "language": lang})

        # Edges: fall through to endpoint-attribute-matched diff would be
        # expensive; report edge-count delta as a single summary op so the
        # LLM sees "K edges need structural review" without pretending to
        # enumerate them one-for-one without IDs.
        de = len(e2) - len(e1)
        if de:
            _push({"kind": "EdgeDelta", "count": de})

        strategy = "name_diff"

    truncated = len(ops) >= max_ops
    return {"ops": ops, "truncated": truncated, "strategy": strategy}


def dag_validate(pipeline_json: str | dict) -> dict:
    """Validate a pipeline DAG structure for common issues.

    Parameters
    ----------
    pipeline_json : str | dict
        The pipeline DAG to validate.

    Returns
    -------
    dict
        ``{"valid": bool, "errors": [...], "warnings": [...]}``
    """
    try:
        dag = _parse_dag(pipeline_json)
    except Exception as e:
        return {"valid": False, "errors": [f"Failed to parse DAG: {e}"], "warnings": []}

    errors: list[str] = []
    warnings: list[str] = []

    # Check: no empty DAG
    if not dag.nodes:
        errors.append("DAG has no nodes")
        return {"valid": False, "errors": errors, "warnings": warnings}

    # Check: edges reference valid nodes
    node_ids = set(dag.nodes.keys())
    for i, edge in enumerate(dag.edges):
        if edge.source not in node_ids:
            errors.append(f"Edge[{i}] source '{edge.source}' not in nodes")
        if edge.destination not in node_ids:
            errors.append(f"Edge[{i}] destination '{edge.destination}' not in nodes")

    # Check: no self-loops
    for i, edge in enumerate(dag.edges):
        if edge.source == edge.destination:
            errors.append(f"Edge[{i}] is a self-loop on '{edge.source}'")

    # Check: Parameters should have outgoing edges (to an operator)
    for nid, node in dag.nodes.items():
        if isinstance(node, Parameter):
            has_outgoing = any(e.source == nid for e in dag.edges)
            if not has_outgoing:
                warnings.append(f"Parameter '{nid}' ({node.name}) has no outgoing edge — orphaned?")

    # Check: Operators should have at least one connection
    for nid, node in dag.nodes.items():
        if isinstance(node, Operator):
            has_edge = any(e.source == nid or e.destination == nid for e in dag.edges)
            if not has_edge:
                warnings.append(f"Operator '{nid}' ({node.name}) is disconnected")

    # Check: duplicate edges
    edge_tuples = [(e.source, e.destination, e.position, e.output) for e in dag.edges]
    seen = set()
    for et in edge_tuples:
        if et in seen:
            warnings.append(f"Duplicate edge: {et[0]} → {et[1]}")
        seen.add(et)

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "node_count": len(dag.nodes),
        "edge_count": len(dag.edges),
    }


def dag_apply_rewrite(
    pipeline_json: str | dict,
    rule_spec: dict | None = None,
    compiled_rule: Any = None,
) -> dict:
    """Dry-run a rewrite rule on a DAG and return the result.

    Parameters
    ----------
    pipeline_json : str | dict
        The pipeline DAG to transform.
    rule_spec : dict, optional
        A JSON rule spec to compile and apply.
    compiled_rule : RewriteRule, optional
        An already-compiled RewriteRule to apply directly.

    Returns
    -------
    dict
        ``{"success": bool, "before": {...}, "after": {...}, "diff": {...}, "error": "..."}``
    """
    from dorian.mcp.rule_compiler import compile_rule

    try:
        dag = _parse_dag(pipeline_json)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse DAG: {e}"}

    # Compile rule if spec provided
    if rule_spec is not None and compiled_rule is None:
        rule, errors, warnings = compile_rule(rule_spec)
        if rule is None:
            return {"success": False, "error": f"Rule compilation failed: {errors}"}
        compiled_rule = rule

    if compiled_rule is None:
        return {"success": False, "error": "No rule provided (pass rule_spec or compiled_rule)"}

    # Apply the rule
    try:
        result_dag = sync_apply(compiled_rule, dag, {})
    except Exception as e:
        return {"success": False, "error": f"Rule application failed: {e}"}

    before_dict = dag.to_json_dict()
    after_dict = result_dag.to_json_dict()

    # Compute diff
    diff = dag_diff(before_dict, after_dict)

    return {
        "success": True,
        "before": dag_inspect(before_dict),
        "after": dag_inspect(after_dict),
        "diff": diff,
        "pipeline_json": after_dict,
    }


def dag_match_pattern(pipeline_json: str | dict, pattern_json: dict) -> dict:
    """Test whether a pattern matches anywhere in a DAG.

    Parameters
    ----------
    pipeline_json : str | dict
        The pipeline DAG to search.
    pattern_json : dict
        A pattern spec (same format as the ``pattern`` field in a rule spec).

    Returns
    -------
    dict
        ``{"matched": bool, "mapping": {"pattern_id": "dag_id", ...} | null}``
    """
    from dorian.mcp.rule_compiler import _compile_pattern

    try:
        dag = _parse_dag(pipeline_json)
        pattern = _compile_pattern(pattern_json)
    except Exception as e:
        return {"matched": False, "mapping": None, "error": str(e)}

    is_matched, candidate = match(pattern, dag)

    if is_matched and candidate:
        # Enrich mapping with node details
        enriched = {}
        for pat_id, dag_id in candidate.items():
            enriched[pat_id] = {
                "dag_node_id": dag_id,
                "node": _node_summary(dag_id, dag.nodes.get(dag_id)),
            }
        return {"matched": True, "mapping": enriched}

    return {"matched": False, "mapping": None}
