//! dorian-types — Core type definitions for the Dorian engine.
//!
//! This crate provides:
//! - Generated protobuf types (graph, execution, runtime, scaling, events)
//! - Shared enums and type aliases used across all engine crates

pub mod pb {
    pub mod graph {
        tonic::include_proto!("dorian.graph");
    }
    pub mod execution {
        tonic::include_proto!("dorian.execution");
    }
    pub mod runtime {
        tonic::include_proto!("dorian.runtime");
    }
    pub mod scaling {
        tonic::include_proto!("dorian.scaling");
    }
    pub mod events {
        tonic::include_proto!("dorian.events");
    }
}

/// UUID type alias (consistent with dorian Python codebase).
pub type NodeId = String;

/// Run identifier for a pipeline execution.
pub type RunId = String;
