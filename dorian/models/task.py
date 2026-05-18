from dataclasses import dataclass, field
from uuid import uuid4

from backend.repository.document import Document
from dorian.types import UUID


@dataclass
class Task(Document):
    uuid: UUID = field(default_factory=lambda: uuid4().hex)
    name: str = field(default_factory=str)