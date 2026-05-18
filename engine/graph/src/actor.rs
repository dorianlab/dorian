//! Actor lifecycle trait — Ptolemy II's actor contract.
//!
//! Why
//! ~~~
//! The directors crate already schedules nodes level-by-level, but
//! actor firing was stubbed: ``DataflowDirector::execute`` dispatches
//! ``RuntimeKind::Engine`` nodes synthetically (Parameters, etc.)
//! and leaves a TODO for ``RuntimeKind::Python``. Pipelines therefore
//! ran nowhere on the Rust side; the Python ``backend.pipeline_runner``
//! still flattened to a Dask dict and ran ``dask.threaded.get``.
//!
//! This trait is the missing primitive: every concrete node — sklearn
//! operator, snippet, parameter, abstract task lowered to a concrete
//! op — implements ``Actor``. Directors call into ``preinitialize``
//! through ``fire`` to ``wrapup`` according to their MoC; the actor
//! itself doesn't know which director ran it.
//!
//! Lifecycle (matches Ptolemy II):
//!
//!   1. ``preinitialize`` — once, when the actor is added to the
//!      enclosing model. Type-resolves ports and rate signatures.
//!   2. ``initialize`` — once per execution, before the first fire.
//!   3. ``prefire`` — every iteration, returns ``Ok(true)`` if the
//!      actor is ready to fire (i.e. all required inputs have a
//!      token). Returns ``Ok(false)`` to skip this iteration.
//!   4. ``fire`` — actually compute the output. Reads inputs,
//!      writes outputs through the ``ActorContext``.
//!   5. ``postfire`` — every iteration, returns ``Ok(true)`` to
//!      keep the actor alive (default), ``Ok(false)`` to retire it.
//!   6. ``wrapup`` — once per execution after the last fire. Free
//!      external resources.
//!
//! Implementations of choice:
//!
//!   * ``StubActor`` — used by Parameter / Group node types where
//!     ``fire`` is a no-op or value pass-through. Lives in this
//!     module.
//!   * ``PyActor`` — wraps a Python callable (the operator
//!     resolver). Lives in ``engine/native`` (pyo3 bindings) so
//!     this crate stays Python-free.
//!   * ``RustNativeActor`` (future) — pure-Rust reimplementations
//!     of common sklearn ops; bypasses pyo3 for hot operators.

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use crate::model::NodeId;

/// One token flowing on a port. JSON-serialisable so the Python /
/// other-runtime sides can exchange values without a separate IPC
/// schema. Future versions may switch to Arrow for hot data paths;
/// the trait-level type is flexible.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Token {
    /// A JSON-encoded value, including ``null``. Default carrier
    /// for parameters, scalars, dicts, lists.
    Json { value: serde_json::Value },
    /// An opaque reference owned by the runtime (e.g. a Python
    /// object handle, an Arrow ``RecordBatch`` id). The
    /// ``ActorContext`` resolves it lazily — actors don't see
    /// the underlying object directly through the trait.
    Ref { runtime: String, handle: String },
}

/// Read-side handle a director gives the actor for one fire cycle.
/// Inputs are addressed by destination port — string for keyword,
/// integer for positional. Mirrors the ``Position`` enum in the
/// graph model.
pub trait InputReader {
    fn get_positional(&self, position: i64) -> Option<&Token>;
    fn get_keyword(&self, name: &str) -> Option<&Token>;
}

/// Write-side handle. Multi-output operators (``train_test_split``
/// returning four arrays) produce one token per output index;
/// single-output operators write to index 0.
pub trait OutputWriter {
    fn put(&mut self, output_index: i64, token: Token);
}

/// Per-fire context the director hands the actor.
pub struct FireContext<'a> {
    pub node_id: &'a NodeId,
    pub inputs: &'a dyn InputReader,
    pub outputs: &'a mut dyn OutputWriter,
}

/// Lifecycle errors. Actors never panic — they return a typed
/// ``ActorError`` so the director can decide retry / abort / skip
/// per the MoC contract.
#[derive(Debug, thiserror::Error)]
pub enum ActorError {
    /// Required input missing at fire time. Director may retry
    /// after upstream produces a token, or abort if data won't
    /// arrive (e.g. SDF static-rate violation).
    #[error("missing input on port {port}")]
    MissingInput { port: String },
    /// Actor body raised. ``message`` carries the human-readable
    /// failure; ``recoverable`` flags whether the director can
    /// retry the same actor on a fresh fire (true) or must
    /// propagate the failure (false).
    #[error("actor {node} failed: {message}")]
    FireFailed {
        node: String,
        message: String,
        recoverable: bool,
    },
    /// Actor's runtime layer (Python interpreter, gRPC channel)
    /// is unreachable. Always non-recoverable for the current run.
    #[error("runtime unavailable: {0}")]
    RuntimeUnavailable(String),
}

