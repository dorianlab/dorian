import json
from time import sleep
from backend.events import Event, emit
from backend.envs import redis
from dorian.infra.keys import STREAM_MAXLEN


def _build_column_table_defaults(columns, col_profiles):
    """Build auto-prefilled defaults from column profiles for column-table questions."""
    if not col_profiles:
        return {}, {}, {}, {}, {}

    range_defaults = {}
    format_defaults = {}
    precision_defaults = {}
    allowed_defaults = {}
    compliance_defaults = {}

    for col in columns:
        p = col_profiles.get(col)
        if not p:
            continue

        # Range: prefill [min, max] for numeric columns
        if p.get("is_numeric") and p.get("min") is not None and p.get("max") is not None:
            range_defaults[f"{col}:range"] = [p["min"], p["max"]]

        # Format: prefill inferred_type
        format_defaults[f"{col}:expected_type"] = p.get("inferred_type", "str")

        # Precision: prefill 0 for numeric columns (user adjusts)
        if p.get("is_numeric"):
            precision_defaults[f"{col}:decimals"] = 0

        # Allowed values: prefill sample_values for categorical/binary columns
        if p.get("scale") in ("categorical", "binary") and p.get("sample_values"):
            allowed_defaults[f"{col}:allowed_values"] = [
                str(v) for v in p["sample_values"] if v is not None
            ]

        # Compliance: prefill a sensible rule from profile
        if p.get("is_numeric") and p.get("min") is not None:
            compliance_defaults[f"{col}:rule"] = {
                "op": "between", "value": [p["min"], p["max"]],
            }
        elif p.get("scale") in ("categorical", "binary") and p.get("sample_values"):
            compliance_defaults[f"{col}:rule"] = {
                "op": "in", "value": [str(v) for v in p["sample_values"] if v is not None],
            }

    return range_defaults, format_defaults, precision_defaults, allowed_defaults, compliance_defaults


