//! directors — Execution strategies for process graph regions.
//!
//! Each director implements a scheduling strategy for a subgraph:
//! - **Dataflow**: Topological sort, level-by-level concurrent dispatch,
//!   pull-based, fire-once. The classic pipeline pattern — replaces
//!   `dask.threaded.get()` from the Python codebase.
//! - **MessagePassing**: Event-loop, push-based, reactive scheduling.
//!   For agent collaboration and chatbot flows. Supports cycles.
//! - **MapReduce**: Fan-out/fan-in, parallel batch processing.
//!   For cross-validation, grid search, ensemble training.
//! - **Sequential**: Strict ordering, one at a time. For debugging
//!   and resource-constrained environments.
//!
//! The Meta-Director (in the optimizer crate) infers which director
//! to assign based on graph structure — activation modes and delivery
//! semantics determine the scheduling strategy.

pub mod cache_wrapper;
pub mod dataflow;
pub mod map_reduce;
pub mod message_passing;
pub mod sequential;

// Re-export key types.
pub use cache_wrapper::{CacheAwareHooks, CacheDecision};
pub use dataflow::{
    DataflowDirector, DirectorError, DirectorHooks, ExecutionPlan, NoopHooks, NodeOutcome,
    resolve_runtime,
};
pub use map_reduce::{MapReduceDirector, Phase as MapReducePhase};
pub use message_passing::{Message, MessagePassingDirector, TerminationCondition};
pub use sequential::SequentialDirector;
