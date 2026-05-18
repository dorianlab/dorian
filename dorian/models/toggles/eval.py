from dataclasses import dataclass

from backend.repository.document import Document

@dataclass
class EvalToggles(Document):
    add: bool = True
    select: bool = True