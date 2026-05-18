//! Postgres client for the per-collection JSONB document store.
//!
//! Python's ``backend.db.pg_docstore`` exposes a generic
//! ``Database`` / ``Collection`` facade over per-collection
//! ``doc_<name>`` Postgres tables (created lazily on first use).
//! The same convention applies here: callers pass a logical
//! collection name (``"datasets"``, ``"ranking_objectives"``, …) and
//! this module routes the SQL onto ``doc_<collection>``.
//!
//! This module is the tactical alternative to a generic facade: a
//! connection pool on ``AppState``, plus thin helpers that issue
//! concrete SQL for the per-collection table shape. Each handler
//! writes the specific SQL it needs. Zero pattern-matching on
//! operators, zero generic filter-to-SQL translation, zero
//! abstractions to debug.
//!
//! Per-collection schema (created lazily by python's
//! ``Collection._ensure_table`` and recreated here when a rust
//! handler beats python to the punch):
//!
//!   CREATE TABLE doc_<name> (
//!     id         TEXT PRIMARY KEY,
//!     data       JSONB NOT NULL,
//!     created_at TIMESTAMPTZ DEFAULT NOW(),
//!     updated_at TIMESTAMPTZ DEFAULT NOW()
//!   );

use anyhow::{anyhow, Context, Result};
use deadpool_postgres::{Config, Pool, Runtime};
use serde_json::Value;
use tokio_postgres::types::Json;
use tokio_postgres::NoTls;

/// Build the connection pool from env. Mirrors ``backend.envs.get_pg_pool``
/// reading dynaconf — here it's plain env vars (POSTGRES_HOST, ...).
pub fn pool_from_env() -> Result<Pool> {
    let mut cfg = Config::new();
    cfg.host = std::env::var("POSTGRES_HOST").ok();
    cfg.port = std::env::var("POSTGRES_PORT")
        .ok()
        .and_then(|s| s.parse::<u16>().ok());
    cfg.user = std::env::var("POSTGRES_USER").ok();
    cfg.password = std::env::var("POSTGRES_PASSWORD").ok();
    cfg.dbname = std::env::var("POSTGRES_DB").ok();
    let pool = cfg
        .create_pool(Some(Runtime::Tokio1), NoTls)
        .with_context(|| "create pg pool")?;
    Ok(pool)
}

/// Resolve a logical collection name to its concrete ``doc_<name>``
/// table. Validates that the name is a plain identifier so the
/// resulting SQL can never carry an injection path — we splice this
/// into the query because PostgreSQL doesn't bind table names as
/// parameters.
fn table_for(collection: &str) -> Result<String> {
    if collection.is_empty() {
        return Err(anyhow!("empty collection name"));
    }
    if !collection
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return Err(anyhow!("invalid collection name: {collection:?}"));
    }
    Ok(format!("doc_{collection}"))
}

/// CREATE TABLE IF NOT EXISTS the per-collection table. Idempotent;
/// mirrors python's ``Collection._ensure_table`` shape exactly so
/// rust + python can interleave writes safely on a fresh deploy.
async fn ensure_table(pool: &Pool, collection: &str) -> Result<String> {
    let table = table_for(collection)?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!(
        "CREATE TABLE IF NOT EXISTS {table} ( \
            id         TEXT PRIMARY KEY, \
            data       JSONB NOT NULL, \
            created_at TIMESTAMPTZ DEFAULT NOW(), \
            updated_at TIMESTAMPTZ DEFAULT NOW() \
         )"
    );
    client
        .execute(sql.as_str(), &[])
        .await
        .with_context(|| format!("ensure_table({collection})"))?;
    Ok(table)
}

