"""Seed AutoML templates from per-dataset column composition.

Generates a fresh template DAG per dataset, wired to match the
dataset's feature dtype mix:

  * all-numeric  → ``imputer (LT) → scaler  (LT) → estimator (LT)``
  * all-categorical → ``imputer (LT) → encoder (LT) → estimator (LT)``
  * mixed        → skipped for now (proper per-column-type split
    requires a `concat_features` snippet path; tracked as a follow-up)

Each preprocessing LogicalTask gets the identity-bypass option via
``slot_from_kb`` on the Rust side: SMAC can choose ``__identity__``
to skip the slot, and the materialiser then drops the slot and
short-circuits its data edges. The estimator slot stays mandatory.

Run from any container that has ``asyncpg`` and DB env vars:

    podman exec -e DORIAN_POSTGRES_PASSWORD=$PGPASS -e \
      DORIAN_POSTGRES_HOST=postgres -e DORIAN_POSTGRES_USER=dorian \
      -e DORIAN_POSTGRES_DATABASE=dorian \
      dorian-backend uv run python scripts/seed_automl_templates.py

Idempotent — each dataset's template id is ``automl-template-<did>``,
and re-running upserts the same row. Prior structurally-different
templates (e.g. the per-source-pipeline templates this seeder used
to emit) get cleaned up by ``--purge-old`` so the optimizer pool
doesn't pollute with stale per-pair surrogates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import asyncpg


# Snippet bodies — must be inline because ``Snippet.code`` is what the
# operator resolver ``exec``s + calls ``foo(...)`` on. The runner
# rejects Snippet nodes with empty ``code``. Mirrors
# ``dorian/pipeline/generation/eval_template.py::_PROJECT_COLUMNS_SNIPPET``
# and the label-encoder snippet body in
# ``dorian/pipeline/mitigation_rewrites.py``.
_PROJECT_COLUMNS_CODE = (
    "def foo(df, columns=None):\n"
    "    if columns is None:\n"
    "        return df\n"
    "    if isinstance(columns, str):\n"
    "        return df[columns]\n"
    "    cols = list(columns)\n"
    "    if len(cols) == 1:\n"
    "        return df[cols[0]]\n"
    "    return df[cols]\n"
)

_LABEL_ENCODER_CODE = (
    "def foo(y):\n"
    "    import pandas as pd\n"
    "    return pd.Categorical(y).codes\n"
)


async def _connect():
    return await asyncpg.connect(
        host=os.environ.get("DORIAN_POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("DORIAN_POSTGRES_PORT", "5432")),
        user=os.environ.get("DORIAN_POSTGRES_USER", "dorian"),
        password=os.environ["DORIAN_POSTGRES_PASSWORD"],
        database=os.environ.get("DORIAN_POSTGRES_DATABASE", "dorian"),
    )


def _classify_dataset(columns: dict) -> tuple[str, list[str], list[str], list[str]]:
    """Return (kind, features, numeric_cols, categorical_cols)."""
    features = list(columns.get("features") or [])
    profiles = columns.get("profiles") or {}
    numeric, categorical = [], []
    for col in features:
        prof = profiles.get(col) or {}
        is_num = bool(prof.get("is_numeric"))
        (numeric if is_num else categorical).append(col)
    if not features:
        return "empty", features, numeric, categorical
    if not categorical:
        return "all_numeric", features, numeric, categorical
    if not numeric:
        return "all_categorical", features, numeric, categorical
    return "mixed", features, numeric, categorical


def _build_template(
    dataset_id: str, kind: str,
) -> dict[str, Any] | None:
    """Construct the per-dataset template DAG.

    Wiring follows the flaml convention the runner already supports:

      * ``project_columns`` snippets slice features and target out
        of the loaded DataFrame.
      * ``label_encoder`` snippet encodes the target so classifier
        metrics see numeric labels.
      * ``train_test_split`` takes the processed (X, y) plus
        ``test_size``/``random_state`` and yields the four splits.
      * The estimator LogicalTask reads (X_train, y_train, X_test)
        from the splits and returns (instance, y_pred); the runner's
        method-shortcut path materialises fit + predict on the
        chosen classifier.
      * Metrics consume y_test (split out=3) at position 0 and y_pred
        (estimator out=1) at position 1.

    Preprocessing slots sit between the X projection and the split,
    so the optimizer's choice of imputer/scaler-or-encoder + the
    optional identity bypass propagates through unchanged.
    """
    if kind not in ("all_numeric", "all_categorical"):
        return None
    estimator_node = f"estimator_{dataset_id[:8]}"
    nodes: dict[str, Any] = {
        "ds": {
            "class_type": "Operator",
            "name": "dorian.io.dataset",
            "language": "python",
        },
        "p_features": {
            "class_type": "Parameter",
            "name": "dorian.io.state",
            "value": "dataset.features",
            "dtype": "state",
        },
        "p_target": {
            "class_type": "Parameter",
            "name": "dorian.io.state",
            "value": "dataset.target",
            "dtype": "state",
        },
        "proj_X": {
            "class_type": "Snippet",
            "name": "project_columns",
            "language": "python",
            "code": _PROJECT_COLUMNS_CODE,
        },
        "proj_y": {
            "class_type": "Snippet",
            "name": "project_columns",
            "language": "python",
            "code": _PROJECT_COLUMNS_CODE,
        },
        "label_encoder": {
            "class_type": "Snippet",
            "name": "label_encoder",
            "language": "python",
            "code": _LABEL_ENCODER_CODE,
        },
        "imputer": {
            "class_type": "LogicalTask",
            "path": ["Missing Data Imputation"],
            "name": "Missing Data Imputation",
        },
        # Slot 2 swaps shape based on dtype — scaler for numeric,
        # encoder for categorical. Identity bypass keeps either valid.
        "preproc2": {
            "class_type": "LogicalTask",
            "path": ["Data Normalization"]
                if kind == "all_numeric"
                else ["Data Encoding"],
            "name": "Data Normalization"
                if kind == "all_numeric"
                else "Data Encoding",
        },
        "p_test_size": {
            "class_type": "Parameter",
            "name": "test_size", "value": "0.2", "dtype": "float",
        },
        "p_random_state": {
            "class_type": "Parameter",
            "name": "random_state", "value": "42", "dtype": "int",
        },
        "tts": {
            "class_type": "Operator",
            "name": "sklearn.model_selection.train_test_split",
            "language": "python",
        },
        estimator_node: {
            "class_type": "LogicalTask",
            "path": ["Classification"],
            "name": "Classification",
        },
        "metric_acc": {
            "class_type": "Operator",
            "name": "sklearn.metrics.accuracy_score",
            "language": "python",
        },
    }

    edges: list[dict[str, Any]] = [
        # project features and target out of the loaded DataFrame
        {"source": "ds", "destination": "proj_X", "position": 0, "output": 0},
        {"source": "p_features", "destination": "proj_X",
         "position": "columns", "output": 0},
        {"source": "ds", "destination": "proj_y", "position": 0, "output": 0},
        {"source": "p_target", "destination": "proj_y",
         "position": "columns", "output": 0},

        # X feature pipeline: project → imputer → preproc2 → tts.0
        {"source": "proj_X", "destination": "imputer",
         "position": 0, "output": 0},
        {"source": "imputer", "destination": "preproc2",
         "position": 0, "output": 0},
        {"source": "preproc2", "destination": "tts",
         "position": 0, "output": 0},

        # y target encoding: project_y → label_encoder → tts.1
        {"source": "proj_y", "destination": "label_encoder",
         "position": 0, "output": 0},
        {"source": "label_encoder", "destination": "tts",
         "position": 1, "output": 0},

        # split parameters
        {"source": "p_test_size", "destination": "tts",
         "position": "test_size", "output": 0},
        {"source": "p_random_state", "destination": "tts",
         "position": "random_state", "output": 0},

        # estimator reads X_train, y_train, X_test from the splits
        # (out=0/2/1 respectively in train_test_split's
        # X_train/X_test/y_train/y_test ordering).
        {"source": "tts", "destination": estimator_node,
         "position": 0, "output": 0},
        {"source": "tts", "destination": estimator_node,
         "position": 1, "output": 2},
        {"source": "tts", "destination": estimator_node,
         "position": 2, "output": 1},

        # metric consumes y_test (split out=3) and y_pred (estimator out=1)
        {"source": "tts", "destination": "metric_acc",
         "position": 0, "output": 3},
        {"source": estimator_node, "destination": "metric_acc",
         "position": 1, "output": 1},
    ]
    return {"nodes": nodes, "edges": edges}


async def _purge_old(conn) -> int:
    res = await conn.execute(
        "DELETE FROM doc_pipelines WHERE id LIKE 'automl-template-%' "
        "AND id NOT LIKE 'automl-template-ds-%'"
    )
    # asyncpg returns a status string like "DELETE 3"
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def run(limit: int, dry_run: bool, purge_old: bool) -> int:
    conn = await _connect()
    try:
        if purge_old and not dry_run:
            n = await _purge_old(conn)
            if n:
                print(f"purged {n} old-format template row(s)")

        rows = await conn.fetch(
            "SELECT id, columns FROM datasets "
            "WHERE columns IS NOT NULL "
            "ORDER BY created_at DESC NULLS LAST "
            "LIMIT $1",
            limit * 4,
        )

        seeded: list[str] = []
        skipped: dict[str, int] = {}
        for row in rows:
            did = row["id"]
            cols = json.loads(row["columns"]) if isinstance(row["columns"], str) else row["columns"]
            kind, features, num, cat = _classify_dataset(cols)
            if kind in ("empty", "mixed"):
                skipped[kind] = skipped.get(kind, 0) + 1
                continue

            tpl = _build_template(did, kind)
            if tpl is None:
                skipped[kind] = skipped.get(kind, 0) + 1
                continue
            template_id = f"automl-template-ds-{did}"

            doc = {
                "_id": template_id,
                "task": "classification",
                "nodes": tpl["nodes"],
                "edges": tpl["edges"],
                "provenance": "automl-template",
                "source": "automl-template",
                "template_id": template_id,
                "dataset_kind": kind,
                "n_features": len(features),
                "n_numeric": len(num),
                "n_categorical": len(cat),
            }
            if dry_run:
                print(
                    f"DRY  {template_id}: {kind} "
                    f"({len(num)}n+{len(cat)}c)"
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO doc_pipelines (id, data, created_at, updated_at)
                    VALUES ($1, $2::jsonb, NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE
                      SET data = EXCLUDED.data,
                          updated_at = NOW()
                    """,
                    template_id, json.dumps(doc),
                )
                print(
                    f"seed {template_id}: {kind} "
                    f"({len(num)}n+{len(cat)}c)"
                )
            seeded.append(template_id)
            if len(seeded) >= limit:
                break

        if skipped:
            sk = ", ".join(f"{k}={v}" for k, v in skipped.items())
            print(f"\nskipped: {sk}")
        verb = "would be " if dry_run else ""
        print(f"{len(seeded)} template(s) {verb}seeded")
        return 0 if seeded else 1
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-old", action="store_true",
                    help="Delete prior-format templates (per-source-pipeline) "
                         "before seeding the new per-dataset rows.")
    args = ap.parse_args()
    return asyncio.run(run(args.limit, args.dry_run, args.purge_old))


if __name__ == "__main__":
    sys.exit(main())
