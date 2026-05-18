//! Pipeline-expansion primitives.
//!
//! Pure-rust ports of ``dorian.pipeline.transforms.expand_*`` and
//! sibling python expansions. Each submodule owns one expansion
//! kind:
//!
//! * `dataset` — replaces ``dorian.io.dataset`` placeholders with a
//!   loader + optional X/y split sub-chain.
//! * (more coming as task #72 progresses: state, compound, encoding,
//!   printout)
//!
//! All expansions take an immutable input graph + a meta dict and
//! return a new graph; side-effects (Redis reads to populate meta)
//! stay in the python caller until the meta-assembly path lands
//! rust-side too.
pub mod categorical;
pub mod compound;
pub mod dataset;
pub mod printout;
pub mod state;

pub use categorical::expand_categorical_encoding;
pub use compound::{expand_compound_operators, CompoundRecord, MethodIo};
pub use dataset::{expand_dataset_refs, DatasetMeta, TargetSpec};
pub use printout::expand_printout_nodes;
pub use state::{expand_state_refs, ResolvedState};
