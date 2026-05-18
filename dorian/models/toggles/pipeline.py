from dataclasses import dataclass

from backend.repository.document import Document

@dataclass
class PipelineToggles(Document):
    compose: bool = True
    upload: bool = True
    execute: bool = True