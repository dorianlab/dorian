"""
dorian.llm.factory — purpose-keyed Responder spawn.

Resolution order for ``spawn(purpose="X")``:

  1. ``config.llm.X`` if present (dedicated settings for that purpose)
  2. ``config.llm.default`` if present (shared defaults across purposes)
  3. ``config.mcp.extraction`` (backward-compat; the original home of
     these settings before the split)

Each source provides: ``backend`` (``groq`` | ``openai``), ``model``,
``api_key``, ``base_url`` (openai only), ``temperature``, ``max_tokens``.
Missing fields in a narrower source fall through to a wider one.
"""
from __future__ import annotations

import os
from typing import Any

from dorian.llm.responder import Responder
from dorian.llm.backends import GroqResponder, OpenAICompatibleResponder


_API_KEY_ENV = "DORIAN_LLM_API_KEY"
_FALLBACK_API_KEY_ENV = "DORIAN_MCP_EXTRACTION_LLM_API_KEY"


def _get_section(name: str) -> Any:
    from backend.config import config
    try:
        return getattr(config, name)
    except Exception:
        return None


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    try:
        val = getattr(obj, name)
    except Exception:
        return default
    return val if val not in ("", None) else default


def _resolve_cfg(purpose: str) -> dict[str, Any]:
    llm_section = _get_section("llm")
    purpose_cfg = _get_attr(llm_section, purpose) if llm_section is not None else None
    default_cfg = _get_attr(llm_section, "default") if llm_section is not None else None
    legacy_cfg = None
    mcp_section = _get_section("mcp")
    if mcp_section is not None:
        legacy_cfg = _get_attr(mcp_section, "extraction")

    def pick(field: str, default: Any = None) -> Any:
        for src in (purpose_cfg, default_cfg, legacy_cfg):
            val = _get_attr(src, f"llm_{field}") if src is legacy_cfg else _get_attr(src, field)
            if val is not None:
                return val
        return default

    backend = pick("backend", "openai")
    model = pick("model", "gpt-4o-mini")
    api_key = pick("api_key") or os.environ.get(_API_KEY_ENV) or os.environ.get(_FALLBACK_API_KEY_ENV, "")
    base_url = pick("base_url", "https://api.openai.com/v1")
    temperature = pick("temperature", 0.1)
    max_tokens = pick("max_tokens", 4096)

    return {
        "backend": backend,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }


def spawn(purpose: str = "default") -> Responder:
    """Return a configured LLM client for ``purpose``.

    Call this freely from any Dorian submodule. Responders are stateless
    across calls; make a new one per logical use-case and let it go.
    """
    cfg = _resolve_cfg(purpose)
    backend = cfg["backend"]

    if backend == "groq":
        return GroqResponder(
            model=cfg["model"],
            api_key=cfg["api_key"],
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
        )
    if backend == "openai":
        return OpenAICompatibleResponder(
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
        )
    raise ValueError(f"Unknown LLM backend: {backend!r}. Use 'groq' or 'openai'.")
