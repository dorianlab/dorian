//! Pattern matching against a [`Region`] / [`Actor`] graph.
//!
//! Mirrors the python ``dorian.dag.match`` semantics:
//!
//! * Each pattern node carries optional regex constraints on
//!   ``kind`` / ``type`` / ``text`` / ``language`` / ``name``.
//!   Empty / missing = wildcard.
//! * A successful match returns a mapping ``pattern_node_id ->
//!   concrete_actor_id``.
//! * Pattern edges are required — a candidate is only valid when
//!   every pattern edge has a matching concrete relation between
//!   the mapped actors.
//!
//! The engine yields each unique mapping once; the caller's
//! ``processed`` set prevents re-matching the same subgraph during
//! a fixpoint loop.

use rustc_hash::FxHashMap;

use crate::model::{Actor, ActorKind, Region};

/// One pattern node — same field names as the runtime [`Actor`] so
/// JSON rule specs read like the data they target. Unset fields
/// match any value.
#[derive(Debug, Clone, Default)]
pub struct PatternNode {
    /// Tagged-union discriminator. Lowercase string ``"parser_leaf"``,
    /// ``"operator"``, ``"parameter"``, ``"snippet"``, ``"composite"``
    /// — matches the snake_case serialisation of [`ActorKind`].
    /// ``"node"`` is accepted as a legacy alias for ``"parser_leaf"``.
    /// ``None`` / ``Some("")`` = wildcard.
    pub kind: Option<String>,
    pub r#type: Option<String>,
    pub text: Option<String>,
    pub language: Option<String>,
    pub name: Option<String>,
}

/// One pattern edge / relation.
#[derive(Debug, Clone)]
pub struct PatternEdge {
    pub source: String,
    pub destination: String,
}

/// A pattern is a small graph with regex-typed nodes + required edges.
#[derive(Debug, Clone, Default)]
pub struct Pattern {
    pub nodes: FxHashMap<String, PatternNode>,
    pub edges: Vec<PatternEdge>,
}

/// Resolved match: pattern-node-id → concrete-actor-id.
pub type Mapping = FxHashMap<String, String>;

/// Match the first candidate that satisfies the pattern AND isn't
/// in ``processed``. Returns ``None`` when no candidate exists.
///
/// A pattern edge `(src, dst)` is satisfied when *some*
/// [`Relation`](crate::model::Relation) in the region carries the
/// mapped `src` actor on the source side AND the mapped `dst`
/// actor on the destination side. Per-port constraints aren't
/// expressed in the basic pattern; rules that need port-specific
/// matches use a richer extension (added when the first such rule
/// lands).
pub fn match_first(pattern: &Pattern, region: &Region, processed: &[Mapping]) -> Option<Mapping> {
    let pattern_keys: Vec<&str> = pattern.nodes.keys().map(|s| s.as_str()).collect();
    if pattern_keys.is_empty() {
        return None;
    }

    let candidate_lists: Vec<Vec<String>> = pattern_keys
        .iter()
        .map(|pk| {
            let pn = &pattern.nodes[*pk];
            region
                .actors
                .iter()
                .filter(|a| actor_matches(pn, a))
                .map(|a| a.id.clone())
                .collect()
        })
        .collect();

    for assignment in cartesian(&candidate_lists) {
        if !all_distinct(&assignment) {
            continue;
        }
        let mut mapping: Mapping = FxHashMap::default();
        for (i, pk) in pattern_keys.iter().enumerate() {
            mapping.insert(pk.to_string(), assignment[i].clone());
        }
        if !edges_satisfied(pattern, region, &mapping) {
            continue;
        }
        if processed.iter().any(|m| m == &mapping) {
            continue;
        }
        return Some(mapping);
    }
    None
}

/// Predicate: does this Actor satisfy the PatternNode?
///
/// Field-by-field:
/// * `kind` — regex on the actor's [`ActorKind`] (snake_case).
///   Accepts ``"node"`` as an alias for ``"parser_leaf"`` so legacy
///   JSON rules keep working.
/// * `type` — regex on `actor.parser.r#type` (only meaningful for
///   parser leaves; semantic actors have an empty `parser.r#type`,
///   so a non-empty `type` regex implicitly filters to parser
///   leaves).
/// * `text` — regex on `actor.parser.text`.
/// * `name` — regex on `actor.name` (Operator FQN, Parameter name,
///   Snippet name, Composite name).
/// * `language` — regex on `actor.language`.
fn actor_matches(p: &PatternNode, a: &Actor) -> bool {
    if !actor_kind_matches(p.kind.as_deref(), a.kind) {
        return false;
    }
    if !regex_match_optional(p.r#type.as_deref(), &a.parser.r#type) {
        return false;
    }
    if !regex_match_optional(p.text.as_deref(), &a.parser.text) {
        return false;
    }
    if !regex_match_optional(p.language.as_deref(), &a.language) {
        return false;
    }
    if !regex_match_optional(p.name.as_deref(), &a.name) {
        return false;
    }
    true
}

