"""Tests for the RL pipeline generation engine.

Covers:
- Eval template construction (frozen nodes, RL zone ports)
- Operator catalog RL-only filtering
- PipelineGenEnv episode lifecycle with frozen template
- Error capture during generation
- OperatorSpec visibility property
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from dorian.dag import DAG, Operator, Parameter
from dorian.pipeline.generation.types import OperatorSpec, PortSpec, ParameterSpec
from dorian.pipeline.generation.eval_template import (
    EvalTemplate,
    build_eval_template,
    _resolve_metric,
)


# ---------------------------------------------------------------------------
# Eval template tests
# ---------------------------------------------------------------------------

class TestEvalTemplate:
    def test_build_classification_template(self):
        tpl = build_eval_template("Classification")
        assert isinstance(tpl, EvalTemplate)
        assert tpl.task == "Classification"
        assert tpl.metric_fqn == "sklearn.metrics.accuracy_score"

    def test_build_regression_template(self):
        tpl = build_eval_template("Regression")
        assert tpl.metric_fqn == "sklearn.metrics.mean_squared_error"

    def test_build_default_template(self):
        tpl = build_eval_template(None)
        assert tpl.metric_fqn == "sklearn.metrics.accuracy_score"

    def test_frozen_nodes_present(self):
        tpl = build_eval_template("Classification")
        # Frozen nodes: dataset (1) + 2 projection snippets + 2 state params
        # + split + 2 split params + metric = 9
        assert len(tpl.frozen_nodes) == 9

    def test_frozen_nodes_in_dag(self):
        tpl = build_eval_template("Classification")
        for nid in tpl.frozen_nodes:
            assert nid in tpl.dag.nodes

    def test_rl_entry_ports(self):
        tpl = build_eval_template("Classification")
        # 3 RL entry ports: X_train, X_test, y_train
        assert len(tpl.rl_entry_ports) == 3
        port_names = {p[1][0] for p in tpl.rl_entry_ports}
        assert port_names == {"X_train", "X_test", "y_train"}

    def test_rl_exit_target(self):
        tpl = build_eval_template("Classification")
        assert "node_id" in tpl.rl_exit_target
        assert "input_port" in tpl.rl_exit_target
        assert tpl.rl_exit_target["input_port"]["name"] == "y_pred"

    def test_frozen_edges_present(self):
        tpl = build_eval_template("Classification")
        # At least: dataset→split, param1→split, param2→split, split→metric = 4
        assert len(tpl.frozen_edges) >= 4

    def test_dag_has_edges(self):
        tpl = build_eval_template("Classification")
        # Should have edges wiring the template
        assert len(tpl.dag.edges) >= 4

    def test_dataset_operator_in_template(self):
        tpl = build_eval_template("Classification")
        ops = [n for n in tpl.dag.nodes.values() if isinstance(n, Operator)]
        op_names = {o.name for o in ops}
        assert "dorian.io.dataset" in op_names
        assert "sklearn.model_selection.train_test_split" in op_names
        assert "sklearn.metrics.accuracy_score" in op_names

    def test_state_params_in_template(self):
        tpl = build_eval_template("Classification")
        state_params = [
            n for n in tpl.dag.nodes.values()
            if isinstance(n, Parameter) and n.dtype == "state"
        ]
        state_keys = {p.value for p in state_params}
        assert state_keys == {"dataset.features", "dataset.target"}


class TestMetricResolution:
    def test_classification(self):
        assert _resolve_metric("Classification") == "sklearn.metrics.accuracy_score"

    def test_regression(self):
        assert _resolve_metric("Regression") == "sklearn.metrics.mean_squared_error"

    def test_unknown_defaults_to_accuracy(self):
        assert _resolve_metric("UnknownTask") == "sklearn.metrics.accuracy_score"

    def test_none_defaults_to_accuracy(self):
        assert _resolve_metric(None) == "sklearn.metrics.accuracy_score"


# ---------------------------------------------------------------------------
# OperatorSpec visibility tests
# ---------------------------------------------------------------------------

class TestOperatorSpecVisibility:
    def test_default_visibility(self):
        spec = OperatorSpec(name="sklearn.svm.SVC", interface="Sklearn Estimator")
        assert spec.visibility == "default"

    def test_secondary_visibility(self):
        spec = OperatorSpec(
            name="pandas.read_csv",
            interface="Function",
            visibility="secondary",
        )
        assert spec.visibility == "secondary"

    def test_hidden_visibility(self):
        spec = OperatorSpec(
            name="internal.op",
            interface="Function",
            visibility="hidden",
        )
        assert spec.visibility == "hidden"


# ---------------------------------------------------------------------------
# Catalog RL-only filtering tests
# ---------------------------------------------------------------------------

class TestCatalogRLFiltering:
    """Test the rl_only filtering logic in load_catalog."""

    @patch("dorian.knowledge.queries.get_all_operators")
    @patch("dorian.pipeline.generation.catalog._get_parameter_specs")
    def test_rl_only_excludes_metrics(self, mock_params, mock_ops):
        """sklearn.metrics.* should be excluded in rl_only mode."""
        from dorian.pipeline.generation.catalog import load_catalog
        load_catalog.cache_clear()

        mock_ops.return_value = [
            {"name": "sklearn.svm.SVC", "interface": "Sklearn Estimator", "tasks": [], "family": "SVM"},
            {"name": "sklearn.metrics.accuracy_score", "interface": "Function", "tasks": [], "family": None},
        ]
        mock_params.return_value = ()

        # Full catalog includes both
        full = load_catalog(task=None, rl_only=False)
        names = {s.name for s in full}
        assert "sklearn.svm.SVC" in names
        assert "sklearn.metrics.accuracy_score" in names

        load_catalog.cache_clear()

        # RL-only excludes metrics
        rl = load_catalog(task=None, rl_only=True)
        rl_names = {s.name for s in rl}
        assert "sklearn.svm.SVC" in rl_names
        assert "sklearn.metrics.accuracy_score" not in rl_names

        load_catalog.cache_clear()

    @patch("dorian.knowledge.queries.get_all_operators")
    @patch("dorian.pipeline.generation.catalog._get_parameter_specs")
    def test_rl_only_excludes_non_sklearn_interfaces(self, mock_params, mock_ops):
        """Only Sklearn Transformer/Estimator interfaces pass rl_only."""
        from dorian.pipeline.generation.catalog import load_catalog
        load_catalog.cache_clear()

        mock_ops.return_value = [
            {"name": "sklearn.preprocessing.StandardScaler", "interface": "Sklearn Transformer", "tasks": [], "family": "Scaler"},
            {"name": "openrouter.chat.completion", "interface": "LLM Chat Completion", "tasks": [], "family": None},
        ]
        mock_params.return_value = ()

        rl = load_catalog(task=None, rl_only=True)
        rl_names = {s.name for s in rl}
        assert "sklearn.preprocessing.StandardScaler" in rl_names
        assert "openrouter.chat.completion" not in rl_names

        load_catalog.cache_clear()

    @patch("dorian.knowledge.queries.get_all_operators")
    @patch("dorian.pipeline.generation.catalog._get_parameter_specs")
    def test_visibility_assigned_correctly(self, mock_params, mock_ops):
        """Operators get correct visibility based on prefix/interface rules."""
        from dorian.pipeline.generation.catalog import load_catalog
        load_catalog.cache_clear()

        mock_ops.return_value = [
            {"name": "sklearn.svm.SVC", "interface": "Sklearn Estimator", "tasks": [], "family": "SVM"},
            {"name": "pandas.read_csv", "interface": "Function", "tasks": [], "family": None},
            {"name": "sklearn.preprocessing.StandardScaler", "interface": "Sklearn Transformer", "tasks": [], "family": "Scaler"},
        ]
        mock_params.return_value = ()

        full = load_catalog(task=None, rl_only=False)
        by_name = {s.name: s for s in full}

        assert by_name["sklearn.svm.SVC"].visibility == "default"
        assert by_name["pandas.read_csv"].visibility == "secondary"
        assert by_name["sklearn.preprocessing.StandardScaler"].visibility == "default"

        load_catalog.cache_clear()


# ---------------------------------------------------------------------------
# PipelineGenEnv tests (with mocked catalog)
# ---------------------------------------------------------------------------

_MOCK_CATALOG = (
    OperatorSpec(
        name="sklearn.preprocessing.StandardScaler",
        interface="Sklearn Transformer",
        tasks=("Data Preprocessing",),
        family="Scaler",
        inputs=(PortSpec("X", 0, "features"),),
        outputs=(PortSpec("X_transformed", 0, "features"),),
        parameters=(),
    ),
    OperatorSpec(
        name="sklearn.svm.SVC",
        interface="Sklearn Estimator",
        tasks=("Classification",),
        family="SVM",
        inputs=(PortSpec("X", 0, "features"), PortSpec("y", 1, "labels")),
        outputs=(PortSpec("predictions", 0, "predictions"),),
        parameters=(
            ParameterSpec(name="C", dtype="float", default=1.0, low=0.01, high=100.0),
        ),
    ),
)


class TestPipelineGenEnv:
    @patch("dorian.pipeline.generation.environment.load_catalog", return_value=_MOCK_CATALOG)
    def test_reset_starts_from_template(self, mock_load):
        from dorian.pipeline.generation.environment import PipelineGenEnv

        env = PipelineGenEnv(task="Classification", seed=42)
        obs, info = env.reset()

        # DAG should have frozen template nodes
        assert len(env._dag.nodes) > 0
        assert "frozen_nodes" in info
        assert len(info["frozen_nodes"]) == 9  # dataset, 2 projections, 2 state params, split, 2 split params, metric

    @patch("dorian.pipeline.generation.environment.load_catalog", return_value=_MOCK_CATALOG)
    def test_free_ports_from_template(self, mock_load):
        from dorian.pipeline.generation.environment import PipelineGenEnv

        env = PipelineGenEnv(task="Classification", seed=42)
        obs, info = env.reset()

        # Should have 3 free ports from train_test_split
        assert len(env._free_ports) == 3
        port_names = {p.name for _, p in env._free_ports}
        assert "X_train" in port_names
        assert "X_test" in port_names
        assert "y_train" in port_names

    @patch("dorian.pipeline.generation.environment.load_catalog", return_value=_MOCK_CATALOG)
    def test_action_space_size(self, mock_load):
        from dorian.pipeline.generation.environment import PipelineGenEnv

        env = PipelineGenEnv(task="Classification", seed=42)
        # n_actions = len(catalog) + 1 (__END__)
        assert env.n_actions == len(_MOCK_CATALOG) + 1

    @patch("dorian.pipeline.generation.environment.load_catalog", return_value=_MOCK_CATALOG)
    def test_errors_captured(self, mock_load):
        from dorian.pipeline.generation.environment import PipelineGenEnv

        env = PipelineGenEnv(task="Classification", seed=42, max_steps=2)
        env.reset()

        # Place valid operators until truncation to capture errors
        for _ in range(10):
            if env.is_done:
                break
            masks = env.action_masks()
            valid_indices = [i for i in range(env.n_actions) if masks[i]]
            if not valid_indices:
                break
            env.step(valid_indices[0])

        # After episode, errors should be accessible
        assert isinstance(env.errors, list)

    @patch("dorian.pipeline.generation.environment.load_catalog", return_value=_MOCK_CATALOG)
    def test_observation_shape(self, mock_load):
        from dorian.pipeline.generation.environment import PipelineGenEnv

        env = PipelineGenEnv(task="Classification", seed=42)
        obs, info = env.reset()

        assert isinstance(obs, np.ndarray)
        assert obs.dtype == np.float32
        assert len(obs) == env.observation_dim
