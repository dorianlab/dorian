"""
dorian/pipeline/mitigation_rewrites_migration.py
------------------------------------------------
Map the five hardcoded ``_APPLY_REGISTRY`` Apply functions in
``mitigation_rewrites.py`` to equivalent primitive-op JSON lists.

Each Apply entry stored in ``expdb.rewrites`` today looks like::

    {"type": "Apply", "function": "<name>", "<args>": "..."}

After migration the rewrite rule's ``transformations`` list holds
``PrimitiveOp`` dicts instead — pure data, no function registry
to dispatch through at apply time. The Rust ``graph::primitive``
evaluator can execute them directly; the Python
``mitigation_rewrites`` compiler stops needing its Python
callback registry.

This module is **read-only**: it exposes ``apply_to_primitives``
for one-shot migration scripts + tests. It never writes to the
KB itself; callers stage the re-serialisation.
"""
from __future__ import annotations

from typing import Any


def apply_to_primitives(apply_entry: dict) -> list[dict]:
    """Return the primitive-op list equivalent to a KB ``Apply``.

    Unknown functions raise ``KeyError`` so callers fail loudly
    rather than silently dropping transformations.
    """
    fn = apply_entry.get("function")
    if fn not in _DISPATCH:
        raise KeyError(
            f"no primitive mapping for Apply function {fn!r} — "
            f"known: {sorted(_DISPATCH.keys())}"
        )
    return _DISPATCH[fn](apply_entry)


# ---------------------------------------------------------------------------
# Per-Apply expansions
# ---------------------------------------------------------------------------


def _reroute_incoming(entry: dict) -> list[dict]:
    """``reroute_incoming(to=<target>, through=<through>, anchor?)``

    Python behaviour (see ``mitigation_rewrites._make_reroute_incoming``):

      * Intercept edges landing on ``mapping[target]`` from non-``through``
        sources.
      * Exclude Parameter-source edges (they're config, not data flow).
      * With ``anchor``: only intercept edges at position == anchor.
      * Without ``anchor``: narrow to FEATURE-FLOW ports on the target
        via KB I/O lookup.

    Primitive equivalent:

      * Selector with
        ``destination = FromMapping(target)``,
        ``source = Not(PayloadKind(Parameter))``,
        ``destination_role = FeatureFlow`` OR
        ``position = KeywordEq(anchor)``,
      * ``through = FromMapping(through)``.
    """
    target = entry.get("to")
    through = entry.get("through")
    anchor = entry.get("anchor")
    if not target or not through:
        raise ValueError(f"reroute_incoming needs to + through keys: {entry!r}")
    selector: dict[str, Any] = {
        "destination": {"sel": "from_mapping", "key": target},
        "source": {
            "sel": "not",
            "inner": {"sel": "payload_kind", "payload": "parameter"},
        },
    }
    if anchor:
        # Anchor overrides role-based narrowing — it's an explicit
        # kwarg-name filter in the Python path.
        selector["position"] = {"pred": "keyword_eq", "k": str(anchor)}
    else:
        # Role-based narrowing. The KB has the ground truth; the
        # Rust ``RoleResolver`` answers at apply time.
        selector["destination_role"] = "feature_flow"
    return [{
        "op": "reroute_edges",
        "selector": selector,
        "through": {"sel": "from_mapping", "key": through},
    }]


def _reroute_outgoing(entry: dict) -> list[dict]:
    """``reroute_outgoing(from=<source>, through=<through>)``

    Python behaviour (see ``_make_reroute_outgoing``):

      * For every edge leaving ``mapping[source]``, re-root the
        outgoing chain through ``mapping[through]``.
      * Produces ``src → through`` + ``through → original_dst``.

    Primitive equivalent: same ``RerouteEdges`` shape but with
    the selector matching edges whose *source* is the tracked
    node (rather than the destination). The evaluator's
    RerouteEdges primitive already handles either orientation.
    """
    src = entry.get("from")
    through = entry.get("through")
    if not src or not through:
        raise ValueError(f"reroute_outgoing needs from + through: {entry!r}")
    return [{
        "op": "reroute_edges",
        "selector": {
            "source": {"sel": "from_mapping", "key": src},
        },
        "through": {"sel": "from_mapping", "key": through},
    }]


def _replace_node(entry: dict) -> list[dict]:
    """``replace_node(target=<node>, new_node_spec=<payload>)``

    Python behaviour: keep the node ID + every incident edge;
    swap the payload (Operator/Parameter/Snippet) in place.

    Primitive equivalent: ``SetNodePayload`` targeting the matched
    node. ``new_node_spec`` in the KB carries the payload as a
    dict; normalise to the ``NodePayloadSpec`` tag.
    """
    target = entry.get("target")
    spec = entry.get("new_node_spec") or entry.get("spec")
    if not target or not isinstance(spec, dict):
        raise ValueError(f"replace_node needs target + new_node_spec: {entry!r}")
    return [{
        "op": "set_node_payload",
        "selector": {"sel": "from_mapping", "key": target},
        "payload": _normalise_payload_spec(spec),
    }]


