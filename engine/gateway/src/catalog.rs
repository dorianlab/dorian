//! KB-catalog endpoints — replaces the python
//! ``dorian/api/routes/catalog.py``.
//!
//! All routes read from ``state.kb`` (the rust ``KbSnapshot`` —
//! see ``engine/optimizer/src/kb/``). No DB access, no python
//! interpreter. Cold response time is whatever it takes to walk
//! a few hashmaps.
//!
//! The python catalog included a ``/operator-params`` route that
//! built a per-operator parameter spec by joining four KB tables.
//! That logic lives in ``optimizer::kb::operator_params_catalog``;
//! this module is just the HTTP shim.
//!
//! UUIDs in the response shape are generated per-call (uuid4) to
//! match the python ``Operators.get()`` / ``Tasks.get()`` etc.
//! behaviour — those classes use a ``uuid4`` default factory so
//! consumers don't rely on stability.

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::get,
    Json, Router,
};
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

use crate::state::AppState;

#[derive(Serialize)]
struct CatalogItem {
    uuid: String,
    name: String,
}

fn name_to_item(name: &str) -> CatalogItem {
    CatalogItem {
        uuid: Uuid::new_v4().simple().to_string(),
        name: name.to_string(),
    }
}

/// Register catalog routes on the gateway's protected router.
///
/// All routes live under ``/catalog/*`` to match the SPA's
/// ``frontend/app/api/catalog.ts`` which uses ``baseURL =
/// ${env.backend}/catalog``. The python predecessor used
/// ``APIRouter(prefix="/catalog")``; we keep that contract.
///
/// The catalog itself is not sensitive — but the SPA's catalog
/// calls already carry HMAC headers (the api-client signs every
/// request), so leaving it under HMAC is consistent with the rest
/// of the authenticated surface.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/catalog", get(full_catalog))
        .route("/catalog/operators", get(list_operators))
        .route("/catalog/tasks", get(list_tasks))
        .route("/catalog/operator-params", get(operator_params))
}

async fn list_operators(State(state): State<AppState>) -> impl IntoResponse {
    let Some(kb) = state.inner.kb.clone() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "kb snapshot unavailable").into_response();
    };
    let items: Vec<CatalogItem> = kb
        .all_operators()
        .into_iter()
        .map(|o| name_to_item(&o.name))
        .collect();
    (StatusCode::OK, Json(items)).into_response()
}

async fn list_tasks(State(state): State<AppState>) -> impl IntoResponse {
    let Some(kb) = state.inner.kb.clone() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "kb snapshot unavailable").into_response();
    };
    // KbSnapshot exposes tasks via the per-operator ``tasks`` field —
    // ``operators_for_task`` enumerates by name; the unique set is the
    // task catalog.
    let mut names: rustc_hash::FxHashSet<String> = rustc_hash::FxHashSet::default();
    for op in kb.all_operators() {
        for t in op.tasks.iter() {
            names.insert(t.clone());
        }
    }
    let mut items: Vec<CatalogItem> =
        names.into_iter().map(|n| name_to_item(&n)).collect();
    items.sort_by(|a, b| a.name.cmp(&b.name));
    (StatusCode::OK, Json(items)).into_response()
}

async fn operator_params(State(state): State<AppState>) -> impl IntoResponse {
    let Some(kb) = state.inner.kb.clone() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "kb snapshot unavailable").into_response();
    };
    let body = build_operator_params_map(&kb);
    (StatusCode::OK, Json(body)).into_response()
}

