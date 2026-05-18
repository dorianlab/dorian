import pytest

from dorian.tabular.data.quality.decision import (
    DecisionFunctionSpec,
    build_decision_specs,
    evaluate_decision_function,
    extract_history_values,
)


def test_build_decision_specs_uses_threshold_and_decision_rows():
    specs = build_decision_specs(
        ["RecordCompleteness", "RiskOfDatasetInaccuracy"],
        threshold_rows=[
            {"metric": "RecordCompleteness", "threshold": "0.95"},
            {"metric": "RiskOfDatasetInaccuracy", "threshold": "0.05"},
        ],
        decision_rows=[
            {"metric": "RecordCompleteness", "decision_function": "constant_threshold"},
            {"metric": "RiskOfDatasetInaccuracy", "decision_function": "constant_threshold"},
        ],
    )

    assert specs["RecordCompleteness"].kind == "constant_threshold"
    assert specs["RecordCompleteness"].threshold == pytest.approx(0.95)
    assert specs["RiskOfDatasetInaccuracy"].threshold == pytest.approx(0.05)


def test_constant_threshold_decision_passes_and_fails():
    passed = evaluate_decision_function(
        0.97,
        DecisionFunctionSpec(kind="constant_threshold", threshold=0.95),
        comparator="gte",
    )
    failed = evaluate_decision_function(
        0.02,
        DecisionFunctionSpec(kind="constant_threshold", threshold=0.05),
        comparator="lte",
    )

    assert passed.status == "passed"
    assert failed.status == "passed"


def test_moving_average_decision_uses_previous_history():
    outcome = evaluate_decision_function(
        0.88,
        DecisionFunctionSpec(
            kind="moving_average",
            threshold=0.10,
            warning_threshold=0.05,
            window_size=3,
            min_history=3,
        ),
        comparator="gte",
        history_values=[0.90, 0.92, 0.91, 0.89],
    )

    assert outcome.status == "warning"
    assert "moving average" in outcome.message


def test_learned_model_decision_uses_history_statistics():
    outcome = evaluate_decision_function(
        0.70,
        DecisionFunctionSpec(
            kind="learned_model",
            threshold=1.0,
            warning_threshold=0.25,
            min_history=3,
        ),
        comparator="gte",
        history_values=[0.90, 0.91, 0.89, 0.92],
    )

    assert outcome.status == "failed"
    assert "learned baseline" in outcome.message


def test_if_then_else_falls_back_to_constant_without_history():
    outcome = evaluate_decision_function(
        0.96,
        DecisionFunctionSpec(
            kind="if_then_else",
            threshold=0.95,
            min_history=3,
            window_size=3,
        ),
        comparator="gte",
        history_values=[0.94],
    )

    assert outcome.status == "passed"
    assert outcome.decision_type == "constant_threshold"


def test_extract_history_values_supports_composite_metrics():
    history = [
        {"ValueCompleteness": {"overall": 0.90}},
        {"ValueCompleteness": {"overall": 0.95}},
        {"ValueCompleteness": {"overall": "bad"}},
    ]

    assert extract_history_values(history, "ValueCompleteness", subkey="overall") == pytest.approx([0.9, 0.95])
