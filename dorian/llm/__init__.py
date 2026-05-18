"""
dorian.llm — spawnable LLM client primitive.

Any Dorian submodule that needs an LLM imports from here, not from
``dorian.mcp.*``. MCP is a consumer of this module (it exposes the LLM
surface to external MCP clients); risk handlers, rule-learning,
data-quality explainers, and anything else with a different
use-case are all independent consumers.

Usage:

    from dorian.llm import spawn
    client = spawn(purpose="rule_suggestion")
    text = client.invoke(prompt, max_tokens=8192)
"""
from dorian.llm.responder import Responder
from dorian.llm.factory import spawn

__all__ = ["Responder", "spawn"]
