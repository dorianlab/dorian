from aif360.algorithms.preprocessing import Reweighing
from aif360.datasets import BinaryLabelDataset
from aif360.metrics import BinaryLabelDatasetMetric

from backend.events import Event, emit


def apply_reweighting(df, target_col, protectedAttributes: list, show_metrics=True):
    """
    Apply AIF360 Reweighing to mitigate class imbalance and group bias.

    Parameters:
    - df: pandas DataFrame
    - target_col: name of the target column
    - protectedAttributes: list of protected attribute column names
    - show_metrics: whether to log fairness metrics

    Returns:
    - X: features DataFrame
    - y: label Series
    - weights: sample weights for model training
    """

    # Wrap data
    dataset = BinaryLabelDataset(
        df=df,
        label_names=[target_col],
        protected_attribute_names=protectedAttributes,
    )

    # add the privileged and unprivileged groups for the credit dataset
    privileged_groups = [{protected_attr: 1}]
    unprivileged_groups = [{protected_attr: 0}]

    # Apply reweighing
    RW = Reweighing(
        unprivileged_groups=unprivileged_groups,
        privileged_groups=privileged_groups,
    )
    dataset_transf = RW.fit_transform(dataset)

    emit(Event("ReweighingApplied", {
        "instance_weights": str(dataset_transf.instance_weights.value_counts().to_dict()),
    }))

    if show_metrics:
        metric = BinaryLabelDatasetMetric(
            dataset_transf,
            privileged_groups=privileged_groups,
            unprivileged_groups=unprivileged_groups,
        )
        emit(Event("FairnessMetricsComputed", {
            "disparate_impact": metric.disparate_impact(),
            "statistical_parity_difference": metric.statistical_parity_difference(),
        }))

    # Extract balanced data
    df_balanced, _ = dataset_transf.convert_to_dataframe()
    X = df_balanced.drop(columns=[target_col])
    y = df_balanced[target_col]
    weights = dataset_transf.instance_weights

    return X, y, weights
