//! In-memory KB snapshot — petgraph-backed replacement for Neo4j.
//!
//! The KB at runtime is read-only; this snapshot is built once from
//! the curated ``dorian/knowledge/sources/*.py`` files via the Python
//! ingest tool (``scripts/export_kb_snapshot.py``), serialised to
//! JSON, and loaded once at process start. Every query that
//! ``dorian/knowledge/queries.py`` exposes is served from in-memory
//! lookup tables here — no network round-trip.
//!
//! Why a single struct of HashMaps + a petgraph for hierarchy:
//!
//!   * The vast majority of queries are 1-hop lookups (operator →
//!     interface, operator → family, interface → methods). Direct
//!     ``FxHashMap`` lookups beat any graph-walking implementation.
//!   * Hierarchical queries (concept → ancestor family, family
//!     adjacency for task-topology distances) need a real graph,
//!     which is what ``petgraph`` is for.
//!
//! The pyo3 bridge in ``engine/native`` exposes every method here as
//! a ``dorian_native.kb_*`` function. ``dorian/knowledge/queries.py``
//! routes through them when ``DORIAN_USE_RUST_KB=1``.

use petgraph::graph::{DiGraph, NodeIndex};
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use crate::kb::types::{IoSpec, OperatorInfo, ParameterSpec};

/// Per-operator record. Mirrors the union of fields returned by the
/// operator-centric queries in ``queries.py``.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OperatorRecord {
    pub name: String,
    pub interface: Option<String>,
    pub family: Option<String>,
    pub tasks: Vec<String>,
    pub parameters: Vec<ParameterSpec>,
    /// Inputs — same shape as ``get_operator_io`` returns.
    pub inputs: Vec<IoSpec>,
    /// Outputs — same shape as ``get_operator_io`` returns.
    pub outputs: Vec<IoSpec>,
    pub import_path: Option<String>,
    pub risks: Vec<String>,
    /// Display name for metrics (None for non-metric operators).
    pub display_name: Option<String>,
}

/// Per-interface record. Captures the data the compound-operator
/// expansion path needs at a glance.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct InterfaceRecord {
    pub name: String,
    /// Ordered method-call sequence (``__init__``, ``fit``, ``predict``, ...).
    pub method_sequence: Vec<String>,
    /// Interface-level inputs (frontend catalog).
    pub inputs: Vec<IoSpec>,
    /// Interface-level outputs.
    pub outputs: Vec<IoSpec>,
    /// Per-method I/O. Map method name → (inputs, outputs).
    pub method_io: FxHashMap<String, (Vec<IoSpec>, Vec<IoSpec>)>,
    /// Boolean attributes the runtime consults (``passthrough`` for
    /// guardrails, etc.). Mirrors ``get_interface_attributes``.
    pub attributes: Vec<String>,
}

/// Mitigation specification — what an AI Debugger suggestion expands
/// into when the user accepts it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MitigationRecord {
    pub name: String,
    pub interface_name: Option<String>,
    pub anchor_inputs: Vec<String>,
    /// Which risks this mitigation can address.
    pub risks: Vec<String>,
}

/// Pathway — a metric/threshold/replacement triple linking data
/// quality breaches to model risks. Mirrors the ``get_all_pathways``
/// row shape.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PathwayRecord {
    pub name: String,
    pub metric: String,
    pub direction: String,
    pub threshold: f64,
    pub families: Vec<String>,
    pub task: Option<String>,
    pub preprocessing: Option<String>,
    pub replacement: Option<String>,
    pub description: Option<String>,
    pub risk: Option<String>,
}

