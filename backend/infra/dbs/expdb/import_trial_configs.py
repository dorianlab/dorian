#!/usr/bin/env python3
"""
Import trial configurations from JSON and convert to DAG format in the
Postgres-backed document store (``per-collection doc_* tables``, pipelines collection).
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import asdict

from backend.config import config
from dorian.dag import DAG, Operator, Parameter, Edge


# Mapping of classifier choice codes to names
CLASSIFIER_MAP = {
    0: 'adaboost',
    1: 'bernoulli_nb',
    2: 'decision_tree',
    3: 'extra_trees',
    4: 'gaussian_nb',
    5: 'gradient_boosting',
    6: 'k_nearest_neighbors',
    7: 'lda',
    8: 'liblinear_svc',
    9: 'mlp',
    10: 'multinomial_nb',
    11: 'passive_aggressive',
    12: 'qda',
    13: 'random_forest',
    14: 'sgd'
}

# Mapping of feature preprocessor choice codes to names
FEATURE_PREPROCESSOR_MAP = {
    0: 'no_preprocessing',
    1: 'fast_ica',
    2: 'feature_agglomeration',
    3: 'kernel_pca',
    4: 'kitchen_sinks',
    5: 'nystroem_sampler',
    6: 'pca',
    7: 'polynomial',
    8: 'random_trees_embedding',
    9: 'select_percentile',
    10: 'select_rates',
    11: 'truncated_svd'
}

# Mapping of imputation strategy codes to names
IMPUTATION_STRATEGY_MAP = {
    0: 'mean',
    1: 'median',
    2: 'most_frequent'
}

# Mapping of rescaling choice codes to names
RESCALING_MAP = {
    0: 'minmax',
    1: 'normalize',
    2: 'quantile_transformer',
    3: 'robust_scaler',
    4: 'standardize',
    5: 'power_transformer',
    6: 'standardize'  # Default
}


def parse_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Parse configuration dictionary and extract key components."""
    parsed = {
        'classifier': None,
        'classifier_params': {},
        'feature_preprocessor': None,
        'feature_preprocessor_params': {},
        'imputation_strategy': 'mean',
        'rescaling': 'standardize'
    }
    
    for key, value in config.items():
        if key == 'classifier:__choice__':
            parsed['classifier'] = CLASSIFIER_MAP.get(value, f'unknown_{value}')
        elif key.startswith('classifier:') and ':' in key[11:]:
            # Extract classifier parameter
            parts = key.split(':')
            if len(parts) >= 3:
                param_name = ':'.join(parts[2:])
                parsed['classifier_params'][param_name] = value
        elif key == 'feature_preprocessor:__choice__':
            parsed['feature_preprocessor'] = FEATURE_PREPROCESSOR_MAP.get(value, 'no_preprocessing')
        elif key.startswith('feature_preprocessor:') and key != 'feature_preprocessor:__choice__':
            # Extract feature preprocessor parameter
            parts = key.split(':')
            if len(parts) >= 3:
                param_name = ':'.join(parts[2:])
                parsed['feature_preprocessor_params'][param_name] = value
        elif 'imputation:strategy' in key:
            parsed['imputation_strategy'] = IMPUTATION_STRATEGY_MAP.get(value, 'mean')
        elif 'rescaling:__choice__' in key:
            parsed['rescaling'] = RESCALING_MAP.get(value, 'standardize')
    
    return parsed


# auto-sklearn uses internal parameter name variants that differ from sklearn's API.
# This map translates them back to the canonical sklearn names.
_PARAM_NAME_ALIASES: Dict[str, str] = {
    'criterion_v2': 'criterion',
    'whiten_v2': 'whiten',
    'n_components_v2': 'n_components',
    'activation_v2': 'activation',
    'solver_v2': 'solver',
    'max_iter_v2': 'max_iter',
    # PCA: optuna.py uses 'keep_variance' (float fraction) instead of 'n_components_v2'
    'keep_variance': 'n_components',
}

