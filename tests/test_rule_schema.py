"""
tests/test_rule_schema.py
--------------------------
Tests for Pydantic rule spec validation (shape + safety gate).
"""
from __future__ import annotations

import unittest

from dorian.mcp.rule_schema import validate_rule_spec, RuleSpec


# ── Reusable valid specs ──────────────────────────────────────────────────

_VALID_SPEC = {
    "description": "Remove standalone operator",
    "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
    "transformations": [{"type": "delete", "nodes": ["0"]}],
}

_APPLY_SPEC = {
    "description": "Rename operator",
    "pattern": {"nodes": {"0": {"type": "Operator", "text": "X"}}, "edges": []},
    "transformations": [{"type": "replace_operator", "target": "0", "new_name": "Y"}],
}

_UPDATE_ATTR_SPEC = {
    "description": "Update text",
    "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
    "transformations": [{
        "type": "update_attribute",
        "target": "0",
        "attribute": "text",
        "value": "new_text",
    }],
}

_CONCAT_SPEC = {
    "description": "Concat values",
    "pattern": {"nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}}, "edges": []},
    "transformations": [{
        "type": "update_attribute",
        "target": "0",
        "attribute": "text",
        "value": {"concat": ["prefix_", {"ref": "1", "attr": "text"}]},
    }],
}


class TestValidSpecs(unittest.TestCase):
    """Valid specs should pass validation and return a normalised dict."""

    def test_delete_spec(self):
        result, errors = validate_rule_spec(_VALID_SPEC)
        assert errors == [], errors
        assert result is not None
        assert result["description"] == "Remove standalone operator"

    def test_replace_operator_spec(self):
        result, errors = validate_rule_spec(_APPLY_SPEC)
        assert errors == []
        assert result is not None

    def test_update_attribute_literal(self):
        result, errors = validate_rule_spec(_UPDATE_ATTR_SPEC)
        assert errors == []

    def test_update_attribute_concat(self):
        result, errors = validate_rule_spec(_CONCAT_SPEC)
        assert errors == []

    def test_update_attribute_ref(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "target": "0",
                "attribute": "text",
                "value": {"ref": "0", "attr": "text"},
            }],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []

    def test_add_parameter(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "add_parameter",
                "target": "0",
                "param_name": "n_estimators",
                "param_value": "100",
            }],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []

    def test_insert_before_and_after(self):
        for ttype in ("insert_before", "insert_after"):
            spec = {
                "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
                "transformations": [{"type": ttype, "target": "0", "new_operator": "Scaler"}],
            }
            result, errors = validate_rule_spec(spec)
            assert errors == [], f"{ttype}: {errors}"

    def test_pattern_with_edges(self):
        spec = {
            "pattern": {
                "nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}},
                "edges": [{"source": "0", "destination": "1"}],
            },
            "transformations": [],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []

    def test_default_description(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []
        assert result["description"] == "LLM-generated rule"

    def test_model_json_schema_is_valid(self):
        schema = RuleSpec.model_json_schema()
        assert "properties" in schema
        assert "pattern" in schema["properties"]


class TestMissingFields(unittest.TestCase):
    """Missing required fields should produce clear errors."""

    def test_missing_pattern(self):
        _, errors = validate_rule_spec({"transformations": []})
        assert len(errors) > 0
        assert any("pattern" in e for e in errors)

    def test_empty_nodes(self):
        spec = {"pattern": {"nodes": {}, "edges": []}, "transformations": []}
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_update_attribute_missing_target(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "attribute": "text",
                "value": "x",
            }],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_replace_operator_missing_new_name(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "replace_operator", "target": "0"}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_unknown_transformation_type(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "explode", "target": "0"}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0


class TestRegexSafety(unittest.TestCase):
    """Regex patterns must be valid and free of ReDoS signatures."""

    def test_invalid_regex_syntax(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "[invalid"}}, "edges": []},
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0
        assert any("regex" in e.lower() for e in errors)

    def test_redos_nested_quantifier(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "(a+)+$"}}, "edges": []},
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0
        assert any("redos" in e.lower() or "backtrack" in e.lower() for e in errors)

    def test_redos_in_text_field(self):
        spec = {
            "pattern": {"nodes": {"0": {"text": "(x*)*y"}}, "edges": []},
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_long_regex_rejected(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "a" * 201}}, "edges": []},
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0
        assert any("max length" in e.lower() for e in errors)

    def test_valid_regex_accepted(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": r"sklearn\.svm\.SVC"}}, "edges": []},
            "transformations": [],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []


class TestSizeLimits(unittest.TestCase):
    """Enforce bounds on spec size to prevent memory abuse."""

    def test_too_many_nodes(self):
        nodes = {str(i): {"type": "Operator"} for i in range(51)}
        spec = {"pattern": {"nodes": nodes, "edges": []}, "transformations": []}
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_too_many_transformations(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [
                {"type": "delete", "nodes": ["0"]} for _ in range(21)
            ],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_description_too_long(self):
        spec = {
            "description": "x" * 501,
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0


class TestConcatDepth(unittest.TestCase):
    """Nested concat expressions must be depth-bounded."""

    def test_depth_exceeded(self):
        # Build a concat nested 7 levels deep
        value: dict = {"concat": ["leaf"]}
        for _ in range(6):
            value = {"concat": [value]}
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "target": "0",
                "attribute": "text",
                "value": value,
            }],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0
        assert any("depth" in e.lower() for e in errors)

    def test_valid_depth_accepted(self):
        # Depth 2 — well within limit
        value = {"concat": ["a", {"concat": ["b", "c"]}]}
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "target": "0",
                "attribute": "text",
                "value": value,
            }],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []


