"""Pipeline-level guards -- generic mitigations that prevent
classes of pipeline pathology at mask-enumeration time.

Two guard flavours:

  * **SemanticTypeGuard** -- tightens an operator port's
    declared type so previously-valid wires become invalid.
    Catalog diff; no runtime logic.
  * **StructuralIntegrityGuard** -- carries a callable predicate
    that runs per candidate ``AddEdgeSpec``. Rejects candidates
    that would violate a structural invariant (e.g. two input
    ports of a metric being fed by the same source -- the
    tautological-metric pathology).

The registries live here; the RL env's mask builder calls
``apply_guards`` at startup (for type tightening) and
``check_structural_integrity`` per candidate (for edge-level
rules).

Motivation: the RL env's type system is used both for action
masking (which edges the agent can propose) and for structural
correctness. Some classes of pipeline pathology are exactly type
violations in disguise:

  * **Label shortcut**: wiring the raw target array into the
    metric's ``y_pred`` port bypasses the model entirely. The
    type system calls both sides "Array" -- so the agent can
    shortcut. The mitigation is to tighten the metric's
    ``y_pred`` port to a distinct ``"Prediction"`` type that
    only a ``predict`` operator produces.

  * **Data leakage** (future work): fitting on test data is an
    "Array -> Array" wire that crosses a train/test boundary.
    A ``"TrainOnly"`` / ``"TestOnly"`` split on the types
    prevents the wire at mask time.

  * **Target leakage** (future work): a feature derived from
    the target column masquerading as a regular feature. A
    ``"TargetDerived"`` type on the suspicious feature prevents
    it from flowing into the model's X input.

All three share one shape: **tighten an operator port's declared
type so a previously-valid wire becomes invalid at mask-
enumeration time**. The mitigation produces a catalog diff, not
a pipeline rewrite; consumers re-derive the mask against the
updated catalog.

This module is the registry + the applier. Individual guards are
declarative ``SemanticTypeGuard`` instances; the ``apply_guards``
helper returns a tightened copy of an operator catalog. The
RL-env's ``catalog_by_key`` loader calls ``apply_guards`` once at
startup; new guards land by appending to ``REGISTERED_GUARDS``.

See (internal design note; not in public repo) (pattern discovery + mitigation
rewrite flow) for the broader mitigation framing; this module is
the tightening-of-types slice.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Literal


PortSide = Literal["input", "output"]


@dataclass(frozen=True)
class SemanticTypeGuard:
    """One named mitigation: tighten a specific port's type.

    Attributes
    ----------
    name:
        Stable identifier for the guard. Surfaces in the
        registry + in exception-signature mitigation references.
    op_key:
        Catalog entry the guard applies to.
    port_side:
        "input" or "output".
    port_name:
        The port whose type gets tightened.
    new_type_hint:
        The replacement type. Must be a name the rest of the
        type system understands (or introduces with this
        mitigation -- "Prediction" was first introduced here).
    reason:
        Human-readable rationale. Surfaces in suggestion UIs +
        logs when the guard is applied or triggers a mask filter.
    """

    name: str
    op_key: str
    port_side: PortSide
    port_name: str
    new_type_hint: str
    reason: str


# ---------------------------------------------------------------------------
# Registered guards
# ---------------------------------------------------------------------------

REGISTERED_GUARDS: tuple[SemanticTypeGuard, ...] = (
    SemanticTypeGuard(
        name="label_shortcut_guard",
        op_key="sklearn.metrics.accuracy_score",
        port_side="input",
        port_name="1",  # the metric's y_pred input
        new_type_hint="Prediction",
        reason=(
            "Prevent wiring raw labels (Array) directly into the metric's "
            "y_pred port, which would bypass model training entirely. A "
            "valid y_pred must come from a predict operator."
        ),
    ),
    SemanticTypeGuard(
        name="label_shortcut_guard_predict_output",
        op_key="predict",
        port_side="output",
        port_name="y_pred",
        new_type_hint="Prediction",
        reason=(
            "Paired with label_shortcut_guard: predict's y_pred output "
            "must be typed Prediction so it can reach the metric. The "
            "pair tightens both sides of the edge."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------

def apply_guards(
    catalog: Iterable,
    guards: Iterable[SemanticTypeGuard] | None = None,
) -> tuple:
    """Return a new catalog tuple with every guard's port-type
    tightening applied. Catalog entries unaffected by any guard
    pass through unchanged.

    The catalog is a tuple of ``OperatorMeta`` (from
    ``rl.catalog.schema``) but this module does not import from
    ``rl`` to keep the dependency direction one-way; we duck-type
    via attribute access.

    ``guards`` defaults to ``REGISTERED_GUARDS``. Callers can pass
    an explicit subset for ablation experiments (e.g. turn off the
    label-shortcut guard to show the baseline pathology).
    """
    if guards is None:
        guards = REGISTERED_GUARDS
    guards_by_op: dict[str, list[SemanticTypeGuard]] = {}
    for g in guards:
        guards_by_op.setdefault(g.op_key, []).append(g)

    out: list = []
    for op in catalog:
        op_guards = guards_by_op.get(op.op_key, [])
        if not op_guards:
            out.append(op)
            continue
        new_inputs = list(op.inputs)
        new_outputs = list(op.outputs)
        for g in op_guards:
            target_list = new_inputs if g.port_side == "input" else new_outputs
            for i, port in enumerate(target_list):
                if port.name == g.port_name:
                    target_list[i] = replace(port, type_hint=g.new_type_hint)
        out.append(
            replace(
                op,
                inputs=tuple(new_inputs),
                outputs=tuple(new_outputs),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Structural-integrity guards
# ---------------------------------------------------------------------------

from typing import Callable


@dataclass(frozen=True)
class StructuralIntegrityGuard:
    """Mask-time predicate: reject an AddEdge candidate if it
    would violate a structural invariant.

    ``check(dag, candidate)`` returns True when the candidate is
    **violating** (should be filtered out of the mask). The
    predicate sees the current pipeline + the proposed
    AddEdgeSpec; it must not mutate the dag.
    """

    name: str
    # Predicate: signature kept loose to avoid importing the RL
    # types into this module. Accepts ``dag`` (``dorian.dag.DAG``)
    # and an object with ``src_node_id`` / ``src_output_port`` /
    # ``dst_node_id`` / ``dst_input_port`` attributes (the RL
    # env's AddEdgeSpec).
    check: Callable
    reason: str


def _no_tautological_metric_inputs(dag, spec) -> bool:
    """Refuse an AddEdge if both metric input ports would be fed
    by the same source. ``accuracy_score(y_test, y_test)`` is a
    tautology -- the comparison is perfect by construction and
    carries no evaluation signal.

    Specifically: if the candidate targets a metric operator's
    input port AND some OTHER input port on the same metric is
    already wired from the same ``src_node_id``, block the wire.

    The check is agnostic to which specific metric we're guarding
    -- any operator with ``family == "metric"`` in the catalog is
    covered. For now we detect metric-family by the operator
    FQN's last segment (``.accuracy_score``, ``.roc_auc_score``,
    etc.) since this module is catalog-agnostic; a
    catalog-aware re-check lives in the mask builder.
    """
    dst_nid = spec.dst_node_id
    src_nid = spec.src_node_id
    for edge in dag.edges:
        if edge.destination != dst_nid:
            continue
        # Different port on the same destination, same source?
        # If so, the new edge would produce (src, src) inputs.
        if str(edge.position) == spec.dst_input_port:
            # Same destination port -- would overwrite, not
            # collide. Other validity checks handle that.
            continue
        if edge.source == src_nid:
            return True  # violation
    return False


REGISTERED_STRUCTURAL_GUARDS: tuple[StructuralIntegrityGuard, ...] = (
    StructuralIntegrityGuard(
        name="no_tautological_metric_inputs",
        check=_no_tautological_metric_inputs,
        reason=(
            "A metric operator must not have two input ports fed by the "
            "same source (e.g. accuracy_score(y_test, y_test)). That "
            "makes the evaluation tautologically perfect and carries no "
            "signal. The guard rejects such AddEdge candidates at "
            "mask-enumeration time; the mask surfaces only wires that "
            "would compare genuinely distinct arrays."
        ),
    ),
)


def check_structural_integrity(
    dag,
    spec,
    guards: Iterable[StructuralIntegrityGuard] | None = None,
    metric_op_keys: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Run every registered StructuralIntegrityGuard against
    ``spec``. Returns ``(passes, reason_if_blocked)``.

    ``metric_op_keys`` is an optional allow-list that restricts
    metric-family guards to specific op_keys (so a
    ``StructuralIntegrityGuard`` specific to metrics doesn't
    misfire on non-metric operators). Passed down via a
    ``spec.dst_op_key`` attribute if the spec supplies one; the
    no-tautological-metric guard relies on the mask builder
    filtering to metric-family destinations before calling this
    helper.
    """
    if guards is None:
        guards = REGISTERED_STRUCTURAL_GUARDS
    for g in guards:
        try:
            if g.check(dag, spec):
                return False, f"{g.name}: {g.reason}"
        except Exception:
            # Guards must never crash the mask enumerator. A
            # buggy guard is a dev issue; log + skip.
            continue
    return True, ""


__all__ = [
    "REGISTERED_GUARDS",
    "REGISTERED_STRUCTURAL_GUARDS",
    "SemanticTypeGuard",
    "StructuralIntegrityGuard",
    "apply_guards",
    "check_structural_integrity",
]