# ---------------------------------------------------------------------------
# KB-driven parameter allowlists.
#
# Valid parameters are determined by the Knowledge Base — the single source
# of truth for operator metadata.  The seeder queries Neo4j via
# ``get_operator_parameters()`` (lru_cached for the process lifetime).
#
# For operators not yet in the KB, we fall back to Python introspection of
# the sklearn class's ``__init__`` signature.  This should be rare and means
# the KB needs updating.
# ---------------------------------------------------------------------------
_kb_param_cache: Dict[str, set | None] = {}   # FQN → {param_names} | None


def _kb_allowed_params(operator_fqn: str) -> set | None:
    """Return the set of valid parameter names for an operator from the KB.

    Falls back to introspection of the Python class when the KB has no
    parameter declarations.  Returns ``None`` only if both methods fail
    (accept-all fallback — defensive, should not happen for sklearn).
    """
    if operator_fqn in _kb_param_cache:
        return _kb_param_cache[operator_fqn]

    allowed: set | None = None
    try:
        from dorian.knowledge.queries import get_operator_parameters
        kb_params = get_operator_parameters(operator_fqn)
        if kb_params:
            allowed = {p["name"] for p in kb_params}
    except Exception:
        pass  # Neo4j not available (e.g. running outside Docker)

    # Fallback: introspect the actual Python class __init__ signature
    if not allowed:
        try:
            import importlib, inspect
            mod_path, cls_name = operator_fqn.rsplit(".", 1)
            cls = getattr(importlib.import_module(mod_path), cls_name)
            allowed = set(inspect.signature(cls.__init__).parameters.keys()) - {"self"}
        except Exception:
            allowed = None

    _kb_param_cache[operator_fqn] = allowed
    return allowed


# ---------------------------------------------------------------------------
# Per-classifier / per-preprocessor categorical decode tables.
#
# auto-sklearn stores CategoricalHyperparameter and OrdinalHyperparameter
# values as integer indices into the choices list.  These tables map
# canonical_param_name → [choice_0, choice_1, ...] in the enumeration order
# used by scripts/optuna.py's define_by_run().
# ---------------------------------------------------------------------------

_CLF_CATEGORICAL: Dict[str, Dict[str, list]] = {
    'mlp': {
        'activation':         ['tanh', 'relu'],
        'batch_size':         ['auto'],
        'beta_1':             [0.9],
        'beta_2':             [0.999],
        'early_stopping':     ['valid', 'train'],
        'epsilon':            [1e-08],
        'shuffle':            [True],
        'solver':             ['adam'],
        'tol':                [0.0001],
        'n_iter_no_change':   [32],
        'validation_fraction':[0.1],
    },
    'extra_trees': {
        'criterion':               ['gini', 'entropy', 'log_loss'],
        'bootstrap':               [True, False],
        'max_depth':               [None],
        'max_leaf_nodes':          [None],
        'min_impurity_decrease':   [0.0],
        'min_weight_fraction_leaf':[0.0],
    },
    'random_forest': {
        'criterion':               ['gini', 'entropy', 'log_loss'],
        'bootstrap':               [True, False],
        'max_depth':               [None],
        'max_leaf_nodes':          [None],
        'min_impurity_decrease':   [0.0],
        'min_weight_fraction_leaf':[0.0],
    },
    'decision_tree': {
        'criterion':               ['gini', 'entropy', 'log_loss'],
        'max_depth':               [None],
        'max_leaf_nodes':          [None],
        'min_impurity_decrease':   [0.0],
        'min_weight_fraction_leaf':[0.0],
    },
    'bernoulli_nb': {
        'fit_prior': [True, False],
    },
    'multinomial_nb': {
        'fit_prior': [True, False],
    },
    'passive_aggressive': {
        'average':       [False, True],
        'fit_intercept': [True],
        'loss':          ['hinge', 'squared_hinge'],
    },
    'k_nearest_neighbors': {
        'weights': ['uniform', 'distance'],
        'p':       [1, 2],
    },
    'lda': {
        'shrinkage': [None, 'auto', 'manual'],
    },
    'liblinear_svc': {
        'fit_intercept': [True],
        'loss':          ['squared_hinge'],
        'penalty':       ['l2'],
        'multi_class':   ['ovr'],
    },
    'sgd': {
        'average':       [False, True],
        'fit_intercept': [True],
        'learning_rate': ['optimal', 'invscaling', 'constant'],
    },
    'adaboost': {
        'algorithm': ['SAMME.R', 'SAMME'],
    },
}

