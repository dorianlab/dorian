//! Reinforcement-learning engine. Phase-1 port of the Python
//! `rl/` modules into Rust so the supervisor binary
//! (`dorian-engines`) can run RL alongside AutoML and the
//! cross-product engine in one process.
//!
//! Phase 1 (this crate's initial state):
//!   * `policy::base` — Policy trait + Observation / ActionCandidate /
//!     Transition types. Mirrors `rl/policy/base.py`.
//!   * `policy::hedge` — HedgePolicy port (multiplicative weights).
//!     The simplest policy: ~220 LoC of arithmetic, no surrogate
//!     model, no kNN retrieval. Lands first to validate the type
//!     surface.
//!
//! Future phases (separate task):
//!   * MemoryPolicy (kNN over Observation.dataset_embedding)
//!   * HybridPolicy (Hedge × Memory composition)
//!   * ActionSpace (catalog → persistent action ids)
//!   * Env contract (`step`, `reset`, `available_actions`)
//!   * Reward channels + partial credit
//!   * Warm-start + persistence
//!   * Driver loop

pub mod policy;

pub use policy::base::{
    cosine_similarity, masked_indices, pick_with_weights, ActionCandidate, Observation,
    Policy, Transition, UpdateMetrics,
};
pub use policy::hedge::HedgePolicy;
