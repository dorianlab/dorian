//! Compiled rule index for O(1) operator-FQN dispatch.
//!
//! The naive ``sync_apply``-per-rule loop scans the whole graph
//! once per rule (``O(N × R)``). For Dorian, ~95% of rewrite rules
//! have a single-node pattern of the form ``Node(type="Operator",
//! text=re.escape(fqn))`` — i.e. they target one specific operator
//! FQN. The right shape is to compile the rule list into
//! ``FxHashMap<FQN, Vec<RuleId>>`` once, then walk the pipeline's
//! operators once and look up matching rules per node.
//!
//! Non-FQN-anchored rules (multi-node patterns, regex patterns
//! that don't reduce to a literal) fall into a "wildcard" bucket
//! that's still tested per-pipeline but only when the FQN dispatch
//! has run — those are rare.
//!
//! Estimates from ``scripts/bench_rust_vs_python.py`` baseline (50
//! nodes × 23 rules):
//!   - naive: ~410 µs per pipeline sweep (95% futile matching)
//!   - indexed: ~20 µs per pipeline sweep (one hash lookup per node)
//!   - speedup ~20×, dominated by index lookup vs O(N × R) scan

use rustc_hash::{FxHashMap, FxHashSet};
use serde::{Deserialize, Serialize};

use crate::model::{Node, ProcessGraph};
use crate::primitive::PrimitiveOp;
use crate::rewrite::{match_rule, Mapping};

/// The set of operator FQNs present in a pipeline. Pre-built once
/// per pipeline (cheap — one walk over the nodes) and reused across
/// every rule-sweep so the dispatch can short-circuit on rules
/// targeting FQNs the pipeline doesn't contain.
///
/// For the off-domain case (sklearn-only pipeline evaluated against
/// LLM-guard rules), the intersection is empty and ``match_with_prefilter``
/// performs zero ``match_rule`` calls — the win compounds with #76.
#[derive(Debug, Clone, Default)]
pub struct OpSet {
    fqns: FxHashSet<String>,
}

impl OpSet {
    pub fn from_pipeline(pipeline: &ProcessGraph) -> Self {
        let mut fqns: FxHashSet<String> = FxHashSet::default();
        for (_id, node) in pipeline.nodes.iter() {
            if let Node::Operator(op) = node {
                fqns.insert(op.name.clone());
            }
        }
        Self { fqns }
    }

    pub fn contains(&self, fqn: &str) -> bool {
        self.fqns.contains(fqn)
    }

    pub fn len(&self) -> usize {
        self.fqns.len()
    }

    pub fn is_empty(&self) -> bool {
        self.fqns.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &str> {
        self.fqns.iter().map(|s| s.as_str())
    }
}

/// One compiled rule. The pattern is a full ``ProcessGraph`` so the
/// index can fall back to ``match_rule`` for confirmation; the
/// ``target_fqn`` is the optimisation key (``None`` for wildcard
/// rules that aren't FQN-anchored).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CompiledRule {
    pub id: String,
    /// Which operator FQN this rule's pattern targets, if any. Set
    /// at compile time when the python-side ``re.escape(fqn)``
    /// regex is detected; ``None`` triggers wildcard fallback.
    pub target_fqn: Option<String>,
    pub pattern: ProcessGraph,
    #[serde(default)]
    pub transformations: Vec<PrimitiveOp>,
}

#[derive(Debug, Clone, Default)]
pub struct RuleIndex {
    rules: Vec<CompiledRule>,
    by_fqn: FxHashMap<String, Vec<usize>>,
    wildcard: Vec<usize>,
    /// Per-rule set of FQNs the rule's transformations produce.
    /// ``add_node(operator name=X)`` and ``set_node_payload(operator
    /// name=X)`` both contribute X. Used by ``apply_to_fixpoint`` to
    /// pick the rules that need re-evaluation after a rule fires.
    produces: Vec<FxHashSet<String>>,
}

