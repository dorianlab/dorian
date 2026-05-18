import numpy as np
import pandas as pd
from pandas.api.types import is_categorical_dtype, is_numeric_dtype
from scipy.stats import chi2_contingency, chisquare, ks_2samp
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score

from backend.events import Event, emit


# TODO: do this for all the protected features


def class_imbalance(df, target_col):
    """
    Check class imbalance in the target column of the dataframe
    """
    emit(Event("CheckClassImbalance", {"target_col": target_col}))
    counts = df[target_col].value_counts()
    total = counts.sum()
    expected = np.full_like(counts, total / len(counts))
    chi2, p = chisquare(f_obs=counts, f_exp=expected)
    return p < 0.05


def covariate_shift(X_train, X_test):
    """
    Checks for covariate shift between training and test feature distributions.
    Returns the AUC of a classifier trained to distinguish them.
    """
    # TODO: check if the threshold can be automatically set
    threshold = 0.55  # fairness
    X = pd.concat([X_train, X_test], axis=0)
    y = np.array([0] * len(X_train) + [1] * len(X_test))

    clf = RandomForestClassifier(n_estimators=50, random_state=42)
    auc = cross_val_score(clf, X, y, cv=5, scoring="roc_auc").mean()

    return auc > threshold


def selection_bias(X_train, X_test, protectedAttributes):
    """
    Check selection bias in the target column of the dataframe
    """
    emit(Event("CheckSelectionBias", {}))
    potential_bias = []
    alpha = 0.05

    for col in protectedAttributes:
        try:
            if is_categorical_dtype(X_train[col]):
                train_counts = X_train[col].value_counts()
                test_counts = X_test[col].value_counts()
                combined = pd.concat([train_counts, test_counts], axis=1).fillna(0)
                combined.columns = ["Train", "Test"]

                chi2, p, _, _ = chi2_contingency(combined)
                if p < alpha:
                    potential_bias.append(True)
                else:
                    potential_bias.append(False)

            elif is_numeric_dtype(X_train[col]):
                stat, p = ks_2samp(X_train[col], X_test[col])
                if p < alpha:
                    potential_bias.append(True)
                else:
                    potential_bias.append(False)

            else:
                emit(Event("CheckSkippedUnsupportedDtype", {"column": col}))

        except Exception as e:
            emit(Event("CheckFeatureError", {"column": col, "error": str(e)}))

    return any(potential_bias)


def group_bias(df, protectedAttributes, target_col):
    """
    Check group bias in the target column of the dataframe
    """
    potential_bias = []
    for col in protectedAttributes:
        ctab = pd.crosstab(df[col], df[target_col], normalize="index")

        chi2, p, _, _ = chi2_contingency(pd.crosstab(df[col], df[target_col]))
        if p < 0.05:
            potential_bias.append(True)
        else:
            potential_bias.append(False)

    return any(potential_bias)


def data_leakage():
    """
    Check data leakage in the target column of the dataframe
    """
    emit(Event("CheckDataLeakage", {"status": "not yet implemented"}))


def sampling_bias(X_train, X_test, protectedAttributes):
    """
    Check sampling bias in the target column of the dataframe
    """
    emit(Event("CheckSamplingBias", {}))
    potential_bias = []

    for col in protectedAttributes:
        if X_train[col].dtype == "category":
            # Chi-squared test for categorical features
            train_counts = X_train[col].value_counts()
            test_counts = X_train[col].value_counts()
            combined = pd.concat([train_counts, test_counts], axis=1).fillna(0)
            chi2, p, _, _ = chi2_contingency(combined)
            if p < 0.05:
                potential_bias.append(True)
            else:
                potential_bias.append(False)
        else:
            # KS test for continuous features
            stat, p = ks_2samp(X_train[col], X_test[col])
            if p < 0.05:
                potential_bias.append(True)
            else:
                potential_bias.append(False)

    return any(potential_bias)


def temporal_bias():
    """
    Check temporal bias in the target column of the dataframe
    """
    emit(Event("CheckTemporalBias", {"status": "not yet implemented"}))


def feature_scaling_bias(df_before, df_after, protectedAttributes):
    """
    Checks for feature scaling bias by comparing standard deviation changes.
    Returns True if bias is likely, False otherwise.
    """
    threshold = 0.1  # Set a threshold for significant change
    emit(Event("CheckFeatureScalingBias", {}))
    potential_bias = []
    stds = df_after.std()

    for col in protectedAttributes:
        if col in df_after.columns and pd.api.types.is_numeric_dtype(df_before[col]):
            std_before = df_before[col].std()
            std_after = df_after[col].std()
            change = abs(std_after - std_before) / (std_before + 1e-8)

            if change > threshold:
                potential_bias.append(True)
            else:
                potential_bias.append(False)

    return any(potential_bias)


def outlier_bias(df_before, df_after, protectedAttributes):
    """
    Detects outlier bias introduced by scaling (e.g., MinMaxScaler or StandardScaler).
    Compares std and range before and after scaling.

    Returns True if significant changes suggest outlier distortion.
    """
    emit(Event("CheckOutlierBias", {}))
    potential_bias = []
    std_drop_threshold = 0.9

    for col in protectedAttributes:
        if col in df_after.columns and pd.api.types.is_numeric_dtype(df_before[col]):
            std_before = df_before[col].std()
            std_after = df_after[col].std()
            range_before = df_before[col].max() - df_before[col].min()
            range_after = df_after[col].max() - df_after[col].min()

            if std_before == 0 or range_before == 0:
                continue  # skip constant features

            std_ratio = std_after / std_before
            range_ratio = range_after / range_before

            if std_ratio < (1 - std_drop_threshold) or range_ratio < 0.2:
                emit(Event("OutlierBiasDetected", {"column": col}))
                potential_bias.append(True)
            else:
                emit(Event("OutlierBiasNone", {"column": col}))
                potential_bias.append(False)

    return any(potential_bias)