/// Build the operator-params catalog map. Shape matches the SPA's
/// ``OperatorParamCatalog`` =
/// ``Record<string, { params: [...], methods?, inputs?, outputs? }>``.
/// Crucially, ``params`` is always present (possibly empty) so the
/// SPA's ``entry.params.filter(...)`` doesn't crash with
/// ``s.filter is not a function``.
fn build_operator_params_map(kb: &optimizer::kb::KbSnapshot) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::with_capacity(kb.all_operators().len());
    for op in kb.all_operators() {
        let params: Vec<Value> = kb
            .operator_parameters(&op.name)
            .into_iter()
            .map(|p| {
                let default_val = match p.default.as_deref() {
                    Some(d) => Value::String(d.to_string()),
                    None => Value::Null,
                };
                let mut spec = serde_json::Map::new();
                spec.insert("name".into(), Value::String(p.name));
                spec.insert("dtype".into(), Value::String(p.dtype));
                spec.insert("default".into(), default_val);
                if let Some(m) = p.method {
                    spec.insert("method".into(), Value::String(m));
                }
                Value::Object(spec)
            })
            .collect();

        let (inputs, outputs) = match kb.operator_io(&op.name) {
            Some((ins, outs)) => (
                ins.into_iter().map(io_to_json).collect::<Vec<_>>(),
                outs.into_iter().map(io_to_json).collect::<Vec<_>>(),
            ),
            None => (Vec::new(), Vec::new()),
        };

        let methods: Vec<Value> = op
            .interface
            .as_deref()
            .map(|iface| kb.method_sequence(iface))
            .unwrap_or_default()
            .into_iter()
            .map(Value::String)
            .collect();

        let mut entry = serde_json::Map::new();
        entry.insert("params".into(), Value::Array(params));
        entry.insert("methods".into(), Value::Array(methods));
        entry.insert("inputs".into(), Value::Array(inputs));
        entry.insert("outputs".into(), Value::Array(outputs));
        // Extra fields the SPA tolerates — useful for dev tooling.
        if let Some(iface) = op.interface.as_deref() {
            entry.insert("interface".into(), Value::String(iface.to_string()));
        }
        if let Some(family) = op.family.as_deref() {
            entry.insert("family".into(), Value::String(family.to_string()));
        }
        out.insert(op.name.clone(), Value::Object(entry));
    }
    out
}

fn io_to_json(io: optimizer::kb::types::IoSpec) -> Value {
    let mut m = serde_json::Map::new();
    m.insert("name".into(), Value::String(io.name));
    // Position is a String — kwarg-style ports keep their kwarg name.
    m.insert("position".into(), Value::String(io.position));
    m.insert("type".into(), Value::String(io.dtype));
    Value::Object(m)
}

async fn full_catalog(State(state): State<AppState>) -> impl IntoResponse {
    let Some(kb) = state.inner.kb.clone() else {
        return (StatusCode::SERVICE_UNAVAILABLE, "kb snapshot unavailable").into_response();
    };
    let operators: Vec<CatalogItem> = kb
        .all_operators()
        .into_iter()
        .map(|o| name_to_item(&o.name))
        .collect();
    let mut task_set: rustc_hash::FxHashSet<String> = rustc_hash::FxHashSet::default();
    for op in kb.all_operators() {
        for t in op.tasks.iter() {
            task_set.insert(t.clone());
        }
    }
    let mut tasks: Vec<CatalogItem> =
        task_set.into_iter().map(|n| name_to_item(&n)).collect();
    tasks.sort_by(|a, b| a.name.cmp(&b.name));

    let params = build_operator_params_map(&kb);

    // Fetch objectives + evals from the Python backend — these live in
    // the Neo4j KB and are not yet in the Rust KB snapshot.
    let backend = &state.inner.config.backend_url;
    let objectives = fetch_from_backend(&state.inner.http_client, backend, "catalog/objectives")
        .await
        .unwrap_or_default();
    let evals = fetch_from_backend(&state.inner.http_client, backend, "catalog/evals")
        .await
        .unwrap_or_default();

    let body = serde_json::json!({
        "operators": operators,
        "tasks":     tasks,
        "objectives": objectives,
        "evals":      evals,
        "operatorParams": Value::Object(params),
    });
    (StatusCode::OK, Json(body)).into_response()
}

/// Fetch a JSON array from a Python-backend sub-path. Returns an empty
/// vec on any error so catalog assembly is always best-effort.
async fn fetch_from_backend(
    client: &reqwest::Client,
    backend_url: &str,
    path: &str,
) -> Option<Vec<Value>> {
    let url = format!("{backend_url}/{path}");
    let resp = client.get(&url).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    resp.json::<Vec<Value>>().await.ok()
}