class TestExtraKeys(unittest.TestCase):
    """Extra/unknown keys should be rejected."""

    def test_extra_top_level_key(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [],
            "sneaky_field": True,
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_extra_node_key(self):
        spec = {
            "pattern": {
                "nodes": {"0": {"type": "Operator", "colour": "red"}},
                "edges": [],
            },
            "transformations": [],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_extra_transformation_key(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "delete", "nodes": ["0"], "force": True}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0


class TestAddEdges(unittest.TestCase):
    """add_edges transformation validation."""

    def test_valid_add_edges(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "add_edges", "edges": [["0", "1"]]}],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []
        assert result["transformations"][0]["type"] == "add_edges"

    def test_add_edges_bad_tuple(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "add_edges", "edges": [["0"]]}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0
        assert any("2-element" in e for e in errors)

    def test_add_edges_three_elements(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "add_edges", "edges": [["0", "1", "2"]]}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0


class TestDeleteEdges(unittest.TestCase):
    """delete transformation with edges."""

    def test_delete_edges_valid(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "delete", "nodes": [], "edges": [["0", "1"]]}],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []

    def test_delete_edges_bad_tuple(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{"type": "delete", "nodes": [], "edges": [["only_one"]]}],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_redirect_pattern(self):
        """A redirect = delete old edge + add new edge — should validate."""
        spec = {
            "pattern": {
                "nodes": {"0": {"type": "Operator"}, "1": {"type": "Operator"}, "2": {"type": "Operator"}},
                "edges": [{"source": "0", "destination": "1"}],
            },
            "transformations": [
                {"type": "delete", "nodes": [], "edges": [["0", "1"]]},
                {"type": "add_edges", "edges": [["0", "2"]]},
            ],
        }
        result, errors = validate_rule_spec(spec)
        assert errors == []
        assert len(result["transformations"]) == 2


class TestUpdateAttributeValueTypes(unittest.TestCase):
    """update_attribute value field must be str, ref, or concat."""

    def test_invalid_value_type_number(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "target": "0",
                "attribute": "text",
                "value": 42,
            }],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0

    def test_invalid_value_dict_no_keys(self):
        spec = {
            "pattern": {"nodes": {"0": {"type": "Operator"}}, "edges": []},
            "transformations": [{
                "type": "update_attribute",
                "target": "0",
                "attribute": "text",
                "value": {"unknown": "key"},
            }],
        }
        _, errors = validate_rule_spec(spec)
        assert len(errors) > 0


if __name__ == "__main__":
    unittest.main()