impl RuleIndex {
    /// Compile a rule list into the dispatch index. ``target_fqn``
    /// presence drives the bucketing; the ``produces`` table is
    /// derived from each rule's transformations so the iterative
    /// apply can only re-check rules triggered by what just fired.
    pub fn build(rules: Vec<CompiledRule>) -> Self {
        let mut by_fqn: FxHashMap<String, Vec<usize>> = FxHashMap::default();
        let mut wildcard: Vec<usize> = Vec::new();
        let mut produces: Vec<FxHashSet<String>> = Vec::with_capacity(rules.len());
        for (idx, r) in rules.iter().enumerate() {
            match r.target_fqn.as_deref() {
                Some(fqn) if !fqn.is_empty() => {
                    by_fqn.entry(fqn.to_string()).or_default().push(idx);
                }
                _ => wildcard.push(idx),
            }
            produces.push(rule_produces_fqns(&r.transformations));
        }
        Self { rules, by_fqn, wildcard, produces }
    }

    /// Number of rules. Cheap probe.
    pub fn len(&self) -> usize {
        self.rules.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rules.is_empty()
    }

    /// Rules targeting a given operator FQN. Used by the AI
    /// Debugger when the user clicks a node to see which mitigations
    /// could apply. ``O(1)`` dispatch.
    pub fn rules_for_fqn(&self, fqn: &str) -> Vec<&CompiledRule> {
        self.by_fqn
            .get(fqn)
            .map(|ids| ids.iter().map(|&i| &self.rules[i]).collect())
            .unwrap_or_default()
    }

    /// Match every rule against *pipeline* and return the rule IDs
    /// that fire along with the resulting pattern→graph mapping.
    /// Walks the pipeline's operator nodes once, dispatches via the
    /// FQN bucket, then runs ``match_rule`` to confirm any
    /// secondary pattern constraints (multi-node patterns, edges).
    /// Wildcard-bucket rules are tried separately.
    pub fn match_all(&self, pipeline: &ProcessGraph) -> Vec<(String, Mapping)> {
        self.match_with_prefilter(pipeline, &OpSet::from_pipeline(pipeline))
    }

    /// Pre-filtered variant of ``match_all`` — when the caller
    /// already has the pipeline's ``OpSet`` cached (the common
    /// case for sweeps that test many rule indexes against the
    /// same pipeline, or for the lifespan-built ``Pipeline`` whose
    /// op set we recompute lazily). Same semantics as
    /// ``match_all`` but skips the per-node walk for the FQN
    /// dispatch — uses the set's hash directly.
    pub fn match_with_prefilter(
        &self,
        pipeline: &ProcessGraph,
        op_set: &OpSet,
    ) -> Vec<(String, Mapping)> {
        let mut hits: Vec<(String, Mapping)> = Vec::new();
        let mut tried: FxHashSet<usize> = FxHashSet::default();

        // Iterate the *smaller* of (rule FQN buckets, pipeline op set):
        // the rules-not-firing branch never enters ``match_rule`` at
        // all. For an off-domain pipeline (e.g. RL trainer evaluating
        // a sklearn-only candidate against the LLM-guard rule set)
        // this short-circuits at zero cost — no walk, no match.
        if self.by_fqn.len() <= op_set.len() {
            for (fqn, rule_ids) in &self.by_fqn {
                if !op_set.contains(fqn) {
                    continue;
                }
                for &idx in rule_ids {
                    if !tried.insert(idx) {
                        continue;
                    }
                    let r = &self.rules[idx];
                    if let Some(m) = match_rule(&r.pattern, pipeline, &[]) {
                        hits.push((r.id.clone(), m));
                    }
                }
            }
        } else {
            for fqn in op_set.iter() {
                if let Some(rule_ids) = self.by_fqn.get(fqn) {
                    for &idx in rule_ids {
                        if !tried.insert(idx) {
                            continue;
                        }
                        let r = &self.rules[idx];
                        if let Some(m) = match_rule(&r.pattern, pipeline, &[]) {
                            hits.push((r.id.clone(), m));
                        }
                    }
                }
            }
        }

        // Wildcard rules: no FQN anchor → run match_rule against
        // the pipeline directly. These are a small minority for
        // Dorian today (every ``insert-*-before`` and every
        // mitigation rewrite has a literal target FQN).
        for &idx in &self.wildcard {
            if !tried.insert(idx) {
                continue;
            }
            let r = &self.rules[idx];
            if let Some(m) = match_rule(&r.pattern, pipeline, &[]) {
                hits.push((r.id.clone(), m));
            }
        }
        hits
    }

