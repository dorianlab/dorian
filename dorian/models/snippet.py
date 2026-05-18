from dataclasses import dataclass, field
from uuid import uuid4 as uuid

from backend.repository.document import Document
from dorian.types import UUID

@dataclass
class Snippet(Document):
    uuid: UUID = field(default_factory=uuid)
    fn: str = field(default_factory=str)