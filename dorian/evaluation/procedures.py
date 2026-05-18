"""Read-only evaluation-procedure catalog backed by the rust KB snapshot."""
from __future__ import annotations

from backend.events import Event, aemit
from dorian.knowledge.ontology_kb import load_kb
from dorian.models import EvaluationProcedure


class EvaluationProcedures:
    @staticmethod
    async def get():
        try:
            kb = load_kb()
            members = sorted(set(kb.incoming("Evaluation Procedure", "is_an")))
            return [EvaluationProcedure(name=n) for n in members]
        except Exception as exc:
            await aemit(Event(
                "KnowledgeBaseError",
                data={
                    "message": str(exc),
                    "trace": "dorian.evaluation.procedures.EvaluationProcedures.get",
                },
            ))
            return []
