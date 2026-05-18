from dataclasses import dataclass, field
from uuid import uuid4 as uuid

from backend.repository.document import Document
from dorian.models.pipeline import Pipeline
from dorian.types import UUID

@dataclass
class EvaluationProcedure(Document):
    uuid: UUID = field(default_factory=uuid)
    name: str = field(default_factory=str)
    pipeline: Pipeline = field(default_factory=Pipeline)