/// Insert or replace a document in *collection*. ``upsert`` is the
/// default semantic (matches the python facade's
/// ``insert_one`` + ``replace_one(upsert=True)`` callers, which is
/// 80% of writes). Caller picks the ``id``; uuid-generation is the
/// caller's job, not the storage layer's.
pub async fn upsert(
    pool: &Pool,
    collection: &str,
    id: &str,
    data: &Value,
) -> Result<()> {
    let table = ensure_table(pool, collection).await?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!(
        "INSERT INTO {table} (id, data) VALUES ($1, $2::jsonb) \
         ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()"
    );
    client
        .execute(sql.as_str(), &[&id, &Json(data)])
        .await
        .with_context(|| format!("upsert({collection}, {id})"))?;
    Ok(())
}

/// Merge a partial JSON object onto an existing document.
/// Equivalent to the docstore's ``{"$set": patch}`` with ``upsert=True``.
/// On a missing row, the row is created with ``data = patch``.
pub async fn merge_set(
    pool: &Pool,
    collection: &str,
    id: &str,
    patch: &Value,
) -> Result<()> {
    let table = ensure_table(pool, collection).await?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!(
        "INSERT INTO {table} (id, data) VALUES ($1, $2::jsonb) \
         ON CONFLICT (id) DO UPDATE SET data = {table}.data || EXCLUDED.data, \
                                        updated_at = NOW()"
    );
    client
        .execute(sql.as_str(), &[&id, &Json(patch)])
        .await
        .with_context(|| format!("merge_set({collection}, {id})"))?;
    Ok(())
}

/// Read by id. Returns ``None`` when the row doesn't exist.
pub async fn find_by_id(
    pool: &Pool,
    collection: &str,
    id: &str,
) -> Result<Option<Value>> {
    let table = table_for(collection)?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!("SELECT data FROM {table} WHERE id = $1");
    let row = match client.query_opt(sql.as_str(), &[&id]).await {
        Ok(row) => row,
        Err(e) => {
            // Table may not exist yet on a fresh deploy that hit a
            // read before any writer. Treat that as "no document".
            if e.to_string().contains("does not exist") {
                return Ok(None);
            }
            return Err(e).with_context(|| format!("find_by_id({collection}, {id})"));
        }
    };
    Ok(row.map(|r| {
        let Json(v): Json<Value> = r.get(0);
        v
    }))
}

/// Find the first document where ``data @> filter``. The JSONB
/// containment operator + the GIN index on ``data jsonb_path_ops``
/// hits an index for any ``{"key": value}``-shaped filter — same
/// cost the python facade pays via its ``data @> $1::jsonb`` path.
pub async fn find_one_matching(
    pool: &Pool,
    collection: &str,
    filter: &Value,
) -> Result<Option<(String, Value)>> {
    let table = table_for(collection)?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!(
        "SELECT id, data FROM {table} WHERE data @> $1::jsonb \
         ORDER BY created_at DESC LIMIT 1"
    );
    let row = match client.query_opt(sql.as_str(), &[&Json(filter)]).await {
        Ok(row) => row,
        Err(e) => {
            if e.to_string().contains("does not exist") {
                return Ok(None);
            }
            return Err(e).with_context(|| format!("find_one_matching({collection})"));
        }
    };
    Ok(row.map(|r| {
        let id: String = r.get(0);
        let Json(data): Json<Value> = r.get(1);
        (id, data)
    }))
}

/// Delete by id. Returns the number of rows actually deleted (0 or 1).
pub async fn delete_by_id(
    pool: &Pool,
    collection: &str,
    id: &str,
) -> Result<u64> {
    let table = table_for(collection)?;
    let client = pool.get().await.with_context(|| "pool.get")?;
    let sql = format!("DELETE FROM {table} WHERE id = $1");
    let n = match client.execute(sql.as_str(), &[&id]).await {
        Ok(n) => n,
        Err(e) => {
            if e.to_string().contains("does not exist") {
                return Ok(0);
            }
            return Err(e).with_context(|| format!("delete_by_id({collection}, {id})"));
        }
    };
    Ok(n)
}
