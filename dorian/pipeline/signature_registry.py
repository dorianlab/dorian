"""KB-sourced signature registry for the Rust validator.

The engine's ``validate_pipeline`` takes a JSON map
``operator_fqn → {inputs: [PortSig], outputs: [PortSig]}``. This
module builds that JSON from the Dorian KB by calling
``get_operator_ios_bulk`` + ``get_interface_ios_bulk`` +
``get_operator_interfaces_bulk``, then merging port annotations
(type, role, split, ...) into the shape ``PortSig`` expects.

Cross-cutting design points:

  * ROLE & SPLIT come from the semantic-bridge triplets in
    ``dorian/knowledge/sources/annotations.py``, surfaced through
    the Represents / OnSplit predicates (see
    internal design note).
  * Interface-level annotations are inherited by every concrete
    operator that implements that interface — a single triplet
    covers sklearn's 60+ classifiers + regressors.
  * Operator-level annotations override interface-level ones
    (e.g. ``train_test_split.X_train`` carries a split tag that
    the parent Function interface doesn't declare).
  * Missing annotations → ``None``. The validator treats unset
    fields as "don't care" wildcards (backward-compat with
    pre-bridge catalog entries).
  * ``required`` defaults to True for positional ports (int name)
    and False for keyword-named ports. Catalog authors can
    override with an ``is_required`` KB flag — not wired yet;
    most unwired-required bugs land on positional args.

The module degrades gracefully: if the KB isn't reachable, it
returns an empty registry JSON so the validator runs structural
checks only. Matches the degradation policy of the validator
gate itself.
"""
from __future__ import annotations

import json
from typing import Any, Callable


# Ports whose name (after KB rename) is exactly a digit-string are
# treated as positional arguments. Everything else is keyword. The
# distinction controls the ``required`` default — positional args
# almost always must be wired; kwargs often have a language default.
def _is_positional(name: str) -> bool:
    return name.isdigit()


def _port_sig_from_kb(port: dict, *, is_input: bool) -> dict:
    """Turn a KB port dict (from get_*_io) into the PortSig JSON
    shape the Rust validator deserialises.

    Robust to the progressive nature of KB annotations: every field
    is optional except ``name``.
    """
    name = str(port.get("name", ""))
    out: dict[str, Any] = {"name": name}
    # KB stores the type under "type"; we map it to PortSig.port_type
    # via serde's rename (``#[serde(rename = "type")]``), so JSON
    # emits it as "type".
    if port.get("type") and port["type"] != "any":
        out["type"] = port["type"]
    if is_input:
        # Positional ports are required unless the KB says otherwise.
        # Keyword-named ports default to optional (language defaults
        # typically cover them).
        out["required"] = _is_positional(name)
    # variadic stays False unless the KB flags it — out-of-scope for
    # the initial bridge pass.
    if "role" in port:
        out["role"] = port["role"]
    if "split" in port:
        out["split"] = port["split"]
    return out


# Variadic fan-out: maximum number of integer positional aliases
# we emit for ports whose KB name starts with ``*`` (train_test_split
# accepts ``*arrays``, etc.). Covers all real callsites; bounds the
# registry growth and still lets the env wire (X, y) at positions
# (0, 1) against a variadic port.
_VARIADIC_POSITION_CAP = 8


def _expand_port_aliases(port: dict, *, is_input: bool) -> list[dict]:
    """Emit one or more PortSig entries for a KB port dict.

    The Rust validator matches an incoming edge to a port by **name**,
    so when the env / import-trial-configs wire an edge with
    ``position=0`` the validator needs a port literally named ``"0"``.
    The KB represents the same port as either:

      * ``name="y_true", position=0``  — keyword-named but also
        bindable positionally (accuracy_score.y_true at 0);
      * ``name="*arrays", position=0`` — variadic positional (any
        integer i binds to ``*arrays``, e.g. train_test_split).

    For the first case we emit BOTH the named port and an integer-
    stringified alias. For the variadic case we emit aliases for
    positions ``0..VARIADIC_POSITION_CAP-1`` so positional wiring
    validates alongside the variadic name itself.

    Output ports are emitted unchanged — the env already derives the
    output slot via ``_port_output_index`` over the KB outputs list,
    so integer aliasing on outputs would only add noise.
    """
    base = _port_sig_from_kb(port, is_input=is_input)
    sigs: list[dict] = [base]
    if not is_input:
        return sigs

    name = base["name"]
    position = port.get("position")
    if isinstance(position, int) and str(position) != name:
        alias_sig = _port_sig_from_kb(
            {**port, "name": str(position)}, is_input=True
        )
        # The alias inherits the parent port's required flag — the
        # alias is just a wiring-convenience name for the same
        # underlying port, so the presence/absence requirement must
        # track the real port (``y_true`` is optional → ``0`` is
        # optional; ``n_estimators`` is optional → ``0`` is optional).
        alias_sig["required"] = base.get("required", False)
        sigs.append(alias_sig)

    if name.startswith("*"):
        # Variadic: emit integer aliases up to the fan-out cap so
        # ``position=0,1,…`` binds against the variadic port. All
        # aliases are optional — variadic args are variable-length,
        # so position N-1 may be unwired without making the pipeline
        # invalid (train_test_split on two arrays uses only 0 and 1).
        for i in range(_VARIADIC_POSITION_CAP):
            alias_sig = _port_sig_from_kb({**port, "name": str(i)}, is_input=True)
            alias_sig["required"] = False
            sigs.append(alias_sig)

    return sigs


