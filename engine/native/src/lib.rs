//! dorian_native — PyO3 extension module.
//!
//! Thin shim that re-exports compiled hot paths from workspace crates
//! (graph, optimizer) as a Python extension module.
//! Existing Python imports (`import dorian_native`) continue working.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::sync::{Mutex, OnceLock, RwLock};

// ═══════════════════════════════════════════════════════════════════
// GED functions (from dorian-graph)
// ═══════════════════════════════════════════════════════════════════

#[pyfunction]
#[pyo3(signature = (dag1_json, dag2_json, beam_limit=50_000))]
fn graph_edit_distance(dag1_json: &str, dag2_json: &str, beam_limit: usize) -> PyResult<usize> {
    let v1: serde_json::Value =
        serde_json::from_str(dag1_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value =
        serde_json::from_str(dag2_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let g1 = graph::ged::DagGraph::from_json(&v1);
    let g2 = graph::ged::DagGraph::from_json(&v2);

    Ok(graph::ged::graph_edit_distance(&g1, &g2, beam_limit))
}

#[pyfunction]
fn fast_distance(dag1_json: &str, dag2_json: &str) -> PyResult<usize> {
    let v1: serde_json::Value =
        serde_json::from_str(dag1_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value =
        serde_json::from_str(dag2_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;

    let g1 = graph::ged::DagGraph::from_json(&v1);
    let g2 = graph::ged::DagGraph::from_json(&v2);

    Ok(graph::ged::fast_distance(&g1, &g2))
}

/// Task-topology-aware weighted graph edit distance.
///
/// Arguments:
///   dag1_json, dag2_json: pipeline DAGs in JSON form.
///   topology_json: JSON with ``{"family_by_op": {...},
///     "family_neighbours": {...}}``. ``family_by_op`` maps
///     operator FQN → family name; ``family_neighbours`` is an
///     undirected adjacency list of family → [neighbour_family].
///   weights_json: optional JSON with any subset of
///     ``w_param_value``, ``w_param_rename``,
///     ``w_task_equivalent``, ``w_task_per_hop``,
///     ``w_unknown_task``, ``w_insert_node``, ``w_delete_node``,
///     ``w_add_edge``, ``w_delete_edge``, ``w_snippet_swap``.
///     Missing keys fall back to defaults.
///
/// Returns the weighted distance as a float.
#[pyfunction]
#[pyo3(signature = (dag1_json, dag2_json, topology_json, weights_json=None))]
fn weighted_fast_distance(
    dag1_json: &str,
    dag2_json: &str,
    topology_json: &str,
    weights_json: Option<&str>,
) -> PyResult<f64> {
    use std::sync::Arc;

    let v1: serde_json::Value = serde_json::from_str(dag1_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value = serde_json::from_str(dag2_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let g1 = graph::ged::DagGraph::from_json(&v1);
    let g2 = graph::ged::DagGraph::from_json(&v2);

    // Parse the topology JSON into a StaticTaskTopology. The shape
    // is the same struct the Rust side uses; ``serde_json`` handles
    // the JSON ↔ FxHashMap conversion as long as the types match.
    #[derive(serde::Deserialize, Default)]
    struct TopoIn {
        #[serde(default)]
        family_by_op: std::collections::HashMap<String, String>,
        #[serde(default)]
        family_neighbours: std::collections::HashMap<String, Vec<String>>,
    }
    let topo_in: TopoIn = serde_json::from_str(topology_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let mut topo = graph::weighted_ged::StaticTaskTopology::default();
    for (k, v) in topo_in.family_by_op {
        topo.assign_family(k, v);
    }
    for (fam, neigh) in topo_in.family_neighbours {
        for n in neigh {
            // ``add_edge`` is symmetric; skip self-loops to avoid
            // a double-insert that would corrupt the BFS.
            if fam != n {
                topo.add_edge(&fam, &n);
            }
        }
    }

    let weights: graph::weighted_ged::GedWeights = match weights_json {
        Some(j) => serde_json::from_str(j)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?,
        None => graph::weighted_ged::GedWeights::default(),
    };

    let topo_arc: Arc<dyn graph::weighted_ged::TaskTopology> = Arc::new(topo);
    Ok(graph::weighted_ged::weighted_fast_distance(
        &g1,
        &g2,
        &weights,
        topo_arc.as_ref(),
    ))
}

// ═══════════════════════════════════════════════════════════════════
// Primitive-op evaluator (rewrite apply path).
//
// Applies a list of declarative ``PrimitiveOp`` entries to a
// ProcessGraph and returns the resulting graph + updated mapping as
// JSON. The Python compiler in
// ``dorian/pipeline/mitigation_rewrites.py`` calls this via the
// optional ``DORIAN_USE_RUST_REWRITES`` flag, mirroring its own
// per-op evaluator. Both paths produce identical post-DAGs; the
// Rust path is the one that survives once the runner finishes
// retiring Python.
//
// Role resolution: this entry uses ``HeuristicRoleResolver`` (name-
// prefix heuristic) so the Python and Rust paths agree without a
// KB round-trip. Production wiring with ``KbTaskTopology`` /
// real KB-backed roles is a separate slice; the heuristic is what
// the Python evaluator already uses.
// ═══════════════════════════════════════════════════════════════════

#[pyfunction]
#[pyo3(signature = (pipeline_json, ops_json, mapping_json=None))]
fn apply_primitives(
    pipeline_json: &str,
    ops_json: &str,
    mapping_json: Option<&str>,
) -> PyResult<String> {
    use graph::model::ProcessGraph;
    use graph::primitive::{
        apply_ops, HeuristicRoleResolver, Mapping, PrimitiveOp,
    };

    let mut pg: ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "ProcessGraph parse failed: {e}"
        )))?;
    let ops: Vec<PrimitiveOp> = serde_json::from_str(ops_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "ops parse failed: {e}"
        )))?;
    let mut mapping: Mapping = match mapping_json {
        Some(j) => serde_json::from_str(j)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
                "mapping parse failed: {e}"
            )))?,
        None => Mapping::default(),
    };

    let roles = HeuristicRoleResolver::default();
    apply_ops(&mut pg, &ops, &mut mapping, &roles)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "apply_ops failed: {e:?}"
        )))?;

    let out = serde_json::json!({
        "graph": pg,
        "mapping": mapping,
    });
    serde_json::to_string(&out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

// ═══════════════════════════════════════════════════════════════════
// Pipeline runner — Rust-driven topology walk with Python operator
// callback. The structural piece that begins replacing
// dask.threaded.get in backend.pipeline_runner: Python submits a
// ProcessGraph + a callable, Rust walks the topology + asks Python
// to fire each Operator / Snippet node.
//
// Returns a JSON-encoded list of events:
//   {"event": "node_started" | "node_completed" | "node_failed",
//    "node_id": "...", "duration_secs": float, "error": "..." | null}
//
// This is the minimum-viable end-to-end Rust scheduling path. It is
// SEQUENTIAL (one node at a time, level-by-level); concurrent
// dispatch comes when we wire it onto directors::DataflowDirector
// + an async runtime in the backend service. For the
// equivalent-of-dask.threaded.get use case (single-process,
// single-pipeline), sequential is exactly the right behaviour.
// ═══════════════════════════════════════════════════════════════════

#[pyfunction]
fn run_pipeline(
    py: Python<'_>,
    pipeline_json: &str,
    fire: PyObject,
) -> PyResult<String> {
    use graph::model::{Node, ProcessGraph};
    use graph::topology;
    use std::time::Instant;

    let pg: ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "ProcessGraph parse failed: {e}"
        )))?;

    let order = topology::topological_sort(&pg)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "topological sort failed: {e:?}"
        )))?;

    let mut events: Vec<serde_json::Value> = Vec::with_capacity(order.len() * 2);
    let mut failed = false;

    for node_id in order {
        let node = match pg.nodes.get(&node_id) {
            Some(n) => n,
            None => continue,
        };

        // Parameters and Group nodes are engine-side: no Python
        // callback needed. Recorded as zero-duration successes so
        // the event stream still describes the full topology.
        let needs_fire = matches!(
            node, Node::Operator(_) | Node::Snippet(_)
        );
        if !needs_fire {
            events.push(serde_json::json!({
                "event": "node_completed",
                "node_id": node_id,
                "duration_secs": 0.0,
                "kind": _node_kind_name(node),
                "engine_side": true,
            }));
            continue;
        }

        if failed {
            events.push(serde_json::json!({
                "event": "node_skipped",
                "node_id": node_id,
                "reason": "upstream failure",
            }));
            continue;
        }

        events.push(serde_json::json!({
            "event": "node_started",
            "node_id": node_id,
            "kind": _node_kind_name(node),
        }));

        let inputs_json = _gather_inputs_json(&pg, &node_id);
        let payload_json = serde_json::to_string(node).unwrap_or_default();

        let t0 = Instant::now();
        let kwargs = PyDict::new(py);
        kwargs.set_item("node_id", &node_id)?;
        kwargs.set_item("payload_json", payload_json)?;
        kwargs.set_item("inputs_json", inputs_json.to_string())?;

        let call_result = fire.call(py, PyTuple::empty(py), Some(&kwargs));
        let duration = t0.elapsed().as_secs_f64();

        match call_result {
            Ok(_) => {
                events.push(serde_json::json!({
                    "event": "node_completed",
                    "node_id": node_id,
                    "duration_secs": duration,
                }));
            }
            Err(err) => {
                let msg = err.to_string();
                events.push(serde_json::json!({
                    "event": "node_failed",
                    "node_id": node_id,
                    "duration_secs": duration,
                    "error": msg,
                }));
                failed = true;
            }
        }
    }

    serde_json::to_string(&serde_json::json!({"events": events}))
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

fn _node_kind_name(node: &graph::model::Node) -> &'static str {
    match node {
        graph::model::Node::Operator(_) => "operator",
        graph::model::Node::Parameter(_) => "parameter",
        graph::model::Node::Snippet(_) => "snippet",
        graph::model::Node::Group(_) => "group",
        _ => "node",
    }
}

