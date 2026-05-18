from dataclasses import dataclass

from backend.repository.document import Document

@dataclass
class DatasetToggles(Document):
    add: bool = True
    delete: bool = True