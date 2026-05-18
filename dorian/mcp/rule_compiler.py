"""
dorian.mcp.rule_compiler
-------------------------
Compile JSON rule specifications into executable RewriteRule instances.

This module is the bridge between the LLM-friendly JSON format and the
internal ``RewriteRule`` + ``Apply`` system.  It replaces the previous
``eval()``-based approach with validated, structured compilation.

JSON Rule Spec Format
---------------------
::

    {
      "description": "...",
      "pattern": {
        "nodes": {
          "0": {"type": "comment", "text": ".*", "language": "python"},
          ...
        },
        "edges": [
          {"source": "0", "destination": "1"},
          ...
        ]
      },
      "transformations": [
        {"type": "delete", "nodes": ["0"]},
        {"type": "update_attribute", "target": "0", "attribute": "text",
         "value": "literal_value"},
        {"type": "update_attribute", "target": "0", "attribute": "text",
         "value": {"ref": "2", "attr": "type"}},
        {"type": "update_attribute", "target": "0", "attribute": "text",
         "value": {"concat": [{"ref": "0", "attr": "text"}, ".", {"ref": "1", "attr": "text"}]}},
        {"type": "replace_operator", "target": "0", "new_name": "sklearn.X.Y"},
        {"type": "add_parameter", "target": "0", "param_name": "k", "param_value": "v"},
        {"type": "insert_before", "target": "0", "new_operator": "sklearn.X.Y"},
        {"type": "insert_after", "target": "0", "new_operator": "sklearn.X.Y"},
      ]
    }

Value References
----------------
Instead of writing lambdas, agents use declarative value expressions:

- Literal string: ``"value": "identifier"``
- Reference:      ``"value": {"ref": "2", "attr": "type"}``
- Concatenation:  ``"value": {"concat": [{"ref": "0", "attr": "text"}, ".", {"ref": "1", "attr": "text"}]}``

These are resolved at match time by the compiled ``Apply`` function.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict
from typing import Any, Sequence
from uuid import uuid4

from dorian.dag import DAG, Edge, Node, Operator, Parameter, wildcard
from dorian.code.parsing.rule import (
    RewriteRule,
    Apply,
    Delete,
    Add,
    ToOperator,
    ToParameter,
    PurgeMode,
)

from dorian.mcp.rule_schema import MAX_CONCAT_DEPTH as _MAX_CONCAT_DEPTH

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Value reference resolution
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_value(value_spec: Any, dag: DAG, mapping: dict[str, str], *, _depth: int = 0) -> str:
    """Resolve a value expression against a matched DAG.

    Parameters
    ----------
    value_spec:
        One of:
        - ``str`` — literal value
        - ``{"ref": "0", "attr": "type"}`` — attribute of matched node
        - ``{"concat": [...]}`` — concatenation of resolved parts

    dag:
        The current DAG state.

    mapping:
        Pattern-key → DAG-node-ID mapping from ``match()``.

    Returns
    -------
    str
        The resolved value.
    """
    if isinstance(value_spec, str):
        return value_spec

    if isinstance(value_spec, dict):
        if "ref" in value_spec and "attr" in value_spec:
            node_id = mapping.get(value_spec["ref"])
            if node_id is None:
                return ""
            node = dag.nodes.get(node_id)
            if node is None:
                return ""
            return str(getattr(node, value_spec["attr"], ""))

        if "concat" in value_spec:
            if _depth >= _MAX_CONCAT_DEPTH:
                return "<concat depth exceeded>"
            parts = []
            for part in value_spec["concat"]:
                parts.append(_resolve_value(part, dag, mapping, _depth=_depth + 1))
            return "".join(parts)

    return str(value_spec)


# ═══════════════════════════════════════════════════════════════════════════
# Pattern compilation
# ═══════════════════════════════════════════════════════════════════════════

def _safe_regex(value: str) -> str:
    """Ensure a pattern value is safe for ``re.match()``.

    LLM-generated patterns often contain literal text with regex
    metacharacters (e.g. ``accuracy_score(y_test, y_pred)``).  The
    ``comparator()`` in ``dag.py`` uses ``re.match(pattern, text)``
    so bare parentheses, brackets, etc. break matching.

    Heuristic: if the value contains a backslash (explicit escape) or
    common regex syntax (``.*``, ``|``, ``^``, ``$``, ``+``, ``?``),
    treat it as an intentional regex.  Otherwise ``re.escape()`` it
    so literals match literally.
    """
    if not value or value == wildcard:
        return value
    # Contains intentional regex syntax → leave as-is
    if re.search(r"[\\|^$+?]|\.\*", value):
        return value
    # Contains metacharacters that need escaping → escape the whole thing
    if re.escape(value) != value:
        return re.escape(value)
    return value


def _compile_pattern(spec: dict) -> DAG:
    """Compile a pattern spec into a DAG for matching.

    Pattern nodes are always ``Node`` instances (type/text are regex patterns).
    """
    nodes_spec = spec.get("nodes", {})
    edges_spec = spec.get("edges", [])

    nodes: dict[str, Node] = {}
    for nid, nspec in nodes_spec.items():
        nodes[nid] = Node(
            type=_safe_regex(nspec.get("type", wildcard)),
            text=_safe_regex(nspec.get("text", wildcard)),
            language=nspec.get("language", wildcard),
        )

    edges = [
        Edge(source=e["source"], destination=e["destination"])
        for e in edges_spec
    ]

    return DAG(nodes=nodes, edges=edges)


# ═══════════════════════════════════════════════════════════════════════════
# Transformation compilation
# ═══════════════════════════════════════════════════════════════════════════

def _compile_transformation(tspec: dict, pattern_nodes: dict) -> Any:
    """Compile a single transformation spec into a Transformation object.

    Parameters
    ----------
    tspec:
        One transformation entry from the spec's ``transformations`` list.

    pattern_nodes:
        The pattern's node IDs (for validation).

    Returns
    -------
    Transformation instance (Delete, Apply, Add, ToOperator, ToParameter).

    Raises
    ------
    ValueError
        If the transformation type is unknown or references invalid nodes.
    """
    ttype = tspec.get("type", "")

    if ttype == "delete":
        nodes = tspec.get("nodes", [])
        edges = tspec.get("edges", [])
        mode = tspec.get("mode", PurgeMode.isolated)
        for nid in nodes:
            if nid not in pattern_nodes:
                raise ValueError(f"delete references node '{nid}' not in pattern")
        return Delete(
            nodes=list(nodes),
            edges=[(e[0], e[1]) for e in edges] if edges else [],
            mode=mode,
        )

    if ttype == "update_attribute":
        target = tspec["target"]
        attribute = tspec["attribute"]
        value_spec = tspec["value"]

        if target not in pattern_nodes:
            raise ValueError(f"update_attribute references node '{target}' not in pattern")

        # Validate refs in value_spec
        _validate_value_refs(value_spec, pattern_nodes)

        def _apply_update(dag: DAG, mapping: dict, meta: dict | None = None) -> DAG:
            node_id = mapping.get(target)
            if node_id is None or node_id not in dag.nodes:
                return dag
            resolved = _resolve_value(value_spec, dag, mapping)
            node = dag.nodes[node_id]
            new_node = Node(**dict(asdict(node), **{attribute: resolved}))
            new_nodes = dict(dag.nodes, **{node_id: new_node})
            return DAG(nodes=new_nodes, edges=dag.edges)

        return Apply(f=_apply_update)

    if ttype == "replace_operator":
        target = tspec["target"]
        new_name = tspec["new_name"]
        if target not in pattern_nodes:
            raise ValueError(f"replace_operator references node '{target}' not in pattern")

        def _apply_replace(dag: DAG, mapping: dict, meta: dict | None = None) -> DAG:
            node_id = mapping.get(target)
            if node_id is None or node_id not in dag.nodes:
                return dag
            old = dag.nodes[node_id]
            if not isinstance(old, Operator):
                return dag
            new_nodes = dict(dag.nodes)
            new_nodes[node_id] = Operator(name=new_name, language=old.language, tasks=old.tasks)
            return DAG(nodes=new_nodes, edges=list(dag.edges))

        return Apply(f=_apply_replace)

    if ttype == "add_parameter":
        target = tspec["target"]
        param_name = tspec["param_name"]
        param_value = tspec.get("param_value", "")
        param_dtype = tspec.get("param_dtype", "eval")
        if target not in pattern_nodes:
            raise ValueError(f"add_parameter references node '{target}' not in pattern")

        def _apply_add_param(dag: DAG, mapping: dict, meta: dict | None = None) -> DAG:
            node_id = mapping.get(target)
            if node_id is None or node_id not in dag.nodes:
                return dag
            pid = f"param_{param_name}_{uuid4().hex[:8]}"
            new_nodes = dict(dag.nodes)
            new_nodes[pid] = Parameter(name=param_name, dtype=param_dtype, value=param_value)
            new_edges = list(dag.edges)
            new_edges.append(Edge(pid, node_id, position=param_name))
            return DAG(nodes=new_nodes, edges=new_edges)

        return Apply(f=_apply_add_param)

    if ttype == "insert_before":
        target = tspec["target"]
        new_operator = tspec["new_operator"]
        if target not in pattern_nodes:
            raise ValueError(f"insert_before references node '{target}' not in pattern")

        def _apply_insert_before(dag: DAG, mapping: dict, meta: dict | None = None) -> DAG:
            from dorian.pipeline.mitigation_rewrites import insert_before as _ib
            node_id = mapping.get(target)
            if node_id is None or node_id not in dag.nodes:
                return dag
            node = dag.nodes[node_id]
            if not isinstance(node, Operator):
                return dag
            # Insert new operator before target: reroute non-Parameter incoming edges
            new_id = str(uuid4())
            new_nodes = dict(dag.nodes)
            new_nodes[new_id] = Operator(name=new_operator, language=node.language)
            incoming = [
                e for e in dag.edges
                if e.destination == node_id
                and not isinstance(dag.nodes.get(e.source), Parameter)
            ]
            kept = [e for e in dag.edges if e not in incoming]
            rerouted = [Edge(e.source, new_id, position=e.position, output=e.output) for e in incoming]
            kept.append(Edge(new_id, node_id))
            return DAG(nodes=new_nodes, edges=kept + rerouted)

        return Apply(f=_apply_insert_before)

    if ttype == "insert_after":
        target = tspec["target"]
        new_operator = tspec["new_operator"]
        if target not in pattern_nodes:
            raise ValueError(f"insert_after references node '{target}' not in pattern")

        def _apply_insert_after(dag: DAG, mapping: dict, meta: dict | None = None) -> DAG:
            from dorian.pipeline.mitigation_rewrites import insert_after as _ia
            node_id = mapping.get(target)
            if node_id is None or node_id not in dag.nodes:
                return dag
            node = dag.nodes[node_id]
            if not isinstance(node, Operator):
                return dag
            # Insert new operator after target: reroute outgoing edges
            new_id = str(uuid4())
            new_nodes = dict(dag.nodes)
            new_nodes[new_id] = Operator(name=new_operator, language=node.language)
            outgoing = [e for e in dag.edges if e.source == node_id]
            kept = [e for e in dag.edges if e not in outgoing]
            rerouted = [Edge(new_id, e.destination, position=e.position, output=0) for e in outgoing]
            kept.append(Edge(node_id, new_id))
            return DAG(nodes=new_nodes, edges=kept + rerouted)

        return Apply(f=_apply_insert_after)

    if ttype in ("add_edges", "add_edge"):
        # add_edge is a single-edge alias for add_edges
        raw = tspec.get("edges") or []
        if not raw and "source" in tspec and "target" in tspec:
            raw = [[tspec["source"], tspec["target"]]]
        return Add(edges=[(e[0], e[1]) for e in raw])

    if ttype == "redirect_edge":
        # Redirect outgoing edges from one pattern node to another
        from_node = tspec.get("from") or tspec.get("source", "")
        to_node = tspec.get("to") or tspec.get("target", "")
        position = tspec.get("position")
        if not from_node or not to_node:
            raise ValueError("redirect_edge requires 'from'/'source' and 'to'/'target'")
        if from_node not in pattern_nodes:
            raise ValueError(f"redirect_edge 'from' node '{from_node}' not in pattern")
        if to_node not in pattern_nodes:
            raise ValueError(f"redirect_edge 'to' node '{to_node}' not in pattern")

        def _apply_redirect_edge(dag: DAG, mapping: dict, meta: dict,
                                  _fn=from_node, _tn=to_node, _pos=position) -> DAG:
            src_id = mapping.get(_fn)
            dst_id = mapping.get(_tn)
            if src_id is None or dst_id is None:
                return dag
            new_edges = []
            for e in dag.edges:
                if e.source == src_id and (_pos is None or int(e.position) == int(_pos)):
                    new_edges.append(Edge(dst_id, e.destination, position=e.position, output=e.output))
                else:
                    new_edges.append(e)
            return DAG(nodes=dag.nodes, edges=new_edges)

        return Apply(f=_apply_redirect_edge)

    if ttype == "add_nodes":
        # For advanced use: add raw nodes
        raise ValueError("add_nodes not yet supported in JSON spec — use Apply for custom logic")

    if ttype == "to_operator":
        return ToOperator(nid=tspec["nid"], content=tspec["content"])

    if ttype == "to_parameter":
        return ToParameter(nid=tspec["nid"], kw=tspec["kw"], value=tspec["value"])

    raise ValueError(f"Unknown transformation type: {ttype!r}")


def _validate_value_refs(value_spec: Any, pattern_nodes: dict) -> None:
    """Validate that all node references in a value spec exist in the pattern."""
    if isinstance(value_spec, dict):
        if "ref" in value_spec:
            ref = value_spec["ref"]
            if ref not in pattern_nodes:
                raise ValueError(f"Value reference '{ref}' not in pattern nodes")
        if "concat" in value_spec:
            for part in value_spec["concat"]:
                _validate_value_refs(part, pattern_nodes)


# ═══════════════════════════════════════════════════════════════════════════
# Main compiler
# ═══════════════════════════════════════════════════════════════════════════

def compile_rule(spec: dict) -> tuple[RewriteRule | None, list[str], list[str]]:
    """Compile a JSON rule spec into a RewriteRule.

    Parameters
    ----------
    spec:
        The JSON rule specification (see module docstring for format).

    Returns
    -------
    (rule, errors, warnings)
        ``rule`` is the compiled RewriteRule (or None if compilation failed).
        ``errors`` is a list of fatal error messages.
        ``warnings`` is a list of non-fatal warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ── Validate top-level structure ──────────────────────────────────

    if "pattern" not in spec:
        errors.append("Missing 'pattern' in rule spec")
        return None, errors, warnings

    pattern_spec = spec["pattern"]
    if "nodes" not in pattern_spec or not pattern_spec["nodes"]:
        errors.append("Pattern must have at least one node")
        return None, errors, warnings

    transformations_spec = spec.get("transformations", [])
    if not transformations_spec:
        warnings.append("Rule has no transformations — it will match but do nothing")

    # ── Compile pattern ───────────────────────────────────────────────

    try:
        pattern = _compile_pattern(pattern_spec)
    except Exception as e:
        errors.append(f"Pattern compilation failed: {e}")
        return None, errors, warnings

    # ── Compile transformations ───────────────────────────────────────

    transformations = []
    pattern_node_ids = set(pattern_spec["nodes"].keys())

    for i, tspec in enumerate(transformations_spec):
        try:
            tf = _compile_transformation(tspec, pattern_node_ids)
            transformations.append(tf)
        except Exception as e:
            errors.append(f"Transformation [{i}] compilation failed: {e}")

    if errors:
        return None, errors, warnings

    # ── Assemble RewriteRule ──────────────────────────────────────────

    description = spec.get("description", "LLM-generated rule")

    rule = RewriteRule(
        pattern=pattern,
        description=description,
        transformations=transformations,
    )

    return rule, errors, warnings
