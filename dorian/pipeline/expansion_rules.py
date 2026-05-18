"""
dorian/pipeline/expansion_rules.py
-----------------------------------
Primitive-op encodings of the platform-operator expansion rules.

Each rule pairs a pattern (a single ``Node`` matching a
``dorian.io.*`` operator FQN) with a list of declarative
``PrimitiveOp`` entries that execute the expansion. The Python
compiler in ``dorian/pipeline/mitigation_rewrites.py`` turns the
list into the same ``Apply`` chain as the legacy ``_expand_*``
functions, and the Rust evaluator in
``engine/graph/src/primitive.rs`` runs the same vocabulary on the
other side of the ``DORIAN_USE_RUST_REWRITES`` opt-in fence — so
this single declarative description retires the imperative Python
expansion code without changing runtime behaviour.

KB-readiness
~~~~~~~~~~~~
The dicts emitted here are JSON-serialisable on purpose: a follow-up
slice copies them into ``expdb.rewrites`` (the same collection the
mitigation migration in 4d75c21 used) so the rules are KB-resident
and the Rust runner can fetch + apply them without consulting any
Python module.

Today's slice ships only ``PRINTOUT_EXPANSION_PRIMITIVES`` because
the printout expansion has no runtime-context dependency — it is a
pure ``SetNodePayload`` swap from ``Operator(dorian.io.printout)``
to a ``Snippet`` carrying the type-detection body. The dataset and
state expansions need a session/Redis lookup to resolve the
operator's ``name`` parameter to a concrete fpath / state value;
those land in a separate slice that adds a ``ResolveAndAddNode``
primitive so the runtime side-channel stays explicit.
"""
from __future__ import annotations

import json
from typing import Any

from dorian.code.parsing.rule import Apply, RewriteRule
from dorian.dag import DAG, Node
from dorian.pipeline.mitigation_rewrites import _primitive_op_to_apply_fn
from dorian.pipeline.printout import _PRINTOUT_SNIPPET_CODE


# ---------------------------------------------------------------------------
# Primitive-op encoding of dorian.io.printout expansion
# ---------------------------------------------------------------------------

PRINTOUT_EXPANSION_PRIMITIVES: list[dict[str, Any]] = [
    {
        "op": "set_node_payload",
        "selector": {"sel": "from_mapping", "key": "n"},
        "payload": {
            "payload": "snippet",
            "name": "dorian.io.printout",
            "code": _PRINTOUT_SNIPPET_CODE,
            "language": "python",
        },
    }
]


def _printout_expansion_apply():
    """Compose the primitive ops into a single ``Apply.f`` closure.

    Mirrors what ``compile_rewrite_rule`` does for migrated
    mitigation docs — this function exists so the printout rule can
    sit alongside the legacy ``Apply(f=_expand_printout)`` form
    until every consumer has migrated. After the next slice removes
    ``_expand_printout`` outright, this becomes the only producer
    of the printout rule's ``Apply.f``.
    """
    fns = [_primitive_op_to_apply_fn(p) for p in PRINTOUT_EXPANSION_PRIMITIVES]

    def f(dag: DAG, mapping: dict, meta: dict) -> DAG:
        for fn in fns:
            dag = fn(dag, mapping, meta)
        return dag

    return f


PRINTOUT_EXPANSION_RULE_V2 = RewriteRule(
    pattern=DAG(
        nodes={"n": Node(type="Operator", text=r"dorian\.io\.printout")},
        edges=[],
    ),
    description="expand dorian.io.printout to a type-detecting display Snippet (primitive-op)",
    transformations=[Apply(f=_printout_expansion_apply())],
)


def primitive_rules_for_kb_seeding() -> dict[str, dict]:
    """Return a JSON-friendly dict of expansion rules keyed by name,
    suitable for the next-slice ``expdb.rewrites`` seeder. The shape
    matches the mitigation migration output (``transformations`` is
    a list of primitive-op dicts), so the existing
    ``compile_rewrite_rule`` consumes the seeded docs unchanged."""
    return {
        "expansion__printout": {
            "name": "expansion__printout",
            "description": (
                "Expand the dorian.io.printout platform operator into a "
                "type-detecting Snippet."
            ),
            "transformations": list(PRINTOUT_EXPANSION_PRIMITIVES),
        },
    }
