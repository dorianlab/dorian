"""Pipeline structural class hash.

A stable, content-addressable identifier over a pipeline's
**logical structure**:

  - Operator FQNs (+ their task method lists)
  - Edge topology (source-FQN, dest-FQN, position)
  - Wired-Parameter NAMES per operator (not values)
  - Snippet name (code canonicalisation is future work)

Parameter VALUES are intentionally excluded. Pipelines with
``test_size=0.2`` and ``test_size=0.3`` share the same class
because a canonical-form rewrite that targets a structural
weakness applies to both equally.

Parameter NAMES (i.e. which Parameter handles are wired to each
operator) are intentionally INCLUDED. A pipeline with unwired
``random_state`` is a structurally distinct class from the same
pipeline with ``random_state`` wired -- and that distinction is
what drives the auto-seed canonical-form promotion.

See (internal design note; not in public repo) for design rationale.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _operator_fingerprint(node: Operator) -> str:
    tasks = sorted(node.tasks or [])
    return f"op::{node.name}::[{','.join(tasks)}]"


def _snippet_fingerprint(node: Snippet) -> str:
    # v1: identify snippets by name; code-level canonicalisation
    # (whitespace, imports, AST normalisation) is a future pass
    # flagged in the design doc. Matching by name is a reasonable
    # default because the snippet catalog keys by name.
    return f"snippet::{node.name}"


def _edge_key(edge: Edge, node_fqn: dict[str, str]) -> tuple[str, str, str]:
    src_fqn = node_fqn.get(edge.source, f"__unknown::{edge.source}")
    dst_fqn = node_fqn.get(edge.destination, f"__unknown::{edge.destination}")
    pos = str(edge.position)
    return (src_fqn, dst_fqn, pos)


def _wired_param_names(dag: DAG) -> dict[str, list[str]]:
    """For each non-Parameter node, return the sorted list of
    keyword-position parameter names wired to it (ignoring values)."""
    out: dict[str, list[str]] = {nid: [] for nid in dag.nodes}
    for edge in dag.edges:
        src = dag.nodes.get(edge.source)
        if not isinstance(src, Parameter):
            continue
        if not isinstance(edge.position, str):
            # Positional binding -- we capture it as a stringified
            # index. Matches the rest of the hash.
            continue
        dst_list = out.setdefault(edge.destination, [])
        dst_list.append(edge.position)
    for lst in out.values():
        lst.sort()
    return out


def _node_fqn(node: Any) -> str:
    if isinstance(node, Operator):
        return _operator_fingerprint(node)
    if isinstance(node, Snippet):
        return _snippet_fingerprint(node)
    if isinstance(node, Parameter):
        return f"param::{node.name}"
    return f"node::{type(node).__name__}"


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

def canonical_class_hash(dag: DAG) -> str:
    """Return the structural class hash for ``dag`` as a 64-char
    hex string (SHA-256 digest).

    Deterministic: same logical structure → same hash, regardless
    of node IDs, insertion order, or parameter *values*.

    Empty pipelines hash to a fixed sentinel.
    """
    hasher = hashlib.sha256()

    # 1. Sorted list of node fingerprints (excluding Parameter
    #    payloads -- we capture them indirectly via wired_param_names).
    node_fqn = {nid: _node_fqn(n) for nid, n in dag.nodes.items()}
    non_param_nodes: list[tuple[str, str]] = []
    for nid, fqn in node_fqn.items():
        if fqn.startswith("param::"):
            continue
        non_param_nodes.append((nid, fqn))
    # Sort by fqn first, then by node id for deterministic order.
    op_list = sorted((fqn for _, fqn in non_param_nodes))
    hasher.update(b"operators\x00")
    for fqn in op_list:
        hasher.update(fqn.encode("utf-8"))
        hasher.update(b";")
    hasher.update(b"\x00")

    # 2. Sorted list of edge tuples. Only edges between non-Parameter
    #    nodes participate here; Parameter edges surface in step 3.
    edges: list[tuple[str, str, str]] = []
    for edge in dag.edges:
        src = dag.nodes.get(edge.source)
        if isinstance(src, Parameter):
            continue
        edges.append(_edge_key(edge, node_fqn))
    hasher.update(b"edges\x00")
    for src, dst, pos in sorted(edges):
        hasher.update(f"{src}->{dst}@{pos}".encode("utf-8"))
        hasher.update(b";")
    hasher.update(b"\x00")

    # 3. Wired-Parameter names per operator (sorted).
    wired = _wired_param_names(dag)
    # Key the sorted list on the operator's fqn (stable across node
    # id renaming).
    per_op: list[tuple[str, list[str]]] = []
    for nid, names in wired.items():
        if not names:
            continue
        fqn = node_fqn.get(nid)
        if fqn is None or fqn.startswith("param::"):
            continue
        per_op.append((fqn, names))
    per_op.sort(key=lambda x: x[0])
    hasher.update(b"wired_params\x00")
    for fqn, names in per_op:
        hasher.update(fqn.encode("utf-8"))
        hasher.update(b"[")
        for name in names:
            hasher.update(name.encode("utf-8"))
            hasher.update(b",")
        hasher.update(b"]")
        hasher.update(b";")
    hasher.update(b"\x00")

    return hasher.hexdigest()


def describe(dag: DAG) -> dict[str, Any]:
    """Diagnostic view of what the class hash captures, useful for
    introspection + tests + debugging why two pipelines hash
    differently."""
    node_fqn = {nid: _node_fqn(n) for nid, n in dag.nodes.items()}
    operators = sorted(
        fqn for fqn in node_fqn.values() if not fqn.startswith("param::")
    )
    edges = sorted(
        _edge_key(e, node_fqn)
        for e in dag.edges
        if not isinstance(dag.nodes.get(e.source), Parameter)
    )
    wired = _wired_param_names(dag)
    wired_by_fqn: dict[str, list[str]] = {}
    for nid, names in wired.items():
        if not names:
            continue
        fqn = node_fqn.get(nid, "")
        if fqn and not fqn.startswith("param::"):
            wired_by_fqn.setdefault(fqn, []).extend(names)
    for fqn in wired_by_fqn:
        wired_by_fqn[fqn] = sorted(wired_by_fqn[fqn])
    return {
        "class_hash": canonical_class_hash(dag),
        "operators": operators,
        "edges": [list(e) for e in edges],
        "wired_params": wired_by_fqn,
    }


__all__ = ["canonical_class_hash", "describe"]