def build_signatures_json(
    *,
    get_operator_ios_bulk: Callable[[], dict[str, tuple[list[dict], list[dict]]]] | None = None,
    get_interface_ios_bulk: Callable[[], dict[str, tuple[list[dict], list[dict]]]] | None = None,
    get_operator_interfaces_bulk: Callable[[], dict[str, str]] | None = None,
    get_all_kb_operator_params: Callable[[], dict[str, list[dict]]] | None = None,
) -> str:
    """Build a JSON SignatureRegistry from the KB, ready to pass
    straight to ``dorian_native.validate_pipeline``.

    The four loaders are injectable so tests can supply fixtures
    without hitting Neo4j. Production callers pass ``None`` and
    get the live KB path.
    """
    if (
        get_operator_ios_bulk is None
        or get_interface_ios_bulk is None
        or get_operator_interfaces_bulk is None
        or get_all_kb_operator_params is None
    ):
        try:
            from dorian.knowledge.queries import (
                get_all_kb_operator_params as _params,
                get_interface_ios_bulk as _iios,
                get_operator_interfaces_bulk as _oifaces,
                get_operator_ios_bulk as _oios,
            )
        except Exception:
            return "{}"
        if get_operator_ios_bulk is None:
            get_operator_ios_bulk = _oios
        if get_interface_ios_bulk is None:
            get_interface_ios_bulk = _iios
        if get_operator_interfaces_bulk is None:
            get_operator_interfaces_bulk = _oifaces
        if get_all_kb_operator_params is None:
            get_all_kb_operator_params = _params

    try:
        iface_ios = get_interface_ios_bulk()
    except Exception:
        iface_ios = {}
    try:
        op_ios = get_operator_ios_bulk()
    except Exception:
        op_ios = {}
    try:
        op_interfaces = get_operator_interfaces_bulk()
    except Exception:
        op_interfaces = {}
    try:
        op_params = get_all_kb_operator_params()
    except Exception:
        op_params = {}

    # The RL catalog declares compound-shape ports for estimators /
    # transformers ((X_train, y_train, X_test) → y_pred) that the KB
    # doesn't know about — the KB has the real sklearn class's
    # __init__ shape, not the RL executor's inline-expanded contract.
    # Merge the catalog's ports into the signature registry so the
    # validator accepts edges wired against the catalog's port names.
    # Graceful-degrades if the catalog import fails (stand-alone KB
    # usage without the RL package).
    catalog_ports: dict[str, tuple[list[dict], list[dict]]] = {}
    try:
        from rl.catalog.loader import seed_catalog_with_guards
        for op in seed_catalog_with_guards():
            catalog_ports[op.op_key] = (
                [
                    {"name": p.name, "type": p.type_hint or "any"}
                    for p in op.inputs
                ],
                [
                    {"name": p.name, "type": p.type_hint or "any"}
                    for p in op.outputs
                ],
            )
    except Exception:
        pass

    registry: dict[str, dict] = {}
    all_ops = set(op_ios) | set(op_interfaces) | set(op_params) | set(catalog_ports)
    for op_name in all_ops:
        iface = op_interfaces.get(op_name)
        iface_in, iface_out = iface_ios.get(iface, ([], [])) if iface else ([], [])
        op_in, op_out = op_ios.get(op_name, ([], []))
        # Merge catalog-declared ports by name. Catalog wins on name
        # overlap because the RL catalog is the authoritative source
        # for the compound-shape contract the env wires against.
        cat_in, cat_out = catalog_ports.get(op_name, ([], []))
        op_in = list(op_in) + cat_in
        op_out = list(op_out) + cat_out

        # Some operators (train_test_split, accuracy_score, ...) declare
        # their configurable settings as "parameters" in the KB rather
        # than as inputs. Wiring a Parameter node to such an op is the
        # same as passing a kwarg, so surface each parameter as an
        # optional input port so the validator doesn't reject it.
        extra_param_inputs = [
            {"name": p.get("name"), "type": p.get("type") or "any"}
            for p in op_params.get(op_name, [])
            if p.get("name")
        ]

        inputs = _merge_ports(
            iface_in, list(op_in) + extra_param_inputs, is_input=True
        )
        outputs = _merge_ports(iface_out, op_out, is_input=False)

        if not inputs and not outputs:
            # Skip operators with zero declared IO — they're
            # indistinguishable from unknown operators at the
            # validator's level and adding an empty sig would
            # silently accept any wiring.
            continue
        registry[op_name] = {"inputs": inputs, "outputs": outputs}

    return json.dumps(registry)


def _merge_ports(
    iface_ports: list[dict], op_ports: list[dict], *, is_input: bool
) -> list[dict]:
    """Merge interface-level + operator-level port declarations by
    name. Operator-level fields override interface-level ones.

    The KB returns each port as a dict with ``name`` + any subset
    of {type, role, split, position, default, ...}. This merge is
    a shallow dict.update() keyed on name.

    Each merged KB port is expanded via ``_expand_port_aliases`` so
    a single ``{name:"y_true", position:0}`` emits both the named
    ``y_true`` port and the ``"0"`` alias the Rust validator needs
    when the env wires the edge with ``position=0``.
    """
    by_name: dict[str, dict] = {}
    for p in iface_ports:
        name = p.get("name")
        if name:
            by_name[str(name)] = dict(p)
    for p in op_ports:
        name = p.get("name")
        if not name:
            continue
        by_name.setdefault(str(name), {}).update(p)

    out: list[dict] = []
    seen: set[str] = set()
    for p in by_name.values():
        for sig in _expand_port_aliases(p, is_input=is_input):
            key = sig["name"]
            if key in seen:
                continue
            seen.add(key)
            out.append(sig)
    return out


__all__ = ["build_signatures_json"]