/// Build the JSON description of a node's incoming wires so the
/// Python ``fire`` callback can resolve which upstream node feeds
/// each input slot. Includes ``source / position / output`` so the
/// resolver knows whether to read positional or kwarg + which
/// multi-output slot.
fn _gather_inputs_json(
    pg: &graph::model::ProcessGraph,
    node_id: &str,
) -> serde_json::Value {
    let mut entries: Vec<serde_json::Value> = Vec::new();
    for e in &pg.edges {
        if e.destination != node_id {
            continue;
        }
        entries.push(serde_json::json!({
            "source": e.source,
            "position": match &e.position {
                graph::model::Position::Index(i) => serde_json::json!(i),
                graph::model::Position::Keyword(k) => serde_json::json!(k),
            },
            "output": match &e.output {
                graph::model::Position::Index(i) => serde_json::json!(i),
                graph::model::Position::Keyword(k) => serde_json::json!(k),
            },
        }));
    }
    serde_json::json!({ "inputs": entries })
}

// Force-use ``PyList`` so the import survives the dead-code lint;
// pyo3 handles the conversion for the Vec<...> case but the import
// is needed for future ``run_pipeline_streaming`` work.
#[allow(dead_code)]
fn _ensure_pylist_imported(_: &Bound<'_, PyList>) {}

// ═══════════════════════════════════════════════════════════════════
// Tier-2 Arrow-IPC intermediate cache. Singleton store keyed on
// content-addressed `CacheKey`. Surface area is:
//
//   cache_init(path, max_gb)        — open / mount the on-disk store
//   cache_compute_key(...)          — derive cache key from op + params + upstream
//   cache_get_bytes(key_hex)        — fetch Arrow IPC or opaque bytes
//   cache_put_arrow(key, bytes)     — store Arrow IPC payload
//   cache_put_opaque(key, bytes)    — store msgpack/pickle/etc payload
//   cache_classify_random_state_param(fqn) — built-in allowlist for the
//                                    forced-seed binder
//   cache_stats()                   — debug counters
//
// The Python side computes keys + decides eligibility; this module is
// pure storage + key derivation. Determinism / bypass policy lives in
// `cache::eligibility_with_incoming` which Python calls before any of
// these functions.
// ═══════════════════════════════════════════════════════════════════

fn _arrow_store_cell() -> &'static OnceLock<cache::ArrowStore> {
    static CELL: OnceLock<cache::ArrowStore> = OnceLock::new();
    &CELL
}

fn _with_store<F, T>(f: F) -> PyResult<T>
where
    F: FnOnce(&cache::ArrowStore) -> T,
{
    match _arrow_store_cell().get() {
        Some(s) => Ok(f(s)),
        None => Err(pyo3::exceptions::PyRuntimeError::new_err(
            "ArrowStore not initialised — call cache_init(path, max_gb) first",
        )),
    }
}

#[pyfunction]
#[pyo3(signature = (path=None, max_gb=None))]
fn cache_init(path: Option<&str>, max_gb: Option<u64>) -> PyResult<usize> {
    if _arrow_store_cell().get().is_some() {
        // Already initialised — caller can re-open by restarting
        // the process. Return current size on disk.
        return Ok(_arrow_store_cell().get().unwrap().len());
    }
    let mut cfg = cache::ArrowStoreConfig::from_env();
    if let Some(p) = path {
        cfg.root = std::path::PathBuf::from(p);
    }
    if let Some(gb) = max_gb {
        cfg.max_bytes = gb.saturating_mul(1024).saturating_mul(1024).saturating_mul(1024);
    }
    let store = cache::ArrowStore::new(cfg)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("ArrowStore init: {e}")))?;
    let _ = store.mount_existing();
    let n = store.len();
    let _ = _arrow_store_cell().set(store);
    Ok(n)
}

#[pyfunction]
fn cache_get_bytes<'py>(py: Python<'py>, key_hex: &str) -> PyResult<Option<Bound<'py, pyo3::types::PyBytes>>> {
    let key = match parse_cache_key_hex(key_hex) {
        Some(k) => k,
        None => return Err(pyo3::exceptions::PyValueError::new_err(
            "key_hex must be 64 hex chars",
        )),
    };
    let bytes_opt = _with_store(|s| s.get_bytes(&key))?;
    Ok(bytes_opt.map(|b| pyo3::types::PyBytes::new(py, &b)))
}

#[pyfunction]
fn cache_put_arrow(key_hex: &str, payload: &[u8]) -> PyResult<()> {
    let key = parse_cache_key_hex(key_hex).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("key_hex must be 64 hex chars")
    })?;
    _with_store(|s| s.put_bytes(key, cache::PayloadKind::Arrow, payload))?
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("cache put: {e}")))?;
    Ok(())
}

#[pyfunction]
fn cache_put_opaque(key_hex: &str, payload: &[u8]) -> PyResult<()> {
    let key = parse_cache_key_hex(key_hex).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("key_hex must be 64 hex chars")
    })?;
    _with_store(|s| s.put_bytes(key, cache::PayloadKind::Opaque, payload))?
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(format!("cache put: {e}")))?;
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (op_fqn, op_tasks, params_json, upstream_keys_hex, op_version=None, root_hash_hex=None))]
fn cache_compute_key(
    op_fqn: &str,
    op_tasks: Vec<String>,
    params_json: &str,
    upstream_keys_hex: Vec<String>,
    op_version: Option<&str>,
    root_hash_hex: Option<&str>,
) -> PyResult<String> {
    let params: Vec<(String, serde_json::Value)> =
        serde_json::from_str(params_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "params_json must be a list of [handle, value] pairs: {e}"
            ))
        })?;
    let upstream_keys: Result<Vec<cache::CacheKey>, _> = upstream_keys_hex
        .iter()
        .map(|s| parse_cache_key_hex(s).ok_or(()))
        .collect();
    let upstream_keys = upstream_keys.map_err(|_| {
        pyo3::exceptions::PyValueError::new_err("upstream_keys_hex entries must be 64-char hex")
    })?;
    let root = match root_hash_hex {
        Some(s) => Some(parse_cache_key_hex(s).ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("root_hash_hex must be 64-char hex")
        })?),
        None => None,
    };
    let inputs = cache::KeyInputs {
        op_fqn,
        op_tasks: &op_tasks,
        op_version,
        params,
        upstream_keys,
        root_content_hash: root,
    };
    Ok(cache::compute_key(&inputs).hex())
}

#[pyfunction]
fn cache_classify_random_state_param(fqn: &str) -> Option<String> {
    graph::dem::classify_random_state_param_builtin(fqn)
}

#[pyfunction]
fn cache_stats() -> PyResult<(usize, u64)> {
    _with_store(|s| (s.len(), s.total_bytes()))
}

#[pyfunction]
fn cache_evict_all() -> PyResult<()> {
    // Test/admin helper — drops the entire cache directory's contents
    // by re-creating the store at the same root. Behind a separate
    // function so we don't accidentally wire it into runtime paths.
    let cfg = match _arrow_store_cell().get() {
        Some(s) => s.cfg().clone(),
        None => return Ok(()),
    };
    if let Err(e) = std::fs::remove_dir_all(&cfg.root) {
        if e.kind() != std::io::ErrorKind::NotFound {
            return Err(pyo3::exceptions::PyOSError::new_err(format!(
                "cache evict_all: {e}"
            )));
        }
    }
    let _ = std::fs::create_dir_all(&cfg.root);
    Ok(())
}

fn parse_cache_key_hex(s: &str) -> Option<cache::CacheKey> {
    if s.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        let hi = hex_nibble(s.as_bytes()[i * 2])?;
        let lo = hex_nibble(s.as_bytes()[i * 2 + 1])?;
        *byte = (hi << 4) | lo;
    }
    Some(cache::CacheKey(out))
}

fn hex_nibble(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

// ═══════════════════════════════════════════════════════════════════
// KB snapshot (Neo4j replacement). The process holds at most one
// loaded snapshot in a global ``RwLock`` — every ``kb_*`` query
// reads it. The ``kb_load_snapshot`` setter is the one writer; it's
// called once at backend startup with a JSON payload produced by
// ``scripts/export_kb_snapshot.py``.
// ═══════════════════════════════════════════════════════════════════

fn _kb_cell() -> &'static RwLock<Option<optimizer::kb::KbSnapshot>> {
    static CELL: OnceLock<RwLock<Option<optimizer::kb::KbSnapshot>>> = OnceLock::new();
    CELL.get_or_init(|| RwLock::new(None))
}

fn _with_kb<F, T>(f: F) -> PyResult<T>
where
    F: FnOnce(&optimizer::kb::KbSnapshot) -> T,
{
    let guard = _kb_cell()
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("kb lock poisoned"))?;
    match guard.as_ref() {
        Some(snap) => Ok(f(snap)),
        None => Err(pyo3::exceptions::PyRuntimeError::new_err(
            "KB snapshot not loaded — call dorian_native.kb_load_snapshot first",
        )),
    }
}

/// Load a JSON-serialised KB snapshot into the process-wide cell.
/// Idempotent: subsequent calls replace the previous snapshot.
#[pyfunction]
fn kb_load_snapshot(snapshot_json: &str) -> PyResult<()> {
    let snap = optimizer::kb::KbSnapshot::from_json(snapshot_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "snapshot parse failed: {e}"
        )))?;
    let mut guard = _kb_cell()
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("kb lock poisoned"))?;
    *guard = Some(snap);
    Ok(())
}

