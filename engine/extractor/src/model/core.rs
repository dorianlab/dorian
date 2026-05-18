//! Core data types — MoC-agnostic.
//!
//! This module holds *only* the structural model: actors with named
//! typed ports, relations between ports, regions that contain a
//! sub-graph, and a top-level model that wraps the root region.
//!
//! It deliberately knows nothing about Ptolemy II, directors, or any
//! particular model of computation. The MoC layer lives in
//! [`super::moc`] and is plugged in by `Region.semantics`. To swap
//! Ptolemy II for a different MoC abstraction, replace `moc.rs` and
//! update the type alias in `mod.rs` — nothing in this file needs
//! to change.

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use super::moc::Semantics;

pub type ActorId = String;
pub type RelationId = String;

/// One computational entity. The Ptolemy-II equivalent of an Actor.
///
/// `kind` discriminates Dorian's specialisations of the abstract
/// actor: built-in operators, inline snippets, hyperparameters,
/// composite (hierarchical) actors that contain a sub-region, and
/// transitional parser leaves the AST emits before semantic
/// promotion.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Actor {
    pub id: ActorId,
    pub kind: ActorKind,
    /// Operator FQN, snippet name, parameter name, composite name —
    /// or empty for a parser leaf.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub language: String,

    /// Named typed input ports. Order is meaningful for legacy
    /// positional callers but not load-bearing — the executor reads
    /// by name, not index.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub inputs: Vec<Port>,
    /// Named typed output ports.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub outputs: Vec<Port>,

    /// Inline literal hyperparameters. Runtime-resolved values
    /// (vault env-var refs, computed defaults from another actor)
    /// stay as separate Parameter-kind actors connected via a
    /// Relation — see [`PortKind::Kwarg`] / [`PortKind::Positional`]
    /// for how those bindings are typed.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub parameters: Vec<ParameterLiteral>,

    /// Sub-region for composite actors (function definitions, class
    /// bodies, async blocks). `None` for leaf actors.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub body: Option<Region>,

    /// Source-span pointer for editor round-trip. `None` for
    /// synthetic actors (mitigation rewrites, runtime-injected
    /// preprocessors, …).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<SourceSpan>,

    /// Snippet body when `kind == ActorKind::Snippet`. Has a
    /// ``foo(...)`` entry point per Dorian's Snippet convention.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub code: String,

    /// Transitional parser-leaf payload. Filled when `kind ==
    /// ActorKind::ParserLeaf` and dropped as rules promote the leaf
    /// to a semantic actor. Populated from tree-sitter — `r#type`
    /// is the AST node type ("identifier", "call", "assignment"),
    /// `text` is the source slice.
    #[serde(default, skip_serializing_if = "ParserPayload::is_empty")]
    pub parser: ParserPayload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorKind {
    /// Tree-sitter AST leaf. Transitional — rules promote these to
    /// one of the semantic kinds.
    #[default]
    ParserLeaf,
    /// Library operator (sklearn estimator, pandas function, LLM
    /// call, …). Identified by FQN in `Actor.name`.
    Operator,
    /// Inline Python snippet with a `foo(...)` entry point.
    Snippet,
    /// Hyperparameter — either a literal value (in `Actor.parameters`
    /// of the consumer) or, when runtime-resolved, a free-standing
    /// actor connected via a Relation.
    Parameter,
    /// Hierarchical actor containing a sub-region (function def,
    /// class body, async block, comprehension).
    Composite,
}

/// One named typed port on an actor. The port `name` is the
/// unique-within-actor identifier — input and output namespaces are
/// disjoint by construction (the role is determined by which side
/// of a Relation the PortRef appears on).
///
/// Names follow the KB's semantic convention (`"X"`, `"y"`,
/// `"self"`, `"random_state"`, `"X_test"`, `"y_pred"`). The legacy
/// numeric index (`"0"`, `"1"`) is used as a fallback only when
/// no semantic name is known yet — rules upgrade these from the
/// KB port table as they fire.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Port {
    pub name: String,
    pub kind: PortKind,
    /// Best-effort type hint. `None` when the rule engine hasn't
    /// inferred one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub type_hint: Option<TypeHint>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortKind {
    /// Generic data flow port. ML data (DataFrame, ndarray, Tensor),
    /// structured records, anything that flows.
    #[default]
    Data,
    /// `self`-port for method-shortcut chain edges. The producer's
    /// `instance` output binds to a method actor's `Self` input,
    /// which is how Dorian represents `clf.fit(X, y)` after
    /// chain-method-shortcut rewriting.
    SelfRef,
    /// Keyword-style binding (`random_state=42`). Port name is the
    /// kwarg name as the consumer sees it.
    Kwarg,
    /// Positional binding (`accuracy_score(y_test, y_pred)` →
    /// y_test at positional port "0"). NOTE: positional ports CAN
    /// be the destination of a Parameter actor's relation, not
    /// only Kwarg ports — the user's curated note on this is
    /// captured in [`super::ParameterLiteral`].
    Positional,
    /// Control flow — loop iteration variable, branch arm
    /// selector. Reserved for the cycle-tolerant extension; not
    /// emitted by today's rule chain.
    Control,
}

