//! JSON rule schema.
//!
//! A rule is **declarative** — every transformation it performs is
//! a named primitive (no closures, no opaque function refs). Agents,
//! humans, and the migration tool all author rules in this format,
//! and the rust engine dispatches by string-keyed op name.
//!
//! Schema is a superset of the python `dorian.mcp.rule_schema`
//! primitives. The seven existing ops (`delete`, `update_attribute`,
//! `replace_operator`, `add_parameter`, `insert_before`,
//! `insert_after`, `add_edges`) are kept verbatim. We add the
//! graph-walking ops the python `rules.py` Apply functions used to
//! cover, with explicit names + parameter slots so they round-trip
//! as JSON.
//!
//! Rule shape:
//!
//! ```json
//! {
//!   "description": "Collapse a single-target variable assignment",
//!   "pattern": {
//!     "nodes": {
//!       "0": {"type": "assignment", "language": "python"},
//!       "1": {"type": "identifier|pattern_list", "language": "python"},
//!       "2": {"language": "python"}
//!     },
//!     "edges": [
//!       {"source": "0", "destination": "1"},
//!       {"source": "0", "destination": "2"}
//!     ]
//!   },
//!   "transformations": [
//!     {"type": "add_edges", "edges": [["2", "1"]]},
//!     {"type": "delete", "nodes": ["0"]}
//!   ]
//! }
//! ```
//!
//! See internal design note (TODO) for the full primitive
//! catalogue.

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use crate::pattern::{Pattern, PatternEdge, PatternNode};

