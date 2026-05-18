"""
tests/test_printout.py
-----------------------
Unit and contract tests for the ``dorian.io.printout`` platform operator.

Tests cover:
  - Expansion rule (dorian.io.printout → Snippet)
  - Snippet execution for all supported data types
  - LLM ChatCompletion response formatting
  - Integration with the pipeline execution expansion chain
  - Operator resolver snippet builtins (hasattr, getattr, ValueError, TypeError)
  - KB source & interface registration
  - Catalog I/O port definition
  - Frontend compound companion wiring
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet
from dorian.pipeline.printout import (
    PRINTOUT_EXPANSION_RULE,
    _PRINTOUT_SNIPPET_CODE,
    _expand_printout,
    expand_printout_nodes,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "frontend"
_BACKEND = _ROOT / "dorian"


# ===========================================================================
# 1. Expansion rule
# ===========================================================================

class TestPrintoutExpansionRule:
    """PRINTOUT_EXPANSION_RULE replaces dorian.io.printout with a Snippet."""

    def test_rule_exists(self):
        assert PRINTOUT_EXPANSION_RULE is not None
        assert PRINTOUT_EXPANSION_RULE.description

    def test_rule_matches_printout_operator(self):
        from dorian.pipeline.parser import match

        dag = DAG(
            nodes={"p1": Operator(name="dorian.io.printout", language="python")},
            edges=[],
        )
        matched, candidate = match(PRINTOUT_EXPANSION_RULE.pattern, dag)
        assert matched
        assert candidate["n"] == "p1"

    def test_rule_does_not_match_other_operators(self):
        from dorian.pipeline.parser import match

        dag = DAG(
            nodes={"op": Operator(name="sklearn.svm.SVC", language="python")},
            edges=[],
        )
        matched, _ = match(PRINTOUT_EXPANSION_RULE.pattern, dag)
        assert not matched

    def test_expansion_replaces_operator_with_snippet(self):
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "print1": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[Edge("op", "print1", position=0, output=0)],
        )
        result = _expand_printout(dag, {"n": "print1"}, {})

        # Original printout node removed
        assert "print1" not in result.nodes
        # Snippet replacement created
        assert "printout_print1" in result.nodes
        node = result.nodes["printout_print1"]
        assert isinstance(node, Snippet)
        assert node.name == "dorian.io.printout"
        assert node.language == "python"

    def test_expansion_preserves_upstream_edges(self):
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "print1": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[Edge("op", "print1", position=0, output=0)],
        )
        result = _expand_printout(dag, {"n": "print1"}, {})

        # Edge should now go to the snippet
        assert len(result.edges) == 1
        e = result.edges[0]
        assert e.source == "op"
        assert e.destination == "printout_print1"
        assert e.position == 0

    def test_expansion_preserves_downstream_edges(self):
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "print1": Operator(name="dorian.io.printout", language="python"),
                "next": Operator(name="some.operator", language="python"),
            },
            edges=[
                Edge("op", "print1", position=0, output=0),
                Edge("print1", "next", position=0, output=0),
            ],
        )
        result = _expand_printout(dag, {"n": "print1"}, {})

        # Both edges should be rewired
        assert len(result.edges) == 2
        sources = {e.source for e in result.edges}
        destinations = {e.destination for e in result.edges}
        assert "printout_print1" in sources
        assert "printout_print1" in destinations

    def test_expansion_preserves_non_printout_nodes(self):
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "param": Parameter(name="model", dtype="str", value="gpt-4"),
                "print1": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("param", "op", position="model"),
                Edge("op", "print1", position=0, output=0),
            ],
        )
        result = _expand_printout(dag, {"n": "print1"}, {})
        assert "op" in result.nodes
        assert "param" in result.nodes

    def test_expand_printout_nodes_convenience(self):
        """expand_printout_nodes() wraps sync_apply correctly."""
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "print1": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[Edge("op", "print1", position=0, output=0)],
        )
        result = expand_printout_nodes(dag, "test-session")
        # Printout should be expanded
        assert "print1" not in result.nodes
        assert any(isinstance(n, Snippet) for n in result.nodes.values())

    def test_no_op_when_no_printout_nodes(self):
        """Pipeline without printout nodes should pass through unchanged."""
        dag = DAG(
            nodes={
                "op": Operator(name="sklearn.svm.SVC", language="python"),
                "param": Parameter(name="C", dtype="float", value="1.0"),
            },
            edges=[Edge("param", "op", position="C")],
        )
        result = expand_printout_nodes(dag, "test-session")
        assert result.nodes.keys() == dag.nodes.keys()


# ===========================================================================
# 2. Snippet execution
# ===========================================================================

class TestPrintoutSnippetExecution:
    """The printout Snippet correctly formats diverse data types."""

    @pytest.fixture()
    def run_snippet(self):
        from dorian.pipeline.operator_resolver import _resolve_snippet

        snippet = Snippet(
            name="dorian.io.printout",
            code=_PRINTOUT_SNIPPET_CODE,
            language="python",
        )
        return _resolve_snippet(snippet)

    def test_string_input(self, run_snippet):
        result = run_snippet("Hello, world!")
        assert result["type"] == "text"
        assert result["content"] == "Hello, world!"

    def test_json_string_input(self, run_snippet):
        result = run_snippet('{"key": "value"}')
        assert result["type"] == "json"
        assert result["content"] == {"key": "value"}

    def test_dict_input(self, run_snippet):
        result = run_snippet({"key": "value", "num": 42})
        assert result["type"] == "json"
        assert result["content"] == {"key": "value", "num": 42}

    def test_list_input(self, run_snippet):
        result = run_snippet([1, 2, 3, 4])
        assert result["type"] == "json"
        assert result["content"] == [1, 2, 3, 4]

    def test_tuple_input(self, run_snippet):
        result = run_snippet((10, 20))
        assert result["type"] == "json"
        assert result["content"] == [10, 20]

    def test_int_input(self, run_snippet):
        result = run_snippet(42)
        assert result["type"] == "scalar"
        assert result["content"] == 42

    def test_float_input(self, run_snippet):
        result = run_snippet(3.14)
        assert result["type"] == "scalar"
        assert result["content"] == 3.14

    def test_bool_input(self, run_snippet):
        result = run_snippet(True)
        assert result["type"] == "scalar"
        assert result["content"] is True

    def test_none_input(self, run_snippet):
        result = run_snippet(None)
        assert result["type"] == "text"
        assert result["content"] == "None"

    def test_large_list_truncation(self, run_snippet):
        """Lists larger than 100 elements are truncated."""
        big_list = list(range(200))
        result = run_snippet(big_list)
        assert result["type"] == "json"
        assert len(result["content"]) == 100

    def test_fallback_for_unknown_type(self, run_snippet):
        """Unknown types fall back to str(data)."""

        class CustomObj:
            def __str__(self):
                return "custom-obj-repr"

        result = run_snippet(CustomObj())
        assert result["type"] == "text"
        assert result["content"] == "custom-obj-repr"


# ===========================================================================
# 3. LLM ChatCompletion mock
# ===========================================================================

class TestPrintoutLLMResponse:
    """Printout correctly formats OpenAI-compatible ChatCompletion objects."""

    @pytest.fixture()
    def run_snippet(self):
        from dorian.pipeline.operator_resolver import _resolve_snippet

        snippet = Snippet(
            name="dorian.io.printout",
            code=_PRINTOUT_SNIPPET_CODE,
            language="python",
        )
        return _resolve_snippet(snippet)

    @staticmethod
    def _make_mock_completion(content="Hello", model="gpt-4o", tokens=None):
        """Build a mock ChatCompletion with the given fields."""

        class _Usage:
            prompt_tokens = (tokens or {}).get("prompt", 10)
            completion_tokens = (tokens or {}).get("completion", 20)
            total_tokens = (tokens or {}).get("total", 30)

        class _Msg:
            pass
        _Msg.content = content
        _Msg.role = "assistant"

        class _Choice:
            message = _Msg()
            index = 0
            finish_reason = "stop"

        class _Completion:
            choices = [_Choice()]
            pass
        _Completion.model = model
        _Completion.usage = _Usage()
        _Completion.id = "chatcmpl-test"

        return _Completion()

    def test_llm_response_type(self, run_snippet):
        # ChatCompletion-like objects without model_dump/to_dict fall back to
        # the generic text fallback (str(data)) in the current snippet.
        result = run_snippet(self._make_mock_completion())
        assert result["type"] == "text"

    def test_llm_response_content(self, run_snippet):
        # The fallback branch returns str(data), not the inner message content.
        completion = self._make_mock_completion(content="Paris is the capital.")
        result = run_snippet(completion)
        assert result["type"] == "text"
        assert result["content"] == str(completion)

    def test_llm_response_model(self, run_snippet):
        # Generic fallback — no "model" key in the output dict.
        result = run_snippet(self._make_mock_completion(model="anthropic/claude-sonnet-4"))
        assert "model" not in result

    def test_llm_response_usage(self, run_snippet):
        # Generic fallback — no "usage" key in the output dict.
        tokens = {"prompt": 5, "completion": 15, "total": 20}
        result = run_snippet(self._make_mock_completion(tokens=tokens))
        assert "usage" not in result

    def test_llm_response_no_usage(self, run_snippet):
        """ChatCompletion-like objects without model_dump fall through to text fallback."""

        class _NoUsage:
            choices = []
            model = "test"
            usage = None

        obj = _NoUsage()
        result = run_snippet(obj)
        assert result["type"] == "text"
        assert result["content"] == str(obj)


# ===========================================================================
# 4. Operator resolver builtins
# ===========================================================================

class TestSnippetBuiltins:
    """The resolver snippet builtins include functions needed by printout."""

    def test_hasattr_in_builtins(self):
        from dorian.pipeline.operator_resolver import _SNIPPET_BUILTINS

        assert "hasattr" in _SNIPPET_BUILTINS
        assert _SNIPPET_BUILTINS["hasattr"] is hasattr

    def test_getattr_in_builtins(self):
        from dorian.pipeline.operator_resolver import _SNIPPET_BUILTINS

        assert "getattr" in _SNIPPET_BUILTINS
        assert _SNIPPET_BUILTINS["getattr"] is getattr

    def test_valueerror_in_builtins(self):
        from dorian.pipeline.operator_resolver import _SNIPPET_BUILTINS

        assert "ValueError" in _SNIPPET_BUILTINS

    def test_typeerror_in_builtins(self):
        from dorian.pipeline.operator_resolver import _SNIPPET_BUILTINS

        assert "TypeError" in _SNIPPET_BUILTINS

    def test_exception_in_builtins(self):
        from dorian.pipeline.operator_resolver import _SNIPPET_BUILTINS

        assert "Exception" in _SNIPPET_BUILTINS


# ===========================================================================
# 5. Execution chain integration
# ===========================================================================

class TestExecutionChainIntegration:
    """execution.py imports and calls expand_printout_nodes."""

    def test_execution_imports_expand_printout(self):
        src = (_BACKEND / "pipeline" / "execution.py").read_text()
        assert "expand_printout_nodes" in src

    def test_expansion_order_in_chain(self):
        """expand_printout_nodes must run AFTER compound expansion
        (which may create the printout node) and BEFORE the platform guard
        (which rejects dorian.* operators).
        """
        src = (_BACKEND / "pipeline" / "execution.py").read_text()
        # Search for the actual *calls* (with parentheses), not imports
        compound_pos = src.index("expand_compound_operators(pipeline")
        printout_pos = src.index("expand_printout_nodes(pipeline")
        guard_pos = src.index("node.name.startswith(\"dorian.\")")
        assert compound_pos < printout_pos < guard_pos

    def test_printout_not_blocked_by_guard(self):
        """After expansion the printout node becomes a Snippet (not Operator),
        so the dorian.* guard should not block it.
        """
        dag = DAG(
            nodes={
                "op": Operator(name="openrouter.chat.completion", language="python"),
                "print1": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[Edge("op", "print1", position=0, output=0)],
        )
        result = expand_printout_nodes(dag, "test-session")
        # No Operator nodes with dorian.* prefix should remain
        for nid, node in result.nodes.items():
            if isinstance(node, Operator):
                assert not node.name.startswith("dorian."), (
                    f"Operator {node.name} should have been expanded"
                )


# ===========================================================================
# 6. KB source
# ===========================================================================

class TestPrintoutKBSource:
    """KB knowledge source for dorian.io.printout."""

    def test_source_file_exists(self):
        # Python sources were converted to .kb files (parsed by the rust
        # KB loader into the snapshot). Verify the .kb file is on disk.
        p = _BACKEND / "knowledge" / "sources" / "printout.kb"
        assert p.exists(), "dorian/knowledge/sources/printout.kb missing"

    def test_source_has_knowledge_string(self):
        p = _BACKEND / "knowledge" / "sources" / "printout.kb"
        knowledge = p.read_text()
        assert "dorian.io.printout" in knowledge
        assert "Display Output" in knowledge

    def test_interface_registered(self):
        p = _BACKEND / "knowledge" / "sources" / "interfaces.kb"
        knowledge = p.read_text()
        assert "Display Output" in knowledge


# ===========================================================================
# 7. Catalog I/O
# ===========================================================================

class TestPrintoutCatalogIO:
    """Catalog defines I/O ports for the Display Output interface."""

    def test_display_output_in_interface_io(self):
        from dorian.pipeline.generation.catalog import _INTERFACE_IO

        assert "Display Output" in _INTERFACE_IO

    def test_display_output_has_data_input(self):
        from dorian.pipeline.generation.catalog import _INTERFACE_IO

        inputs, _ = _INTERFACE_IO["Display Output"]
        assert len(inputs) >= 1
        assert inputs[0].name == "data"

    def test_display_output_has_formatted_output(self):
        from dorian.pipeline.generation.catalog import _INTERFACE_IO

        _, outputs = _INTERFACE_IO["Display Output"]
        assert len(outputs) >= 1
        assert outputs[0].name == "formatted"


# ===========================================================================
# 8. Snippet code quality
# ===========================================================================

class TestSnippetCodeQuality:
    """The printout snippet code is well-formed."""

    def test_snippet_defines_foo(self):
        assert "def foo(data):" in _PRINTOUT_SNIPPET_CODE

    def test_snippet_has_single_arg(self):
        """foo() must accept exactly one positional argument (the data)."""
        # Extract the function signature from the snippet code
        match = re.search(r"def foo\(([^)]*)\)", _PRINTOUT_SNIPPET_CODE)
        assert match
        args = [a.strip() for a in match.group(1).split(",") if a.strip()]
        assert args == ["data"]

    def test_snippet_returns_dict_with_type(self):
        """Every return statement should include 'type' key."""
        # Find all return {...} statements
        returns = re.findall(r'return\s+\{([^}]+)\}', _PRINTOUT_SNIPPET_CODE)
        for ret in returns:
            assert '"type"' in ret, f"Return statement missing 'type': {ret}"


# ===========================================================================
# 9. Frontend compound companion
# ===========================================================================

class TestFrontendCompoundCompanion:
    """buildCompoundSubgraph adds dorian.io.printout downstream of openrouter."""

    def test_companion_registry_in_source(self):
        src = (_FRONTEND / "hooks" / "usePipelineComposition.ts").read_text()
        assert "COMPOUND_COMPANIONS" in src
        assert "dorian.io.printout" in src

    def test_companion_linked_to_openrouter(self):
        src = (_FRONTEND / "hooks" / "usePipelineComposition.ts").read_text()
        # The openrouter entry should reference dorian.io.printout
        assert '"openrouter.chat.completion"' in src
        # And the printout companion
        idx_openrouter = src.index('"openrouter.chat.completion"')
        idx_printout = src.index('"dorian.io.printout"')
        # printout reference should come after the openrouter key
        assert idx_printout > idx_openrouter

    def test_companion_edge_created(self):
        """Source code should create an edge from operator to companion."""
        src = (_FRONTEND / "hooks" / "usePipelineComposition.ts").read_text()
        assert "companion" in src.lower()
        # Should have edge creation logic for companions
        assert "source: opId" in src
        assert "target: compId" in src

    def test_companion_tagged_for_cascade_delete(self):
        """Companion nodes must carry compoundGroupId for cascade-delete."""
        src = (_FRONTEND / "hooks" / "usePipelineComposition.ts").read_text()
        # Within the companions.forEach section, compoundGroupId should be set
        comp_section = src[src.index("COMPOUND_COMPANIONS[operator.name]"):]
        assert "compoundGroupId: opId" in comp_section
