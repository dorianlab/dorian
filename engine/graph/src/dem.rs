//! Dorian Execution Model (DEM) primitives.
//!
//! Ptolemy-II-inspired heterogeneous domain composition. A graph is a
//! collection of actors connected by channels; each actor is scheduled by
//! some domain (SDF, DE, ...); channels carry typed tokens.
//!
//! The existing `model::ProcessGraph` carries structural information
//! (nodes, edges). This module adds the per-actor and per-channel
//! annotations a domain scheduler needs:
//!
//! * `DomainKind`   — which scheduler owns an actor (SDF / DE today).
//! * `ActorAnnotations` — determinism, warmstart, version, token rates.
//! * `ChannelAnnotations` — declared token type, delivery mode.
//! * `Domain` trait — per-domain `can_fire` / `fire` protocol.
//!
//! Annotations live alongside `ProcessGraph` rather than inside it so the
//! existing structural types stay untouched and compatible with the
//! Python DAG wire format. A parser populates both together; see
//! `graph::parser`.

use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

use crate::model::{Node, NodeId, ProcessGraph};

// ---------------------------------------------------------------------------
// Domain
// ---------------------------------------------------------------------------

/// Which scheduling domain owns an actor.
///
/// v1 ships two domains. Additional domains (PN for streams, FSM for modal
/// behaviour, CT for continuous time) slot in without touching existing
/// actors — the `Domain` trait is the extension point.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DomainKind {
    /// Synchronous Dataflow. Fixed token-rate per firing. Today's
    /// pipelines map here: one input dataframe → one output dataframe.
    #[default]
    Sdf,
    /// Discrete Event. Timestamped triggers, asynchronous firings.
    /// The event bus + AI Debugger live here.
    De,
}

/// Per-domain scheduling protocol. Each domain implements this once;
/// actor-level details come from `ActorAnnotations`.
///
/// Kept intentionally small for v1 — concrete schedulers in the `sdf`
/// and `de` crates layer richer behaviour (cache consultation, event
/// queues) on top of these primitives.
pub trait Domain {
    /// The domain this implementation owns.
    fn kind(&self) -> DomainKind;

    /// True when the actor's preconditions to fire are met.
    ///
    /// For SDF this is "all input ports have their declared token
    /// rate available". For DE it is "a trigger event is queued".
    fn can_fire(&self, graph: &ProcessGraph, annotations: &DemAnnotations, node_id: &str) -> bool;
}

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

/// Whether an actor's output is determined by its inputs + params alone.
///
/// Gates cache eligibility. `Deterministic` ops participate in
/// content-addressable reuse; `NonDeterministic` always execute.
/// `Unknown` is the conservative default — the cache skips unknowns
/// until the KB or operator itself asserts a classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DeterminismClass {
    Deterministic,
    NonDeterministic,
    #[default]
    Unknown,
}

impl DeterminismClass {
    /// True iff the actor's firings may be cached.
    pub fn is_cacheable(self) -> bool {
        matches!(self, DeterminismClass::Deterministic)
    }
}

// ---------------------------------------------------------------------------
// Token type system (declared at channel, dynamic at payload)
// ---------------------------------------------------------------------------

/// Logical token type declared on a channel.
///
/// Strongly typed at the channel level so schedulers can catch rate and
/// type mismatches before dispatch; concrete payloads stay dynamic
/// (serialised blobs crossing the exec-jobs boundary).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenType {
    /// Tabular data (pandas DataFrame, Arrow table).
    DataFrame,
    /// 1-D or 2-D numeric array.
    Array,
    /// Scalar parameter value (int, float, string, bool).
    Scalar,
    /// Arbitrary Python object serialised over the wire.
    #[default]
    PyObject,
    /// Trained-model artifact (scikit-learn estimator, torch state dict).
    Model,
    /// Fit statistics (mean, variance, class priors). Mergeable per MaR.
    Statistics,
    /// Discrete event payload (JSON map).
    Event,
}

// ---------------------------------------------------------------------------
// Ports
// ---------------------------------------------------------------------------

/// Input or output port on an actor.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortDeclaration {
    /// Port position (positional index) or keyword name, as a string.
    /// Mirrors the `Edge.position` shape in the structural graph.
    pub handle: String,
    /// Declared token type. `TokenType::PyObject` is the wildcard.
    #[serde(default)]
    pub token_type: TokenType,
    /// SDF token rate — how many tokens consumed/produced per firing.
    /// Today every actor is rate=1; extension point for future batch ops.
    #[serde(default = "default_rate")]
    pub rate: u32,
}

fn default_rate() -> u32 {
    1
}

// ---------------------------------------------------------------------------
// Actor annotations
// ---------------------------------------------------------------------------

