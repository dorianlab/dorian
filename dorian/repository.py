# TODO: the `dorian` folder contains domain logic and should be independent
# from the choice of tools, libraries, and infrastructure.

from typing import Protocol, List

from dorian.types import UUID, T


class Repository(Protocol[T]):
    def get(self, key: UUID) -> T:
        """Retrieve element by ID/key"""

    def put(self, key: UUID, item: T) -> None:
        """Put an Item"""

    def list(self) -> List[T]:
        """Return all items"""