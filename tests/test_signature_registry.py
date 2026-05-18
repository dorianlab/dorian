"""Signature registry loader — mocks the KB bulk queries so tests
don't need a live Neo4j."""
from __future__ import annotations

import json

from dorian.pipeline.signature_registry import build_signatures_json


def _empty_bulk() -> dict:
    return {}


def test_empty_kb_returns_empty_registry():
    out = build_signatures_json(
        get_operator_ios_bulk=_empty_bulk,
        get_interface_ios_bulk=_empty_bulk,
        get_operator_interfaces_bulk=_empty_bulk,
    )
    assert out == "{}"


def test_operator_level_ports_build_expected_portsig_shape():
    """Operator-level IO → PortSig JSON. Positional ports get
    required=True, named ports don't."""
    op_ios = {
        "sklearn.metrics.accuracy_score": (
            [  # inputs
                {"name": "0", "type": "Array", "role": "target", "split": "test"},
                {"name": "1", "type": "Array", "role": "prediction"},
            ],
            [  # outputs
                {"name": "score", "type": "Metric", "role": "metric"},
            ],
        ),
    }
    out = build_signatures_json(
        get_operator_ios_bulk=lambda: op_ios,
        get_interface_ios_bulk=_empty_bulk,
        get_operator_interfaces_bulk=_empty_bulk,
    )
    reg = json.loads(out)
    sig = reg["sklearn.metrics.accuracy_score"]
    # Inputs: positional ports required; role/split carried through.
    assert sig["inputs"][0] == {
        "name": "0", "type": "Array",
        "required": True,
        "role": "target", "split": "test",
    }
    assert sig["inputs"][1] == {
        "name": "1", "type": "Array",
        "required": True,
        "role": "prediction",
    }
    # Output port: required flag is NOT set (it only applies to inputs).
    assert sig["outputs"][0] == {
        "name": "score", "type": "Metric", "role": "metric",
    }


def test_interface_annotations_inherit_to_concrete_operator():
    """An operator's ``is a`` interface contributes its port
    annotations; operator-level entries OVERRIDE."""
    iface_ios = {
        "Sklearn Estimator": (
            [
                {"name": "0", "type": "any", "role": "model"},
                {"name": "1", "type": "any", "role": "feature", "split": "train"},
                {"name": "2", "type": "any", "role": "target",  "split": "train"},
            ],
            [],
        ),
    }
    op_interfaces = {
        "sklearn.ensemble.RandomForestClassifier": "Sklearn Estimator",
    }
    # The operator itself has no extra IO in the KB (crawler default).
    op_ios = {"sklearn.ensemble.RandomForestClassifier": ([], [])}

    out = build_signatures_json(
        get_operator_ios_bulk=lambda: op_ios,
        get_interface_ios_bulk=lambda: iface_ios,
        get_operator_interfaces_bulk=lambda: op_interfaces,
    )
    reg = json.loads(out)
    sig = reg["sklearn.ensemble.RandomForestClassifier"]
    # Three inherited inputs.
    names = [p["name"] for p in sig["inputs"]]
    assert names == ["0", "1", "2"]
    # Role/split tags survived the interface → operator merge.
    by_name = {p["name"]: p for p in sig["inputs"]}
    assert by_name["1"]["role"] == "feature"
    assert by_name["1"]["split"] == "train"
    assert by_name["2"]["role"] == "target"


def test_operator_level_overrides_interface_level():
    """train_test_split is `Function` (no IO at interface level)
    but has its own rich IO declared at the operator level,
    including role/split on every output."""
    iface_ios = {"Function": ([], [])}
    op_interfaces = {"sklearn.model_selection.train_test_split": "Function"}
    op_ios = {
        "sklearn.model_selection.train_test_split": (
            [  # inputs — plain
                {"name": "0", "type": "any"},
                {"name": "1", "type": "any"},
            ],
            [  # outputs with split annotations
                {"name": "X_train", "role": "feature", "split": "train"},
                {"name": "X_test",  "role": "feature", "split": "test"},
                {"name": "y_train", "role": "target",  "split": "train"},
                {"name": "y_test",  "role": "target",  "split": "test"},
            ],
        ),
    }
    out = build_signatures_json(
        get_operator_ios_bulk=lambda: op_ios,
        get_interface_ios_bulk=lambda: iface_ios,
        get_operator_interfaces_bulk=lambda: op_interfaces,
    )
    reg = json.loads(out)
    sig = reg["sklearn.model_selection.train_test_split"]
    by_name = {p["name"]: p for p in sig["outputs"]}
    assert by_name["X_train"]["split"] == "train"
    assert by_name["X_test"]["split"] == "test"
    assert by_name["y_train"]["role"] == "target"


def test_operators_with_zero_io_are_skipped():
    """An operator with no declared IO is indistinguishable from
    unknown at validator level; skipping avoids silently accepting
    any wiring."""
    out = build_signatures_json(
        get_operator_ios_bulk=lambda: {"empty.op": ([], [])},
        get_interface_ios_bulk=_empty_bulk,
        get_operator_interfaces_bulk=lambda: {"empty.op": "Function"},
    )
    assert json.loads(out) == {}


def test_kb_failure_degrades_gracefully():
    """Any loader raising → treat as unavailable; return empty
    registry string so validate_pipeline still runs structural-only."""
    def boom():
        raise RuntimeError("neo4j unreachable")
    out = build_signatures_json(
        get_operator_ios_bulk=boom,
        get_interface_ios_bulk=boom,
        get_operator_interfaces_bulk=boom,
    )
    assert out == "{}"