/// Parse curated DSL blobs into raw ``(subject, predicate, object)``
/// triples plus parse errors. Used by python callers that need
/// predicate-level walking the snapshot doesn't expose (MCP tools
/// reading ``might_introduce`` / ``with_description`` / etc.).
///
/// Returns ``{"triples": [{"subject", "predicate", "object"}, ...],
/// "errors": [...]}`` JSON. Errors don't abort parsing — every bad
/// line is surfaced separately so callers can decide policy.
#[pyfunction]
fn kb_parse(sources: Vec<(String, String)>) -> PyResult<String> {
    let mut all_triples = Vec::new();
    let mut all_errors = Vec::new();
    for (label, text) in &sources {
        let (triples, errors) = optimizer::kb::parse_statements(text, label);
        all_triples.extend(triples);
        all_errors.extend(errors);
    }
    let payload = serde_json::json!({
        "triples": all_triples.iter().map(|t| serde_json::json!({
            "subject": t.subject,
            "predicate": t.predicate,
            "object": t.object,
        })).collect::<Vec<_>>(),
        "errors": all_errors.iter().map(|e| serde_json::json!({
            "source": e.source,
            "line_no": e.line_no,
            "line": e.line,
            "message": e.message,
        })).collect::<Vec<_>>(),
    });
    serde_json::to_string(&payload)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

/// Build a KB snapshot from one or more curated DSL blobs. Used by
/// ``scripts/export_kb_snapshot.py`` — replaces the
/// python ``OntologyKB`` walker. Returns ``{"snapshot": ..., "errors":
/// [{source, line_no, line, message}, ...]}`` JSON.
///
/// ``sources`` is a list of ``[label, text]`` pairs. ``label`` is
/// surfaced in error reports (typically a file path). All statements
/// that fail to parse are collected and returned alongside the
/// snapshot — fail-fast was rejected as too brittle for community-
/// curated content.
#[pyfunction]
fn kb_build_snapshot(sources: Vec<(String, String)>) -> PyResult<String> {
    let pairs: Vec<(&str, &str)> = sources
        .iter()
        .map(|(label, text)| (label.as_str(), text.as_str()))
        .collect();
    let (snap, errors) = optimizer::kb::build_snapshot(&pairs);
    let payload = serde_json::json!({
        "snapshot": snap,
        "errors": errors.iter().map(|e| serde_json::json!({
            "source": e.source,
            "line_no": e.line_no,
            "line": e.line,
            "message": e.message,
        })).collect::<Vec<_>>(),
    });
    serde_json::to_string(&payload)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

// ``extract_python_ast`` and ``extract_pipeline`` PyO3 bindings
// were retired with the Ptolemy II model redesign. Pipeline
// extraction is core/orchestration — handled rust-natively in
// ``engine/backend/src/handlers/extraction.rs`` (subscribes to
// ``ExtractPipeline`` events, calls the ``extractor`` crate
// directly, persists to ``doc_extractions``). Python no longer
// needs to invoke the extractor; whatever consumed the bindings
// has been deleted.

/// Whether a snapshot is currently loaded. Cheap probe used by the
/// Python opt-in path to fall back to Neo4j when the snapshot is
/// missing.
#[pyfunction]
fn kb_is_loaded() -> bool {
    _kb_cell()
        .read()
        .map(|g| g.is_some())
        .unwrap_or(false)
}

#[pyfunction]
fn kb_operator_interface(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.operator_interface(fqn).map(|v| v.to_string()))
}

#[pyfunction]
fn kb_operator_family(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.operator_family(fqn))
}

#[pyfunction]
fn kb_model_family(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.model_family(fqn))
}

#[pyfunction]
fn kb_operator_parameters(fqn: &str) -> PyResult<String> {
    _with_kb(|s| {
        serde_json::to_string(&s.operator_parameters(fqn)).unwrap_or_else(|_| "[]".to_string())
    })
}

#[pyfunction]
fn kb_operator_io(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| {
        s.operator_io(fqn).and_then(|v| serde_json::to_string(&v).ok())
    })
}

#[pyfunction]
fn kb_operator_import_path(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.operator_import_path(fqn).map(|v| v.to_string()))
}

#[pyfunction]
fn kb_operator_risks(fqn: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.operator_risks(fqn))
}

#[pyfunction]
fn kb_metric_display_name(fqn: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.metric_display_name(fqn).map(|v| v.to_string()))
}

#[pyfunction]
fn kb_method_sequence(iface: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.method_sequence(iface))
}

#[pyfunction]
fn kb_interface_io(iface: &str) -> PyResult<Option<String>> {
    _with_kb(|s| {
        s.interface_io(iface).and_then(|v| serde_json::to_string(&v).ok())
    })
}

#[pyfunction]
fn kb_method_io(iface: &str) -> PyResult<String> {
    _with_kb(|s| serde_json::to_string(&s.method_io(iface)).unwrap_or_else(|_| "{}".to_string()))
}

#[pyfunction]
fn kb_interface_attributes(iface: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.interface_attributes(iface))
}

#[pyfunction]
fn kb_all_operators() -> PyResult<String> {
    _with_kb(|s| serde_json::to_string(&s.all_operators()).unwrap_or_else(|_| "[]".to_string()))
}

#[pyfunction]
fn kb_operators_for_task(task: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.operators_for_task(task))
}

#[pyfunction]
fn kb_operators_by_interface(iface: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.operators_by_interface(iface))
}

#[pyfunction]
fn kb_all_interface_methods() -> PyResult<Vec<String>> {
    _with_kb(|s| s.all_interface_methods().to_vec())
}

#[pyfunction]
fn kb_library_package_map() -> PyResult<String> {
    _with_kb(|s| {
        serde_json::to_string(s.library_package_map()).unwrap_or_else(|_| "{}".to_string())
    })
}

#[pyfunction]
fn kb_metrics_for_task(task: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.metrics_for_task(task))
}

#[pyfunction]
fn kb_sensitive_families_for_risk(risk: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.sensitive_families_for_risk(risk))
}

#[pyfunction]
fn kb_risks_surfaced_by_metric(metric: &str) -> PyResult<Vec<String>> {
    _with_kb(|s| s.risks_surfaced_by_metric(metric))
}

#[pyfunction]
fn kb_all_pathways() -> PyResult<String> {
    _with_kb(|s| serde_json::to_string(s.all_pathways()).unwrap_or_else(|_| "[]".to_string()))
}

#[pyfunction]
fn kb_mitigation_spec(name: &str) -> PyResult<Option<String>> {
    _with_kb(|s| s.mitigation_spec(name).and_then(|v| serde_json::to_string(v).ok()))
}

#[pyfunction]
fn kb_mitigations_for_risk(risk: &str) -> PyResult<String> {
    _with_kb(|s| {
        serde_json::to_string(&s.mitigations_for_risk(risk)).unwrap_or_else(|_| "[]".to_string())
    })
}

/// Pattern-match a rewrite rule's LHS against a pipeline.
///
/// Mirrors ``dorian/pipeline/parser.py::match`` semantics: regex on
/// pattern Node type/text/language, Operator → name match, Parameter →
/// type-only match, Snippet never matches. Returns the first
/// successful pattern-id → graph-id mapping or ``None`` if no
/// candidate satisfies the edge constraints.
///
/// ``pattern_json`` and ``dag_json`` are both ``ProcessGraph`` JSON
/// (nodes carry the ``class_type`` discriminator). ``processed_json``
/// is an optional list of mappings already returned in earlier
/// rounds, used by ``apply()`` to avoid re-emitting the same
/// candidate when a rule fires repeatedly.
#[pyfunction]
#[pyo3(signature = (pattern_json, dag_json, processed_json=None))]
fn match_pattern(
    pattern_json: &str,
    dag_json: &str,
    processed_json: Option<&str>,
) -> PyResult<Option<String>> {
    use graph::model::ProcessGraph;
    use graph::rewrite::{match_rule, Mapping};

    let pattern: ProcessGraph = serde_json::from_str(pattern_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "pattern parse failed: {e}"
        )))?;
    let dag: ProcessGraph = serde_json::from_str(dag_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
            "dag parse failed: {e}"
        )))?;
    let processed: Vec<Mapping> = match processed_json {
        Some(j) => serde_json::from_str(j)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
                "processed parse failed: {e}"
            )))?,
        None => Vec::new(),
    };

    match match_rule(&pattern, &dag, &processed) {
        Some(mapping) => {
            let s = serde_json::to_string(&mapping)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            Ok(Some(s))
        }
        None => Ok(None),
    }
}

// ═══════════════════════════════════════════════════════════════════
// Pipeline pyclass — opaque Rust handle around ``ProcessGraph``.
//
// The "DAG" framing in Dorian's earlier era is gone — pipelines are
// Ptolemy II-style process graphs (Actor lifecycle, port-typed
// channels, hierarchical directors). The python ``dorian.dag.DAG``
// is a legacy shim; the rust ``ProcessGraph`` is the actual data
// model and this pyclass exposes it directly so the rewrite path
// can move from "JSON-marshal per call" to "shared rust handle"
// without round-tripping through the python class on every step.
//
// Python builds a Pipeline once per rewrite (one marshal in), runs
// the entire ``sync_apply_rule`` loop against the in-process
// ``ProcessGraph`` (zero marshalling during the loop), and pulls the
// JSON back out at the end (one marshal out). Total cost per rule
// is constant in primitive count + match iteration count — the
// previous design paid that cost per primitive AND per match.
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════
// RuleIndex pyclass — compiled rule set with FQN dispatch.
//
// Builds once from a list of rule JSON docs (each carrying
// ``id``/``target_fqn``/``pattern``/``transformations``); thereafter
// ``match_pipeline`` walks a ``Pipeline`` once and returns every
// (rule_id, mapping) pair that fires. Replaces N independent
// ``sync_apply`` scans with one O(N) hash-dispatched sweep.
// ═══════════════════════════════════════════════════════════════════

#[pyclass]
struct RuleIndex {
    inner: graph::rule_index::RuleIndex,
}

