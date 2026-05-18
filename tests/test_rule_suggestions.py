"""
tests/test_rule_suggestions.py
-------------------------------
Tests for the LLM-assisted rewrite rule suggestion backend.

Covers three layers:
  1. rule_codegen  — JSON spec → Python source generation
  2. rule_learning — LLM response parsing, compilation, validation
  3. WS handler + DraftStore integration
"""
from __future__ import annotations

import ast
import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Backend stubs are in conftest.py (loaded automatically by pytest).

from dorian.code.rule_codegen import json_spec_to_python  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 1. TestJsonSpecToPython — pure unit tests for code generation
# ═══════════════════════════════════════════════════════════════════════════


class TestJsonSpecToPython(unittest.TestCase):
    """Test json_spec_to_python() produces correct Python source."""

    def test_delete(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "delete", "nodes": ["0"]}],
        }
        code = json_spec_to_python(spec)
        assert "Delete(nodes=['0'])" in code

    def test_delete_with_mode(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "delete", "nodes": ["0"], "mode": "cascade"}],
        }
        code = json_spec_to_python(spec)
        assert "PurgeMode.cascade" in code

    def test_replace_operator(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator", "text": "OldOp"}}, "edges": []},
            "transformations": [{"type": "replace_operator", "target": "0", "new_name": "NewOp"}],
        }
        code = json_spec_to_python(spec)
        assert "Operator(name='NewOp'" in code

    def test_add_parameter(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [
                {"type": "add_parameter", "target": "0", "param_name": "n_estimators", "param_value": "100"}
            ],
        }
        code = json_spec_to_python(spec)
        assert "Parameter(name='n_estimators'" in code

    def test_update_attribute_literal(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [
                {"type": "update_attribute", "target": "0", "attribute": "text", "value": "new_text"}
            ],
        }
        code = json_spec_to_python(spec)
        assert "_update(g, m['0'], 'text', 'new_text')" in code

    def test_update_attribute_ref(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}}, "edges": []},
            "transformations": [
                {"type": "update_attribute", "target": "0", "attribute": "text", "value": {"ref": "1", "attr": "text"}}
            ],
        }
        code = json_spec_to_python(spec)
        assert "getattr(g.nodes[m['1']], 'text', '')" in code

    def test_update_attribute_concat(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}}, "edges": []},
            "transformations": [
                {
                    "type": "update_attribute",
                    "target": "0",
                    "attribute": "text",
                    "value": {"concat": ["prefix_", {"ref": "1", "attr": "text"}]},
                }
            ],
        }
        code = json_spec_to_python(spec)
        assert "+" in code
        assert "'prefix_'" in code
        assert "getattr(g.nodes[m['1']], 'text', '')" in code

    def test_insert_before(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "insert_before", "target": "0", "new_operator": "Scaler"}],
        }
        code = json_spec_to_python(spec)
        assert "insert_before(g," in code
        assert "'Scaler'" in code

    def test_insert_after(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "insert_after", "target": "0", "new_operator": "Evaluator"}],
        }
        code = json_spec_to_python(spec)
        assert "insert_after(g," in code
        assert "'Evaluator'" in code

    def test_full_spec_round_trip(self):
        """A complete spec should produce parseable Python."""
        spec = {
            "description": "Test rule",
            "pattern": {
                "nodes": {"0": {"type": "Operator", "text": "SomeOp"}},
                "edges": [],
            },
            "transformations": [{"type": "delete", "nodes": ["0"]}],
        }
        code = json_spec_to_python(spec)
        assert code.startswith("RewriteRule(")
        assert "pattern=DAG(" in code
        # Should be valid Python syntax
        ast.parse(code, mode="eval")

    def test_empty_transformations(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [],
        }
        code = json_spec_to_python(spec)
        assert "transformations=[]" in code

    def test_pattern_with_edges(self):
        spec = {
            "pattern": {
                "nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}},
                "edges": [{"source": "0", "destination": "1"}],
            },
            "transformations": [],
        }
        code = json_spec_to_python(spec)
        assert "Edge(source='0', destination='1')" in code


# ═══════════════════════════════════════════════════════════════════════════
# 2. TestSuggestRules — mock the LLM responder
# ═══════════════════════════════════════════════════════════════════════════


def _make_responder(response_text: str) -> MagicMock:
    """Create a mock LLM responder that returns *response_text*."""
    responder = MagicMock()
    responder.invoke.return_value = response_text
    return responder