/// Best-effort type hint. Strings are intentional — keeps the
/// model independent of any particular type system. Concrete type
/// inference happens at execution time against the operator
/// catalog.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TypeHint {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub element: Option<Box<TypeHint>>,
}

/// A connection between actor ports. Multi-source, multi-destination
/// to support fan-in (a single value consumed by multiple actors)
/// and merge-multiplexing (loop bodies, branch joins).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Relation {
    pub id: RelationId,
    pub sources: Vec<PortRef>,
    pub destinations: Vec<PortRef>,
}

/// Pointer to a specific port on a specific actor. The role
/// (input vs. output) is determined by which side of a Relation
/// this ref sits on: `Relation.sources` carry output-port refs,
/// `Relation.destinations` carry input-port refs.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortRef {
    pub actor: ActorId,
    pub port: String,
}

/// Inline hyperparameter literal. Lives on the consumer actor
/// (`Actor.parameters`) when its value is a static literal that
/// doesn't need runtime resolution. Vault refs (`${HF_TOKEN}`),
/// computed defaults, and any Parameter that consumers want to
/// trace via the relation graph stay as standalone Parameter-kind
/// actors instead.
///
/// Even though most literals bind by name (kwargs), the schema
/// allows positional bindings too — rare cases like
/// `train_test_split(0.2, random_state=42)` where the test_size
/// is a literal at positional 0. The `port` field carries the
/// destination port name (or numeric index when only positional
/// is known).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParameterLiteral {
    pub name: String,
    pub port: String,
    pub value: String,
    pub dtype: String,
}

/// Source-span pointer for editor round-trip. Byte offsets into
/// the original source string, plus line/col for display.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceSpan {
    pub start_byte: u32,
    pub end_byte: u32,
    pub start_line: u32,
    pub start_col: u32,
}

/// Tree-sitter parser payload, populated only on `ActorKind::ParserLeaf`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ParserPayload {
    /// AST node type (`"identifier"`, `"call"`, `"assignment"`, …).
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub r#type: String,
    /// Source-text slice.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub text: String,
}

impl ParserPayload {
    pub fn is_empty(&self) -> bool {
        self.r#type.is_empty() && self.text.is_empty()
    }
}

/// One bounded region of the model. The director ([`Semantics`])
/// determines the model-of-computation for actors inside this
/// region. Top-level Python files default to a synchronous-dataflow
/// region; `async` blocks, FSMs, loops can be carried as nested
/// regions with different semantics.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Region {
    /// Model-of-computation in effect for this region. See
    /// [`super::moc`].
    #[serde(default)]
    pub semantics: Semantics,
    pub actors: Vec<Actor>,
    #[serde(default)]
    pub relations: Vec<Relation>,
}

/// Top-level pipeline model. One Region at the root; nested regions
/// hang off composite actors via `Actor.body`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Model {
    pub root: Region,
}

// ---------------------------------------------------------------------------
// Builder helpers — convenience for tests + JSON round-trip
// ---------------------------------------------------------------------------

impl Model {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Region {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_actor(mut self, a: Actor) -> Self {
        self.actors.push(a);
        self
    }

    pub fn with_relation(mut self, r: Relation) -> Self {
        self.relations.push(r);
        self
    }

    /// Look up an actor by id. O(n) — fine for the model sizes we
    /// extract today (sample pipelines run ~30 actors). If we ever
    /// extract larger models, layer an index on top.
    pub fn actor(&self, id: &str) -> Option<&Actor> {
        self.actors.iter().find(|a| a.id == id)
    }

    pub fn actor_mut(&mut self, id: &str) -> Option<&mut Actor> {
        self.actors.iter_mut().find(|a| a.id == id)
    }

    /// Iterate every actor whose port appears as a destination of
    /// any relation incoming to `actor_id`.
    pub fn upstream_relations<'a>(&'a self, actor_id: &'a str) -> impl Iterator<Item = &'a Relation> + 'a {
        self.relations
            .iter()
            .filter(move |r| r.destinations.iter().any(|p| p.actor == actor_id))
    }

    pub fn downstream_relations<'a>(&'a self, actor_id: &'a str) -> impl Iterator<Item = &'a Relation> + 'a {
        self.relations
            .iter()
            .filter(move |r| r.sources.iter().any(|p| p.actor == actor_id))
    }
}

