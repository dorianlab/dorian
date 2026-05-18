"""
dorian/exec/objective.py
------------------------
Exec-worker job: validate user-defined ranking objectives.

The rust ``RankingObjectiveAdded`` handler in
``engine/backend/src/handlers/ranking_objective.rs`` does every state
write (session-meta merge + ``ranking_objectives`` postgres upsert)
and submits this job. We run the python compile/exec validation
off the hot path so the rust core stays GIL-free and pyo3-marshaling
is bounded to one JSON crossing per submission.
"""
from __future__ import annotations

from typing import Any

from dorian.exec.registry import register
from dorian.pipeline.recommendation.objectives import UserDefinedObjective


@register("objective:validate")
async def validate_objective(inputs: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    """Compile-check a user-defined objective.

    Inputs (from rust submitter):
        name, code, language, uid, session, lane

    Returns:
        {name, valid, error}

    The completion event ``ObjectiveValidateCompleted`` is emitted by
    the worker scaffolding; the rust completion handler picks it up
    and pushes ``state/objectives/validation`` to the WS stream.
    """
    name = str(inputs.get("name") or "")
    code = str(inputs.get("code") or "")
    language = str(inputs.get("language") or "python")

    obj = UserDefinedObjective(name=name, code=code, language=language)
    return {
        "name":  name,
        "valid": bool(obj.is_valid),
        "error": obj.compile_error or "",
    }