#[pymethods]
impl RuleIndex {
    /// Build from a JSON array of compiled rule docs. Each entry:
    /// ``{"id": "...", "target_fqn": "..." | null, "pattern": <ProcessGraph>,
    /// "transformations": [<PrimitiveOp>, ...]}``.
    #[new]
    fn new(rules_json: &str) -> PyResult<Self> {
        let rules: Vec<graph::rule_index::CompiledRule> = serde_json::from_str(rules_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
                "rule index parse failed: {e}"
            )))?;
        Ok(Self { inner: graph::rule_index::RuleIndex::build(rules) })
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    /// Match every indexed rule against the pipeline. Returns a JSON
    /// list of ``[rule_id, mapping_dict]`` pairs in iteration order.
    /// Uses the pipeline's cached ``OpSet`` so rules targeting FQNs
    /// the pipeline doesn't contain short-circuit at zero cost —
    /// big win for off-domain pipelines (sklearn-only graphs
    /// evaluated against the LLM-guard rule set).
    fn match_pipeline(&self, pipeline: &mut Pipeline) -> PyResult<String> {
        // Build the op-set up front so the immutable + mutable
        // borrows on ``pipeline`` don't overlap. The set is cached
        // on the pipeline so subsequent calls cost zero.
        pipeline.ensure_op_set();
        let inner_ref = &pipeline.inner;
        let op_set_ref = pipeline.op_set.as_ref().unwrap();
        let hits = self.inner.match_with_prefilter(inner_ref, op_set_ref);
        let payload: Vec<serde_json::Value> = hits
            .into_iter()
            .map(|(rid, m)| {
                serde_json::json!([rid, m])
            })
            .collect();
        serde_json::to_string(&payload)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
    }

    /// Rule IDs whose pattern targets the given FQN. ``O(1)`` —
    /// useful for the AI Debugger's per-node mitigation lookup.
    fn rules_for_fqn(&self, fqn: &str) -> PyResult<Vec<String>> {
        Ok(self
            .inner
            .rules_for_fqn(fqn)
            .into_iter()
            .map(|r| r.id.clone())
            .collect())
    }

    /// Iteratively apply rules to *pipeline* until no rule fires.
    /// After each fire, the next iteration only re-checks rules
    /// whose target FQN intersects the just-produced FQNs — every
    /// other rule is statically known not to have a fresh match.
    ///
    /// Returns the ordered list of (rule_id, mapping) for each fire,
    /// JSON-encoded. The pipeline is mutated in place; the cached
    /// op-set is invalidated after each fire.
    fn apply_to_fixpoint(&self, pipeline: &mut Pipeline) -> PyResult<String> {
        let history = self.inner.apply_to_fixpoint(&mut pipeline.inner);
        if !history.is_empty() {
            pipeline.op_set = None;
        }
        let payload: Vec<serde_json::Value> = history
            .into_iter()
            .map(|(rid, m)| serde_json::json!([rid, m]))
            .collect();
        serde_json::to_string(&payload)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
    }
}

#[pyclass]
#[derive(Clone)]
struct Pipeline {
    inner: graph::model::ProcessGraph,
    /// Cached operator-FQN set. Built lazily on first ``RuleIndex``
    /// dispatch, invalidated when the graph mutates
    /// (``sync_apply_rule``). Lets every subsequent rule-index call
    /// against this pipeline skip the per-node walk.
    op_set: Option<graph::rule_index::OpSet>,
}

impl Pipeline {
    fn ensure_op_set(&mut self) {
        if self.op_set.is_none() {
            self.op_set = Some(graph::rule_index::OpSet::from_pipeline(&self.inner));
        }
    }
}

#[pymethods]
impl Pipeline {
    /// Build a pipeline from ``ProcessGraph`` JSON. Used at the
    /// boundary where a python ``DAG`` is still the source — the
    /// python side serialises once via ``_dag_to_pg_json`` and
    /// hands the string here.
    #[new]
    fn new(json: &str) -> PyResult<Self> {
        let inner: graph::model::ProcessGraph = serde_json::from_str(json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!(
                "pipeline parse failed: {e}"
            )))?;
        Ok(Self { inner, op_set: None })
    }

    /// Round-trip JSON for callers that still need a python-side
    /// view of the graph (legacy ``DAG`` consumers, observability,
    /// tests).
    fn to_json(&self) -> PyResult<String> {
        serde_json::to_string(&self.inner)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
    }

    /// Number of nodes — cheap probe used by tests + observability
    /// (avoids paying ``to_json`` just to count).
    fn node_count(&self) -> usize {
        self.inner.nodes.len()
    }

    /// Number of edges.
    fn edge_count(&self) -> usize {
        self.inner.edges.len()
    }

    /// Pattern-match against this pipeline using *pattern* (also a
    /// ``Pipeline`` — the rewrite rule's LHS, pre-built once and
    /// reused across calls). Zero marshalling: both sides are
    /// already in-process ``ProcessGraph`` instances, so the match
    /// runs against rust-native data and only the result mapping
    /// is serialised on the way out.
    ///
    /// Returns ``None`` when no candidate satisfies the pattern, or
    /// the JSON-encoded ``Mapping`` when one does. ``processed_json``
    /// is the optional already-tried mappings list (used by the
    /// surrounding ``apply()`` loop to avoid re-emitting the same
    /// candidate).
    #[pyo3(signature = (pattern, processed_json=None))]
    fn match_pattern(
        &self,
        pattern: &Pipeline,
        processed_json: Option<&str>,
    ) -> PyResult<Option<String>> {
        use graph::rewrite::{match_rule, Mapping};
        let processed: Vec<Mapping> = match processed_json {
            Some(j) => serde_json::from_str(j).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("processed parse failed: {e}"))
            })?,
            None => Vec::new(),
        };
        match match_rule(&pattern.inner, &self.inner, &processed) {
            Some(m) => {
                let s = serde_json::to_string(&m).map_err(|e| {
                    pyo3::exceptions::PyValueError::new_err(e.to_string())
                })?;
                Ok(Some(s))
            }
            None => Ok(None),
        }
    }

    /// Run the entire ``sync_apply`` loop on the in-process pipeline.
    ///
    /// ``rule_json`` shape::
    ///
    ///   {
    ///     "pattern": <ProcessGraph>,
    ///     "transformations": [<PrimitiveOp>, ...]
    ///   }
    ///
    /// Repeats: match pattern against the current graph; if a fresh
    /// candidate is found, run every primitive op against the graph
    /// + mapping, push the candidate to ``processed`` so the same
    /// match doesn't re-fire, and try again. Exits when no further
    /// match. The ``Pipeline`` mutates in place — callers wanting
    /// the pre-rewrite state should ``clone`` first.
    ///
    /// Returns ``True`` if at least one match fired. Useful for the
    /// python caller to short-circuit observability emits.
    fn sync_apply_rule(&mut self, rule_json: &str) -> PyResult<bool> {
        use graph::primitive::{apply_ops, HeuristicRoleResolver, Mapping, PrimitiveOp};
        use graph::rewrite::match_rule;

        #[derive(serde::Deserialize)]
        struct RuleSpec {
            pattern: graph::model::ProcessGraph,
            #[serde(default)]
            transformations: Vec<PrimitiveOp>,
        }

        let rule: RuleSpec = serde_json::from_str(rule_json).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("rule parse failed: {e}"))
        })?;

        let roles = HeuristicRoleResolver::default();
        let mut processed: Vec<Mapping> = Vec::new();
        let mut fired = false;
        loop {
            let candidate = match match_rule(&rule.pattern, &self.inner, &processed) {
                Some(m) => m,
                None => break,
            };
            if processed.contains(&candidate) {
                break;
            }
            let mut mapping = candidate.clone();
            // Apply primitives — silent no-op on Ambiguous{Source,Destination}
            // matches the python contract for unsatisfiable selectors.
            match apply_ops(&mut self.inner, &rule.transformations, &mut mapping, &roles) {
                Ok(()) => {}
                Err(e) => {
                    let msg = format!("{e:?}");
                    if !msg.contains("Ambiguous") {
                        return Err(pyo3::exceptions::PyValueError::new_err(format!(
                            "apply_ops failed: {msg}"
                        )));
                    }
                }
            }
            processed.push(candidate);
            fired = true;
        }
        // The graph mutated — drop the cached op-set so the next
        // ``RuleIndex.match_pipeline`` call rebuilds it.
        if fired {
            self.op_set = None;
        }
        Ok(fired)
    }
}

/// Expand every ``dorian.io.dataset`` placeholder in ``pipeline_json``.
/// Returns the rewritten pipeline JSON. Pure function — meta is
/// supplied by the python caller (which assembles fpath / loader /
/// features / target from session redis state).
///
/// ``meta_json`` shape::
///
///     {
///         "fpath":    "/path/to/data.csv",
///         "loader":   "pandas.read_csv",
///         "features": ["sepal_length", "sepal_width"],   // optional
///         "target":   "species" | ["species"] | ""        // optional
///     }
///
/// First pass of the python-to-rust ``expand_*`` chain port (task #72).
/// Subsequent passes will move ``expand_state_refs``,
/// ``expand_categorical_encoding``, ``expand_compound_operators``
/// and ``expand_printout_nodes`` to the same module.
#[pyfunction]
fn expand_dataset_refs(pipeline_json: &str, meta_json: &str) -> PyResult<String> {
    use graph::expand::{expand_dataset_refs as core_expand, DatasetMeta, TargetSpec};

    let graph: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {e}")))?;
    let meta_v: serde_json::Value = serde_json::from_str(meta_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("meta_json: {e}")))?;
    let target = match meta_v.get("target") {
        Some(serde_json::Value::String(s)) if !s.is_empty() => TargetSpec::Single(s.clone()),
        Some(serde_json::Value::Array(a)) if !a.is_empty() => TargetSpec::Many(
            a.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect(),
        ),
        _ => TargetSpec::None,
    };
    let features: Vec<String> = meta_v
        .get("features")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();
    let meta = DatasetMeta {
        fpath: meta_v
            .get("fpath")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        loader: meta_v
            .get("loader")
            .and_then(|v| v.as_str())
            .unwrap_or("pandas.read_csv")
            .to_string(),
        features,
        target,
    };
    let expanded = core_expand(graph, &meta);
    serde_json::to_string(&expanded)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

/// Substitute ``dorian.io.state`` placeholders with pre-resolved
/// Parameter nodes. The python caller assembles the resolution list
/// (running the allowlisted resolver functions against session
/// meta + dataset Redis keys) and passes it as JSON; the rust side
/// does pure graph mutation.
///
/// ``resolutions_json`` shape::
///
///     [{
///         "node_id": "<placeholder id>",
///         "key":     "dataset.features",
///         "dtype":   "str" | "eval" | ...,
///         "value":   "<repr or string>"
///     }, ...]
///
/// Placeholder shapes handled:
///   * legacy ``Operator(dorian.io.state)`` + incoming
///     ``Parameter(name="key" / "dataset")`` — both consumed.
///   * compact ``Parameter(name="dorian.io.state", dtype="state")``
///     standing alone — only itself consumed.
#[pyfunction]
fn expand_state_refs(pipeline_json: &str, resolutions_json: &str) -> PyResult<String> {
    use graph::expand::{expand_state_refs as core_expand, ResolvedState};

    let graph: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {e}")))?;
    let raw: Vec<serde_json::Value> = serde_json::from_str(resolutions_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("resolutions_json: {e}")))?;
    let resolutions: Vec<ResolvedState> = raw
        .iter()
        .filter_map(|v| {
            Some(ResolvedState {
                node_id: v.get("node_id")?.as_str()?.to_string(),
                key: v.get("key")?.as_str()?.to_string(),
                dtype: v.get("dtype")?.as_str()?.to_string(),
                value: v.get("value")?.as_str()?.to_string(),
            })
        })
        .collect();
    let expanded = core_expand(graph, &resolutions);
    serde_json::to_string(&expanded)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