/// Top-level snapshot. ``Default`` produces the empty KB so callers
/// can load lazily.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct KbSnapshot {
    /// Operator records by FQN.
    pub operators: FxHashMap<String, OperatorRecord>,
    /// Interface records by interface name.
    pub interfaces: FxHashMap<String, InterfaceRecord>,
    /// Concept hierarchy edges: concept → its parent family.
    /// Replaces the ``is_subclass_of`` traversal.
    pub concept_parents: FxHashMap<String, String>,
    /// Module name → package name. Replaces ``get_library_package_map``.
    pub libraries: FxHashMap<String, String>,
    /// Mitigations keyed by canonical name.
    pub mitigations: FxHashMap<String, MitigationRecord>,
    /// Risk-name → sorted list of mitigations.
    pub mitigations_by_risk: FxHashMap<String, Vec<MitigationRecord>>,
    /// Task-name → list of metric operator FQNs.
    pub metrics_by_task: FxHashMap<String, Vec<String>>,
    /// Risk-name → list of model families that fall under that risk.
    pub families_for_risk: FxHashMap<String, Vec<String>>,
    /// Metric FQN → list of risks the metric surfaces.
    pub risks_surfaced_by_metric: FxHashMap<String, Vec<String>>,
    /// All pathways in declaration order.
    pub pathways: Vec<PathwayRecord>,
    /// Method names that appear on any interface (for fast membership
    /// checks in the operator resolver). Mirrors
    /// ``get_all_interface_methods``.
    pub interface_methods: Vec<String>,

    // ── AI Debugger lookups ────────────────────────────────────────
    // Added 2026-05-05 to support the rust risk_chain handlers.
    // ``#[serde(default)]`` on each so old snapshot files (built
    // before these were populated) still deserialize.
    /// Risk → list of principles it threatens. Populated from
    /// ``(risk)-[:is_threat_to]->(principle)`` at build time.
    #[serde(default)]
    pub principles_by_risk: FxHashMap<String, Vec<String>>,
    /// Risk → list of check-node names that detect it. Populated from
    /// ``(check)-[:checks_for]->(risk)``.
    #[serde(default)]
    pub checks_by_risk: FxHashMap<String, Vec<String>>,
    /// Mitigation name → (short_template, long_template). Populated
    /// from ``(m)-[:with_description]->(short)`` and
    /// ``(m)-[:with_long_description]->(long)``.
    #[serde(default)]
    pub mitigation_descriptions: FxHashMap<String, (String, String)>,
}

impl KbSnapshot {
    /// Parse a JSON-serialised snapshot. The Python ingest writes the
    /// shape this method consumes.
    pub fn from_json(s: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(s)
    }

    // -----------------------------------------------------------------
    // Operator queries
    // -----------------------------------------------------------------

    /// Mirrors ``get_operator_interface``.
    pub fn operator_interface(&self, fqn: &str) -> Option<&str> {
        self.operators
            .get(fqn)
            .and_then(|r| r.interface.as_deref())
    }

    /// Mirrors ``get_operator_family``. Resolves the concept hierarchy
    /// to find the top-level family if a direct family isn't recorded.
    pub fn operator_family(&self, fqn: &str) -> Option<String> {
        let rec = self.operators.get(fqn)?;
        if let Some(fam) = rec.family.clone() {
            return Some(fam);
        }
        // Fall back to walking the concept hierarchy via the operator's
        // recorded interface — useful for KB rules that bind family at
        // the interface level rather than per-operator.
        rec.interface
            .as_ref()
            .and_then(|i| self.concept_root(i.as_str()).map(|s| s.to_string()))
    }

    /// Walk ``concept_parents`` upwards to find the root concept (the
    /// family). Returns the input itself when it has no parent.
    pub fn concept_root<'a>(&'a self, mut concept: &'a str) -> Option<&'a str> {
        let mut visited = 0;
        while let Some(parent) = self.concept_parents.get(concept) {
            concept = parent.as_str();
            visited += 1;
            if visited > 32 {
                // Defensive guard: KB hierarchy should never be that
                // deep; abort to avoid pathological cycles.
                return None;
            }
        }
        Some(concept)
    }

    /// Mirrors ``get_operator_parameters``. Returns a clone so callers
    /// can mutate (e.g. mask defaults) without affecting the snapshot.
    pub fn operator_parameters(&self, fqn: &str) -> Vec<ParameterSpec> {
        self.operators
            .get(fqn)
            .map(|r| r.parameters.clone())
            .unwrap_or_default()
    }

    /// Mirrors ``get_operator_io``. ``(inputs, outputs)`` tuple per the
    /// Python contract.
    pub fn operator_io(&self, fqn: &str) -> Option<(Vec<IoSpec>, Vec<IoSpec>)> {
        self.operators
            .get(fqn)
            .map(|r| (r.inputs.clone(), r.outputs.clone()))
    }

    /// Mirrors ``get_operator_import_path``.
    pub fn operator_import_path(&self, fqn: &str) -> Option<&str> {
        self.operators
            .get(fqn)
            .and_then(|r| r.import_path.as_deref())
    }

    /// Mirrors ``get_operator_risks``.
    pub fn operator_risks(&self, fqn: &str) -> Vec<String> {
        self.operators
            .get(fqn)
            .map(|r| r.risks.clone())
            .unwrap_or_default()
    }

