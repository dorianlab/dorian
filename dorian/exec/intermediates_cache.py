"""Tier-2 intermediates cache — Python facade over the Rust ArrowStore.

The Rust crate (`engine/cache`) owns content-addressed storage on disk
(Arrow IPC files for tabular intermediates, opaque bytes for opaque
ones) and key derivation. This module is the Python adapter the
trial loop and the rust runner integration call into.

Usage shape — trial-side (RL, AutoML, cross-product):

    from dorian.exec.intermediates_cache import (
        ensure_open, lookup_subgraph, store_subgraph_outputs,
        cache_key_for_node,
    )

    ensure_open()                                        # idempotent
    cached = lookup_subgraph(dag, dataset_root_hash)
    # Returns (cached_outputs: dict[node_id, output],
    #          missing: list[node_id])
    # If `missing == []` the entire DAG was cached.
    # Otherwise the trial loop executes only the missing nodes,
    # then calls store_subgraph_outputs to persist them.

    if missing:
        outputs = run_pipeline(dag.subgraph(missing), preloaded=cached)
        store_subgraph_outputs(dag, outputs, dataset_root_hash)

The two key invariants the rust crate enforces (we rely on them
here, do not work around them):

1. **Bypass on undeclared seed.** When an operator declares a
   `random_state`-equivalent param but no Parameter node is wired,
   eligibility says Bypass and we never compute a cache key — that
   firing is non-reproducible by definition. The trial-side binder
   forces seeds before reaching this module.

2. **Content-addressed.** The cache key is derived from the operator
   FQN, version, canonicalised params, and the upstream node keys.
   Identical subgraphs across pipelines collapse onto the same
   stored entries automatically — the cross-trial reuse win the
   user's throughput target depends on.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Mapping

import pyarrow as pa
import pyarrow.ipc as pa_ipc

try:
    import dorian_native as _dn  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised in tests where dn not built
    _dn = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)


def is_available() -> bool:
    """True when the dorian_native extension exposes the cache surface."""
    return _dn is not None and hasattr(_dn, "cache_init")


_INITIALIZED = False


def ensure_open(path: str | None = None, max_gb: int | None = None) -> int:
    """Open the on-disk cache (idempotent). Returns the number of
    entries already present from a prior process — useful for
    "warm cache hits across restarts" reporting.

    `path` and `max_gb` override the env defaults
    (``DORIAN_CACHE_DIR``, ``DORIAN_CACHE_MAX_GB``). Pass ``None`` to
    take the env-resolved values.
    """
    global _INITIALIZED
    if not is_available():
        return 0
    n = _dn.cache_init(path, max_gb)
    if not _INITIALIZED:
        _log.info(
            "intermediates cache opened (entries=%d, root=%s)",
            n, path or os.environ.get("DORIAN_CACHE_DIR", "/tmp/dorian-cache"),
        )
        _INITIALIZED = True
    return n


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def cache_key_for_node(
    op_fqn: str,
    op_tasks: Iterable[str],
    params: Mapping[str, Any],
    upstream_keys_hex: Iterable[str],
    op_version: str | None = None,
    root_hash_hex: str | None = None,
) -> str:
    """Compute the cache key for one operator firing.

    `params` are the parameter bindings flowing into the node — keys
    are the edge ports / handle names (`"random_state"`, `"0"` for
    positional slot 0, etc.); values are the parameter payloads.

    The function delegates to the Rust `compute_key` so the canonical
    form is identical to what the rust runner uses internally.
    """
    if not is_available():
        raise RuntimeError("dorian_native not available — cache cannot derive keys")
    # Rust expects params as JSON list of [handle, value] pairs in
    # sorted-by-handle order so iteration order on the Python side
    # doesn't perturb the digest.
    pairs = sorted(
        ([k, _canonical_value(v)] for k, v in params.items()),
        key=lambda p: p[0],
    )
    params_json = json.dumps(pairs, separators=(",", ":"))
    return _dn.cache_compute_key(
        op_fqn,
        list(op_tasks),
        params_json,
        list(upstream_keys_hex),
        op_version,
        root_hash_hex,
    )


def _canonical_value(v: Any) -> Any:
    """Coerce to a JSON-roundtrippable form. Anything we can't make
    canonical is rejected — silently letting an opaque object through
    would cause the cache key to be unstable across runs.

    Float canonicalisation: round to 12 significant digits via
    ``%.12g`` formatting then re-parse. This absorbs the ulp-level
    noise that arithmetic produces (``0.1 + 0.2 = 0.30000000000000004``
    vs the literal ``0.3`` they should share a key on) without
    collapsing meaningfully different values. Bool guard is needed
    because Python's ``bool`` is a subclass of ``int``; without it
    ``isinstance(True, (int, float))`` is True and we'd round it.
    """
    if v is None or isinstance(v, bool) or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return _round_float(v)
    if isinstance(v, (list, tuple)):
        return [_canonical_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _canonical_value(x) for k, x in sorted(v.items())}
    raise TypeError(
        f"intermediates cache: parameter value of type {type(v).__name__} "
        f"is not canonicalisable; declare it as a Parameter node or coerce "
        f"to a primitive before binding"
    )


_DEFAULT_PRECISION_DIGITS = 12


def _resolve_precision(precision: int | None) -> int:
    """Resolve the float precision to use for canonicalisation.

    Order: explicit ``precision`` arg > ``DORIAN_PARAM_PRECISION``
    env var > 12 (full precision, the default that preserves
    every IEEE-754-distinguishable value).

    The lower the precision, the more neighbouring float values
    collapse to the same canonical form — drives the coarse-to-fine
    HPO strategy: AutoML / RL can launch early-exploration trials at
    precision=2 so adjacent hyperparameter samples share cache
    keys, then refine to precision=4 / 6 as the surrogate
    identifies promising regions. The trade-off: lower precision
    accidentally collides genuinely different values; higher
    precision misses caching opportunities for arithmetic-noise.
    """
    if precision is not None:
        return max(1, int(precision))
    raw = os.environ.get("DORIAN_PARAM_PRECISION")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_PRECISION_DIGITS


def _round_float(x: float, precision: int | None = None) -> float:
    """Round to N significant digits. NaN / inf pass through unchanged
    — they're already self-canonical."""
    import math
    if not math.isfinite(x):
        return x
    digits = _resolve_precision(precision)
    return float(f"{x:.{digits}g}")


