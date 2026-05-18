//! Knowledge Base query engine — Neo4j bolt protocol.
//!
//! Ports `dorian/knowledge/queries.py` to Rust using `neo4rs` for the
//! bolt connection and `lru::LruCache` for per-function result caching.
//!
//! Architecture:
//! - `KbClient`: Async Neo4j client with connection pooling and LRU caches
//! - All queries are parameterized (no string interpolation — safe from injection)
//! - Caches are process-lifetime, keyed by query arguments
//! - Thread-safe: all caches behind `tokio::sync::Mutex` for async access
//!
//! Query categories:
//! - **Operator resolution**: get_operator_interface, get_method_sequence, get_import_path
//! - **Parameter metadata**: get_operator_parameters, get_interface_io, get_method_io
//! - **Operator catalog**: get_operators_for_task, get_all_operators, get_operator_family
//! - **Risk/mitigation**: get_operator_risks, get_mitigation_spec
//! - **Data pathways**: get_model_family, get_sensitive_families, get_pathways

pub mod builder;
pub mod client;
pub mod queries;
pub mod resolution;
pub mod snapshot;
pub mod types;

pub use builder::{build_snapshot, parse_statements, ParseError, Triple};
pub use client::KbClient;
pub use resolution::{OperatorResolver, Resolution, RuntimeTarget, InvocationKind};
pub use snapshot::{ConceptHierarchy, InterfaceRecord, KbSnapshot, MitigationRecord, OperatorRecord, PathwayRecord};
pub use types::*;