def _dataset_setup_questions(did, columns, col_profiles=None):
    (
        range_defaults,
        format_defaults,
        precision_defaults,
        allowed_defaults,
        compliance_defaults,
    ) = _build_column_table_defaults(columns, col_profiles)

    return [
        # ── Section: Column selection ────────────────────────────────
        {
            "id": f"dataset:{did}:feature_columns",
            "type": "multi-select",
            "question": "Select the feature columns to use for profiling and modeling.",
            "options": columns,
            "section": "columns",
        },
        {
            "id": f"dataset:{did}:target_columns",
            "type": "multi-select",
            "question": "Select the target column. Leave empty if this dataset has no label/target.",
            "options": columns,
            "section": "columns",
        },
        # ── Section: Quality thresholds ──────────────────────────────
        {
            "id": f"dataset:{did}:quality_threshold_mode",
            "type": "select",
            "question": "Use the default global quality threshold, or override it for this dataset?",
            "options": ["accept_default", "override"],
            "section": "thresholds",
        },
        {
            "id": f"dataset:{did}:quality_threshold_override",
            "type": "text",
            "multiline": False,
            "question": "If overriding, enter one threshold between 0 and 1 for all quality checks. Leave blank to keep the default.",
            "section": "thresholds",
        },
        # ── Section: Accuracy ────────────────────────────────────────
        {
            "id": f"dataset:{did}:syntactic_allowed_values",
            "type": "column-table",
            "question": "Syntactic accuracy: define allowed values per column.",
            "rows": columns,
            "fields": [
                {"key": "allowed_values", "label": "Allowed values", "cellType": "tag-list",
                 "placeholder": "Add allowed values..."},
            ],
            "profiles": col_profiles or {},
            "initialValue": allowed_defaults,
            "section": "accuracy",
        },
        {
            "id": f"dataset:{did}:range_rules",
            "type": "column-table",
            "question": "Data accuracy: define valid [min, max] ranges per numeric column.",
            "rows": [c for c in columns if col_profiles and col_profiles.get(c, {}).get("is_numeric")],
            "fields": [
                {"key": "range", "label": "Valid range [min, max]", "cellType": "range",
                 "placeholder": "e.g. 18, 100"},
            ],
            "profiles": col_profiles or {},
            "initialValue": range_defaults,
            "section": "accuracy",
        },
        {
            "id": f"dataset:{did}:semantic_accuracy_rules",
            "type": "text",
            "multiline": True,
            "question": (
                "Semantic accuracy rules (cross-column). JSON list of conditional rules. "
                'Example: [{"condition":{"operator":"AND","clauses":[{"column":"country","value":"DE"}]},"target_column":"currency","valid_values":["EUR"]}]'
            ),
            "section": "accuracy",
        },
        {
            "id": f"dataset:{did}:inaccuracy_columns",
            "type": "multi-select",
            "question": "Select numeric columns to check for outliers/inaccuracy.",
            "options": columns,
            "section": "accuracy",
        },
        {
            "id": f"dataset:{did}:value_occurrence_expectations",
            "type": "column-table",
            "question": "Value occurrence completeness: expected value and minimum count per column.",
            "rows": columns,
            "fields": [
                {"key": "expected_value", "label": "Expected value", "cellType": "text",
                 "placeholder": "e.g. 1"},
                {"key": "expected_count", "label": "Min count", "cellType": "number",
                 "placeholder": "e.g. 1200"},
            ],
            "profiles": col_profiles or {},
            "section": "accuracy",
        },
        # ── Section: Consistency ─────────────────────────────────────
        {
            "id": f"dataset:{did}:format_schema",
            "type": "column-table",
            "question": "Format consistency: confirm or correct the expected data type per column.",
            "rows": columns,
            "fields": [
                {"key": "expected_type", "label": "Expected type", "cellType": "type-select"},
            ],
            "profiles": col_profiles or {},
            "initialValue": format_defaults,
            "section": "consistency",
        },
        {
            "id": f"dataset:{did}:compliance_rules",
            "type": "column-table",
            "question": "Data compliance: define validation rules per column.",
            "rows": columns,
            "fields": [
                {"key": "rule", "label": "Compliance rule", "cellType": "predicate",
                 "placeholder": "e.g. between 18, 100"},
            ],
            "profiles": col_profiles or {},
            "initialValue": compliance_defaults,
            "section": "consistency",
        },
        {
            "id": f"dataset:{did}:consistency_label_threshold",
            "type": "text",
            "multiline": False,
            "question": "Label consistency clustering threshold (0-1). Leave blank for 0.5.",
            "section": "consistency",
        },
        {
            "id": f"dataset:{did}:semantic_consistency_rules",
            "type": "text",
            "multiline": True,
            "question": (
                "Semantic consistency. JSON list of row-level rules. "
                'Example: [{"operator":"AND","clauses":[{"column":"loan_status","op":"in","value":[0,1]}]}]'
            ),
            "section": "consistency",
        },
        {
            "id": f"dataset:{did}:precision_requirements",
            "type": "column-table",
            "question": "Precision: set the required number of decimal places for numeric columns.",
            "rows": [c for c in columns if col_profiles and col_profiles.get(c, {}).get("is_numeric")],
            "fields": [
                {"key": "decimals", "label": "Decimal places", "cellType": "number",
                 "placeholder": "e.g. 2"},
            ],
            "profiles": col_profiles or {},
            "initialValue": precision_defaults,
            "section": "consistency",
        },
        # ── Section: Effectiveness ───────────────────────────────────
        {
            "id": f"dataset:{did}:sensitive_columns",
            "type": "multi-select",
            "question": "Select columns containing sensitive data to exclude from LLM-based mitigation.",
            "options": columns,
            "section": "effectiveness",
        },
        {
            "id": f"dataset:{did}:category_column",
            "type": "select",
            "question": "Select one category column for balance, diversity, and category-size metrics.",
            "options": columns,
            "section": "effectiveness",
        },
        {
            "id": f"dataset:{did}:balance_target_labels",
            "type": "tag-list",
            "question": "Label balance/diversity: enter target labels to compare. Leave blank to use all observed labels.",
            "placeholder": "Type a label and press Enter...",
            "section": "effectiveness",
        },
        {
            "id": f"dataset:{did}:feature_effectiveness_rules",
            "type": "column-table",
            "question": "Feature effectiveness: define valid-range predicates per feature.",
            "rows": columns,
            "fields": [
                {"key": "rule", "label": "Effectiveness rule", "cellType": "predicate",
                 "placeholder": "e.g. between 18, 100"},
            ],
            "profiles": col_profiles or {},
            "section": "effectiveness",
        },
        {
            "id": f"dataset:{did}:category_size_threshold",
            "type": "text",
            "multiline": False,
            "question": "Minimum category size threshold. Leave blank to skip.",
            "section": "effectiveness",
        },
        {
            "id": f"dataset:{did}:label_effectiveness_rules",
            "type": "tag-list",
            "question": "Label effectiveness: enter acceptable label values.",
            "placeholder": "Type a label and press Enter...",
            "section": "effectiveness",
        },
        # ── Section: Relevance ───────────────────────────────────────
        {
            "id": f"dataset:{did}:target_size",
            "type": "text",
            "multiline": False,
            "question": "Target dataset size in bytes. Leave blank to skip.",
            "section": "relevance",
        },
        {
            "id": f"dataset:{did}:relevant_features",
            "type": "multi-select",
            "question": "Select the features expected to be relevant. Leave blank to skip.",
            "options": columns,
            "section": "relevance",
        },
        {
            "id": f"dataset:{did}:record_relevance_condition",
            "type": "text",
            "multiline": True,
            "question": (
                "Record relevance. JSON rule for relevant rows. "
                'Example: {"operator":"AND","clauses":[{"column":"loan_status","op":"eq","value":1}]}'
            ),
            "section": "relevance",
        },
        {
            "id": f"dataset:{did}:required_attributes",
            "type": "multi-select",
            "question": "Select attributes that must be represented in the sample. Leave blank to skip.",
            "options": columns,
            "section": "relevance",
        },
    ]


