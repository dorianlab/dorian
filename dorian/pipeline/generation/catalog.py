"""KB-driven operator catalog for the generation engine.

No hardcoded operator lists.  ``load_catalog()`` queries the Neo4j KB for
all operators with their interface, task, and family metadata, then enriches
each entry with parameter specs (also from the KB) and interface-level I/O
port definitions.

When a new operator is added to the KB (with proper ``performs``,
``implements``, and interface / parameter annotations), it automatically
appears in the catalog without code changes.
"""
from __future__ import annotations

import functools
from typing import Any, Sequence

from backend.events import Event, emit
from dorian.pipeline.generation.types import OperatorSpec, ParameterSpec, PortSpec


# ---------------------------------------------------------------------------
# KB → ParameterSpec conversion
# ---------------------------------------------------------------------------

def _parse_kb_value(val: str | None, dtype: str) -> Any:
    """Parse a string value from the KB into the appropriate Python type."""
    if val is None or val == "None":
        return None
    if dtype == "int":
        return int(float(val))
    if dtype == "float":
        return float(val)
    if dtype == "bool":
        return val.lower() in ("true", "1") if isinstance(val, str) else bool(val)
    return val  # categorical / str / env — keep as string


def _kb_param_to_spec(d: dict) -> ParameterSpec:
    """Convert a single KB parameter dict to a ``ParameterSpec``."""
    dtype = d.get("type", "str")
    default = _parse_kb_value(d.get("default"), dtype)
    low = float(d["low"]) if d.get("low") is not None else None
    high = float(d["high"]) if d.get("high") is not None else None
    log_scale = str(d.get("log_scale", "")).lower() in ("true", "1")

    choices = None
    choices_raw = d.get("choices")
    if choices_raw and isinstance(choices_raw, str):
        choices = tuple(_parse_kb_value(c.strip(), dtype) for c in choices_raw.split(","))

    return ParameterSpec(
        name=d["name"],
        dtype=dtype,
        default=default,
        low=low,
        high=high,
        log_scale=log_scale,
        choices=choices,
    )


def _get_parameter_specs(op_name: str) -> tuple[ParameterSpec, ...]:
    """Query KB for operator parameters and convert to ``ParameterSpec``.

    Only includes chain-annotated params (those with ``is of type …`` in the
    KB).  Plain ``has parameter name`` declarations without tuning domains
    are excluded — the generation engine only needs tunable params.
    """
    from dorian.knowledge.queries import get_operator_parameters
    kb_params = get_operator_parameters(op_name)
    if not kb_params:
        return ()
    return tuple(
        _kb_param_to_spec(d)
        for d in kb_params
        if d.get("type")  # only chain-annotated params
    )


# ---------------------------------------------------------------------------
# Interface-level I/O templates
# ---------------------------------------------------------------------------
# These define the high-level input/output ports for each interface type.
# The compound operator expansion (transforms.py) handles the detailed
# sub-DAG wiring (init → fit → transform / predict).

_INTERFACE_IO: dict[str, tuple[tuple[PortSpec, ...], tuple[PortSpec, ...]]] = {
    "Sklearn Transformer": (
        # inputs
        (PortSpec("X", 0, "features"),),
        # outputs
        (PortSpec("X_transformed", 0, "features"),),
    ),
    "Sklearn Estimator": (
        # inputs
        #
        # X (pos 0) carries X_train into fit; y (pos 1) carries y_train
        # into fit; X_test (pos 2) carries X_test into predict. Compound
        # expansion routes each interface input to the method that
        # consumes it (see Sklearn Estimator method_io in the KB:
        # fit.X→"X", fit.y→"y", predict.X→"X_test"). Without X_test in
        # the catalog, _place_operator never wires a test-set feed, and
        # predict raises "missing 1 required positional argument: 'X'"
        # at execution time.
        (
            PortSpec("X", 0, "features"),
            PortSpec("y", 1, "labels"),
            PortSpec("X_test", 2, "features"),
        ),
        # outputs
        (PortSpec("predictions", 0, "predictions"),),
    ),
    "Function": (
        # Generic — varies per operator; default to single in/out
        (PortSpec("input", 0, "any"),),
        (PortSpec("output", 0, "any"),),
    ),
    "LLM Chat Completion": (
        # inputs — messages is keyword-only in chat.send(*, messages=...)
        (PortSpec("messages", "messages", "list[dict]"),),
        # outputs
        (PortSpec("response", 0, "ChatResponse"),),
    ),
    "Display Output": (
        # inputs — single data input to visualise
        (PortSpec("data", 0, "any"),),
        # outputs — formatted dict (terminal node, usually no downstream)
        (PortSpec("formatted", 0, "any"),),
    ),
}