/// One rule.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuleSpec {
    #[serde(default)]
    pub description: String,
    pub pattern: PatternSpec,
    #[serde(default)]
    pub transformations: Vec<TransformationSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternSpec {
    pub nodes: FxHashMap<String, PatternNodeSpec>,
    #[serde(default)]
    pub edges: Vec<PatternEdgeSpec>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PatternNodeSpec {
    /// Tagged-union discriminator (``"node"``, ``"operator"``,
    /// ``"parameter"``, ``"snippet"``). Empty = wildcard. Accepts a
    /// regex so a single rule can match multiple kinds.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub kind: String,
    #[serde(default, skip_serializing_if = "String::is_empty", rename = "type")]
    pub r#type: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub text: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub language: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PatternEdgeSpec {
    pub source: String,
    pub destination: String,
}

/// Discriminated transformation union.
///
/// Mirrors the python `rule_schema.TransformationSpec` for the
/// existing 7 ops; adds graph-walking ops the python rules.py used
/// to cover via opaque Apply lambdas.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TransformationSpec {
    /// Delete nodes (and optionally specific edges) from the DAG.
    Delete {
        #[serde(default)]
        nodes: Vec<String>,
        #[serde(default)]
        edges: Vec<[String; 2]>,
        #[serde(default)]
        mode: DeleteMode,
    },
    /// Update one attribute on a target node.
    UpdateAttribute {
        target: String,
        attribute: String,
        value: ValueExpr,
    },
    /// Replace a node's payload with an Operator(name=…).
    ReplaceOperator {
        target: String,
        new_name: String,
    },
    /// Insert a Parameter node and wire it as a kwarg into target.
    AddParameter {
        target: String,
        param_name: String,
        #[serde(default)]
        param_value: String,
        #[serde(default = "default_param_dtype")]
        param_dtype: String,
    },
    /// Insert an Operator node before target (i.e., target's
    /// inputs go through new_op first).
    InsertBefore {
        target: String,
        new_operator: String,
    },
    /// Insert an Operator node after target.
    InsertAfter {
        target: String,
        new_operator: String,
    },
    /// Add literal edges. Each edge is `[source_pattern_id, dest_pattern_id]`.
    AddEdges {
        edges: Vec<[String; 2]>,
    },

    // ── Graph-walking primitives (extends the python rule_schema) ──

    /// Rewire every outgoing edge of `use_key` to come from `producer_id`,
    /// preserving each edge's `position` / `output`. The `use_key` node
    /// is dropped. Used by the variable-resolution rule chain.
    RewireVarUses {
        use_key: String,
        producer_id_from_match: String,
    },

    /// Replace `op_key` (an `Operator(name="<var>.<method>")`) with the
    /// bare method shortcut + add a chain edge from `producer_id` at
    /// position "self". Existing positional data-arg edges shift by +1
    /// to make room for the chain edge. Looks up KB-declared port names
    /// per method to translate numeric positions to semantic ones
    /// (mirrors python `_rewrite_method_call_local`).
    ChainMethod {
        op_key: String,
        producer_id_from_match: String,
        method_name: String,
    },

    /// `X, y = f()` — fan a `pattern_list` node out into direct
    /// `call → identifier_i` edges with `output=i` so each downstream
    /// consumer of `X` / `y` carries the right slice.
    UnpackPatternList {
        pattern_list_key: String,
        source_call_key: String,
    },

    /// Convert a matched `subscript` Node into a Snippet that runs the
    /// slice on the root identifier's value.
    SubscriptToSnippet {
        subscript_key: String,
    },

    /// Wire a `call`'s `argument_list` into positional edges
    /// (positions 0..N in source order). Replaces the python
    /// `_expand_argument_list` Apply.
    ExpandArgList {
        call_key: String,
        argument_list_key: String,
    },

    /// Promote a target node into an `Operator` whose `name` is
    /// taken from the matched ``content_key`` node's ``text``.
    /// Mirrors python ``ToOperator(nid=..., content=...)``.
    ToOperator {
        target: String,
        content_key: String,
    },

    /// Promote a target node into a `Parameter`. ``kw_key`` is the
    /// pattern id whose ``text`` becomes the parameter name;
    /// ``value_key`` is the pattern id whose ``text`` becomes the
    /// parameter value (and whose ``type`` becomes the dtype, with
    /// the python ``Parameter`` convention — ``integer``/``float``/
    /// ``string`` from the AST node type, falling back to
    /// ``string``). Mirrors python ``ToParameter(nid, kw, value)``.
    ToParameter {
        target: String,
        kw_key: String,
        value_key: String,
    },

    /// Global pass: rewire every identifier-use to its producer.
    ///
    /// After ``10_collapse_assignment.json`` runs, each LHS
    /// identifier has an incoming edge from its RHS producer. This
    /// pass walks the DAG and for every other identifier with the
    /// same text but no incoming producer edge (a "use" site),
    /// rewires its outgoing edge to come from the producer instead.
    /// Method-shortcut producers contribute ``output=1`` (the
    /// result), non-method producers contribute ``output=0``.
    ///
    /// Replaces python's nested per-LHS sub-rules in
    /// ``rules.py`` — no per-LHS rule generation needed because
    /// this primitive computes the LHS → uses map by walking the
    /// graph once.
    ResolveVarReferences {},

    /// Global pass: collapse every ``Operator(name="<var>.<method>")``
    /// into a bare method shortcut + chain edge from ``<var>``'s
    /// producer at position ``"self"``. Mirrors the python nested
    /// per-LHS method-shortcut rule chain.
    ///
    /// Only fires for methods in the canonical shortcut set
    /// (``fit``, ``predict``, ``transform``, …) so the rule doesn't
    /// hijack arbitrary attribute calls.
    ChainAllMethodShortcuts {},

    /// Global pass: walk every ``import_statement`` /
    /// ``import_from_statement`` / ``aliased_import`` subtree, build
    /// an alias → FQN map, and rewrite every Operator name that
    /// either equals an alias or starts with ``alias + "."`` to use
    /// the FQN. Then delete the import nodes.
    ///
    /// Mirrors the python rule chain that captures import bindings
    /// at outer-match time and generates per-import nested rules to
    /// rewrite later attribute / identifier text. Doing it as a
    /// single global pass avoids the closure-over-mapping pattern
    /// that doesn't translate to declarative JSON.
    ///
    /// Examples:
    ///
    /// * ``from sklearn.ensemble import RandomForestClassifier`` —
    ///   alias[``RandomForestClassifier``] = ``sklearn.ensemble.RandomForestClassifier``.
    ///   ``Operator(name="RandomForestClassifier")`` → ``Operator(name="sklearn.ensemble.RandomForestClassifier")``.
    /// * ``import pandas as pd`` — alias[``pd``] = ``pandas``.
    ///   ``Operator(name="pd.read_csv")`` → ``Operator(name="pandas.read_csv")``.
    /// * ``import numpy`` — no rewrite needed, but the import node is
    ///   still removed so the post-extract DAG is purely operators
    ///   and parameters.
    ResolveImports {},
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeleteMode {
    #[default]
    Isolated,
    Cascade,
    Recursive,
}

fn default_param_dtype() -> String {
    "eval".to_string()
}

/// Value expression for `UpdateAttribute.value`. Either a literal
/// string or a structured reference / concat.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ValueExpr {
    Literal(String),
    Structured(StructuredValue),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StructuredValue {
    Ref { r#ref: String, attr: String },
    Concat { concat: Vec<ValueExpr> },
}

// ---------------------------------------------------------------------------
// Compilation: RuleSpec → runtime Pattern + transformations
// ---------------------------------------------------------------------------

/// Compiled rule — pattern materialised into the runtime
/// [`Pattern`] type, transformations kept as the JSON-spec enum
/// for the rewrite engine to dispatch.
#[derive(Debug, Clone)]
pub struct CompiledRule {
    pub description: String,
    pub pattern: Pattern,
    pub transformations: Vec<TransformationSpec>,
}

impl RuleSpec {
    pub fn compile(self) -> CompiledRule {
        let nodes: FxHashMap<String, PatternNode> = self
            .pattern
            .nodes
            .into_iter()
            .map(|(k, v)| {
                let mut pn = PatternNode::default();
                if !v.kind.is_empty() {
                    pn.kind = Some(v.kind);
                }
                if !v.r#type.is_empty() {
                    pn.r#type = Some(v.r#type);
                }
                if !v.text.is_empty() {
                    pn.text = Some(v.text);
                }
                if !v.language.is_empty() {
                    pn.language = Some(v.language);
                }
                if !v.name.is_empty() {
                    pn.name = Some(v.name);
                }
                (k, pn)
            })
            .collect();
        let edges: Vec<PatternEdge> = self
            .pattern
            .edges
            .into_iter()
            .map(|e| PatternEdge {
                source: e.source,
                destination: e.destination,
            })
            .collect();
        CompiledRule {
            description: self.description,
            pattern: Pattern { nodes, edges },
            transformations: self.transformations,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_assignment_rule() {
        let json = r#"
{
  "description": "Collapse single-target assignment",
  "pattern": {
    "nodes": {
      "0": {"type": "assignment", "language": "python"},
      "1": {"type": "identifier", "language": "python"},
      "2": {"language": "python"}
    },
    "edges": [
      {"source": "0", "destination": "1"},
      {"source": "0", "destination": "2"}
    ]
  },
  "transformations": [
    {"type": "add_edges", "edges": [["2", "1"]]},
    {"type": "delete", "nodes": ["0"]}
  ]
}
"#;
        let spec: RuleSpec = serde_json::from_str(json).expect("parse");
        let compiled = spec.compile();
        assert_eq!(compiled.pattern.nodes.len(), 3);
        assert_eq!(compiled.pattern.edges.len(), 2);
        assert_eq!(compiled.transformations.len(), 2);
    }

    #[test]
    fn parses_extended_op() {
        let json = r#"
{
  "description": "Rewire variable use",
  "pattern": {
    "nodes": {
      "use": {"type": "identifier", "language": "python"},
      "consumer": {"language": "python"}
    },
    "edges": [{"source": "use", "destination": "consumer"}]
  },
  "transformations": [
    {"type": "rewire_var_uses", "use_key": "use", "producer_id_from_match": "1"}
  ]
}
"#;
        let spec: RuleSpec = serde_json::from_str(json).expect("parse");
        match &spec.compile().transformations[0] {
            TransformationSpec::RewireVarUses { use_key, .. } => {
                assert_eq!(use_key, "use");
            }
            _ => panic!("wrong variant"),
        }
    }
}