/// Everything the scheduler needs about an actor beyond its structural
/// `Node` — domain, determinism, ports, and version/warmstart flags.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActorAnnotations {
    /// Which domain schedules this actor.
    #[serde(default)]
    pub domain: DomainKind,
    /// Determinism class — gates cache participation.
    #[serde(default)]
    pub determinism: DeterminismClass,
    /// Whether the operator implements a warmstart path from a prior
    /// materialised model (Derakhshan thesis Ch 2 reuse strategy).
    #[serde(default)]
    pub is_warmstartable: bool,
    /// Operator version — embedded in the cache key so a library
    /// upgrade invalidates affected entries. `None` means "unknown";
    /// cache skips unversioned ops by default.
    #[serde(default)]
    pub operator_version: Option<String>,
    /// Name of this operator's reproducibility seed parameter, if
    /// any (e.g. `"random_state"` for many sklearn ops). When
    /// declared, the cache's eligibility check forces Bypass at
    /// runtime IF no upstream Parameter node is wired to that
    /// handle — an unseeded stochastic op must never serve from
    /// the cache. When the parameter IS wired, its value flows into
    /// the cache key like any other param.
    #[serde(default)]
    pub random_state_param_name: Option<String>,
    /// Declared input ports (ordered).
    #[serde(default)]
    pub inputs: Vec<PortDeclaration>,
    /// Declared output ports (ordered).
    #[serde(default)]
    pub outputs: Vec<PortDeclaration>,
}

impl Default for ActorAnnotations {
    fn default() -> Self {
        ActorAnnotations {
            domain: DomainKind::Sdf,
            determinism: DeterminismClass::Unknown,
            is_warmstartable: false,
            operator_version: None,
            random_state_param_name: None,
            inputs: Vec::new(),
            outputs: Vec::new(),
        }
    }
}

impl ActorAnnotations {
    /// An SDF actor with unknown determinism and no declared ports.
    pub fn sdf_default() -> Self {
        ActorAnnotations {
            domain: DomainKind::Sdf,
            determinism: DeterminismClass::Unknown,
            ..Default::default()
        }
    }

    /// A DE actor — used for the async operators (Cancel, mitigation
    /// triggers) that already flow through the event bus.
    pub fn de_default() -> Self {
        ActorAnnotations {
            domain: DomainKind::De,
            determinism: DeterminismClass::NonDeterministic,
            ..Default::default()
        }
    }
}

// ---------------------------------------------------------------------------
// Channel annotations
// ---------------------------------------------------------------------------

/// Keyed by (source, destination, position-as-string) so annotations
/// survive across serialisation boundaries where the underlying
/// `Edge` index may change.
#[derive(Debug, Clone, Hash, Eq, PartialEq, Serialize, Deserialize)]
pub struct ChannelKey {
    pub source: NodeId,
    pub destination: NodeId,
    /// Position rendered to string (keyword name or decimal index).
    pub position: String,
}