def _insert_x_preprocessor(entry: dict) -> list[dict]:
    """``insert_x_preprocessor(through=<new_op>)``

    Python behaviour: specialised feature-flow reroute — routes
    X-flow edges landing on any operator through the new node.
    Effectively a reroute_incoming with feature-flow role,
    targetting every compound-aware operator the rule's pattern
    matched.

    Primitive equivalent: a RerouteEdges on feature-flow destination
    role. The pattern mapping supplies the destination set — the
    KB rule already wrote ``"to"`` or references the pattern-
    local var. Extract that + delegate to the same primitive
    shape as ``reroute_incoming``.
    """
    through = entry.get("through")
    target = entry.get("to") or entry.get("target") or "n"
    if not through:
        raise ValueError(f"insert_x_preprocessor needs through: {entry!r}")
    return [{
        "op": "reroute_edges",
        "selector": {
            "destination": {"sel": "from_mapping", "key": target},
            "destination_role": "feature_flow",
            "source": {
                "sel": "not",
                "inner": {"sel": "payload_kind", "payload": "parameter"},
            },
        },
        "through": {"sel": "from_mapping", "key": through},
    }]


def _duplicate_data_kwarg(entry: dict) -> list[dict]:
    """``duplicate_data_kwarg(target=<node>, source_position=<int>, kwarg_name=<str>)``

    Python behaviour: find the existing edge into ``target`` at
    positional index ``source_position``, clone it with
    ``position = kwarg_name`` (keyword binding). Used for rewrites
    that need the same data wired to both a positional slot *and*
    a keyword slot.

    Primitive equivalent:
      * An ``AddEdge`` whose source is resolved from the tracked
        positional edge. We can't resolve "source of that
        positional edge" declaratively in the current primitive
        vocabulary without a new selector kind (edge-by-edge-
        selector). Expand to AddEdge using a new ``edge_source``
        selector for future work; for now, emit a placeholder
        that the migration script logs as "needs primitive
        extension".
    """
    target = entry.get("target")
    pos = entry.get("source_position")
    kwarg = entry.get("kwarg_name")
    if not target or pos is None or not kwarg:
        raise ValueError(
            f"duplicate_data_kwarg needs target + source_position + kwarg_name: {entry!r}"
        )
    # Gap: primitive vocabulary lacks an "edge-reference source"
    # selector (we can select an EDGE by predicate for
    # DeleteEdges, but AddEdge wants a NODE selector on each
    # side). Until that lands, keep a diagnostic-tagged primitive
    # the evaluator refuses; migration script surfaces it to the
    # operator rather than silently dropping the clone. Ship
    # the extension in the next primitive slice.
    return [{
        "op": "add_edge",
        "__needs_primitive_extension__": "edge_ref_source_selector",
        "source": {
            "sel": "from_mapping",
            "key": f"__edge_ref::{target}@{pos}__",
        },
        "destination": {"sel": "from_mapping", "key": target},
        "position": kwarg,
        "output": 0,
    }]


_DISPATCH = {
    "reroute_incoming": _reroute_incoming,
    "reroute_outgoing": _reroute_outgoing,
    "replace_node": _replace_node,
    "insert_x_preprocessor": _insert_x_preprocessor,
    "duplicate_data_kwarg": _duplicate_data_kwarg,
}


# ---------------------------------------------------------------------------
# Payload normalisation
# ---------------------------------------------------------------------------

def _normalise_payload_spec(spec: dict) -> dict:
    """KB ``new_node_spec`` uses ``{"node_type": ..., "name": ..., ...}``
    shape. Convert to the primitive's tagged ``NodePayloadSpec``.
    """
    kind = (spec.get("node_type") or spec.get("kind") or "").lower()
    if kind == "operator":
        return {
            "payload": "operator",
            "name": spec.get("name", ""),
            "language": spec.get("language", "python"),
        }
    if kind == "parameter":
        return {
            "payload": "parameter",
            "name": spec.get("name", ""),
            "dtype": spec.get("dtype", "string"),
            "value": str(spec.get("value", "")),
        }
    if kind == "snippet":
        return {
            "payload": "snippet",
            "name": spec.get("name", ""),
            "code": spec.get("code", ""),
            "language": spec.get("language", "python"),
        }
    raise ValueError(f"unknown payload kind {kind!r} in {spec!r}")


# ---------------------------------------------------------------------------
# Rule-level migration
# ---------------------------------------------------------------------------

def migrate_rewrite_doc(doc: dict) -> tuple[dict, list[str]]:
    """Rewrite one ``expdb.rewrites`` doc: replace every Apply
    entry in ``transformations`` with its primitive equivalent.

    Returns ``(migrated_doc, warnings)``. Add / Delete entries
    pass through unchanged — they're already declarative.
    """
    out = dict(doc)
    warnings: list[str] = []
    transforms = list(doc.get("transformations") or [])
    new_transforms: list[dict] = []
    for t in transforms:
        t_type = (t.get("type") or "").lower()
        if t_type == "apply":
            try:
                prims = apply_to_primitives(t)
            except (KeyError, ValueError) as exc:
                warnings.append(f"apply migration failed: {exc}")
                new_transforms.append(t)
                continue
            for p in prims:
                if "__needs_primitive_extension__" in p:
                    warnings.append(
                        f"rule {doc.get('name')}: primitive gap — "
                        f"{p['__needs_primitive_extension__']}"
                    )
            new_transforms.extend(prims)
        else:
            new_transforms.append(t)
    out["transformations"] = new_transforms
    return out, warnings
