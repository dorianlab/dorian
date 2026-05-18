import math

import pytest
import pandas as pd

from dorian.tabular.data.quality.balance import (
    LabelDistributionBalance,
    LabelProportionBalance,
)
from dorian.tabular.data.quality.compliance import DataItemCompliance
from dorian.tabular.data.quality.consistency import (
    DataFormatConsistency,
    DataLabelConsistency,
    SemanticConsistency,
)
from dorian.tabular.data.quality.diversity import (
    CategorySizeDiversity,
    LabelRichness,
    RelativeLabelAbundance,
)
from dorian.tabular.data.quality.effectiveness import (
    CategorySizeEffectiveness,
    FeatureEffectiveness,
    LabelEffectiveness,
)
from dorian.tabular.data.quality.efficiency import (
    DataFormatEfficiency,
    DataProcessingEfficiency,
    RiskOfWastedSpace,
)
from dorian.tabular.data.quality.precision import PrecisionOfDataValues
from dorian.tabular.data.quality.relevance import FeatureRelevance, RecordRelevance
from dorian.tabular.data.quality.representativeness import RepresentativenessRatio
from dorian.tabular.data.quality.similarity import (
    SampleIndependency,
    SampleSimilarity,
    SampleTightness,
)


def test_balance_metrics_compute_expected_values():
    df = pd.DataFrame(
        {
            "group": ["A", "A", "B", "B"],
            "label": ["yes", "no", "yes", "yes"],
        }
    )

    proportion = LabelProportionBalance(df, "group", "label", ["yes", "no"])
    distribution = LabelDistributionBalance(df, "label", ["yes", "no"])

    assert proportion == pytest.approx(
        {
            "A|B|yes": -0.5,
            "A|B|no": 0.5,
        }
    )
    assert distribution == pytest.approx(0.05, abs=0.01)


def test_compliance_and_consistency_metrics_compute_expected_values():
    compliance_df = pd.DataFrame(
        {
            "age": [25, 17, 30],
            "country": ["DE", "US", "FR"],
        }
    )
    compliance_rules = {
        "age": {"op": "gte", "value": 18},
        "country": {"op": "in", "value": ["DE", "FR"]},
    }
    assert DataItemCompliance(compliance_df, compliance_rules) == pytest.approx(0.67, abs=0.01)

    format_df = pd.DataFrame({"num": [1, 2, 3], "flag": [True, False, True]})
    assert DataFormatConsistency(format_df, {"num": "int", "flag": "bool"}) == pytest.approx(1.0)

    semantic_df = pd.DataFrame(
        {
            "country": ["DE", "US", "FR"],
            "currency": ["EUR", "USD", "EUR"],
        }
    )
    semantic_rules = [
        {
            "operator": "AND",
            "clauses": [{"column": "country", "value": "DE"}],
        },
        {
            "operator": "AND",
            "clauses": [{"column": "currency", "value": "EUR"}],
        },
    ]
    assert SemanticConsistency(semantic_df, semantic_rules) == pytest.approx(0.5)

    label_df = pd.DataFrame(
        {
            "x": [0.0, 0.1, 10.0, 10.1],
            "y": [0.0, 0.1, 10.0, 10.1],
            "target": ["a", "a", "b", "b"],
        }
    )
    result = DataLabelConsistency(label_df, "target", consistency_label_threshold=1.0)
    assert result == pytest.approx(1.0)


def test_diversity_metrics_compute_expected_values():
    df = pd.DataFrame(
        {
            "target": ["a", "a", "b", "c"],
            "category": ["x", "x", "x", "y"],
        }
    )

    assert LabelRichness(df, "target") == pytest.approx(0.75)
    assert RelativeLabelAbundance(df, "target", ["a", "b", "c"]) == pytest.approx(
        {
            "a": 0.5,
            "b": 0.25,
            "c": 0.25,
        }
    )
    assert CategorySizeDiversity(df, "category") == pytest.approx(0.5)


def test_effectiveness_metrics_compute_expected_values():
    df = pd.DataFrame(
        {
            "score": [0.9, 0.7, 0.95, 0.85],
            "target": ["keep", "drop", "keep", "keep"],
            "bucket": ["x", "x", "x", "y"],
        }
    )

    feature_rules = {"score": [{"op": "gte", "value": 0.8}]}
    assert FeatureEffectiveness(df, feature_rules) == pytest.approx({"score": 0.75})
    assert CategorySizeEffectiveness(df, "bucket", 2) == pytest.approx(0.5)
    assert LabelEffectiveness(df, "target", [{"op": "eq", "value": "keep"}]) == pytest.approx(0.75)


def test_efficiency_metrics_compute_expected_values():
    format_df = pd.DataFrame({"num": pd.Series([1, 2, 3], dtype="int64")})
    format_eff = DataFormatEfficiency(format_df)
    assert format_eff == pytest.approx(0.12, abs=0.01)

    processing_df = pd.DataFrame({"text_num": ["1", "2", "3", "4"]})
    processing_eff = DataProcessingEfficiency(processing_df)
    assert not pd.isna(processing_eff)
    assert processing_eff > 0

    wasted_df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    wasted = RiskOfWastedSpace(wasted_df, target_size=100)
    assert wasted > 0


def test_precision_relevance_and_representativeness_metrics_compute_expected_values():
    precision_df = pd.DataFrame({"amount": [1.23, 2.34, 3.456]})
    assert PrecisionOfDataValues(precision_df, {"amount": 2}) == pytest.approx(0.67, abs=0.01)

    relevance_df = pd.DataFrame(
        {
            "a": [1, 2, 3],
            "b": ["x", "y", "z"],
            "flag": [True, False, True],
        }
    )
    assert FeatureRelevance(relevance_df, ["a", "flag", "missing"]) == pytest.approx(0.67, abs=0.01)
    assert RecordRelevance(
        relevance_df,
        {"operator": "AND", "clauses": [{"column": "flag", "value": True}]},
    ) == pytest.approx(0.67, abs=0.01)
    assert RepresentativenessRatio(relevance_df, ["a", "flag", "missing"]) == pytest.approx(0.67, abs=0.01)


def test_similarity_metrics_compute_expected_values():
    df = pd.DataFrame(
        {
            "f1": [0.0, 0.1, 9.9, 10.0],
            "f2": [0.0, 0.1, 9.8, 10.1],
            "f3": [0.05, 0.2, 9.7, 10.2],
            "target": ["a", "a", "b", "b"],
        }
    )

    similarity = SampleSimilarity(df, "target")
    tightness = SampleTightness(df, "target")
    independency = SampleIndependency(df, "target")

    assert similarity == pytest.approx(0.5)
    assert tightness == pytest.approx(3.0)
    assert independency == pytest.approx(0.67, abs=0.01)