impl ChannelKey {
    pub fn from_edge(edge: &crate::model::Edge) -> Self {
        let position = match &edge.position {
            crate::model::Position::Index(i) => i.to_string(),
            crate::model::Position::Keyword(k) => k.clone(),
        };
        ChannelKey {
            source: edge.source.clone(),
            destination: edge.destination.clone(),
            position,
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ChannelAnnotations {
    #[serde(default)]
    pub token_type: TokenType,
    /// Population of this channel's declared rate — defaults to 1 (SDF).
    #[serde(default = "default_rate")]
    pub rate: u32,
}

// ---------------------------------------------------------------------------
// DEM annotations container
// ---------------------------------------------------------------------------

/// Sidecar map holding DEM annotations for a `ProcessGraph`. The parser
/// populates this; schedulers consult it per firing.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DemAnnotations {
    pub actors: FxHashMap<NodeId, ActorAnnotations>,
    pub channels: FxHashMap<ChannelKey, ChannelAnnotations>,
}

impl DemAnnotations {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn actor(&self, node_id: &str) -> Option<&ActorAnnotations> {
        self.actors.get(node_id)
    }

    pub fn actor_mut(&mut self, node_id: &str) -> &mut ActorAnnotations {
        self.actors
            .entry(node_id.to_string())
            .or_insert_with(ActorAnnotations::default)
    }

    pub fn channel(&self, key: &ChannelKey) -> Option<&ChannelAnnotations> {
        self.channels.get(key)
    }

    /// Nodes registered under a specific domain.
    pub fn nodes_in_domain(&self, domain: DomainKind) -> Vec<&str> {
        self.actors
            .iter()
            .filter(|(_, a)| a.domain == domain)
            .map(|(id, _)| id.as_str())
            .collect()
    }
}

// ---------------------------------------------------------------------------
// Built-in classification helpers
// ---------------------------------------------------------------------------

/// Best-guess determinism for a node from the node kind + operator FQN
/// alone, used when the KB hasn't annotated the operator yet.
///
/// The policy is conservative: opt out on anything we can't prove.
/// The parser calls this; schedulers should prefer KB-driven
/// annotations when available.
pub fn classify_determinism_builtin(node: &Node) -> DeterminismClass {
    match node {
        // User-authored code: the semantics depend on the snippet
        // body. We can't reason about determinism — never cache.
        Node::Snippet(_) => DeterminismClass::NonDeterministic,
        // Constants are trivially deterministic.
        Node::Parameter(_) => DeterminismClass::Deterministic,
        Node::Operator(op) => classify_operator_builtin(&op.name),
        // Pattern nodes only appear in rewrite rules, not execution.
        Node::Node(_) => DeterminismClass::Unknown,
        // Groups are compound; classification happens post-expansion.
        Node::Group(_) => DeterminismClass::Unknown,
    }
}

/// Per-FQN determinism classification. Conservative default is
/// `Unknown` — the cache skips unknowns. Explicit `Deterministic`
/// for the common sklearn / pandas primitives that carry no hidden
/// randomness; explicit `NonDeterministic` for the known stochastic
/// ones (LLM, generative models).
pub fn classify_operator_builtin(fqn: &str) -> DeterminismClass {
    // Known non-deterministic operators.
    // LLM chat completion — stochastic sampling.
    if fqn == "openrouter.chat.completion" {
        return DeterminismClass::NonDeterministic;
    }
    // Platform primitives are opaque before expansion; treat as unknown.
    if fqn.starts_with("dorian.io.") {
        return DeterminismClass::Unknown;
    }
    // pandas I/O: deterministic given a fixed file (input_key
    // carries the file-content hash at the root).
    if fqn.starts_with("pandas.") {
        return DeterminismClass::Deterministic;
    }
    // sklearn ops: deterministic when `random_state` is set. We can't
    // tell without inspecting params, so default Deterministic and
    // let the parser downgrade if a `random_state` parameter is
    // connected AND un-set. For now: treat as deterministic — the
    // cache key includes the params, so an unset random_state still
    // maps to a stable key. The real risk is implicit-PRNG ops.
    if fqn.starts_with("sklearn.") {
        return DeterminismClass::Deterministic;
    }
    // Guardrails: content-safety classifiers are typically
    // deterministic given the model + input; leave as unknown until
    // the KB annotates.
    DeterminismClass::Unknown
}

/// Best-guess reproducibility-seed parameter name per operator
/// FQN. Returns `Some("random_state")` for common sklearn ops whose
/// default `random_state` is `None` (non-deterministic when unset),
/// `None` otherwise. The KB supersedes this once populated.
///
/// The conservative policy: any sklearn operator that accepts
/// `random_state` as a parameter gets flagged. The downstream
/// eligibility check verifies that an upstream Parameter is
/// actually wired to that handle; if not, caching is disabled for
/// that firing regardless of what the other params look like.
pub fn classify_random_state_param_builtin(fqn: &str) -> Option<String> {
    // Small allowlist of sklearn ops where `random_state` is a
    // well-known accepted parameter. Operators whose behavior is
    // genuinely independent of a seed (e.g. `StandardScaler`,
    // `accuracy_score`) are omitted intentionally.
    const HAS_RANDOM_STATE: &[&str] = &[
        "sklearn.model_selection.train_test_split",
        "sklearn.model_selection.KFold",
        "sklearn.model_selection.StratifiedKFold",
        "sklearn.model_selection.ShuffleSplit",
        "sklearn.model_selection.StratifiedShuffleSplit",
        "sklearn.ensemble.RandomForestClassifier",
        "sklearn.ensemble.RandomForestRegressor",
        "sklearn.ensemble.ExtraTreesClassifier",
        "sklearn.ensemble.ExtraTreesRegressor",
        "sklearn.ensemble.GradientBoostingClassifier",
        "sklearn.ensemble.GradientBoostingRegressor",
        "sklearn.ensemble.AdaBoostClassifier",
        "sklearn.ensemble.AdaBoostRegressor",
        "sklearn.ensemble.RandomTreesEmbedding",
        "sklearn.tree.DecisionTreeClassifier",
        "sklearn.tree.DecisionTreeRegressor",
        "sklearn.tree.ExtraTreeClassifier",
        "sklearn.tree.ExtraTreeRegressor",
        "sklearn.linear_model.SGDClassifier",
        "sklearn.linear_model.SGDRegressor",
        "sklearn.linear_model.LogisticRegression",
        "sklearn.linear_model.Perceptron",
        "sklearn.linear_model.PassiveAggressiveClassifier",
        "sklearn.linear_model.PassiveAggressiveRegressor",
        "sklearn.neural_network.MLPClassifier",
        "sklearn.neural_network.MLPRegressor",
        "sklearn.cluster.KMeans",
        "sklearn.cluster.MiniBatchKMeans",
        "sklearn.cluster.SpectralClustering",
        "sklearn.decomposition.PCA",
        "sklearn.decomposition.TruncatedSVD",
        "sklearn.decomposition.FastICA",
        "sklearn.decomposition.KernelPCA",
        "sklearn.kernel_approximation.Nystroem",
        "sklearn.kernel_approximation.RBFSampler",
        "sklearn.manifold.TSNE",
        "sklearn.random_projection.GaussianRandomProjection",
        "sklearn.random_projection.SparseRandomProjection",
        "sklearn.mixture.GaussianMixture",
        "sklearn.mixture.BayesianGaussianMixture",
        "sklearn.svm.SVC",
        "sklearn.svm.NuSVC",
        "sklearn.svm.LinearSVC",
        "sklearn.svm.SVR",
        "sklearn.svm.NuSVR",
        "sklearn.svm.LinearSVR",
    ];
    if HAS_RANDOM_STATE.contains(&fqn) {
        Some("random_state".to_string())
    } else {
        None
    }
}

/// Domain classification from the FQN — defaults to SDF with a small
/// allowlist of async primitives mapped to DE.
///
/// The allowlist is intentionally tiny for v1. Extend as new async
/// operators land; anything routed through the event bus (AI
/// Debugger, cancel, mitigation triggers) should appear here.
pub fn classify_domain_builtin(fqn: &str) -> DomainKind {
    // Operators that only ever fire in response to an async trigger.
    const DE_OPERATORS: &[&str] = &[
        "dorian.cancel",
        "dorian.mitigation.trigger",
        "dorian.ai_debugger.rewrite",
    ];
    if DE_OPERATORS.contains(&fqn) {
        DomainKind::De
    } else {
        DomainKind::Sdf
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Operator;

    #[test]
    fn determinism_snippet_is_non_deterministic() {
        let snippet = Node::Snippet(crate::model::Snippet {
            name: "foo".into(),
            code: "def foo(x): return x".into(),
            language: "python".into(),
        });
        assert_eq!(
            classify_determinism_builtin(&snippet),
            DeterminismClass::NonDeterministic
        );
    }

    #[test]
    fn determinism_llm_is_non_deterministic() {
        assert_eq!(
            classify_operator_builtin("openrouter.chat.completion"),
            DeterminismClass::NonDeterministic
        );
    }

    #[test]
    fn determinism_sklearn_is_deterministic() {
        assert_eq!(
            classify_operator_builtin("sklearn.preprocessing.StandardScaler"),
            DeterminismClass::Deterministic
        );
    }

    #[test]
    fn domain_defaults_to_sdf() {
        assert_eq!(
            classify_domain_builtin("sklearn.preprocessing.StandardScaler"),
            DomainKind::Sdf
        );
    }

    #[test]
    fn domain_cancel_is_de() {
        assert_eq!(classify_domain_builtin("dorian.cancel"), DomainKind::De);
    }

    #[test]
    fn determinism_class_cacheable() {
        assert!(DeterminismClass::Deterministic.is_cacheable());
        assert!(!DeterminismClass::NonDeterministic.is_cacheable());
        assert!(!DeterminismClass::Unknown.is_cacheable());
    }

    #[test]
    fn annotations_sdf_default_has_sdf_domain() {
        let a = ActorAnnotations::sdf_default();
        assert_eq!(a.domain, DomainKind::Sdf);
    }

    #[test]
    fn annotations_de_default_is_non_deterministic() {
        let a = ActorAnnotations::de_default();
        assert_eq!(a.domain, DomainKind::De);
        assert_eq!(a.determinism, DeterminismClass::NonDeterministic);
    }

    #[test]
    fn dem_annotations_nodes_in_domain() {
        let mut dem = DemAnnotations::new();
        dem.actors
            .insert("a".into(), ActorAnnotations::sdf_default());
        dem.actors.insert("b".into(), ActorAnnotations::de_default());
        dem.actors
            .insert("c".into(), ActorAnnotations::sdf_default());
        let sdf = dem.nodes_in_domain(DomainKind::Sdf);
        assert_eq!(sdf.len(), 2);
        let de = dem.nodes_in_domain(DomainKind::De);
        assert_eq!(de.len(), 1);
    }

    #[test]
    fn operator_node_uses_fqn_classification() {
        let node = Node::Operator(Operator {
            name: "openrouter.chat.completion".into(),
            language: "python".into(),
            tasks: vec![],
        });
        assert_eq!(
            classify_determinism_builtin(&node),
            DeterminismClass::NonDeterministic
        );
    }
}