_PREP_CATEGORICAL: Dict[str, Dict[str, list]] = {
    'fast_ica': {
        'algorithm': ['parallel', 'deflation'],
        'fun':       ['logcosh', 'exp', 'cube'],
        # whiten_v2 aliased to whiten; choices match optuna.py's suggest_categorical order
        'whiten':    [False, True, 'unit-variance', 'arbitrary-variance'],
    },
    'polynomial': {
        'include_bias':     [True, False],
        'interaction_only': [False, True],
    },
    'pca': {
        'whiten': [False, True],
    },
    'feature_agglomeration': {
        'linkage':       ['complete', 'average'],
        'pooling_func':  ['mean', 'median', 'max'],
    },
    'nystroem_sampler': {
        'kernel': ['poly', 'rbf', 'sigmoid', 'cosine'],
    },
    'kitchen_sinks': {},
    'select_percentile': {},
    'select_rates': {},
    'random_trees_embedding': {},
    'truncated_svd': {},
}


def _make_param(nodes, edges, gen_id, param_name, param_value, target_id):
    """Create a Parameter node and wire it as a kwarg to target_id."""
    # Normalise auto-sklearn internal alias → sklearn canonical name
    canonical = _PARAM_NAME_ALIASES.get(param_name, param_name)

    if param_value is None:
        # eval("eval")("None") → None  (Python None, not the string "None")
        dtype, value = 'eval', 'None'
    elif isinstance(param_value, bool):
        # Must check bool before int: isinstance(True, int) is True.
        # eval("eval")("True") → True  (Python bool, not the string "True")
        dtype, value = 'eval', str(param_value)
    elif isinstance(param_value, tuple):
        # Tuple values (e.g. hidden_layer_sizes) need eval() to reconstruct at call time.
        # Parameter.__call__ does eval(dtype)(value); eval("eval")("(64, 64)") → (64, 64).
        dtype, value = 'eval', repr(param_value)
    elif isinstance(param_value, int):
        dtype, value = 'int', str(param_value)
    elif isinstance(param_value, float):
        dtype, value = 'float', str(param_value)
    else:
        dtype, value = 'str', str(param_value)

    param_id = gen_id(f'p_{canonical}')
    nodes[param_id] = Parameter(name=canonical, dtype=dtype, value=value)
    edges.append(Edge(param_id, target_id, position=canonical))


_AUTO_SELECT_CODE = """\
def foo(df):
    \"\"\"Select all feature columns and last column as target.

    All columns are passed through so the downstream OrdinalEncoder can
    handle categorical features before the data reaches the ML pipeline.
    \"\"\"
    import numpy as np
    feature_cols = df.columns[:-1].tolist()
    target_col = df.columns[-1]
    X = df[feature_cols]
    y = df[target_col].to_numpy()
    return X, y
"""


