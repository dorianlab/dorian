// Recursive serde-derived enums (NodeSelector in ``primitive``) push
// the serializer's type-resolution recursion past the default 128
// limit during test-target monomorphisation. 256 covers comfortably.
#![recursion_limit = "256"]

//! dorian-graph — Port-Typed Process Graph with heterogeneous execution semantics.
//!
//! Implements the Ptolemy II-inspired process graph model where:
//! - Nodes are processes with activation modes (Transform, Reactive, Service, Router)
//! - Edges are typed channels with delivery semantics (Once, Stream, Mailbox)
//! - Subgraphs have execution directors (Dataflow, MessagePassing, MapReduce, Sequential)
//! - The meta-director infers director assignments from graph structure
//!
//! This crate owns:
//! - Graph model: ProcessGraph, ProcessNode, Channel, Edge
//! - Topology: topological sort, cycle detection, execution levels, validation
//! - Graph algorithms: GED computation, BK-Tree similarity search
//! - (Phase 1.2) Rewrite rule engine: pattern matching + transforms

pub mod model;
pub mod topology;
pub mod rewrite;
pub mod primitive;
pub mod rule_index;
pub mod actor;
pub mod ged;
pub mod weighted_ged;
pub mod bktree;
pub mod dem;
pub mod expand;
pub mod parser;
pub mod validator;

// Re-export key types at crate root.
pub use model::{
    ActivationMode, DeliveryMode, Edge, GraphError, Group, IOMapping, Node, NodeId,
    Operator, Parameter, ParamDtype, PatternNode, Position, ProcessGraph, Snippet, DAG,
};
pub use topology::{execution_levels, has_cycle, topological_sort, validate};
pub use rewrite::{
    match_rule, sync_apply, transform, RewriteRule, Transformation,
    Add, Apply, Delete, Replace, EdgeSpec, Mapping, Meta, Priority, PurgeMode,
    single_node_pattern, add_nodes_and_edges, delete_nodes, apply_fn,
    remove_node_isolated,
};
pub use dem::{
    classify_determinism_builtin, classify_domain_builtin, ActorAnnotations,
    ChannelAnnotations, ChannelKey, DemAnnotations, DeterminismClass, Domain, DomainKind,
    PortDeclaration, TokenType,
};
pub use parser::{parse_pipeline_json, summarise_domain_map, DomainMapSummary};
pub use validator::{
    validate_pipeline, OperatorSig, PortSig, SignatureRegistry, ValidationError,
};
