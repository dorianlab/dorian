# Catalog routes — objectives and evals are served here; all other
# catalog routes (operators, tasks, operator-params) are owned by
# engine/gateway/src/catalog.rs (KB-snapshot reads).
from __future__ import annotations

from fastapi import APIRouter

from dorian.evaluation.procedures import EvaluationProcedures
from dorian.ranking.objectives import Objectives

router = APIRouter()


@router.get("/catalog/objectives")
async def list_objectives():
    items = await Objectives.get()
    return [{"name": o.name} for o in items]


@router.get("/catalog/evals")
async def list_evals():
    items = await EvaluationProcedures.get()
    return [{"name": e.name} for e in items]