fn actor_kind_matches(pat: Option<&str>, value: ActorKind) -> bool {
    let pat = match pat {
        None => return true,
        Some(s) if s.is_empty() => return true,
        Some(s) => s,
    };
    let canonical = match value {
        ActorKind::ParserLeaf => "parser_leaf",
        ActorKind::Operator => "operator",
        ActorKind::Snippet => "snippet",
        ActorKind::Parameter => "parameter",
        ActorKind::Composite => "composite",
    };
    // Tolerate the legacy ``"node"`` token as an alias for
    // ``"parser_leaf"`` so JSON rules written against the old
    // matcher don't have to change to keep matching tree-sitter
    // leaves on the new model.
    let aliased: &str = match value {
        ActorKind::ParserLeaf => "node",
        _ => canonical,
    };
    match regex::Regex::new(pat) {
        Ok(re) => re.is_match(canonical) || re.is_match(aliased),
        Err(_) => false,
    }
}

fn edges_satisfied(pattern: &Pattern, region: &Region, mapping: &Mapping) -> bool {
    pattern.edges.iter().all(|pe| {
        let s = match mapping.get(&pe.source) {
            Some(s) => s,
            None => return false,
        };
        let d = match mapping.get(&pe.destination) {
            Some(s) => s,
            None => return false,
        };
        region.relations.iter().any(|r| {
            r.sources.iter().any(|p| &p.actor == s)
                && r.destinations.iter().any(|p| &p.actor == d)
        })
    })
}

fn regex_match_optional(pat: Option<&str>, value: &str) -> bool {
    let pat = match pat {
        None => return true,           // wildcard
        Some(s) if s.is_empty() => return true,
        Some(s) => s,
    };
    match regex::Regex::new(pat) {
        Ok(re) => re.is_match(value),
        Err(_) => false,                // malformed pattern — never matches
    }
}

fn all_distinct(v: &[String]) -> bool {
    let mut seen = std::collections::HashSet::with_capacity(v.len());
    v.iter().all(|x| seen.insert(x.as_str()))
}

/// Eager cartesian product (allocates per yielded vec). Acceptable
/// for small pattern sets (≤ 8 nodes); the python original uses
/// ``itertools.product`` with the same complexity.
fn cartesian(lists: &[Vec<String>]) -> Vec<Vec<String>> {
    let mut out = vec![Vec::new()];
    for list in lists {
        let mut next = Vec::with_capacity(out.len() * list.len());
        for prefix in &out {
            for item in list {
                let mut row = prefix.clone();
                row.push(item.clone());
                next.push(row);
            }
        }
        out = next;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Actor, PortRef, Region, Relation};

    #[test]
    fn matches_two_actors_with_relation() {
        // ``call → identifier`` shape against a Region of parser leaves.
        let call = Actor::parser_leaf("a", "call", "f()");
        let ident = Actor::parser_leaf("b", "identifier", "f");
        let region = Region::new()
            .with_actor(call)
            .with_actor(ident)
            .with_relation(Relation::point_to_point(
                "rel:a->b",
                PortRef { actor: "a".into(), port: "".into() },
                PortRef { actor: "b".into(), port: "0".into() },
            ));

        let mut nodes = FxHashMap::default();
        let mut p_call = PatternNode::default();
        p_call.r#type = Some("call".into());
        nodes.insert("0".into(), p_call);
        let mut p_ident = PatternNode::default();
        p_ident.r#type = Some("identifier".into());
        nodes.insert("1".into(), p_ident);

        let pattern = Pattern {
            nodes,
            edges: vec![PatternEdge {
                source: "0".into(),
                destination: "1".into(),
            }],
        };
        let m = match_first(&pattern, &region, &[]).expect("match");
        assert_eq!(m["0"], "a");
        assert_eq!(m["1"], "b");
    }

    #[test]
    fn skips_processed_mappings() {
        let region = Region::new()
            .with_actor(Actor::parser_leaf("a", "identifier", "X"))
            .with_actor(Actor::parser_leaf("b", "identifier", "X"));

        let mut nodes = FxHashMap::default();
        let mut p = PatternNode::default();
        p.r#type = Some("identifier".into());
        nodes.insert("0".into(), p);
        let pattern = Pattern { nodes, edges: vec![] };

        let m1 = match_first(&pattern, &region, &[]).unwrap();
        let m2 = match_first(&pattern, &region, &[m1.clone()]).unwrap();
        assert_ne!(m1, m2);
    }

    #[test]
    fn legacy_kind_alias_matches_parser_leaf() {
        // JSON rules written for the old matcher use ``kind = "node"``
        // — that token has to keep matching parser leaves on the new
        // model.
        let leaf = Actor::parser_leaf("a", "identifier", "X");
        let region = Region::new().with_actor(leaf);

        let mut nodes = FxHashMap::default();
        let mut p = PatternNode::default();
        p.kind = Some("^node$".into());
        nodes.insert("0".into(), p);
        let pattern = Pattern { nodes, edges: vec![] };

        let m = match_first(&pattern, &region, &[]).expect("match");
        assert_eq!(m["0"], "a");
    }
}
