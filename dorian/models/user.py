from dataclasses import dataclass, field
from typing import Optional

from backend.repository.document import Document

@dataclass
class User(Document):
    """User document schema."""
    login: str = field(default_factory=str)
    email: Optional[str] = field(default_factory=None) 
    name: Optional[str] = field(default_factory=None)
    avatar: Optional[str] = field(default_factory=None)
