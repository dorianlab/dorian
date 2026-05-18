"""
dorian/code/parsing/parser.py
------------------------------
AST-to-DAG parsing engine.  Converts source code to a tree-sitter AST,
then transforms the AST into an executable dataflow graph using rewrite rules.
"""
import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Tree
from typing import Sequence, Dict, Any, List
from functools import reduce
from classes import typeclass
from itertools import product, combinations
from collections import deque

import asyncio
from uuid import uuid4
from pathlib import Path

from dorian.dag import (
    DAG,
    Node,
    Edge,
    Operator,
    Parameter,
    match,
    _class_of,
    to_dag,
)
import dorian.dag as ast
from dorian.languages import SupportedLanguage

from dorian.code.parsing.rule import (
    RewriteRule,
    Transformation,
    Add,
    Apply,
    Replace,
    Delete,
    Revert,
    ToOperator,
    ToParameter,
    PurgeMode,
)
from dorian.dag import ID
from dorian.code.parsing.rules import get_rules, Rules
from dorian.code.parsing.debugging import profiling


# ---------------------------------------------------------------------------
# Parser creation
# ---------------------------------------------------------------------------

def create_parser(language: str) -> Parser:
    """Create a tree-sitter parser for the given *language*."""
    langs = {"python": tspython.language()}
    lang = langs.get(language, None)
    if lang is None:
        raise ValueError(f"Language '{language}' is not supported.")
    parser = Parser(language=Language(lang))
    return parser


# ---------------------------------------------------------------------------
# Rewriting engine
# ---------------------------------------------------------------------------

def _handle_deleted_nodes(dag: DAG, nodes: List[ID]) -> DAG:
    """Delete nodes, rewiring their parents to their children."""
    to_remove: list = []
    to_add: list[Edge] = []
    for nid in nodes:
        sources = [e.source for e in dag.edges if e.destination == nid]
        destinations = [e.destination for e in dag.edges if e.source == nid]
        if sources:
            to_remove.extend([(s, nid) for s in sources])
        if destinations:
            to_remove.extend([(nid, d) for d in destinations])
        if sources and destinations:
            to_add.extend(
                [
                    ast.Edge(source=s, destination=d)
                    for s, d in product(sources, destinations)
                ]
            )
    return ast.DAG(
        nodes=dict((k, v) for k, v in dag.nodes.items() if k not in nodes),
        edges=[
            e for e in dag.edges + to_add if (e.source, e.destination) not in to_remove
        ],
    )


def _rewrite(
    dag: ast.DAG, mapping: Dict[ID, ID], transformation: Transformation
) -> ast.DAG:
    """Apply a single transformation to *dag* using *mapping*."""
    match transformation:
        case Add():
            _nodes = {}
            if transformation.nodes:
                new_ids = [str(uuid4()) for _ in range(len(transformation.nodes))]
                _nodes = dict(zip(new_ids, transformation.nodes))
            _edges = (
                [
                    ast.Edge(
                        source=mapping.get(e[0], e[0]),
                        destination=mapping.get(e[1], e[1]),
                    )
                    for e in transformation.edges
                ]
                if transformation.edges
                else []
            )
            return ast.DAG(nodes=dict(dag.nodes, **_nodes), edges=dag.edges + _edges)
        case Apply():
            return transformation.f(dag, mapping)
        case Replace():
            return ast.DAG(nodes={}, edges=[])
        case Delete():
            mapped_nodes = (
                list(map(lambda x: mapping[x], transformation.nodes))
                if transformation.nodes
                else []
            )
            if mapped_nodes:
                match transformation.mode:
                    case PurgeMode.recursive:
                        queue = mapped_nodes[:]
                        while queue:
                            nid = queue.pop()
                            added = [
                                e.destination for e in dag.edges if e.source == nid
                            ]
                            mapped_nodes.extend(added)
                            queue.extend(added)
                    case PurgeMode.isolated:
                        dag = _handle_deleted_nodes(dag, mapped_nodes)
            _nodes = dict((k, v) for k, v in dag.nodes.items() if k not in mapped_nodes)
            foo = lambda x: (mapping[x[0]], mapping[x[1]])
            to_remove = (
                list(map(foo, transformation.edges)) if transformation.edges else []
            )
            _edges = [
                e
                for e in dag.edges
                if ((e.source, e.destination) not in to_remove)
                and (e.source not in mapped_nodes)
                and (e.destination not in mapped_nodes)
            ]
            return DAG(nodes=_nodes, edges=_edges)
        case Revert():
            edges = list(
                filter(
                    lambda x: Edge(source=mapping[x[0]], destination=mapping[x[1]])
                    in dag.edges,
                    combinations(transformation.nodes, 2),
                )
            )
            to_remove = [
                Edge(source=mapping[e[0]], destination=mapping[e[1]])
                for e in transformation.edges + edges
            ]
            to_add = [
                Edge(source=mapping[e[1]], destination=mapping[e[0]])
                for e in transformation.edges + edges
            ]
            return DAG(
                nodes=dag.nodes,
                edges=list(set([e for e in dag.edges if e not in to_remove] + to_add)),
            )
        case ToOperator():
            op = dict(
                (k, getattr(dag.nodes[mapping[transformation.content]], v))
                for k, v in {"name": "text", "language": "language"}.items()
            )
            nid = mapping[transformation.nid]
            return DAG(nodes=dict(dag.nodes, **{nid: Operator(**op)}), edges=dag.edges)
        case ToParameter():
            nid = mapping[transformation.nid]
            language = dag.nodes[nid].language  # type: ignore[union-attr]
            kw = dag.nodes[mapping[transformation.kw]].text  # type: ignore[union-attr]
            value = dag.nodes[mapping[transformation.value]]
            return DAG(
                nodes=dict(
                    dag.nodes,
                    **{
                        nid: Parameter(
                            name=kw,
                            dtype=value.type,  # type: ignore[union-attr]
                            value=value.text,  # type: ignore[union-attr]
                        )
                    },
                ),
                edges=dag.edges,
            )
        case unknown:
            raise NotImplementedError(f'Unknown transformation "{unknown}" in f: rewrite')