# Override I/O for specific Function operators that don't follow the 1-in/1-out pattern
_FUNCTION_IO_OVERRIDES: dict[str, tuple[tuple[PortSpec, ...], tuple[PortSpec, ...]]] = {
    "sklearn.model_selection.train_test_split": (
        (
            PortSpec("X", 0, "features"),
            PortSpec("y", 1, "labels"),
        ),
        (
            PortSpec("X_train", 0, "features"),
            PortSpec("X_test", 1, "features"),
            PortSpec("y_train", 2, "labels"),
            PortSpec("y_test", 3, "labels"),
        ),
    ),
    "sklearn.metrics.accuracy_score": (
        (
            PortSpec("y_true", 0, "labels"),
            PortSpec("y_pred", 1, "predictions"),
        ),
        (PortSpec("score", 0, "score"),),
    ),
    "pandas.read_csv": (
        (PortSpec("filepath", 0, "any"),),
        (PortSpec("dataframe", 0, "features"),),
    ),
}


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------

def _resolve_io(name: str, interface: str | None) -> tuple[tuple[PortSpec, ...], tuple[PortSpec, ...]] | None:
    """Resolve I/O ports for an operator.

    Resolution order (most specific wins):
      1. KB per-operator declarations (``get_operator_io`` — for Function-
         family operators that declare I/O on their own node).
      2. Python ``_FUNCTION_IO_OVERRIDES`` dict -- legacy pre-KB
         overrides, kept as a transitional fallback. Remove once the
         KB covers every entry.
      3. Python ``_INTERFACE_IO`` template by interface name.
      4. KB per-interface declarations (``get_interface_io``).
      5. ``None`` — caller emits a ``KBPortNameGap`` diagnostic so the
         hole is surfaced instead of silently falling through to
         positional indices in the UI.
    """
    from dorian.knowledge.queries import get_interface_io, get_operator_io

    # 1. Per-operator KB declarations (most authoritative).
    kb_op_in, kb_op_out = get_operator_io(name)
    if kb_op_in or kb_op_out:
        inputs = tuple(
            PortSpec(p["name"], int(p.get("position", i)), p.get("type", "any"))
            for i, p in enumerate(kb_op_in)
        )
        outputs = tuple(
            PortSpec(p["name"], int(p.get("position", i)), p.get("type", "any"))
            for i, p in enumerate(kb_op_out)
        )
        return inputs, outputs

    # 2. Legacy Python override (kept until KB fully covers).
    if name in _FUNCTION_IO_OVERRIDES:
        return _FUNCTION_IO_OVERRIDES[name]

    # 3. Python interface template.
    if interface and interface in _INTERFACE_IO:
        return _INTERFACE_IO[interface]

    # 4. KB interface declarations.
    if interface:
        kb_inputs, kb_outputs = get_interface_io(interface)
        if kb_inputs or kb_outputs:
            inputs = tuple(
                PortSpec(p["name"], int(p.get("position", i)), p.get("type", "any"))
                for i, p in enumerate(kb_inputs)
            )
            outputs = tuple(
                PortSpec(p["name"], int(p.get("position", i)), p.get("type", "any"))
                for i, p in enumerate(kb_outputs)
            )
            return inputs, outputs

    # 5. Unknown — caller logs KBPortNameGap.
    return None


# Operators excluded from RL generation — these are helpers, utilities,
# metrics, or I/O operators that should not be placed by the RL agent.
# They either form part of the frozen evaluation template (metrics, split,
# dataset loader) or are utility functions without meaningful ML semantics.
_RL_EXCLUDED_PREFIXES = (
    "sklearn.metrics.",        # metrics — handled by eval procedure
    "sklearn.model_selection.",  # train_test_split — in eval template
    "sklearn.utils.",
    "sklearn.base.",
    "sklearn.exceptions.",
    "sklearn.datasets.",
    "pandas.",                 # data I/O — in eval template
    "dorian.io.",              # platform operators — in eval template
    "openrouter.",             # LLM operators — not in RL scope yet
    "trust_guardrails.",       # guardrails — not in RL scope yet
)