def create_dag_from_config(parsed_config: Dict[str, Any], trial_id: int) -> DAG:
    """Create a DAG from parsed configuration.

    Data-flow model
    ---------------
    Dataset loading uses the ``dorian.io.dataset`` platform primitive:

        dorian.io.dataset  (expanded at runtime → Parameter(fpath) → loader)
          → Snippet: select all features + last column as target             → (X, y)
          → OrdinalEncoder.fit_transform(X)                                  → encoded_X
          → sklearn.model_selection.train_test_split(encoded_X, y)           → (X_train, X_test, y_train, y_test)

    The ML transformation pipeline follows:

        SimpleImputer.fit(X_train) → imputer
        imputer.transform(X_train) → imputed_X_train
        imputer.transform(X_test)  → imputed_X_test
        StandardScaler.fit(imputed_X_train) → scaler
        scaler.transform(imputed_X_train) → scaled_X_train
        scaler.transform(imputed_X_test)  → scaled_X_test
        [optional feature preprocessor].fit(scaled_X_train) → prep
        prep.transform(scaled_X_train) → prep_X_train
        prep.transform(scaled_X_test)  → prep_X_test
        clf.fit(prep_X_train, y_train) → fitted_clf
        fitted_clf.predict(prep_X_test) → y_pred
        accuracy_score(y_test, y_pred)

    ``dorian.io.dataset`` is the only platform-level node in the DAG.  The
    execution engine expands it before graph compilation (via
    ``DATASET_EXPANSION_RULE`` in ``dorian/pipeline/transforms.py``) into a
    concrete sub-chain (Parameter(fpath) → pandas.read_csv / read_excel / …)
    selected from the MIME type of the dataset uploaded via the sidebar.
    """
    from dorian.dag import Snippet as _Snippet  # local import to avoid circular at module level

    nodes = {}
    edges = []
    node_counter = 0

    def gen_id(name: str) -> str:
        nonlocal node_counter
        node_id = f"node_{name}_{node_counter}"
        node_counter += 1
        return node_id

    # ------------------------------------------------------------------ #
    # 0. Dataset reference                                                 #
    #    Expanded at execution time by DATASET_EXPANSION_RULE into:       #
    #        Parameter(fpath) → pandas.read_csv (or read_excel, …)       #
    #    based on the MIME type of the session-bound dataset.             #
    # ------------------------------------------------------------------ #
    dataset_id = gen_id('dataset')
    nodes[dataset_id] = Operator(name='dorian.io.dataset', language='python')

    # Auto-detect numeric features + last column as target → returns (X, y)
    auto_select_id = gen_id('auto_select')
    nodes[auto_select_id] = _Snippet(name='auto_select', code=_AUTO_SELECT_CODE, language='python')
    edges.append(Edge(dataset_id, auto_select_id, position=0))

    # ------------------------------------------------------------------ #
    # 0b. OrdinalEncoder  →  encode categorical features before splitting  #
    #     Compound expansion creates __init__ → fit → transform at runtime #
    # ------------------------------------------------------------------ #
    encoder_id = gen_id('encoder')
    nodes[encoder_id] = Operator(name="sklearn.preprocessing.OrdinalEncoder", language="python")
    _make_param(nodes, edges, gen_id, 'handle_unknown', 'use_encoded_value', encoder_id)
    _make_param(nodes, edges, gen_id, 'unknown_value', -1, encoder_id)
    edges.append(Edge(auto_select_id, encoder_id, position=0, output=0))  # X from auto_select

    # ------------------------------------------------------------------ #
    # 0c. sklearn.model_selection.train_test_split  →  (X_tr, X_te, y_tr, y_te) #
    # ------------------------------------------------------------------ #
    split_id = gen_id('train_test_split')
    nodes[split_id] = Operator(name='sklearn.model_selection.train_test_split', language='python')
    edges.append(Edge(encoder_id, split_id, position=0))                    # encoded X (compound expansion output)
    edges.append(Edge(auto_select_id, split_id, position=1, output=1))      # y  (output 1 of auto_select)

    # ------------------------------------------------------------------ #
    # 1. Imputer — compound expansion creates __init__ → fit → transform  #
    #    Multi-path: position 0 = X_train (fit+transform), position 1 =   #
    #    X_test (extra transform).  Output 0 = train, output 1 = test.    #
    # ------------------------------------------------------------------ #
    imputer_id = gen_id('imputer')
    nodes[imputer_id] = Operator(name="sklearn.impute.SimpleImputer", language="python")
    _make_param(nodes, edges, gen_id, 'strategy', parsed_config['imputation_strategy'], imputer_id)
    edges.append(Edge(split_id, imputer_id, position=0, output=0))          # X_train → fit+transform
    edges.append(Edge(split_id, imputer_id, position=1, output=1))          # X_test  → extra transform

    # ------------------------------------------------------------------ #
    # 2. Scaler                                                            #
    # ------------------------------------------------------------------ #
    scaler_id = gen_id('scaler')
    nodes[scaler_id] = Operator(name="sklearn.preprocessing.StandardScaler", language="python")
    edges.append(Edge(imputer_id, scaler_id, position=0, output=0))         # imputed X_train
    edges.append(Edge(imputer_id, scaler_id, position=1, output=1))         # imputed X_test

    last_stage_id = scaler_id

    # ------------------------------------------------------------------ #
    # 3. Feature Preprocessor (optional)                                   #
    # ------------------------------------------------------------------ #
    feature_prep = parsed_config['feature_preprocessor']
    if feature_prep and feature_prep != 'no_preprocessing':
        prep_class_map = {
            'fast_ica': 'sklearn.decomposition.FastICA',
            'feature_agglomeration': 'sklearn.cluster.FeatureAgglomeration',
            'kernel_pca': 'sklearn.decomposition.KernelPCA',
            'kitchen_sinks': 'sklearn.kernel_approximation.RBFSampler',
            'nystroem_sampler': 'sklearn.kernel_approximation.Nystroem',
            'pca': 'sklearn.decomposition.PCA',
            'polynomial': 'sklearn.preprocessing.PolynomialFeatures',
            'random_trees_embedding': 'sklearn.ensemble.RandomTreesEmbedding',
            'select_percentile': 'sklearn.feature_selection.SelectPercentile',
            'select_rates': 'sklearn.feature_selection.SelectKBest',
            'truncated_svd': 'sklearn.decomposition.TruncatedSVD',
        }

        prep_class = prep_class_map.get(feature_prep, f'sklearn.preprocessing.{feature_prep}')
        prep_id = gen_id('preprocessor')
        nodes[prep_id] = Operator(name=prep_class, language="python")

        # Only wire params declared in the KB for this operator.
        allowed = _kb_allowed_params(prep_class)
        prep_categorical = _PREP_CATEGORICAL.get(feature_prep, {})
        for param_name, param_value in parsed_config['feature_preprocessor_params'].items():
            canonical = _PARAM_NAME_ALIASES.get(param_name, param_name)
            if allowed is not None and canonical not in allowed:
                continue
            if not isinstance(param_value, bool) and isinstance(param_value, int):
                choices = prep_categorical.get(canonical)
                if choices is not None and param_value < len(choices):
                    param_value = choices[param_value]
            _make_param(nodes, edges, gen_id, param_name, param_value, prep_id)

        # Transformers consume positional ports — KB-driven compound
        # expansion routes position 0 to fit+transform, additional
        # positional edges to the extra-transform copy. Supervised
        # selectors (SelectKBest / SelectPercentile) bind y_train at
        # position 1, X_test at 2; unsupervised ones use X_train=0,
        # X_test=1.
        _SUPERVISED_SELECTORS = {
            'sklearn.feature_selection.SelectKBest',
            'sklearn.feature_selection.SelectPercentile',
        }
        if prep_class in _SUPERVISED_SELECTORS:
            edges.append(Edge(scaler_id, prep_id, position=0, output=0))         # scaled X_train
            edges.append(Edge(split_id, prep_id, position=1, output=2))          # y_train
            edges.append(Edge(scaler_id, prep_id, position=2, output=1))         # scaled X_test
        else:
            edges.append(Edge(scaler_id, prep_id, position=0, output=0))         # scaled X_train
            edges.append(Edge(scaler_id, prep_id, position=1, output=1))         # scaled X_test
        last_stage_id = prep_id

    # ------------------------------------------------------------------ #
    # 4. Classifier                                                        #
    #    Sklearn Estimator: fit(X_train, y_train) → predict(X_test)        #
    #    fit arity = 2 (X, y) → edges 0,1 go to fit; edge 2 = extra       #
    #    predict copy receiving X_test.                                     #
    # ------------------------------------------------------------------ #
    classifier_class_map = {
        'adaboost': 'sklearn.ensemble.AdaBoostClassifier',
        'bernoulli_nb': 'sklearn.naive_bayes.BernoulliNB',
        'decision_tree': 'sklearn.tree.DecisionTreeClassifier',
        'extra_trees': 'sklearn.ensemble.ExtraTreesClassifier',
        'gaussian_nb': 'sklearn.naive_bayes.GaussianNB',
        'gradient_boosting': 'sklearn.ensemble.GradientBoostingClassifier',
        'k_nearest_neighbors': 'sklearn.neighbors.KNeighborsClassifier',
        'lda': 'sklearn.discriminant_analysis.LinearDiscriminantAnalysis',
        'liblinear_svc': 'sklearn.svm.LinearSVC',
        'mlp': 'sklearn.neural_network.MLPClassifier',
        'multinomial_nb': 'sklearn.naive_bayes.MultinomialNB',
        'passive_aggressive': 'sklearn.linear_model.PassiveAggressiveClassifier',
        'qda': 'sklearn.discriminant_analysis.QuadraticDiscriminantAnalysis',
        'random_forest': 'sklearn.ensemble.RandomForestClassifier',
        'sgd': 'sklearn.linear_model.SGDClassifier',
    }

    clf_class = classifier_class_map.get(parsed_config['classifier'], f'sklearn.{parsed_config["classifier"]}')
    clf_id = gen_id('classifier')
    nodes[clf_id] = Operator(name=clf_class, language="python")

    # Pre-process classifier params
    clf_params = dict(parsed_config['classifier_params'])
    classifier_name = parsed_config['classifier']
    if classifier_name == 'mlp':
        depth = clf_params.pop('hidden_layer_depth', None)
        width = clf_params.pop('num_nodes_per_layer', None)
        if depth is not None and width is not None:
            clf_params['hidden_layer_sizes'] = tuple([int(width)] * int(depth))

    clf_categorical = _CLF_CATEGORICAL.get(classifier_name, {})

    decoded_clf: Dict[str, Any] = {}
    for param_name, param_value in clf_params.items():
        canonical = _PARAM_NAME_ALIASES.get(param_name, param_name)
        if not isinstance(param_value, bool) and isinstance(param_value, int):
            choices = clf_categorical.get(canonical)
            if choices is not None and param_value < len(choices):
                param_value = choices[param_value]
        decoded_clf[canonical] = param_value

    skip_clf: set = set()
    if classifier_name == 'mlp':
        es = decoded_clf.get('early_stopping')
        if es == 'valid':
            decoded_clf['early_stopping'] = True
        elif es == 'train':
            decoded_clf['early_stopping'] = False
            skip_clf.add('validation_fraction')

    # Only wire params declared in the KB for this operator.
    clf_param_allowlist = _kb_allowed_params(clf_class)

    for canonical, param_value in decoded_clf.items():
        if canonical in skip_clf:
            continue
        if clf_param_allowlist is not None and canonical not in clf_param_allowlist:
            continue
        _make_param(nodes, edges, gen_id, canonical, param_value, clf_id)

    # Classifier (Sklearn Estimator interface) — KB declares X@0 and
    # y@1 for fit, X_test@2 for the extra-predict copy.
    edges.append(Edge(last_stage_id, clf_id, position=0, output=0))         # preprocessed X_train
    edges.append(Edge(split_id, clf_id,      position=1, output=2))         # y_train (output 2 of split)
    edges.append(Edge(last_stage_id, clf_id, position=2, output=1))         # preprocessed X_test

    # ------------------------------------------------------------------ #
    # 5. Evaluate — accuracy_score(y_true, y_pred)                         #
    #    Classifier output 0 = primary predict (on X_train, less useful)   #
    #    Classifier output 1 = extra predict (on X_test) — what we need    #
    # ------------------------------------------------------------------ #
    evaluate_id = gen_id('evaluate')
    nodes[evaluate_id] = Operator(name="sklearn.metrics.accuracy_score", language="python")
    edges.append(Edge(split_id, evaluate_id, position=0, output=3))         # y_true (y_test from split)
    edges.append(Edge(clf_id, evaluate_id, position=1, output=1))           # y_pred (extra predict on X_test)

    return DAG(nodes=nodes, edges=edges)


