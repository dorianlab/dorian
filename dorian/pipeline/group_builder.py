"""
dorian/pipeline/group_builder.py
--------------------------------
Build a :class:`~dorian.dag.Group` for a compound operator using KB metadata.

Called at drag-and-drop time: the backend constructs the method-chain
sub-DAG so the frontend renders a single operator-like node.  Parameters
remain as separate canvas nodes — only the interface methods are collapsed.
At execution time the Group is flattened by ``_flatten_groups``.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from dorian.dag import Edge, Group, IOMapping, Operator, Snippet

_log = logging.getLogger(__name__)

# Re-used from transforms.py — passthrough snippet for Guardrail operators.
from dorian.pipeline.transforms import _PASSTHROUGH_SNIPPET_CODE


def build_group(operator_name: str, node_id: str) -> Group | None:
    """Build a Group for a compound operator using KB interface metadata.

    Returns ``None`` for Function-interface operators (no expansion needed)
    or operators without a known KB interface.

    The Group only wraps the method chain (e.g. __init__ + chat.send).
    Parameters are NOT included as children — they remain as separate
    visible/editable nodes on the canvas.  The ``io_map`` exposes handles
    for both data I/O and parameter connections so the flattening pass
    can rewire edges to the correct internal method node.

    Parameters
    ----------
    operator_name:
        Fully qualified operator name (e.g. ``sklearn.preprocessing.StandardScaler``).
    node_id:
        The frontend-generated UUID for the node being dropped.
    """
    from dorian.knowledge.queries import (
        get_interface_attributes,
        get_interface_io,
        get_method_io,
        get_method_sequence,
        get_operator_interface,
        get_operator_parameters,
    )

    interface = get_operator_interface(operator_name)
    if interface is None:
        _log.debug("No interface for %s — not a compound operator", operator_name)
        return None

    methods = get_method_sequence(interface)
    if len(methods) < 2:
        # Function interface — direct callable, no sub-DAG.
        return None

    is_passthrough = "passthrough" in get_interface_attributes(interface)
    inputs, outputs = get_interface_io(interface)
    kb_params = get_operator_parameters(operator_name)

    prefix = f"{node_id}_cx"

    children: Dict[str, dict] = {}
    internal_edges: List[Edge] = []
    io_map: Dict[str, IOMapping] = {}

    # ---- Build child nodes (method chain only — NO parameters) ----

    # 1. __init__ node (constructor — always present)
    init_id = f"{prefix}_init"
    children[init_id] = Operator(
        name=operator_name, language="python"
    ).to_dict()

    if is_passthrough:
        _build_passthrough_group(
            prefix, init_id, operator_name, inputs, outputs,
            children, internal_edges, io_map,
        )
    else:
        method_io = get_method_io(interface)
        _build_generic_group(
            prefix, init_id, operator_name, methods, interface,
            inputs, outputs, method_io,
            children, internal_edges, io_map,
        )

    # ---- Add parameter handles to io_map (params stay external) ----
    if kb_params:
        # Build method_ids for param routing (same IDs as _build_generic_group)
        method_ids: Dict[str, str] = {"__init__": init_id}
        if not is_passthrough:
            for i, method_name in enumerate(methods[1:], start=1):
                method_ids[method_name] = f"{prefix}_{method_name.replace('.', '_')}_{i}"

        for p in kb_params:
            _add_param_handle(
                init_id, p, methods, is_passthrough, method_ids, prefix, io_map,
            )

    return Group(
        name=operator_name,
        children=children,
        internal_edges=internal_edges,
        io_map=io_map,
        collapsed=True,
        source_interface=interface,
    )


# ---------------------------------------------------------------------------
# Passthrough mode (Guardrail: __init__ + passthrough Snippet)
# ---------------------------------------------------------------------------

def _build_passthrough_group(
    prefix: str,
    init_id: str,
    operator_name: str,
    inputs: list[dict],
    outputs: list[dict],
    children: Dict[str, dict],
    internal_edges: List[Edge],
    io_map: Dict[str, IOMapping],
) -> None:
    snippet_id = f"{prefix}_passthrough"
    children[snippet_id] = Snippet(
        name=f"{operator_name}__passthrough",
        code=_PASSTHROUGH_SNIPPET_CODE,
        language="python",
    ).to_dict()

    # __init__ → snippet (instance at position 0)
    internal_edges.append(Edge(init_id, snippet_id, position=0, output=0))

    # IO mapping: one data input → snippet position 1
    if inputs:
        handle_name = inputs[0].get("name", "data")
        io_map[handle_name] = IOMapping(
            direction="input",
            internal_node_id=snippet_id,
            internal_handle=1,
        )
    else:
        io_map["data"] = IOMapping(
            direction="input",
            internal_node_id=snippet_id,
            internal_handle=1,
        )

    # Output mapping: snippet output → external (by name AND by index)
    if outputs:
        for i, out in enumerate(outputs):
            out_name = out.get("name", "output")
            m = IOMapping(direction="output", internal_node_id=snippet_id, internal_handle=0)
            io_map[out_name] = m
            io_map[str(i)] = m
    else:
        m = IOMapping(direction="output", internal_node_id=snippet_id, internal_handle=0)
        io_map["output"] = m
        io_map["0"] = m


# ---------------------------------------------------------------------------
# Generic N-method group (KB-driven)
# ---------------------------------------------------------------------------

def _build_generic_group(
    prefix: str,
    init_id: str,
    operator_name: str,
    methods: list[str],
    interface: str,
    inputs: list[dict],
    outputs: list[dict],
    method_io: dict[str, tuple[list[dict], list[dict]]],
    children: Dict[str, dict],
    internal_edges: List[Edge],
    io_map: Dict[str, IOMapping],
) -> None:
    """Build child nodes, internal edges, and io_map for any method chain length."""

    # Create one child node per non-init method.
    method_ids: Dict[str, str] = {"__init__": init_id}
    for i, method_name in enumerate(methods[1:], start=1):
        mid = f"{prefix}_{method_name.replace('.', '_')}_{i}"
        children[mid] = Operator(name=method_name, language="python").to_dict()
        method_ids[method_name] = mid

    # Chain: init → method1 → method2 → … (instance at position 0)
    for i in range(len(methods) - 1):
        src = method_ids[methods[i]]
        dst = method_ids[methods[i + 1]]
        internal_edges.append(Edge(src, dst, position=0, output=0))

    # --- Input mapping using per-method I/O ---
    if method_io and inputs:
        # Map: input name → (method, internal position) — first consuming method
        name_to_target: Dict[str, tuple[str, int | str]] = {}
        for method_name, (m_inputs, _) in method_io.items():
            for m_inp in m_inputs:
                inp_name = m_inp.get("name", "")
                if inp_name not in name_to_target:
                    raw_pos = m_inp.get("position", 1)
                    try:
                        int_pos = int(raw_pos)
                    except (TypeError, ValueError):
                        int_pos = raw_pos
                    name_to_target[inp_name] = (method_name, int_pos)

        for inp in inputs:
            inp_name = inp.get("name", "input")
            target = name_to_target.get(inp_name)
            if target and target[0] in method_ids:
                io_map[inp_name] = IOMapping(
                    direction="input",
                    internal_node_id=method_ids[target[0]],
                    internal_handle=target[1],
                )
            else:
                # Fallback: first non-init method
                _pos = inp.get("position", 1)
                try:
                    _pos = int(_pos)
                except (TypeError, ValueError):
                    pass
                io_map[inp_name] = IOMapping(
                    direction="input",
                    internal_node_id=method_ids[methods[1]],
                    internal_handle=_pos,
                )
    elif inputs:
        # No per-method I/O: route all inputs to the first non-init method
        for inp in inputs:
            inp_name = inp.get("name", "input")
            _pos = inp.get("position", 1)
            try:
                _pos = int(_pos)
            except (TypeError, ValueError):
                pass
            io_map[inp_name] = IOMapping(
                direction="input",
                internal_node_id=method_ids[methods[1]],
                internal_handle=_pos,
            )
    else:
        io_map["input"] = IOMapping(
            direction="input",
            internal_node_id=method_ids[methods[1]],
            internal_handle=1,
        )

    # --- Output mapping using per-method I/O ---
    terminal_mid = method_ids[methods[-1]]

    if method_io and outputs:
        # Map interface output names to producing methods.
        output_name_to_mid: Dict[str, str] = {}
        for method_name, (_, m_outputs) in method_io.items():
            for m_out in m_outputs:
                out_name = m_out.get("name", "")
                if method_name in method_ids:
                    output_name_to_mid[out_name] = method_ids[method_name]

        for i, out in enumerate(outputs):
            out_name = out.get("name", "output")
            src_mid = output_name_to_mid.get(out_name, terminal_mid)
            m = IOMapping(direction="output", internal_node_id=src_mid, internal_handle=0)
            io_map[out_name] = m
            io_map[str(i)] = m
    elif outputs:
        for i, out in enumerate(outputs):
            out_name = out.get("name", "output")
            m = IOMapping(direction="output", internal_node_id=terminal_mid, internal_handle=0)
            io_map[out_name] = m
            io_map[str(i)] = m
    else:
        m = IOMapping(direction="output", internal_node_id=terminal_mid, internal_handle=0)
        io_map["output"] = m
        io_map["0"] = m


# ---------------------------------------------------------------------------
# Parameter handle helper (adds io_map entry — no child node)
# ---------------------------------------------------------------------------

def _add_param_handle(
    init_id: str,
    param: dict,
    methods: list[str],
    is_passthrough: bool,
    method_ids: Dict[str, str],
    prefix: str,
    io_map: Dict[str, IOMapping],
) -> None:
    """Add an io_map entry for a parameter so external param edges get rewired.

    Parameters stay as separate canvas nodes — this only adds the routing
    information so _flatten_groups knows where to send param edges.
    """
    pname = param["name"]
    target_method = param.get("method")

    if is_passthrough:
        snippet_id = f"{prefix}_passthrough"
        if target_method and target_method != "__init__":
            io_map[pname] = IOMapping(
                direction="input",
                internal_node_id=snippet_id,
                internal_handle=pname,
            )
        else:
            io_map[pname] = IOMapping(
                direction="input",
                internal_node_id=init_id,
                internal_handle=pname,
            )
    elif target_method and target_method in method_ids:
        io_map[pname] = IOMapping(
            direction="input",
            internal_node_id=method_ids[target_method],
            internal_handle=pname,
        )
    else:
        io_map[pname] = IOMapping(
            direction="input",
            internal_node_id=init_id,
            internal_handle=pname,
        )
