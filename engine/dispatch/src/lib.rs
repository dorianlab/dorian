//! dorian-dispatch — Multi-runtime dispatch with backpressure.
//!
//! Defines the `Runtime` trait and dispatch layer that routes node execution
//! to the appropriate runtime (Python subprocess, API, WASM, container).
//! Backpressure is built in from day one: bounded queues per runtime,
//! circuit breakers, and admission control.

pub mod runtime;
pub mod dispatcher;
pub mod python;
pub mod wasm;
pub mod container;

// Phase 3+: Additional runtime implementations
// pub mod api;           // ApiRuntime — HTTP/gRPC external services

// Re-export runtime implementations.
pub use python::PythonRuntime;
pub use wasm::WasmRuntime;
pub use container::ContainerRuntime;
