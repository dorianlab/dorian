"""Port-name lookup for the frontend DAG format.

``kb_port_maps(operator_fqn)`` returns ``(in_map, out_map)`` where both maps
translate port *position* values (the same int/str stored on
``Edge.position`` / ``Edge.output``) to human-readable labels sourced from
the KB so the canvas renders ``X_train`` / ``y_true`` instead of bare numbers.

Falls back to empty maps when the KB is unreachable or the operator is
unknown — callers must always handle missing labels gracefully.
"""
from __future__ import annotations

import functools
from typing import Any


def _coerce_position(pos: Any) -> Any:
    """Mirror ``Edge._coerce``: numeric-string positions become ``int``.

    KB positions are stored as strings in the Rust snapshot
    (``"0"``, ``"1"``, …) but ``Edge.position`` and ``Edge.output`` are
    ``int`` for numeric slots.  Coercing here lets the in/out maps key-match
    the actual edge values at render time.
    """
    if isinstance(pos, str):
        try:
            return int(pos)
        except (ValueError, TypeError):
            pass
    return pos


@functools.lru_cache(maxsize=512)
def kb_port_maps(operator_fqn: str) -> tuple[dict[Any, str], dict[Any, str]]:
    """Return ``(in_map, out_map)`` for *operator_fqn*.

    ``in_map``  — maps ``edge.position`` values to input-port label strings.
    ``out_map`` — maps ``edge.output`` values to output-port label strings.

    Both maps are keyed by the coerced position type (``int`` for numeric
    slots, ``str`` for named keyword slots) so they align with the values
    stored on ``Edge`` instances after ``Edge._coerce`` runs.

    Returns ``({}, {})`` on any KB error so the caller can always
    destructure safely.
    """
    try:
        from dorian.knowledge.queries import get_operator_io
        inputs, outputs = get_operator_io(operator_fqn)
    except Exception:
        return {}, {}

    in_map: dict[Any, str] = {
        _coerce_position(p["position"]): p["name"]
        for p in inputs
        if p.get("name") and p.get("position") is not None
    }
    out_map: dict[Any, str] = {
        _coerce_position(p["position"]): p["name"]
        for p in outputs
        if p.get("name") and p.get("position") is not None
    }
    return in_map, out_map
