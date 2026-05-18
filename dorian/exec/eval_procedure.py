"""
dorian/exec/eval_procedure.py
-----------------------------
Exec-worker job: validate user-defined evaluation procedures.

The rust ``EvaluationProcedureAdded`` handler in
``engine/backend/src/handlers/evaluation_procedure.rs`` does every
state write (session-meta upsert + ``evaluation_procedures`` postgres
upsert) and submits this job. The python compile/exec runs off the
hot path so the rust core stays GIL-free.
"""
from __future__ import annotations

from typing import Any

from dorian.exec.registry import register


@register("eval_procedure:validate")
async def validate_eval_procedure(
    inputs: dict[str, Any], *, job_id: str
) -> dict[str, Any]:
    """Compile-check a user-defined evaluation procedure.

    The procedure must define a callable ``foo(y_test, y_pred, X_test)``
    that returns a metric value.

    Inputs (from rust submitter):
        uid, session, uuid, name, code, lane

    Returns:
        {valid, error}

    The completion event ``EvalProcedureValidateCompleted`` is emitted
    by the worker scaffolding; the rust completion handler picks it
    up off ``state/evaluation/validation``.
    """
    code = str(inputs.get("code") or "")
    if not code:
        return {"valid": False, "error": "empty code"}

    try:
        compiled = compile(code, "<custom_eval>", "exec")
    except SyntaxError as exc:
        return {"valid": False, "error": f"SyntaxError: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": str(exc)}

    ns: dict[str, Any] = {}
    try:
        exec(compiled, ns)  # noqa: S102 — user-supplied; sandboxed at runtime
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": str(exc)}

    if not callable(ns.get("foo")):
        return {
            "valid": False,
            "error": (
                "Custom evaluation must define a callable "
                "'foo(y_test, y_pred, X_test)'"
            ),
        }
    return {"valid": True, "error": ""}