    /// Mirrors ``get_metric_display_name``.
    pub fn metric_display_name(&self, fqn: &str) -> Option<&str> {
        self.operators
            .get(fqn)
            .and_then(|r| r.display_name.as_deref())
    }

    /// Mirrors ``get_all_operators``. Returns lightweight summaries
    /// suitable for catalog enumeration; per-operator detail goes
    /// through the other accessors.
    pub fn all_operators(&self) -> Vec<OperatorInfo> {
        self.operators
            .values()
            .map(|r| OperatorInfo {
                name: r.name.clone(),
                interface: r.interface.clone(),
                tasks: r.tasks.clone(),
                family: r.family.clone(),
            })
            .collect()
    }

    /// Operators that perform a given task.
    /// Mirrors ``get_operators_for_task``.
    pub fn operators_for_task(&self, task: &str) -> Vec<String> {
        self.operators
            .values()
            .filter(|r| r.tasks.iter().any(|t| t == task))
            .map(|r| r.name.clone())
            .collect()
    }

    /// Operators that implement a given interface.
    /// Mirrors ``get_operators_by_interface``.
    pub fn operators_by_interface(&self, iface: &str) -> Vec<String> {
        self.operators
            .values()
            .filter(|r| r.interface.as_deref() == Some(iface))
            .map(|r| r.name.clone())
            .collect()
    }

    // -----------------------------------------------------------------
    // Interface queries
    // -----------------------------------------------------------------

    /// Mirrors ``get_method_sequence``.
    pub fn method_sequence(&self, iface: &str) -> Vec<String> {
        self.interfaces
            .get(iface)
            .map(|r| r.method_sequence.clone())
            .unwrap_or_default()
    }

    /// Mirrors ``get_interface_io``.
    pub fn interface_io(&self, iface: &str) -> Option<(Vec<IoSpec>, Vec<IoSpec>)> {
        self.interfaces
            .get(iface)
            .map(|r| (r.inputs.clone(), r.outputs.clone()))
    }

    /// Mirrors ``get_method_io``. Returns the per-method I/O map for
    /// an interface.
    pub fn method_io(
        &self,
        iface: &str,
    ) -> FxHashMap<String, (Vec<IoSpec>, Vec<IoSpec>)> {
        self.interfaces
            .get(iface)
            .map(|r| r.method_io.clone())
            .unwrap_or_default()
    }

    /// Mirrors ``get_interface_attributes``.
    pub fn interface_attributes(&self, iface: &str) -> Vec<String> {
        self.interfaces
            .get(iface)
            .map(|r| r.attributes.clone())
            .unwrap_or_default()
    }

    /// Mirrors ``get_all_interface_methods``. Used by the operator
    /// resolver to detect compound-operator method names (``fit``,
    /// ``predict``, ...) without per-operator KB lookup.
    pub fn all_interface_methods(&self) -> &[String] {
        &self.interface_methods
    }

    // -----------------------------------------------------------------
    // Mitigation / risk / pathway queries
    // -----------------------------------------------------------------

    /// Mirrors ``get_mitigation_kb_spec``.
    pub fn mitigation_spec(&self, name: &str) -> Option<&MitigationRecord> {
        self.mitigations.get(name)
    }

    /// Mirrors ``get_mitigations_batch`` for a single risk. Callers
    /// loop the input list themselves.
    pub fn mitigations_for_risk(&self, risk: &str) -> Vec<MitigationRecord> {
        self.mitigations_by_risk
            .get(risk)
            .cloned()
            .unwrap_or_default()
    }

    /// Mirrors ``get_metrics_for_task``.
    pub fn metrics_for_task(&self, task: &str) -> Vec<String> {
        self.metrics_by_task
            .get(task)
            .cloned()
            .unwrap_or_default()
    }

    /// Mirrors ``get_sensitive_families_for_risk``.
    pub fn sensitive_families_for_risk(&self, risk: &str) -> Vec<String> {
        self.families_for_risk
            .get(risk)
            .cloned()
            .unwrap_or_default()
    }

    /// Mirrors ``get_risks_surfaced_by_metric``.
    pub fn risks_surfaced_by_metric(&self, metric: &str) -> Vec<String> {
        self.risks_surfaced_by_metric
            .get(metric)
            .cloned()
            .unwrap_or_default()
    }