def canonicalise_param_string(
    dtype: str, value: str, precision: int | None = None,
) -> str:
    """Normalise a Parameter's `value` string to its canonical form
    based on its declared dtype. Used in upstream-key derivation +
    in `force_random_state`'s seed derivation, both of which feed
    Parameter values into a hash and need them to be stable across
    trials that supply the same value via different float formatting.

    Float values get rounded to ``precision`` significant digits via
    ``%.{precision}g`` formatting. Default precision (when not passed
    + no env var) is 12 — the highest setting that still absorbs
    ulp-level arithmetic noise. AutoML / RL pass lower precision
    during coarse exploration; see ``_resolve_precision``.

    Other dtypes pass through (str / int / bool / eval are already
    canonical at the parser layer)."""
    if not value:
        return value
    if dtype in ("float", "Float"):
        try:
            digits = _resolve_precision(precision)
            return f"{float(value):.{digits}g}"
        except (ValueError, TypeError):
            return value
    return value


# ---------------------------------------------------------------------------
# Determinism gating
# ---------------------------------------------------------------------------

def random_state_param_for(op_fqn: str) -> str | None:
    """The seed parameter name (typically `'random_state'`) the
    operator at `op_fqn` accepts, or `None` if the operator is
    seed-free or the KB hasn't classified it.

    The trial-side binder calls this to know which arg to force when
    materialising a Trial's pipeline.
    """
    if not is_available():
        return None
    return _dn.cache_classify_random_state_param(op_fqn)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def get_arrow_table(key_hex: str) -> pa.Table | None:
    """Fetch an Arrow IPC payload as a pyarrow.Table. Returns None on
    miss. Zero-copy: pyarrow's IPC reader memory-maps the buffer the
    Rust side returns to us.
    """
    if not is_available():
        return None
    raw = _dn.cache_get_bytes(key_hex)
    if raw is None:
        return None
    try:
        reader = pa_ipc.open_file(pa.BufferReader(raw))
        return reader.read_all()
    except Exception as exc:
        _log.warning("intermediates cache: arrow decode failed for %s (%s)", key_hex, exc)
        return None