# Minimal valid spec that passes compile_rule
_VALID_SPEC = {
    "description": "Remove standalone operator",
    "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
    "transformations": [{"type": "delete", "nodes": ["0"]}],
}

# Spec that uses Apply (replace_operator) — works with sync_apply in rule_test
_APPLY_SPEC = {
    "description": "Rename operator",
    "pattern": {"nodes": {"0": {"type": "Operator", "text": "X"}}, "edges": []},
    "transformations": [{"type": "replace_operator", "target": "0", "new_name": "Y"}],
}

# Spec that references a non-existent pattern node → compile fails
_INVALID_SPEC = {
    "description": "Bad ref",
    "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
    "transformations": [{"type": "replace_operator", "target": "MISSING", "new_name": "X"}],
}


class TestSuggestRules(unittest.TestCase):
    """Test suggest_rules() from dorian.code.rule_learning."""

    def setUp(self):
        self._aemit_patcher = patch("dorian.code.rule_learning.aemit", new_callable=AsyncMock)
        self._aemit_patcher.start()

    def tearDown(self):
        self._aemit_patcher.stop()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_valid_suggestion(self):
        llm_response = json.dumps({"reasoning": "test", "rules": [_VALID_SPEC]})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import suggest_rules
            result = self._run(suggest_rules("ext1", "x=1", {"nodes": {}, "edges": []}, "rules"))
        assert len(result.rules) == 1
        assert result.rules[0].valid is True
        assert result.rules[0].draft_id is None  # not staged yet (handler does that)

    def test_invalid_spec(self):
        llm_response = json.dumps({"reasoning": "test", "rules": [_INVALID_SPEC]})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import suggest_rules
            result = self._run(suggest_rules("ext1", "x=1", {"nodes": {}, "edges": []}, "rules"))
        assert len(result.rules) == 1
        assert result.rules[0].valid is False
        assert len(result.rules[0].errors) > 0

    def test_empty_rules(self):
        llm_response = json.dumps({"reasoning": "Nothing to suggest", "rules": []})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import suggest_rules
            result = self._run(suggest_rules("ext1", "x=1", {"nodes": {}, "edges": []}, "rules"))
        assert result.rules == []

    def test_unparseable_llm_response(self):
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder("not json at all")):
            from dorian.code.rule_learning import suggest_rules
            result = self._run(suggest_rules("ext1", "x=1", {"nodes": {}, "edges": []}, "rules"))
        assert result.rules == []
        assert "Failed" in result.reasoning

    def test_only_first_rule_taken(self):
        """Even if LLM returns multiple rules, only the first is used."""
        llm_response = json.dumps({"reasoning": "mix", "rules": [_VALID_SPEC, _INVALID_SPEC]})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import suggest_rules
            result = self._run(suggest_rules("ext1", "x=1", {"nodes": {}, "edges": []}, "rules"))
        assert len(result.rules) == 1
        assert result.rules[0].valid is True

    def test_propose_rule_returns_first_valid(self):
        llm_response = json.dumps({"reasoning": "ok", "rules": [_VALID_SPEC, _VALID_SPEC]})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import propose_rule
            result = self._run(propose_rule("x=1", "v1", {"nodes": {}, "edges": []}, {"nodes": {}, "edges": []}))
        assert result is not None
        assert isinstance(result, dict)  # returns spec dict, not Python code

    def test_propose_rule_returns_none_when_no_valid(self):
        llm_response = json.dumps({"reasoning": "bad", "rules": [_INVALID_SPEC]})
        with patch("dorian.mcp.extraction._get_responder", return_value=_make_responder(llm_response)):
            from dorian.code.rule_learning import propose_rule
            result = self._run(propose_rule("x=1", "v1", {"nodes": {}, "edges": []}, {"nodes": {}, "edges": []}))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 4. TestHandlerDraftStoreIntegration — test suggestion handlers with DraftStore
# ═══════════════════════════════════════════════════════════════════════════

from dorian.mcp.draft_store import DraftStore  # noqa: E402
from dorian.event.handlers.extraction import (  # noqa: E402
    handle_suggest_rules,
    handle_accept_rule,
    handle_reject_rule,
    handle_extract_pipeline,
)


def _make_event(data=None):
    """Create a minimal event object."""
    evt = MagicMock()
    evt.data = data or {}
    return evt


