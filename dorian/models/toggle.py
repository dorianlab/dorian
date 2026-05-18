from dataclasses import dataclass, field

from backend.repository.document import Document
from dorian.models.toggles import (
    DatasetToggles,
    PipelineToggles,
    ObjectivesToggles,
    TaskToggles,
    EvalToggles,
)

@dataclass
class Toggles(Document):
    dataset: DatasetToggles = field(default_factory=DatasetToggles)
    task: TaskToggles = field(default_factory=TaskToggles)
    pipeline: PipelineToggles = field(default_factory=PipelineToggles)
    eval: EvalToggles = field(default_factory=EvalToggles)
    objectives: ObjectivesToggles = field(default_factory=ObjectivesToggles)