def get_opaque_bytes(key_hex: str) -> bytes | None:
    """Fetch a non-Arrow payload as raw bytes. The caller is
    responsible for deserialising (msgpack, pickle, etc.)."""
    if not is_available():
        return None
    return _dn.cache_get_bytes(key_hex)


def put_arrow_table(key_hex: str, table: pa.Table) -> None:
    """Serialize `table` as an Arrow IPC file payload and persist."""
    if not is_available():
        return
    sink = pa.BufferOutputStream()
    with pa_ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    buf = sink.getvalue()
    _dn.cache_put_arrow(key_hex, bytes(buf))


def put_opaque_bytes(key_hex: str, payload: bytes) -> None:
    """Persist arbitrary opaque bytes under `key_hex`. Used for
    sklearn estimators (msgpack-of-pickle), guardrail model state,
    anything not naturally Arrow-shaped."""
    if not is_available():
        return
    _dn.cache_put_opaque(key_hex, payload)


def stats() -> tuple[int, int]:
    """Returns (entry_count, total_bytes). Cheap — reads in-memory
    index counters."""
    if not is_available():
        return (0, 0)
    return _dn.cache_stats()


def is_enabled() -> bool:
    """Cache is enabled when the native surface is available AND
    `DORIAN_CACHE_ENABLED` is not explicitly off. Default: on. Setting
    `DORIAN_CACHE_ENABLED=0` (or `false`) disables it across the
    process — useful for benchmarking the no-cache baseline or for
    debug runs where you want every operator to actually fire."""
    if not is_available():
        return False
    flag = os.environ.get("DORIAN_CACHE_ENABLED", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Pipeline-graph elision
# ---------------------------------------------------------------------------

def elide_cached_nodes(
    graph: dict,
    pipeline,
    key_map: dict[str, str] | None = None,
    root_hash_hex: str | None = None,
) -> tuple[dict, dict[str, str], dict[str, int]]:
    """Walk a Dask-style task graph in topological order. For each
    operator node, compute its content-addressed cache key, look it
    up, and on hit replace the graph entry with the cached constant —
    so the downstream runner never executes the operator at all.

    Returns:
      * `new_graph` — the graph with cache hits replaced by constants
      * `node_keys` — mapping from prefixed Dask key to its computed
        cache key hex (descendants need it for upstream-key derivation)
      * `stats` — `{"hits": int, "misses": int, "skipped": int,
        "uncacheable": int}` for emitting telemetry / observability
        events. `skipped` counts non-operator nodes (parameters,
        snippets, slices) which the cache doesn't address. `uncacheable`
        counts operators whose KB classifies as Bypass (random_state
        unwired, non-deterministic by declaration).

    The graph is shape-preserved (same keys, same dependency
    structure) so callers don't need to special-case anything — the
    runner sees a graph where some operators are now constants. The
    runner's downstream slice entries (``{node_id}_{output_idx}``)
    will index into the cached constant exactly as they would index
    into a freshly-computed result.

    `pipeline` is the dorian `DAG` whose `build_dag_graph` produced
    `graph`. We need it to look up operator FQNs + params per node.

    `key_map` is the optional mapping from original DAG node IDs to
    prefixed graph keys (used by `run_pipeline` to avoid Dask key
    collisions across runs of the same pipeline). When provided we
    walk the graph keys; when absent we assume graph keys ARE node
    IDs.
    """
    if not is_enabled():
        return graph, {}, {"hits": 0, "misses": 0, "skipped": 0, "uncacheable": 0}

    # Inverse map: graph_key → original_node_id. Falls back to
    # identity when no key_map.
    if key_map:
        inv_map = {v: k for k, v in key_map.items()}
    else:
        inv_map = {k: k for k in graph}

    # Topological walk over graph keys. We cannot rely on dict
    # insertion order alone — handle dependencies explicitly.
    order = _topo_order(graph)

    new_graph: dict = dict(graph)
    node_keys: dict[str, str] = {}
    hits = misses = skipped = uncacheable = 0

    for graph_key in order:
        entry = graph.get(graph_key)
        # Non-tuple entries are constants — skip.
        if not isinstance(entry, tuple) or not entry:
            skipped += 1
            continue

        # Slice synthesizers (`{parent_graph_key}_{slot_index}`)
        # produced by `build_dag_graph` for multi-output ops. Their
        # cache key is derived from the parent operator's key + the
        # slot index — see `_slot_key`. If the parent has been cached
        # AND the slot table is present, we can short-circuit BOTH
        # the parent op AND this slice synthesizer.
        slice_parent, slice_idx = _try_parse_slice_key(graph_key, node_keys)
        if slice_parent is not None:
            slot_hex = _slot_key(node_keys[slice_parent], slice_idx)
            slot_table = get_arrow_table(slot_hex)
            if slot_table is not None:
                new_graph[graph_key] = _arrow_to_native(slot_table)
                node_keys[graph_key] = slot_hex
                hits += 1
            else:
                # Slot not cached — leave the slice entry as-is so
                # the runner's `_slice` helper indexes into the
                # parent's (possibly cached, possibly fresh) tuple.
                skipped += 1
            continue

        original_id = inv_map.get(graph_key, graph_key)
        node = pipeline.nodes.get(original_id) if hasattr(pipeline, "nodes") else None
        if node is None:
            # Try the pre-slice base.
            base = original_id.rsplit("_", 1)[0]
            node = pipeline.nodes.get(base) if hasattr(pipeline, "nodes") else None
            if node is None:
                skipped += 1
                continue

        # Only Operator nodes can be cached at this layer.
        # Snippets are user-authored code; their canonicalisation is
        # tricky and out-of-scope for the first cut.
        from dorian.dag import Operator
        if not isinstance(node, Operator):
            skipped += 1
            continue

        op_fqn = node.name
        seed_param = random_state_param_for(op_fqn)

        # Eligibility: when KB declares a seed param but no Parameter
        # is wired to that handle, we Bypass. The force_random_state
        # mitigation (seeded into expdb.rewrites) handles this when
        # auto-applied; until then we just decline to cache.
        params, has_seed = _params_and_seed_check(pipeline, original_id, seed_param)
        if seed_param and not has_seed:
            uncacheable += 1
            continue

        # Upstream cache keys: walk the graph's deps and pull from
        # node_keys. Slice synthesizers (`{parent}_{slot}`) get a
        # SLOT-specific key so two consumers indexing different slots
        # of the same multi-output op don't collide on the upstream
        # hash. Without that distinction, a node that consumes
        # `train_test_split[0]` (X_train) and a node that consumes
        # `train_test_split[2]` (y_train) would derive identical
        # cache keys downstream.
        _fn, *deps = entry
        upstream_hex: list[str] = []
        for d in deps:
            if isinstance(d, str):
                if d in node_keys:
                    upstream_hex.append(node_keys[d])
                    continue
                slice_parent_dep, slice_dep_idx = _try_parse_slice_key(
                    d, node_keys,
                )
                if slice_parent_dep is not None:
                    upstream_hex.append(
                        _slot_key(node_keys[slice_parent_dep], slice_dep_idx),
                    )
                    continue
                # Unknown upstream — usually a Parameter satellite.
                # Hash the Parameter's `(name, dtype, value)` rather
                # than the graph entry tuple (which contains a
                # resolve-closure whose `repr` includes a memory
                # address — unstable across trials, defeats every
                # cross-pipeline cache hit).
                upstream_hex.append(
                    _upstream_key_for_dep(d, pipeline, inv_map, graph),
                )
            # Non-string deps (slice indices) don't enter the upstream
            # vector — they're args of the slice helper, not data deps.

        try:
            key_hex = cache_key_for_node(
                op_fqn=op_fqn,
                op_tasks=list(getattr(node, "tasks", []) or []),
                params=params,
                upstream_keys_hex=upstream_hex,
                root_hash_hex=root_hash_hex,
            )
        except TypeError:
            # Parameter values not canonicalisable — skip caching.
            uncacheable += 1
            continue

        node_keys[graph_key] = key_hex

        # Lookup
        table = get_arrow_table(key_hex)
        if table is not None:
            new_graph[graph_key] = _arrow_to_native(table)
            hits += 1
            continue
        # Try opaque payload (sklearn estimator etc.) — None on miss
        op_bytes = get_opaque_bytes(key_hex) if False else None  # noqa: SIM222
        # Disabled by default: deserialising a pickle from cache
        # without strict KB-side opt-in is a code-execution risk
        # the first cut declines to take. Enable per-operator via
        # an `intermediate_format` predicate in a follow-up.
        if op_bytes is not None:
            new_graph[graph_key] = _opaque_to_native(op_bytes)
            hits += 1
            continue
        misses += 1

    if hits + misses + uncacheable > 0:
        _log.debug(
            "intermediates_cache.elide: hits=%d misses=%d uncacheable=%d skipped=%d",
            hits, misses, uncacheable, skipped,
        )
    return new_graph, node_keys, {
        "hits": hits, "misses": misses, "skipped": skipped, "uncacheable": uncacheable,
    }


def _try_parse_slice_key(
    graph_key: str, node_keys: Mapping[str, str],
) -> tuple[str | None, int]:
    """If `graph_key` matches `<some_parent>_<integer>` AND the
    parent has already been keyed, return `(parent_key, slot)`.
    Otherwise `(None, 0)`. Parent must be in `node_keys` because we
    need its cache hex to derive the slot key.
    """
    if "_" not in graph_key:
        return None, 0
    base, _, suffix = graph_key.rpartition("_")
    try:
        slot = int(suffix)
    except ValueError:
        return None, 0
    if base not in node_keys:
        return None, 0
    return base, slot


def _slot_key(parent_key_hex: str, slot: int) -> str:
    """Derive a slot's cache key from its parent operator's key. The
    same scheme is used on both the put side (when storing each
    element of a multi-output tuple after the parent runs) and the
    get side (when a slice synthesizer in the graph looks up its
    specific slot). Keeping the derivation deterministic + symmetric
    means a producer/consumer can race without a shared registry —
    the cache keys themselves carry the relationship.
    """
    import hashlib
    h = hashlib.sha256()
    h.update(b"slot:")
    h.update(parent_key_hex.encode("ascii"))
    h.update(b"\x00")
    h.update(str(slot).encode("ascii"))
    return h.hexdigest()


def store_node_outputs(
    results: Mapping[str, Any],
    node_keys: Mapping[str, str],
) -> int:
    """Persist freshly-computed outputs for every cache-miss node.

    `results` is the runner's keyed output map. `node_keys` is the
    mapping returned by `elide_cached_nodes` — only nodes whose
    upstream chain cleared the eligibility gates have a key. Each
    matched key is hashed only once, so re-storing on a hit is a
    no-op (the file is already on disk).

    Returns the count of entries stored. Errors during put don't
    abort — the runner already produced its result; cache just
    misses the chance to memoise it.
    """
    if not is_enabled():
        return 0
    stored = skipped_kind = missing = 0
    for graph_key, key_hex in node_keys.items():
        if graph_key not in results:
            missing += 1
            continue
        value = results[graph_key]
        # Multi-output operators return tuples/lists. Store each
        # element under a slot-derived key so downstream slice
        # synthesizers (`{node}_{i}` in the Dask graph) can look up
        # their specific slot. Sklearn method shortcuts (`fit`,
        # `predict`, `transform`) all return `(instance, result)`,
        # and `train_test_split` returns the 4-tuple. Caching the
        # data slots is the high-leverage win — the instance slot
        # is opaque and skipped.
        if isinstance(value, (tuple, list)):
            slot_stored = False
            for slot, elem in enumerate(value):
                table = _native_to_arrow(elem)
                if table is None:
                    continue
                try:
                    put_arrow_table(_slot_key(key_hex, slot), table)
                    slot_stored = True
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "intermediates cache: slot put failed for %s[%d] (%s)",
                        key_hex[:8], slot, exc,
                    )
            if slot_stored:
                stored += 1
            else:
                skipped_kind += 1
            continue
        # Single-output node (DataFrame, ndarray, scalar, opaque…).
        table = _native_to_arrow(value)
        if table is None:
            # Non-tabular output — first-cut policy: skip. (See note
            # on opaque-bytes path in `elide_cached_nodes`.)
            skipped_kind += 1
            continue
        try:
            put_arrow_table(key_hex, table)
            stored += 1
        except Exception as exc:  # noqa: BLE001 — non-fatal
            _log.warning("intermediates cache: put failed for %s (%s)", key_hex[:8], exc)
    if stored or skipped_kind or missing:
        _log.debug(
            "intermediates_cache.store: stored=%d skipped_kind=%d missing=%d",
            stored, skipped_kind, missing,
        )
    return stored


