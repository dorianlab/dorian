//! Event-bus endpoints — XADD producer side of the Go ``cmd/eventbus``.
//!
//! Responsibilities we take on:
//!
//!   * ``POST /emit`` — accept a JSON ``{lane, event}`` payload and
//!     ``XADD`` to the matching Redis stream (``events:user`` or
//!     ``events:bg``). Returns the assigned stream id.
//!   * ``GET /stats`` — per-lane stream depth + approximate memory
//!     usage, for operator dashboards.
//!   * ``GET /eventbus/health`` — dedicated health probe so
//!     compose can monitor the event-bus role separately from the
//!     HTTP-gateway role even though both live in this binary.
//!
//! Deliberately OUT of scope here (stays in Go until a later commit):
//!   * Consumer groups + Go-side handlers (``DORIAN_EVENTBUS_SUBSCRIBER_GO``).
//!     Python subscribers already own the consumer-group side via
//!     ``backend/events.py``; the Go-side subscriber is optional and
//!     off by default on this deployment.
//!
//! Backpressure policy mirrors the Go bus's: if stream depth
//! exceeds ``backpressure_threshold × maxlen`` (default 0.90 × 100k),
//! the producer gets HTTP 429. Python's ``aemit`` treats that as a
//! soft fail and schedules a retry; events that can't be emitted
//! don't wedge the caller.

use axum::{
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use tracing::{debug, warn};

use crate::state::AppState;

/// Convenience: XADD an `EventBody` envelope to the user lane.
/// Used by native handlers (contact, session) that need to emit
/// without going through the `/emit` HTTP route. Mirrors the
/// shape `emit_event` in `session.rs` produces.
pub async fn xadd_user(state: &AppState, body: &EventBody) {
    use redis::AsyncCommands;
    use redis::streams::StreamMaxlen;
    let cfg = &state.inner.config;
    let json = match serde_json::to_string(body) {
        Ok(s) => s,
        Err(e) => {
            warn!("xadd_user serialise failed: {e}");
            return;
        }
    };
    let mut conn = state.inner.redis.clone();
    let res: redis::RedisResult<String> = conn
        .xadd_maxlen(
            cfg.stream_user.as_str(),
            StreamMaxlen::Approx(cfg.stream_maxlen as usize),
            "*",
            &[("event", json.as_str())],
        )
        .await;
    if let Err(e) = res {
        warn!("xadd_user failed: {e}");
    }
}

/// Register all event-bus routes onto an existing router.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/emit", post(emit))
        // Renamed from ``/stats`` to ``/eventbus/stats`` on 2026-04-28
        // — the bare ``/stats`` path is the public platform-counters
        // endpoint (welcome screen, no HMAC) and the conflict made
        // axum's merge route the platform stats request through the
        // HMAC-wrapped eventbus handler. Eventbus stats now lives
        // under its own prefix alongside ``/eventbus/health``.
        .route("/eventbus/stats", get(stats))
        .route("/eventbus/health", get(eventbus_health))
}