class TestHandlerDraftStoreIntegration(unittest.TestCase):
    """Test suggest/accept/reject handlers with a real DraftStore."""

    def _run(self, coro):
        return asyncio.run(coro)

    def setUp(self):
        self.store = DraftStore()
        # Patch draft_store singleton to our fresh instance
        self._store_patcher = patch(
            "dorian.mcp.stores.draft_store", self.store,
        )
        self._store_patcher.start()

    def tearDown(self):
        self._store_patcher.stop()

    # -- suggest ---------------------------------------------------------------

    def test_suggest_matching_rule_sent_to_frontend(self):
        """A rule whose pattern matches the DAG should be staged and sent."""
        # DAG has an Operator node so _VALID_SPEC's pattern matches
        fake_record = {"code": "x=1", "autoDag": {
            "nodes": {"0": {
                "class_type": "Operator", "name": "X",
                "type": "Operator", "language": "python",
            }},
            "edges": [],
        }}
        llm_response = json.dumps({"reasoning": "ok", "rules": [_VALID_SPEC]})

        captured = {}

        async def fake_xadd(uid, session, msg):
            captured.update(msg)

        with (
            patch("dorian.code.extraction_store.get_extraction",
                  new_callable=AsyncMock, return_value=fake_record),
            patch("dorian.mcp.extraction._get_responder",
                  return_value=_make_responder(llm_response)),
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
        ):
            mock_docstore.rule_suggestions.insert_one = AsyncMock()
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=None)
            self._run(handle_suggest_rules(
                _make_event(),
                uid="u1", session="s1",
                payload={"extractionId": "ext1"},
                request_id="r1", ts=0,
            ))

        assert captured["event"] == "extraction/rules-suggestion"
        sent = json.loads(captured["value"])
        rules = sent["rules"]
        assert len(rules) == 1
        assert rules[0]["draftId"] is not None
        # Draft should exist in the store
        assert self.store.get_rule(rules[0]["draftId"]) is not None

    def test_suggest_non_matching_rule_retries_then_errors(self):
        """Non-matching rules trigger retries; after exhaustion an error is sent."""
        # Empty DAG — no nodes to match, so every attempt fails
        fake_record = {"code": "x=1", "autoDag": {"nodes": {}, "edges": []}}
        llm_response = json.dumps({"reasoning": "ok", "rules": [_VALID_SPEC]})

        captured_events = []

        async def fake_xadd(uid, session, msg):
            captured_events.append(dict(msg))

        with (
            patch("dorian.code.extraction_store.get_extraction",
                  new_callable=AsyncMock, return_value=fake_record),
            patch("dorian.mcp.extraction._get_responder",
                  return_value=_make_responder(llm_response)),
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
        ):
            mock_docstore.rule_suggestions.insert_one = AsyncMock()
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=None)
            self._run(handle_suggest_rules(
                _make_event(),
                uid="u1", session="s1",
                payload={"extractionId": "ext1"},
                request_id="r1", ts=0,
            ))

        # Final event should be an error after retry exhaustion
        last = captured_events[-1]
        assert last["event"] == "extraction/suggest-error"
        error_data = json.loads(last["value"])
        assert "attempts" in error_data
        assert error_data["attempts"] == 5
        # No drafts should remain in the store (cleaned up between retries)
        assert len(self.store.rules) == 0

    def test_suggest_invalid_spec_retries_then_errors(self):
        """Invalid specs trigger retries with schema feedback; exhaustion sends error."""
        fake_record = {"code": "x=1", "autoDag": {"nodes": {}, "edges": []}}
        llm_response = json.dumps({"reasoning": "bad", "rules": [_INVALID_SPEC]})

        captured_events = []

        async def fake_xadd(uid, session, msg):
            captured_events.append(dict(msg))

        with (
            patch("dorian.code.extraction_store.get_extraction",
                  new_callable=AsyncMock, return_value=fake_record),
            patch("dorian.mcp.extraction._get_responder",
                  return_value=_make_responder(llm_response)),
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
        ):
            mock_docstore.rule_suggestions.insert_one = AsyncMock()
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=None)
            self._run(handle_suggest_rules(
                _make_event(),
                uid="u1", session="s1",
                payload={"extractionId": "ext1"},
                request_id="r1", ts=0,
            ))

        last = captured_events[-1]
        assert last["event"] == "extraction/suggest-error"
        error_data = json.loads(last["value"])
        assert error_data["attempts"] == 5

    # -- accept ----------------------------------------------------------------

    def test_accept_commits_draft(self):
        """Accepting a rule with a valid draft_id should commit it."""
        from dorian.mcp.rule_tools import rule_create, rule_test

        # Pre-stage a draft — use _APPLY_SPEC so sync_apply produces a diff
        create_result = rule_create(self.store, _APPLY_SPEC)
        draft_id = create_result["draft_id"]
        _test_dag = {
            "nodes": {"0": {"class_type": "Operator", "name": "X", "language": "python"}},
            "edges": [],
        }
        rule_test(self.store, draft_id, _test_dag)

        suggestion_doc = {
            "_id": "sug-1",
            "extractionId": "ext1",
            "rules": [{"ruleId": "r1", "spec": _APPLY_SPEC, "draftId": draft_id}],
        }

        captured = {}

        async def fake_xadd(uid, session, msg):
            captured.update(msg)

        with (
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
        ):
            mock_docstore.rule_suggestions.find_one = AsyncMock(return_value=suggestion_doc)
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=None)
            mock_docstore.extraction_rule_versions.insert_one = AsyncMock()
            self._run(handle_accept_rule(
                _make_event(),
                uid="u1", session="s1",
                payload={"suggestionId": "sug-1", "ruleId": "r1", "draftId": draft_id},
                request_id="r1", ts=0,
            ))

        sent = json.loads(captured["value"])
        assert "error" not in sent
        assert sent["draftId"] == draft_id
        # Draft should be removed after commit
        assert self.store.get_rule(draft_id) is None

    def test_accept_fallback_recreates_draft(self):
        """If the draft is gone (server restart), accept should re-create from spec."""
        fake_record = {"code": "x=1", "autoDag": {
            "nodes": {"0": {"class_type": "Operator", "name": "X", "language": "python"}},
            "edges": [],
        }}
        suggestion_doc = {
            "_id": "sug-1",
            "extractionId": "ext1",
            "rules": [{"ruleId": "r1", "spec": _APPLY_SPEC, "draftId": "gone-id"}],
        }

        captured = {}

        async def fake_xadd(uid, session, msg):
            captured.update(msg)

        with (
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
            patch("dorian.code.extraction_store.get_extraction",
                  new_callable=AsyncMock, return_value=fake_record),
        ):
            mock_docstore.rule_suggestions.find_one = AsyncMock(return_value=suggestion_doc)
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=None)
            mock_docstore.extraction_rule_versions.insert_one = AsyncMock()
            self._run(handle_accept_rule(
                _make_event(),
                uid="u1", session="s1",
                payload={"suggestionId": "sug-1", "ruleId": "r1", "draftId": "gone-id"},
                request_id="r1", ts=0,
            ))

        sent = json.loads(captured["value"])
        assert "error" not in sent
        assert sent["draftId"] is not None
        assert sent["draftId"] != "gone-id"  # a new draft was created

    # -- reject ----------------------------------------------------------------

    def test_reject_cleans_up_draft(self):
        """Rejecting a rule should remove its draft from the store."""
        from dorian.mcp.rule_tools import rule_create

        create_result = rule_create(self.store, _VALID_SPEC)
        draft_id = create_result["draft_id"]
        assert self.store.get_rule(draft_id) is not None

        with patch("dorian.event.handlers.extraction._xadd", new_callable=AsyncMock):
            self._run(handle_reject_rule(
                _make_event(),
                uid="u1", session="s1",
                payload={"ruleId": "r1", "suggestionId": "sug-1", "draftId": draft_id},
                request_id="r1", ts=0,
            ))

        assert self.store.get_rule(draft_id) is None

    def test_reject_without_draft_id(self):
        """Rejecting without a draftId should not crash."""
        with patch("dorian.event.handlers.extraction._xadd", new_callable=AsyncMock):
            self._run(handle_reject_rule(
                _make_event(),
                uid="u1", session="s1",
                payload={"ruleId": "r1", "suggestionId": "sug-1"},
                request_id="r1", ts=0,
            ))

    # -- extract with json_specs -----------------------------------------------

    def test_extract_loads_json_specs_format(self):
        """handle_extract_pipeline should load json_specs format docs."""
        from dorian.code.parsing.rules import get_rules

        json_content = json.dumps([_VALID_SPEC])
        doc = {
            "content": json_content,
            "format": "json_specs",
            "isValid": True,
        }

        captured = {}

        async def fake_xadd(uid, session, msg):
            captured.update(msg)

        with (
            patch("dorian.event.handlers.extraction._xadd",
                  new_callable=AsyncMock, side_effect=fake_xadd),
            patch("dorian.event.handlers.extraction.expdb") as mock_docstore,
            patch("dorian.event.handlers.extraction.aioredis") as mock_redis,
            patch("dorian.event.handlers.extraction.parse_code") as mock_parse,
            patch("dorian.event.handlers.extraction._persist_extraction",
                  new_callable=AsyncMock),
        ):
            mock_docstore.extraction_rule_versions.find_one = AsyncMock(return_value=doc)
            mock_redis.set = AsyncMock()
            # parse_code returns two DAGs
            from dorian.dag import DAG
            empty_dag = DAG()
            mock_parse.return_value = (empty_dag, empty_dag)

            self._run(handle_extract_pipeline(
                _make_event(),
                uid="u1", session="s1",
                payload={"code": "x=1", "language": "python"},
                request_id="r1", ts=0,
            ))

        # parse_code should have been called with rules that include the compiled custom rule
        call_args = mock_parse.call_args
        rules_used = call_args[0][2]  # third positional arg
        default_count = len(get_rules())
        assert len(rules_used) == default_count + 1