# ---------------------------------------------------------------------------
# Conversion helpers — pyarrow ↔ pandas/numpy
# ---------------------------------------------------------------------------

def _native_to_arrow(value: Any) -> pa.Table | None:
    """Best-effort conversion of pipeline outputs to an Arrow table.
    Returns None for outputs we don't know how to canonicalise (sklearn
    estimators, multi-output tuples, scalars, …) — caller treats that
    as "non-cacheable in v1"."""
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return None
    if isinstance(value, pa.Table):
        return value
    if isinstance(value, pd.DataFrame):
        try:
            return pa.Table.from_pandas(value, preserve_index=False)
        except Exception:
            return None
    if isinstance(value, pd.Series):
        try:
            return pa.Table.from_pandas(value.to_frame(), preserve_index=False)
        except Exception:
            return None
    if isinstance(value, np.ndarray):
        try:
            if value.ndim == 1:
                return pa.table({"_v": value})
            if value.ndim == 2:
                cols = {f"_c{i}": value[:, i] for i in range(value.shape[1])}
                return pa.table(cols)
        except Exception:
            return None
    return None


def _arrow_to_native(table: pa.Table) -> Any:
    """Reverse of `_native_to_arrow`. Returns the most natural Python
    shape for the table — a DataFrame for general tables, a 1-D
    ndarray for ``_v``-only tables, a 2-D ndarray for ``_c{i}``
    tables. Callers only see DataFrames or ndarrays here, never raw
    pyarrow Tables, so downstream operators don't need to know about
    the cache layer."""
    schema_names = table.schema.names
    if schema_names == ["_v"]:
        col = table.column("_v")
        return col.to_numpy(zero_copy_only=False)
    if all(n.startswith("_c") for n in schema_names):
        try:
            import numpy as np
            return np.column_stack([
                table.column(n).to_numpy(zero_copy_only=False) for n in schema_names
            ])
        except Exception:
            pass
    return table.to_pandas()


