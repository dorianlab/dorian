import pandas as pd

from dorian.tabular.data.quality.mitigation import (
    enforce_compliance_rules,
    normalize_format_values,
    remove_irrelevant_records,
    round_values_to_required_precision,
)


def test_enforce_compliance_rules_clamps_and_normalizes_supported_rules():
    df = pd.DataFrame({
        "age": [15, 35, 105],
        "status": ["bad", "approved", "unknown"],
    })

    mitigated, log = enforce_compliance_rules(df, {
        "age": {"op": "between", "value": [18, 100]},
        "status": {"op": "in", "value": ["approved", "pending"]},
    })

    assert mitigated["age"].tolist() == [18, 35, 100]
    assert mitigated["status"].tolist() == ["approved", "approved", "approved"]
    assert len(log) > 0
    assert all("method" in entry for entry in log)


def test_normalize_format_values_converts_common_types():
    df = pd.DataFrame({
        "age": ["20", "31.0"],
        "score": ["1.5", "2"],
        "active": ["yes", "0"],
        "when": ["2024-01-01", "2024-01-02"],
    })

    mitigated, log = normalize_format_values(df, {
        "age": "int",
        "score": "float",
        "active": "bool",
        "when": "datetime",
    })

    assert mitigated["age"].tolist() == [20, 31]
    assert mitigated["score"].tolist() == [1.5, 2.0]
    assert mitigated["active"].tolist() == [True, False]
    assert all(isinstance(value, str) for value in mitigated["when"].tolist())
    assert len(log) > 0


def test_round_values_to_required_precision_rounds_numeric_columns():
    df = pd.DataFrame({
        "interest_rate": [1.2345, 2.3456],
        "amount": [100.987, 200.111],
    })

    mitigated, log = round_values_to_required_precision(df, {
        "interest_rate": 2,
        "amount": 1,
    })

    assert mitigated["interest_rate"].tolist() == [1.23, 2.35]
    assert mitigated["amount"].tolist() == [101.0, 200.1]
    assert len(log) > 0


def test_remove_irrelevant_records_filters_rows_by_condition():
    df = pd.DataFrame({
        "loan_status": [1, 0, 1],
        "amount": [1000, 2000, 3000],
    })

    mitigated, log = remove_irrelevant_records(df, {
        "operator": "AND",
        "clauses": [
            {"column": "loan_status", "op": "eq", "value": 1},
        ],
    })

    assert mitigated["loan_status"].tolist() == [1, 1]
    assert mitigated["amount"].tolist() == [1000, 3000]
    assert len(log) == 1  # 1 row removed