/// Replace every ``dorian.io.printout`` operator with the type-
/// detecting display ``Snippet``. Pure graph mutation — no inputs
/// beyond the pipeline JSON.
#[pyfunction]
fn expand_printout_nodes(pipeline_json: &str) -> PyResult<String> {
    use graph::expand::expand_printout_nodes as core_expand;

    let graph: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {e}")))?;
    let expanded = core_expand(graph);
    serde_json::to_string(&expanded)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

/// Insert ``OrdinalEncoder`` upstream of every ``train_test_split``
/// when ``should_insert`` is true and no encoding op is already
/// present. The python facade decides whether to insert (``force``
/// override OR ``profile.NumberOfCategoricalFeatures > 0``) so the
/// rust path stays I/O-free.
#[pyfunction]
fn expand_categorical_encoding(pipeline_json: &str, should_insert: bool) -> PyResult<String> {
    use graph::expand::expand_categorical_encoding as core_expand;

    let graph: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {e}")))?;
    let expanded = core_expand(graph, should_insert);
    serde_json::to_string(&expanded)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

/// Expand class-interface compound operators (sklearn transformers,
/// estimators, …) into their internal method sub-DAG. The python
/// facade pre-resolves all KB look-ups (interface, method sequence,
/// per-method I/O, parameter routing) and packs them into the
/// ``records_json`` array; this rust path does pure graph mutation.
///
/// ``records_json`` is a JSON list of:
///   {
///     "node_id": str,
///     "methods": [str, ...],                    // deduped chain, __init__ first
///     "kb_params": [[name, target_method], ...],
///     "interface_inputs": [[name, ext_pos], ...],
///     "method_io": [{
///         "method": str,
///         "inputs":  [[name, internal_pos], ...],
///         "outputs": [[name, position],     ...],   // order = output index
///     }, ...],
///   }
///
/// Records that fall outside the rust-supported subset (passthrough
/// interfaces, missing method I/O, etc.) are filtered out by the
/// python facade — so the residual python `sync_apply` pass picks
/// them up. Rust-already-expanded methods carry ``_cx_`` in the id
/// and a KB-known shortcut name, both of which trip the python rule's
/// guards.
#[pyfunction]
fn expand_compound_operators(pipeline_json: &str, records_json: &str) -> PyResult<String> {
    use graph::expand::{expand_compound_operators as core_expand, CompoundRecord, MethodIo};

    let graph: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {e}")))?;

    #[derive(serde::Deserialize)]
    struct RecJ {
        node_id: String,
        methods: Vec<String>,
        kb_params: Vec<(String, String)>,
        interface_inputs: Vec<(String, i64)>,
        method_io: Vec<MioJ>,
    }
    #[derive(serde::Deserialize)]
    struct MioJ {
        method: String,
        inputs: Vec<(String, i64)>,
        outputs: Vec<(String, i64)>,
    }

    let raw: Vec<RecJ> = serde_json::from_str(records_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("records_json: {e}")))?;

    let records: Vec<CompoundRecord> = raw
        .into_iter()
        .map(|r| CompoundRecord {
            node_id: r.node_id,
            methods: r.methods,
            kb_params: r.kb_params,
            interface_inputs: r.interface_inputs,
            method_io: r
                .method_io
                .into_iter()
                .map(|m| MethodIo {
                    method: m.method,
                    inputs: m.inputs,
                    outputs: m.outputs,
                })
                .collect(),
        })
        .collect();

    let expanded = core_expand(graph, &records);
    serde_json::to_string(&expanded)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("serialise: {e}")))
}

#[pyfunction]
fn extract_operator_names(dag_json: &str) -> PyResult<Vec<String>> {
    let v: serde_json::Value =
        serde_json::from_str(dag_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(graph::ged::extract_operator_names(&v))
}

/// Structural + type-level pipeline validation.
///
/// Consumed by the RL env's ``_evaluate_terminal`` path and the AI
/// Debugger's risk-check path. Returns ``None`` on a well-formed
/// pipeline; returns a JSON list of typed ``ValidationError`` entries
/// otherwise. Each error is already a metadata-rich leaf identity
/// clustered by its ``kind`` field — see
/// ``engine/graph/src/validator.rs`` for the variant list.
///
/// Arguments:
///   pipeline_json:  the full DAG JSON (format produced by
///                   ``DAG.to_json_dict()`` / ``_node_to_shadow_dict``).
///   signatures_json: operator → {inputs, outputs} signature registry,
///                    sourced from the KB at engine init.
///   sink_node_id:    optional node id of the designated sink (e.g.
///                    the metric operator in the RL frozen harness).
///                    When provided, reachability-to-sink is checked.
#[pyfunction]
#[pyo3(signature = (pipeline_json, signatures_json, sink_node_id=None))]
fn validate_pipeline(
    pipeline_json: &str,
    signatures_json: &str,
    sink_node_id: Option<String>,
) -> PyResult<Option<String>> {
    let dag: graph::ProcessGraph = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("pipeline_json: {}", e)))?;
    let sigs: graph::SignatureRegistry = {
        let map: std::collections::HashMap<String, graph::OperatorSig> =
            serde_json::from_str(signatures_json).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("signatures_json: {}", e))
            })?;
        let mut fx = graph::SignatureRegistry::default();
        for (k, v) in map {
            fx.insert(k, v);
        }
        fx
    };
    let sink = sink_node_id.as_ref();
    match graph::validate_pipeline(&dag, &sigs, sink) {
        Ok(()) => Ok(None),
        Err(errs) => {
            let out = serde_json::to_string(&errs).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("serialize errors: {}", e))
            })?;
            Ok(Some(out))
        }
    }
}

/// Minimum-signal edit path turning ``dag1`` into ``dag2``.
///
/// Returns JSON: ``{ops: [...], strategy: "id_diff"|"name_diff", truncated: bool}``.
/// ID-keyed diff is exact in O(|V|+|E|) when the two graphs share node IDs
/// (the common case after a user-correction on the canvas); name-keyed
/// fallback matches on (type, text) fingerprints.
#[pyfunction]
#[pyo3(signature = (dag1_json, dag2_json, max_ops=200))]
fn graph_edit_path(dag1_json: &str, dag2_json: &str, max_ops: usize) -> PyResult<String> {
    let v1: serde_json::Value = serde_json::from_str(dag1_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let v2: serde_json::Value = serde_json::from_str(dag2_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let g1 = graph::ged::DagGraph::from_json(&v1);
    let g2 = graph::ged::DagGraph::from_json(&v2);
    let res = graph::ged::graph_edit_path(&g1, &g2, max_ops);

    let ops_val: Vec<serde_json::Value> =
        res.ops.iter().map(|op| op.to_json_value()).collect();
    let out = serde_json::json!({
        "ops": ops_val,
        "strategy": res.strategy,
        "truncated": res.truncated,
    });
    Ok(out.to_string())
}

// ═══════════════════════════════════════════════════════════════════
// BK-Tree (from dorian-graph)
// ═══════════════════════════════════════════════════════════════════

#[pyclass]
struct BKTree {
    inner: Mutex<graph::bktree::PipelineBKTree>,
}

#[pymethods]
impl BKTree {
    #[new]
    #[pyo3(signature = (use_exact_ged=true, beam_limit=50_000))]
    fn new(use_exact_ged: bool, beam_limit: usize) -> Self {
        BKTree {
            inner: Mutex::new(graph::bktree::PipelineBKTree::new(use_exact_ged, beam_limit)),
        }
    }

    #[getter]
    fn size(&self) -> usize {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    fn contains(&self, pipeline_id: &str) -> bool {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).contains(pipeline_id)
    }

    fn add(&self, pipeline_id: &str, dag_json: &str) -> bool {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).add(pipeline_id, dag_json)
    }

    #[pyo3(signature = (dag_json, max_distance=5))]
    fn query(&self, dag_json: &str, max_distance: usize) -> Vec<(String, usize)> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).query(dag_json, max_distance)
    }

    #[pyo3(signature = (dag_json, k=5, max_distance=10))]
    fn find_nearest(&self, dag_json: &str, k: usize, max_distance: usize) -> Vec<(String, usize)> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).find_nearest(dag_json, k, max_distance)
    }
}

// ═══════════════════════════════════════════════════════════════════
// Shadow Execution (from directors + graph)
// ═══════════════════════════════════════════════════════════════════

/// Validate a pipeline JSON and build a Rust execution plan.
///
/// Returns a JSON dict with:
/// - `valid`: bool — whether the graph parsed and validated successfully
/// - `node_count`: int — number of nodes in the graph
/// - `levels`: list[list[str]] — execution levels (nodes grouped by depth)
/// - `sink_nodes`: list[str] — leaf nodes (no outgoing edges)
/// - `topo_order`: list[str] — topological sort order
/// - `max_concurrency`: int — maximum parallelism at any level
/// - `depth`: int — number of execution levels
/// - `runtime_map`: dict[str, str] — node_id → runtime kind (Python, Api, Engine)
/// - `errors`: list[str] — validation errors (if any)
/// - `parse_time_ms`: float — time to parse JSON → ProcessGraph
/// - `plan_time_ms`: float — time to compute execution plan
///
/// This function is used by the shadow engine: Python calls it with the
/// pipeline JSON after expansion, and compares the Rust plan against the
/// Dask execution to catch discrepancies.
#[pyfunction]
fn shadow_validate_plan(pipeline_json: &str) -> PyResult<String> {
    use std::time::Instant;

    let t0 = Instant::now();

    // Parse JSON string into a serde_json::Value.
    let pipeline_val: serde_json::Value = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {e}")))?;

    // Build ProcessGraph from JSON.
    let graph_result = graph::model::ProcessGraph::from_json(&pipeline_val);
    let parse_ms = t0.elapsed().as_secs_f64() * 1000.0;

    let graph = match graph_result {
        Ok(g) => g,
        Err(e) => {
            let result = serde_json::json!({
                "valid": false,
                "errors": [format!("ProcessGraph parse error: {e}")],
                "parse_time_ms": parse_ms,
                "plan_time_ms": 0.0,
            });
            return Ok(result.to_string());
        }
    };

    // Run structural validation.
    let mut errors: Vec<String> = Vec::new();
    if let Err(validation_errors) = graph::topology::validate(&graph) {
        for e in validation_errors {
            errors.push(format!("Validation: {e}"));
        }
    }

    // Build execution plan.
    let t1 = Instant::now();
    let plan_result = directors::ExecutionPlan::from_graph(&graph);
    let plan_ms = t1.elapsed().as_secs_f64() * 1000.0;

    match plan_result {
        Ok(plan) => {
            // Build runtime map: node_id → runtime kind string.
            let mut runtime_map = serde_json::Map::new();
            for (node_id, node) in &graph.nodes {
                let kind = directors::resolve_runtime(node);
                runtime_map.insert(
                    node_id.clone(),
                    serde_json::Value::String(format!("{:?}", kind)),
                );
            }

            let result = serde_json::json!({
                "valid": errors.is_empty(),
                "node_count": plan.node_count,
                "levels": plan.levels,
                "sink_nodes": plan.sink_nodes,
                "topo_order": plan.topo_order,
                "max_concurrency": plan.max_concurrency(),
                "depth": plan.depth(),
                "runtime_map": runtime_map,
                "errors": errors,
                "parse_time_ms": parse_ms,
                "plan_time_ms": plan_ms,
            });
            Ok(result.to_string())
        }
        Err(e) => {
            errors.push(format!("Execution plan error: {e}"));
            let result = serde_json::json!({
                "valid": false,
                "node_count": graph.nodes.len(),
                "errors": errors,
                "parse_time_ms": parse_ms,
                "plan_time_ms": plan_ms,
            });
            Ok(result.to_string())
        }
    }
}