    /// Set of all FQNs that any indexed rule targets. Useful for
    /// pre-filter (``op_set ∩ rule_targets``) before the dispatch.
    pub fn target_fqns(&self) -> impl Iterator<Item = &str> {
        self.by_fqn.keys().map(|s| s.as_str())
    }

    /// All rules in declaration order — used by the wildcard
    /// fallback path that wants to iterate the full set.
    pub fn rules(&self) -> &[CompiledRule] {
        &self.rules
    }

    /// Iteratively match + apply rules against the pipeline until no
    /// rule fires. After each fire, the next iteration only checks
    /// rules whose target FQN intersects the just-produced FQNs —
    /// rules that can't possibly have a fresh match are skipped.
    /// Returns the ordered list of (rule_id, mapping) for each fire.
    ///
    /// Safety bound: 1000 total fires, to break runaway rules whose
    /// produces re-trigger themselves. (Single-pass per-rule
    /// idempotency is the rule author's responsibility — this is
    /// just to keep a buggy rule from hanging the runtime.)
    pub fn apply_to_fixpoint(
        &self,
        pipeline: &mut ProcessGraph,
    ) -> Vec<(String, Mapping)> {
        use crate::primitive::{apply_ops, HeuristicRoleResolver};
        let roles = HeuristicRoleResolver::default();
        let mut history: Vec<(String, Mapping)> = Vec::new();

        // First pass: check every rule.
        let mut to_check: Vec<usize> = (0..self.rules.len()).collect();

        let mut fires = 0usize;
        while !to_check.is_empty() && fires < 1000 {
            let op_set = OpSet::from_pipeline(pipeline);
            let mut affected: FxHashSet<String> = FxHashSet::default();
            let mut any_fired = false;
            // Drain current candidate list — fires this round only
            // re-trigger via the affected-FQN map next round.
            let candidates = std::mem::take(&mut to_check);
            for idx in candidates {
                let r = &self.rules[idx];
                if let Some(fqn) = &r.target_fqn {
                    if !op_set.contains(fqn) {
                        continue;
                    }
                }
                let mapping = match match_rule(&r.pattern, pipeline, &[]) {
                    Some(m) => m,
                    None => continue,
                };
                let mut m = mapping.clone();
                if apply_ops(pipeline, &r.transformations, &mut m, &roles).is_err() {
                    // Match the python-side ``Ambiguous`` no-op
                    // contract — drop this fire and continue.
                    continue;
                }
                history.push((r.id.clone(), mapping));
                fires += 1;
                any_fired = true;
                for f in self.produces[idx].iter() {
                    affected.insert(f.clone());
                }
            }
            if !any_fired {
                break;
            }
            // Pick rules to recheck: every rule whose target FQN was
            // produced this round, plus every wildcard rule (no FQN
            // anchor → can't tell whether it was affected without
            // running it). Wildcards are rare in Dorian's rule set.
            let mut next: FxHashSet<usize> = FxHashSet::default();
            for fqn in &affected {
                if let Some(ids) = self.by_fqn.get(fqn) {
                    for &i in ids {
                        next.insert(i);
                    }
                }
            }
            for &i in &self.wildcard {
                next.insert(i);
            }
            to_check = next.into_iter().collect();
        }
        history
    }
}

