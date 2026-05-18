"""PostgreSQL DDL for the Experiment Store.

Creates tables on startup if they don't exist.  No ORM — raw DDL via asyncpg,
matching the project's low-abstraction style (raw Cypher for Neo4j, raw Redis
commands, raw SQL).

Tables
------
datasets      — persistent metafeature profiles (KD-Tree backing store)
pipelines     — pipeline snapshots with operator lists (BK-Tree backing store)
evaluations   — one row per (pipeline, dataset, run) execution result
interactions  — pairwise pipeline comparisons (paper §6.3)
"""
from __future__ import annotations

import asyncpg

from backend.events import Event, aemit

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

_DDL = """\
-- Datasets: canonical dataset records — metafeature profile, identity,
-- task definition, column profiles, and storage pointer. Pre-docstore
-- retirement these fields lived in per-collection doc_* tables JSONB; consolidating
-- them into the schema-typed table puts dataset truth in one place
-- under dorian-domain naming (dataset, pipeline, rewrite, exception
-- pattern — not "collection").
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    session         TEXT NOT NULL,
    profile         JSONB NOT NULL,
    profile_vec     DOUBLE PRECISION[],
    vec_version     INTEGER NOT NULL DEFAULT 1,
    -- Identity + provenance
    name            TEXT,
    description     TEXT,
    owner_id        TEXT,
    is_public       BOOLEAN NOT NULL DEFAULT FALSE,
    data_type       TEXT,
    item_count      BIGINT,
    content_hash    TEXT,
    schema_version  INTEGER,
    -- Task + columns (target / features / per-column profiles)
    task            JSONB,
    columns         JSONB,
    -- Source (openml / user upload / etc) + storage location
    source          JSONB,
    storage         JSONB,
    -- Free-form analysis (auto-generated insights, etc.)
    analysis        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Pipelines: canonical pipeline records — DAG plus indexable operator
-- list. Operators[] feeds the BK-Tree similarity index; dag holds the
-- full nodes/edges payload. ``source`` distinguishes user-authored
-- from RL/AutoML/cross-product-generated pipelines.
--
-- ``model`` carries the new Ptolemy II actor-graph shape produced
-- by the rust extractor. Coexists with ``dag`` during the storage
-- canonicalisation phase: writers gradually flip to populate
-- ``model`` instead of ``dag``; readers migrate to consume
-- ``model``; the legacy ``dag`` column drops once the last
-- consumer ports.
--
-- New deployments get the column from CREATE TABLE; existing
-- deployments pick it up via the ALTER TABLE block below.
CREATE TABLE IF NOT EXISTS pipelines (
    id              TEXT PRIMARY KEY,
    session         TEXT NOT NULL,
    task            TEXT,
    dag             JSONB NOT NULL,
    model           JSONB,
    operators       TEXT[] NOT NULL,
    provenance      TEXT DEFAULT 'user',
    source          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Rewrites: KB-driven mitigation rules. Each row carries the
-- (pattern, transformations) pair compiled by
-- ``dorian.pipeline.mitigation_rewrites.compile_rewrite_rule``.
-- Fed by ``backend.infra.dbs.expdb.seed_rewrites``.
CREATE TABLE IF NOT EXISTS rewrites (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    pattern         JSONB NOT NULL,
    transformations JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Exception patterns: AI Debugger's exception → mitigation index.
-- A trial / user execution that surfaces a Python exception matching
-- one of these rows triggers the listed mitigations.
--
-- ``active`` powers Phase-2 pattern-gated retries
-- ((internal design note; not in public repo)): xproduct's gate joins on this row
-- with ``active = true`` so a pattern flipped to ``false`` (because
-- a curated rewrite landed for it) re-opens every (pipeline,
-- dataset) pair that previously failed against it. New patterns
-- default to ``true`` — the failure-of-the-day is gating until a
-- fix lands.
--
-- Existing deployments without the column must apply:
--    ALTER TABLE exception_patterns
--        ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS exception_patterns (
    id                TEXT PRIMARY KEY,
    exception_type    TEXT NOT NULL,
    message_regex     TEXT,
    message_template  TEXT,
    status            TEXT,
    scope             TEXT,
    source            TEXT,
    operator_fqn      TEXT,
    site_library      TEXT,
    user_frame_depth  INTEGER,
    mitigations       JSONB,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Pipeline evaluations: one row per (pipeline, dataset, metric) outcome.
-- Serves as the unified Trial record across every consumer (RL
-- trainer, AutoML BO, cross-product engine, user-driven canvas
-- runs).
--
-- Columns:
--   * ``source``         — who created this trial ('rl', 'automl',
--                          'xproduct', 'user'). Drives surrogate
--                          weighting + UI filtering.
--   * ``status``         — terminal state ('success', 'failed',
--                          'timeout', 'cancelled'). Failed trials
--                          carry their error_message; surrogates
--                          can still learn from them.
--   * ``wall_clock_s``   — execution time. Feeds AutoML's
--                          early-stop / budget logic + the cache
--                          benefit scorer.
--   * ``error_message``  — body when ``status != 'success'``;
--                          truncated to 4 KiB at insert.
--   * ``config``         — full hyperparameter binding set. Distinct
--                          from ``eval_config`` (eval procedure:
--                          holdout / k-fold / etc). The BO
--                          surrogate keys on ``config`` + dataset
--                          profile.
--
-- Existing deployments running schemas without the source / status
-- / wall_clock_s / error_message / config columns must apply this
-- one-liner before re-running this DDL:
--
--    ALTER TABLE evaluations
--        ADD COLUMN IF NOT EXISTS source        TEXT NOT NULL DEFAULT 'user',
--        ADD COLUMN IF NOT EXISTS status        TEXT NOT NULL DEFAULT 'success',
--        ADD COLUMN IF NOT EXISTS wall_clock_s  DOUBLE PRECISION,
--        ADD COLUMN IF NOT EXISTS error_message TEXT,
--        ADD COLUMN IF NOT EXISTS config        JSONB;
CREATE TABLE IF NOT EXISTS evaluations (
    id              SERIAL PRIMARY KEY,
    pipeline_id     TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    DOUBLE PRECISION NOT NULL,
    eval_config     JSONB,
    source          TEXT          NOT NULL DEFAULT 'user',
    status          TEXT          NOT NULL DEFAULT 'success',
    wall_clock_s    DOUBLE PRECISION,
    error_message   TEXT,
    config          JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Interaction Table (paper §6.3): pairwise pipeline comparisons
CREATE TABLE IF NOT EXISTS interactions (
    id              SERIAL PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    task            TEXT,
    compared_id     TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    preferred_id    TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    discarded_id    TEXT REFERENCES pipelines(id) ON DELETE SET NULL,
    user_id         TEXT NOT NULL,
    eval_id         INTEGER REFERENCES evaluations(id) ON DELETE SET NULL,
    performance     DOUBLE PRECISION,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indices for efficient querying
CREATE INDEX IF NOT EXISTS idx_datasets_session         ON datasets(session);
CREATE INDEX IF NOT EXISTS idx_pipelines_task           ON pipelines(task);
CREATE INDEX IF NOT EXISTS idx_pipelines_session        ON pipelines(session);
CREATE INDEX IF NOT EXISTS idx_evaluations_pipeline     ON evaluations(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_dataset      ON evaluations(dataset_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_run          ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_interactions_dataset     ON interactions(dataset_id);
CREATE INDEX IF NOT EXISTS idx_interactions_task        ON interactions(task);
CREATE INDEX IF NOT EXISTS idx_interactions_user        ON interactions(user_id);
CREATE INDEX IF NOT EXISTS idx_interactions_preferred   ON interactions(preferred_id);
CREATE INDEX IF NOT EXISTS idx_interactions_compared    ON interactions(compared_id, preferred_id);

-- Extractions: relational index for pipeline extraction history.
-- Full code + DAG blobs live in the docstore; this table stores IDs + rules version
-- for regression testing and cross-referencing.
CREATE TABLE IF NOT EXISTS extractions (
    id               TEXT PRIMARY KEY,
    code_hash        TEXT NOT NULL,
    auto_dag_id      TEXT NOT NULL,
    corrected_dag_id TEXT,
    rules_version    TEXT NOT NULL,
    session          TEXT,
    uid              TEXT,
    status           TEXT NOT NULL DEFAULT 'auto',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    corrected_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_extractions_rules_version ON extractions(rules_version);
CREATE INDEX IF NOT EXISTS idx_extractions_session        ON extractions(session);
CREATE INDEX IF NOT EXISTS idx_extractions_status         ON extractions(status);

-- Backfill the columns added when consolidating per-collection doc_* tables rows
-- into the schema-typed `datasets` and `pipelines` tables. Each
-- ALTER is idempotent so re-runs on already-migrated deployments
-- are no-ops.
--
-- IMPORTANT: ALTER TABLE comes BEFORE the indexes below so a
-- pre-consolidation `datasets` row schema (no `name` / `is_public`)
-- has the columns added before ``CREATE INDEX ... ON datasets(name)``
-- runs. The reverse order crashed `ExperimentStore.init` with
-- ``UndefinedColumnError('column "name" does not exist')`` and the
-- entire DDL aborted before any index landed.
ALTER TABLE datasets
    ADD COLUMN IF NOT EXISTS name           TEXT,
    ADD COLUMN IF NOT EXISTS description    TEXT,
    ADD COLUMN IF NOT EXISTS owner_id       TEXT,
    ADD COLUMN IF NOT EXISTS is_public      BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS data_type      TEXT,
    ADD COLUMN IF NOT EXISTS item_count     BIGINT,
    ADD COLUMN IF NOT EXISTS content_hash   TEXT,
    ADD COLUMN IF NOT EXISTS schema_version INTEGER,
    ADD COLUMN IF NOT EXISTS task           JSONB,
    ADD COLUMN IF NOT EXISTS columns        JSONB,
    ADD COLUMN IF NOT EXISTS source         JSONB,
    ADD COLUMN IF NOT EXISTS storage        JSONB,
    ADD COLUMN IF NOT EXISTS analysis       JSONB;

ALTER TABLE pipelines
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS model  JSONB;

-- Per-trial telemetry columns on ``evaluations``. Older deploys
-- created the table when only ``eval_config`` JSONB existed; the
-- schema later moved the trial telemetry out of ``eval_config``
-- into top-level columns (``source``, ``status``, ``wall_clock_s``,
-- ``error_message``, ``config``) for indexable filtering. The
-- CREATE TABLE IF NOT EXISTS above is a no-op on existing tables,
-- so without this ALTER TABLE the columns never land on a
-- live database — every ``store_trial`` write fails silently and
-- xproduct's "skip failed pipelines" filter has no signal to read.
ALTER TABLE evaluations
    ADD COLUMN IF NOT EXISTS source        TEXT NOT NULL DEFAULT 'user',
    ADD COLUMN IF NOT EXISTS status        TEXT NOT NULL DEFAULT 'success',
    ADD COLUMN IF NOT EXISTS wall_clock_s  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS config        JSONB;

-- Phase 2 of pattern-gated retries ((internal design note; not in public repo)):
-- xproduct's gate joins ``evaluations`` failures with this table on
-- ``eval_config->>'pattern_id' = exception_patterns.id`` and only
-- gates when ``active = true``. ``MitigationRewriteApplied`` /
-- ``RLMitigationApplied`` flip ``active`` to false for every pattern
-- whose ``mitigations`` list contains the just-applied rewrite, so
-- xproduct's next tick re-enqueues every previously-failed pair.
ALTER TABLE exception_patterns
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_evaluations_source ON evaluations(source);
CREATE INDEX IF NOT EXISTS idx_evaluations_status ON evaluations(status);
CREATE INDEX IF NOT EXISTS idx_evaluations_pip_ds ON evaluations(pipeline_id, dataset_id);

-- Indices for the consolidated tables — must come AFTER the ALTER
-- TABLE backfills above.
CREATE INDEX IF NOT EXISTS idx_datasets_name              ON datasets(name);
CREATE INDEX IF NOT EXISTS idx_datasets_is_public         ON datasets(is_public);
CREATE INDEX IF NOT EXISTS idx_datasets_content_hash      ON datasets(content_hash);
CREATE INDEX IF NOT EXISTS idx_pipelines_source           ON pipelines(source);
CREATE INDEX IF NOT EXISTS idx_rewrites_name              ON rewrites(name);
CREATE INDEX IF NOT EXISTS idx_exception_patterns_op      ON exception_patterns(operator_fqn);
CREATE INDEX IF NOT EXISTS idx_exception_patterns_type    ON exception_patterns(exception_type);
CREATE INDEX IF NOT EXISTS idx_exception_patterns_active  ON exception_patterns(active) WHERE active = false;
"""


async def create_schema(pool: asyncpg.Pool) -> None:
    """Execute DDL to ensure all Experiment Store tables exist.

    Safe to call on every startup — uses ``CREATE TABLE IF NOT EXISTS``.
    """
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
    await aemit(Event("ExperimentStoreSchemaCreated", {}))
