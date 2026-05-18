from dataclasses import dataclass, field
from pendulum import DateTime, now

from backend.repository.document import Document
from dorian.models import Query, Toggles
from dorian.types import UUID


@dataclass
class Session(Document):
    user: UUID
    created_at: DateTime = field(default_factory=now)
    query: Query = field(default_factory=Query)
    toggles: Toggles = field(default_factory=Toggles)


