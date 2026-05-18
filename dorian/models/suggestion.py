
from pydantic import BaseModel
from typing import Literal, Dict, Any
from dataclasses import dataclass
@dataclass
class SuggestionInteraction(BaseModel):
    suggestion_id: str
    uid: str
    session: str
    type: Literal["accept", "reject", "upvote", "downvote"]
    suggestion: Dict[str, Any]