def model_interpretability_bias():
    """
    Check model interpretability bias in the target column of the dataframe
    """
    emit(Event("CheckModelInterpretabilityBias", {}))


def domain_shift_bias(X_train, X_test):
    """
    Detects domain shift bias by comparing distributions of features
    between training and test sets using statistical tests.

    Returns True if domain shift is detected for any feature.
    """
    alpha = 0.05

    emit(Event("CheckDomainShiftBias", {}))
    potential_bias = []

    for col in X_train.columns:
        try:
            if pd.api.types.is_numeric_dtype(X_train[col]):
                stat, p = ks_2samp(X_train[col], X_test[col])
                if p < alpha:
                    potential_bias.append(True)
                else:
                    potential_bias.append(False)
            else:
                train_counts = X_train[col].value_counts()
                test_counts = X_test[col].value_counts()
                combined = pd.concat([train_counts, test_counts], axis=1).fillna(0)
                chi2, p, _, _ = chi2_contingency(combined)
                if p < alpha:
                    potential_bias.append(True)
                else:
                    potential_bias.append(False)
        except Exception as e:
            emit(Event("DomainShiftFeatureError", {"column": col, "error": str(e)}))

    return any(potential_bias)


def loss_of_ordinality_bias(df):
    """
    Detects if any ordered categorical variables have lost their ordinality.
    Returns True if any such issue is found.
    """

    potential_bias = []
    for col in df.columns:
        if pd.api.types.is_categorical_dtype(df[col]):
            if not df[col].cat.ordered:
                potential_bias.append(True)
            else:
                potential_bias.append(False)

    return any(potential_bias)


def zero_variance_feature_bias(df):
    """
    Detects zero-variance (or near-zero) features in the dataset.
    Returns True if any are found.
    """
    emit(Event("CheckZeroVarianceBias", {}))
    threshold = 1
    zero_var_features = df.columns[df.nunique() <= threshold].tolist()
    if zero_var_features:
        emit(Event("ZeroVarianceFeaturesFound", {
            "count": len(zero_var_features),
            "features": zero_var_features,
        }))
        return True
    else:
        emit(Event("ZeroVarianceFeaturesNone", {}))
        return False


def data_bias():
    """
    Check data bias in the target column of the dataframe
    """
    emit(Event("CheckDataBias", {}))


# ---------------------------------------------------------------------------
# LLM guardrail checks — operate on text, not DataFrames.
# These are stubs; real implementations will use NLP classifiers or
# pattern-based scanners at pipeline execution time.
# ---------------------------------------------------------------------------

def prompt_injection_scan(text: str) -> bool:
    """Scan input text for common prompt injection patterns."""
    emit(Event("CheckPromptInjectionScan", {"status": "stub"}))
    return False


def toxicity_scan(text: str) -> bool:
    """Scan text for toxic or harmful content."""
    emit(Event("CheckToxicityScan", {"status": "stub"}))
    return False


def pii_leak_scan(text: str) -> bool:
    """Scan text for personally identifiable information."""
    emit(Event("CheckPiiLeakScan", {"status": "stub"}))
    return False


def hallucination_check(text: str) -> bool:
    """Check generated text for hallucinated claims."""
    emit(Event("CheckHallucination", {"status": "stub"}))
    return False


mapChecks = {
    "class_imbalance": "dorian.toolbox.checks.class_imbalance",
    "covariate_shift": "dorian.toolbox.checks.covariate_shift",
    "selection_bias": "dorian.toolbox.checks.selection_bias",
    "group_bias": "dorian.toolbox.checks.group_bias",
    "data_leakage": "dorian.toolbox.checks.data_leakage",
    "sampling_bias": "dorian.toolbox.checks.sampling_bias",
    "temporal_bias": "dorian.toolbox.checks.temporal_bias",
    "feature_scaling_bias": "dorian.toolbox.checks.feature_scaling_bias",
    "outlier_bias": "dorian.toolbox.checks.outlier_bias",
    "model_interpretability_bias": "dorian.toolbox.checks.model_interpretability_bias",
    "domain_shift_bias": "dorian.toolbox.checks.domain_shift_bias",
    "loss_of_ordinality_bias": "dorian.toolbox.checks.loss_of_ordinality_bias",
    "zero-variance_feature_bias": "dorian.toolbox.checks.zero_variance_feature_bias",
    "data_bias": "dorian.toolbox.checks.data_bias",
    # LLM guardrail checks
    "prompt_injection_scan": "dorian.toolbox.checks.prompt_injection_scan",
    "toxicity_scan": "dorian.toolbox.checks.toxicity_scan",
    "pii_leak_scan": "dorian.toolbox.checks.pii_leak_scan",
    "hallucination_check": "dorian.toolbox.checks.hallucination_check",
}

if __name__ == "__main__":
    df = pd.read_csv("data/credit_dtypes.csv")
    target_cols = ["Creditability"]
    emit(Event("CheckResult", {"result": class_imbalance(df, target_cols)}))
