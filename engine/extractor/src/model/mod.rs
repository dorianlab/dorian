//! Pipeline model — Ptolemy II-style heterogeneous actor graph.
//!
//! Two-layer design:
//!
//! * [`core`] — MoC-agnostic data: [`Actor`], [`Port`], [`Relation`],
//!   [`Region`], [`Model`]. No knowledge of any particular model of
//!   computation.
//! * [`moc`] — Ptolemy II directors: [`Semantics`] enum carrying the
//!   MoC choice for each region. **This module is the swap
//!   boundary.** Replace `moc.rs` to plug in a different MoC
//!   abstraction; nothing in `core.rs` cares.
//!
//! Outside of this directory, callers should depend only on the
//! re-exports below — never reach into `core::` or `moc::` directly.
//! That gives us a single point to renegotiate names if we ever
//! pivot the abstraction (e.g. dropping Ptolemy II for a custom
//! actor calculus).

pub mod core;
pub mod moc;

pub use core::{
    Actor, ActorId, ActorKind, Model, ParameterLiteral, ParserPayload, Port, PortKind, PortRef,
    Region, RegionIndex, Relation, RelationId, SourceSpan, TypeHint,
};
pub use moc::Semantics;