/// Compute the graph edit distance between two pipeline DAGs using the
/// Rust execution plan, returning a structural comparison.
///
/// Returns a JSON dict with comparison fields:
/// - `node_count_match`: bool
/// - `sink_match`: bool
/// - `level_count_match`: bool
/// - `rust_node_count` / `python_node_count`
/// - `rust_sink_nodes` / `python_sink_nodes`
/// - `rust_depth` / `python_depth`
/// - `missing_in_rust`: list[str] — node IDs in Python but not Rust
/// - `extra_in_rust`: list[str] — node IDs in Rust but not Python
#[pyfunction]
fn shadow_compare_graphs(
    pipeline_json: &str,
    python_node_ids: Vec<String>,
    python_sink_nodes: Vec<String>,
    python_graph_depth: usize,
) -> PyResult<String> {
    let pipeline_val: serde_json::Value = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON parse error: {e}")))?;

    let graph = graph::model::ProcessGraph::from_json(&pipeline_val)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Graph parse error: {e}")))?;

    let plan = directors::ExecutionPlan::from_graph(&graph)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Plan error: {e}")))?;

    let rust_ids: std::collections::HashSet<&str> =
        graph.nodes.keys().map(|s| s.as_str()).collect();
    let python_ids: std::collections::HashSet<&str> =
        python_node_ids.iter().map(|s| s.as_str()).collect();

    let missing: Vec<&str> = python_ids.difference(&rust_ids).copied().collect();
    let extra: Vec<&str> = rust_ids.difference(&python_ids).copied().collect();

    let rust_sinks: std::collections::HashSet<&str> =
        plan.sink_nodes.iter().map(|s| s.as_str()).collect();
    let python_sinks: std::collections::HashSet<&str> =
        python_sink_nodes.iter().map(|s| s.as_str()).collect();

    let result = serde_json::json!({
        "node_count_match": rust_ids.len() == python_ids.len(),
        "sink_match": rust_sinks == python_sinks,
        "level_count_match": plan.depth() == python_graph_depth,
        "rust_node_count": rust_ids.len(),
        "python_node_count": python_ids.len(),
        "rust_sink_nodes": plan.sink_nodes,
        "python_sink_nodes": python_sink_nodes,
        "rust_depth": plan.depth(),
        "python_depth": python_graph_depth,
        "missing_in_rust": missing,
        "extra_in_rust": extra,
    });
    Ok(result.to_string())
}

// ═══════════════════════════════════════════════════════════════════
// Ranking (from dorian-optimizer)
// ═══════════════════════════════════════════════════════════════════

#[pyfunction]
#[pyo3(signature = (scores, n_candidates, n_objectives, alpha=0.5))]
fn jensen_divergence_fitness(
    scores: Vec<f64>,
    n_candidates: usize,
    n_objectives: usize,
    alpha: f64,
) -> Vec<f64> {
    optimizer::ranking::jensen_divergence_fitness(&scores, n_candidates, n_objectives, alpha)
}

#[pyfunction]
fn non_dominated_sort(scores: Vec<f64>, n_candidates: usize, n_objectives: usize) -> Vec<usize> {
    optimizer::ranking::non_dominated_sort(&scores, n_candidates, n_objectives)
}

#[pyfunction]
#[pyo3(signature = (scores, n_candidates, n_objectives, strategy="nds"))]
fn rank(
    scores: Vec<f64>,
    n_candidates: usize,
    n_objectives: usize,
    strategy: &str,
) -> Vec<usize> {
    optimizer::ranking::rank(&scores, n_candidates, n_objectives, strategy)
}

// ═══════════════════════════════════════════════════════════════════
// Recommendation engine — end-to-end score + rank.
//
// One pyo3 entry that:
//   1. Reads the process-wide ``ExperimentStore`` (KD-tree + win-rate
//      cache, populated by ``rec_load_experiment_store`` at lifespan
//      start).
//   2. Constructs the requested objectives via
//      ``create_builtin_objective_with_store``.
//   3. Scores every candidate × objective in parallel (rayon, slice
//      #86).
//   4. Ranks via ENS-SS (slice #85) or weighted-sum.
//   5. Returns ``[{"id": ..., "front": ..., "scores": [...]}, ...]``
//      sorted best-first.
//
// The python ``recommend()`` flow that previously orchestrated all
// of this in-process now passes the JSON envelopes here and reads
// the result. One pyo3 boundary cross per recommendation round
// instead of one per candidate per objective.
// ═══════════════════════════════════════════════════════════════════

fn _experiment_store_cell(
) -> &'static RwLock<Option<std::sync::Arc<optimizer::recommendation::ExperimentStore>>> {
    static CELL: OnceLock<
        RwLock<Option<std::sync::Arc<optimizer::recommendation::ExperimentStore>>>,
    > = OnceLock::new();
    CELL.get_or_init(|| RwLock::new(None))
}

/// Populate the process-wide experiment store from a JSON dump of
/// ``{"datasets": [[id, [vec...]], ...], "win_rates": {id: rate, ...}}``.
/// The python lifespan dumps its in-memory state via this format
/// once at startup and again whenever a new dataset is profiled
/// (rebuild semantic — full replace, not incremental).
#[pyfunction]
fn rec_load_experiment_store(snapshot_json: &str) -> PyResult<()> {
    use optimizer::recommendation::ExperimentStore;
    use rustc_hash::FxHashMap;

    #[derive(serde::Deserialize)]
    struct Dump {
        #[serde(default)]
        datasets: Vec<(String, Vec<f64>)>,
        #[serde(default)]
        win_rates: std::collections::HashMap<String, f64>,
    }

    let dump: Dump = serde_json::from_str(snapshot_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("snapshot parse failed: {e}"))
    })?;
    let win_rates: FxHashMap<String, f64> = dump.win_rates.into_iter().collect();
    let store = std::sync::Arc::new(ExperimentStore::from_parts(dump.datasets, win_rates));
    let mut guard = _experiment_store_cell()
        .write()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("store lock poisoned"))?;
    *guard = Some(store);
    Ok(())
}

/// Whether the experiment store cell has been populated. Cheap probe
/// for the python opt-in path to fall back when the store isn't
/// loaded yet.
#[pyfunction]
fn rec_experiment_store_is_loaded() -> bool {
    _experiment_store_cell()
        .read()
        .map(|g| g.is_some())
        .unwrap_or(false)
}

/// Top-``k`` pipeline IDs by win rate. Used by the orchestrator when
/// the user's primary objective is ``PipelinePreferenceRatio`` —
/// candidate pool is the top-K preferred pipelines instead of a
/// random ``$sample``. Returns ``[]`` when the store isn't loaded
/// or the win-rate cache is empty (cold start).
#[pyfunction]
#[pyo3(signature = (k, exclude_ids=None))]
fn rec_top_pipelines_by_win_rate(
    k: usize,
    exclude_ids: Option<Vec<String>>,
) -> PyResult<Vec<String>> {
    let guard = _experiment_store_cell()
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("store lock poisoned"))?;
    let Some(store) = guard.as_ref() else {
        return Ok(Vec::new());
    };
    let exclude = exclude_ids.unwrap_or_default();
    Ok(store.top_pipelines_by_win_rate(k, &exclude))
}

