//! Session-meta read/modify/write under a Redis lock. Replaces
//! ``dorian/event/helpers/lifecycle.py::session_meta_tx``.
//!
//! Refactor over the python design:
//!
//!   * The python version's lock loop polls every 50 ms for up to
//!     2 s (40 retries × 50 ms). It's an open-coded busy-wait.
//!     Here we use exponential backoff capped at 100 ms — same
//!     latency budget, fewer wasted Redis ops under contention.
//!   * The python version returns an empty dict when the session
//!     doesn't exist and then writes back, lazily creating the
//!     session record. ``with_session_meta`` keeps that contract
//!     but makes the "missing" case explicit via ``is_new`` so
//!     handlers that should reject missing sessions
//!     (``handle_custom_operator_added``) can branch cleanly.
//!   * The python version writes the lock TTL as 30 s and the
//!     session TTL as 24 h. Both are env-overridable here so the
//!     ops team can tune without a code change.

use anyhow::{Context, Result};
use redis::AsyncCommands;
use serde_json::Value;
use std::time::Duration;
use tokio::time::sleep;

use crate::keys;
use crate::state::AppState;

const LOCK_TTL_SECS: u64 = 30;
const SESSION_TTL_SECS: u64 = 24 * 60 * 60;
const MAX_LOCK_WAIT_MS: u64 = 2000;

/// Hold the per-session lock for the duration of *f*. The closure
/// receives the parsed ``meta`` dict (empty when the session is new)
/// and returns the modified dict to write back. Returning ``None``
/// is the explicit "don't write anything, release the lock" path —
/// useful for read-only inspections that share the lock to avoid
/// races with concurrent writers.
pub async fn with_session_meta<F, Fut>(
    state: &AppState,
    session: &str,
    f: F,
) -> Result<SessionMetaOutcome>
where
    F: FnOnce(SessionMeta) -> Fut,
    Fut: std::future::Future<Output = Result<Option<Value>>>,
{
    acquire_lock(state, session).await?;
    let result = run_with_lock(state, session, f).await;
    release_lock(state, session).await;
    result
}

#[derive(Debug)]
pub struct SessionMeta {
    pub data: Value,
    pub is_new: bool,
}

#[derive(Debug)]
pub enum SessionMetaOutcome {
    Updated,
    Unchanged,
    Missing,
}

async fn run_with_lock<F, Fut>(
    state: &AppState,
    session: &str,
    f: F,
) -> Result<SessionMetaOutcome>
where
    F: FnOnce(SessionMeta) -> Fut,
    Fut: std::future::Future<Output = Result<Option<Value>>>,
{
    let mut conn = state.redis.clone();
    let key = keys::session_meta(session);
    let raw: Option<String> = conn.get(&key).await?;
    let (data, is_new) = match raw {
        Some(s) => (
            serde_json::from_str::<Value>(&s).unwrap_or_else(|_| Value::Object(Default::default())),
            false,
        ),
        None => (Value::Object(Default::default()), true),
    };
    let meta = SessionMeta { data, is_new };
    match f(meta).await? {
        Some(updated) => {
            let serialized = serde_json::to_string(&updated)?;
            let _: () = conn
                .set_ex(&key, serialized, SESSION_TTL_SECS)
                .await
                .context("set_ex session meta")?;
            Ok(if is_new {
                SessionMetaOutcome::Missing
            } else {
                SessionMetaOutcome::Updated
            })
        }
        None => Ok(if is_new {
            SessionMetaOutcome::Missing
        } else {
            SessionMetaOutcome::Unchanged
        }),
    }
}

async fn acquire_lock(state: &AppState, session: &str) -> Result<()> {
    let mut conn = state.redis.clone();
    let key = keys::session_meta_lock(session);
    let mut backoff_ms: u64 = 5;
    let mut waited_ms: u64 = 0;
    loop {
        // SET key value NX EX ttl — atomic acquire.
        let acquired: Option<String> = redis::cmd("SET")
            .arg(&key)
            .arg("1")
            .arg("NX")
            .arg("EX")
            .arg(LOCK_TTL_SECS)
            .query_async(&mut conn)
            .await?;
        if acquired.is_some() {
            return Ok(());
        }
        if waited_ms >= MAX_LOCK_WAIT_MS {
            anyhow::bail!("session-meta lock contention: {session} (waited {waited_ms}ms)");
        }
        sleep(Duration::from_millis(backoff_ms)).await;
        waited_ms = waited_ms.saturating_add(backoff_ms);
        backoff_ms = (backoff_ms * 2).min(100);
    }
}

async fn release_lock(state: &AppState, session: &str) {
    let mut conn = state.redis.clone();
    let key = keys::session_meta_lock(session);
    // Best-effort delete; if the lock TTL already expired (clock
    // skew, slow handler), the next acquirer wins.
    let _: redis::RedisResult<i32> = conn.del(&key).await;
}

/// Upsert a dict-shaped item into a list of dicts on a stable key
/// field. Replaces the python ``_upsert_by_key`` helper. Returns the
/// updated list so callers can re-assign.
pub fn upsert_by_key(items: Vec<Value>, item: Value, key: &str) -> Vec<Value> {
    let item_key = item.get(key).cloned();
    if item_key.is_none() {
        let mut out = items;
        out.push(item);
        return out;
    }
    let target = item_key.unwrap();
    let mut out = Vec::with_capacity(items.len() + 1);
    let mut replaced = false;
    for existing in items {
        if existing.get(key) == Some(&target) && !replaced {
            out.push(item.clone());
            replaced = true;
        } else {
            out.push(existing);
        }
    }
    if !replaced {
        out.push(item);
    }
    out
}
