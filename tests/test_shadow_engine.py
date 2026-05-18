"""
tests/test_shadow_engine.py
----------------------------
Tests for the shadow engine integration (Phase 1.7).

Tests the Python shadow module (dorian/pipeline/shadow.py) which
bridges the Rust engine's structural validation with the Python
execution pipeline.

Some tests use the actual dorian_native Rust module (if compiled);
others mock it to test the Python-side logic independently.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dorian.dag import DAG, Edge, Operator, Parameter, Snippet
from dorian.pipeline.execution import (
    _compute_graph_depth,
    _node_to_shadow_dict,
)
from dorian.pipeline.shadow import (
    shadow_validate,
    launch_shadow_validation,
    _shadow_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def linear_pipeline():
    """A → B → C linear pipeline."""
    return DAG(
        nodes={
            "a": Parameter(name="input", dtype="string", value="hello"),
            "b": Operator(name="sklearn.preprocessing.StandardScaler", language="python"),
            "c": Operator(name="sklearn.linear_model.LinearRegression", language="python"),
        },
        edges=[
            Edge(source="a", destination="b", position=0, output=0),
            Edge(source="b", destination="c", position=1, output=0),
        ],
    )


@pytest.fixture
def diamond_pipeline():
    """Diamond shape: A → B, A → C, B → D, C → D."""
    return DAG(
        nodes={
            "a": Parameter(name="data", dtype="string", value="x"),
            "b": Operator(name="sklearn.preprocessing.StandardScaler", language="python"),
            "c": Operator(name="sklearn.preprocessing.MinMaxScaler", language="python"),
            "d": Operator(name="sklearn.linear_model.LinearRegression", language="python"),
        },
        edges=[
            Edge(source="a", destination="b", position=0, output=0),
            Edge(source="a", destination="c", position=0, output=0),
            Edge(source="b", destination="d", position=1, output=0),
            Edge(source="c", destination="d", position=2, output=0),
        ],
    )


@pytest.fixture
def snippet_pipeline():
    """Pipeline with a Snippet node."""
    return DAG(
        nodes={
            "p": Parameter(name="x", dtype="int", value="42"),
            "s": Snippet(name="double", code="def foo(x): return x * 2", language="python"),
        },
        edges=[
            Edge(source="p", destination="s", position=0, output=0),
        ],
    )


# ---------------------------------------------------------------------------
# Tests: _node_to_shadow_dict
# ---------------------------------------------------------------------------

class TestNodeToShadowDict:
    def test_operator(self):
        op = Operator(name="sklearn.svm.SVC", language="python")
        d = _node_to_shadow_dict(op)
        assert d["class_type"] == "Operator"
        assert d["name"] == "sklearn.svm.SVC"
        assert d["language"] == "python"

    def test_parameter(self):
        p = Parameter(name="alpha", dtype="float", value="0.5")
        d = _node_to_shadow_dict(p)
        assert d["class_type"] == "Parameter"
        assert d["name"] == "alpha"
        assert d["dtype"] == "float"
        assert d["value"] == "0.5"

    def test_snippet(self):
        s = Snippet(name="preprocess", code="def foo(x): return x", language="python")
        d = _node_to_shadow_dict(s)
        assert d["class_type"] == "Snippet"
        assert d["name"] == "preprocess"
        assert "def foo" in d["code"]


# ---------------------------------------------------------------------------
# Tests: _compute_graph_depth
# ---------------------------------------------------------------------------

class TestComputeGraphDepth:
    def test_empty_dag(self):
        dag = DAG(nodes={}, edges=[])
        assert _compute_graph_depth(dag) == 0

    def test_single_node(self):
        dag = DAG(
            nodes={"a": Parameter(name="x", dtype="int", value="1")},
            edges=[],
        )
        assert _compute_graph_depth(dag) == 1

    def test_linear(self, linear_pipeline):
        # A → B → C = depth 3 (levels 0, 1, 2)
        assert _compute_graph_depth(linear_pipeline) == 3

    def test_diamond(self, diamond_pipeline):
        # A → B → D, A → C → D = depth 3 (A=0, B&C=1, D=2)
        assert _compute_graph_depth(diamond_pipeline) == 3

    def test_wide_graph(self):
        """Many independent nodes = depth 1."""
        nodes = {f"n{i}": Parameter(name=f"p{i}", dtype="int", value=str(i)) for i in range(10)}
        dag = DAG(nodes=nodes, edges=[])
        assert _compute_graph_depth(dag) == 1


# ---------------------------------------------------------------------------
# Tests: shadow_validate (with mocked dorian_native)
# ---------------------------------------------------------------------------

class TestShadowValidate:
    def _make_pipeline_json(self, pipeline: DAG) -> str:
        """Convert a DAG to the JSON format expected by the Rust engine."""
        return json.dumps({
            "nodes": {
                nid: _node_to_shadow_dict(node)
                for nid, node in pipeline.nodes.items()
            },
            "edges": [
                {
                    "source": e.source,
                    "destination": e.destination,
                    "position": e.position,
                    "output": e.output,
                }
                for e in pipeline.edges
            ],
        })

    @patch("dorian.pipeline.shadow._get_native")
    def test_native_not_available(self, mock_get_native, linear_pipeline):
        """When dorian_native is not compiled, shadow_validate returns None."""
        mock_get_native.return_value = None
        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json=self._make_pipeline_json(linear_pipeline),
            python_node_ids=list(linear_pipeline.nodes.keys()),
            python_sink_nodes=["c"],
            python_graph_depth=3,
        )
        assert result is None

    @patch("dorian.pipeline.shadow._get_native")
    def test_matching_graph(self, mock_get_native, linear_pipeline):
        """When Rust and Python agree, no discrepancies are reported."""
        mock_native = MagicMock()
        mock_get_native.return_value = mock_native

        # Mock Rust returning a matching plan
        mock_native.shadow_validate_plan.return_value = json.dumps({
            "valid": True,
            "node_count": 3,
            "depth": 3,
            "max_concurrency": 1,
            "sink_nodes": ["c"],
            "levels": [["a"], ["b"], ["c"]],
            "topo_order": ["a", "b", "c"],
            "runtime_map": {"a": "Engine", "b": "Python", "c": "Python"},
            "errors": [],
            "parse_time_ms": 0.1,
            "plan_time_ms": 0.05,
        })
        mock_native.shadow_compare_graphs.return_value = json.dumps({
            "node_count_match": True,
            "sink_match": True,
            "level_count_match": True,
            "rust_node_count": 3,
            "python_node_count": 3,
            "rust_sink_nodes": ["c"],
            "python_sink_nodes": ["c"],
            "rust_depth": 3,
            "python_depth": 3,
            "missing_in_rust": [],
            "extra_in_rust": [],
        })

        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json=self._make_pipeline_json(linear_pipeline),
            python_node_ids=list(linear_pipeline.nodes.keys()),
            python_sink_nodes=["c"],
            python_graph_depth=3,
        )

        assert result is not None
        assert result["discrepancy_count"] == 0
        assert result["valid"] is True

    @patch("dorian.pipeline.shadow._get_native")
    def test_node_count_mismatch(self, mock_get_native, linear_pipeline):
        """Detect when Rust sees fewer nodes than Python."""
        mock_native = MagicMock()
        mock_get_native.return_value = mock_native

        mock_native.shadow_validate_plan.return_value = json.dumps({
            "valid": True,
            "node_count": 2,  # Mismatch
            "depth": 2,
            "max_concurrency": 1,
            "sink_nodes": ["c"],
            "levels": [["a"], ["c"]],
            "topo_order": ["a", "c"],
            "runtime_map": {"a": "Engine", "c": "Python"},
            "errors": [],
            "parse_time_ms": 0.1,
            "plan_time_ms": 0.05,
        })
        mock_native.shadow_compare_graphs.return_value = json.dumps({
            "node_count_match": False,
            "sink_match": True,
            "level_count_match": False,
            "rust_node_count": 2,
            "python_node_count": 3,
            "rust_sink_nodes": ["c"],
            "python_sink_nodes": ["c"],
            "rust_depth": 2,
            "python_depth": 3,
            "missing_in_rust": ["b"],
            "extra_in_rust": [],
        })

        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json=self._make_pipeline_json(linear_pipeline),
            python_node_ids=list(linear_pipeline.nodes.keys()),
            python_sink_nodes=["c"],
            python_graph_depth=3,
        )

        assert result is not None
        assert result["discrepancy_count"] > 0
        assert any("Node count" in d for d in result["discrepancies"])

    @patch("dorian.pipeline.shadow._get_native")
    def test_rust_validation_failure(self, mock_get_native, linear_pipeline):
        """Handle Rust reporting validation errors."""
        mock_native = MagicMock()
        mock_get_native.return_value = mock_native

        mock_native.shadow_validate_plan.return_value = json.dumps({
            "valid": False,
            "errors": ["Validation: cycle detected"],
            "parse_time_ms": 0.1,
            "plan_time_ms": 0.0,
        })
        mock_native.shadow_compare_graphs.return_value = json.dumps({
            "node_count_match": True,
            "sink_match": True,
            "level_count_match": True,
            "rust_node_count": 3,
            "python_node_count": 3,
            "rust_sink_nodes": ["c"],
            "python_sink_nodes": ["c"],
            "rust_depth": 3,
            "python_depth": 3,
            "missing_in_rust": [],
            "extra_in_rust": [],
        })

        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json=self._make_pipeline_json(linear_pipeline),
            python_node_ids=list(linear_pipeline.nodes.keys()),
            python_sink_nodes=["c"],
            python_graph_depth=3,
        )

        assert result is not None
        assert result["valid"] is False
        assert any("validation failed" in d for d in result["discrepancies"])

    @patch("dorian.pipeline.shadow._get_native")
    def test_rust_exception_returns_error_dict(self, mock_get_native, linear_pipeline):
        """If Rust raises an exception, shadow_validate returns an error dict."""
        mock_native = MagicMock()
        mock_get_native.return_value = mock_native
        mock_native.shadow_validate_plan.side_effect = ValueError("bad JSON")

        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json="{}",
            python_node_ids=["a"],
            python_sink_nodes=["a"],
            python_graph_depth=1,
        )

        assert result is not None
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: launch_shadow_validation
# ---------------------------------------------------------------------------

class TestLaunchShadowValidation:
    @patch("dorian.pipeline.shadow._shadow_enabled", return_value=False)
    def test_disabled(self, _mock_enabled):
        """Returns None when shadow engine is disabled."""
        result = launch_shadow_validation(
            run_id="run-1",
            uid="uid-1",
            session="sess-1",
            pipeline_json="{}",
            python_node_ids=[],
            python_sink_nodes=[],
            python_graph_depth=0,
        )
        assert result is None

    @patch("dorian.pipeline.shadow._shadow_enabled", return_value=True)
    @patch("dorian.pipeline.shadow.shadow_validate")
    def test_enabled_launches_thread(self, mock_validate, _mock_enabled):
        """When enabled, returns a thread that eventually calls shadow_validate."""
        thread = launch_shadow_validation(
            run_id="run-1",
            uid="uid-1",
            session="sess-1",
            pipeline_json="{}",
            python_node_ids=[],
            python_sink_nodes=[],
            python_graph_depth=0,
        )
        assert thread is not None
        thread.join(timeout=5)
        assert not thread.is_alive()
        mock_validate.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _shadow_enabled
# ---------------------------------------------------------------------------

class TestShadowEnabled:
    def test_returns_bool(self):
        """_shadow_enabled always returns a bool."""
        result = _shadow_enabled()
        assert isinstance(result, bool)

    @patch("dorian.pipeline.shadow._get_native", return_value=None)
    def test_shadow_validate_skips_when_no_native(self, _mock):
        """Even if enabled, shadow_validate returns None when native not compiled."""
        result = shadow_validate(
            run_id="r", uid="u", session="s",
            pipeline_json="{}", python_node_ids=[],
            python_sink_nodes=[], python_graph_depth=0,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests: native integration (only run if dorian_native is available)
# ---------------------------------------------------------------------------

def _has_native():
    try:
        import dorian_native
        return hasattr(dorian_native, "shadow_validate_plan")
    except ImportError:
        return False


@pytest.mark.skipif(not _has_native(), reason="dorian_native not compiled")
class TestNativeIntegration:
    """Integration tests that use the actual Rust dorian_native module."""

    def _make_pipeline_json(self, pipeline: DAG) -> str:
        return json.dumps({
            "nodes": {
                nid: _node_to_shadow_dict(node)
                for nid, node in pipeline.nodes.items()
            },
            "edges": [
                {
                    "source": e.source,
                    "destination": e.destination,
                    "position": e.position,
                    "output": e.output,
                }
                for e in pipeline.edges
            ],
        })

    def test_validate_linear(self, linear_pipeline):
        """Rust validates a simple linear pipeline."""
        import dorian_native
        result_json = dorian_native.shadow_validate_plan(
            self._make_pipeline_json(linear_pipeline)
        )
        result = json.loads(result_json)
        assert result["valid"] is True
        assert result["node_count"] == 3
        assert result["depth"] == 3

    def test_validate_diamond(self, diamond_pipeline):
        """Rust validates a diamond pipeline."""
        import dorian_native
        result_json = dorian_native.shadow_validate_plan(
            self._make_pipeline_json(diamond_pipeline)
        )
        result = json.loads(result_json)
        assert result["valid"] is True
        assert result["node_count"] == 4
        assert result["depth"] == 3
        assert result["max_concurrency"] == 2  # b and c run in parallel

    def test_compare_matching(self, linear_pipeline):
        """Rust comparison matches Python metadata."""
        import dorian_native
        result_json = dorian_native.shadow_compare_graphs(
            self._make_pipeline_json(linear_pipeline),
            list(linear_pipeline.nodes.keys()),
            ["c"],
            3,
        )
        result = json.loads(result_json)
        assert result["node_count_match"] is True
        assert result["sink_match"] is True
        assert result["level_count_match"] is True

    def test_compare_mismatch(self, linear_pipeline):
        """Rust detects when Python provides wrong node list."""
        import dorian_native
        result_json = dorian_native.shadow_compare_graphs(
            self._make_pipeline_json(linear_pipeline),
            ["a", "b"],  # Missing "c"
            ["c"],
            3,
        )
        result = json.loads(result_json)
        assert result["node_count_match"] is False
        assert "c" in result["extra_in_rust"]

    def test_full_shadow_validate(self, linear_pipeline):
        """End-to-end shadow_validate with real Rust engine."""
        result = shadow_validate(
            run_id="test-run",
            uid="test-uid",
            session="test-session",
            pipeline_json=self._make_pipeline_json(linear_pipeline),
            python_node_ids=list(linear_pipeline.nodes.keys()),
            python_sink_nodes=["c"],
            python_graph_depth=3,
        )
        assert result is not None
        assert result["discrepancy_count"] == 0
        assert result["valid"] is True
        assert result["rust_plan"]["parse_time_ms"] >= 0
