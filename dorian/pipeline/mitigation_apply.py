"""
dorian/pipeline/mitigation_apply.py
-----------------------------------
Shared autonomous mitigation-apply helpers.

Two consumers, one apply path:

* **User-facing AI Debugger** (``dorian.event.handlers.risk_checks``) —
  matches against ``EXECUTION_ERROR_PATTERNS`` and emits a *suggestion
  card*. The user clicks Apply, which re-routes through
  ``apply_mitigation`` (still in ``risk_checks``) — that path uses the
  pipeline JSON the SPA tracks.
* **Autonomous runners** — the cross-product evaluator
  (``scripts.cross_product_eval``) and the RL error-mitigation loop
  (``dorian.event.handlers.rl_error_mitigation``) — never have a UI in
  the loop. They auto-apply the same mitigation directly to a DAG via
  the helpers in this module and re-execute.

Importantly, the *user-facing* and *autonomous* paths share the same
:class:`ExecutionErrorPattern` registry, the same ``doc_rewrites``
collection, and the same set of named ``Apply`` primitives in
``mitigation_rewrites._APPLY_REGISTRY``. The only divergence is who
gates application (``apply_mitigation`` waits for an explicit user
accept; the autonomous helpers fire on their own).

If a future "auto-apply for humans" mode lands, it MUST go through
``apply_mitigation`` so both ``pipeline/rewritten`` and
``ui/mitigation-applied`` notifications fire — silent auto-applies are
not acceptable for human sessions. The autonomous helpers below
explicitly skip those notification emissions because the consumer
("system" uid / ``rl:*`` session / cross-product runner subprocess)
has no WebSocket subscriber.
"""
from __future__ import annotations

import re
from typing import Any

from dorian.dag import DAG, Edge, Operator, Parameter
from dorian.knowledge.execution_errors import (
    EXECUTION_ERROR_PATTERNS,
    ExecutionErrorPattern,
)


# ═══════════════════════════════════════════════════════════════════════
# Pattern matching
# ═══════════════════════════════════════════════════════════════════════

def match_execution_error(
    error_msg: str | None,
    *,
    operator_fqn: str | None = None,
) -> tuple[ExecutionErrorPattern, re.Match[str]] | None:
    """Find the first ``ExecutionErrorPattern`` matching *error_msg*.

    When *operator_fqn* is provided, patterns whose ``operators`` set is
    non-empty are filtered by membership; an empty ``operators`` set
    means "any operator" and always passes. Returns the match object so
    callers can substitute named groups into ``description_short`` /
    compute parameter fixes via ``compute_fix``.
    """
    if not error_msg:
        return None
    for pat in EXECUTION_ERROR_PATTERNS:
        if pat.operators and operator_fqn and operator_fqn not in pat.operators:
            continue
        m = pat.pattern.search(error_msg)
        if m is not None:
            return pat, m
    return None


# ═══════════════════════════════════════════════════════════════════════
# Apply primitives — pure DAG mutations, no I/O, no notifications.
# ═══════════════════════════════════════════════════════════════════════

def apply_parameter_change_to_dag(
    dag: DAG,
    *,
    operator_fqn: str,
    param_name: str,
    fix_value: str,
) -> DAG | None:
    """Find the Parameter node attached to *operator_fqn* under the kwarg
    *param_name* and rewrite its value to *fix_value*.

    Returns a new DAG, or ``None`` when no matching Parameter node was
    found (in which case the caller should short-circuit — the DAG is
    unchanged).
    """
    if not operator_fqn or not param_name:
        return None

    target_nids = {
        nid for nid, node in dag.nodes.items()
        if isinstance(node, Operator) and node.name == operator_fqn
    }
    if not target_nids:
        return None

    param_nid = None
    for e in dag.edges:
        if e.destination in target_nids and str(e.position) == param_name:
            src = dag.nodes.get(e.source)
            if isinstance(src, Parameter):
                param_nid = e.source
                break

    new_nodes = dict(dag.nodes)
    new_edges = list(dag.edges)

    if param_nid is not None:
        old = new_nodes[param_nid]
        new_nodes[param_nid] = Parameter(
            name=old.name, dtype=old.dtype, value=str(fix_value),
        )
    else:
        new_pid = f"fix_{param_name}_{next(iter(target_nids))}"
        new_nodes[new_pid] = Parameter(
            name=param_name, dtype="str", value=str(fix_value),
        )
        for tgt in target_nids:
            new_edges.append(Edge(new_pid, tgt, position=param_name))

    return DAG(nodes=new_nodes, edges=new_edges)


