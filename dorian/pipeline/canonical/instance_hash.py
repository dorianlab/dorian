"""Pipeline **instance** hash — value-sensitive identity for the pipeline store.

Distinct from ``canonical_class_hash`` (which is intentionally
value-insensitive for rewrite matching — two pipelines with
``test_size=0.2`` vs ``test_size=0.3`` share a class so a rewrite
targeting the class applies to both). Storage identity must NOT
collapse them: they are different *instances* and each earns its
own leaderboard entry, its own BK-Tree slot, its own cache bucket.

Inclusions:
  - Everything ``canonical_class_hash`` includes (operators + edges
    + wired Parameter names).
  - **Parameter values** (and dtypes — because ``int("1")`` vs
    ``string("1")`` materialise differently at the resolver).
  - **Snippet source code** (normalised via ``str.strip``) — two
    snippets with the same ``name`` but different bodies are
    different pipelines. Class hash currently dedupes by name only;
    instance hash refuses that.

The two hashes serve two different questions:
  - class hash  -> "which rewrite rules match this pipeline?"
  - instance hash -> "is this pipeline already in the store?"

Changing one without the other is the bug path. Both live here so
future edits update them together.
"""
from __future__ import annotations

import hashlib

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet

from .class_hash import canonical_class_hash

__all__ = ["canonical_instance_hash", "canonical_class_hash"]


def _operator_fp(node: Operator) -> str:
    tasks = sorted(node.tasks or [])
    return f"op::{node.name}::[{','.join(tasks)}]"


def _parameter_fp(node: Parameter) -> str:
    # dtype matters: ``int("1")`` vs ``str("1")`` resolve to
    # different values at execution time. Empty string values are
    # normalised to the empty string so None / "" collapse.
    dtype = (node.dtype or "").strip()
    value = "" if node.value is None else str(node.value)
    return f"param::{node.name}::{dtype}::{value}"


def _snippet_fp(node: Snippet) -> str:
    # Normalise by stripping leading/trailing whitespace. Deeper
    # canonicalisation (AST / import rewrites) is explicitly future
    # work — noted in class_hash.py for symmetry.
    body = (node.code or "").strip()
    return f"snippet::{node.name}::{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def _node_fp(node) -> str:
    if isinstance(node, Operator):
        return _operator_fp(node)
    if isinstance(node, Parameter):
        return _parameter_fp(node)
    if isinstance(node, Snippet):
        return _snippet_fp(node)
    return f"node::{type(node).__name__}"


def _edge_key(edge: Edge, node_fp: dict[str, str]) -> tuple[str, str, str, str]:
    src = node_fp.get(edge.source, f"__unknown::{edge.source}")
    dst = node_fp.get(edge.destination, f"__unknown::{edge.destination}")
    pos = str(edge.position)
    out = str(edge.output)
    return (src, dst, pos, out)


def canonical_instance_hash(dag: DAG) -> str:
    """Return a SHA-256 hex hash that uniquely identifies a pipeline
    **instance** — including every parameter value and snippet body.

    Two pipelines with identical structure but different parameter
    values produce different hashes. Use as storage identity
    (``pipeline_id``), not as a rewrite-match key.
    """
    hasher = hashlib.sha256()

    # 1. Sorted node fingerprints — every node, including Parameters.
    fps = sorted(_node_fp(n) for n in dag.nodes.values())
    hasher.update(b"nodes\x00")
    for fp in fps:
        hasher.update(fp.encode("utf-8"))
        hasher.update(b";")
    hasher.update(b"\x00")

    # 2. Sorted edge tuples using node fingerprints (stable across
    #    NodeId renaming). Includes output index so multi-output
    #    producers wired to different slots are distinguishable.
    node_fp_by_id = {nid: _node_fp(n) for nid, n in dag.nodes.items()}
    edges = [_edge_key(e, node_fp_by_id) for e in dag.edges]
    edges.sort()
    hasher.update(b"edges\x00")
    for src, dst, pos, out in edges:
        hasher.update(f"{src}->{dst}@{pos}#{out}".encode("utf-8"))
        hasher.update(b";")
    hasher.update(b"\x00")

    return hasher.hexdigest()