def _ensure_dataset_setup_questions(user, session, did, columns, col_profiles=None):
    feature_key = f"dataset:{did}:feature_columns"
    target_key = f"dataset:{did}:target_columns"

    if redis.exists(feature_key) or redis.exists(target_key):
        return

    message = {
        "event": "state/queries",
        "value": json.dumps(_dataset_setup_questions(did, columns, col_profiles)),
        "uid": user,
        "session": session,
        "callback": f"dataset:{did}:setup",
    }
    redis.xadd(f"{user}:{session}:stream", message, maxlen=STREAM_MAXLEN, approximate=True)


def get_features(user, session, did, columns, col_profiles=None):
    emit(Event('GettingDatasetFeatures', {
        'uid': user,
        'session': session,
        'did': did,
        'columns': columns
    }))

    if not columns:
        emit(Event('ValueError', {
            'uid': user,
            'session': session,
            'did': did,
            'error': 'No feature columns specified.'
        }))
        return []

    callback = f"dataset:{did}:feature_columns"

    if not redis.exists(callback):
        _ensure_dataset_setup_questions(user, session, did, columns, col_profiles)

        while not redis.exists(callback):
            sleep(1)

    return json.loads(redis.get(callback))

def get_targets(user, session, did, columns, col_profiles=None):
    if not columns:
        emit(Event('ValueError', {
            'uid': user,
            'session': session,
            'did': did,
            'error': 'No target columns specified.'
        }))
        return []

    callback = f"dataset:{did}:target_columns"

    if not redis.exists(callback):
        _ensure_dataset_setup_questions(user, session, did, columns, col_profiles)

        while not redis.exists(callback):
            sleep(1)

    return json.loads(redis.get(callback))
