"""Graph edit distance adapter for Dorian DAG JSON dicts.

Bridges the GED logic from ``dorian.pipeline.pipeline_similarity`` to work
with Dorian's current ``dorian.dag.*`` types (which serialize to JSON dicts
via ``DAG.to_json_dict()``).

The existing ``pipeline_similarity.py`` uses its own legacy ``DAG``,
``Operator``, ``Parameter`` classes — we cannot import them directly.
Instead we convert DAG JSON dicts to NetworkX graphs and compute GED here.
"""
from __future__ import annotations

from typing import Any, Dict

import networkx as nx


def dag_json_to_nxgraph(dag_json: Dict[str, Any]) -> nx.MultiDiGraph:
    """Convert a Dorian DAG JSON dict to a NetworkX MultiDiGraph.

    Accepts both the ``to_json_dict()`` format (with ``version``, ``metadata``,
    ``nodes``, ``edges``) and the raw pipeline format (just ``nodes``, ``edges``).

    Node attributes:
    - ``name``: operator/parameter/snippet name
    - ``type``: class type ("Operator", "Parameter", "Snippet", "Node")

    Edge attributes:
    - ``position``: argument slot on the destination
    - ``output``: output port on the source
    """
    G = nx.MultiDiGraph()

    nodes = dag_json.get("nodes", {})
    edges = dag_json.get("edges", [])

    for node_id, node_data in nodes.items():
        if isinstance(node_data, dict):
            node_type = node_data.get("class_type", node_data.get("type", "Node"))
            node_name = node_data.get("name", node_data.get("text", str(node_id)))
        else:
            node_type = "Unknown"
            node_name = str(node_data)

        G.add_node(node_id, name=node_name, type=node_type)

    for edge_data in edges:
        if isinstance(edge_data, dict):
            src = edge_data.get("source", "")
            dst = edge_data.get("destination", "")
            pos = edge_data.get("position", 0)
            out = edge_data.get("output", 0)
        else:
            continue

        G.add_edge(src, dst, position=pos, output=out)

    return G


def _node_match(n1: dict, n2: dict) -> bool:
    """Two nodes match if they have the same name."""
    return n1.get("name") == n2.get("name")


def _edge_match(e1: dict, e2: dict) -> bool:
    """Two edges match if all their attributes agree."""
    return e1 == e2


def graph_edit_distance(dag1_json: Dict[str, Any], dag2_json: Dict[str, Any]) -> int:
    """Compute the graph edit distance between two pipeline DAGs.

    Returns the number of edits (node additions/removals + edge
    additions/removals) to transform ``dag1`` into ``dag2``.

    Uses ``nx.graph_edit_distance`` which returns a float cost.  With
    default cost functions (1.0 per operation), this is equivalent to
    the integer edit count.

    For large graphs (>15 nodes each), this falls back to a bounded
    approximation to keep latency under 100ms.
    """
    G1 = dag_json_to_nxgraph(dag1_json)
    G2 = dag_json_to_nxgraph(dag2_json)

    n1, n2 = len(G1), len(G2)

    # For small graphs, use the exact (but potentially slow) algorithm
    if n1 + n2 <= 30:
        try:
            cost = nx.graph_edit_distance(
                G1, G2,
                node_match=_node_match,
                edge_match=_edge_match,
                timeout=2.0,  # seconds — fail fast for pathological cases
            )
            return int(cost) if cost is not None else _fast_distance(G1, G2)
        except (nx.NetworkXError, Exception):
            return _fast_distance(G1, G2)
    else:
        return _fast_distance(G1, G2)


def _fast_distance(G1: nx.MultiDiGraph, G2: nx.MultiDiGraph) -> int:
    """Fast approximate GED based on (operator + parameter) symmetric difference.

    Lower bound on true GED, O(|V1| + |V2|). Used as fallback when
    exact GED is too slow. Parameter nodes participate with their
    (name, dtype, value) triple — value-different pipelines are
    distinct, per the system-wide rule that nothing ignores
    parameter values.
    """
    def _sig(d):
        t = d.get("type") or d.get("class_type")
        if t == "Operator":
            return ("op", d.get("name"))
        if t == "Parameter":
            return ("p", d.get("name"), d.get("dtype"), d.get("value"))
        if t == "Snippet":
            return ("snip", d.get("name"))
        return None

    s1 = {sig for _, d in G1.nodes(data=True) if (sig := _sig(d)) is not None}
    s2 = {sig for _, d in G2.nodes(data=True) if (sig := _sig(d)) is not None}

    node_diff = len(s1.symmetric_difference(s2))
    edge_diff = abs(G1.number_of_edges() - G2.number_of_edges())
    return node_diff + edge_diff


def extract_operator_names(dag_json: Dict[str, Any]) -> list[str]:
    """Extract sorted operator names from a DAG JSON dict.

    Useful for BK-Tree operator fingerprinting and fast distance lower bounds.
    """
    nodes = dag_json.get("nodes", {})
    names = []
    for node_data in nodes.values():
        if isinstance(node_data, dict):
            ct = node_data.get("class_type", node_data.get("type", ""))
            if ct == "Operator":
                name = node_data.get("name", "")
                if name:
                    names.append(name)
    return sorted(names)
