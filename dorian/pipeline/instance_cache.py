"""
dorian/pipeline/instance_cache.py
----------------------------------
Process-level cache for expensive operator instances.

Guardrail operators (HuggingFace-based models) take minutes to initialise
on first run because they download and load large transformer models.
This module caches the constructed instances in a module-level dict so
that subsequent pipeline runs reuse the already-initialised objects
instead of re-creating them.

The cache is keyed by ``(operator_fqn, frozen_kwargs)`` — same class +
same constructor arguments always yields the same cached instance.

**sklearn-class caveat (fixed 2026-04-30):** an earlier revision of this
module returned the cached instance directly for *every* class,
including sklearn estimators. That looked safe ("the unfitted instance
is cheap to recreate but constructor resolution still benefits") but
it isn't: sklearn's ``fit`` mutates the instance in place
(``feature_names_in_``, ``mean_``, etc.). When two pipeline runs on
different datasets execute concurrently in the same process — the
common AutoML / xproduct shape — both runs got the same physical
imputer/scaler/encoder, both called ``fit(X_a)`` then ``fit(X_b)``,
and whichever run's ``transform`` ran second saw the *other* run's
fitted feature names. Symptom: ``feature names should match those that
were passed during fit. Feature names unseen at fit time: …``
flooding from ``imputer_cx_transform_2`` / ``preproc2_cx_transform_2``.

Fix: for sklearn-derived classes (anything with ``fit``), the cached
entry is a *template* — we ``sklearn.base.clone(template)`` on each
call so every run receives a fresh, unfitted estimator. The clone is
cheap (just copies hyperparameters), and we still save the
import-and-class-lookup cost on the cache hit. Non-sklearn classes
(guardrails, custom operators without ``fit``) keep the
return-the-cached-singleton behaviour because they're either
stateless or the call sites manage state explicitly.

Thread-safety: a ``threading.Lock`` guards creation so that concurrent
Dask threads don't double-initialise the same operator.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from backend.events import Event, emit


_cache: dict[str, object] = {}
_lock = threading.Lock()


def _cache_key(class_name: str, kwargs: dict[str, Any]) -> str:
    """Deterministic cache key from class name + sorted kwargs."""
    raw = json.dumps(
        {"cls": class_name, "kw": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_sklearn_estimator(obj: Any) -> bool:
    """True iff *obj* is an sklearn-style estimator that mutates on
    ``fit`` and supports ``sklearn.base.clone``. Detected by duck-type
    rather than ``isinstance(BaseEstimator)`` so non-sklearn classes
    that follow the same protocol (``get_params`` / ``set_params``)
    also get clone-on-each-call semantics."""
    return (
        callable(getattr(obj, "fit", None))
        and callable(getattr(obj, "get_params", None))
        and callable(getattr(obj, "set_params", None))
    )


def _fresh_copy(template: Any) -> Any:
    """Return an unfitted copy of *template*. Uses ``sklearn.base.clone``
    when available (it copies hyperparameters and discards fitted state
    — exactly what we want); falls back to deepcopy for non-sklearn
    classes that still pass the duck-type check."""
    try:
        from sklearn.base import clone as _sk_clone
        return _sk_clone(template, safe=False)
    except Exception:
        import copy
        return copy.deepcopy(template)


def get_or_create(class_name: str, cls: type, kwargs: dict[str, Any]) -> object:
    """Return a cached instance or create, cache, and return a new one.

    For sklearn-style estimators (anything with ``fit`` + ``get_params``
    / ``set_params``) we cache the instance as a *template* and hand
    out a fresh clone on every call — see the module docstring for why
    sharing the same instance across concurrent pipeline runs corrupts
    fitted state.

    Parameters
    ----------
    class_name : str
        Fully-qualified operator name (used for logging / key derivation).
    cls : type
        The class to instantiate on cache miss.
    kwargs : dict
        Constructor keyword arguments (must be JSON-serialisable via
        ``default=str`` — handles enum values, pathlib.Path, etc.).

    Returns
    -------
    object
        A (possibly cached) instance of *cls*. For sklearn estimators
        a fresh clone — never the shared template.
    """
    key = _cache_key(class_name, kwargs)

    # Fast path — no lock needed for read-only hit.
    if key in _cache:
        emit(Event("InstanceCacheHit", {"operator": class_name, "key": key}))
        cached = _cache[key]
        return _fresh_copy(cached) if _is_sklearn_estimator(cached) else cached

    # Slow path — double-checked locking.
    with _lock:
        if key in _cache:
            emit(Event("InstanceCacheHit", {"operator": class_name, "key": key}))
            cached = _cache[key]
            return _fresh_copy(cached) if _is_sklearn_estimator(cached) else cached

        emit(Event("InstanceCacheCreating", {"operator": class_name, "key": key}))
        instance = cls(**kwargs)
        _cache[key] = instance
        emit(Event("InstanceCacheCached", {"operator": class_name, "key": key}))

    return _fresh_copy(instance) if _is_sklearn_estimator(instance) else instance


def clear() -> None:
    """Evict all cached instances (e.g. on shutdown or config change)."""
    with _lock:
        count = len(_cache)
        _cache.clear()
    emit(Event("InstanceCacheCleared", {"evicted": count}))


def size() -> int:
    """Return the number of currently cached instances."""
    return len(_cache)
