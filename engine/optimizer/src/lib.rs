//! dorian-optimizer — Meta-director, ranking, and task-graph optimisation.
//!
//! This crate owns:
//! - Ranking: Jensen divergence, non-dominated sorting, weighted sum
//! - Phase 4: KB query engine (Neo4j bolt), operator resolution
//! - Phase 5: AI Debugger risk analysis, recommendation engine
//! - Phase 6: Meta-director (automatic director inference from graph structure)
//! - Phase 7: Task-graph lowering + optimisation — the Rust replacement
//!   for Dorian's Python-side Dask dict task graph. ``task_graph`` owns
//!   the representation; ``lowering`` produces one from a
//!   ``graph::ProcessGraph``. Execution lives in ``sdf`` (unchanged);
//!   this module is the explicit boundary so optimiser passes
//!   (constant-fold, prune, fuse) run on a concrete serialisable form.

pub mod ranking;
pub mod kb;
pub mod kb_topology;
pub mod risk;
pub mod recommendation;
pub mod meta_director;
pub mod task_graph;
pub mod lowering;

pub use lowering::{lower, prune, LoweringError};
pub use task_graph::{ArgRef, OperatorRef, Task, TaskGraph, TaskKey};
