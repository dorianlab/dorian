"""
dorian/knowledge/execution_errors.py
----------------------------------------------
Registry of known execution error patterns and their mitigation actions.

Each entry maps a regex pattern (matched against the exception message or
traceback) to a parameter-change fix that can be applied as a pipeline
rewrite.  This is the KB layer — the handler in
``dorian.event.handlers.execution_error_handler`` consumes this registry
at runtime.

Design:
  - ``pattern``: compiled regex applied to ``str(exc)`` (the error message).
    Named groups capture dynamic values needed by the fix function.
  - ``operators``: set of operator FQNs that this pattern applies to.
    Empty set means "any operator".
  - ``param_name``: the Parameter node name whose value should be changed.
  - ``fix_value``: either a static replacement or a callable
    ``(match, error_msg) -> str`` that computes the new value from regex
    groups.
  - ``description``: human-readable explanation shown in the suggestion card.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class ExecutionErrorPattern:
    """A known execution error pattern with its mitigation.

    Two fix types are supported:

    ``parameter_change`` (the historical default)
        A single Parameter node's value is updated to ``fix_value``. The
        parameter is identified by ``param_name``; if it does not exist on
        the failing operator, the fix is skipped.

    ``structural_rewrite``
        A named mitigation rewrite is looked up in ``expdb.rewrites``
        collection by ``mitigation_slug`` and applied to the failing
        operator. This unlocks fixes that cannot be expressed as a single
        parameter change — e.g. *insert an OrdinalEncoder before the
        estimator when its training set contains string features*.

    The class stays ``frozen``: new fields carry safe defaults so existing
    patterns remain valid without edits.
    """

    id: str
    pattern: re.Pattern[str]
    operators: frozenset[str]
    param_name: str
    fix_value: str | Callable[[re.Match, str], str]
    risk_name: str
    description_short: str
    description_long: str
    # New (optional) fields — default preserves parameter-change semantics.
    fix_type: str = "parameter_change"
    mitigation_slug: str | None = None

    def compute_fix(self, match: re.Match, error_msg: str) -> str:
        """Return the concrete parameter value to apply."""
        if callable(self.fix_value):
            return self.fix_value(match, error_msg)
        return self.fix_value


def _pca_n_components_fix(match: re.Match, _error_msg: str) -> str:
    """Compute the corrected n_components from the captured max value."""
    max_val = int(match.group("max_val"))
    # Use the max allowed value (conservative — preserves most variance)
    return str(max_val)


# ═══════════════════════════════════════════════════════════════════════════
# Pattern registry
# ═══════════════════════════════════════════════════════════════════════════

EXECUTION_ERROR_PATTERNS: list[ExecutionErrorPattern] = [
    # ── PCA / KernelPCA n_components exceeds data dimensions ──────────────
    ExecutionErrorPattern(
        id="pca_n_components_too_large",
        pattern=re.compile(
            r"n_components=(?P<requested>\d+) must be between "
            r"(?:0|1) and min\(n_samples, n_features\)=(?P<max_val>\d+)"
        ),
        operators=frozenset({
            "sklearn.decomposition.PCA",
            "sklearn.decomposition.KernelPCA",
            "sklearn.decomposition.IncrementalPCA",
            "sklearn.decomposition.SparsePCA",
            "sklearn.decomposition.TruncatedSVD",
        }),
        param_name="n_components",
        fix_value=_pca_n_components_fix,
        risk_name="Parameter Mismatch",
        description_short=(
            "n_components ({requested}) exceeds the data dimensions ({max_val}). "
            "Reduce to {max_val}."
        ),
        description_long=(
            "The PCA operator requested {requested} components, but the input "
            "data has at most {max_val} usable dimensions "
            "(min of n_samples and n_features). The fix reduces n_components "
            "to {max_val} so that PCA can fit without error. Consider tuning "
            "this value further after reviewing the explained variance ratio."
        ),
    ),

    # ── Train/test size too small ─────────────────────────────────────────
    ExecutionErrorPattern(
        id="train_test_split_too_few_samples",
        pattern=re.compile(
            r"The (?:test_size|train_size) is not compatible with "
            r".*?resulting (?:train|test) set will be empty"
        ),
        operators=frozenset({
            "sklearn.model_selection.train_test_split",
        }),
        param_name="test_size",
        fix_value="0.2",
        risk_name="Parameter Mismatch",
        description_short=(
            "Train/test split produced an empty set. Reset test_size to 0.2."
        ),
        description_long=(
            "The current train/test split configuration produces an empty "
            "training or testing set, likely because the dataset is too small "
            "for the requested ratio. Resetting test_size to 0.2 (80/20 split) "
            "ensures both sets contain data."
        ),
    ),

    # ── n_neighbors exceeds n_samples ─────────────────────────────────────
    ExecutionErrorPattern(
        id="knn_n_neighbors_too_large",
        pattern=re.compile(
            r"Expected n_neighbors <= n_samples_fit,\s*"
            r"but n_neighbors\s*=\s*(?P<requested>\d+),\s*"
            r"n_samples_fit\s*=\s*(?P<n_samples>\d+)"
        ),
        operators=frozenset({
            "sklearn.neighbors.KNeighborsClassifier",
            "sklearn.neighbors.KNeighborsRegressor",
        }),
        param_name="n_neighbors",
        fix_value=lambda m, _: str(max(1, int(m.group("n_samples")) // 5)),
        risk_name="Parameter Mismatch",
        description_short=(
            "n_neighbors ({requested}) exceeds training set size ({n_samples}). "
            "Reduce to {fix_value}."
        ),
        description_long=(
            "The K-Neighbors estimator was configured with {requested} "
            "neighbors, but the training set only has {n_samples} samples. "
            "The fix sets n_neighbors to n_samples // 5 as a reasonable "
            "default."
        ),
    ),

    # ── Categorical features reach numeric estimator without encoding ─────
    # Triggered by sklearn classifiers/regressors that call np.asarray(X,
    # dtype=float) internally. The observed message is
    # "could not convert string to float: 'Casual'" (or similar). The fix
    # is structural: insert an OrdinalEncoder before the failing node so
    # string columns become integers before they hit the estimator.
    ExecutionErrorPattern(
        id="string_to_float_needs_encoder",
        pattern=re.compile(
            r"could not convert string to float:\s*['\"]?(?P<sample>[^'\"\\n]+)['\"]?"
        ),
        # Empty = applies to any operator. The handler constrains by
        # "operator fails at ``fit``/``transform`` on string data", which is
        # already implied by the error message.
        operators=frozenset(),
        param_name="",  # unused by structural rewrites
        fix_value="",
        risk_name="Missing Categorical Encoding",
        description_short=(
            "{sample!r} — operator expected numeric input but received "
            "categorical strings. Insert an OrdinalEncoder before this node."
        ),
        description_long=(
            "This operator only accepts numeric feature matrices, but its "
            "training data contains categorical string values (first offending "
            "value: {sample!r}). The mitigation inserts a "
            "``sklearn.preprocessing.OrdinalEncoder`` upstream of this node "
            "so that all feature columns are encoded to integers before "
            "reaching the estimator / transformer. ``handle_unknown='use_encoded_value'`` "
            "and ``unknown_value=-1`` are set so that test-time categories "
            "not seen during training do not raise."
        ),
        fix_type="structural_rewrite",
        mitigation_slug="insert-ordinal-encoder-before",
    ),

    # ── LDA / QDA: shrinkage incompatible with svd solver ────────────────
    ExecutionErrorPattern(
        id="lda_shrinkage_requires_lsqr_or_eigen",
        pattern=re.compile(
            r"shrinkage not supported with ['\"]?svd['\"]? solver"
        ),
        operators=frozenset({
            "sklearn.discriminant_analysis.LinearDiscriminantAnalysis",
        }),
        param_name="solver",
        fix_value="lsqr",
        risk_name="Parameter Mismatch",
        description_short=(
            "LDA shrinkage is not supported with the 'svd' solver. "
            "Switch solver to 'lsqr'."
        ),
        description_long=(
            "``LinearDiscriminantAnalysis`` was configured with a non-null "
            "``shrinkage`` value but its default ``solver='svd'`` rejects "
            "shrinkage entirely. Swapping the solver to ``'lsqr'`` keeps "
            "the requested shrinkage and is the most common production "
            "choice; ``'eigen'`` is the other supported alternative."
        ),
    ),

    # ── LinearSVC: penalty='l2' + loss='hinge' require dual=True ─────────
    ExecutionErrorPattern(
        id="linearsvc_l2_hinge_requires_dual_true",
        pattern=re.compile(
            r"penalty=['\"]?l2['\"]?\s+and\s+loss=['\"]?hinge['\"]?\s+"
            r"are not supported when\s+dual=False",
            re.IGNORECASE,
        ),
        operators=frozenset({
            "sklearn.svm.LinearSVC",
        }),
        param_name="dual",
        fix_value="True",
        risk_name="Parameter Mismatch",
        description_short=(
            "LinearSVC with penalty='l2' + loss='hinge' requires dual=True."
        ),
        description_long=(
            "``LinearSVC`` rejects ``dual=False`` when combined with "
            "``penalty='l2'`` and ``loss='hinge'`` — the dual formulation is "
            "the only one sklearn implements for that objective. Flipping "
            "``dual`` back to ``True`` restores training; "
            "``penalty='l1'`` or ``loss='squared_hinge'`` would be the other "
            "valid alternatives if the primal form is specifically needed."
        ),
    ),

    # ── max_features exceeds n_features ───────────────────────────────────
    ExecutionErrorPattern(
        id="rf_max_features_too_large",
        pattern=re.compile(
            r"max_features must be in \(0, n_features\].*"
            r"Got max_features=(?P<requested>\d+).*"
            r"n_features is (?P<n_features>\d+)",
            re.DOTALL,
        ),
        operators=frozenset({
            "sklearn.ensemble.RandomForestClassifier",
            "sklearn.ensemble.RandomForestRegressor",
            "sklearn.ensemble.ExtraTreesClassifier",
            "sklearn.ensemble.ExtraTreesRegressor",
        }),
        param_name="max_features",
        fix_value=lambda m, _: str(int(m.group("n_features"))),
        risk_name="Parameter Mismatch",
        description_short=(
            "max_features ({requested}) exceeds available features ({n_features}). "
            "Reduce to {n_features}."
        ),
        description_long=(
            "The ensemble estimator's max_features parameter ({requested}) "
            "exceeds the number of input features ({n_features}). "
            "The fix sets max_features to n_features."
        ),
    ),

    # ── FastICA whiten=True rejected by sklearn ≥1.3 ─────────────────────
    # auto-sklearn / FLAML enumerate ``whiten=True`` as a categorical
    # option; sklearn ≥1.3 dropped support and now requires False or
    # one of 'unit-variance' / 'arbitrary-variance'. Surfaced as a
    # parameter_change suggestion (single-Parameter fix; no DAG
    # structural change).
    ExecutionErrorPattern(
        id="fastica_whiten_true_rejected",
        pattern=re.compile(
            r"The 'whiten' parameter of FastICA must be a str.*Got True"
        ),
        operators=frozenset({"sklearn.decomposition.FastICA"}),
        param_name="whiten",
        fix_value="unit-variance",
        risk_name="Parameter Mismatch",
        description_short=(
            "FastICA whiten=True is rejected by sklearn ≥1.3. "
            "Switch to 'unit-variance'."
        ),
        description_long=(
            "``FastICA`` was configured with ``whiten=True``, but sklearn "
            "≥1.3 only accepts ``False`` or ``'unit-variance'`` / "
            "``'arbitrary-variance'`` for this parameter. The fix swaps "
            "the value to ``'unit-variance'`` (variance normalised to 1, "
            "the closest behavioural equivalent to the legacy True)."
        ),
    ),

    # ── SGDClassifier penalty enumerated as int ──────────────────────────
    # auto-sklearn ships ``penalty=<int>`` (0=l1, 1=l2, 2=elasticnet);
    # sklearn rejects non-string penalty. parameter_change to 'l2'.
    ExecutionErrorPattern(
        id="sgd_penalty_int_rejected",
        pattern=re.compile(
            r"The 'penalty' parameter of SGDClassifier must be a str"
        ),
        operators=frozenset({"sklearn.linear_model.SGDClassifier"}),
        param_name="penalty",
        fix_value="l2",
        risk_name="Parameter Mismatch",
        description_short=(
            "SGDClassifier penalty must be a string. Set to 'l2'."
        ),
        description_long=(
            "auto-sklearn ships the penalty option as an int index "
            "(0=l1, 1=l2, 2=elasticnet); sklearn requires a string. "
            "The fix sets it to 'l2' (the most common default), which "
            "validates without changing the regularisation family."
        ),
    ),

    # ── score_func not callable (Select* selectors) ──────────────────────
    # auto-sklearn enumerates score_func as 0/1/2 → chi2 / f_classif /
    # mutual_info_classif. sklearn rejects non-callable. The fix swaps
    # the Parameter for a Snippet that imports + returns the function.
    ExecutionErrorPattern(
        id="score_func_not_callable",
        pattern=re.compile(
            r"The 'score_func' parameter of \w+ must be a callable"
        ),
        operators=frozenset({
            "sklearn.feature_selection.SelectKBest",
            "sklearn.feature_selection.SelectPercentile",
            "sklearn.feature_selection.SelectFdr",
            "sklearn.feature_selection.SelectFpr",
            "sklearn.feature_selection.SelectFwe",
            "sklearn.feature_selection.GenericUnivariateSelect",
        }),
        param_name="score_func",
        fix_value="",
        fix_type="structural_rewrite",
        mitigation_slug="fix-score-func-callable",
        risk_name="Parameter Type Mismatch",
        description_short=(
            "score_func must be a callable, not an int / string. "
            "Replace the Parameter with a Snippet that imports + "
            "returns the scoring function."
        ),
        description_long=(
            "sklearn's ``Select*`` selectors require ``score_func`` to "
            "be a callable (function reference). auto-sklearn / FLAML "
            "ship the option as an int index or a string alias, neither "
            "of which sklearn's parameter validator accepts. The "
            "mitigation rewrite ``fix-score-func-callable`` replaces "
            "the Parameter satellite with a Snippet whose body is "
            "``def foo(): from sklearn.feature_selection import "
            "<fn>; return <fn>`` — keyed on the original Parameter "
            "value (0 → chi2, 1 → f_classif, 2 → mutual_info_classif)."
        ),
    ),

    # ── pooling_func not callable (FeatureAgglomeration) ─────────────────
    ExecutionErrorPattern(
        id="pooling_func_not_callable",
        pattern=re.compile(
            r"The 'pooling_func' parameter of FeatureAgglomeration "
            r"must be a callable"
        ),
        operators=frozenset({"sklearn.cluster.FeatureAgglomeration"}),
        param_name="pooling_func",
        fix_value="",
        fix_type="structural_rewrite",
        mitigation_slug="fix-pooling-func-callable",
        risk_name="Parameter Type Mismatch",
        description_short=(
            "pooling_func must be a callable. Replace the string alias "
            "with a Snippet returning the matching numpy reducer."
        ),
        description_long=(
            "``FeatureAgglomeration`` requires ``pooling_func`` to be "
            "a callable. auto-sklearn ships the option as a string "
            "alias (``'mean'`` / ``'median'`` / ``'max'``); sklearn "
            "rejects strings. The mitigation rewrite "
            "``fix-pooling-func-callable`` replaces the Parameter "
            "with a Snippet that imports and returns the matching "
            "``numpy`` reducer (mean → numpy.mean, etc.)."
        ),
    ),

    # ── Sparse data passed where dense is required ───────────────────────
    # OneHotEncoder, CountVectorizer, RBFSampler emit sparse matrices;
    # downstream MLPClassifier / GaussianNB / QDA reject them. Splice a
    # ``.toarray()`` Snippet on the X edge.
    ExecutionErrorPattern(
        id="sparse_data_dense_required",
        pattern=re.compile(
            r"Sparse data was passed for X, but dense data is required"
        ),
        operators=frozenset(),  # any consumer that rejects sparse
        param_name="",
        fix_value="",
        fix_type="structural_rewrite",
        mitigation_slug="insert-dense-converter-before",
        risk_name="Sparse / Dense Mismatch",
        description_short=(
            "Upstream encoder produced sparse data but this operator "
            "requires dense. Insert a ``.toarray()`` converter."
        ),
        description_long=(
            "The failing operator (e.g. ``MLPClassifier``, ``GaussianNB``, "
            "``QuadraticDiscriminantAnalysis``) requires a dense numpy "
            "ndarray, but its X input is a sparse matrix produced by "
            "an upstream ``OneHotEncoder`` / ``CountVectorizer`` / "
            "``RBFSampler``. The mitigation rewrite "
            "``insert-dense-converter-before`` splices a Snippet "
            "``def foo(x): return x.toarray() if hasattr(x, "
            "'toarray') else x`` on the X edge — the consumer's "
            "kwarg position is preserved and the rest of the "
            "pipeline shape stays unchanged."
        ),
    ),

    # ── LightGBM rejects string columns (no implicit categorical encoder) ────
    # FLAML imports ship the bare estimator without TabularPredictor's
    # implicit OrdinalEncoder pre-step, so any dataset with string
    # features (V2: object, A1: object, ...) trips LightGBM's dtype
    # check before fit ever sees the data. The mitigation reuses the
    # already-curated ``insert-ordinal-encoder-before`` rewrite — same
    # remedy as the sklearn ``could not convert string to float`` path.
    ExecutionErrorPattern(
        id="lightgbm_string_features_rejected",
        pattern=re.compile(
            r"pandas dtypes must be int, float or bool"
        ),
        operators=frozenset(),  # any consumer that rejects object dtypes
        param_name="",
        fix_value="",
        fix_type="structural_rewrite",
        mitigation_slug="insert-ordinal-encoder-before",
        risk_name="Missing Categorical Encoding",
        description_short=(
            "LightGBM rejects object columns. Insert an OrdinalEncoder "
            "before this node."
        ),
        description_long=(
            "LightGBM's pandas-dtype gate refuses any column that isn't "
            "``int``, ``float`` or ``bool``; the failing pipeline ships "
            "string features through to fit because the FLAML import / "
            "trial-config seed dropped FLAML's implicit "
            "``TabularPredictor`` preprocessing layer. The mitigation "
            "rewrite ``insert-ordinal-encoder-before`` splices a "
            "``sklearn.preprocessing.OrdinalEncoder`` upstream of this "
            "node so every feature column is encoded to integers before "
            "reaching the classifier. ``handle_unknown='use_encoded_value'`` "
            "/ ``unknown_value=-1`` are set so test-time categories not "
            "seen during training don't raise."
        ),
    ),

    # ── Classifier rejects non-zero-indexed / string label vectors ────────
    # Sklearn classifiers (and LightGBM via its sklearn API) want
    # ``y`` to be a 0-indexed integer array. FLAML internally
    # label-encodes y as part of TabularPredictor; the extracted
    # pipeline doesn't, so any dataset whose target is strings
    # (``['bad','good']``) or 1-indexed ints (``[1,2,3]``) fails fit
    # with ``Invalid classes inferred from unique values of `y`.
    # Expected: [0 1 ...], got [...]``. Fix: insert a Snippet that
    # runs ``pd.Categorical(y).codes`` on the y-source so every
    # downstream consumer (fit, accuracy_score) sees the same
    # integer encoding.
    ExecutionErrorPattern(
        id="classifier_y_label_encoding_required",
        pattern=re.compile(
            r"Invalid classes inferred from unique values of `y`"
        ),
        operators=frozenset(),  # any classifier with string / non-zero-indexed labels
        param_name="",
        fix_value="",
        fix_type="structural_rewrite",
        mitigation_slug="insert-label-encoder-before",
        risk_name="Missing Label Encoding",
        description_short=(
            "Classifier expected 0-indexed integer labels but received "
            "strings or 1-indexed values. Insert a label encoder."
        ),
        description_long=(
            "The classifier's fit method requires ``y`` to be a "
            "0-indexed integer array; the failing pipeline ships "
            "string targets (``['bad','good']``) or 1-indexed integers "
            "(``[1,2,3]``) because the FLAML / trial-config seeder "
            "dropped FLAML's implicit ``TabularPredictor`` label "
            "encoding. The mitigation rewrite "
            "``insert-label-encoder-before`` finds the y-edge feeding "
            "this classifier, walks back to the y-source node, and "
            "splices a ``pd.Categorical(y).codes`` Snippet so EVERY "
            "downstream consumer of that source (fit AND the metric) "
            "sees the same 0-indexed integer encoding. The encoding "
            "is alphabetical-by-default (deterministic across train / "
            "test slices)."
        ),
    ),

    # ── Method node missing data input (validator-caught wiring gap) ──────
    # Emitted by ``_validate_pipeline`` after compound expansion when a
    # fit / predict / transform method has only its instance-chain edge
    # and no external data edge — the method would raise
    # ``missing 1 required positional argument: 'X'`` / ``'y'`` at
    # execution. See dorian/pipeline/dag_analysis.py.
    #
    # There's no single-parameter fix, and no one-size structural rewrite
    # that can safely wire the missing input without knowing the upstream
    # source. The suggestion therefore carries ``fix_type='manual'`` —
    # the debugger surfaces the node and method, but the user (or an
    # outer scheduler, like the RL error-learning mask) picks the real
    # remedy. For RL: the failing operator signature feeds
    # ``error_learning.invalid_ops_for_dataset`` and gets masked from
    # future episodes on the same dataset. For HITL: the user sees the
    # specific node and rewires the canvas, or regenerates the pipeline.
    ExecutionErrorPattern(
        id="method_node_missing_data_input",
        pattern=re.compile(
            r"Method node '(?P<node_id>[^']+)' \('(?P<method>[^']+)'\) "
            r"has no data input"
        ),
        operators=frozenset(),  # any — the method is whatever's failing
        param_name="",
        fix_value="",
        fix_type="manual",
        mitigation_slug=None,
        risk_name="Wiring Gap",
        description_short=(
            "Method '{method}' on node '{node_id}' has no data input — "
            "check upstream wiring."
        ),
        description_long=(
            "The compound-expanded method node '{node_id}' (method "
            "'{method}') has only its instance-chain edge (position 0) "
            "and no external data edge. Running the pipeline would "
            "raise \"{method}() missing 1 required positional argument\" "
            "at the Dask worker.\n\n"
            "Common causes:\n"
            "  * A preceding mitigation rewrite rerouted the feature "
            "flow through a transformer but left the estimator's "
            "predict.X_test or fit.y dangling.\n"
            "  * The RL agent placed an operator whose output dtype "
            "doesn't match what this method expects.\n"
            "  * The canvas user didn't connect an input handle.\n\n"
            "No automatic parameter fix is safe here — the right "
            "source edge depends on pipeline intent. In the RL loop "
            "the failing operator signature is recorded in the error "
            "corpus (``execution_error_instances``) and "
            "``error_learning.invalid_ops_for_dataset`` will mask the "
            "offending operator from future episodes on this dataset "
            "until a working combination is found."
        ),
    ),
]

# Quick-lookup index by pattern id
PATTERN_INDEX: dict[str, ExecutionErrorPattern] = {p.id: p for p in EXECUTION_ERROR_PATTERNS}
