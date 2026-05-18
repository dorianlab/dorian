import pandas as pd
from aif360.datasets import BinaryLabelDataset
from aif360.metrics import ClassificationMetric

from backend.events import Event, emit


def evaluate_model_fairness(model, X_test, y_test, df, protected_attrs: list, priv, unpriv, label_col="Creditability"):
    """
    Evaluates the fairness of a scikit-learn model using AIF360 ClassificationMetric.

    Parameters:
    - model: trained sklearn model (e.g., SVC, RandomForest)
    - X_test: test feature DataFrame (must not include label or protected attr)
    - y_test: true labels for test set
    - df: original full DataFrame (to pull protected attribute from test index)
    - protected_attrs: list of protected attribute column names
    - priv: dict of privileged group values
    - unpriv: dict of unprivileged group values
    - label_col: name of the target column

    Returns:
    - dict of fairness metrics
    """

    emit(Event("FairnessEvalStarted", {
        "X_test_columns": X_test.columns.tolist(),
        "df_columns": df.columns.tolist(),
        "protected_attrs": protected_attrs,
        "privileged": priv,
        "unprivileged": unpriv,
    }))

    newpriv = {}
    newunpriv = {}
    # converting the unprivileged and privileged groups names such that they can be used with the one hot encoded dataset
    for key, value in priv.items():
        new_key = str(key) + "_" + str(value) + ".0"
        new_value = 1.0
        newpriv[new_key] = new_value

    for key, value in unpriv.items():
        new_key = str(key) + "_" + str(value) + ".0"
        new_value = 1.0
        newunpriv[new_key] = new_value

    priv = newpriv
    unpriv = newunpriv
    protected_attrs = list(priv.keys()) + list(unpriv.keys())
    emit(Event("FairnessGroupsMapped", {"priv": priv, "unpriv": unpriv, "attrs": protected_attrs}))

    for protected_attr in protected_attrs:
        # Step 1: Construct AIF360 test dataset with protected attribute
        X_test_with_attr = X_test.copy()
        emit(Event("FairnessProcessingAttribute", {"protected_attr": protected_attr}))
        X_test_with_attr[protected_attr] = df.loc[X_test.index, protected_attr]
        y_test_full = y_test.copy()

        test_df = X_test_with_attr.copy()
        test_df[label_col] = y_test_full

        emit(Event("FairnessTestDataShape", {"shape": list(test_df.shape)}))

        test_dataset = BinaryLabelDataset(
            df=test_df,
            label_names=[label_col],
            protected_attribute_names=[protected_attr],
        )

        # Step 2: Predict
        y_pred = model.predict(X_test)

        # Step 3: Create prediction dataset
        pred_df = test_df.copy()
        pred_df[label_col] = y_pred

        pred_dataset = BinaryLabelDataset(
            df=pred_df,
            label_names=[label_col],
            protected_attribute_names=[protected_attr],
        )

        # Step 4: Fairness metrics
        privileged = [priv]
        unprivileged = [unpriv]

        metric = ClassificationMetric(
            test_dataset,
            pred_dataset,
            unprivileged_groups=unprivileged,
            privileged_groups=privileged,
        )

        result = {
            "Disparate Impact": metric.disparate_impact(),
            "Statistical Parity Difference": metric.statistical_parity_difference(),
            "Equal Opportunity Difference": metric.equal_opportunity_difference(),
            "Theil Index": metric.theil_index(),
        }

        emit(Event("FairnessMetricsResult", {"protected_attr": protected_attr, "metrics": result}))

    # return result
