"""
dorian/code/llm/rule_generation.py
------------------------------------
LLM API integration for rewrite rule generation.

Supports OpenRouter (any model) and LangChain/Perplexity backends.
Configure via environment variables:

- ``OPENROUTER_API_BASE_URL`` / ``OPENROUTER_API_KEY`` / ``LLM_MODEL``
- ``PERPLEXITY_API_KEY`` (for LangChain backend)
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel


class RewriteRuleLLMResponseFormat(BaseModel):
    """Structured output format for LLM rule generation."""
    chain_of_thought: str
    rule: str


def generate_rules_openrouter(full_prompt: str, **kwargs: Any) -> str | None:
    """Generate a rewrite rule via OpenRouter API.

    Requires ``OPENROUTER_API_BASE_URL``, ``OPENROUTER_API_KEY``, and
    ``LLM_MODEL`` environment variables.
    """
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ["OPENROUTER_API_BASE_URL"],
        api_key=os.environ["OPENROUTER_API_KEY"],
    )
    completion = client.chat.completions.create(
        model=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": full_prompt}],
    )
    return completion.choices[0].message.content


def generate_rules_langchain(full_prompt: str, **kwargs: Any) -> Any:
    """Generate a rewrite rule via LangChain + Perplexity API.

    Requires ``PERPLEXITY_API_KEY`` and ``LLM_MODEL`` environment variables.
    """
    from langchain_perplexity import ChatPerplexity

    chat = ChatPerplexity(
        temperature=0,
        model=os.environ["LLM_MODEL"],
        api_key=os.environ["PERPLEXITY_API_KEY"],  # type: ignore
        timeout=10,
    )
    messages = [{"role": "human", "content": full_prompt}]
    return chat.invoke(messages)
