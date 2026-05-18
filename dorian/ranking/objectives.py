"""Read-only ranking-objective catalog backed by the rust KB snapshot."""
from __future__ import annotations

from backend.events import Event, aemit
from dorian.knowledge.ontology_kb import load_kb
from dorian.models.objective import RankingObjective


class Objectives:
    @staticmethod
    async def get():
        try:
            kb = load_kb()
            members = sorted(set(kb.incoming("Ranking Objective", "is_a")))
            return [RankingObjective(name=n) for n in members]
        except Exception as exc:
            await aemit(Event(
                "KnowledgeBaseError",
                data={
                    "message": str(exc),
                    "trace": "dorian.ranking.objectives.Objectives.get",
                },
            ))
            return []

    @staticmethod
    async def set_from_docstore(*, objectives: list, uid: str, session: str):
        """Placeholder mirror of docstore-persisted objectives.

        Today this is a no-op stub kept for ``handle_ranking_objectives_added``
        to call without crashing; runtime KB additions go through
        ``dorian.knowledge.overlay.add_statement`` instead. When the
        promotion flow lands, this becomes the place that writes
        ``<obj.name> is a Ranking Objective`` to the overlay.
        """
        try:
            return True
        except Exception as exc:
            await aemit(Event(
                "KnowledgeBaseError",
                data={
                    "message": str(exc),
                    "trace": "dorian.ranking.objectives.Objectives.set_from_docstore",
                },
            ))
            return False
