//! Model-of-computation layer — Ptolemy II directors.
//!
//! This file is the swap boundary. To replace Ptolemy II with a
//! different MoC abstraction (e.g. SystemC TLM, Esterel, a custom
//! actor calculus), keep [`Semantics`] as the public name and
//! re-implement what the variants mean. Nothing in
//! [`super::core`] cares about the internal representation —
//! `Region.semantics` just stores whatever this module exports.
//!
//! Today's variants mirror Ptolemy II's headline directors:
//!
//! * **SDF** (Synchronous Dataflow) — actors fire when all input
//!   ports have a token. Most ML pipelines parse to a single
//!   top-level SDF region (the canonical sklearn pipeline).
//! * **DE** (Discrete Event) — actors fire on time-stamped events.
//!   Streaming / online-learning pipelines.
//! * **PN** (Process Network) — Kahn process networks, lossless
//!   asynchronous channels. Maps cleanly to `asyncio` blocks.
//! * **FSM** (Finite State Machine) — control-flow regions:
//!   `if`/`else`, `match`, conditional branches.
//! * **PR** (Process / Procedural) — sequential statement region;
//!   default for function bodies before a more refined MoC is
//!   inferred. Acts as a placeholder so we don't have to declare a
//!   precise MoC at AST extraction time.
//!
//! `Custom(String)` carries a free-form name for user-defined or
//! experimental MoCs that aren't in the catalogue. The runtime
//! treats unknown directors as `PR` for now.

use serde::{Deserialize, Serialize};

/// One MoC choice for a [`super::core::Region`].
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(tag = "kind", content = "name", rename_all = "snake_case")]
pub enum Semantics {
    /// Procedural / sequential — placeholder default, used while the
    /// extractor hasn't classified the region yet.
    #[default]
    Procedural,
    /// Synchronous Dataflow.
    Sdf,
    /// Discrete Event.
    De,
    /// Process Network.
    Pn,
    /// Finite State Machine.
    Fsm,
    /// User-defined or experimental MoC. Free-form name.
    Custom(String),
}

impl Semantics {
    /// Stable string label for display / serialisation contexts
    /// where the enum tag isn't available (logs, error messages).
    pub fn label(&self) -> &str {
        match self {
            Semantics::Procedural => "procedural",
            Semantics::Sdf => "sdf",
            Semantics::De => "de",
            Semantics::Pn => "pn",
            Semantics::Fsm => "fsm",
            Semantics::Custom(s) => s.as_str(),
        }
    }

    /// Whether this MoC permits cycles in the relation graph.
    /// Ptolemy II's SDF director rejects cycles by definition; PN
    /// and FSM tolerate them. Useful for the validator that runs
    /// before we hand a region to an executor.
    pub fn permits_cycles(&self) -> bool {
        matches!(
            self,
            Semantics::Pn | Semantics::Fsm | Semantics::Procedural | Semantics::Custom(_)
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_procedural() {
        assert_eq!(Semantics::default(), Semantics::Procedural);
    }

    #[test]
    fn sdf_disallows_cycles() {
        assert!(!Semantics::Sdf.permits_cycles());
        assert!(Semantics::Pn.permits_cycles());
        assert!(Semantics::Fsm.permits_cycles());
    }

    #[test]
    fn custom_label_round_trips() {
        let s = Semantics::Custom("dataflow_with_state".into());
        assert_eq!(s.label(), "dataflow_with_state");
    }

    #[test]
    fn serializes_with_kind_tag() {
        let json = serde_json::to_string(&Semantics::Sdf).unwrap();
        assert_eq!(json, r#"{"kind":"sdf"}"#);
        let custom = serde_json::to_string(&Semantics::Custom("foo".into())).unwrap();
        assert_eq!(custom, r#"{"kind":"custom","name":"foo"}"#);
    }
}