def rewrite(
    dag: ast.DAG,
    mapping: Dict[ast.ID, ast.ID],
    transformations: Sequence[Transformation],
) -> ast.DAG:
    """Apply a sequence of transformations to *dag*."""
    return reduce(lambda g, tf: _rewrite(g, mapping, tf), transformations, dag)


def has_single_value(_list: Sequence[Any]) -> bool:
    return len(set(_list)) == 1


async def transform(dag: ast.DAG, rules: Rules) -> ast.DAG:
    """Apply rewrite rules to *dag* sequentially on the whole DAG.

    Uses a deque to support dynamic sub-rules: when a rule's ``rules``
    field is non-empty, the generated sub-rules are pushed to the front
    of the queue so they execute before the remaining rules.
    """
    # Strip the AST module root (node "0") before applying rules.
    # The old split_line_based approach implicitly excluded it; keeping it
    # causes Revert rules to route edges through the root, breaking data flow.
    dag = DAG(
        nodes={k: v for k, v in dag.nodes.items() if k != "0"},
        edges=[e for e in dag.edges if e.source != "0" and e.destination != "0"],
    )

    queue = deque(rules)

    while queue:
        rule = queue.popleft()
        processed: list = []
        match_found, candidate = match(rule.pattern, dag, processed=processed)
        while match_found:
            if rule.rules:
                queue.extendleft([r(dag, candidate) for r in rule.rules])
            processed.append(candidate)
            dag = rewrite(dag, candidate, rule.transformations)
            match_found, candidate = match(rule.pattern, dag, processed=processed)

    _nn = dag.nodes
    return DAG(
        nodes=_nn,
        edges=[e for e in dag.edges if e.source in _nn and e.destination in _nn],
    )


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

@typeclass
def to_dot(instance) -> None:
    """Convert objects to DOT grammar (Graphviz)."""


@to_dot.instance(DAG)
def _to_dot_dag(dag: DAG):
    import pygraphviz as pgv
    G = pgv.AGraph(directed=True)
    for id, node in dag.nodes.items():
        label = _class_of(node)
        for attr in ["text", "type", "name"]:
            label += f"\n{attr}: '{getattr(node, attr)}'" if hasattr(node, attr) else ""
        G.add_node(id, label=label)
    for edge in dag.edges:
        G.add_edge(edge.source, edge.destination)
    return G


@typeclass
def pprint(instance) -> None:
    """Pretty-print tree-like objects."""


@pprint.instance(Tree)
def _pprint_tree(instance: Tree):
    def _foo(n, depth=0):
        print("  " * depth, n.type, n.text)
        for child in n.children:
            _foo(child, depth + 1)

    _foo(instance.root_node)


@pprint.instance(ast.Node)
def _pprint_node(instance: ast.Node):
    if instance.type in [
        "module",
        "expression_statement",
        "assignment",
        "argument_list",
        "keyword_argument",
        "pattern_list",
        "call",
    ]:
        return instance.type
    else:
        return f"{instance.type} {instance.text}"


@pprint.instance(Operator)
def _pprint_operator(instance: Operator):
    return repr(instance)


@pprint.instance(Parameter)
def _pprint_parameter(instance: Parameter):
    return repr(instance)


@pprint.instance(DAG)
def _pprint_dag(instance):
    def _foo(nid, depth=0):
        if nid in instance.nodes:
            print("  " * depth, pprint(instance.nodes[nid]))
        for edge in instance.edges:
            if edge.source != nid:
                continue
            _foo(edge.destination, depth + 1)

    if instance.edges:
        root = sorted(instance.edges, key=lambda e: int(e.source))[0].source
        _foo(root)
    else:
        print(instance)


def draw_graph(
    graph, name: str, folder_path: str | Path, layout: str = "dot", **kwargs
) -> None:
    """Write graph to DOT and render as PNG."""
    if isinstance(folder_path, str):
        folder_path = Path(folder_path)
    (folder_path / "dot").mkdir(exist_ok=True)
    (folder_path / "png").mkdir(exist_ok=True)
    graph.write(folder_path / f"dot/{name}.dot")
    graph.layout(layout)
    graph.draw(folder_path / f"png/{name}.png", **kwargs)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse(
    code: str,
    language: str,
    rewrite_rules: Rules | None = None,
) -> tuple[DAG, DAG]:
    """Parse *code* into an initial AST DAG, then transform it.

    Returns ``(initial_dag, final_dag)``.  If *rewrite_rules* is ``None``,
    the default rule set from :func:`get_rules` is used.
    """
    parser = create_parser(language)
    tree = parser.parse(bytes(code, "utf8"))
    dag = to_dag(tree, language)
    rules = rewrite_rules if rewrite_rules is not None else get_rules()
    final = asyncio.run(transform(dag, rules))
    return dag, final
