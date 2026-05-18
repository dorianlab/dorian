"""Seed catalog loader for the Tier-C / Tier-D RL demo.

Hard-codes the 10-operator subset required to build reasonable
tabular classification pipelines. Each entry is an
``OperatorMeta`` from ``rl.catalog.schema``; the full Neo4j-backed
loader is future work.

The seed deliberately mirrors what ``sklearn.datasets.fetch_openml``
can deliver for CC-18 classification: loader -> imputer ->
encoder -> scaler -> splitter -> estimator -> metric. The RL
agent is free to skip steps or combine them; the env's mask
enforces validity.
"""
from __future__ import annotations

from .schema import (
    DeterminismClass,
    DomainKind,
    OperatorMeta,
    ParameterSpec,
    PortSpec,
)


def seed_catalog() -> tuple[OperatorMeta, ...]:
    """Return the 10-op seed catalog for the demo.

    The set covers:
      * loader:        ``dorian.io.dataset`` (Dorian primitive that
                       wraps the session-bound CSV)
      * split:         ``sklearn.model_selection.train_test_split``
      * imputer:       ``sklearn.impute.SimpleImputer``
      * encoder:       ``sklearn.preprocessing.OrdinalEncoder``
                       (chosen over OneHotEncoder because of the
                       all-categorical kr-vs-kp case)
      * scaler:        ``sklearn.preprocessing.StandardScaler``
      * reducer:       ``sklearn.decomposition.PCA``
      * classifiers:   ``sklearn.ensemble.RandomForestClassifier``,
                       ``sklearn.ensemble.ExtraTreesClassifier``,
                       ``sklearn.ensemble.GradientBoostingClassifier``,
                       ``sklearn.ensemble.HistGradientBoostingClassifier``,
                       ``sklearn.ensemble.AdaBoostClassifier``,
                       ``sklearn.ensemble.BaggingClassifier``,
                       ``sklearn.linear_model.LogisticRegression``
      * composers:     ``sklearn.ensemble.VotingClassifier`` (2 base
                       estimator inputs -> composed Model),
                       ``sklearn.ensemble.StackingClassifier`` (2 base
                       estimator inputs -> composed Model)
      * fit:           ``fit`` method shortcut (max_occurrence=3
                       so the agent can train multiple base
                       estimators for a voter)
      * predict:       ``predict`` method shortcut (max_occurrence=3
                       for the same reason)
      * metric:        ``sklearn.metrics.accuracy_score``

    Estimators no longer carry ``exclusivity_group="estimator"``.
    That exclusivity was blocking multi-voter composition; max
    occurrence per op_key (2-3) is a softer cap that still keeps
    the search space bounded.
    """
    return (
        # Loader ------------------------------------------------------------
        OperatorMeta(
            op_key="dorian.io.dataset",
            family="loader",
            task_tags=("classification",),
            inputs=(),
            outputs=(PortSpec("X", "DataFrame"), PortSpec("y", "Array")),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="dorian-platform-1",
            max_occurrence=1,
        ),
        # Splitter ----------------------------------------------------------
        # train_test_split takes *arrays positionally -- inputs use
        # numeric port names so mask enumeration + executor edges
        # agree with sklearn's actual signature.
        OperatorMeta(
            op_key="sklearn.model_selection.train_test_split",
            family="splitter",
            task_tags=("classification",),
            inputs=(PortSpec("0", "DataFrame"), PortSpec("1", "Array")),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("y_test", "Array"),
            ),
            parameters=(
                ParameterSpec("test_size", "float", default="0.2",
                              low=0.1, high=0.4),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # Imputer -----------------------------------------------------------
        OperatorMeta(
            op_key="sklearn.impute.SimpleImputer",
            family="imputer",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("strategy", "string", default="most_frequent",
                              choices=("mean", "median", "most_frequent")),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            # No exclusivity + occurrence cap 2 -- the agent may
            # stack imputers (e.g. one for train, one for test)
            # or leave the pipeline without one. Keeping the cap
            # low prevents combinatorial blow-up.
            max_occurrence=2,
        ),
        # Encoder: ordinal --------------------------------------------------
        OperatorMeta(
            op_key="sklearn.preprocessing.OrdinalEncoder",
            family="encoder",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("handle_unknown", "string",
                              default="use_encoded_value"),
                ParameterSpec("unknown_value", "int", default="-1"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=2,
        ),
        # Encoder: one-hot --------------------------------------------------
        # Alternative to OrdinalEncoder for nominal categoricals — the
        # canonical fix for the "DataFrame contains string '<0'" class
        # of runtime failures that blocks ~2/3 of the default CC-18
        # dataset pool (credit-g, kr-vs-kp) when the pipeline lacks
        # categorical handling. Exclusivity with OrdinalEncoder would
        # be wrong (both can coexist on different columns via a
        # future ColumnTransformer); kept independent for now.
        OperatorMeta(
            op_key="sklearn.preprocessing.OneHotEncoder",
            family="encoder",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("handle_unknown", "string",
                              default="ignore",
                              choices=("error", "ignore", "infrequent_if_exist")),
                ParameterSpec("sparse_output", "bool", default="False"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Scaler: standard (z-score) ---------------------------------------
        OperatorMeta(
            op_key="sklearn.preprocessing.StandardScaler",
            family="scaler",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("with_mean", "bool", default="True"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=2,
        ),
        # Scaler: min-max [0, 1] -------------------------------------------
        OperatorMeta(
            op_key="sklearn.preprocessing.MinMaxScaler",
            family="scaler",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=2,
        ),
        # Scaler: robust (median + IQR, outlier-resistant) -----------------
        OperatorMeta(
            op_key="sklearn.preprocessing.RobustScaler",
            family="scaler",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("with_centering", "bool", default="True"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=2,
        ),
        # Selector: variance threshold -------------------------------------
        OperatorMeta(
            op_key="sklearn.feature_selection.VarianceThreshold",
            family="selector",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("threshold", "float", default="0.0",
                              low=0.0, high=0.5),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Selector: univariate K-best --------------------------------------
        # Supervised: y_train is read during fit (score_func scored
        # against the target) but passed through unchanged.
        OperatorMeta(
            op_key="sklearn.feature_selection.SelectKBest",
            family="selector",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("k", "int", default="10",
                              low=1, high=50),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Reducer: truncated SVD (sparse-friendly PCA alternative) ---------
        OperatorMeta(
            op_key="sklearn.decomposition.TruncatedSVD",
            family="reducer",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("n_components", "int", default="5",
                              low=2, high=50),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # Reducer: feature agglomeration (hierarchical column merging) -----
        OperatorMeta(
            op_key="sklearn.cluster.FeatureAgglomeration",
            family="reducer",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("n_clusters", "int", default="5",
                              low=2, high=50),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Transform: quantile (rank-based scaler that normalises to
        # uniform or normal) -----------------------------------------------
        OperatorMeta(
            op_key="sklearn.preprocessing.QuantileTransformer",
            family="transform",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("output_distribution", "string",
                              default="uniform",
                              choices=("uniform", "normal")),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # Transform: K-bins discretizer (continuous → ordinal bins) --------
        OperatorMeta(
            op_key="sklearn.preprocessing.KBinsDiscretizer",
            family="transform",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("n_bins", "int", default="5",
                              low=2, high=20),
                ParameterSpec("encode", "string", default="ordinal",
                              choices=("onehot-dense", "ordinal")),
                ParameterSpec("strategy", "string", default="quantile",
                              choices=("uniform", "quantile", "kmeans")),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Transform: binarizer (threshold to 0/1) --------------------------
        OperatorMeta(
            op_key="sklearn.preprocessing.Binarizer",
            family="transform",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("threshold", "float", default="0.0",
                              low=-1.0, high=1.0),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Generator: polynomial features (degree-d interactions) -----------
        # The one member of the "Generator" taxonomy in the figure —
        # expands feature space rather than reducing it, complementary
        # to the Reducer category below. degree>2 on large datasets
        # blows up dim so capped to 3 in the RL env.
        OperatorMeta(
            op_key="sklearn.preprocessing.PolynomialFeatures",
            family="generator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("degree", "int", default="2", low=2, high=3),
                ParameterSpec("interaction_only", "bool", default="False"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Imputer: KNN-based (nearest-neighbour imputation) ----------------
        OperatorMeta(
            op_key="sklearn.impute.KNNImputer",
            family="imputer",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("n_neighbors", "int", default="5",
                              low=1, high=20),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Reducer: PCA -----------------------------------------------------
        # Reclassified from family="selector" per the 11-category
        # taxonomy: Selector picks a SUBSET of features (KBest,
        # VarianceThreshold, FromModel), Reducer transforms into a
        # lower-rank REPRESENTATION (PCA, SVD, Feat.Agglom).
        OperatorMeta(
            op_key="sklearn.decomposition.PCA",
            family="reducer",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("X_test", "DataFrame"),
            ),
            parameters=(
                ParameterSpec("n_components", "int", default="10"),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: random forest ----------------------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.RandomForestClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_estimators", "int", default="100",
                              low=10, high=500),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: extra trees ------------------------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.ExtraTreesClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_estimators", "int", default="100",
                              low=10, high=500),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: gradient boosting ------------------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.GradientBoostingClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_estimators", "int", default="100",
                              low=20, high=500),
                ParameterSpec("learning_rate", "float", default="0.1",
                              low=0.01, high=1.0, log_scale=True),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: histogram gradient boosting --------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.HistGradientBoostingClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("max_iter", "int", default="100",
                              low=20, high=500),
                ParameterSpec("learning_rate", "float", default="0.1",
                              low=0.01, high=1.0, log_scale=True),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: logistic regression ----------------------------------
        OperatorMeta(
            op_key="sklearn.linear_model.LogisticRegression",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("max_iter", "int", default="500"),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Ensemble: AdaBoost -----------------------------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.AdaBoostClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_estimators", "int", default="50",
                              low=10, high=500),
                ParameterSpec("learning_rate", "float", default="1.0",
                              low=0.01, high=2.0, log_scale=True),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Ensemble: Bagging ------------------------------------------------
        OperatorMeta(
            op_key="sklearn.ensemble.BaggingClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_estimators", "int", default="10",
                              low=5, high=100),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: decision tree ----------------------------------------
        OperatorMeta(
            op_key="sklearn.tree.DecisionTreeClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("max_depth", "int", default="10",
                              low=2, high=30),
                ParameterSpec("min_samples_split", "int", default="2",
                              low=2, high=20),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=2,
        ),
        # Classifier: support vector machine -------------------------------
        # kernel="rbf" default is the widely-used baseline; agent can
        # pick from the choices tuple via ChangeParamValue actions.
        OperatorMeta(
            op_key="sklearn.svm.SVC",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("C", "float", default="1.0",
                              low=0.01, high=100.0, log_scale=True),
                ParameterSpec("kernel", "string", default="rbf",
                              choices=("linear", "poly", "rbf", "sigmoid")),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # Classifier: k-nearest neighbours ---------------------------------
        OperatorMeta(
            op_key="sklearn.neighbors.KNeighborsClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("n_neighbors", "int", default="5",
                              low=1, high=50),
                ParameterSpec("weights", "string", default="uniform",
                              choices=("uniform", "distance")),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Classifier: Gaussian naive Bayes ---------------------------------
        # Parameter-less; useful as a baseline + diversity source for
        # ensembles. No random_state because it's fully deterministic
        # under its closed-form MLE.
        OperatorMeta(
            op_key="sklearn.naive_bayes.GaussianNB",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
        # Classifier: ridge -----------------------------------------------
        # Linear-model alternative to LogisticRegression that trains
        # via closed-form least squares; cheaper + stable on tall
        # datasets.
        OperatorMeta(
            op_key="sklearn.linear_model.RidgeClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("alpha", "float", default="1.0",
                              low=0.01, high=100.0, log_scale=True),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # Classifier: multi-layer perceptron ("Neural Net" in the
        # taxonomy figure). Single hidden layer by default keeps wall-
        # clock manageable; max_iter capped so pathological runs time
        # out on the env-level executor timeout rather than hanging.
        OperatorMeta(
            op_key="sklearn.neural_network.MLPClassifier",
            family="estimator",
            task_tags=("classification",),
            inputs=(
                PortSpec("X_train", "DataFrame"),
                PortSpec("y_train", "Array"),
                PortSpec("X_test", "DataFrame"),
            ),
            outputs=(PortSpec("y_pred", "Prediction"),),
            parameters=(
                ParameterSpec("alpha", "float", default="0.0001",
                              low=1e-5, high=1e-1, log_scale=True),
                ParameterSpec("learning_rate_init", "float", default="0.001",
                              low=1e-4, high=1e-1, log_scale=True),
                ParameterSpec("max_iter", "int", default="200",
                              low=50, high=500),
                ParameterSpec("random_state", "int", default="42"),
            ),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            random_state_param_name="random_state",
            max_occurrence=1,
        ),
        # NB: ``dorian.compose.pack`` + ``VotingClassifier`` +
        # ``StackingClassifier`` are deliberately OMITTED from the
        # catalog at this revision. They were designed against the
        # old (X, y) → Model rail where each classifier exposed an
        # unfitted Model output that pack could collect into a
        # ModelList for the ensemble composer. The new rail
        # ((X_train, y_train, X_test) → Prediction) folds fit +
        # predict into a single node per-classifier, so there's no
        # "unfitted Model" port to feed into a voter. Restoring
        # ensemble composition needs a parallel unfitted-classifier
        # family (outputs Model, no data inputs) that voting/stacking
        # can consume — deferred to a separate design pass.
        #
        # NB: ``fit`` and ``predict`` method-shortcut ops are NO LONGER
        # in the catalog — they used to be first-class actions the
        # agent had to add + wire manually. Now each estimator/
        # transformer node carries (X_train, y_train, X_test) inputs
        # directly; the RL executor's inline expansion (see
        # rl/env/executor._inline_expand_ml_nodes) turns each class-
        # interface node into its init + fit + predict | transform×2
        # sub-DAG at run time. Users see the compact representation;
        # Dask sees the expanded one.
        # Metric ------------------------------------------------------------
        # y_pred's type is tightened to "Prediction" by the
        # label-shortcut guard; see seed_catalog_with_guards.
        #
        # Input ports keep their positional names (``"0"``, ``"1"``)
        # for the env's positional wiring convention, but carry
        # ``semantic_name`` so the suggestion layer can match
        # ``src.y_pred → dst.y_pred`` (the canonical edge) and rank
        # it above other type-compatible candidates.
        OperatorMeta(
            op_key="sklearn.metrics.accuracy_score",
            family="metric",
            task_tags=("classification",),
            inputs=(
                PortSpec("0", "Array", semantic_name="y_true"),
                PortSpec("1", "Array", semantic_name="y_pred"),
            ),
            outputs=(PortSpec("score", "Scalar"),),
            domain=DomainKind.SDF,
            determinism=DeterminismClass.DETERMINISTIC,
            operator_version="sklearn-1.7",
            max_occurrence=1,
        ),
    )


def catalog_by_key(catalog: tuple[OperatorMeta, ...]) -> dict[str, OperatorMeta]:
    return {op.op_key: op for op in catalog}


def seed_catalog_with_guards() -> tuple[OperatorMeta, ...]:
    """Return ``seed_catalog()`` with semantic-type guards applied.

    The guards tighten specific port types (e.g. metric.y_pred
    becomes "Prediction" instead of "Array") so classes of
    pipeline pathology are rejected at mask time rather than at
    execution time. See
    ``dorian/pipeline/semantic_type_guards.py`` for the
    registered guards + rationale.
    """
    from dorian.pipeline.semantic_type_guards import apply_guards
    return apply_guards(seed_catalog())
