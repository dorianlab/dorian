"""
tests/test_compound_expansion.py
---------------------------------
Tests for the generic KB-driven compound operator expansion.

Stubs KB queries to avoid hitting real Neo4j/Redis.  Verifies that
``_expand_compound_operator`` and ``build_group`` correctly handle
any-length method chains using per-method I/O from the KB.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

# Backend stubs are in conftest.py (loaded automatically by pytest).

import pytest
from dorian.dag import DAG, Edge, Operator, Parameter
from dorian.pipeline.transforms import (
    _expand_compound_operator,
    sync_apply,
    COMPOUND_OPERATOR_EXPANSION_RULE,
)
from dorian.pipeline.group_builder import build_group

# ---------------------------------------------------------------------------
# KB mock data
# ---------------------------------------------------------------------------

_SKLEARN_ESTIMATOR_METHODS = ["__init__", "fit", "predict"]
_SKLEARN_TRANSFORMER_METHODS = ["__init__", "fit", "transform"]
_LLM_METHODS = ["__init__", "chat.send"]
_MODEL_TRACER_METHODS = ["__init__", "fit", "predict", "trace"]
_MODEL_AGNOSTIC_TRACER_METHODS = ["__init__", "predict", "trace"]

_METHOD_IO = {
    "Sklearn Estimator": {
        "fit": (
            [{"name": "X", "type": "any", "position": 1},
             {"name": "y", "type": "any", "position": 2}],
            [],
        ),
        "predict": (
            # predict consumes interface input X_test (pos 2), NOT X (pos 0).
            # That separation is what lets the evaluation procedure train
            # on X_train and predict on X_test with two different data edges.
            [{"name": "X_test", "type": "any", "position": 1}],
            [{"name": "predictions", "type": "any", "position": 0}],
        ),
    },
    "Sklearn Transformer": {
        "fit": (
            [{"name": "X", "type": "any", "position": 1}],
            [],
        ),
        "transform": (
            # Transformers are semantically single-input: fit.X and
            # transform.X operate on the same data. Mid-pipeline
            # preprocessors (StandardScaler, OrdinalEncoder, …) have
            # only one X. The eval procedure's "also transform X_test"
            # pattern is its own concern — it inserts explicit
            # .transform(fitted, X_test) nodes rather than relying on
            # compound expansion to synthesise test-side branches.
            [{"name": "X", "type": "any", "position": 1}],
            [{"name": "X_transformed", "type": "any", "position": 0}],
        ),
    },
    "LLM Chat Completion": {
        "chat.send": (
            [{"name": "messages", "type": "list[dict]", "position": "messages"}],
            [{"name": "response", "type": "ChatResponse", "position": 0}],
        ),
    },
    "Model Tracer": {
        "fit": (
            [{"name": "X", "type": "any", "position": 1},
             {"name": "y", "type": "any", "position": 2}],
            [],
        ),
        "predict": (
            [{"name": "X", "type": "any", "position": 1}],
            [{"name": "predictions", "type": "any", "position": 0}],
        ),
        "trace": (
            [],
            [{"name": "trace_urls", "type": "list", "position": 0}],
        ),
    },
    "Model Agnostic Tracer": {
        "predict": (
            [{"name": "model", "type": "any", "position": 1},
             {"name": "X_train", "type": "any", "position": 2},
             {"name": "X", "type": "any", "position": 3}],
            [],
        ),
        "trace": (
            [],
            [{"name": "trace_urls", "type": "list", "position": 0}],
        ),
    },
}

_INTERFACE_IO = {
    "Sklearn Estimator": (
        [{"name": "X", "position": 0},
         {"name": "y", "position": 1},
         {"name": "X_test", "position": 2}],
        [{"name": "predictions", "position": 0}],
    ),
    "Sklearn Transformer": (
        [{"name": "X", "position": 0}],
        [{"name": "X_transformed", "position": 0}],
    ),
    "LLM Chat Completion": (
        [{"name": "messages", "position": "messages"}],
        [{"name": "response", "position": 0}],
    ),
    "Model Tracer": (
        [{"name": "X", "position": 0}, {"name": "y", "position": 1}],
        [{"name": "predictions", "position": 0}, {"name": "trace_urls", "position": 1}],
    ),
    "Model Agnostic Tracer": (
        [{"name": "model", "position": 0}, {"name": "X_train", "position": 1}, {"name": "X", "position": 2}],
        [{"name": "trace_urls", "position": 0}],
    ),
}


def _mock_kb(interface_name, methods, method_io=None, interface_io=None, params=None, attributes=None, operator_name=None):
    """Return a dict of patch targets → return values for KB queries.

    When *operator_name* is given, ``get_operator_interface`` returns the
    interface only for that operator (``None`` for everything else), so
    ``sync_apply`` won't accidentally expand unrelated nodes.
    """
    if operator_name:
        def _get_iface(op):
            return interface_name if op == operator_name else None
        iface_mock = MagicMock(side_effect=_get_iface)
    else:
        iface_mock = MagicMock(return_value=interface_name)

    base = "dorian.knowledge.queries"
    return {
        f"{base}.get_operator_interface": iface_mock,
        f"{base}.get_method_sequence": MagicMock(return_value=methods),
        f"{base}.get_method_io": MagicMock(return_value=method_io or {}),
        f"{base}.get_interface_io": MagicMock(return_value=interface_io or ([], [])),
        f"{base}.get_operator_parameters": MagicMock(return_value=params or []),
        f"{base}.get_interface_attributes": MagicMock(return_value=attributes or []),
        f"{base}.get_all_interface_methods": MagicMock(
            return_value=frozenset({"fit", "predict", "transform", "chat.send", "validate", "trace"})
        ),
    }


# ---------------------------------------------------------------------------
# Tests: transforms._expand_compound_operator
# ---------------------------------------------------------------------------

class TestGenericExpansion:
    """Generic N-method compound operator expansion."""

    def _expand(self, dag, kb_patches):
        """Helper: apply COMPOUND_OPERATOR_EXPANSION_RULE with mocked KB."""
        with patch.multiple("dorian.knowledge.queries", **{
            k.split(".")[-1]: v for k, v in kb_patches.items()
        }):
            return sync_apply(COMPOUND_OPERATOR_EXPANSION_RULE, dag, {})

    def test_3method_sklearn_estimator(self):
        """Standard sklearn: __init__ → fit → predict.

        With the fixed KB, fit consumes interface X (pos 0) / y (pos 1), and
        predict consumes interface X_test (pos 2).  The three data edges
        (X_train at pos 0, y_train at pos 1, X_test at pos 2) are routed to
        the correct method based on name — no fan-out, no extra terminals.
        """
        dag = DAG(
            nodes={
                "data_x": Operator(name="pandas.read_csv", language="python"),
                "data_y": Operator(name="pandas.read_csv", language="python"),
                "data_x_test": Operator(name="pandas.read_csv", language="python"),
                "clf": Operator(name="sklearn.svm.SVC", language="python"),
                "out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("data_x", "clf", position=0, output=0),       # X_train
                Edge("data_y", "clf", position=1, output=0),       # y_train
                Edge("data_x_test", "clf", position=2, output=0),  # X_test
                Edge("clf", "out", position=0, output=0),
            ],
        )
        patches = _mock_kb(
            "Sklearn Estimator", _SKLEARN_ESTIMATOR_METHODS,
            method_io=_METHOD_IO["Sklearn Estimator"],
            interface_io=_INTERFACE_IO["Sklearn Estimator"],
            operator_name="sklearn.svm.SVC",
        )
        result = self._expand(dag, patches)

        # clf should be replaced with init, fit, predict nodes
        assert "clf" not in result.nodes
        init_ids = [k for k in result.nodes if "_cx_init" in k]
        fit_ids = [k for k in result.nodes if "_cx_fit_" in k]
        predict_ids = [k for k in result.nodes if "_cx_predict_" in k]
        assert len(init_ids) == 1
        assert len(fit_ids) == 1
        assert len(predict_ids) == 1, (
            "Exactly one predict node — no 'extra terminal' copies."
        )

        # Check chain: init → fit → predict
        edges_by_src = {}
        for e in result.edges:
            edges_by_src.setdefault(e.source, []).append(e)

        init_out = edges_by_src[init_ids[0]]
        assert any(e.destination == fit_ids[0] and e.position == 0 for e in init_out)

        fit_out = edges_by_src[fit_ids[0]]
        assert any(e.destination == predict_ids[0] and e.position == 0 for e in fit_out)

        # X_train → fit@1 only (NOT predict — that's what X_test is for)
        data_x_edges = [e for e in result.edges if e.source == "data_x"]
        assert any(e.destination == fit_ids[0] and e.position == 1 for e in data_x_edges)
        assert not any(e.destination == predict_ids[0] for e in data_x_edges), (
            "X_train must not be routed to predict — that's the bug this test guards against."
        )

        # y_train → fit@2 only
        data_y_edges = [e for e in result.edges if e.source == "data_y"]
        assert any(e.destination == fit_ids[0] and e.position == 2 for e in data_y_edges)

        # X_test → predict@1 only
        data_x_test_edges = [e for e in result.edges if e.source == "data_x_test"]
        assert any(e.destination == predict_ids[0] and e.position == 1 for e in data_x_test_edges)
        assert not any(e.destination == fit_ids[0] for e in data_x_test_edges), (
            "X_test must not be routed to fit — fit operates on X_train."
        )

    def test_4method_model_tracer(self):
        """Model Tracer: __init__ → fit → predict → trace."""
        dag = DAG(
            nodes={
                "x": Operator(name="pandas.read_csv", language="python"),
                "y": Operator(name="pandas.read_csv", language="python"),
                "tracer": Operator(name="model_tracing.DecisionTreeTracer", language="python"),
                "out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("x", "tracer", position=0, output=0),
                Edge("y", "tracer", position=1, output=0),
                Edge("tracer", "out", position=0, output=0),  # predictions
            ],
        )
        patches = _mock_kb(
            "Model Tracer", _MODEL_TRACER_METHODS,
            method_io=_METHOD_IO["Model Tracer"],
            interface_io=_INTERFACE_IO["Model Tracer"],
            operator_name="model_tracing.DecisionTreeTracer",
        )
        result = self._expand(dag, patches)

        # 4 sub-nodes: init, fit, predict, trace
        assert "tracer" not in result.nodes
        assert sum(1 for k in result.nodes if "_cx_init" in k) == 1
        assert sum(1 for k in result.nodes if "_cx_fit_" in k) == 1
        assert sum(1 for k in result.nodes if "_cx_predict_" in k) == 1
        assert sum(1 for k in result.nodes if "_cx_trace_" in k) == 1

        # Chain: init → fit → predict → trace
        init_id = next(k for k in result.nodes if "_cx_init" in k)
        fit_id = next(k for k in result.nodes if "_cx_fit_" in k)
        predict_id = next(k for k in result.nodes if "_cx_predict_" in k)
        trace_id = next(k for k in result.nodes if "_cx_trace_" in k)

        chain_edges = [e for e in result.edges if e.position == 0 and e.output == 0]
        assert Edge(init_id, fit_id, position=0, output=0) in chain_edges
        assert Edge(fit_id, predict_id, position=0, output=0) in chain_edges
        assert Edge(predict_id, trace_id, position=0, output=0) in chain_edges

        # Data routing: X → fit@1, y → fit@2, X → predict@1
        x_edges = [e for e in result.edges if e.source == "x"]
        assert any(e.destination == fit_id and e.position == 1 for e in x_edges)
        assert any(e.destination == predict_id and e.position == 1 for e in x_edges)

        y_edges = [e for e in result.edges if e.source == "y"]
        assert any(e.destination == fit_id and e.position == 2 for e in y_edges)

    def test_multi_output_routing(self):
        """Model Tracer: output 0 (predictions) from predict, output 1 (trace_urls) from trace."""
        dag = DAG(
            nodes={
                "x": Operator(name="pandas.read_csv", language="python"),
                "y": Operator(name="pandas.read_csv", language="python"),
                "tracer": Operator(name="model_tracing.DecisionTreeTracer", language="python"),
                "pred_out": Operator(name="dorian.io.printout", language="python"),
                "trace_out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("x", "tracer", position=0, output=0),
                Edge("y", "tracer", position=1, output=0),
                Edge("tracer", "pred_out", position=0, output=0),   # predictions
                Edge("tracer", "trace_out", position=0, output=1),  # trace_urls
            ],
        )
        patches = _mock_kb(
            "Model Tracer", _MODEL_TRACER_METHODS,
            method_io=_METHOD_IO["Model Tracer"],
            interface_io=_INTERFACE_IO["Model Tracer"],
            operator_name="model_tracing.DecisionTreeTracer",
        )
        result = self._expand(dag, patches)

        predict_id = next(k for k in result.nodes if "_cx_predict_" in k)
        trace_id = next(k for k in result.nodes if "_cx_trace_" in k)

        # Output 0 → predict node → pred_out
        pred_out_edges = [e for e in result.edges if e.destination == "pred_out"]
        assert any(e.source == predict_id for e in pred_out_edges)

        # Output 1 → trace node → trace_out
        trace_out_edges = [e for e in result.edges if e.destination == "trace_out"]
        assert any(e.source == trace_id for e in trace_out_edges)

    def test_2method_llm(self):
        """LLM: __init__ → chat.send with kwarg position."""
        dag = DAG(
            nodes={
                "msgs": Operator(name="pandas.read_csv", language="python"),
                "llm": Operator(name="openrouter.chat.completion", language="python"),
                "out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("msgs", "llm", position="messages", output=0),
                Edge("llm", "out", position=0, output=0),
            ],
        )
        patches = _mock_kb(
            "LLM Chat Completion", _LLM_METHODS,
            method_io=_METHOD_IO["LLM Chat Completion"],
            interface_io=_INTERFACE_IO["LLM Chat Completion"],
            operator_name="openrouter.chat.completion",
        )
        result = self._expand(dag, patches)

        assert "llm" not in result.nodes
        init_id = next(k for k in result.nodes if "_cx_init" in k)
        send_ids = [k for k in result.nodes if "chat_send" in k]
        assert len(send_ids) == 1

        # msgs → chat.send with kwarg position "messages"
        msg_edges = [e for e in result.edges if e.source == "msgs"]
        assert any(e.destination == send_ids[0] and e.position == "messages" for e in msg_edges)

    def test_parameter_routing(self):
        """Parameters route to the method specified by KB method field."""
        dag = DAG(
            nodes={
                "x": Operator(name="pandas.read_csv", language="python"),
                "y": Operator(name="pandas.read_csv", language="python"),
                "c_param": Parameter(name="C", dtype="float", value="1.0"),
                "clf": Operator(name="sklearn.svm.SVC", language="python"),
                "out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("x", "clf", position=0, output=0),
                Edge("y", "clf", position=1, output=0),
                Edge("c_param", "clf", position="C", output=0),
                Edge("clf", "out", position=0, output=0),
            ],
        )
        patches = _mock_kb(
            "Sklearn Estimator", _SKLEARN_ESTIMATOR_METHODS,
            method_io=_METHOD_IO["Sklearn Estimator"],
            interface_io=_INTERFACE_IO["Sklearn Estimator"],
            params=[{"name": "C", "method": None}],  # None = __init__
            operator_name="sklearn.svm.SVC",
        )
        result = self._expand(dag, patches)

        init_id = next(k for k in result.nodes if "_cx_init" in k)
        # C param → __init__
        c_edges = [e for e in result.edges if e.source == "c_param"]
        assert any(e.destination == init_id for e in c_edges)

    def test_function_interface_noop(self):
        """Function interface returns DAG unchanged."""
        dag = DAG(
            nodes={"op": Operator(name="sklearn.metrics.accuracy_score", language="python")},
            edges=[],
        )
        patches = _mock_kb("Function", [], operator_name="sklearn.metrics.accuracy_score")
        result = self._expand(dag, patches)
        assert result.nodes == dag.nodes

    def test_3method_agnostic_tracer(self):
        """Model Agnostic Tracer: __init__ → predict → trace with 3 data inputs."""
        dag = DAG(
            nodes={
                "model": Operator(name="some.model", language="python"),
                "x_train": Operator(name="pandas.read_csv", language="python"),
                "x_test": Operator(name="pandas.read_csv", language="python"),
                "tracer": Operator(name="model_tracing.LIMETracer", language="python"),
                "out": Operator(name="dorian.io.printout", language="python"),
            },
            edges=[
                Edge("model", "tracer", position=0, output=0),
                Edge("x_train", "tracer", position=1, output=0),
                Edge("x_test", "tracer", position=2, output=0),
                Edge("tracer", "out", position=0, output=0),
            ],
        )
        patches = _mock_kb(
            "Model Agnostic Tracer", _MODEL_AGNOSTIC_TRACER_METHODS,
            method_io=_METHOD_IO["Model Agnostic Tracer"],
            interface_io=_INTERFACE_IO["Model Agnostic Tracer"],
            operator_name="model_tracing.LIMETracer",
        )
        result = self._expand(dag, patches)

        predict_id = next(k for k in result.nodes if "_cx_predict_" in k)
        trace_id = next(k for k in result.nodes if "_cx_trace_" in k)

        # All 3 inputs → predict
        model_edges = [e for e in result.edges if e.source == "model"]
        assert any(e.destination == predict_id and e.position == 1 for e in model_edges)

        x_train_edges = [e for e in result.edges if e.source == "x_train"]
        assert any(e.destination == predict_id and e.position == 2 for e in x_train_edges)

        x_test_edges = [e for e in result.edges if e.source == "x_test"]
        assert any(e.destination == predict_id and e.position == 3 for e in x_test_edges)


# ---------------------------------------------------------------------------
# Tests: group_builder.build_group
# ---------------------------------------------------------------------------

class TestGroupBuilder:
    """Generic Group construction for compound operators."""

    def test_4method_model_tracer_group(self):
        """build_group produces correct Group for 4-method Model Tracer."""
        with patch.multiple("dorian.knowledge.queries",
            get_operator_interface=MagicMock(return_value="Model Tracer"),
            get_method_sequence=MagicMock(return_value=_MODEL_TRACER_METHODS),
            get_method_io=MagicMock(return_value=_METHOD_IO["Model Tracer"]),
            get_interface_io=MagicMock(return_value=_INTERFACE_IO["Model Tracer"]),
            get_operator_parameters=MagicMock(return_value=[
                {"name": "output_dir", "method": None},
            ]),
            get_interface_attributes=MagicMock(return_value=[]),
        ):
            group = build_group("model_tracing.DecisionTreeTracer", "node123")

        assert group is not None
        assert group.name == "model_tracing.DecisionTreeTracer"

        # 4 children: init, fit, predict, trace
        assert len(group.children) == 4

        # Chain edges: 3 (init→fit, fit→predict, predict→trace)
        chain_edges = [e for e in group.internal_edges if e.position == 0]
        assert len(chain_edges) == 3

        # Input handles: X → fit, y → fit (first consuming method)
        assert "X" in group.io_map
        assert "y" in group.io_map
        assert group.io_map["X"].direction == "input"
        assert group.io_map["y"].direction == "input"

        # Output handles: predictions from predict, trace_urls from trace
        assert "predictions" in group.io_map
        assert "trace_urls" in group.io_map
        assert group.io_map["predictions"].direction == "output"
        assert group.io_map["trace_urls"].direction == "output"
        # They should come from different internal nodes
        assert group.io_map["predictions"].internal_node_id != group.io_map["trace_urls"].internal_node_id

    def test_2method_llm_group(self):
        """build_group produces correct Group for LLM Chat Completion."""
        with patch.multiple("dorian.knowledge.queries",
            get_operator_interface=MagicMock(return_value="LLM Chat Completion"),
            get_method_sequence=MagicMock(return_value=_LLM_METHODS),
            get_method_io=MagicMock(return_value=_METHOD_IO["LLM Chat Completion"]),
            get_interface_io=MagicMock(return_value=_INTERFACE_IO["LLM Chat Completion"]),
            get_operator_parameters=MagicMock(return_value=[]),
            get_interface_attributes=MagicMock(return_value=[]),
        ):
            group = build_group("openrouter.chat.completion", "node456")

        assert group is not None
        assert len(group.children) == 2  # init + chat.send
        assert "messages" in group.io_map
        assert group.io_map["messages"].internal_handle == "messages"
