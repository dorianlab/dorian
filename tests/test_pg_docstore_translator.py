"""Unit tests for ``backend.db.pg_docstore._Translator``.

Pure SQL-string generation — no DB required. Catches the regression
where dotted-key equality (``{"source.type": "openml"}``) was
double-wrapped by the outer ``{head: ...}`` plus ``_rebuild_nested``,
yielding ``{"source": {"source": "openml"}}`` and silently matching
zero rows. Symptom in the wild: ``openml_loader``'s idempotent
``find_one({"source.type": "openml", "source.originalId": ...})``
dedup never found existing entries on re-runs and crashed at the
INSERT step with a unique-constraint violation.
"""
from __future__ import annotations

import json

from backend.db.pg_docstore import _Translator, _rebuild_nested


def test_rebuild_nested_top_level_wraps():
    # Was bare ``value`` before the 2026-04-28 fix; that put the burden
    # on the caller to wrap, and the caller wrapped *with the head*,
    # which double-wrapped dotted paths.
    assert _rebuild_nested("name", "foo") == {"name": "foo"}


def test_rebuild_nested_dotted():
    assert _rebuild_nested("source.type", "openml") == {"source": {"type": "openml"}}
    assert _rebuild_nested("a.b.c", 1) == {"a": {"b": {"c": 1}}}


def test_translator_dotted_key_equality():
    t = _Translator()
    where = t.translate({"source.type": "openml", "source.originalId": "40996"})
    # Both clauses are JSONB-containment ``data @>`` checks.
    assert where.count("data @>") == 2
    # Args are JSON-encoded nested dicts that match the ACTUAL stored shape.
    args_decoded = [json.loads(a) for a in t.args]
    assert {"source": {"type": "openml"}} in args_decoded
    assert {"source": {"originalId": "40996"}} in args_decoded
    # Regression guards: the buggy form double-wrapped under the head.
    assert {"source": {"source": "openml"}} not in args_decoded
    assert {"source": {"source": "40996"}} not in args_decoded


def test_translator_top_level_key_equality():
    t = _Translator()
    where = t.translate({"name": "credit-g"})
    assert "data @>" in where
    assert json.loads(t.args[0]) == {"name": "credit-g"}


def test_translator_id_clause_uses_id_column():
    # ``_id`` is a special case — it maps to the TEXT ``id`` column,
    # not a JSONB lookup. Document the contract here so a future
    # refactor doesn't accidentally route ``_id`` through containment.
    t = _Translator()
    where = t.translate({"_id": "deadbeef"})
    assert "id =" in where.lower() or "id=" in where.lower()