/// Score + rank candidates against the chosen objectives. The
/// ``objective_names`` list is the user-curated order (top of
/// sidebar = first entry). The default ``"nds_lex"`` strategy
/// computes Pareto fronts (so non-dominated candidates surface
/// first regardless of objective order) and breaks ties
/// lexicographically by user order — same front, primary
/// objective wins.
///
/// ``user_defined_scores_json``: optional ``{name: [score per
/// candidate, in input order]}``. Lets the python orchestrator
/// supply scores for ``UserDefinedObjective`` (compiled python
/// code that must stay python-side) and slot them into the right
/// column. Built-in objectives in the same list are still scored
/// rust-side. Order is preserved across the mix.
///
/// Strategy:
///   - ``"nds_lex"`` (default): Pareto fronts, lex tie-break by
///     user order. Best of both — surfaces non-dominated picks
///     while respecting curated priority.
///   - ``"lexicographic"``: pure user order. Strict priority,
///     ignores Pareto structure.
///   - ``"nds"``: ENS-SS Pareto fronts; tie-broken by score sum.
///     Order-symmetric (use when objectives are co-equal).
///   - ``"jensen"``: Generalised Jensen-Shannon fitness.
///     Order-symmetric.
///   - anything else: weighted sum. Order-symmetric.
///
/// Returns ``[{id, front, scores}]`` sorted best-first.
/// ``front`` is the Pareto front (always computed for the UI's
/// "show me the top non-dominated layer" view).
#[pyfunction]
#[pyo3(signature = (
    context_json, candidates_json, objective_names,
    strategy="nds_lex", user_defined_scores_json=None,
))]
fn rec_score_and_rank(
    context_json: &str,
    candidates_json: &str,
    objective_names: Vec<String>,
    strategy: &str,
    user_defined_scores_json: Option<&str>,
) -> PyResult<String> {
    use optimizer::recommendation::objectives::{
        create_builtin_objective, create_builtin_objective_with_store, Candidate,
    };
    use optimizer::recommendation::{Objective, RecommendationContext};
    use rayon::prelude::*;
    use std::collections::HashMap;

    let ctx: RecommendationContext = serde_json::from_str(context_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("context parse failed: {e}"))
    })?;
    let candidates: Vec<Candidate> = serde_json::from_str(candidates_json).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!("candidates parse failed: {e}"))
    })?;
    let precomputed: HashMap<String, Vec<f64>> = match user_defined_scores_json {
        Some(j) => serde_json::from_str(j).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "user_defined_scores parse failed: {e}"
            ))
        })?,
        None => HashMap::new(),
    };

    // Resolve built-in objectives upfront so the inner loop can pull
    // by index. ``None`` slot = "this column comes from precomputed
    // (or stays 0.0 when neither path matches)". Unknown names are
    // tolerated rather than dropped — dropping would shift columns
    // and silently change the order the user curated.
    let store_guard = _experiment_store_cell()
        .read()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("store lock poisoned"))?;
    let store = store_guard.clone();
    drop(store_guard);

    let resolved: Vec<Option<Box<dyn Objective>>> = objective_names
        .iter()
        .map(|name| {
            // Skip rust resolution for names that already have a
            // precomputed column — saves a Box::new for user-defined
            // entries the python orchestrator handed us.
            if precomputed.contains_key(name) {
                return None;
            }
            match &store {
                Some(s) => create_builtin_objective_with_store(name, s),
                None => create_builtin_objective(name),
            }
        })
        .collect();

    let n_obj = objective_names.len();
    let n_cand = candidates.len();
    if n_obj == 0 || n_cand == 0 {
        return Ok("[]".to_string());
    }
    let mut scores = vec![0.0f64; n_cand * n_obj];

    // Parallel across rows (#86). Each row reads its row index ``i``
    // for precomputed-column lookups — closures own the precomputed
    // map by reference, no cloning per worker.
    scores
        .par_chunks_exact_mut(n_obj)
        .zip(candidates.par_iter())
        .enumerate()
        .for_each(|(i, (row, candidate))| {
            for (j, name) in objective_names.iter().enumerate() {
                if let Some(precomp) = precomputed.get(name) {
                    row[j] = precomp.get(i).copied().unwrap_or(0.0);
                } else if let Some(obj) = resolved[j].as_ref() {
                    row[j] = obj.score(candidate, &ctx);
                }
                // else: unknown name with no precomputed column —
                // leaves 0.0. Lexicographic rank then ignores it
                // (every candidate scores 0 on that axis).
            }
        });

    let order = optimizer::ranking::rank(&scores, n_cand, n_obj, strategy);
    let fronts = optimizer::ranking::non_dominated_sort(&scores, n_cand, n_obj);

    let result: Vec<serde_json::Value> = order
        .iter()
        .map(|&i| {
            let row = &scores[i * n_obj..(i + 1) * n_obj];
            serde_json::json!({
                "id": candidates[i].id,
                "front": fronts[i],
                "scores": row,
            })
        })
        .collect();
    serde_json::to_string(&result)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

// ═══════════════════════════════════════════════════════════════════
// DEM parser + map summary (from graph::parser)
// ═══════════════════════════════════════════════════════════════════

/// Parse a Dorian pipeline JSON into a DEM-annotated graph and return
/// a summary dict. Used by the Python RL trainer to validate pipelines
/// before dispatch and to read the domain/determinism classification
/// out of the same parser the Rust scheduler uses.
///
/// Returns JSON with:
///   - `node_count` / `edge_count`
///   - `sdf_count` / `de_count`
///   - `deterministic_count` / `non_deterministic_count` / `unknown_count`
///   - `de_node_ids`: list[str]
///   - `non_deterministic_node_ids`: list[str]
#[pyfunction]
fn dem_map_summary(pipeline_json: &str) -> PyResult<String> {
    let v: serde_json::Value = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
    let (g, dem) = graph::parse_pipeline_json(&v)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
    let s = graph::summarise_domain_map(&dem);
    let result = serde_json::json!({
        "node_count": g.node_count(),
        "edge_count": g.edge_count(),
        "sdf_count": s.sdf_count,
        "de_count": s.de_count,
        "deterministic_count": s.deterministic_count,
        "non_deterministic_count": s.non_deterministic_count,
        "unknown_count": s.unknown_count,
        "de_node_ids": s.de_node_ids,
        "non_deterministic_node_ids": s.non_deterministic_node_ids,
    });
    Ok(result.to_string())
}

// ═══════════════════════════════════════════════════════════════════
// Experiment Graph + batch plan (from cache)
// ═══════════════════════════════════════════════════════════════════

/// Cache-affinity score for a single pipeline against an empty index
/// (no prior materialised artifacts). Used by the RL agent's
/// logit-nudge term — pipelines with higher affinity against the
/// shared experiment graph should be preferred when the terminal
/// reward ties.
///
/// v1 takes an empty index; v2 will accept a handle to a live
/// `ExperimentGraphIndex` bridged through a PyO3 class. Today the
/// primary use is sanity-checking that the cache-key machinery
/// actually emits nonzero keys for the given pipeline shape.
#[pyfunction]
fn cache_affinity_empty(pipeline_json: &str) -> PyResult<f64> {
    let v: serde_json::Value = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
    let (g, dem) = graph::parse_pipeline_json(&v)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
    let idx = cache::ExperimentGraphIndex::new();
    Ok(cache::cache_affinity(&idx, &g, &dem))
}

/// Plan a batch of N pipelines against an empty experiment graph and
/// return collapse statistics. The RL trainer calls this after a
/// rollout to project how much compute would be shared if all N
/// candidates were executed together.
///
/// Returns JSON with:
///   - `pipelines`: int
///   - `naive_fire_count`: int  (sum of hits+misses across all pipelines)
///   - `unique_fire_count`: int (firings after dedup)
///   - `collapsed_firings`: int
///   - `collapse_ratio`: float  [0.0, 1.0]
///   - `implied_speedup`: float  (naive / unique)
///
/// Non-SDF and non-deterministic nodes do not participate — the
/// numbers reflect the cacheable subset only.
#[pyfunction]
fn plan_batch_empty_index(pipeline_jsons: Vec<String>) -> PyResult<String> {
    let mut graphs: Vec<graph::ProcessGraph> = Vec::with_capacity(pipeline_jsons.len());
    let mut anns: Vec<graph::DemAnnotations> = Vec::with_capacity(pipeline_jsons.len());
    for j in &pipeline_jsons {
        let v: serde_json::Value = serde_json::from_str(j)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
        let (g, ann) = graph::parse_pipeline_json(&v)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
        graphs.push(g);
        anns.push(ann);
    }
    let g_refs: Vec<&graph::ProcessGraph> = graphs.iter().collect();
    let a_refs: Vec<&graph::DemAnnotations> = anns.iter().collect();
    let idx = cache::ExperimentGraphIndex::new();
    let plan = cache::plan_batch(&idx, &g_refs, &a_refs);
    let naive = plan.naive_fire_count();
    let unique = plan.unique_fire_count();
    // unique == 0 && naive > 0 means "batch is fully served from the
    // index; no firings needed" — report the raw naive count as the
    // speedup (i.e. we avoided that many firings).
    let speedup = match (naive, unique) {
        (0, _) => 1.0,
        (n, 0) => n as f64,
        (n, u) => n as f64 / u as f64,
    };
    let result = serde_json::json!({
        "pipelines": graphs.len(),
        "naive_fire_count": naive,
        "unique_fire_count": unique,
        "collapsed_firings": plan.collapsed_firings,
        "collapse_ratio": plan.collapse_ratio(),
        "implied_speedup": speedup,
    });
    Ok(result.to_string())
}

