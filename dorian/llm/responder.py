"""
dorian.llm.responder — protocol for LLM client backends.
"""
from __future__ import annotations

from typing import Any, Protocol


class Responder(Protocol):
    """Minimal LLM client interface.

    Backends are stateless with respect to each other; a single process
    can hold several responders concurrently (e.g. one for rule
    suggestion at low temperature, another for user-facing explanation
    at higher temperature).
    """

    def invoke(self, prompt: str, **kwargs: Any) -> str:
        """Send ``prompt``, return the model's response text."""
        ...
