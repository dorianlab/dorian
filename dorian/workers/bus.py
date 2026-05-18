"""Event bus bridge — uses Dorian's bus when available, local fallback otherwise.

This module provides emit/subscribe wrappers so the rest of the workers package
never imports backend.events directly.  In standalone mode, events are just
printed to stdout.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Try to import Dorian's event bus
# ---------------------------------------------------------------------------
_dorian_bus = False

try:
    from backend.events import Event, subscribe as _subscribe, aemit as _aemit
    _dorian_bus = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Standalone fallback
# ---------------------------------------------------------------------------

if not _dorian_bus:
    from dataclasses import dataclass, field

    @dataclass
    class Event:  # type: ignore[no-redef]
        type: str
        data: dict[str, Any] = field(default_factory=dict)

    _local_handlers: dict[str, list[Callable]] = {}

    def _subscribe(event: str, fn: Callable) -> None:
        _local_handlers.setdefault(event, []).append(fn)

    async def _aemit(*events: Event) -> None:
        for ev in events:
            print(f"[workers] {ev.type}: {ev.data}")
            for fn in _local_handlers.get(ev.type, []):
                if asyncio.iscoroutinefunction(fn):
                    await fn(ev)
                else:
                    fn(ev)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def subscribe(event: str, fn: Callable) -> None:
    """Subscribe a handler to an event type."""
    _subscribe(event, fn)


async def aemit(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Emit an event through the bus."""
    await _aemit(Event(type=event_type, data=data or {}))


def is_dorian_bus() -> bool:
    """True when connected to Dorian's in-process event bus."""
    return _dorian_bus
