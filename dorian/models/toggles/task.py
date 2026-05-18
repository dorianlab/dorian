from dataclasses import dataclass

from backend.repository.document import Document

@dataclass
class TaskToggles(Document):
    add: bool = True
    select: bool = True