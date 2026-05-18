"""
dorian/knowledge/queries.py
----------------------------
Synchronous KB read accessors for the execution engine.

All queries route through ``dorian_native.kb_*`` against a snapshot
loaded once at process start (``$DORIAN_KB_SNAPSHOT``, default
``/app/volumes/kb_snapshot.json``). Neo4j has been retired — there
is no Cypher fallback, no Bolt driver, no ``config.neo4j`` lookup
on this hot path. Regenerate the snapshot with
``scripts/export_kb_snapshot.py`` if structure changes.

Caching
-------
Each accessor is decorated with ``functools.lru_cache`` so repeated
calls within a single process incur one pyo3 round-trip per
distinct argument. ``invalidate_all_caches()`` clears every cache
(used by FORCE_SEED + tenant overlay paths).
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
from pathlib import Path

from backend.events import Event, emit as _raw_emit

_log = logging.getLogger(__name__)


# ─── Rust KB snapshot — loaded once, raises on miss ─────────────────
_KB_SNAPSHOT_PATH = os.environ.get(
    "DORIAN_KB_SNAPSHOT", "/app/volumes/kb_snapshot.json"
)
_kb_load_lock = threading.Lock()
_kb_load_attempted = False


def _kb():
    """Return ``dorian_native`` with the KB snapshot loaded.

    Raises ``FileNotFoundError`` / ``RuntimeError`` rather than
    falling back to anything — the Cypher path was retired with
    Neo4j. Run ``scripts/export_kb_snapshot.py`` if the snapshot is
    missing.
    """
    global _kb_load_attempted
    import dorian_native  # type: ignore
    if dorian_native.kb_is_loaded():
        return dorian_native
    with _kb_load_lock:
        if dorian_native.kb_is_loaded():
            return dorian_native
        if _kb_load_attempted:
            raise RuntimeError(
                f"KB snapshot at {_KB_SNAPSHOT_PATH} not loadable; "
                "see prior log for the underlying error"
            )
        _kb_load_attempted = True
        snap_path = Path(_KB_SNAPSHOT_PATH)
        if not snap_path.is_file():
            raise FileNotFoundError(
                f"KB snapshot {_KB_SNAPSHOT_PATH} missing — "
                "run scripts/export_kb_snapshot.py to regenerate"
            )
        dorian_native.kb_load_snapshot(snap_path.read_text())
        _log.info("loaded KB snapshot from %s", snap_path)
        return dorian_native


def _safe_emit(event: Event) -> None:
    """Emit an event, swallowing the async-context error.

    KB queries are synchronous (lru_cache'd, run in background
    threads). The RL generation scheduler calls them from an async
    context where ``emit()`` raises ``RuntimeError`` — we log
    instead since KB events are observability signals.
    """
    try:
        _raw_emit(event)
    except RuntimeError:
        _log.info("%s: %s", event.type, event.data)


def close_driver() -> None:
    """No-op kept for backward compat with ``main.py`` shutdown.

    Neo4j queries used to open a Bolt driver here; it's been retired
    along with the snapshot port. Callers in the lifecycle hooks
    can keep invoking this without raising.
    """
    return None


# ═══════════════════════════════════════════════════════════════════
# Per-record queries (rust singletons)
# ═══════════════════════════════════════════════════════════════════

@functools.lru_cache(maxsize=512)
def get_operator_interface(operator_name: str) -> str | None:
    """Named interface for an operator (e.g. ``'Sklearn Transformer'``)
    or ``None`` when the operator is not registered.
    """
    return _kb().kb_operator_interface(operator_name)


@functools.lru_cache(maxsize=1)
def get_library_package_map() -> dict[str, str]:
    """Map of import name → pip package name (``{"sklearn": "scikit-learn"}``)."""
    return json.loads(_kb().kb_library_package_map())


@functools.lru_cache(maxsize=256)
def get_operator_import_path(operator_name: str) -> str | None:
    """Python import path for an operator declared via ``is_subclass_of``.

    Returns ``None`` when the operator's FQN itself is the import path.
    """
    return _kb().kb_operator_import_path(operator_name)


@functools.lru_cache(maxsize=1)
def get_all_interface_methods() -> frozenset[str]:
    """All method names usable as pipeline method-shortcut nodes.

    Sources: KB ``calls`` chains (``fit``, ``transform``, …) plus
    well-known sklearn compound methods (``fit_transform``, …) that
    aren't part of any chain. ``__init__`` is excluded.
    """
    return frozenset(_kb().kb_all_interface_methods()) | frozenset({
        "fit_transform", "fit_predict",
        "predict_proba", "predict_log_proba",
        "decision_function", "score",
        "inverse_transform", "partial_fit",
    }) - {"__init__"}


@functools.lru_cache(maxsize=256)
def get_method_sequence(interface_name: str) -> list[str]:
    """Ordered method call chain for a class interface.

    Returns ``[]`` for interfaces (e.g. ``Function``) without a
    ``calls`` chain — signals "no sub-DAG expansion needed".
    """
    return list(_kb().kb_method_sequence(interface_name))


# ─── Generation-engine queries ──────────────────────────────────────

@functools.lru_cache(maxsize=512)
def get_operators_for_task(task_name: str) -> list[str]:
    """FQN operators that ``performs`` the given task."""
    return list(_kb().kb_operators_for_task(task_name))


@functools.lru_cache(maxsize=256)
def get_operator_family(operator_name: str) -> str | None:
    """Algorithmic family name for an operator (e.g. ``'Ensemble'``)."""
    return _kb().kb_operator_family(operator_name)


@functools.lru_cache(maxsize=128)
def get_operators_by_interface(interface_name: str) -> list[str]:
    """FQN operators implementing the given interface."""
    return list(_kb().kb_operators_by_interface(interface_name))


@functools.lru_cache(maxsize=128)
def get_metrics_for_task(task_name: str) -> list[str]:
    """FQN metric operators that ``evaluates`` the given task."""
    return list(_kb().kb_metrics_for_task(task_name))


@functools.lru_cache(maxsize=256)
def get_metric_display_name(operator_name: str) -> str | None:
    """Abstract metric name (``'Accuracy'``, ``'F1 Score'``) for an operator."""
    return _kb().kb_metric_display_name(operator_name)


@functools.lru_cache(maxsize=64)
def get_all_operators() -> list[dict]:
    """All operators with ``{name, interface, tasks, family}``."""
    return json.loads(_kb().kb_all_operators())


# ─── Parameters / I/O ───────────────────────────────────────────────

@functools.lru_cache(maxsize=256)
def get_operator_parameters(operator_name: str) -> list[dict]:
    """Parameters declared for an operator (direct + interface + method).

    Translates the snapshot's ``ParameterSpec`` into the legacy
    Python contract: ``dtype`` → ``type``, optional ``low/high``
    coerced to strings, ``choices`` joined by comma. The snapshot
    already collapses inheritance precedence at ingest time, so
    ``level`` is omitted.
    """
    out: list[dict] = []
    for p in json.loads(_kb().kb_operator_parameters(operator_name) or "[]"):
        entry = {
            "name": p.get("name", ""),
            "type": p.get("dtype") or "any",
        }
        if p.get("default") is not None:
            entry["default"] = p["default"]
        if p.get("low") is not None:
            entry["low"] = (
                str(p["low"]) if isinstance(p["low"], (int, float)) else p["low"]
            )
        if p.get("high") is not None:
            entry["high"] = (
                str(p["high"]) if isinstance(p["high"], (int, float)) else p["high"]
            )
        if p.get("choices") is not None:
            entry["choices"] = ",".join(p["choices"])
        if p.get("log_scale") is not None:
            entry["log_scale"] = p["log_scale"]
        if p.get("method"):
            entry["method"] = p["method"]
        out.append(entry)
    return out


@functools.lru_cache(maxsize=128)
def get_interface_io(interface_name: str) -> tuple[list[dict], list[dict]]:
    """``(inputs, outputs)`` for an interface — list of ``{name, type, position}``."""
    raw = _kb().kb_interface_io(interface_name)
    if raw is None:
        return [], []
    ins, outs = json.loads(raw)
    return (
        [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in ins],
        [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in outs],
    )


@functools.lru_cache(maxsize=512)
def get_operator_io(operator_name: str) -> tuple[list[dict], list[dict]]:
    """Per-operator I/O port declarations (Function-family + auto-crawled)."""
    raw = _kb().kb_operator_io(operator_name)
    if raw is None:
        return [], []
    ins, outs = json.loads(raw)
    return (
        [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in ins],
        [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in outs],
    )


@functools.lru_cache(maxsize=128)
def get_method_io(interface_name: str) -> dict[str, tuple[list[dict], list[dict]]]:
    """Per-method I/O for an interface: ``{method: ([inputs], [outputs])}``."""
    raw = json.loads(_kb().kb_method_io(interface_name))
    out: dict[str, tuple[list[dict], list[dict]]] = {}
    for method, (ins, outs) in raw.items():
        out[method] = (
            [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in ins],
            [{"name": p["name"], "type": p["dtype"], "position": p["position"]} for p in outs],
        )
    return out


@functools.lru_cache(maxsize=128)
def get_interface_attributes(interface_name: str) -> list[str]:
    """Attribute names declared on an interface (e.g. ``'passthrough'``)."""
    return list(_kb().kb_interface_attributes(interface_name))


@functools.lru_cache(maxsize=256)
def get_operator_risks(operator_name: str) -> list[str]:
    """Risk names that an operator ``checks_for``."""
    return list(_kb().kb_operator_risks(operator_name))


# ─── Mitigation rewrite KB spec ─────────────────────────────────────

@functools.lru_cache(maxsize=256)
def get_mitigation_kb_spec(mitigation_name: str) -> dict | None:
    """Ontological metadata for a mitigation rewrite rule.

    Returns ``None`` when neither an interface target nor anchor
    inputs are declared.
    """
    raw = _kb().kb_mitigation_spec(mitigation_name)
    if raw is None:
        return None
    rec = json.loads(raw)
    if not (rec.get("interface_name") or rec.get("anchor_inputs")):
        return None
    return {
        "interface_name": rec.get("interface_name"),
        "anchor_inputs": list(rec.get("anchor_inputs") or []),
    }


def get_mitigations_batch(risk_names: list[str]) -> dict[str, list[dict]]:
    """Mitigations for multiple risks in one pass.

    Returns ``{risk_name: [{name, short, long}, ...]}``. ``short``
    and ``long`` are empty strings — descriptions don't ship in the
    snapshot yet (TODO: add to ``kb_mitigations_for_risk``).
    """
    if not risk_names:
        return {}
    kb = _kb()
    result: dict[str, list[dict]] = {r: [] for r in risk_names}
    for r in risk_names:
        for m in json.loads(kb.kb_mitigations_for_risk(r) or "[]"):
            result[r].append({
                "name": m.get("name", ""),
                "short": "",
                "long": "",
            })
    return result


# ─── Data-view ↔ Model-view pathway queries ─────────────────────────

@functools.lru_cache(maxsize=256)
def get_model_family(operator_name: str) -> str | None:
    """Model family for an operator (``Logistic``, ``Tree``, ...)."""
    return _kb().kb_model_family(operator_name)


@functools.lru_cache(maxsize=64)
def get_sensitive_families_for_risk(risk_name: str) -> tuple[str, ...]:
    """Model families particularly sensitive to a risk."""
    return tuple(_kb().kb_sensitive_families_for_risk(risk_name))


@functools.lru_cache(maxsize=64)
def get_risks_surfaced_by_metric(metric_name: str) -> tuple[str, ...]:
    """Risks surfaced when a metric crosses its threshold."""
    return tuple(_kb().kb_risks_surfaced_by_metric(metric_name))


@functools.lru_cache(maxsize=1)
def get_all_pathways() -> list[dict]:
    """Pathway rules with conditions, filters, and actions.

    Each pathway: ``{name, metric, direction, threshold, families,
    task, preprocessing, replacement, description, risk}``.
    """
    return json.loads(_kb().kb_all_pathways())


# ═══════════════════════════════════════════════════════════════════
# Bulk helpers — composed from the rust singletons above
# ═══════════════════════════════════════════════════════════════════
#
# Cypher used to do these in one round-trip; rust currently has no
# equivalent bulk pyo3 entry, so we walk the singleton catalog. The
# only callsite is one-time startup (``main.py`` warm-up via
# ``signature_registry``), and the functions are lru_cached, so the
# extra pyo3 hops are amortised across the process lifetime.

def get_operator_interfaces_bulk() -> dict[str, str]:
    """``{operator_name: interface_name}`` for every operator."""
    return {
        op["name"]: op["interface"]
        for op in get_all_operators()
        if op.get("name") and op.get("interface")
    }


def get_interface_ios_bulk() -> dict[str, tuple[list[dict], list[dict]]]:
    """``{interface_name: (inputs, outputs)}`` for every interface."""
    interfaces = {
        op["interface"] for op in get_all_operators() if op.get("interface")
    }
    out: dict[str, tuple[list[dict], list[dict]]] = {}
    for iface in interfaces:
        ins, outs = get_interface_io(iface)
        if ins or outs:
            out[iface] = (ins, outs)
    return out


def get_operator_ios_bulk() -> dict[str, tuple[list[dict], list[dict]]]:
    """``{operator_name: (inputs, outputs)}`` for every operator."""
    out: dict[str, tuple[list[dict], list[dict]]] = {}
    for op in get_all_operators():
        name = op.get("name")
        if not name or "." not in name:
            continue
        ins, outs = get_operator_io(name)
        if ins or outs:
            out[name] = (ins, outs)
    return out


def get_method_sequences_bulk() -> dict[str, list[str]]:
    """``{interface_name: [method_name, ...]}`` for every interface with a chain."""
    interfaces = {
        op["interface"] for op in get_all_operators() if op.get("interface")
    }
    out: dict[str, list[str]] = {}
    for iface in interfaces:
        seq = get_method_sequence(iface)
        if seq:
            out[iface] = seq
    return out


_all_kb_params_cache: dict[str, list[dict]] | None = None


def get_all_kb_operator_params() -> dict[str, list[dict]]:
    """Batch-fetch all KB-declared operator parameters.

    Builds a per-operator map by looping ``get_operator_parameters``
    over every operator. Cached for the lifetime of the process;
    invalidated by ``invalidate_all_kb_params_cache()`` after a
    runtime KB mutation (mitigation commit, FORCE_SEED reload).
    """
    global _all_kb_params_cache
    if _all_kb_params_cache is not None:
        return _all_kb_params_cache
    out: dict[str, list[dict]] = {}
    for op in get_all_operators():
        name = op.get("name")
        if not name:
            continue
        params = get_operator_parameters(name)
        if params:
            out[name] = params
    _all_kb_params_cache = out
    return _all_kb_params_cache


def invalidate_all_kb_params_cache() -> None:
    """Clear the cached result of ``get_all_kb_operator_params()``."""
    global _all_kb_params_cache
    _all_kb_params_cache = None


# ═══════════════════════════════════════════════════════════════════
# Cache management
# ═══════════════════════════════════════════════════════════════════

_CACHED_FUNCTIONS = [
    get_operator_interface,
    get_library_package_map,
    get_operator_import_path,
    get_all_interface_methods,
    get_method_sequence,
    get_operators_for_task,
    get_operator_family,
    get_operators_by_interface,
    get_metrics_for_task,
    get_metric_display_name,
    get_all_operators,
    get_operator_parameters,
    get_interface_io,
    get_operator_io,
    get_method_io,
    get_interface_attributes,
    get_operator_risks,
    get_mitigation_kb_spec,
    get_model_family,
    get_sensitive_families_for_risk,
    get_risks_surfaced_by_metric,
    get_all_pathways,
]


def invalidate_all_caches() -> None:
    """Clear every KB query cache in this module.

    Called when the snapshot is regenerated at runtime (FORCE_SEED)
    or a tenant overlay activates.
    """
    for fn in _CACHED_FUNCTIONS:
        fn.cache_clear()
    invalidate_all_kb_params_cache()
