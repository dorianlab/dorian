"""
dorian/pipeline/state.py
-------------------------
Expansion rule for the ``dorian.io.state`` platform operator.

``dorian.io.state`` resolves session-scoped state from Redis at expansion
time and replaces itself with a ``Parameter`` node containing the resolved
value.  The Dask worker never touches Redis.

Security model
--------------
- **Allowlisted keys only** — ``_ALLOWED_STATE_KEYS`` maps user-facing
  namespaced key names to resolver functions.  Unknown keys cause a hard
  failure.  The key Parameter value is never interpolated into Redis key
  patterns.
- **Session scoping** — all Redis reads go through ``RedisKeys.*``
  factory methods with ``did`` / ``session`` extracted from the session's
  own meta blob.  No path from user input to arbitrary Redis keys.
- **System state exclusion** — vault secrets, execution internals, WS
  streams, and cancel flags have no resolvers and cannot be accessed.
- **Safe serialization** — ``repr()`` produces Python literals consumed
  by ``ast.literal_eval`` (the ``eval`` dtype handler).  No arbitrary
  code execution.

Usage in the pipeline DAG::

    Operator(name="dorian.io.state")
    + Parameter(name="key", dtype="str", value="dataset.features")
    + Parameter(name="dataset", dtype="str", value="housing")   # optional

The ``key`` Parameter is required.  The ``dataset`` Parameter is optional
and used only for ``dataset.*`` keys — it specifies the dataset by
alias (defaults to filename).  ``session.*`` keys ignore it.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any, Callable, Dict

from dorian.code.parsing.rule import Apply, RewriteRule
from dorian.dag import DAG, Edge, Node, Parameter
from dorian.infra.keys import RedisKeys
from dorian.pipeline.parser import match
from backend.envs import redis
from backend.events import Event, emit


# ---------------------------------------------------------------------------
# Resolver helpers
# ---------------------------------------------------------------------------

def _did_from_meta(session_meta: dict, dataset_alias: str | None) -> str | None:
    """Resolve a dataset alias to its internal ``did``.

    Current model: single dataset per session — ``meta["dataset"]["did"]``.
    If *dataset_alias* is provided, it is matched against the basename of
    ``meta["dataset"]["fpath"]``.  A mismatch emits a warning but still
    returns the only available ``did`` (graceful degradation).

    Future model: ``meta["datasets"]`` list with user-defined aliases.
    """
    ds = session_meta.get("dataset") or {}
    did = ds.get("did")
    if not did:
        return None

    if dataset_alias is not None:
        fpath = ds.get("fpath", "")
        name = PurePosixPath(fpath).stem if fpath else ""
        if dataset_alias != name and dataset_alias != did:
            emit(Event("StateDatasetAliasMismatch", {
                "alias": dataset_alias,
                "available": name,
                "did": did,
                "note": "falling back to session dataset (single-dataset model)",
            }))
    return did


def _resolve_dataset_key(redis_key_name: str) -> Callable:
    """Factory: build a resolver that reads a ``dataset:{did}:*`` Redis key."""
    _factory_map = {
        "feature_columns": RedisKeys.dataset_feature_columns,
        "target_columns": RedisKeys.dataset_target_columns,
        "protected_attributes": RedisKeys.protected_attributes,
    }
    factory = _factory_map[redis_key_name]

    def resolver(session: str, meta: dict, did: str | None) -> Any:
        if not did:
            return None
        raw = redis.get(factory(did))
        return json.loads(raw) if raw else None

    resolver.__qualname__ = f"_resolve_dataset_key({redis_key_name!r})"
    return resolver


def _resolve_dataset_meta(*path: str) -> Callable:
    """Factory: build a resolver that reads a nested path under ``meta["dataset"]``."""
    def resolver(session: str, meta: dict, did: str | None) -> Any:
        obj: Any = meta.get("dataset") or {}
        for key in path:
            if not isinstance(obj, dict):
                return None
            obj = obj.get(key)
        return obj

    resolver.__qualname__ = f"_resolve_dataset_meta({'.'.join(path)!r})"
    return resolver


def _resolve_session_meta(*path: str) -> Callable:
    """Factory: build a resolver that reads a top-level session meta field."""
    def resolver(session: str, meta: dict, did: str | None) -> Any:
        obj: Any = meta
        for key in path:
            if not isinstance(obj, dict):
                return None
            obj = obj.get(key)
        return obj

    resolver.__qualname__ = f"_resolve_session_meta({'.'.join(path)!r})"
    return resolver


# ---------------------------------------------------------------------------
# Allowlist — the ONLY keys the state operator can resolve
# ---------------------------------------------------------------------------
# Security: each entry maps a user-facing namespaced name to a resolver
# function.  The user-supplied key value is checked against this dict
# BEFORE any Redis access.  Unknown keys are rejected.

_ALLOWED_STATE_KEYS: Dict[str, Callable] = {
    "dataset.features":             _resolve_dataset_key("feature_columns"),
    "dataset.target":               _resolve_dataset_key("target_columns"),
    "dataset.protected_attributes": _resolve_dataset_key("protected_attributes"),
    "dataset.profile":              _resolve_dataset_meta("profile"),
    "session.task":                 _resolve_session_meta("selectedDataScienceTask"),
    "session.eval":                 _resolve_session_meta("selectedEvaluationProcedureName"),
}


# ---------------------------------------------------------------------------
# Expansion function
# ---------------------------------------------------------------------------

def _expand_state(dag: DAG, mapping: Dict[str, str], meta: Dict[str, Any]) -> DAG:
    """Replace ``dorian.io.state`` + its Parameters with a resolved value.

    Supports two node shapes:

    **Legacy (Operator + Parameters)**::

        Operator(name="dorian.io.state") ← Parameter(name="key", value="dataset.features")

    **Compact (single Parameter)**::

        Parameter(name="dorian.io.state", dtype="state", value="dataset.features")

    The resolved value is serialised via ``repr()`` into a
    ``Parameter(dtype="eval")`` node so ``ast.literal_eval`` can reconstruct
    the Python object at Dask execution time.
    """
    nid = mapping["n"]
    node = dag.nodes[nid]

    # ── 1. Extract the state key ────────────────────────────────────────
    consumed_param_ids: set[str] = set()
    dataset_alias: str | None = None

    if isinstance(node, Parameter) and node.dtype == "state":
        # Compact form: key is stored directly in the Parameter value
        key_value: str | None = node.value
    else:
        # Legacy form: find incoming Parameter(name="key")
        key_value = None
        for e in dag.edges:
            if e.destination != nid:
                continue
            src = dag.nodes.get(e.source)
            if not isinstance(src, Parameter):
                continue
            if src.name == "key":
                key_value = src.value
                consumed_param_ids.add(e.source)
            elif src.name == "dataset":
                dataset_alias = src.value
                consumed_param_ids.add(e.source)

    if key_value is None:
        emit(Event("StateExpansionFailed", {
            "node": nid,
            "reason": "no state key — attach Parameter(name='key') or use dtype='state'",
        }))
        return dag  # caught by dorian.* guard

    # ── 2. Validate against allowlist (SECURITY: no interpolation) ──────
    if key_value not in _ALLOWED_STATE_KEYS:
        emit(Event("StateExpansionFailed", {
            "node": nid,
            "key": key_value,
            "reason": f"key {key_value!r} not in allowlist",
            "allowed": list(_ALLOWED_STATE_KEYS),
        }))
        return dag  # caught by dorian.* guard

    # ── 3. Read session meta ────────────────────────────────────────────
    session = meta.get("session", "")
    raw_meta = redis.get(RedisKeys.session_meta(session))
    session_meta = json.loads(raw_meta) if raw_meta else {}

    # ── 4. Resolve dataset alias → did (for dataset.* keys) ────────────
    did: str | None = None
    if key_value.startswith("dataset."):
        did = _did_from_meta(session_meta, dataset_alias)

    # ── 5. Call resolver ────────────────────────────────────────────────
    resolver = _ALLOWED_STATE_KEYS[key_value]
    resolved = resolver(session, session_meta, did)

    # ── 6. Serialise to Parameter value ─────────────────────────────────
    if resolved is None:
        emit(Event("StateKeyMissing", {
            "key": key_value, "session": session,
            "note": "downstream operators will receive None",
        }))
        param_dtype, param_value = "eval", "None"
    elif isinstance(resolved, str):
        param_dtype, param_value = "str", resolved
    else:
        # list, dict → Python literal via repr() for ast.literal_eval
        param_dtype, param_value = "eval", repr(resolved)

    # ── 7. Build replacement DAG ────────────────────────────────────────
    value_id = f"state_{nid}"
    remove_ids = {nid} | consumed_param_ids

    outgoing = [
        (e.destination, e.position, e.output)
        for e in dag.edges if e.source == nid
    ]

    new_nodes = {k: v for k, v in dag.nodes.items() if k not in remove_ids}
    new_nodes[value_id] = Parameter(name=key_value, dtype=param_dtype, value=param_value)

    new_edges = [
        e for e in dag.edges
        if e.source not in remove_ids and e.destination not in remove_ids
    ]
    for dst, pos, out in outgoing:
        new_edges.append(Edge(value_id, dst, position=pos, output=out))

    return DAG(nodes=new_nodes, edges=new_edges)


# ---------------------------------------------------------------------------
# Rule + public entry point
# ---------------------------------------------------------------------------

_STATE_OPERATOR_RULE = RewriteRule(
    pattern=DAG(
        nodes={"n": Node(type="Operator", text=r"dorian\.io\.state")},
        edges=[],
    ),
    description="expand dorian.io.state Operator to a resolved session-state Parameter",
    transformations=[Apply(f=_expand_state)],
)

# Keep legacy name for any external references
STATE_EXPANSION_RULE = _STATE_OPERATOR_RULE


def expand_state_refs(pipeline: DAG, session: str) -> DAG:
    """Expand all ``dorian.io.state`` nodes in the pipeline.

    Handles two shapes:
    - **Legacy**: ``Operator(name="dorian.io.state")`` + ``Parameter(name="key")``
    - **Compact**: ``Parameter(name="dorian.io.state", dtype="state", value="…")``

    Called from ``run_pipeline`` after ``expand_dataset_refs`` (so session
    meta with ``did`` is available) and before ``expand_compound_operators``.

    With ``DORIAN_USE_RUST_EXPAND_STATE=1`` the resolver allowlist still
    runs python-side (it touches Redis); the resulting ``[node_id, key,
    dtype, value]`` records feed the rust ``expand_state_refs`` for the
    actual graph mutation.
    """
    import os as _os
    from dorian.pipeline.transforms import sync_apply

    if _os.environ.get("DORIAN_USE_RUST_EXPAND_STATE", "").lower() in ("1", "true", "yes", "on"):
        try:
            return _expand_state_refs_rust(pipeline, session)
        except Exception as exc:  # noqa: BLE001
            try:
                emit(Event("ExpandStateRustFallback", {"error": str(exc)}))
            except Exception:
                pass

    # 1. Expand legacy Operator-based state refs via pattern matching
    dag = sync_apply(_STATE_OPERATOR_RULE, pipeline, {"session": session})

    # 2. Expand compact Parameter(dtype="state") nodes directly
    #    (no pattern rule needed — just iterate and replace in-place)
    changed = True
    while changed:
        changed = False
        for nid, node in list(dag.nodes.items()):
            if isinstance(node, Parameter) and node.dtype == "state":
                dag = _expand_state(dag, {"n": nid}, {"session": session})
                changed = True
                break  # dag mutated, restart iteration

    return dag


def _expand_state_refs_rust(pipeline: DAG, session: str) -> DAG:
    """Resolve every state placeholder python-side, hand the resulting
    record list to ``dorian_native.expand_state_refs`` for the graph
    mutation. Resolvers stay python-side because they hit Redis.
    """
    import dorian_native  # type: ignore

    raw_meta = redis.get(RedisKeys.session_meta(session))
    session_meta = json.loads(raw_meta) if raw_meta else {}

    resolutions: list[dict] = []
    for nid, node in list(pipeline.nodes.items()):
        # Discover placeholders + their consumed metadata.
        if isinstance(node, Parameter) and node.dtype == "state":
            key_value = node.value
            dataset_alias = None
        elif _is_state_operator(node):
            key_value, dataset_alias = _extract_state_inputs(pipeline, nid)
        else:
            continue
        if key_value is None or key_value not in _ALLOWED_STATE_KEYS:
            # Match python fallback: emit + skip — caller's
            # ``dorian.*`` guard catches the unresolved placeholder.
            if key_value is None:
                emit(Event("StateExpansionFailed", {
                    "node": nid,
                    "reason": "no state key",
                }))
            else:
                emit(Event("StateExpansionFailed", {
                    "node": nid, "key": key_value,
                    "reason": f"key {key_value!r} not in allowlist",
                    "allowed": list(_ALLOWED_STATE_KEYS),
                }))
            continue
        did = (
            _did_from_meta(session_meta, dataset_alias)
            if key_value.startswith("dataset.")
            else None
        )
        resolved = _ALLOWED_STATE_KEYS[key_value](session, session_meta, did)
        if resolved is None:
            emit(Event("StateKeyMissing", {
                "key": key_value, "session": session,
            }))
            dtype, value = "eval", "None"
        elif isinstance(resolved, str):
            dtype, value = "str", resolved
        else:
            dtype, value = "eval", repr(resolved)
        resolutions.append({
            "node_id": nid,
            "key": key_value,
            "dtype": dtype,
            "value": value,
        })

    if not resolutions:
        return pipeline

    expanded = dorian_native.expand_state_refs(
        json.dumps(pipeline.to_json_dict()),
        json.dumps(resolutions),
    )
    return DAG.from_json_dict(json.loads(expanded))


def _is_state_operator(node) -> bool:
    """``Operator(name="dorian.io.state")`` only — for the legacy form."""
    from dorian.dag import Operator as _Operator
    return isinstance(node, _Operator) and node.name == "dorian.io.state"


def _extract_state_inputs(pipeline: DAG, nid: str) -> tuple[str | None, str | None]:
    """Pull ``key`` + ``dataset`` Parameter values from the legacy form."""
    key_value: str | None = None
    dataset_alias: str | None = None
    for e in pipeline.edges:
        if e.destination != nid:
            continue
        src = pipeline.nodes.get(e.source)
        if not isinstance(src, Parameter):
            continue
        if src.name == "key":
            key_value = src.value
        elif src.name == "dataset":
            dataset_alias = src.value
    return key_value, dataset_alias
