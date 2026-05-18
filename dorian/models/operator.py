from dataclasses import dataclass, field
from uuid import uuid4

from dorian.types import UUID

@dataclass
class Operator:
    uuid: UUID = field(default_factory=lambda: str(uuid4().hex))
    name: str = field(default_factory=str)