# Only these interfaces are in scope for RL generation
_RL_ALLOWED_INTERFACES = frozenset({
    "Sklearn Transformer",
    "Sklearn Estimator",
})


@functools.lru_cache(maxsize=32)
def load_catalog(task: str | None = None, *, rl_only: bool = False) -> tuple[OperatorSpec, ...]:
    """Load operator catalog from the Neo4j KB, optionally filtered by task.

    Returns a frozen tuple of ``OperatorSpec`` instances, each enriched with:
    - I/O port specs (from interface templates)
    - Parameter specs (from KB chain annotations)

    The result is LRU-cached per *task* string.  Pass ``task=None`` for the
    full catalog.

    Parameters
    ----------
    task : str or None
        If given, only operators that ``performs`` this task (or any of its
        sub-tasks) are included.  Pass ``None`` for the full catalog.
    """
    from dorian.knowledge.queries import get_all_operators

    raw = get_all_operators()
    if not raw:
        # Don't cache an empty result. ``load_catalog`` is lru_cached;
        # if Neo4j happened to be empty the first time this is called
        # (e.g. the scheduler started before KB seeding finished), the
        # cached () would persist for the whole process lifetime and
        # every subsequent call would silently hit the empty catalog.
        # Clearing the cache here forces a fresh KB read on the next
        # call — by which time the seeder has usually caught up.
        try:
            load_catalog.cache_clear()
        except Exception:
            pass
        # Observability emit. ``emit()`` refuses to run from an async
        # context by design; this function can be called either from
        # sync startup code (uvicorn worker init) or from async scheduler
        # code (scheduler.run_once → GenerationEngine(...) →
        # PipelineGenEnv(...) → load_catalog). Swallow the RuntimeError
        # when we're on the async path; the empty-catalog situation is
        # still visible via the return value and the scheduler logs.
        try:
            emit(Event("CatalogEmpty", {"detail": "KB returned empty operator catalog — is Neo4j populated?"}))
        except RuntimeError:
            pass
        return ()

    specs: list[OperatorSpec] = []
    for entry in raw:
        name = entry["name"]
        interface = entry.get("interface")
        tasks = tuple(entry.get("tasks") or ())
        family = entry.get("family")

        # Filter by task if requested
        if task and task not in tasks:
            continue

        # Skip operators without a resolved I/O spec — the generation
        # engine can only construct pipelines from operators it knows how
        # to wire. KBPortNameGap surfaces the hole so it gets fixed in
        # the KB rather than silently papered over with numeric indices
        # in the UI.
        io = _resolve_io(name, interface)
        if io is None:
            emit(Event("KBPortNameGap", {
                "operator": name,
                "interface": interface,
                "reason": "no per-operator or per-interface I/O declarations",
            }))
            emit(Event("CatalogOperatorSkipped", {"operator": name, "interface": interface}))
            continue

        inputs, outputs = io
        parameters = _get_parameter_specs(name)

        # Determine visibility — operators matching excluded prefixes or
        # non-RL interfaces are "secondary" (findable via search but not
        # shown by default in the catalog widget, and excluded from RL).
        visibility = "default"
        if any(name.startswith(pfx) for pfx in _RL_EXCLUDED_PREFIXES):
            visibility = "secondary"
        elif interface and interface not in _RL_ALLOWED_INTERFACES:
            visibility = "secondary"

        # RL-only mode: only include operators that are default-visible
        # (i.e. sklearn transformers & estimators not in excluded prefixes)
        if rl_only and visibility != "default":
            continue

        specs.append(OperatorSpec(
            name=name,
            interface=interface or "Function",
            tasks=tasks,
            family=family,
            inputs=inputs,
            outputs=outputs,
            parameters=parameters,
            visibility=visibility,
        ))

    return tuple(specs)


def get_catalog_by_interface(
    catalog: Sequence[OperatorSpec],
) -> dict[str, list[OperatorSpec]]:
    """Group a catalog by interface type."""
    by_iface: dict[str, list[OperatorSpec]] = {}
    for spec in catalog:
        by_iface.setdefault(spec.interface, []).append(spec)
    return by_iface


def get_catalog_by_family(
    catalog: Sequence[OperatorSpec],
) -> dict[str | None, list[OperatorSpec]]:
    """Group a catalog by algorithmic family."""
    by_family: dict[str | None, list[OperatorSpec]] = {}
    for spec in catalog:
        by_family.setdefault(spec.family, []).append(spec)
    return by_family
