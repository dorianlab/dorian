from dataclasses import dataclass, field
from typing import Optional, List, Dict

from backend.repository.document import Document
from dorian.models import (
    Dataset,
    Task,
    EvaluationProcess,
    RankingObjective
)
from dorian.types import UUID


@dataclass
class Query(Document):
    task: Optional[Task] = field(default_factory=None)
    name: Optional[str] = field(default_factory=None)
    data: Dict[UUID, Dataset] = field(default_factory=dict)
    pipeline: Optional[str] = field(default_factory=None)
    eval: Optional[EvaluationProcess] = field(default_factory=None)
    objectives: Optional[List[RankingObjective]] = field(default_factory=list)

