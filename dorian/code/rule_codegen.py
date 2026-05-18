"""
dorian/code/rule_codegen.py
----------------------------
JSON rule spec → Python ``RewriteRule(...)`` code generator.

Converts the JSON specification format (used by the LLM and the rule compiler)
into a human-readable Python source string that can be reviewed, edited, and
pasted into a rules file.

This is the inverse of ``dorian.mcp.rule_compiler.compile_rule`` — while
``compile_rule`` turns JSON into an executable ``RewriteRule`` object,
``json_spec_to_python`` turns JSON into the *source code* representation.
"""
from __future__ import annotations

import json
import logging
from typing import Any

_log = logging.getLogger(__name__)


def json_spec_to_python(spec: dict[str, Any]) -> str:
    """Convert a JSON rule specification into Python ``RewriteRule(...)`` source.

    Parameters
    ----------
    spec:
        A JSON rule spec as described in ``dorian.mcp.rule_compiler``.

    Returns
    -------
    str
        Python source code for a ``RewriteRule(...)`` expression.
    """
    lines: list[str] = []
    lines.append("RewriteRule(")

    # ── Description ──────────────────────────────────────────────────
    desc = spec.get("description", "LLM-generated rule")
    lines.append(f"    description={desc!r},")

    # ── Pattern ──────────────────────────────────────────────────────
    pattern_spec = spec.get("pattern", {})
    nodes_spec = pattern_spec.get("nodes", {})
    edges_spec = pattern_spec.get("edges", [])

    lines.append("    pattern=DAG(")
    lines.append("        nodes={")
    for nid, nspec in nodes_spec.items():
        ntype = nspec.get("type", ".*")
        ntext = nspec.get("text", ".*")
        nlang = nspec.get("language", "python")
        lang_str = "PYTHON" if nlang == "python" else repr(nlang)
        if ntext == ".*":
            lines.append(f"            {nid!r}: Node(type={ntype!r}, language={lang_str}),")
        else:
            lines.append(f"            {nid!r}: Node(type={ntype!r}, text={ntext!r}, language={lang_str}),")
    lines.append("        },")

    if edges_spec:
        lines.append("        edges=[")
        for e in edges_spec:
            src = e.get("source", "0")
            dst = e.get("destination", "1")
            lines.append(f"            Edge(source={src!r}, destination={dst!r}),")
        lines.append("        ],")
    else:
        lines.append("        edges=[],")

    lines.append("    ),")

    # ── Transformations ──────────────────────────────────────────────
    transformations_spec = spec.get("transformations", [])
    if transformations_spec:
        lines.append("    transformations=[")
        for tspec in transformations_spec:
            tf_code = _transformation_to_python(tspec)
            lines.append(f"        {tf_code},")
        lines.append("    ],")
    else:
        lines.append("    transformations=[],")

    lines.append(")")
    return "\n".join(lines)


def _transformation_to_python(tspec: dict[str, Any]) -> str:
    """Convert a single transformation spec dict to Python source."""
    ttype = tspec.get("type", "")

    if ttype == "delete":
        nodes = tspec.get("nodes", [])
        mode = tspec.get("mode", "isolated")
        if mode != "isolated":
            return f"Delete(nodes={nodes!r}, mode=PurgeMode.{mode})"
        return f"Delete(nodes={nodes!r})"

    if ttype == "update_attribute":
        target = tspec["target"]
        attribute = tspec["attribute"]
        value_spec = tspec["value"]
        value_code = _value_expr_to_python(value_spec)
        return (
            f"Apply(f=lambda g, m: _update(g, m[{target!r}], {attribute!r}, {value_code}))"
        )

    if ttype == "replace_operator":
        target = tspec["target"]
        new_name = tspec["new_name"]
        return (
            f"Apply(f=lambda g, m: DAG("
            f"nodes={{**g.nodes, m[{target!r}]: Operator(name={new_name!r}, "
            f"language=g.nodes[m[{target!r}]].language)}}, "
            f"edges=list(g.edges)))"
        )

    if ttype == "add_parameter":
        target = tspec["target"]
        param_name = tspec["param_name"]
        param_value = tspec.get("param_value", "")
        param_dtype = tspec.get("param_dtype", "eval")
        return (
            f"Add(nodes=[Parameter(name={param_name!r}, dtype={param_dtype!r}, "
            f"value={param_value!r})], "
            f"edges=[({param_name!r}, m[{target!r}])])"
        )

    if ttype == "insert_before":
        target = tspec["target"]
        new_operator = tspec["new_operator"]
        return (
            f"Apply(f=lambda g, m: insert_before(g, "
            f"g.nodes[m[{target!r}]].name, {new_operator!r}))"
        )

    if ttype == "insert_after":
        target = tspec["target"]
        new_operator = tspec["new_operator"]
        return (
            f"Apply(f=lambda g, m: insert_after(g, "
            f"g.nodes[m[{target!r}]].name, {new_operator!r}))"
        )

    if ttype in ("add_edges", "add_edge"):
        raw = tspec.get("edges") or []
        if not raw and "source" in tspec and "target" in tspec:
            raw = [[tspec["source"], tspec["target"]]]
        return f"Add(edges={[(e[0], e[1]) for e in raw]!r})"

    if ttype == "redirect_edge":
        from_node = tspec.get("from") or tspec.get("source", "")
        to_node = tspec.get("to") or tspec.get("target", "")
        position = tspec.get("position")
        pos_filter = f" and int(e.position) == {int(position)}" if position is not None else ""
        return (
            f"Apply(f=lambda g, m: DAG(nodes=g.nodes, edges=["
            f"Edge(m[{to_node!r}], e.destination, position=e.position, output=e.output) "
            f"if e.source == m[{from_node!r}]{pos_filter} else e "
            f"for e in g.edges]))"
        )

    if ttype == "to_operator":
        nid = tspec["nid"]
        content = tspec["content"]
        return f"ToOperator(nid={nid!r}, content={content!r})"

    if ttype == "to_parameter":
        nid = tspec["nid"]
        kw = tspec["kw"]
        value = tspec["value"]
        return f"ToParameter(nid={nid!r}, kw={kw!r}, value={value!r})"

    return f"# Unknown transformation type: {ttype!r}"


def _value_expr_to_python(value_spec: Any) -> str:
    """Convert a value expression to Python source code."""
    if isinstance(value_spec, str):
        return repr(value_spec)

    if isinstance(value_spec, dict):
        if "ref" in value_spec and "attr" in value_spec:
            ref = value_spec["ref"]
            attr = value_spec["attr"]
            return f"getattr(g.nodes[m[{ref!r}]], {attr!r}, '')"

        if "concat" in value_spec:
            parts = [_value_expr_to_python(p) for p in value_spec["concat"]]
            return " + ".join(parts)

    return repr(str(value_spec))
