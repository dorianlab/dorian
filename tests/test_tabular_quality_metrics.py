import pytest
import pandas as pd

from dorian.tabular.data.quality.accuracy import (
    BuildValidationRulesFromAllowedValues,
    DataAccuracyRange,
    RiskOfDatasetInaccuracy,
    SemanticDataAccuracy,
    SyntacticDataAccuracy,
)
from dorian.tabular.data.quality.completeness import (
    FeatureCompleteness,
    LabelCompleteness,
    RecordCompleteness,
    ValueCompleteness,
    ValueOccurrenceCompleteness,
)
from dorian.tabular.data.quality.consistency import DataRecordConsistency


def test_syntactic_data_accuracy_uses_closed_set_normalization():
    df = pd.DataFrame({"status": [" Open ", "closed", "BAD", None]})
    rules = BuildValidationRulesFromAllowedValues({"status": ["OPEN", "CLOSED"]})

    assert SyntacticDataAccuracy(df, rules) == pytest.approx(0.5)


def test_semantic_data_accuracy_counts_only_applicable_rules():
    df = pd.DataFrame(
        {
            "country": ["DE", "DE", "US"],
            "currency": ["EUR", "USD", "USD"],
        }
    )
    rules = [
        {
            "condition": {
                "operator": "AND",
                "clauses": [{"column": "country", "value": "DE"}],
            },
            "target_column": "currency",
            "valid_values": ["EUR"],
        }
    ]

    assert SemanticDataAccuracy(df, rules) == pytest.approx(0.5)


def test_risk_of_dataset_inaccuracy_counts_outlier_ratio():
    df = pd.DataFrame({"amount": [10, 12, 11, 500]})

    assert RiskOfDatasetInaccuracy(df, ["amount"]) == pytest.approx(0.25)


def test_data_accuracy_range_counts_in_range_values():
    df = pd.DataFrame({"age": [10, 20, 130, None]})

    assert DataAccuracyRange(df, {"age": [0, 120]}) == pytest.approx(0.5)


def test_value_completeness_returns_overall_and_column_scores():
    df = pd.DataFrame(
        {
            "a": [1, None, 3],
            "b": ["x", "y", None],
        }
    )

    result = ValueCompleteness(df)

    assert result == pytest.approx(
        {
            "overall": 0.67,
            "a": 0.67,
            "b": 0.67,
        },
        abs=0.01,
    )


def test_feature_completeness_returns_requested_feature_scores():
    df = pd.DataFrame(
        {
            "a": [1, None, 3],
            "b": [1, 2, 3],
            "target": ["x", "y", "z"],
        }
    )

    assert FeatureCompleteness(df, ["a", "b"]) == pytest.approx({"a": 0.67, "b": 1.0}, abs=0.01)


def test_existing_completeness_and_consistency_metrics_still_work():
    df = pd.DataFrame(
        {
            "feature": [1, None, 1],
            "label": ["yes", "", "yes"],
        }
    )

    assert RecordCompleteness(df) == pytest.approx(0.67, abs=0.01)
    assert LabelCompleteness(df, "label") == pytest.approx(0.67, abs=0.01)
    assert ValueOccurrenceCompleteness(df, [("label", "yes", 2)]) == pytest.approx({"label:yes": 1.0})
    assert DataRecordConsistency(df) == pytest.approx(0.33, abs=0.01)