def _opaque_to_native(_b: bytes) -> Any:
    """Placeholder. Opaque-bytes path is gated off in v1 — returning
    raw bytes here would break callers expecting a DataFrame. Once
    the KB declares per-operator deserialisation modes, this picks
    them up."""
    return _b


def _params_and_seed_check(
    pipeline, node_id: str, seed_param: str | None,
) -> tuple[dict, bool]:
    """Walk Parameter neighbours of `node_id`. Returns the
    canonicalised params dict (suitable for `cache_key_for_node`)
    plus whether a Parameter is wired to the seed handle.

    Float values are canonicalised to 12-significant-digit form so
    `0.1 + 0.2` and the literal `0.3` collapse to the same key.
    """
    from dorian.dag import Parameter as _Parameter
    params: dict = {}
    has_seed = False
    for edge in getattr(pipeline, "edges", []):
        if edge.destination != node_id:
            continue
        src = pipeline.nodes.get(edge.source) if hasattr(pipeline, "nodes") else None
        if not isinstance(src, _Parameter):
            continue
        handle = str(edge.position)
        if seed_param and handle == seed_param:
            has_seed = True
        # Param value goes into the key as `(dtype, value)` so the
        # int 1 vs string "1" don't collide.
        params[handle] = {
            "dtype": src.dtype,
            "value": canonicalise_param_string(str(src.dtype), str(src.value)),
        }
    return params, has_seed


