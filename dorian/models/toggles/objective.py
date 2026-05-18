from dataclasses import dataclass

from backend.repository.document import Document

@dataclass
class ObjectivesToggles(Document):
    add: bool = True
    select: bool = True
    delete: bool = True
    drag: bool = True