def dag_to_document(dag: DAG, pipeline_id: str) -> Dict:
    """Convert DAG to a storage-ready document (same shape as before)."""
    nodes_dict = {}
    for node_id, node in dag.nodes.items():
        node_dict = {
            'type': node.__class__.__name__,
            **asdict(node)
        }
        nodes_dict[node_id] = node_dict

    edges_list = [asdict(edge) for edge in dag.edges]

    return {
        'pipeline_id': pipeline_id,
        'nodes': nodes_dict,
        'edges': edges_list,
    }


async def _seed_relational_pipelines(pipeline_docs: list[Dict]) -> int:
    """Populate the relational ``pipelines`` table (ExperimentStore / BK-Tree
    backing store) from the same DAGs we just wrote into the document store.

    The document store is the source of truth for the UI; the relational
    table is what ``ExperimentStore.load_from_db`` reads to build the
    in-memory BK-Tree and what ``record_evaluation`` FK-joins against.
    Without this second write the BK-Tree boots empty, memory-based
    policies have no seed pipelines, and ``record_evaluation`` refuses
    to link RL results to seeded pipelines.
    """
    from backend.envs import get_pg_pool
    from dorian.experiment.schema import create_schema
    from dorian.experiment.similarity import extract_operator_names

    pool = await get_pg_pool()
    # Idempotent: creates tables only if missing.
    await create_schema(pool)

    rows: list[tuple[str, str, str, str, list[str], str]] = []
    session = "trial-config-seed"
    task = "classification"
    provenance = "trial-config"
    for doc in pipeline_docs:
        pipeline_id = doc["pipeline_id"]
        dag_json = {"nodes": doc["nodes"], "edges": doc["edges"]}
        ops = extract_operator_names(dag_json)
        rows.append(
            (pipeline_id, session, task, json.dumps(dag_json), ops, provenance)
        )

    if not rows:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO pipelines (id, session, task, dag, operators, provenance)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )

    return len(rows)


