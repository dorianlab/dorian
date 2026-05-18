//! Pipeline extractor — Python source code → Dorian [`Model`].
//!
//! Replaces the python `dorian.code.parsing.parser` + the ~30
//! hand-written rules in `dorian.code.parsing.rules`. The python
//! rules used closure-typed Apply lambdas; this crate's rule
//! format is JSON-spec only — every transformation is a named
//! primitive the engine dispatches by string key.
//!
//! Layers:
//!
//! 1. [`ast`]      — tree-sitter-python parsing → raw [`Model`]
//!                   with one [`model::Actor`] of kind
//!                   [`model::ActorKind::ParserLeaf`] per
//!                   tree-sitter node and parent → child relations
//!                   carrying the sibling index in the destination
//!                   port name.
//! 2. [`model`]    — Ptolemy-II-style heterogeneous actor graph.
//!                   Two-layer split: ``core`` (MoC-agnostic data)
//!                   + ``moc`` (Ptolemy II directors). Replace
//!                   ``moc.rs`` to swap the MoC abstraction.
//! 3. [`pattern`]  — pattern matching against a [`model::Region`].
//!                   Same semantics as `dorian.dag.match`.
//! 4. [`rule`]     — JSON rule spec definitions. Extends the
//!                   python `dorian.mcp.rule_schema` with the
//!                   variable-resolution / method-shortcut /
//!                   tuple-unpacking / subscript-to-snippet
//!                   primitives the python rules.py uses.
//! 5. [`rewrite`]  — primitive ops dispatch (``apply(region,
//!                   mapping, op_spec)``).
//! 6. [`engine`]   — outer loop: parse → load rules →
//!                   match-rewrite-fixpoint.

pub mod ast;
pub mod model;
pub mod pattern;
pub mod rule;
pub mod rewrite;
pub mod engine;

#[cfg(test)]
mod tests {
    use super::*;

    /// Smoke: parse the canonical sample pipeline into an AST
    /// [`model::Model`] without panicking.
    #[test]
    fn ast_parses_sample_pipeline() {
        let code = r#"
from sklearn.datasets import make_classification
from sklearn.ensemble import RandomForestClassifier

X, y = make_classification(n_samples=500, random_state=42)
clf = RandomForestClassifier(n_estimators=100)
clf.fit(X, y)
"#;
        let model = ast::parse_python(code).expect("parse sample");
        assert!(!model.root.actors.is_empty(), "AST Model has no actors");
    }
}