    /// Mirrors ``get_all_pathways``. Returns by reference; callers
    /// that need owned data should clone selectively.
    pub fn all_pathways(&self) -> &[PathwayRecord] {
        &self.pathways
    }

    /// Mirrors ``get_library_package_map``.
    pub fn library_package_map(&self) -> &FxHashMap<String, String> {
        &self.libraries
    }

    /// Mirrors ``get_model_family``. Resolves the operator's family,
    /// then walks the concept hierarchy if the operator's recorded
    /// family is itself a subclass of a higher-level model family
    /// (Tree-Based / Linear / Kernel / ...).
    pub fn model_family(&self, fqn: &str) -> Option<String> {
        let fam = self.operator_family(fqn)?;
        Some(self.concept_root(fam.as_str()).unwrap_or(fam.as_str()).to_string())
    }

    // -----------------------------------------------------------------
    // AI Debugger queries (rust handlers under engine/backend)
    // -----------------------------------------------------------------

    /// Mirrors python ``_kb_principles_for_risk``:
    /// ``(risk)-[:is_threat_to]->(principle)``.
    pub fn principles_for_risk(&self, risk: &str) -> Vec<String> {
        self.principles_by_risk.get(risk).cloned().unwrap_or_default()
    }

    /// Mirrors python ``_kb_checks_for_risk``:
    /// ``(check)-[:checks_for]->(risk)``.
    pub fn checks_for_risk(&self, risk: &str) -> Vec<String> {
        self.checks_by_risk.get(risk).cloned().unwrap_or_default()
    }

    /// Short + long description templates for a mitigation. The
    /// templates contain ``{operator}`` / ``{risk}`` / ``{task}`` /
    /// ``{alternatives}`` placeholders the caller fills with
    /// ``str::replace`` (no ``format_map`` in rust — explicit substitution
    /// in ``risk_chain::format_template``).
    pub fn mitigation_description(&self, mitigation: &str) -> Option<(String, String)> {
        self.mitigation_descriptions.get(mitigation).cloned()
    }

    /// Mirrors python ``_kb_direct_alternatives``: find operators that
    /// ``performs`` the same task as ``operator`` but do NOT
    /// ``might_introduce`` ``risk``. Returns ``(task, alternatives)``
    /// where task is the *first* task name the operator declares (or
    /// empty if none).
    pub fn direct_alternatives(
        &self,
        operator: &str,
        risk: &str,
    ) -> (String, Vec<String>) {
        let Some(op) = self.operators.get(operator) else {
            return (String::new(), Vec::new());
        };
        let Some(task) = op.tasks.first() else {
            return (String::new(), Vec::new());
        };
        let alts: Vec<String> = self
            .operators_for_task(task)
            .into_iter()
            .filter(|other| {
                other != operator
                    && self
                        .operators
                        .get(other)
                        .map(|o| !o.risks.iter().any(|r| r == risk))
                        .unwrap_or(false)
            })
            .collect();
        (task.clone(), alts)
    }
}

// ---------------------------------------------------------------------------
// Petgraph view — used for ``KbTaskTopology``-style traversals that need
// arbitrary-depth ancestor / descendant walks. The lookup-table queries
// don't need this; this view is for the (rarer) graph-walking cases.
// ---------------------------------------------------------------------------

/// Concept-hierarchy view of the snapshot. Edges go child → parent
/// (``is_subclass_of`` direction), so ``ancestors`` is reachability
/// from a node out-edges.
pub struct ConceptHierarchy {
    pub graph: DiGraph<String, ()>,
    pub by_name: FxHashMap<String, NodeIndex>,
}

impl ConceptHierarchy {
    /// Build the concept hierarchy from a snapshot. ``O(|concepts|)``.
    pub fn from_snapshot(snap: &KbSnapshot) -> Self {
        let mut graph = DiGraph::<String, ()>::new();
        let mut by_name: FxHashMap<String, NodeIndex> = FxHashMap::default();

        let intern = |g: &mut DiGraph<String, ()>,
                          map: &mut FxHashMap<String, NodeIndex>,
                          name: &str|
         -> NodeIndex {
            if let Some(&ix) = map.get(name) {
                return ix;
            }
            let ix = g.add_node(name.to_string());
            map.insert(name.to_string(), ix);
            ix
        };

        for (child, parent) in snap.concept_parents.iter() {
            let ci = intern(&mut graph, &mut by_name, child);
            let pi = intern(&mut graph, &mut by_name, parent);
            graph.add_edge(ci, pi, ());
        }

        Self { graph, by_name }
    }

