"""OpenAI-compatible chat-completion prior source.

Uses the existing :func:`dorian.llm.factory.spawn` plumbing so
configuration (endpoint URL, model, API key, temperature) flows
through the project's standard ``config.llm.<purpose>`` path — no
new config surface, no per-trainer API key handling.

Integration contract:

  * One LLM call per *unique* :class:`DatasetProfile`. Results are
    cached by profile hash — the same dataset won't pay the round
    trip twice per batch.
  * Any error (network, auth, malformed response, parse failure)
    degrades to an empty recommendation list. Trainer never breaks
    on a misconfigured LLM.
  * Response must be strict JSON with a known shape; free-form text
    is rejected. This keeps the prior signal auditable from the
    trainer logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from .base import DatasetProfile, PriorRecommendation

_log = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """You are an ML-pipeline expert advising an RL agent \
on which scikit-learn operators to include for a classification task. \
Return ONE JSON object with a ``recommendations`` list; nothing else.

Dataset profile (measured from the CSV, not guessed):
{profile_json}

Available catalog op_keys:
{catalog_list}

Recommend 3-5 op_keys in the order the agent should add them. \
Prefer operators that resolve structural mismatches visible in the \
profile (e.g. ``has_strings=true`` → include an encoder; \
``has_nulls=true`` → include an imputer). Include one classifier \
unless the profile is clearly unsuitable for any.

Each recommendation entry has:
  - ``op_key``: exact string from the catalog list
  - ``reason``: one short sentence tying the choice to a profile field
  - ``weight``: float in [1.0, 20.0] reflecting confidence

Response schema (required):
{{"recommendations": [
  {{"op_key": "...", "reason": "...", "weight": 5.0}},
  ...
]}}
"""


@dataclass
class OpenAIChatPriorSource:
    """Chat-completion backend. Stateless apart from an in-memory
    profile-hash cache.

    Pass ``purpose`` to target a specific ``config.llm.<purpose>``
    section; defaults to ``"rl-prior"`` which falls back through
    ``config.llm.default`` then ``config.mcp.extraction`` via the
    factory's standard resolution.
    """

    purpose: str = "rl-prior"
    _cache: dict[str, list[PriorRecommendation]] = field(default_factory=dict)
    _catalog_op_keys: tuple[str, ...] = ()

    def set_catalog(self, op_keys: tuple[str, ...]) -> None:
        """Inject the catalog's op_keys so the prompt can constrain
        the model's output to valid recommendations. Called by the
        env once the catalog is known."""
        self._catalog_op_keys = tuple(op_keys)

    def recommend(self, profile: DatasetProfile) -> list[PriorRecommendation]:
        key = _profile_hash(profile)
        if key in self._cache:
            return self._cache[key]
        recs = self._query(profile)
        self._cache[key] = recs
        return recs

    def _query(self, profile: DatasetProfile) -> list[PriorRecommendation]:
        try:
            from dorian.llm.factory import spawn
            responder = spawn(self.purpose)
        except Exception as exc:  # pragma: no cover - config path
            _log.info("rl-prior LLM not configured (%s) — skipping", exc)
            return []

        prompt = _PROMPT_TEMPLATE.format(
            profile_json=json.dumps(profile.to_prompt_dict(), indent=2),
            catalog_list="\n".join(f"  - {k}" for k in self._catalog_op_keys),
        )
        try:
            raw = responder.invoke(prompt, max_tokens=800, temperature=0.0)
        except Exception as exc:
            _log.warning("rl-prior LLM call failed (%s) — empty recs", exc)
            return []
        return _parse(raw, self._catalog_op_keys)


def _profile_hash(profile: DatasetProfile) -> str:
    """Stable hash over the measurable fields. Two profiles with the
    same measurements cache together regardless of dataset name."""
    data = json.dumps(profile.to_prompt_dict(), sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def _parse(
    raw: str, allowed_op_keys: tuple[str, ...]
) -> list[PriorRecommendation]:
    """Extract recommendations from the LLM's response. Rejects
    entries whose op_key isn't in the provided catalog — prevents
    the model from hallucinating operators the env can't enumerate.
    """
    allowed = set(allowed_op_keys)
    # Accept either a bare JSON object or a fenced block; strip
    # markdown fences if the model added them despite instructions.
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("rl-prior response not JSON (%s) — empty recs", exc)
        return []
    entries = obj.get("recommendations") if isinstance(obj, dict) else None
    if not isinstance(entries, list):
        return []
    out: list[PriorRecommendation] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        op = str(e.get("op_key") or "").strip()
        if allowed and op not in allowed:
            continue
        try:
            weight = float(e.get("weight", 5.0))
        except (TypeError, ValueError):
            weight = 5.0
        weight = max(1.0, min(20.0, weight))
        out.append(PriorRecommendation(
            op_key=op,
            reason=str(e.get("reason", ""))[:200],
            weight=weight,
        ))
    return out


__all__ = ["OpenAIChatPriorSource"]
