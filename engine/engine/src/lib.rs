//! dorian-engine — Composition root for the Dorian execution engine.
//!
//! This crate owns:
//! - Pipeline execution lifecycle (state machine + event sourcing)
//! - Orchestration: parse → expand → build graph → dispatch → finalize
//! - gRPC server for the Go gateway
//! - Admission control and pipeline queuing

pub mod state;
pub mod events;
pub mod grpc;
pub mod redis_cache;

pub use redis_cache::RedisCacheStore;