    /// Walk ancestors of ``concept`` (parents, grandparents, …) until
    /// the root. Returns names in walked order. Empty for unknown
    /// concepts. Defensive against cycles via the visited guard.
    pub fn ancestors(&self, concept: &str) -> Vec<&str> {
        let mut out: Vec<&str> = Vec::new();
        let Some(&start) = self.by_name.get(concept) else {
            return out;
        };
        let mut visited: rustc_hash::FxHashSet<NodeIndex> = Default::default();
        let mut current = start;
        while let Some(parent) = self
            .graph
            .neighbors_directed(current, petgraph::Direction::Outgoing)
            .next()
        {
            if !visited.insert(parent) {
                break;
            }
            out.push(self.graph[parent].as_str());
            current = parent;
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_snapshot() -> KbSnapshot {
        let mut snap = KbSnapshot::default();
        snap.operators.insert(
            "sklearn.preprocessing.StandardScaler".to_string(),
            OperatorRecord {
                name: "sklearn.preprocessing.StandardScaler".to_string(),
                interface: Some("Sklearn Transformer".to_string()),
                family: Some("Standard Scaling".to_string()),
                tasks: vec!["Feature Engineering".to_string()],
                ..Default::default()
            },
        );
        snap.operators.insert(
            "sklearn.ensemble.RandomForestClassifier".to_string(),
            OperatorRecord {
                name: "sklearn.ensemble.RandomForestClassifier".to_string(),
                interface: Some("Sklearn Estimator".to_string()),
                family: Some("Random Forest".to_string()),
                tasks: vec!["Classification".to_string()],
                ..Default::default()
            },
        );
        snap.concept_parents
            .insert("Standard Scaling".to_string(), "Scaling".to_string());
        snap.concept_parents
            .insert("Random Forest".to_string(), "Ensemble".to_string());
        snap.concept_parents
            .insert("Ensemble".to_string(), "Tree-Based".to_string());
        snap.interfaces.insert(
            "Sklearn Estimator".to_string(),
            InterfaceRecord {
                name: "Sklearn Estimator".to_string(),
                method_sequence: vec!["__init__".to_string(), "fit".to_string(), "predict".to_string()],
                ..Default::default()
            },
        );
        snap
    }

    #[test]
    fn operator_interface_resolves() {
        let s = sample_snapshot();
        assert_eq!(
            s.operator_interface("sklearn.preprocessing.StandardScaler"),
            Some("Sklearn Transformer"),
        );
        assert_eq!(s.operator_interface("nonexistent.op"), None);
    }

    #[test]
    fn operator_family_falls_through_concept_root() {
        let s = sample_snapshot();
        assert_eq!(
            s.operator_family("sklearn.ensemble.RandomForestClassifier"),
            Some("Random Forest".to_string()),
        );
        // Walking "Random Forest" up reaches "Tree-Based".
        assert_eq!(
            s.model_family("sklearn.ensemble.RandomForestClassifier"),
            Some("Tree-Based".to_string()),
        );
    }

    #[test]
    fn method_sequence_returns_in_order() {
        let s = sample_snapshot();
        assert_eq!(
            s.method_sequence("Sklearn Estimator"),
            vec!["__init__", "fit", "predict"],
        );
        assert!(s.method_sequence("Unknown Interface").is_empty());
    }

    #[test]
    fn json_roundtrip() {
        let s = sample_snapshot();
        let j = serde_json::to_string(&s).unwrap();
        let s2 = KbSnapshot::from_json(&j).unwrap();
        assert_eq!(
            s2.operator_interface("sklearn.preprocessing.StandardScaler"),
            Some("Sklearn Transformer"),
        );
    }

    #[test]
    fn concept_hierarchy_walks_ancestors() {
        let s = sample_snapshot();
        let h = ConceptHierarchy::from_snapshot(&s);
        let ancestors = h.ancestors("Random Forest");
        assert_eq!(ancestors, vec!["Ensemble", "Tree-Based"]);
        assert!(h.ancestors("Unknown").is_empty());
    }

    #[test]
    fn operators_for_task_filters() {
        let s = sample_snapshot();
        let ops = s.operators_for_task("Classification");
        assert_eq!(ops.len(), 1);
        assert_eq!(ops[0], "sklearn.ensemble.RandomForestClassifier");
    }
}
