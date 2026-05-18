"""
dorian/exec/registry.py
-----------------------
Job-kind registry — maps a stream entry's ``kind`` field to the
coroutine that performs the compute.

Each kind is a short identifier (``"dq_check:missing_values"``,
``"ranking_objective:compute_score"``, etc.). Registration is usually
done via the ``@register(kind)`` decorator in the module that defines
the compute function — the worker imports the package, the decorator
runs, the function is findable.

Handlers receive:

    async def fn(inputs: dict, *, job_id: str) -> dict

``inputs`` is the decoded ``inputs`` field of the job envelope (the
submitter-provided payload). The returned dict becomes the completion
event's payload and is also stored at ``exec:result:{job_id}``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

JobFn = Callable[..., Awaitable[Dict[str, Any]]]

_registry: dict[str, JobFn] = {}


def register(kind: str):
    """Decorator: register ``fn`` as the implementation of ``kind``.

    Double registration for the same kind is rejected to catch the
    "two modules define the same job kind" mistake at import time
    rather than at first invocation.
    """
    def deco(fn: JobFn) -> JobFn:
        if kind in _registry:
            raise ValueError(f"exec kind already registered: {kind}")
        _registry[kind] = fn
        return fn
    return deco


def get_registry() -> dict[str, JobFn]:
    """Return a shallow copy — callers must not mutate the registry."""
    return dict(_registry)


def get(kind: str) -> JobFn | None:
    return _registry.get(kind)
