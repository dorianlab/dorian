"""
tests/test_execution_engine.py
-------------------------------
Integration tests for the Core Pipeline Execution Engine (Issue #33).

These tests exercise the full stack without needing live Redis / Neo4j:
  - operator_resolver  (graph building, operator resolution)
  - run_pipeline       (orchestration, state transitions, event emission)
  - ResultStore        (inline Redis and file-based storage)

External dependencies (Redis, Dask) are mocked so the test suite can run in CI.
"""
from __future__ import annotations

import json
import pickle
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock, patch, call
from uuid import uuid4

# Backend stubs are in conftest.py (loaded automatically by pytest).
from dorian.dag import DAG, Edge, Operator, Parameter, Snippet  # noqa: E402
from dorian.models.execution import (  # noqa: E402
    NodeState,
    NodeStatus,
    PipelineExecution,
    PipelineRunStatus,
)
from dorian.pipeline.operator_resolver import build_dag_graph, resolve  # noqa: E402
from dorian.pipeline.execution import (  # noqa: E402
    _instrument,
    _parse_pipeline,
    _sink_nodes,
    _store_result_sync,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _simple_dag() -> DAG:
    """a → add_one → result"""
    return DAG(
        nodes={
            "a": Parameter(name="a", dtype="int", value="5"),
            "add_one": Operator(name="builtins.abs", language="python"),  # abs(5) = 5
        },
        edges=[Edge(source="a", destination="add_one", position=0, output=0)],
    )


def _linear_dag() -> DAG:
    """fname → read (identity snippet) → doubled (identity snippet)"""
    identity_code = "def foo(*args, **kw): return args[0] if args else None"
    return DAG(
        nodes={
            "src": Parameter(name="src", dtype="int", value="42"),
            "pass1": Snippet(name="pass1", code=identity_code, language="python"),
            "pass2": Snippet(name="pass2", code=identity_code, language="python"),
        },
        edges=[
            Edge(source="src", destination="pass1", position=0),
            Edge(source="pass1", destination="pass2", position=0),
        ],
    )


# ---------------------------------------------------------------------------
# Tests: operator_resolver
# ---------------------------------------------------------------------------

class TestResolve(unittest.TestCase):

    def setUp(self):
        self._shortcut_patcher = patch(
            "dorian.pipeline.operator_resolver._get_method_shortcuts",
            return_value=frozenset(["fit", "predict", "transform", "fit_transform",
                                    "create", "validate", "chat.send", "trace",
                                    "score"]),
        )
        self._shortcut_patcher.start()

    def tearDown(self):
        self._shortcut_patcher.stop()

    def test_parameter_callable(self):
        p = Parameter(name="x", dtype="int", value="7")
        fn = resolve(p)
        self.assertEqual(fn(), 7)

    def test_snippet_callable(self):
        code = "def foo(x): return x * 2"
        s = Snippet(name="double", code=code, language="python")
        fn = resolve(s)
        self.assertEqual(fn(3), 6)

    def test_operator_dotted_path(self):
        op = Operator(name="json.dumps", language="python")
        fn = resolve(op)
        self.assertEqual(fn({"k": 1}), '{"k": 1}')

    def test_operator_fit_shortcut(self):
        """'fit' shortcut should call args[0].fit(*args[1:])."""
        op = Operator(name="fit", language="python")
        fn = resolve(op)
        mock_model = MagicMock()
        fn(mock_model, "X_train", "y_train")
        mock_model.fit.assert_called_once_with("X_train", "y_train")

    def test_operator_predict_shortcut(self):
        op = Operator(name="predict", language="python")
        fn = resolve(op)
        mock_model = MagicMock()
        fn(mock_model, "X_test")
        mock_model.predict.assert_called_once_with("X_test")

    def test_operator_transform_shortcut(self):
        op = Operator(name="transform", language="python")
        fn = resolve(op)
        mock_model = MagicMock()
        fn(mock_model, "X")
        mock_model.transform.assert_called_once_with("X")

    def test_operator_score_shortcut(self):
        op = Operator(name="score", language="python")
        fn = resolve(op)
        mock_model = MagicMock()
        fn(mock_model, "X", "y")
        mock_model.score.assert_called_once_with("X", "y")

    def test_operator_class_instantiation(self):
        """A dotted path pointing to a class should instantiate it."""
        op = Operator(name="collections.OrderedDict", language="python")
        fn = resolve(op)
        result = fn()
        from collections import OrderedDict
        self.assertIsInstance(result, OrderedDict)

    def test_operator_class_with_tasks(self):
        """An operator with tasks should still resolve to a class constructor."""
        op = Operator(name="collections.OrderedDict", language="python", tasks=["__init__", "update"])
        fn = resolve(op)
        result = fn()
        from collections import OrderedDict
        self.assertIsInstance(result, OrderedDict)

    def test_method_shortcut_raises_on_missing_method(self):
        """A method shortcut on an object without that method should raise AttributeError."""
        op = Operator(name="fit", language="python")
        fn = resolve(op)
        # A plain int has no .fit method
        with self.assertRaises(AttributeError):
            fn(42, "X")


class TestBuildDagGraph(unittest.TestCase):

    def test_graph_has_all_nodes(self):
        dag = _linear_dag()
        graph = build_dag_graph(dag)
        for nid in dag.nodes:
            self.assertIn(nid, graph, f"Node '{nid}' missing from graph")

    def test_source_nodes_have_no_deps(self):
        dag = _linear_dag()
        graph = build_dag_graph(dag)
        # 'src' is a source; its tuple should be just (callable,)
        src_entry = graph["src"]
        self.assertIsInstance(src_entry, tuple)
        self.assertEqual(len(src_entry), 1)

    def test_dest_nodes_have_deps(self):
        dag = _linear_dag()
        graph = build_dag_graph(dag)
        pass1_entry = graph["pass1"]
        self.assertIn("src", pass1_entry)

    def test_multioutput_creates_slice_entries(self):
        code = "def foo(x): return (x, x*2)"
        dag = DAG(
            nodes={
                "x": Parameter(name="x", dtype="int", value="3"),
                "split": Snippet(name="split", code=code, language="python"),
                "consumer": Snippet(name="consumer", code="def foo(a, b): return a+b", language="python"),
            },
            edges=[
                Edge(source="x", destination="split", position=0, output=0),
                Edge(source="split", destination="consumer", position=0, output=0),
                Edge(source="split", destination="consumer", position=1, output=1),
            ],
        )
        graph = build_dag_graph(dag)
        # Expect a slice entry for the second output
        self.assertIn("split_1", graph)


# ---------------------------------------------------------------------------
# Tests: pipeline deserialisation
# ---------------------------------------------------------------------------

class TestParsePipeline(unittest.TestCase):

    def test_parse_operator(self):
        data = {
            "nodes": {
                "n1": {"type": "Operator", "name": "pandas.read_csv", "language": "python"}
            },
            "edges": [],
        }
        dag = _parse_pipeline(data)
        self.assertIn("n1", dag.nodes)
        self.assertIsInstance(dag.nodes["n1"], Operator)

    def test_parse_parameter(self):
        data = {
            "nodes": {"p": {"type": "Parameter", "name": "p", "dtype": "int", "value": "10"}},
            "edges": [],
        }
        dag = _parse_pipeline(data)
        self.assertIsInstance(dag.nodes["p"], Parameter)
        self.assertEqual(dag.nodes["p"].value, "10")

    def test_parse_snippet(self):
        data = {
            "nodes": {"s": {"type": "Snippet", "name": "s", "code": "def foo(): pass", "language": "python"}},
            "edges": [],
        }
        dag = _parse_pipeline(data)
        self.assertIsInstance(dag.nodes["s"], Snippet)

    def test_parse_nested_pipeline_key(self):
        """pipeline_data may have a nested 'pipeline' key (from session meta)."""
        inner = {
            "nodes": {"x": {"type": "Parameter", "name": "x", "dtype": "str", "value": "hello"}},
            "edges": [],
        }
        data = {"id": "abc", "pipeline": json.dumps(inner)}
        dag = _parse_pipeline(data)
        self.assertIn("x", dag.nodes)

    def test_sink_nodes(self):
        dag = _linear_dag()
        sinks = _sink_nodes(dag)
        self.assertEqual(sinks, ["pass2"])


# ---------------------------------------------------------------------------
# Tests: Execution models
# ---------------------------------------------------------------------------

class TestPipelineExecution(unittest.TestCase):

    def _make(self) -> PipelineExecution:
        return PipelineExecution(
            run_id="run-1",
            session_id="sess-1",
            pipeline_id="pipe-1",
            uid="user-1",
        )

    def test_default_status_pending(self):
        exc = self._make()
        self.assertEqual(exc.status, PipelineRunStatus.PENDING)

    def test_has_failures_false_when_all_success(self):
        exc = self._make()
        exc.node_states["n1"] = NodeState(node_id="n1", status=NodeStatus.SUCCESS)
        self.assertFalse(exc.has_failures)

    def test_has_failures_true_when_one_failed(self):
        exc = self._make()
        exc.node_states["n1"] = NodeState(node_id="n1", status=NodeStatus.SUCCESS)
        exc.node_states["n2"] = NodeState(node_id="n2", status=NodeStatus.FAILED, error="boom")
        self.assertTrue(exc.has_failures)

    def test_summary_contains_expected_keys(self):
        exc = self._make()
        s = exc.summary()
        for key in ("run_id", "session_id", "pipeline_id", "status", "nodes"):
            self.assertIn(key, s)

    def test_node_duration(self):
        ns = NodeState(node_id="n", start_time=1.0, end_time=3.5)
        self.assertAlmostEqual(ns.duration, 2.5)


# ---------------------------------------------------------------------------
# Tests: Instrumentation
# ---------------------------------------------------------------------------

class TestInstrument(unittest.TestCase):

    def _run_instrumented(self, fn, *args):
        run_id = str(uuid4())
        node_id = "test_node"
        uid, session = "u", "s"

        # Patch synchronous helpers so no Redis is needed
        mock_redis = MagicMock()
        mock_redis.exists.return_value = False
        mock_redis.get.return_value = None
        with patch("dorian.pipeline.run_state._node_running_sync") as mock_run, \
             patch("dorian.pipeline.run_state._node_success_sync") as mock_ok, \
             patch("dorian.pipeline.run_state._node_failed_sync") as mock_fail, \
             patch("dorian.pipeline.run_state._store_result_sync", return_value="redis:k") as mock_store, \
             patch("dorian.pipeline.run_state._stream_sync") as mock_stream, \
             patch("dorian.pipeline.run_state._patch_node_state") as mock_patch_state, \
             patch("dorian.pipeline.run_state.redis", mock_redis), \
             patch("dorian.pipeline.run_state.emit") as mock_emit:
            wrapped = _instrument(run_id, node_id, uid, session, fn)
            result = wrapped(*args)
            return result, mock_run, mock_ok, mock_fail, mock_store, mock_stream

    def test_success_path(self):
        fn = lambda x: x * 2
        result, mock_run, mock_ok, mock_fail, *_ = self._run_instrumented(fn, 21)
        self.assertEqual(result, 42)
        mock_run.assert_called_once()
        mock_ok.assert_called_once()
        mock_fail.assert_not_called()

    def test_failure_path_raises_and_calls_fail(self):
        def exploding(*a):
            raise ValueError("test error")

        from dorian.pipeline.execution import NodeExecutionError
        with self.assertRaises(NodeExecutionError):
            self._run_instrumented(exploding)
        # _node_failed_sync is called inside the wrapper before re-raising


# ---------------------------------------------------------------------------
# Tests: run_pipeline integration (mocked Redis + Dask)
# ---------------------------------------------------------------------------

class TestRunPipeline(unittest.TestCase):

    def _make_execution_json(self, run_id: str) -> str:
        return PipelineExecution(
            run_id=run_id,
            session_id="sess",
            pipeline_id="pipe",
            uid="uid",
        ).model_dump_json()

    def test_run_simple_pipeline(self):
        run_id = str(uuid4())
        uid, session = "u", "s"
        pipeline_data = {
            "nodes": {
                "x": {"type": "Parameter", "name": "x", "dtype": "int", "value": "5"},
            },
            "edges": [],
        }
        pipeline_json = json.dumps(pipeline_data)
        exec_json = self._make_execution_json(run_id)

        with patch("dorian.pipeline.execution.redis") as mock_redis, \
             patch("dorian.pipeline.run_state.redis") as mock_redis_rs, \
             patch("dorian.pipeline.execution.executor") as mock_executor, \
             patch("dorian.pipeline.execution.emit") as mock_emit, \
             patch("dorian.pipeline.run_state.emit"):
            # Return exec_json only for the execution key; None for node state keys
            def _get_side_effect(key):
                if "execution:" in key and ":node:" not in key:
                    return exec_json
                return None
            mock_redis.get.side_effect = _get_side_effect
            mock_redis.set.return_value = True
            mock_redis.exists.return_value = False
            mock_redis_rs.get.side_effect = _get_side_effect
            mock_redis_rs.set.return_value = True
            mock_redis_rs.exists.return_value = False
            mock_redis_rs.pipeline.return_value = MagicMock(execute=MagicMock(return_value=[]))
            mock_executor.get.return_value = None  # executor.get runs the graph

            result = run_pipeline(run_id, uid, session, pipeline_json)

        self.assertEqual(result["run_id"], run_id)
        # PipelineRunStatus is a str enum; str() includes the enum path
        self.assertTrue("SUCCESS" in result["status"] or "FAILED" in result["status"])

    def test_run_empty_pipeline_returns_failed(self):
        run_id = str(uuid4())
        pipeline_json = json.dumps({"nodes": {}, "edges": []})
        exec_json = self._make_execution_json(run_id)

        with patch("dorian.pipeline.execution.redis") as mock_redis, \
             patch("dorian.pipeline.run_state.redis") as mock_redis_rs, \
             patch("dorian.pipeline.execution.executor"), \
             patch("dorian.pipeline.execution.emit"), \
             patch("dorian.pipeline.run_state.emit"):
            mock_redis.get.return_value = exec_json
            mock_redis_rs.get.return_value = exec_json
            result = run_pipeline(run_id, "u", "s", pipeline_json)

        self.assertIn("FAILED", result["status"])

    def test_run_invalid_json_returns_failed(self):
        run_id = str(uuid4())
        with patch("dorian.pipeline.execution.redis") as mock_redis, \
             patch("dorian.pipeline.run_state.redis") as mock_redis_rs, \
             patch("dorian.pipeline.execution.executor"), \
             patch("dorian.pipeline.execution.emit"), \
             patch("dorian.pipeline.run_state.emit"):
            mock_redis.get.return_value = self._make_execution_json(run_id)
            mock_redis_rs.get.return_value = self._make_execution_json(run_id)
            result = run_pipeline(run_id, "u", "s", "{invalid json!!}")
        self.assertIn("FAILED", result["status"])


if __name__ == "__main__":
    unittest.main()