// ---------------------------------------------------------------------------
// Request / response shapes — match the Go bus's JSON surface so existing
// Python producers (``backend/events.py::_eventbus_client``) don't change.
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
pub struct EmitRequest {
    #[serde(default)]
    pub lane: Option<String>,
    pub event: EventBody,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct EventBody {
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(default)]
    pub payload: serde_json::Value,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub timestamp: Option<f64>,
    #[serde(default)]
    pub request_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct EmitResponse {
    id: String,
}

#[derive(Debug, Serialize)]
struct ErrorResponse {
    error: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    lane: Option<String>,
}

// ---------------------------------------------------------------------------
// Lane selection
// ---------------------------------------------------------------------------

/// Resolve a ``lane`` request field to the Redis stream name + maxlen.
/// ``user`` events get priority / larger headroom; ``bg`` events are
/// the default for diagnostic / observability traffic.
fn lane_for(state: &AppState, lane: Option<&str>) -> (String, u64) {
    let cfg = &state.inner.config;
    match lane.unwrap_or("bg") {
        "user" | "User" | "USER" => (cfg.stream_user.clone(), cfg.stream_maxlen),
        _ => (cfg.stream_bg.clone(), cfg.stream_maxlen),
    }
}

// ---------------------------------------------------------------------------
// POST /emit
// ---------------------------------------------------------------------------

async fn emit(
    State(state): State<AppState>,
    Json(req): Json<EmitRequest>,
) -> impl IntoResponse {
    if req.event.event_type.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(ErrorResponse {
                error: "event.type is required".into(),
                lane: None,
            }),
        )
            .into_response();
    }

    let (stream, maxlen) = lane_for(&state, req.lane.as_deref());
    let mut redis = state.inner.redis.clone();

    // Backpressure check — cheap XLEN before the XADD. Matches Go bus's
    // pre-emit check at ``internal/eventbus/bus.go``. Skips when
    // ``maxlen == 0`` (disabled).
    if maxlen > 0 {
        match redis.xlen::<_, u64>(&stream).await {
            Ok(depth) => {
                let threshold =
                    (maxlen as f64 * state.inner.config.backpressure_threshold) as u64;
                if depth >= threshold {
                    warn!(
                        stream = %stream,
                        depth,
                        maxlen,
                        "backpressure — rejecting emit"
                    );
                    return (
                        StatusCode::TOO_MANY_REQUESTS,
                        Json(ErrorResponse {
                            error: "backpressure".into(),
                            lane: Some(stream),
                        }),
                    )
                        .into_response();
                }
            }
            Err(e) => {
                warn!(%stream, "XLEN failed, proceeding with emit: {e}");
            }
        }
    }

    // Fill bus-authoritative timestamp when the producer didn't. Keeps
    // per-entry latency measurable even when clocks drift upstream.
    let mut event = req.event;
    if event.timestamp.is_none() {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        event.timestamp = Some(now);
    }

    // Serialise the whole event body as the stream entry. Matches the Go
    // bus's ``bus.Emit`` shape: every field becomes a name/value pair
    // via JSON-round-trip. The Python consumer decodes it the same way
    // regardless of which gateway handled the emit.
    let payload_bytes = match serde_json::to_string(&event) {
        Ok(s) => s,
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorResponse {
                    error: format!("encode: {e}"),
                    lane: None,
                }),
            )
                .into_response();
        }
    };

    // MAXLEN ~ approximate trim so Redis doesn't pay the full O(n) on
    // every XADD; same default the Go bus uses.
    let maxlen_opt = redis::streams::StreamMaxlen::Approx(maxlen as usize);
    let xadd_result: redis::RedisResult<String> = redis
        .xadd_maxlen(&stream, maxlen_opt, "*", &[("event", payload_bytes.as_str())])
        .await;

    match xadd_result {
        Ok(id) => {
            debug!(%stream, %id, event_type = %event.event_type, "emitted");
            (StatusCode::OK, Json(EmitResponse { id })).into_response()
        }
        Err(e) => {
            warn!(%stream, event_type = %event.event_type, "XADD failed: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorResponse {
                    error: format!("xadd: {e}"),
                    lane: Some(stream),
                }),
            )
                .into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// GET /stats
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct LaneStats {
    stream: String,
    depth: u64,
}

#[derive(Debug, Serialize)]
struct StatsResponse {
    lanes: Vec<LaneStats>,
}

async fn stats(State(state): State<AppState>) -> impl IntoResponse {
    let mut redis = state.inner.redis.clone();
    let cfg = &state.inner.config;
    let mut lanes = Vec::with_capacity(2);
    for stream in [&cfg.stream_user, &cfg.stream_bg] {
        let depth = redis.xlen(stream).await.unwrap_or(0);
        lanes.push(LaneStats {
            stream: stream.clone(),
            depth,
        });
    }
    (StatusCode::OK, Json(StatsResponse { lanes })).into_response()
}

// ---------------------------------------------------------------------------
// GET /eventbus/health
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct EventbusHealth {
    status: &'static str,
    redis_ping_ms: Option<u128>,
}

async fn eventbus_health(State(state): State<AppState>) -> impl IntoResponse {
    let mut redis = state.inner.redis.clone();
    let t0 = std::time::Instant::now();
    let ping_ok: redis::RedisResult<String> = redis::cmd("PING").query_async(&mut redis).await;
    let elapsed = t0.elapsed().as_millis();
    let (status, ping_ms) = match ping_ok {
        Ok(_) => ("ok", Some(elapsed)),
        Err(_) => ("degraded", None),
    };
    (
        if status == "ok" {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        Json(EventbusHealth {
            status,
            redis_ping_ms: ping_ms,
        }),
    )
        .into_response()
}
