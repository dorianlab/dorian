"""Auto-apply universally-beneficial mitigations to trial-side
pipelines.

Trial sessions (RL, AutoML, cross-product) are non-interactive — they
auto-apply the rewrites this module collects. User canvas pipelines
bypass this entirely; they get the same rewrites surfaced via the AI
Debugger accept/reject flow per the existing mitigation contract
(memory: feedback_mitigation_must_notify).

Currently the auto-apply set has one entry:

  * ``force_random_state`` — wires deterministic seed Parameters into
    every Operator that takes a ``random_state``-equivalent argument
    but has none wired. Required for the intermediates cache to
    classify multi-output ops as cacheable; without it,
    ``train_test_split``/``fit``/``predict`` Bypass and slot caching
    can never kick in.

Adding a new auto-apply mitigation: register the rewrite in
``_APPLY_REGISTRY`` (already done for force_random_state), seed it
into expdb.rewrites with ``auto_apply_for_trials=True``, and add its
factory call to ``_TRIAL_MITIGATIONS`` below.

Why a hardcoded list rather than a Postgres lookup at every call:
trial loops fire thousands of pipelines per minute. Going to expdb
on every pipeline costs ~1 ms and isn't justified for a set that
changes once a release. We refresh by editing this file. When the
list grows beyond a handful, swap to an LRU-cached lookup with a
TTL-bounded refresh.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from dorian.dag import DAG, Operator, Parameter
from dorian.pipeline.mitigation_rewrites import _APPLY_REGISTRY

try:
    from dorian.exec.intermediates_cache import random_state_param_for
except ImportError:  # pragma: no cover — exercised when dorian_native isn't loaded
    random_state_param_for = lambda _fqn: None  # type: ignore[assignment]

_log = logging.getLogger(__name__)


def apply_trial_mitigations(
    pipeline: DAG,
    meta: Mapping[str, Any] | None = None,
) -> DAG:
    """Run every auto-apply mitigation against ``pipeline``. Each
    mitigation iterates the DAG's Operator nodes in declaration order
    and applies its rewrite per match.

    Returns a new DAG (input is not mutated). The caller is expected
    to pass ``meta`` with at minimum a ``trial_id`` key — the
    ``force_random_state`` Apply uses it to derive deterministic
    seeds. When ``meta`` is None or trial_id is absent, the seed
    derivation falls back to a hash of the node id + op fqn (still
    deterministic, just not trial-distinguished — fine for canvas
    or one-off runs).

    Idempotent: a second call on the result is a no-op (every
    mitigation's Apply checks for already-wired state).
    """
    if not pipeline.nodes:
        return pipeline
    meta_dict = dict(meta) if meta else {}

    for mitigation_name, fixed_args in _TRIAL_MITIGATIONS:
        factory = _APPLY_REGISTRY.get(mitigation_name)
        if factory is None:
            continue
        if mitigation_name == "force_random_state":
            pipeline = _apply_force_random_state(pipeline, factory, meta_dict)
        else:
            # Future mitigations: pass the fixed args + walk the
            # pipeline. Each entry's `match_predicate` would decide
            # which nodes it applies to. For now only force_random_state
            # is registered, so the bespoke walker above is enough.
            apply_fn = factory(fixed_args)
            pipeline = apply_fn(
                pipeline, {"n": next(iter(pipeline.nodes.keys()))}, meta_dict,
            )
    return pipeline


_TRIAL_MITIGATIONS: tuple[tuple[str, dict], ...] = (
    ("force_random_state", {"through": "n", "seed_param": "random_state"}),
)


def _apply_force_random_state(
    pipeline: DAG, factory, meta: dict,
) -> DAG:
    """Bespoke walker for `force_random_state` because each
    Operator may need a different seed_param (per its KB-declared
    randomness arg). The factory is rebuilt per match with the
    correct ``seed_param``."""
    current = pipeline
    for node_id in list(pipeline.nodes.keys()):
        node = pipeline.nodes[node_id]
        if not isinstance(node, Operator):
            continue
        seed_param = random_state_param_for(node.name)
        if not seed_param:
            continue
        if _has_seed_wired(current, node_id, seed_param):
            continue
        apply_fn = factory({"through": "n", "seed_param": seed_param})
        new_dag = apply_fn(current, {"n": node_id}, meta)
        if new_dag is not None:
            current = new_dag
    return current


def _has_seed_wired(dag: DAG, dest_id: str, seed_param: str) -> bool:
    """Mirror of the Apply's idempotency check — runs before the
    Apply so we don't churn DAG copies on already-wired nodes."""
    for e in dag.edges:
        if e.destination != dest_id:
            continue
        if e.position != seed_param:
            continue
        src = dag.nodes.get(e.source)
        if isinstance(src, Parameter):
            return True
    return False


__all__ = ["apply_trial_mitigations"]
