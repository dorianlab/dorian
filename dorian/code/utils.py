"""
dorian/code/utils.py
---------------------
Graph comparison and conversion utilities for the code-parsing layer.

Used to compare generated dataflow graphs against ground truth and
to convert DAGs to NetworkX DiGraphs for analysis.
"""
import json
from typing import Dict, Set, Tuple

import networkx as nx

from dorian.dag import DAG


def find_graph_difference(
    ground_truth: nx.DiGraph, generated: nx.DiGraph
) -> Dict[str, Set]:
    """Compare *ground_truth* against *generated* and return the diff.

    Returns a dict with four keys:
    - ``missing_nodes``: nodes in ground_truth but not in generated
    - ``extra_nodes``: nodes in generated but not in ground_truth
    - ``missing_edges``: edges in ground_truth but not in generated
    - ``extra_edges``: edges in generated but not in ground_truth
    """
    return {
        "missing_nodes": set(ground_truth.nodes()) - set(generated.nodes()),
        "extra_nodes": set(generated.nodes()) - set(ground_truth.nodes()),
        "missing_edges": set(ground_truth.edges()) - set(generated.edges()),
        "extra_edges": set(generated.edges()) - set(ground_truth.edges()),
    }


def dag_to_graph(dag: DAG) -> nx.DiGraph:
    """Convert a :class:`DAG` to a ``networkx.DiGraph``.

    Node identities are based on their JSON-serialized dict (sorted keys)
    so that structurally identical nodes compare as equal across graphs.
    """
    G = nx.DiGraph()
    for _, node in dag.nodes.items():
        G.add_node(json.dumps(node.to_dict(), sort_keys=True))
    for edge in dag.edges:
        G.add_edge(
            json.dumps(dag.nodes[edge.source].to_dict(), sort_keys=True),
            json.dumps(dag.nodes[edge.destination].to_dict(), sort_keys=True),
        )
    return G


def relabel_graph(graph: nx.DiGraph) -> nx.DiGraph:
    """Rename graph nodes using a ``type_attr1_attr2`` scheme.

    Useful for producing human-readable node labels when visualizing
    the graph comparison.
    """
    renaming_mapping = {}
    for n_data in graph.nodes.data():
        label: str = n_data[1].get("label", str(n_data[0]))
        parts: list[str] = label.split("\n")
        node_type = parts[0].lower().strip().strip("'\"\\")
        attribute_values = []
        for att in parts[1:]:
            att_value = att.split(":")[1].lower().strip().strip("'\"\\") if ":" in att else att
            attribute_values.append(att_value)

        new_node_id = node_type + "_" + "_".join(attribute_values) if attribute_values else node_type
        renaming_mapping[n_data[0]] = new_node_id

    return nx.relabel_nodes(graph, renaming_mapping)
