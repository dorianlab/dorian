//! Recommendation engine — fetch, score, rank pipeline candidates.
//!
//! Ports the orchestration logic from `dorian/pipeline/recommendation/` to Rust.
//! The Rust engine owns:
//!
//! - **Objective functions**: Built-in scoring (GeneralPerformance, PreviouslyUnseen,
//!   AtomicChanges) and objective resolution
//! - **Ranking**: Jensen divergence, non-dominated sorting, weighted sum (already
//!   implemented in `ranking.rs`)
//! - **Context management**: Immutable snapshot for each ranking round
//! - **Dependency checking**: Status metadata for frontend (active vs degraded)
//!
//! The Python runtime still handles:
//! - User-defined objectives (compiled Python code with sandbox)
//! - docstore candidate fetching (aggregation pipeline)
//!
//! Experiment Store queries (KD-Tree similarity, win-rate cache)
//! moved into Rust via ``store::ExperimentStore`` —
//! ``SimilarDataPerformance`` and ``PipelinePreferenceRatio``
//! score against the in-memory store directly.
//!
//! Architecture:
//! ```text
//! DataProfiled / TaskSelected / EvalSelected
//!     → build context (session meta + interactions)
//!     → fetch candidates (docstore $sample)
//!     → resolve objectives (built-in + custom)
//!     → score candidates (objective.score per candidate)
//!     → rank (Jensen / NDS / weighted_sum)
//!     → return top-N ranked + objective status
//! ```

pub mod objectives;
pub mod store;

pub use objectives::{
    Objective, ObjectiveStatus, RecommendationContext,
    GeneralPerformance, PreviouslyUnseen, AtomicChanges,
    SimilarDataPerformance, PipelinePreferenceRatio,
    check_dependencies, extract_operator_names,
};
pub use store::{ExperimentStore, StoredEval};