async def apply_structural_rewrite_to_dag(
    dag: DAG,
    *,
    operator_fqn: str,
    mitigation_slug: str,
) -> DAG | None:
    """Compile the named docstore rewrite for *operator_fqn* and apply it.

    Returns ``None`` when the rewrite is missing OR produces an invalid
    DAG (cycle / dangling edge). Without this validation, a buggy
    rewrite rule silently spawns thousands of cyclic ``rl_auto_mitigation``
    pipelines that xproduct will retry forever (because validation
    failures don't record an evaluation row, so the
    ``NOT EXISTS (... evaluations ...)`` filter keeps re-selecting them).
    """
    from dorian.pipeline.mitigation_rewrites import build_mitigation_rewrite
    from dorian.pipeline.dag_analysis import _validate_pipeline
    from backend.events import aemit, Event

    if not operator_fqn or not mitigation_slug:
        return None

    rewrite_fn = await build_mitigation_rewrite(mitigation_slug, operator_fqn)
    if rewrite_fn is None:
        return None
    rewritten = rewrite_fn(dag)
    if rewritten is None:
        return None

    errors = _validate_pipeline(rewritten)
    if errors:
        await aemit(Event("RLMitigationProducedInvalidDAG", {
            "source": "mitigation_apply.apply_structural_rewrite_to_dag",
            "operator_fqn": operator_fqn,
            "mitigation_slug": mitigation_slug,
            "errors": errors[:3],
        }))
        return None
    return rewritten


# ═══════════════════════════════════════════════════════════════════════
# High-level helper: pattern → operator resolution → apply
# ═══════════════════════════════════════════════════════════════════════

def resolve_operator_fqn(
    dag: DAG,
    *,
    failed_operator: str | None = None,
    failing_node_id: str | None = None,
) -> str:
    """Best-effort resolution of the failing operator's class FQN.

    Strategies (first non-empty wins):
      1. If *failed_operator* is dotted (e.g. ``sklearn.foo.Bar``) and
         not a node id, return it directly.
      2. Strip a compound-expansion suffix (``_cx_method_idx``) from
         *failing_node_id* and look the base node up in *dag.nodes*.
      3. Return an empty string when neither resolves.
    """
    if failed_operator and "." in failed_operator and not failed_operator.startswith("node_"):
        return failed_operator

    if failing_node_id:
        base = failing_node_id
        if "_cx_" in base:
            base = base[: base.index("_cx_")]
        node = dag.nodes.get(base)
        if isinstance(node, Operator) and node.name:
            return node.name

    return ""


async def apply_pattern_to_dag(
    dag: DAG,
    *,
    pattern: ExecutionErrorPattern,
    match: re.Match[str],
    error_msg: str,
    operator_fqn: str,
) -> tuple[DAG | None, dict[str, Any]]:
    """Apply the mitigation described by *pattern* to *dag*.

    Returns ``(new_dag, info)`` where ``info`` carries the fix payload
    that would have populated a suggestion card — the autonomous
    callers persist these for telemetry / reward attribution.

    Returns ``(None, info)`` when the apply is a no-op (e.g.
    ``fix_type='manual'``, missing operator, or rewrite produced no
    change).
    """
    fix_type = pattern.fix_type
    info: dict[str, Any] = {
        "pattern_id": pattern.id,
        "fix_type": fix_type,
        "operator": operator_fqn,
        "mitigation_slug": pattern.mitigation_slug,
        "param_name": pattern.param_name,
        "fix_value": "",
    }

    if fix_type == "manual":
        return None, info

    if fix_type == "parameter_change":
        try:
            fix_value = pattern.compute_fix(match, error_msg)
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"compute_fix raised: {exc}"
            return None, info
        info["fix_value"] = fix_value
        new_dag = apply_parameter_change_to_dag(
            dag,
            operator_fqn=operator_fqn,
            param_name=pattern.param_name,
            fix_value=fix_value,
        )
        return new_dag, info

    if fix_type == "structural_rewrite":
        new_dag = await apply_structural_rewrite_to_dag(
            dag,
            operator_fqn=operator_fqn,
            mitigation_slug=pattern.mitigation_slug or "",
        )
        return new_dag, info

    info["error"] = f"unknown fix_type {fix_type!r}"
    return None, info
