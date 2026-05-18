
from __future__ import annotations
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar
from backend.events import Event


T = TypeVar('T')
UUID = str
Payload = Dict[str, Any]
RequestId = Optional[str]
Ts = Optional[int]

HandlerFn = Callable[[Event, str, str, Payload, RequestId, Ts], Awaitable[None]]
