//! Redis-stream enqueue helper. Submits a trial under the same
//! `task_queue` Redis sorted-set the existing `bridge_logic`
//! reads from, but at `BACKGROUND_LOW` priority so the
//! cross-product engine never starves user-driven runs.
//!
//! Wire format mirrors what `backend.queue.submit_background`
//! produces: a JSON envelope with `pipeline_id`, `dataset_id`,
//! `_source: "xproduct"`, and a synthetic session id `xproduct:N`.
//! Existing executors don't need to be aware of the new source —
//! they read the standard payload shape.

use redis::AsyncCommands;
use serde_json::json;
use uuid::Uuid;

/// Priority constants matching `backend/queue.py::Priority`.
/// We use BACKGROUND (-1) plus a 100-offset so we sort AFTER
/// user-priority work (-10) but BEFORE the foreground SYSTEM
/// floor (-20). The exact value is admin-tunable; we only need
/// to be lower than Priority.USER.
pub const PRIORITY_BACKGROUND_LOW: i64 = 100;

pub struct TrialQueue {
    client: redis::Client,
    queue_key: String,
}

impl TrialQueue {
    pub fn new(redis_url: &str, queue_key: String) -> anyhow::Result<Self> {
        let client = redis::Client::open(redis_url)?;
        Ok(Self { client, queue_key })
    }

    /// Enqueue one trial. Returns the run_id used so callers can
    /// correlate with `evaluations.run_id` after completion.
    ///
    /// Side effect: writes `session:{session}:meta` plus the
    /// `dataset:{did}:feature_columns` / `target_columns` Redis keys
    /// so the runner's `dorian.io.dataset` expansion + state-key
    /// resolver work for engine-driven sessions. Without these the
    /// runner fails every xproduct trial at expansion with "please
    /// upload a dataset" and `KeyError('dataset.features')`.
    pub async fn enqueue(
        &self,
        pipeline_id: &str,
        dataset_id: &str,
        dataset_path: Option<&str>,
        task_type: Option<&str>,
        feature_cols: Option<&serde_json::Value>,
        target_cols: Option<&serde_json::Value>,
    ) -> anyhow::Result<String> {
        let run_id = format!("xproduct-{}", Uuid::new_v4());
        let session = format!("xproduct:{run_id}");
        let mut conn = self.client.get_multiplexed_async_connection().await?;

        // Resolve absolute fpath so the runner can open the CSV
        // regardless of CWD. Relative paths from `datasets.storage`
        // (e.g. ``datasets/<did>/foo.csv``) anchor under
        // ``/app/data/``.
        let fpath = dataset_path.map(|p| {
            if p.starts_with('/') { p.to_string() } else { format!("/app/data/{p}") }
        });
        let task_name = task_type.map(capitalise).unwrap_or_else(|| "Classification".to_string());

        // Plant the synthetic session meta the runner expects.
        let meta_key = format!("session:{session}:meta");
        let mut dataset_meta = serde_json::Map::new();
        dataset_meta.insert("did".into(), json!(dataset_id));
        if let Some(p) = &fpath {
            dataset_meta.insert("fpath".into(), json!(p));
            dataset_meta.insert("mime".into(), json!("text/csv"));
        }
        let meta_payload = json!({
            "dataset": serde_json::Value::Object(dataset_meta),
            "selectedDataScienceTask": { "id": null, "name": task_name },
            "uid": "xproduct",
            "session": session,
        }).to_string();
        let _: () = redis::cmd("SET")
            .arg(&meta_key)
            .arg(&meta_payload)
            .arg("EX")
            .arg(3600_i64)
            .query_async(&mut conn)
            .await?;

        // Plant the feature/target column lists. Persistent (no TTL)
        // because they're dataset-scoped and shared across every
        // xproduct + automl trial that ever runs against this did.
        if let Some(cols) = feature_cols {
            let _: () = redis::cmd("SET")
                .arg(format!("dataset:{dataset_id}:feature_columns"))
                .arg(cols.to_string())
                .query_async(&mut conn)
                .await?;
        }
        if let Some(cols) = target_cols {
            let _: () = redis::cmd("SET")
                .arg(format!("dataset:{dataset_id}:target_columns"))
                .arg(cols.to_string())
                .query_async(&mut conn)
                .await?;
        }

        // Bridge reads `pipelineId` (camelCase). Snake-case mirrors
        // stay in the envelope for compatibility with observability
        // handlers that read either form.
        let payload = json!({
            "uid": "xproduct",
            "session": session,
            "run_id": run_id,
            "runId": run_id,
            "pipelineId": pipeline_id,
            "pipeline_id": pipeline_id,
            "datasetId": dataset_id,
            "dataset_id": dataset_id,
            "_source": "xproduct",
        });
        let body = payload.to_string();
        // ZADD with score = priority. Lowest score pops first
        // (the bridge consumer uses ZPOPMIN), so we want a HIGH
        // numeric score to land below the user/foreground priorities
        // which use NEGATIVE scores. See backend.queue.Priority.
        let _: i64 = conn
            .zadd(&self.queue_key, body, PRIORITY_BACKGROUND_LOW)
            .await?;
        Ok(run_id)
    }
}

/// First-letter capitalisation for `selectedDataScienceTask.name`
/// ("classification" → "Classification"). Mirrors the AutoML
/// driver's helper so both engines write the same meta shape.
fn capitalise(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_ascii_uppercase().to_string() + chars.as_str(),
    }
}