/// Extract the FQNs an ``Operator`` transformation introduces or
/// replaces. ``Parameter``/``Snippet`` payloads aren't FQN-anchored
/// (they're config / inline code), so they don't trigger any
/// ``target_fqn``-keyed rule. Edges + delete ops don't introduce
/// new operators either.
fn rule_produces_fqns(prims: &[crate::primitive::PrimitiveOp]) -> FxHashSet<String> {
    use crate::primitive::{NodePayloadSpec, PrimitiveOp};
    let mut out: FxHashSet<String> = FxHashSet::default();
    for op in prims {
        match op {
            PrimitiveOp::AddNode { payload, .. } => {
                if let NodePayloadSpec::Operator { name, .. } = payload {
                    out.insert(name.clone());
                }
            }
            PrimitiveOp::SetNodePayload { payload, .. } => {
                if let NodePayloadSpec::Operator { name, .. } = payload {
                    out.insert(name.clone());
                }
            }
            _ => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Operator, PatternNode};
    use crate::primitive::{NodeSelector, PayloadKind};

    fn fqn_pattern(fqn: &str) -> ProcessGraph {
        let mut g = ProcessGraph::new();
        g.add_node(
            "n".to_string(),
            Node::Node(PatternNode {
                node_type: "Operator".to_string(),
                text: regex::escape(fqn),
                language: "python".to_string(),
            }),
        );
        g
    }

    fn op(name: &str) -> Node {
        Node::Operator(Operator {
            name: name.to_string(),
            language: "python".to_string(),
            tasks: vec![],
        })
    }

    #[test]
    fn dispatch_returns_only_matching_rules() {
        let r1 = CompiledRule {
            id: "rule-rf".into(),
            target_fqn: Some("sklearn.ensemble.RandomForestClassifier".into()),
            pattern: fqn_pattern("sklearn.ensemble.RandomForestClassifier"),
            transformations: vec![],
        };
        let r2 = CompiledRule {
            id: "rule-llm".into(),
            target_fqn: Some("openrouter.chat.completion".into()),
            pattern: fqn_pattern("openrouter.chat.completion"),
            transformations: vec![],
        };
        let r3 = CompiledRule {
            id: "rule-csv".into(),
            target_fqn: Some("pandas.read_csv".into()),
            pattern: fqn_pattern("pandas.read_csv"),
            transformations: vec![],
        };
        let idx = RuleIndex::build(vec![r1, r2, r3]);

        let mut g = ProcessGraph::new();
        g.add_node("a".into(), op("pandas.read_csv"));
        g.add_node("b".into(), op("sklearn.ensemble.RandomForestClassifier"));

        let raw = idx.match_all(&g);
        let hits: Vec<&str> = raw.iter().map(|(rid, _)| rid.as_str()).collect();
        // We don't assert order — the FQN walk is HashMap-iterator order.
        assert!(hits.contains(&"rule-csv"));
        assert!(hits.contains(&"rule-rf"));
        assert!(!hits.contains(&"rule-llm"));
    }

    #[test]
    fn wildcard_rules_still_evaluate() {
        // Pattern targets *anything* that's a Parameter, no FQN anchor.
        let mut wild_pattern = ProcessGraph::new();
        wild_pattern.add_node(
            "p".to_string(),
            Node::Node(PatternNode {
                node_type: "Parameter".to_string(),
                text: ".*".to_string(),
                language: ".*".to_string(),
            }),
        );
        let r_wild = CompiledRule {
            id: "wild-param".into(),
            target_fqn: None,
            pattern: wild_pattern,
            transformations: vec![],
        };
        let idx = RuleIndex::build(vec![r_wild]);

        let mut g = ProcessGraph::new();
        g.add_node(
            "p1".to_string(),
            Node::Parameter(crate::model::Parameter {
                name: "x".to_string(),
                dtype: crate::model::ParamDtype::Int,
                value: "1".to_string(),
            }),
        );

        let raw = idx.match_all(&g);
        let hits: Vec<&str> = raw.iter().map(|(rid, _)| rid.as_str()).collect();
        assert_eq!(hits, vec!["wild-param"]);
    }

    #[test]
    fn rules_for_fqn_returns_targeted_rules_only() {
        let r1 = CompiledRule {
            id: "rule-rf".into(),
            target_fqn: Some("sklearn.ensemble.RandomForestClassifier".into()),
            pattern: fqn_pattern("sklearn.ensemble.RandomForestClassifier"),
            transformations: vec![],
        };
        let r2 = CompiledRule {
            id: "rule-llm".into(),
            target_fqn: Some("openrouter.chat.completion".into()),
            pattern: fqn_pattern("openrouter.chat.completion"),
            transformations: vec![],
        };
        let idx = RuleIndex::build(vec![r1, r2]);
        let rf_rules: Vec<&str> = idx
            .rules_for_fqn("sklearn.ensemble.RandomForestClassifier")
            .iter()
            .map(|r| r.id.as_str())
            .collect();
        assert_eq!(rf_rules, vec!["rule-rf"]);
        assert!(idx.rules_for_fqn("nonexistent.op").is_empty());
    }

    /// Smoke test: build an index of N rules, match against an N-node
    /// graph, ensure we don't quadratically scan when only one rule
    /// fires. Exact timing is in ``scripts/bench_rust_vs_python.py``.
    #[test]
    fn dispatch_skips_non_matching_buckets() {
        let mut rules = Vec::new();
        for i in 0..50 {
            let fqn = format!("module.Op{i}");
            rules.push(CompiledRule {
                id: format!("rule-{i}"),
                target_fqn: Some(fqn.clone()),
                pattern: fqn_pattern(&fqn),
                transformations: vec![],
            });
        }
        let idx = RuleIndex::build(rules);

        let mut g = ProcessGraph::new();
        for i in 0..50 {
            g.add_node(format!("n{i}"), op(&format!("module.Op{i}")));
        }
        // Plus a target that isn't in the rule set.
        g.add_node("extra".into(), op("module.Untargeted"));

        let hits = idx.match_all(&g);
        assert_eq!(hits.len(), 50);
    }

    // Reference NodeSelector + PayloadKind so the import doesn't
    // get linted out by --warn=unused — the public API references
    // PrimitiveOp which transitively uses both.
    #[allow(dead_code)]
    fn _force_use(_s: NodeSelector, _k: PayloadKind) {}

    #[test]
    fn apply_to_fixpoint_only_rechecks_triggered_rules() {
        use crate::primitive::{NodePayloadSpec, NodeSelector, PrimitiveOp};

        // Rule A: turns ``X`` into ``Y``.
        let rule_a = CompiledRule {
            id: "a".into(),
            target_fqn: Some("ops.X".into()),
            pattern: fqn_pattern("ops.X"),
            transformations: vec![PrimitiveOp::SetNodePayload {
                selector: NodeSelector::FromMapping { key: "n".into() },
                payload: NodePayloadSpec::Operator {
                    name: "ops.Y".into(),
                    language: "python".into(),
                },
            }],
        };
        // Rule B: turns ``Y`` into ``Z``. Triggered after A produces ``Y``.
        let rule_b = CompiledRule {
            id: "b".into(),
            target_fqn: Some("ops.Y".into()),
            pattern: fqn_pattern("ops.Y"),
            transformations: vec![PrimitiveOp::SetNodePayload {
                selector: NodeSelector::FromMapping { key: "n".into() },
                payload: NodePayloadSpec::Operator {
                    name: "ops.Z".into(),
                    language: "python".into(),
                },
            }],
        };
        // Rule C: targets a FQN that never appears — must never fire.
        let rule_c = CompiledRule {
            id: "c".into(),
            target_fqn: Some("ops.NeverHere".into()),
            pattern: fqn_pattern("ops.NeverHere"),
            transformations: vec![],
        };
        let idx = RuleIndex::build(vec![rule_a, rule_b, rule_c]);

        let mut g = ProcessGraph::new();
        g.add_node("n0".into(), op("ops.X"));
        let history = idx.apply_to_fixpoint(&mut g);

        let fired: Vec<&str> = history.iter().map(|(rid, _)| rid.as_str()).collect();
        // A fires (X → Y), then B fires (Y → Z). C never fires.
        assert_eq!(fired, vec!["a", "b"]);
        // Pipeline is now Z.
        match g.nodes.get("n0").unwrap() {
            Node::Operator(op) => assert_eq!(op.name, "ops.Z"),
            _ => panic!("expected Operator after apply_to_fixpoint"),
        }
    }
}