async def import_trial_configs():
    """Import trial configurations from JSON into the pipelines collection."""
    from backend.db import get_pg_db

    print("=" * 80)
    print("Importing Trial Configurations to Postgres (pipelines collection)")
    print("=" * 80)

    # Load JSON file
    json_file = Path(__file__).parent / 'successful_trials_500.json'
    print(f"\n1. Loading configurations from {json_file.name}...")

    with open(json_file, 'r') as f:
        trials = json.load(f)

    print(f"   [ok] Loaded {len(trials)} trial configurations")

    # Connect to Postgres via the facade
    print("\n2. Connecting to Postgres...")
    db = await get_pg_db()
    print(f"   [ok] Connected")

    # Skip if already seeded (survives compose down/up)
    force = os.environ.get("FORCE_SEED", "").strip() == "1"
    existing = await db.pipelines.count_documents({})
    if existing and not force:
        print(
            f"\n   Postgres already contains pipelines ({existing}) — "
            "skipping seed (set FORCE_SEED=1 to re-seed)"
        )
        return

    # Clear existing pipelines
    print("\n3. Clearing existing pipelines...")
    result = await db.pipelines.delete_many({})
    print(f"   [ok] Deleted {result.deleted_count} existing pipelines")

    # Process each trial
    print(f"\n4. Converting {len(trials)} trials to DAG format...")
    pipeline_docs: list[Dict] = []

    # Track statistics
    classifier_counts: Dict[str, int] = {}
    feature_prep_counts: Dict[str, int] = {}

    for i, trial in enumerate(trials, 1):
        if i % 50 == 0:
            print(f"   Processing trial {i}/{len(trials)}...")

        trial_id = trial['trial_id']
        trial_cfg = trial['configurations']

        parsed = parse_config(trial_cfg)

        classifier_counts[parsed['classifier']] = classifier_counts.get(parsed['classifier'], 0) + 1
        feature_prep_counts[parsed['feature_preprocessor']] = feature_prep_counts.get(parsed['feature_preprocessor'], 0) + 1

        dag = create_dag_from_config(parsed, trial_id)
        pipeline_id = str(uuid.uuid4())

        doc = dag_to_document(dag, pipeline_id)
        pipeline_docs.append(doc)

    print(f"   [ok] Converted {len(pipeline_docs)} configurations to DAG format")

    # Insert
    print(f"\n5. Storing {len(pipeline_docs)} pipelines in Postgres...")
    if pipeline_docs:
        result = await db.pipelines.insert_many(pipeline_docs)
        print(f"   [ok] Inserted {len(result.inserted_ids)} pipelines")

    # Also seed the relational ``pipelines`` table that the ExperimentStore's
    # BK-Tree loads from. Without this step the BK-Tree boots empty and
    # RL rollouts can't be linked to seeded pipelines via evaluation FKs.
    print(f"\n5b. Seeding relational pipelines table (BK-Tree backing store)...")
    try:
        inserted = await _seed_relational_pipelines(pipeline_docs)
        print(f"   [ok] Upserted {inserted} rows into Postgres.pipelines")
    except Exception as exc:
        print(f"   [warn] relational seed skipped: {exc}")

    # Statistics
    print(f"\n6. Summary:")
    print(f"   Total pipelines: {len(pipeline_docs)}")
    print(f"   Unique classifiers: {len(classifier_counts)}")
    print(f"   Unique feature preprocessors: {len(feature_prep_counts)}")

    print(f"\n   Classifier distribution:")
    for clf, count in sorted(classifier_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"      {clf}: {count}")

    print(f"\n   Feature preprocessor distribution:")
    for prep, count in sorted(feature_prep_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"      {prep}: {count}")

    # Show sample
    print(f"\n7. Sample pipeline (first trial):")
    sample = await db.pipelines.find_one()
    if sample:
        param_count = sum(1 for n in sample['nodes'].values() if n['type'] == 'Parameter')
        operator_count = sum(1 for n in sample['nodes'].values() if n['type'] == 'Operator')
        print(f"   Pipeline ID: {sample['pipeline_id']}")
        print(f"   Total nodes: {len(sample['nodes'])}")
        print(f"   Parameters: {param_count}")
        print(f"   Operators: {operator_count}")

        print(f"\n   Sample parameters:")
        count = 0
        for node_id, node_data in sample['nodes'].items():
            if node_data['type'] == 'Parameter' and count < 5:
                print(f"      {node_data['name']} ({node_data['dtype']}) = {node_data['value']}")
                count += 1

    print("\n" + "=" * 80)
    print("[ok] Import completed successfully!")
    print("=" * 80)


async def main() -> None:
    """Async entry point consumed by ``backend.infra.bootstrap``."""
    await import_trial_configs()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
