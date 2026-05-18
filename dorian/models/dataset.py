from dataclasses import dataclass, field
from typing import List, Dict

from backend.repository.document import Document

@dataclass
class Dataset(Document):
    filepath: str = field(default_factory=str)
    features: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    public: bool = True
    profile: Dict[str, float] = field(default_factory=dict)