/// Return the list of node IDs in the given pipeline that declare a
/// reproducibility-seed parameter (e.g. `random_state`) but do NOT
/// wire a Parameter node to that handle. Mitigation rewrites in
/// Python consume this list to auto-inject
/// `Parameter(random_state, int, 42)` nodes; until then, the cache
/// forces Bypass on these firings.
#[pyfunction]
fn detect_missing_random_state(pipeline_json: &str) -> PyResult<Vec<String>> {
    let v: serde_json::Value = serde_json::from_str(pipeline_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
    let (g, dem) = graph::parse_pipeline_json(&v)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
    Ok(cache::detect_missing_random_state(&g, &dem))
}

// ═══════════════════════════════════════════════════════════════════
// Live ExperimentGraphIndex — shared cache state across calls
// ═══════════════════════════════════════════════════════════════════

/// Python handle to a live `cache::ExperimentGraphIndex`.
///
/// The index is the Derakhshan-thesis Experiment Graph: a flat
/// key->artifact map that survives across RL episodes, making
/// `cache_affinity` carry real signal as rollouts complete.
///
/// Typical use from Python:
///
///     idx = ExperimentGraphIndex()
///     idx.insert_from_pipeline(pipeline_json, "feature", "{}", 0.5)
///     aff = idx.cache_affinity(candidate_json)  # 0..1
///     stats = idx.plan_batch([cand1, cand2, cand3])
///
/// `insert_from_pipeline` walks the parsed pipeline, computes each
/// deterministic node's cache key, and stores a placeholder entry
/// under each — so the whole pedigree becomes available for future
/// lookups in one call. This is the seam the RL trainer uses to
/// "commit" a rolled-out pipeline to the shared cache after a
/// successful evaluation.
#[pyclass]
struct ExperimentGraphIndex {
    inner: Mutex<cache::ExperimentGraphIndex>,
}

#[pymethods]
impl ExperimentGraphIndex {
    #[new]
    fn new() -> Self {
        ExperimentGraphIndex {
            inner: Mutex::new(cache::ExperimentGraphIndex::new()),
        }
    }

    /// Number of stored entries.
    fn __len__(&self) -> usize {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    fn is_empty(&self) -> bool {
        self.inner.lock().unwrap_or_else(|e| e.into_inner()).is_empty()
    }

    /// Single-pipeline reuse match against this index. Returns JSON
    /// with hits / misses / bypassed / hit_ratio / node_keys (hex).
    fn match_pipeline(&self, pipeline_json: &str) -> PyResult<String> {
        let v: serde_json::Value = serde_json::from_str(pipeline_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
        let (g, dem) = graph::parse_pipeline_json(&v)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
        let guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let m = cache::match_pipeline(&guard, &g, &dem);
        let node_keys: serde_json::Map<String, serde_json::Value> = m
            .node_keys
            .iter()
            .map(|(id, k)| (id.clone(), serde_json::Value::String(k.hex())))
            .collect();
        let result = serde_json::json!({
            "hits": m.hits.keys().collect::<Vec<_>>(),
            "misses": m.misses,
            "bypassed": m.bypassed,
            "hit_ratio": m.hit_ratio(),
            "node_keys": node_keys,
        });
        Ok(result.to_string())
    }

    /// Cache-affinity scalar for one pipeline against this live
    /// index. 0.0 when everything misses; 1.0 when every cacheable
    /// node is already materialised.
    fn cache_affinity(&self, pipeline_json: &str) -> PyResult<f64> {
        let v: serde_json::Value = serde_json::from_str(pipeline_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
        let (g, dem) = graph::parse_pipeline_json(&v)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
        let guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        Ok(cache::cache_affinity(&guard, &g, &dem))
    }

    /// Batch plan against this live index. Hits from the index count
    /// as collapsed firings on top of in-batch duplicates.
    fn plan_batch(&self, pipeline_jsons: Vec<String>) -> PyResult<String> {
        let mut graphs: Vec<graph::ProcessGraph> = Vec::with_capacity(pipeline_jsons.len());
        let mut anns: Vec<graph::DemAnnotations> = Vec::with_capacity(pipeline_jsons.len());
        for j in &pipeline_jsons {
            let v: serde_json::Value = serde_json::from_str(j)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
            let (g, ann) = graph::parse_pipeline_json(&v)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
            graphs.push(g);
            anns.push(ann);
        }
        let g_refs: Vec<&graph::ProcessGraph> = graphs.iter().collect();
        let a_refs: Vec<&graph::DemAnnotations> = anns.iter().collect();
        let guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        let plan = cache::plan_batch(&guard, &g_refs, &a_refs);
        let naive = plan.naive_fire_count();
        let unique = plan.unique_fire_count();
        let speedup = match (naive, unique) {
            (0, _) => 1.0,
            (n, 0) => n as f64,
            (n, u) => n as f64 / u as f64,
        };
        let result = serde_json::json!({
            "pipelines": graphs.len(),
            "naive_fire_count": naive,
            "unique_fire_count": unique,
            "collapsed_firings": plan.collapsed_firings,
            "collapse_ratio": plan.collapse_ratio(),
            "implied_speedup": speedup,
            "index_hits": plan.unique_hits.len(),
        });
        Ok(result.to_string())
    }

    /// Materialise every deterministic node in the given pipeline as
    /// a placeholder cache entry under its computed key. Mirrors what
    /// the scheduler does on a miss-then-success path; exposed here
    /// so the RL trainer can "commit" a completed episode's pedigree
    /// to the shared index after a successful evaluation.
    ///
    /// `artifact` must be one of "feature" / "statistics" / "model" /
    /// "opaque".
    ///
    /// Returns the number of entries inserted (equal to the number of
    /// deterministic, non-parameter nodes in the pipeline).
    fn insert_from_pipeline(
        &self,
        pipeline_json: &str,
        artifact: &str,
        payload_json: &str,
        compute_secs: f64,
    ) -> PyResult<usize> {
        let v: serde_json::Value = serde_json::from_str(pipeline_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("JSON: {e}")))?;
        let (g, dem) = graph::parse_pipeline_json(&v)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("parse: {e}")))?;
        let payload: serde_json::Value = serde_json::from_str(payload_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("payload JSON: {e}")))?;
        let artifact_kind = match artifact {
            "feature" => cache::Artifact::Feature,
            "statistics" => cache::Artifact::Statistics,
            "model" => cache::Artifact::Model,
            "opaque" => cache::Artifact::Opaque,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown artifact kind: {other}"
                )))
            }
        };
        // Walk the pipeline the same way match_pipeline does so we
        // materialise the same keys the scheduler would compute.
        let empty = cache::ExperimentGraphIndex::new();
        let m = cache::match_pipeline(&empty, &g, &dem);
        let mut count = 0usize;
        let mut guard = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        for (_node_id, key) in &m.node_keys {
            let entry = cache::CacheEntry::new(
                *key,
                artifact_kind,
                payload.clone(),
                compute_secs,
            );
            guard.insert(entry);
            count += 1;
        }
        Ok(count)
    }
}

// ═══════════════════════════════════════════════════════════════════
// Module
// ═══════════════════════════════════════════════════════════════════

#[pymodule]
fn dorian_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // GED
    m.add_function(wrap_pyfunction!(graph_edit_distance, m)?)?;
    m.add_function(wrap_pyfunction!(graph_edit_path, m)?)?;
    m.add_function(wrap_pyfunction!(fast_distance, m)?)?;
    m.add_function(wrap_pyfunction!(weighted_fast_distance, m)?)?;
    m.add_function(wrap_pyfunction!(apply_primitives, m)?)?;
    m.add_function(wrap_pyfunction!(match_pattern, m)?)?;
    m.add_function(wrap_pyfunction!(run_pipeline, m)?)?;
    m.add_class::<Pipeline>()?;
    m.add_class::<RuleIndex>()?;

    // KB snapshot — Neo4j replacement.
    m.add_function(wrap_pyfunction!(kb_load_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(kb_build_snapshot, m)?)?;

    // (extract_pipeline / extract_python_ast retired — see above.)

    m.add_function(wrap_pyfunction!(kb_parse, m)?)?;
    m.add_function(wrap_pyfunction!(kb_is_loaded, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_interface, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_family, m)?)?;
    m.add_function(wrap_pyfunction!(kb_model_family, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_parameters, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_io, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_import_path, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operator_risks, m)?)?;
    m.add_function(wrap_pyfunction!(kb_metric_display_name, m)?)?;
    m.add_function(wrap_pyfunction!(kb_method_sequence, m)?)?;
    m.add_function(wrap_pyfunction!(kb_interface_io, m)?)?;
    m.add_function(wrap_pyfunction!(kb_method_io, m)?)?;
    m.add_function(wrap_pyfunction!(kb_interface_attributes, m)?)?;
    m.add_function(wrap_pyfunction!(kb_all_operators, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operators_for_task, m)?)?;
    m.add_function(wrap_pyfunction!(kb_operators_by_interface, m)?)?;
    m.add_function(wrap_pyfunction!(kb_all_interface_methods, m)?)?;
    m.add_function(wrap_pyfunction!(kb_library_package_map, m)?)?;
    m.add_function(wrap_pyfunction!(kb_metrics_for_task, m)?)?;
    m.add_function(wrap_pyfunction!(kb_sensitive_families_for_risk, m)?)?;
    m.add_function(wrap_pyfunction!(kb_risks_surfaced_by_metric, m)?)?;
    m.add_function(wrap_pyfunction!(kb_all_pathways, m)?)?;
    m.add_function(wrap_pyfunction!(kb_mitigation_spec, m)?)?;
    m.add_function(wrap_pyfunction!(kb_mitigations_for_risk, m)?)?;
    m.add_function(wrap_pyfunction!(expand_dataset_refs, m)?)?;
    m.add_function(wrap_pyfunction!(expand_state_refs, m)?)?;
    m.add_function(wrap_pyfunction!(expand_printout_nodes, m)?)?;
    m.add_function(wrap_pyfunction!(expand_categorical_encoding, m)?)?;
    m.add_function(wrap_pyfunction!(expand_compound_operators, m)?)?;
    m.add_function(wrap_pyfunction!(extract_operator_names, m)?)?;
    m.add_function(wrap_pyfunction!(validate_pipeline, m)?)?;

    // BK-Tree
    m.add_class::<BKTree>()?;

    // Ranking
    m.add_function(wrap_pyfunction!(jensen_divergence_fitness, m)?)?;
    m.add_function(wrap_pyfunction!(non_dominated_sort, m)?)?;
    m.add_function(wrap_pyfunction!(rank, m)?)?;

    // Recommendation engine end-to-end.
    m.add_function(wrap_pyfunction!(rec_load_experiment_store, m)?)?;
    m.add_function(wrap_pyfunction!(rec_experiment_store_is_loaded, m)?)?;
    m.add_function(wrap_pyfunction!(rec_score_and_rank, m)?)?;
    m.add_function(wrap_pyfunction!(rec_top_pipelines_by_win_rate, m)?)?;

    // Shadow execution (Phase 1.7)
    m.add_function(wrap_pyfunction!(shadow_validate_plan, m)?)?;
    m.add_function(wrap_pyfunction!(shadow_compare_graphs, m)?)?;

    // DEM primitives (Tier 1/2)
    m.add_function(wrap_pyfunction!(dem_map_summary, m)?)?;
    m.add_function(wrap_pyfunction!(cache_affinity_empty, m)?)?;
    m.add_function(wrap_pyfunction!(plan_batch_empty_index, m)?)?;

    // Live Experiment Graph
    m.add_class::<ExperimentGraphIndex>()?;

    // Correctness helpers
    m.add_function(wrap_pyfunction!(detect_missing_random_state, m)?)?;

    // Tier-2 Arrow-IPC intermediate cache.
    m.add_function(wrap_pyfunction!(cache_init, m)?)?;
    m.add_function(wrap_pyfunction!(cache_get_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(cache_put_arrow, m)?)?;
    m.add_function(wrap_pyfunction!(cache_put_opaque, m)?)?;
    m.add_function(wrap_pyfunction!(cache_compute_key, m)?)?;
    m.add_function(wrap_pyfunction!(cache_classify_random_state_param, m)?)?;
    m.add_function(wrap_pyfunction!(cache_stats, m)?)?;
    m.add_function(wrap_pyfunction!(cache_evict_all, m)?)?;

    Ok(())
}
