from redis.commands.json.path import Path
from typing import Protocol, Dict, Any
from dataclasses import asdict

# TODO: separate State and its storage infrastructure, e.g., redis
from dorian.types import UUID, T
from backend.envs import aioredis


class State(Protocol):
    async def get(self, *keys: UUID) -> T:
        """Retrieve element by ID/key"""

    async def put(self, item: T, *keys: UUID) -> None:
        """Put an Item"""

    async def all(self) -> Dict[str, Any]:
        """Retrieve the whole state"""


class RedisState(State):
    async def get(self, *keys: UUID) -> T:
        """Retrieve element by ID/key"""
        return await aioredis.json().get('state', Path(".".join(keys)))

    async def put(self, item: T, *keys: UUID) -> None:
        """Put an Item"""
        await aioredis.json().set('state', Path(".".join(keys)), asdict(item))
        return self
    
    async def all(self) -> Dict[str, Any]:
        return await aioredis.json().get('state')


