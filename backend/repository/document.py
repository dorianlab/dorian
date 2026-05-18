"""Legacy Document base used by a handful of model dataclasses.

Historically this wrapped docstore's ObjectId — each document's ``id`` was
a binary ObjectId coerced between the ``id`` and ``_id`` fields. After
docstore was retired, identifiers are plain strings (TEXT PK in the
Postgres ``per-collection doc_* tables`` table), so the wrapper is a thin conversion
between the dataclass attribute (``id``) and the document key (``_id``).
"""
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class Document:
    """Base class for document-store-backed dataclasses.

    ``id`` is a string (TEXT in Postgres) — the legacy ObjectId has been
    retired along with docstore. Use ``to_dict`` when persisting (maps
    ``id`` → ``_id``) and ``from_dict`` when loading (maps ``_id`` → ``id``).
    """

    id: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.id:
            data["_id"] = self.id
            del data["id"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        if "_id" in data:
            data["id"] = data.pop("_id")
        return cls(**data)
