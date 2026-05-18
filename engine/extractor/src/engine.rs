//! Outer extractor loop: source → [`Model`].
//!
//! 1. tree-sitter-python → raw [`Model`] (via [`crate::ast::parse_python`]).
//! 2. Load [`crate::rule::CompiledRule`] list.
//! 3. Match-rewrite-fixpoint: pop a rule, match it on the Region
//!    until no new mapping, apply each transformation per match,
//!    repeat until the queue is empty.
//!
//! Mirrors python ``dorian.code.parsing.parser.transform``.

use anyhow::{Context, Result};
use std::collections::VecDeque;
use std::path::Path;

use crate::ast::parse_python;
use crate::model::{Model, Region};
use crate::pattern::{match_first, Mapping};
use crate::rewrite::apply;
use crate::rule::{CompiledRule, RuleSpec};

/// Load every `*.json` rule under `dir`, compile, return the ordered
/// list (sorted by filename so the leading-number ordering controls
/// execution sequence).
pub fn load_rules_dir(dir: impl AsRef<Path>) -> Result<Vec<CompiledRule>> {
    let dir = dir.as_ref();
    let mut entries: Vec<std::path::PathBuf> = std::fs::read_dir(dir)
        .with_context(|| format!("read_dir {}", dir.display()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().map(|e| e == "json").unwrap_or(false))
        .collect();
    entries.sort();

    let mut rules = Vec::with_capacity(entries.len());
    for path in entries {
        let body = std::fs::read_to_string(&path)
            .with_context(|| format!("read {}", path.display()))?;
        let spec: RuleSpec = serde_json::from_str(&body)
            .with_context(|| format!("parse {}", path.display()))?;
        rules.push(spec.compile());
    }
    Ok(rules)
}

/// End-to-end: parse Python source, apply rules, return final
/// [`Model`].
pub fn extract(code: &str, rules: Vec<CompiledRule>) -> Result<Model> {
    let mut model = parse_python(code)?;
    model.root = transform(model.root, rules);
    Ok(model)
}

/// Apply a rule queue to a [`Region`] to fixpoint.
///
/// The deque allows rules to push sub-rules to the front (a future
/// extension; today the queue is consumed left-to-right and each
/// rule runs to completion before the next).
pub fn transform(mut region: Region, rules: Vec<CompiledRule>) -> Region {
    let mut queue: VecDeque<CompiledRule> = VecDeque::from(rules);

    while let Some(rule) = queue.pop_front() {
        let mut processed: Vec<Mapping> = Vec::new();
        loop {
            let m = match match_first(&rule.pattern, &region, &processed) {
                Some(m) => m,
                None => break,
            };
            processed.push(m.clone());
            for op in &rule.transformations {
                region = apply(region, &m, op);
            }
        }
    }

    // Drop dangling relations whose endpoints disappeared during
    // rewriting. Relations whose sources / destinations all
    // reference dropped actors get fully removed; partial-port
    // references are also dropped.
    let live: rustc_hash::FxHashSet<&str> =
        region.actors.iter().map(|a| a.id.as_str()).collect();
    for r in region.relations.iter_mut() {
        r.sources.retain(|p| live.contains(p.actor.as_str()));
        r.destinations.retain(|p| live.contains(p.actor.as_str()));
    }
    region
        .relations
        .retain(|r| !r.sources.is_empty() && !r.destinations.is_empty());
    region
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ActorKind;
    use crate::rule::RuleSpec;

    #[test]
    fn empty_rules_passthrough() {
        let code = "X = 1\n";
        let model = extract(code, vec![]).expect("extract");
        assert!(!model.root.actors.is_empty());
    }

    #[test]
    fn delete_comment_rule() {
        // Rule: any actor whose parser type is "comment" gets deleted.
        let json = r#"
{
  "description": "drop comments",
  "pattern": {
    "nodes": {"0": {"type": "comment", "language": "python"}}
  },
  "transformations": [
    {"type": "delete", "nodes": ["0"], "mode": "isolated"}
  ]
}
"#;
        let rule: RuleSpec = serde_json::from_str(json).unwrap();
        let code = "# this is a comment\nX = 1\n";
        let model = extract(code, vec![rule.compile()]).unwrap();
        assert!(model
            .root
            .actors
            .iter()
            .all(|a| a.parser.r#type != "comment"));
    }

    #[test]
    fn loads_curated_rules_and_runs_sample_pipeline() {
        // Find the curated rules dir from the repo root. Test runs
        // from the cargo target; walk up from CARGO_MANIFEST_DIR.
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let rules_dir = std::path::PathBuf::from(manifest_dir)
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.join("dorian/code/parsing/rules"));
        let Some(rules_dir) = rules_dir else { return };
        if !rules_dir.is_dir() {
            // Test is best-effort — runs on the dev tree only.
            return;
        }
        let rules = load_rules_dir(&rules_dir).expect("load rules");
        assert!(!rules.is_empty(), "found 0 curated JSON rules");

        let code = include_str!("../../../data/sample_pipeline.py");
        let model = extract(code, rules).expect("extract");
        // The starter rule set strips a lot of noise; we expect the
        // operator chips to survive.
        assert!(
            model.root.actors.iter().any(|a| matches!(a.kind, ActorKind::Operator)
                && a.name.contains("RandomForestClassifier")),
            "expected RandomForestClassifier somewhere in extracted Model",
        );
    }
}
