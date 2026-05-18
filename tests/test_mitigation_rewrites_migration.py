"""Tests for ``dorian.pipeline.mitigation_rewrites_migration``.

Each of the five Apply functions in ``_APPLY_REGISTRY`` must
round-trip into a serialisable primitive-op list that the Rust
``graph::primitive`` evaluator can execute — validating the
claim that the Python registry is replaceable by pure KB data.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


_proj_root = str(Path(__file__).resolve().parents[1])
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

_mod = importlib.import_module(
    "dorian.pipeline.mitigation_rewrites_migration",
)
apply_to_primitives = _mod.apply_to_primitives
migrate_rewrite_doc = _mod.migrate_rewrite_doc


class TestApplyToPrimitives(unittest.TestCase):

    def test_reroute_incoming_default_maps_to_role_aware_reroute(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "reroute_incoming",
            "to": "n", "through": "guard",
        })
        self.assertEqual(len(prims), 1)
        op = prims[0]
        self.assertEqual(op["op"], "reroute_edges")
        self.assertEqual(op["selector"]["destination_role"], "feature_flow")
        self.assertEqual(op["selector"]["destination"]["key"], "n")
        self.assertEqual(op["through"]["key"], "guard")

    def test_reroute_incoming_anchor_maps_to_keyword_filter(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "reroute_incoming",
            "to": "n", "through": "guard", "anchor": "messages",
        })
        self.assertEqual(len(prims), 1)
        # Anchor takes precedence over role narrowing.
        self.assertNotIn("destination_role", prims[0]["selector"])
        pos = prims[0]["selector"]["position"]
        self.assertEqual(pos["pred"], "keyword_eq")
        self.assertEqual(pos["k"], "messages")

    def test_reroute_outgoing(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "reroute_outgoing",
            "from": "n", "through": "guard",
        })
        self.assertEqual(len(prims), 1)
        self.assertEqual(prims[0]["selector"]["source"]["key"], "n")
        self.assertEqual(prims[0]["through"]["key"], "guard")

    def test_replace_node_sets_payload(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "replace_node",
            "target": "n",
            "new_node_spec": {
                "node_type": "Operator",
                "name": "sklearn.linear_model.LogisticRegression",
                "language": "python",
            },
        })
        self.assertEqual(len(prims), 1)
        self.assertEqual(prims[0]["op"], "set_node_payload")
        self.assertEqual(prims[0]["payload"]["payload"], "operator")
        self.assertEqual(
            prims[0]["payload"]["name"],
            "sklearn.linear_model.LogisticRegression",
        )

    def test_insert_x_preprocessor(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "insert_x_preprocessor",
            "through": "encoder", "to": "n",
        })
        self.assertEqual(len(prims), 1)
        op = prims[0]
        self.assertEqual(op["selector"]["destination_role"], "feature_flow")
        self.assertEqual(op["through"]["key"], "encoder")

    def test_duplicate_data_kwarg_flags_gap(self):
        prims = apply_to_primitives({
            "type": "Apply", "function": "duplicate_data_kwarg",
            "target": "fit", "source_position": 1, "kwarg_name": "X_test",
        })
        self.assertEqual(len(prims), 1)
        self.assertIn("__needs_primitive_extension__", prims[0])

    def test_unknown_function_raises(self):
        with self.assertRaises(KeyError):
            apply_to_primitives({"type": "Apply", "function": "bogus"})


class TestMigrateRewriteDoc(unittest.TestCase):

    def test_mixed_transformations_pass_through_non_apply(self):
        doc = {
            "name": "Resampling",
            "pattern": {"nodes": {"n": {"text": ".*", "type": "Operator"}}},
            "transformations": [
                {"type": "Add", "nodes": {}, "edges": []},
                {"type": "Apply", "function": "reroute_incoming",
                 "to": "n", "through": "guard"},
            ],
        }
        out, warnings = migrate_rewrite_doc(doc)
        self.assertEqual(len(out["transformations"]), 2)
        # The Add is untouched.
        self.assertEqual(out["transformations"][0]["type"], "Add")
        # The Apply became a reroute_edges primitive.
        self.assertEqual(out["transformations"][1]["op"], "reroute_edges")
        self.assertEqual(warnings, [])

    def test_duplicate_kwarg_surfaces_warning(self):
        doc = {
            "name": "DupKwarg",
            "pattern": {"nodes": {"fit": {"text": ".*", "type": "Operator"}}},
            "transformations": [
                {"type": "Apply", "function": "duplicate_data_kwarg",
                 "target": "fit", "source_position": 1,
                 "kwarg_name": "X_test"},
            ],
        }
        _, warnings = migrate_rewrite_doc(doc)
        self.assertTrue(any("primitive gap" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