def _topo_order(graph: dict) -> list[str]:
    """Kahn's algorithm over the graph's (callable, *deps) entries.
    Stable order — descendants follow ancestors. Nodes whose deps
    aren't in the graph are appended at the front (they're either
    constants or Parameter satellites)."""
    indeg: dict[str, int] = {k: 0 for k in graph}
    for k, entry in graph.items():
        if not isinstance(entry, tuple) or not entry:
            continue
        _fn, *deps = entry
        for d in deps:
            if isinstance(d, str) and d in graph:
                indeg[k] = indeg.get(k, 0) + 1
    ready = [k for k, n in indeg.items() if n == 0]
    out: list[str] = []
    children: dict[str, list[str]] = {k: [] for k in graph}
    for k, entry in graph.items():
        if not isinstance(entry, tuple) or not entry:
            continue
        _fn, *deps = entry
        for d in deps:
            if isinstance(d, str) and d in graph:
                children[d].append(k)
    while ready:
        k = ready.pop(0)
        out.append(k)
        for c in children.get(k, []):
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
    # Append any leftover (cycle — shouldn't happen on DAGs).
    for k in graph:
        if k not in out:
            out.append(k)
    return out


def _upstream_key_for_dep(
    dep: str, pipeline, inv_map: Mapping[str, str], graph: Mapping[str, Any],
) -> str:
    """Stable upstream-key for a non-Operator dependency.

    The two cases worth distinguishing:

      * **Parameter node** — the most common one. Hash the
        `(name, dtype, value)` triple. This is the field set the
        cache key derivation uses everywhere else, and it's
        invariant across pipeline rebuilds (UUIDs change, values
        don't).

      * **Anything else** — slice synth that wasn't recognised, a
        Snippet output, etc. Fall back to ``_constant_key_hex`` on
        whatever's in the graph entry. May be unstable for closures
        but it's the best we can do without operator metadata.
    """
    from dorian.dag import Parameter
    original_id = inv_map.get(dep, dep)
    node = None
    if hasattr(pipeline, "nodes"):
        node = pipeline.nodes.get(original_id)
        if node is None:
            base = original_id.rsplit("_", 1)[0]
            node = pipeline.nodes.get(base)
    if isinstance(node, Parameter):
        import hashlib
        h = hashlib.sha256()
        h.update(b"param:")
        h.update(node.name.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(node.dtype).encode("utf-8"))
        h.update(b"\x00")
        h.update(canonicalise_param_string(
            str(node.dtype), str(node.value),
        ).encode("utf-8"))
        return h.hexdigest()
    return _constant_key_hex(graph.get(dep, dep))


def _constant_key_hex(value: Any) -> str:
    """Stable hex digest of a constant (int, str, float, list, dict).
    Used to derive an upstream key for Parameter / literal entries
    that don't have an associated cache key. Non-hashable inputs get
    a digest of `repr(value)` — collisions are theoretically
    possible but in practice the params are simple types."""
    import hashlib
    h = hashlib.sha256()
    try:
        canon = json.dumps(_canonical_value(value), separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        canon = repr(value)
    h.update(b"const:")
    h.update(canon.encode("utf-8"))
    return h.hexdigest()


__all__ = [
    "ensure_open",
    "is_available",
    "is_enabled",
    "cache_key_for_node",
    "random_state_param_for",
    "get_arrow_table",
    "get_opaque_bytes",
    "put_arrow_table",
    "put_opaque_bytes",
    "stats",
    "elide_cached_nodes",
    "store_node_outputs",
]