class TestPatternLanguageDefault(unittest.TestCase):
    """Regression test for dorian/dag.py::comparator language regex match.

    The pattern schema defaults ``language`` to ``.*``; the match
    comparator must treat that as a regex (wildcard) rather than require
    exact-string equality with the concrete operator's ``python`` /
    ``r`` / etc. Historical bug: any rule authored with the default
    language silently matched nothing. The parallel comparator in
    ``dorian/pipeline/parser.py`` always did regex-match; the code-
    parsing layer diverged.
    """

    def test_default_language_matches_any_operator(self):
        from dorian.mcp.rule_compiler import compile_rule
        from dorian.mcp.dag_tools import _parse_dag
        from dorian.dag import match

        spec = {
            "description": "pattern with default language",
            "pattern": {"nodes": {"0": {"type": "Operator", "text": "StandardScaler"}},
                        "edges": []},
            "transformations": [
                {"type": "replace_operator", "target": "0", "new_name": "RobustScaler"},
            ],
        }
        compiled, errors, _warn = compile_rule(spec)
        assert compiled is not None and not errors
        # Schema defaults language to the regex ".*"
        assert compiled.pattern.nodes["0"].language == ".*"

        dag = _parse_dag({
            "nodes": {"a": {"class_type": "Operator",
                            "name": "StandardScaler",
                            "language": "python"}},
            "edges": [],
        })
        found, mapping = match(compiled.pattern, dag)
        assert found is True
        assert mapping == {"0": "a"}

    def test_explicit_language_literal_still_works(self):
        # The compile layer currently restricts language to a closed set
        # ("python") or the wildcard ".*"; richer regex patterns are
        # rejected by the compiler. Verify the allowed literal flows
        # through to a successful match.
        from dorian.mcp.rule_compiler import compile_rule
        from dorian.mcp.dag_tools import _parse_dag
        from dorian.dag import match

        spec = {
            "description": "explicit language literal",
            "pattern": {"nodes": {"0": {"type": "Operator", "text": "X",
                                         "language": "python"}},
                        "edges": []},
            "transformations": [
                {"type": "replace_operator", "target": "0", "new_name": "Y"},
            ],
        }
        compiled, _errs, _warns = compile_rule(spec)
        assert compiled is not None

        dag = _parse_dag({
            "nodes": {"a": {"class_type": "Operator",
                            "name": "X", "language": "python"}},
            "edges": [],
        })
        found, mapping = match(compiled.pattern, dag)
        assert found is True and mapping == {"0": "a"}

    def test_language_mismatch_via_bypass_rejects(self):
        # Construct a pattern directly (bypassing compile_rule's
        # allow-list) with a concrete-language regex; a concrete node
        # with a different language must not match. Verifies that the
        # regex-match behaviour is a regex match, not a permissive
        # fall-through that accepts anything.
        import re as _re
        from dorian.dag import DAG, Node, Operator, match

        pattern = DAG(
            nodes={"0": Node(type="Operator", text="X", language="python")},
            edges=[],
        )
        dag = DAG(
            nodes={"a": Operator(name="X", language="r")},
            edges=[],
        )
        found, _mapping = match(pattern, dag)
        assert found is False


if __name__ == "__main__":
    unittest.main()
