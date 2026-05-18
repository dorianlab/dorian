"""
dorian/evaluation/resolver.py
------------------------------
Resolves which evaluation procedure to use and returns a structured
``ResolvedProcedure`` with type + config, not just a name string.

The user can select a procedure via the sidebar (stored in session meta
as ``selectedEvaluationProcedureName`` / ``selectedEvaluationProcedureId``).
When nothing is selected, the resolver infers a default from the task type.

Custom procedures carry their code in ``meta["EvaluationProcedures"]``,
looked up by the selected ID.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Supervised learning tasks that default to hold-out evaluation.
_HOLDOUT_TASKS = frozenset({
    "Classification",
    "Regression",
    "Binary Classification",
    "Multiclass Classification",
    "Multi-label Classification",
    "Linear Regression",
    "Polynomial Regression",
    "Logistic Regression",
})

_AUTOMATED_HOLDOUT_NAME = "Automated (Hold-out)"

# Map KB procedure names to internal type strings.
_NAME_TO_TYPE: dict[str, str] = {
    "Automated (Hold-out)": "holdout",
    "K-fold Cross-validation": "kfold",
    "Pairwise Comparison": "pairwise",
    # ``No Evaluation`` is the canonical KB name (renamed from ``None`` —
    # users were reading the literal "None" as a "missing data" bug).
    # Old sessions whose meta still carries ``selectedEvaluationProcedureName="None"``
    # still resolve via the legacy alias below so reconnect doesn't lose
    # their selection.
    "No Evaluation": "none",
    "None": "none",
}


@dataclass(frozen=True)
class ResolvedProcedure:
    """Fully resolved evaluation procedure with type and config."""

    name: str
    type: str          # "holdout" | "kfold" | "custom" | "pairwise" | "none"
    config: dict = field(default_factory=dict)
    # For custom: config["code"], config["language"], config["outputs"]
    # For kfold:  config["k"] (default 5)


def resolve_default_eval(task_name: str | None) -> str | None:
    """Return the default evaluation procedure name for a task, or None.

    Kept for backward compatibility — internal callers should prefer
    ``resolve_eval_procedure()``.
    """
    if not task_name:
        return None
    if task_name in _HOLDOUT_TASKS:
        return _AUTOMATED_HOLDOUT_NAME
    return None


def resolve_eval_procedure(meta: dict) -> ResolvedProcedure:
    """Resolve the evaluation procedure from session meta.

    Resolution order:
    1. Explicitly selected procedure (by name/id in meta).
    2. Default procedure based on the selected data science task.
    3. ``none`` if nothing matches.

    For custom procedures, the code is extracted from the
    ``EvaluationProcedures`` list in session meta (written by
    ``handle_evaluation_procedure_added``).
    """
    selected_name = meta.get("selectedEvaluationProcedureName")
    selected_id = meta.get("selectedEvaluationProcedureId")

    # ── 1. Check if the selected name maps to a known KB procedure ──
    if selected_name:
        ptype = _NAME_TO_TYPE.get(selected_name)
        if ptype:
            config: dict = {}
            if ptype == "kfold":
                config["k"] = 5  # default; can be overridden via procedure config
            return ResolvedProcedure(name=selected_name, type=ptype, config=config)

        # Not a known KB name — might be a custom procedure
        if selected_id:
            custom_proc = _lookup_custom_procedure(meta, selected_id)
            if custom_proc is not None:
                return custom_proc

        # Unknown name, no custom match — treat as custom with the name only
        # (the user may have typed a name without code, which is a no-op)
        return ResolvedProcedure(name=selected_name, type="none")

    # ── 2. No explicit selection — resolve default from task ──
    task_info = meta.get("selectedDataScienceTask") or {}
    task_name = task_info.get("name")
    default_name = resolve_default_eval(task_name)

    if default_name:
        ptype = _NAME_TO_TYPE.get(default_name, "holdout")
        return ResolvedProcedure(name=default_name, type=ptype)

    # ── 3. No task or no default → skip evaluation ──
    return ResolvedProcedure(name="No Evaluation", type="none")


def _lookup_custom_procedure(meta: dict, procedure_id: str) -> ResolvedProcedure | None:
    """Find a custom procedure in session meta by its UUID."""
    procedures = meta.get("EvaluationProcedures") or []
    for proc in procedures:
        if not isinstance(proc, dict):
            continue
        if proc.get("uuid") == procedure_id or proc.get("id") == procedure_id:
            proc_meta = proc.get("meta") or proc.get("pipeline") or {}
            code = proc_meta.get("code")
            if not code:
                return None
            return ResolvedProcedure(
                name=proc.get("name", "Custom"),
                type="custom",
                config={
                    "code": code,
                    "language": proc_meta.get("language", "python"),
                    "outputs": proc_meta.get("outputs", []),
                },
            )
    return None
