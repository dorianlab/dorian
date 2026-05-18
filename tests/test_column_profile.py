"""Tests for column-level profiling used to prefill DQ feedback forms."""
import numpy as np
import pandas as pd

from dorian.tabular.data.profiling.column_profile import (
    compute_column_profiles,
    _infer_type,
    _detect_mixed_types,
    _infer_scale,
)


# ---------------------------------------------------------------------------
# _infer_type
# ---------------------------------------------------------------------------

class TestInferType:

    def test_int_column(self):
        s = pd.Series([1, 2, 3])
        assert _infer_type(s) == "int"

    def test_float_column(self):
        s = pd.Series([1.1, 2.2, 3.3])
        assert _infer_type(s) == "float"

    def test_bool_column(self):
        s = pd.Series([True, False, True], dtype="bool")
        assert _infer_type(s) == "bool"

    def test_string_column(self):
        s = pd.Series(["a", "b", "c"])
        assert _infer_type(s) == "str"

    def test_object_column_numeric_strings(self):
        s = pd.Series(["1", "2", "3"])
        # 90%+ parse as numeric → int
        assert _infer_type(s) in ("int", "float")

    def test_object_column_mixed(self):
        s = pd.Series(["hello", "world", None])
        assert _infer_type(s) == "str"

    def test_all_null(self):
        s = pd.Series([None, None, None])
        assert _infer_type(s) == "str"


# ---------------------------------------------------------------------------
# _detect_mixed_types
# ---------------------------------------------------------------------------

class TestDetectMixedTypes:

    def test_uniform_types(self):
        assert not _detect_mixed_types(pd.Series([1, 2, 3]))
        assert not _detect_mixed_types(pd.Series(["a", "b"]))

    def test_mixed_types(self):
        assert _detect_mixed_types(pd.Series([1, "two", 3.0]))

    def test_empty_series(self):
        assert not _detect_mixed_types(pd.Series([], dtype=object))

    def test_all_null(self):
        assert not _detect_mixed_types(pd.Series([None, None]))


# ---------------------------------------------------------------------------
# _infer_scale
# ---------------------------------------------------------------------------

class TestInferScale:

    def test_binary(self):
        s = pd.Series([0, 1, 0, 1])
        assert _infer_scale(s, "int") == "binary"

    def test_categorical_string(self):
        s = pd.Series(["a", "b", "c", "a"])
        assert _infer_scale(s, "str") == "categorical"

    def test_ordinal_int_small(self):
        s = pd.Series(list(range(10)) * 5)
        assert _infer_scale(s, "int") == "ordinal"

    def test_continuous_float(self):
        s = pd.Series(np.random.randn(100))
        assert _infer_scale(s, "float") == "continuous"


# ---------------------------------------------------------------------------
# compute_column_profiles — integration
# ---------------------------------------------------------------------------

class TestComputeColumnProfiles:

    def test_basic_dataframe(self):
        df = pd.DataFrame({
            "age": [25, 30, 35, 40, None],
            "name": ["Alice", "Bob", "Charlie", "Dave", "Eve"],
            "active": [True, False, True, False, True],
            "score": [1.5, 2.7, 3.1, 4.9, 5.0],
        })
        profiles = compute_column_profiles(df)
        assert set(profiles.keys()) == {"age", "name", "active", "score"}

        # age — numeric
        age = profiles["age"]
        assert age["is_numeric"] is True
        assert age["null_count"] == 1
        assert age["null_pct"] == 0.2
        assert age["min"] == 25.0
        assert age["max"] == 40.0
        assert age["unique_count"] == 4
        assert age["inferred_type"] in ("int", "float")

        # name — string
        name = profiles["name"]
        assert name["is_numeric"] is False
        assert name["inferred_type"] == "str"
        assert name["scale"] == "categorical"
        assert len(name["sample_values"]) == 5

        # active — bool
        active = profiles["active"]
        assert active["inferred_type"] == "bool"
        assert active["scale"] == "binary"

        # score — float
        score = profiles["score"]
        assert score["is_numeric"] is True
        assert score["inferred_type"] == "float"

    def test_empty_dataframe(self):
        df = pd.DataFrame({"a": pd.Series([], dtype=float)})
        profiles = compute_column_profiles(df)
        assert "a" in profiles
        assert profiles["a"]["null_count"] == 0
        assert profiles["a"]["unique_count"] == 0

    def test_all_null_column(self):
        df = pd.DataFrame({"x": [None, None, None]})
        profiles = compute_column_profiles(df)
        assert profiles["x"]["null_count"] == 3
        assert profiles["x"]["null_pct"] == 1.0
        assert profiles["x"]["min"] is None

    def test_json_serializable(self):
        """Profiles must be JSON-serializable (no numpy scalars)."""
        import json
        df = pd.DataFrame({
            "ints": [1, 2, 3],
            "floats": [1.1, 2.2, 3.3],
            "strs": ["a", "b", "c"],
        })
        profiles = compute_column_profiles(df)
        # This will raise if any numpy scalar slips through
        json.dumps(profiles)

    def test_sample_values_capped(self):
        df = pd.DataFrame({"x": list(range(100))})
        profiles = compute_column_profiles(df)
        assert len(profiles["x"]["sample_values"]) <= 12