impl Actor {
    /// Default constructor for a parser leaf with the given
    /// `r#type` + `text`. Convenience for the AST walker; rules
    /// that promote leaves to semantic actors construct via
    /// dedicated builders (`Actor::operator(name)`,
    /// `Actor::snippet(name, code)`, …).
    pub fn parser_leaf(id: impl Into<ActorId>, ast_type: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            kind: ActorKind::ParserLeaf,
            name: String::new(),
            language: "python".to_string(),
            inputs: Vec::new(),
            outputs: Vec::new(),
            parameters: Vec::new(),
            body: None,
            source: None,
            code: String::new(),
            parser: ParserPayload {
                r#type: ast_type.into(),
                text: text.into(),
            },
        }
    }

    pub fn operator(id: impl Into<ActorId>, name: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            kind: ActorKind::Operator,
            name: name.into(),
            language: "python".to_string(),
            inputs: Vec::new(),
            outputs: Vec::new(),
            parameters: Vec::new(),
            body: None,
            source: None,
            code: String::new(),
            parser: ParserPayload::default(),
        }
    }

    pub fn parameter(id: impl Into<ActorId>, name: impl Into<String>, value: impl Into<String>, dtype: impl Into<String>) -> Self {
        let mut a = Self::operator(id, name);
        a.kind = ActorKind::Parameter;
        a.parameters.push(ParameterLiteral {
            name: a.name.clone(),
            port: "value".into(),
            value: value.into(),
            dtype: dtype.into(),
        });
        a
    }

    pub fn snippet(id: impl Into<ActorId>, name: impl Into<String>, code: impl Into<String>) -> Self {
        let mut a = Self::operator(id, name);
        a.kind = ActorKind::Snippet;
        a.code = code.into();
        a
    }

    /// Find-or-create an input port by name. Used by rule primitives
    /// that need a named port without caring whether it already
    /// exists. Idempotent — repeated calls with the same `name`
    /// keep the first-call's `kind`.
    pub fn upsert_input(&mut self, name: &str, kind: PortKind) {
        if self.inputs.iter().any(|p| p.name == name) {
            return;
        }
        self.inputs.push(Port {
            name: name.to_string(),
            kind,
            type_hint: None,
        });
    }

    pub fn upsert_output(&mut self, name: &str, kind: PortKind) {
        if self.outputs.iter().any(|p| p.name == name) {
            return;
        }
        self.outputs.push(Port {
            name: name.to_string(),
            kind,
            type_hint: None,
        });
    }
}

impl Relation {
    /// Single-source single-destination convenience constructor.
    pub fn point_to_point(id: impl Into<RelationId>, from: PortRef, to: PortRef) -> Self {
        Self {
            id: id.into(),
            sources: vec![from],
            destinations: vec![to],
        }
    }
}

// ---------------------------------------------------------------------------
// Index — derived lookup tables. Built on demand from a Region.
// ---------------------------------------------------------------------------

/// O(1) lookup tables over a region's actors and relations. Built
/// once per pass; the rule engine consumes the index for its
/// pattern matcher and rebuilds when the region mutates.
#[derive(Debug, Default)]
pub struct RegionIndex<'a> {
    pub actor_by_id: FxHashMap<&'a str, &'a Actor>,
    /// (actor_id, port_name) → relation that has this PortRef as a destination.
    pub by_destination: FxHashMap<(&'a str, &'a str), Vec<&'a Relation>>,
    /// (actor_id, port_name) → relation that has this PortRef as a source.
    pub by_source: FxHashMap<(&'a str, &'a str), Vec<&'a Relation>>,
}

impl<'a> RegionIndex<'a> {
    pub fn build(region: &'a Region) -> Self {
        let mut idx = Self::default();
        for a in &region.actors {
            idx.actor_by_id.insert(a.id.as_str(), a);
        }
        for r in &region.relations {
            for p in &r.sources {
                idx.by_source
                    .entry((p.actor.as_str(), p.port.as_str()))
                    .or_default()
                    .push(r);
            }
            for p in &r.destinations {
                idx.by_destination
                    .entry((p.actor.as_str(), p.port.as_str()))
                    .or_default()
                    .push(r);
            }
        }
        idx
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_default_has_empty_root_region() {
        let m = Model::default();
        assert!(m.root.actors.is_empty());
        assert!(m.root.relations.is_empty());
    }

    #[test]
    fn upsert_input_is_idempotent() {
        let mut a = Actor::operator("0", "fit");
        a.upsert_input("X", PortKind::Data);
        a.upsert_input("X", PortKind::Data);
        assert_eq!(a.inputs.len(), 1);
        assert_eq!(a.inputs[0].name, "X");
    }

    #[test]
    fn region_index_builds_lookup_tables() {
        let make = Actor::operator("make", "sklearn.datasets.make_classification");
        let tts = Actor::operator("tts", "sklearn.model_selection.train_test_split");
        let mut region = Region::new().with_actor(make).with_actor(tts);
        region.actor_mut("make").unwrap().upsert_output("X", PortKind::Data);
        region.actor_mut("tts").unwrap().upsert_input("X", PortKind::Data);
        region = region.with_relation(Relation::point_to_point(
            "rel:make.X→tts.X",
            PortRef { actor: "make".into(), port: "X".into() },
            PortRef { actor: "tts".into(), port: "X".into() },
        ));
        let idx = RegionIndex::build(&region);
        assert_eq!(idx.actor_by_id.len(), 2);
        assert_eq!(idx.by_source.get(&("make", "X")).map(Vec::len), Some(1));
        assert_eq!(idx.by_destination.get(&("tts", "X")).map(Vec::len), Some(1));
    }
}