/// Aggregate of ``preinitialize / initialize`` results that
/// directors carry across fire cycles. Currently empty — the
/// shape is here so per-MoC schedulers can attach scheduling
/// metadata (SDF token-rates, DE next-fire timestamps) without a
/// trait-API churn later.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ActorPlan {
    /// Per-port consumption rate for SDF; ``None`` = dynamic /
    /// not statically known.
    #[serde(default)]
    pub input_rates: FxHashMap<String, Option<u32>>,
    /// Per-output production rate for SDF.
    #[serde(default)]
    pub output_rates: FxHashMap<String, Option<u32>>,
}

/// Ptolemy II-style actor lifecycle. Directors call these methods
/// in order: ``preinitialize → initialize → (prefire → fire →
/// postfire)* → wrapup``. Default implementations cover the
/// stateless-transform case (Parameters, simple sklearn ops).
pub trait Actor: Send + Sync {
    /// Identity of the actor in the graph.
    fn node_id(&self) -> &NodeId;

    /// Once, on construction. Resolve port types / static rates.
    /// Default: empty plan (dynamic-rate, type-erased).
    fn preinitialize(&mut self) -> Result<ActorPlan, ActorError> {
        Ok(ActorPlan::default())
    }

    /// Once per execution before the first fire. Default no-op.
    fn initialize(&mut self) -> Result<(), ActorError> {
        Ok(())
    }

    /// Each iteration: ready to fire? Default = always ready.
    fn prefire(&mut self, _ctx: &FireContext<'_>) -> Result<bool, ActorError> {
        Ok(true)
    }

    /// Compute one fire's outputs.
    fn fire(&mut self, ctx: &mut FireContext<'_>) -> Result<(), ActorError>;

    /// Each iteration: stay alive? Default = yes.
    fn postfire(&mut self, _ctx: &FireContext<'_>) -> Result<bool, ActorError> {
        Ok(true)
    }

    /// Once per execution after the last fire. Default no-op.
    fn wrapup(&mut self) -> Result<(), ActorError> {
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// In-memory port readers / writers — minimal directors can use these
// directly; richer schedulers (SDF with bounded buffers, DE with event
// queues) attach their own implementations.
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct InMemoryInputs {
    pub positional: FxHashMap<i64, Token>,
    pub keyword: FxHashMap<String, Token>,
}

impl InputReader for InMemoryInputs {
    fn get_positional(&self, position: i64) -> Option<&Token> {
        self.positional.get(&position)
    }
    fn get_keyword(&self, name: &str) -> Option<&Token> {
        self.keyword.get(name)
    }
}

#[derive(Debug, Default)]
pub struct InMemoryOutputs {
    pub by_index: FxHashMap<i64, Token>,
}

impl OutputWriter for InMemoryOutputs {
    fn put(&mut self, output_index: i64, token: Token) {
        self.by_index.insert(output_index, token);
    }
}

// ---------------------------------------------------------------------------
// StubActor — used for Parameter / Group nodes where ``fire`` is a no-op
// pass-through. The director maps the node's static value (Parameter)
// onto output 0 directly.
// ---------------------------------------------------------------------------

pub struct StubActor {
    pub node_id: NodeId,
    pub value: Token,
}

impl StubActor {
    pub fn new(node_id: NodeId, value: Token) -> Self {
        Self { node_id, value }
    }
}

impl Actor for StubActor {
    fn node_id(&self) -> &NodeId {
        &self.node_id
    }

    fn fire(&mut self, ctx: &mut FireContext<'_>) -> Result<(), ActorError> {
        ctx.outputs.put(0, self.value.clone());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stub_actor_emits_value_on_output_zero() {
        let mut actor = StubActor::new(
            "p1".into(),
            Token::Json { value: serde_json::json!(42) },
        );
        actor.preinitialize().unwrap();
        actor.initialize().unwrap();

        let inputs = InMemoryInputs::default();
        let mut outputs = InMemoryOutputs::default();
        let nid = actor.node_id().clone();
        let mut ctx = FireContext {
            node_id: &nid,
            inputs: &inputs,
            outputs: &mut outputs,
        };
        assert!(actor.prefire(&ctx).unwrap());
        actor.fire(&mut ctx).unwrap();
        assert!(actor.postfire(&ctx).unwrap());
        actor.wrapup().unwrap();

        let v = outputs.by_index.get(&0).expect("output 0 written");
        match v {
            Token::Json { value } => assert_eq!(value, &serde_json::json!(42)),
            _ => panic!("expected Json token"),
        }
    }

    #[test]
    fn token_json_roundtrip() {
        let t = Token::Json { value: serde_json::json!({"x": 1, "y": [2, 3]}) };
        let s = serde_json::to_string(&t).unwrap();
        let back: Token = serde_json::from_str(&s).unwrap();
        match back {
            Token::Json { value } => {
                assert_eq!(value["x"], serde_json::json!(1));
            }
            _ => panic!(),
        }
    }

    #[test]
    fn token_ref_roundtrip() {
        let t = Token::Ref {
            runtime: "python".into(),
            handle: "obj_42".into(),
        };
        let s = serde_json::to_string(&t).unwrap();
        let back: Token = serde_json::from_str(&s).unwrap();
        match back {
            Token::Ref { runtime, handle } => {
                assert_eq!(runtime, "python");
                assert_eq!(handle, "obj_42");
            }
            _ => panic!(),
        }
    }
}
