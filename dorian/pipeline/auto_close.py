"""Declarative DAG closure via semantic-name port matching.

Given a partial DAG, resolve every unwired input port whose semantic
identity is shared by **exactly one** producer anywhere else in the
graph. Cascades: wiring one port may reduce another port's candidate
set from two to one, enabling it in the next pass. Halts when no
further edges resolve.

Pure function. No env state, no in-place mutation — returns a new
DAG + a list of the edges that were added. Callers that need to
attribute the closures (credit a policy, log an audit trail, pay a
tax proportional to shortcut use) read the returned edge list.

Used by:
  * The RL env's ``_evaluate_terminal`` to close the scoring cage on
    partial pipelines — the agent needs to wire fewer edges manually.
  * The UI's "suggest completion" affordance (future).
  * DB-authored rewrite rules via the ``add_edge_by_semantic_match``
    entry in ``_APPLY_REGISTRY``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from dorian.dag import DAG, Edge, Operator


_CASCADE_CAP = 10


@dataclass(frozen=True)
class ClosedEdge:
    """One edge added by the auto-close cascade.

    The tuple ``(src_node_id, src_output, dst_node_id, dst_port)`` is
    exactly what an ``AddEdgeSpec`` would carry for the same wiring —
    callers can reconstruct the synthetic action_id via their
    ``ActionSpace.id_for`` without re-deriving endpoints.
    """

    src_node_id: str
    src_output: int
    dst_node_id: str
    dst_port: str


def close_by_semantic_match(
    dag: DAG, catalog: Iterable, *, cascade_cap: int = _CASCADE_CAP,
) -> tuple[DAG, list[ClosedEdge]]:
    """Iteratively add edges where exactly one semantic-name-matched
    producer exists for an unwired input port.

    ``catalog`` is any iterable of ``OperatorMeta``-shaped objects
    (each exposing ``op_key`` + ``inputs``/``outputs`` lists of
    ``PortSpec``). A port is eligible as producer/consumer only
    when it carries a truthy semantic identity — pure positional
    names (``"0"``, ``"1"``) don't match because their identity is
    "i'm the N-th slot" not "i'm y_pred."

    Returns ``(new_dag, wires)`` — new_dag is always a fresh
    instance (input dag is never mutated); wires lists every edge
    the cascade added, in the order they were resolved.
    """
    op_by_key = {op.op_key: op for op in catalog}
    added: list[ClosedEdge] = []
    # Copy defensively so we can mutate lists freely inside the loop
    # and still return a clean DAG at the end.
    current_nodes = dict(dag.nodes)
    current_edges = list(dag.edges)

    for _ in range(cascade_cap):
        wired_destinations = {
            (e.destination, str(e.position)) for e in current_edges
        }
        producers = _index_producers(current_nodes, op_by_key)

        progress = False
        for nid, node in current_nodes.items():
            op = op_by_key.get(getattr(node, "name", ""))
            if op is None:
                continue
            for in_port in op.inputs:
                if getattr(in_port, "variadic", False):
                    continue
                if (nid, in_port.name) in wired_destinations:
                    continue
                sem = _semantic_identity(in_port)
                if not sem:
                    continue
                candidates = [
                    (src_id, src_idx, src_name)
                    for src_id, src_idx, src_name, src_sem in producers
                    if src_sem == sem and src_id != nid
                ]
                if len(candidates) != 1:
                    continue
                src_id, src_idx, src_name = candidates[0]
                current_edges.append(Edge(
                    source=src_id, destination=nid,
                    position=in_port.name, output=src_idx,
                ))
                added.append(ClosedEdge(
                    src_node_id=src_id,
                    src_output=src_idx,
                    dst_node_id=nid,
                    dst_port=in_port.name,
                ))
                wired_destinations.add((nid, in_port.name))
                progress = True
        if not progress:
            break

    if not added:
        return dag, []
    return DAG(nodes=current_nodes, edges=current_edges), added


def _index_producers(
    nodes: dict, op_by_key: dict,
) -> list[tuple[str, int, str, str]]:
    """Collect (node_id, output_idx, port_name, semantic_id) for every
    output port with a truthy semantic identity. Non-operator nodes
    (Parameter, Snippet) and unknown operators are skipped — they
    don't carry semantic output names."""
    out: list[tuple[str, int, str, str]] = []
    for nid, node in nodes.items():
        if not isinstance(node, Operator):
            continue
        op = op_by_key.get(node.name)
        if op is None:
            continue
        for idx, port in enumerate(op.outputs):
            sem = _semantic_identity(port)
            if not sem:
                continue
            out.append((nid, idx, port.name, sem))
    return out


def _semantic_identity(port) -> str:
    """The port's semantic identity for matching purposes.

    ``semantic_name`` wins when set; otherwise ``name`` is the identity,
    unless it's a pure positional alias (digit-string) in which case
    the port has no semantic identity and doesn't participate in
    matching.
    """
    sem = getattr(port, "semantic_name", None)
    if sem:
        return sem
    name = getattr(port, "name", "") or ""
    return name if name and not name.isdigit() else ""


__all__ = ["ClosedEdge", "close_by_semantic_match"]
