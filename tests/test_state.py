"""
tests/test_state.py
-------------------
Tests for the ``dorian.io.state`` platform operator expansion.

Stubs Redis to avoid hitting real infrastructure.  Verifies allowlist
enforcement, resolution for each key, missing-data handling, and the
guard that catches unexpanded state nodes.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# Backend stubs are in conftest.py (loaded automatically by pytest).

import pytest
from dorian.dag import DAG, Edge, Operator, Parameter
from dorian.pipeline.state import (
    _expand_state,
    _ALLOWED_STATE_KEYS,
    STATE_EXPANSION_RULE,
    expand_state_refs,
)
from dorian.pipeline.transforms import sync_apply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state_dag(key: str, dataset: str | None = None) -> DAG:
    """Build a minimal DAG: dorian.io.state + key param + optional dataset param → sink."""
    nodes = {
        "state": Operator(name="dorian.io.state", language="python"),
        "key_param": Parameter(name="key", dtype="str", value=key),
        "sink": Operator(name="sink", language="python"),
    }
    edges = [
        Edge("key_param", "state", position="key", output=0),
        Edge("state", "sink", position=0, output=0),
    ]
    if dataset is not None:
        nodes["ds_param"] = Parameter(name="dataset", dtype="str", value=dataset)
        edges.append(Edge("ds_param", "state", position="dataset", output=0))
    return DAG(nodes=nodes, edges=edges)


_SESSION_META = {
    "uid": "u1",
    "session": "s1",
    "dataset": {
        "did": "d123",
        "fpath": "/data/housing.csv",
        "mime": "text/csv",
        "profile": {"n_features": 10, "n_samples": 500},
    },
    "selectedDataScienceTask": "Classification",
    "selectedEvaluationProcedureName": "Holdout",
}


def _mock_redis_get(key):
    """Simulate Redis GET for test keys."""
    store = {
        "session:s1:meta": json.dumps(_SESSION_META),
        "dataset:d123:feature_columns": json.dumps(["col1", "col2", "col3"]),
        "dataset:d123:target_columns": json.dumps(["target"]),
        "dataset:d123:protected_attributes": json.dumps(["gender"]),
    }
    return store.get(key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAllowlist:
    """Verify that only allowlisted keys are accepted."""

    def test_blocked_key_returns_dag_unchanged(self):
        dag = _state_dag("vault.secrets")
        with patch("dorian.pipeline.state.redis") as mock_redis:
            mock_redis.get.side_effect = _mock_redis_get
            result = _expand_state(dag, {"n": "state"}, {"session": "s1"})
        # DAG unchanged — state node still present
        assert "state" in result.nodes
        assert isinstance(result.nodes["state"], Operator)

    def test_system_keys_blocked(self):
        for bad_key in ["execution.run", "vault.env", "stream", "cancel", ""]:
            dag = _state_dag(bad_key)
            with patch("dorian.pipeline.state.redis") as mock_redis:
                mock_redis.get.side_effect = _mock_redis_get
                result = _expand_state(dag, {"n": "state"}, {"session": "s1"})
            assert "state" in result.nodes, f"key {bad_key!r} should be blocked"

    def test_all_allowed_keys_accepted(self):
        for key in _ALLOWED_STATE_KEYS:
            dag = _state_dag(key)
            with patch("dorian.pipeline.state.redis") as mock_redis:
                mock_redis.get.side_effect = _mock_redis_get
                result = _expand_state(dag, {"n": "state"}, {"session": "s1"})
            assert "state" not in result.nodes, f"key {key!r} should expand"


class TestResolution:
    """Verify that each allowed key resolves to the correct value."""

    @patch("dorian.pipeline.state.redis")
    def test_dataset_features(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.features")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert isinstance(value_node, Parameter)
        assert value_node.dtype == "eval"
        assert value_node.value == repr(["col1", "col2", "col3"])

    @patch("dorian.pipeline.state.redis")
    def test_dataset_target(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.target")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert isinstance(value_node, Parameter)
        assert value_node.value == repr(["target"])

    @patch("dorian.pipeline.state.redis")
    def test_dataset_protected_attributes(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.protected_attributes")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.value == repr(["gender"])

    @patch("dorian.pipeline.state.redis")
    def test_dataset_profile(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.profile")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.value == repr({"n_features": 10, "n_samples": 500})

    @patch("dorian.pipeline.state.redis")
    def test_session_task(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("session.task")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert isinstance(value_node, Parameter)
        assert value_node.dtype == "str"
        assert value_node.value == "Classification"

    @patch("dorian.pipeline.state.redis")
    def test_session_eval(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("session.eval")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.dtype == "str"
        assert value_node.value == "Holdout"


class TestEdgeRewiring:
    """Verify that outgoing edges are correctly rewired."""

    @patch("dorian.pipeline.state.redis")
    def test_outgoing_edges_rewired(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.features")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        # The value parameter should connect to sink
        edges_to_sink = [e for e in result.edges if e.destination == "sink"]
        assert len(edges_to_sink) == 1
        assert edges_to_sink[0].source == "state_state"
        assert edges_to_sink[0].position == 0

    @patch("dorian.pipeline.state.redis")
    def test_consumed_params_removed(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.features", dataset="housing")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        # key_param and ds_param should be removed
        assert "key_param" not in result.nodes
        assert "ds_param" not in result.nodes
        # No edges from consumed params
        consumed_edges = [e for e in result.edges if e.source in ("key_param", "ds_param")]
        assert len(consumed_edges) == 0


class TestMissingData:
    """Verify graceful handling of missing state."""

    @patch("dorian.pipeline.state.redis")
    def test_missing_redis_key(self, mock_redis):
        """Redis key exists but has no value → None."""
        mock_redis.get.return_value = json.dumps(_SESSION_META)
        # Override for the specific feature key
        def selective_get(key):
            if key == "session:s1:meta":
                return json.dumps(_SESSION_META)
            return None  # feature_columns not set

        mock_redis.get.side_effect = selective_get
        dag = _state_dag("dataset.features")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.dtype == "eval"
        assert value_node.value == "None"

    @patch("dorian.pipeline.state.redis")
    def test_no_dataset_in_meta(self, mock_redis):
        """Session meta has no dataset → None for dataset.* keys."""
        mock_redis.get.return_value = json.dumps({"uid": "u1", "session": "s1"})
        dag = _state_dag("dataset.features")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.value == "None"

    @patch("dorian.pipeline.state.redis")
    def test_missing_session_meta_key(self, mock_redis):
        """Session meta missing the requested field → None."""
        meta_no_task = {**_SESSION_META}
        del meta_no_task["selectedDataScienceTask"]
        mock_redis.get.return_value = json.dumps(meta_no_task)
        dag = _state_dag("session.task")
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})

        value_node = result.nodes["state_state"]
        assert value_node.value == "None"

    def test_no_key_parameter(self):
        """State node with no key Parameter → DAG unchanged."""
        dag = DAG(
            nodes={
                "state": Operator(name="dorian.io.state", language="python"),
                "sink": Operator(name="sink", language="python"),
            },
            edges=[Edge("state", "sink", position=0, output=0)],
        )
        result = _expand_state(dag, {"n": "state"}, {"session": "s1"})
        assert "state" in result.nodes  # unchanged


class TestSyncApply:
    """Verify the rule works via sync_apply (the production code path)."""

    @patch("dorian.pipeline.state.redis")
    def test_expand_state_refs(self, mock_redis):
        mock_redis.get.side_effect = _mock_redis_get
        dag = _state_dag("dataset.features")
        result = expand_state_refs(dag, "s1")

        assert "state" not in result.nodes
        assert "state_state" in result.nodes
        assert isinstance(result.nodes["state_state"], Parameter)

    @patch("dorian.pipeline.state.redis")
    def test_multiple_state_nodes(self, mock_redis):
        """Multiple dorian.io.state nodes expand independently."""
        mock_redis.get.side_effect = _mock_redis_get
        dag = DAG(
            nodes={
                "feat_state": Operator(name="dorian.io.state", language="python"),
                "feat_key": Parameter(name="key", dtype="str", value="dataset.features"),
                "task_state": Operator(name="dorian.io.state", language="python"),
                "task_key": Parameter(name="key", dtype="str", value="session.task"),
                "sink": Operator(name="sink", language="python"),
            },
            edges=[
                Edge("feat_key", "feat_state", position="key", output=0),
                Edge("task_key", "task_state", position="key", output=0),
                Edge("feat_state", "sink", position=0, output=0),
                Edge("task_state", "sink", position=1, output=0),
            ],
        )
        result = expand_state_refs(dag, "s1")

        # Both state nodes should be expanded
        assert "feat_state" not in result.nodes
        assert "task_state" not in result.nodes
        # Two resolved Parameters
        resolved = [n for n in result.nodes.values() if isinstance(n, Parameter) and n.name in ("dataset.features", "session.task")]
        assert len(resolved) == 2
