//! KB DSL parser + snapshot builder.
//!
//! The curated knowledge sources live in plain text (`.kb` files);
//! each line is a statement in the predicate DSL described in
//! internal design note. Examples:
//!
//! ```text
//! sklearn.svm.SVC has parameter C; is of type float; with default 1.0
//! Resampling is a Mitigation
//! Resampling might mitigate Class Imbalance
//! ```
//!
//! Two responsibilities:
//!
//! 1. [`parse_statements`] turns a text blob into `(subject, predicate, object)`
//!    triples, mirroring the python `dorian.knowledge.base.parse` semantics:
//!    `;`-chained statements thread fresh UUIDs through intermediaries;
//!    `has_name` collapses those onto human-readable labels at lookup time.
//!
//! 2. [`build_snapshot`] walks the parsed triples (plus optional extra
//!    lines from the io-crawler / postgres overlay) and populates a
//!    [`KbSnapshot`] directly — no Neo4j, no Cypher, no python.
//!
//! Errors are collected into [`ParseError`] entries and returned alongside
//! the result so callers can surface every bad line in one report.

use std::collections::{BTreeMap, HashMap, HashSet};

use rustc_hash::{FxHashMap, FxHashSet};
use uuid::Uuid;

use crate::kb::snapshot::{
    InterfaceRecord, KbSnapshot, MitigationRecord, OperatorRecord, PathwayRecord,
};
use crate::kb::types::{IoSpec, ParameterSpec};

/// Predicate vocabulary, declaration order matters — longer predicates
/// must precede shorter substrings (`with_long_description` before
/// `with_description`, `is_subclass_of` before `is_a`, …). Mirrors
/// `dorian.knowledge.base.Predicate`.
const PREDICATES: &[&str] = &[
    // 3-word predicates first
    "is_subclass_of",
    "is_equivalent_to",
    "with_long_description",
    "with_log_scale",
    "is_dimension_of",
    "and_contains_family",
    "and_performs_task",
    "has_decision_function",
    // 2-word
    "is_an",
    "is_a",
    "is_of_type",
    "has_parameter",
    "has_input",
    "has_output",
    "has_attribute",
    "has_package",
    "has_position",
    "has_name",
    "applies_to",
    "with_description",
    "with_parameter",
    "with_default",
    "with_choices",
    "with_low",
    "with_high",
    "contributes_to",
    "might_introduce",
    "is_threat_to",
    "attributes_to",
    "should_ensure",
    "might_mitigate",
    "surfaces_risk",
    "sensitive_family",
    "belongs_to_family",
    "suggests_preprocessing",
    "suggests_replacement",
    "when_below",
    "when_above",
    "implemented_by",
    "represents",
    "on_split",
    "performs",
    "has_threshold",
    "has",
    "implements",
    "ensures",
    "calls",
    "fallback",
    "checks_for",
    "evaluates",
];

/// One curated triple. ``object`` is either a literal value (`"float"`,
/// `"1.0"`) or an anonymous UUID intermediary that other triples resolve
/// via `has_name`.
#[derive(Debug, Clone)]
pub struct Triple {
    pub subject: String,
    pub predicate: String,
    pub object: String,
}

/// One parse failure. ``line_no`` is 1-indexed within ``source``.
#[derive(Debug, Clone)]
pub struct ParseError {
    pub source: String,
    pub line_no: usize,
    pub line: String,
    pub message: String,
}

/// Parse one DSL statement (no separator splits — caller does the
/// per-line iteration). Returns the triples for the head + recurses
/// over `;`-chained tails. ``id_factory`` produces fresh UUIDs for
/// chain intermediaries.
fn parse_statement(
    statement: &str,
    id_factory: &mut impl FnMut() -> String,
) -> Result<Vec<Triple>, String> {
    let separator = ';';
    let (head, rest) = match statement.split_once(separator) {
        Some((h, r)) => (h, Some(r)),
        None => (statement, None),
    };

    for predicate in PREDICATES {
        let needle = predicate.replace('_', " ");
        let needle_padded = format!(" {} ", needle);
        // Prefer space-padded match so ``has`` doesn't sneak inside
        // ``hashing``. Fall back to plain substring (matches python's
        // ``e in part``) for statements where the subject is empty
        // or whitespace-stripped on either side.
        let (idx, span) = if let Some(i) = head.find(&needle_padded) {
            (i, needle_padded.len())
        } else if let Some(i) = head.find(&needle) {
            (i, needle.len())
        } else {
            continue;
        };
        let subject = head[..idx].trim().to_string();
        let object_raw = head[idx + span..].trim().to_string();
        let object = unquote(&object_raw);
        {

            let mut out = Vec::new();
            if let Some(rest_str) = rest {
                let intermediary = id_factory();
                out.push(Triple {
                    subject,
                    predicate: predicate.to_string(),
                    object: intermediary.clone(),
                });
                out.push(Triple {
                    subject: intermediary.clone(),
                    predicate: "has_name".to_string(),
                    object,
                });
                let next_input = format!("{}{}", intermediary, rest_str);
                let inner = parse_statement(&next_input, id_factory)?;
                out.extend(inner);
            } else {
                out.push(Triple {
                    subject,
                    predicate: predicate.to_string(),
                    object,
                });
            }
            return Ok(out);
        }
    }

    Err(format!("unknown predicate in: {head}"))
}

fn unquote(s: &str) -> String {
    let s = s.trim();
    if s.len() >= 2 && s.starts_with('"') && s.ends_with('"') {
        s[1..s.len() - 1].to_string()
    } else {
        s.to_string()
    }
}

/// Parse a full text blob (multi-line). Comments (``#``-prefixed)
/// and blank lines are skipped silently. ``source_label`` is shown
/// in error reports — typically the file path.
pub fn parse_statements(text: &str, source_label: &str) -> (Vec<Triple>, Vec<ParseError>) {
    let mut triples = Vec::new();
    let mut errors = Vec::new();

    let mut id_factory = || Uuid::new_v4().simple().to_string();

    for (i, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        match parse_statement(line, &mut id_factory) {
            Ok(more) => triples.extend(more),
            Err(message) => errors.push(ParseError {
                source: source_label.to_string(),
                line_no: i + 1,
                line: line.to_string(),
                message,
            }),
        }
    }

    (triples, errors)
}

/// Indexed view over a triple set. Keeps UUID intermediaries distinct
/// (no rename collapse — mirrors ``OntologyKB``); ``display_name``
/// surfaces the human-readable label per node.
struct IndexedTriples {
    /// ``adj[node]`` returns the per-predicate destination buckets.
    adj: FxHashMap<String, FxHashMap<String, Vec<String>>>,
    /// ``display_name[uuid]`` → human label set by ``has_name``.
    display_name: FxHashMap<String, String>,
}

impl IndexedTriples {
    fn from_triples(triples: &[Triple]) -> Self {
        let mut adj: FxHashMap<String, FxHashMap<String, Vec<String>>> = FxHashMap::default();
        let mut display_name: FxHashMap<String, String> = FxHashMap::default();

        for t in triples {
            if t.predicate == "has_name" || t.predicate == "hasname" {
                display_name
                    .entry(t.subject.clone())
                    .or_insert_with(|| t.object.clone());
                continue;
            }
            adj.entry(t.subject.clone())
                .or_default()
                .entry(t.predicate.clone())
                .or_default()
                .push(t.object.clone());
        }
        IndexedTriples { adj, display_name }
    }

    fn display(&self, node: &str) -> String {
        self.display_name
            .get(node)
            .cloned()
            .unwrap_or_else(|| node.to_string())
    }

    fn out(&self, node: &str, predicate: &str) -> &[String] {
        self.adj
            .get(node)
            .and_then(|m| m.get(predicate))
            .map(|v| v.as_slice())
            .unwrap_or(&[])
    }

    /// Walk the property chain anchored at ``node``. The DSL parser
    /// threads predicates through fresh UUIDs (param → type → default
    /// → low → high → …) rather than clustering them on one node.
    /// Stops after a bounded number of hops to defend against malformed
    /// cycles.
    fn chain_props(&self, node: &str) -> FxHashMap<&'static str, String> {
        const PROP_KEYS: &[(&str, &str)] = &[
            ("is_of_type", "type"),
            ("with_default", "default"),
            ("with_low", "low"),
            ("with_high", "high"),
            ("with_choices", "choices"),
            ("with_log_scale", "log_scale"),
            ("has_position", "position"),
            ("represents", "role"),
            ("on_split", "split"),
        ];
        let mut out: FxHashMap<&'static str, String> = FxHashMap::default();
        let mut seen: FxHashSet<String> = FxHashSet::default();
        seen.insert(node.to_string());
        let mut cursor = node.to_string();
        for _ in 0..16 {
            let mut advanced = false;
            for (rel, key) in PROP_KEYS {
                if out.contains_key(*key) {
                    continue;
                }
                let dests = self.out(&cursor, rel);
                if dests.is_empty() {
                    continue;
                }
                let target = dests[0].clone();
                out.insert(*key, self.display(&target));
                if !seen.contains(&target) {
                    seen.insert(target.clone());
                    cursor = target;
                    advanced = true;
                }
                break;
            }
            if !advanced {
                break;
            }
        }
        out
    }
}

/// Build a [`KbSnapshot`] from one or more text blobs.
///
/// ``sources`` is the list of `(label, text)` pairs — typically the
/// curated `.kb` files plus the io-crawler output and any validated
/// postgres-overlay statements appended in DSL form.
pub fn build_snapshot(sources: &[(&str, &str)]) -> (KbSnapshot, Vec<ParseError>) {
    let mut all_triples = Vec::new();
    let mut all_errors = Vec::new();
    for (label, text) in sources {
        let (triples, errors) = parse_statements(text, label);
        all_triples.extend(triples);
        all_errors.extend(errors);
    }

    let idx = IndexedTriples::from_triples(&all_triples);
    let snapshot = build_snapshot_from_index(&idx);

    (snapshot, all_errors)
}

fn build_snapshot_from_index(idx: &IndexedTriples) -> KbSnapshot {
    let mut snap = KbSnapshot::default();

    // Operators: nodes with ``is_an Operator``.
    let operator_nodes: Vec<String> = idx
        .adj
        .iter()
        .filter(|(_, edges)| {
            edges
                .get("is_an")
                .map(|d| d.iter().any(|x| x == "Operator"))
                .unwrap_or(false)
        })
        .map(|(n, _)| n.clone())
        .collect();

    // Interfaces: nodes with ``is_an Interface``.
    let interface_nodes: Vec<String> = idx
        .adj
        .iter()
        .filter(|(_, edges)| {
            edges
                .get("is_an")
                .map(|d| d.iter().any(|x| x == "Interface"))
                .unwrap_or(false)
        })
        .map(|(n, _)| n.clone())
        .collect();

    // ── Operators ───────────────────────────────────────────────
    for op in &operator_nodes {
        let interface = first_with_class(idx, op, "is_a", "Interface");
        let family = idx
            .out(op, "implements")
            .first()
            .and_then(|c| idx.out(c, "is_subclass_of").first().cloned());
        let model_family = idx.out(op, "belongs_to_family").first().cloned();
        let tasks: Vec<String> = idx
            .out(op, "performs")
            .iter()
            .map(|t| idx.display(t))
            .collect();
        let parameters = collect_operator_parameters(idx, op);
        let (mut inputs, mut outputs) = port_records(idx, op);
        // Class operators don't declare ``has_input`` directly — the
        // data ports (X, y, X_test, predictions) live on the interface
        // (``Sklearn Estimator`` / ``Sklearn Transformer`` / ...). Fall
        // back to the interface's IO so the catalog renders semantic
        // handle names instead of bare position indexes. Without this
        // the frontend's ``HandleRenderer`` falls through to ``→0/1/2``
        // labels and edges become unwireable for end users.
        if let Some(iface_node) = interface.as_ref() {
            if inputs.is_empty() {
                inputs = port_records(idx, iface_node).0;
            }
            if outputs.is_empty() {
                outputs = port_records(idx, iface_node).1;
            }
        }
        let import_path = idx
            .out(op, "is_subclass_of")
            .iter()
            .find(|t| t.contains('.'))
            .cloned();
        // ``(operator)-[:might_introduce]->(risk)`` is the predicate
        // the .kb sources actually populate (see
        // ``dorian/knowledge/sources/risks.kb`` + ``llm.kb``).
        // ``checks_for`` is "(check)→(risk)" — different relation, leaves
        // ``OperatorRecord.risks`` empty and breaks the AI Debugger.
        let risks: Vec<String> = idx
            .out(op, "might_introduce")
            .iter()
            .map(|r| idx.display(r))
            .collect();
        // `metric_display_name` historically surfaced the abstract
        // metric an operator implements (e.g. "Accuracy" for
        // sklearn.metrics.accuracy_score).
        let display_name = idx.out(op, "implements").first().cloned();

        snap.operators.insert(
            op.clone(),
            OperatorRecord {
                name: op.clone(),
                interface,
                family: family.or(model_family),
                tasks,
                parameters,
                inputs,
                outputs,
                import_path,
                risks,
                display_name,
            },
        );
    }

    // ── Interfaces ─────────────────────────────────────────────
    for iface in &interface_nodes {
        let method_sequence = walk_method_sequence(idx, iface);
        let (inputs, outputs) = port_records(idx, iface);
        let attributes: Vec<String> = idx
            .out(iface, "has_attribute")
            .iter()
            .map(|a| idx.display(a))
            .collect();
        let method_io = walk_method_io(idx, iface);

        snap.interfaces.insert(
            iface.clone(),
            InterfaceRecord {
                name: iface.clone(),
                method_sequence,
                inputs,
                outputs,
                method_io,
                attributes,
            },
        );
    }

    // ── Concept hierarchy (is_subclass_of) ─────────────────────
    for (child, edges) in &idx.adj {
        if let Some(parents) = edges.get("is_subclass_of") {
            if let Some(parent) = parents.first() {
                snap.concept_parents
                    .entry(child.clone())
                    .or_insert_with(|| parent.clone());
            }
        }
    }

    // ── Libraries (has_package) ────────────────────────────────
    for (lib, edges) in &idx.adj {
        if let Some(pkgs) = edges.get("has_package") {
            if let Some(pkg) = pkgs.first() {
                snap.libraries.insert(lib.clone(), pkg.clone());
            }
        }
    }

    // ── Mitigations (is_a Mitigation) ──────────────────────────
    for (n, edges) in &idx.adj {
        if !edges
            .get("is_a")
            .map(|d| d.iter().any(|x| x == "Mitigation"))
            .unwrap_or(false)
        {
            continue;
        }
        let interface_name = idx
            .out(n, "applies_to")
            .iter()
            .find(|t| {
                idx.adj
                    .get(*t)
                    .and_then(|m| m.get("is_an"))
                    .map(|d| d.iter().any(|x| x == "Interface"))
                    .unwrap_or(false)
            })
            .cloned();
        let anchor_inputs: Vec<String> = idx
            .out(n, "has_input")
            .iter()
            .map(|p| idx.display(p))
            .collect();
        let risks: Vec<String> = idx
            .out(n, "might_mitigate")
            .iter()
            .map(|r| idx.display(r))
            .collect();
        snap.mitigations.insert(
            n.clone(),
            MitigationRecord {
                name: n.clone(),
                interface_name,
                anchor_inputs,
                risks: risks.clone(),
            },
        );
        for r in &risks {
            snap.mitigations_by_risk
                .entry(r.clone())
                .or_default()
                .push(snap.mitigations.get(n).cloned().unwrap_or_default());
        }

        // Mitigation description templates — short + long. Both are
        // optional; absent → empty string. The templates carry
        // ``{operator}`` / ``{risk}`` / ``{task}`` / ``{alternatives}``
        // placeholders the rust ``risk_chain::format_template`` fills
        // in (no python ``str.format_map`` analogue in rust).
        let short_desc = idx
            .out(n, "with_description")
            .first()
            .map(|d| idx.display(d))
            .unwrap_or_default();
        let long_desc = idx
            .out(n, "with_long_description")
            .first()
            .map(|d| idx.display(d))
            .unwrap_or_default();
        if !short_desc.is_empty() || !long_desc.is_empty() {
            snap.mitigation_descriptions
                .insert(n.clone(), (short_desc, long_desc));
        }
    }

    // ── Principles per risk (is_threat_to) ─────────────────────
    // Risks are nodes that are ``is_a Risk``. For each, collect the
    // principles they threaten via ``(risk)-[:is_threat_to]->(principle)``.
    for (n, edges) in &idx.adj {
        if !edges
            .get("is_a")
            .map(|d| d.iter().any(|x| x == "Risk"))
            .unwrap_or(false)
        {
            continue;
        }
        let principles: Vec<String> = idx
            .out(n, "is_threat_to")
            .iter()
            .map(|p| idx.display(p))
            .collect();
        if !principles.is_empty() {
            snap.principles_by_risk.insert(idx.display(n), principles);
        }
    }

    // ── Checks per risk ────────────────────────────────────────
    // ``(check)-[:checks_for]->(risk)`` — invert by walking checks
    // (``is_a Check``) and recording the inverse edge.
    for (n, edges) in &idx.adj {
        if !edges
            .get("is_a")
            .map(|d| d.iter().any(|x| x == "Check"))
            .unwrap_or(false)
        {
            continue;
        }
        let check_name = idx.display(n);
        for risk_uuid in idx.out(n, "checks_for") {
            let risk_name = idx.display(&risk_uuid);
            snap.checks_by_risk
                .entry(risk_name)
                .or_default()
                .push(check_name.clone());
        }
    }

    // ── Pathways ───────────────────────────────────────────────
    let mut metrics_by_task: HashMap<String, Vec<String>> = HashMap::new();
    let mut families_for_risk: HashMap<String, Vec<String>> = HashMap::new();
    let mut risks_surfaced_by_metric: HashMap<String, Vec<String>> = HashMap::new();
    for (n, edges) in &idx.adj {
        if !edges
            .get("is_a")
            .map(|d| d.iter().any(|x| x == "Pathway"))
            .unwrap_or(false)
        {
            continue;
        }
        let (uuid, direction) = if let Some(below) = edges.get("when_below").and_then(|d| d.first())
        {
            (below.clone(), "below".to_string())
        } else if let Some(above) = edges.get("when_above").and_then(|d| d.first()) {
            (above.clone(), "above".to_string())
        } else {
            continue;
        };
        let metric = idx.display(&uuid);
        let threshold_str = match idx.out(&uuid, "has_threshold").first() {
            Some(t) => idx.display(t),
            None => continue,
        };
        let threshold: f64 = match threshold_str.parse() {
            Ok(v) => v,
            Err(_) => continue,
        };
        let families: Vec<String> = idx
            .out(n, "and_contains_family")
            .iter()
            .map(|f| idx.display(f))
            .collect();
        let task_uuid = idx.out(n, "and_performs_task").first().cloned();
        let task = task_uuid.as_ref().map(|u| idx.display(u));
        let preprocessing = idx
            .out(n, "suggests_preprocessing")
            .first()
            .cloned()
            .map(|p| idx.display(&p));
        let replacement = idx
            .out(n, "suggests_replacement")
            .first()
            .cloned()
            .map(|p| idx.display(&p));
        let description = idx
            .out(n, "with_description")
            .first()
            .cloned()
            .map(|d| idx.display(&d));

        let risks_for_this_metric: Vec<String> = idx
            .out(&metric, "surfaces_risk")
            .iter()
            .map(|r| idx.display(r))
            .collect();
        let risk = risks_for_this_metric.first().cloned().or(Some(metric.clone()));

        snap.pathways.push(PathwayRecord {
            name: n.clone(),
            metric: metric.clone(),
            direction,
            threshold,
            families: families.clone(),
            task: task.clone(),
            preprocessing,
            replacement,
            description,
            risk: risk.clone(),
        });

        if let (Some(t), m) = (task.as_ref(), metric.clone()) {
            let bucket = metrics_by_task.entry(t.clone()).or_default();
            if !bucket.contains(&m) {
                bucket.push(m);
            }
        }
        if let Some(r) = risk.as_ref() {
            let bucket = families_for_risk.entry(r.clone()).or_default();
            for f in &families {
                if !bucket.contains(f) {
                    bucket.push(f.clone());
                }
            }
            let m_bucket = risks_surfaced_by_metric.entry(metric.clone()).or_default();
            if !m_bucket.contains(r) {
                m_bucket.push(r.clone());
            }
        }
    }
    // Augment ``metrics_by_task`` with model-evaluation metrics
    // declared via ``<sklearn-metric-fqn> evaluates <task>`` triples
    // in ``metrics.kb``. The pathway-derived entries above are
    // data-quality metrics (``LabelCompleteness`` etc.) used by the
    // debugger; the evaluation harness wants the actual sklearn
    // metric FQNs (``sklearn.metrics.accuracy_score``,
    // ``sklearn.metrics.f1_score``, …) so a Classification template
    // spawns the right multi-metric fanout. Both kinds coexist in
    // ``metrics_by_task`` because they share the "evaluates the
    // task" relationship semantically — the consumer filters by FQN
    // shape (dotted path → eval metric, bare name → DQ metric).
    for (subject, edges) in &idx.adj {
        let evaluates = match edges.get("evaluates") {
            Some(targets) => targets,
            None => continue,
        };
        for target_uuid in evaluates {
            let task = idx.display(target_uuid);
            if task.is_empty() {
                continue;
            }
            // ``subject`` is the metric operator's FQN in the
            // ``<sklearn.metrics.foo> evaluates <Task>`` shape — already
            // a display name, no UUID indirection.
            let bucket = metrics_by_task.entry(task).or_default();
            if !bucket.contains(subject) {
                bucket.push(subject.clone());
            }
        }
    }
    snap.metrics_by_task = metrics_by_task.into_iter().collect();
    snap.families_for_risk = families_for_risk.into_iter().collect();
    snap.risks_surfaced_by_metric = risks_surfaced_by_metric.into_iter().collect();

    // ── Interface methods ──────────────────────────────────────
    let mut methods: HashSet<String> = HashSet::new();
    for (_, edges) in &idx.adj {
        if let Some(dsts) = edges.get("calls") {
            for d in dsts {
                let name = idx.display(d);
                if name != "__init__" {
                    methods.insert(name);
                }
            }
        }
    }
    snap.interface_methods = methods.into_iter().collect();
    snap.interface_methods.sort();

    snap
}

fn first_with_class(
    idx: &IndexedTriples,
    node: &str,
    pred: &str,
    target_class: &str,
) -> Option<String> {
    for candidate in idx.out(node, pred) {
        if idx
            .adj
            .get(candidate)
            .and_then(|m| m.get("is_an"))
            .map(|d| d.iter().any(|x| x == target_class))
            .unwrap_or(false)
        {
            return Some(candidate.clone());
        }
    }
    None
}

fn port_records(idx: &IndexedTriples, node: &str) -> (Vec<IoSpec>, Vec<IoSpec>) {
    let ins = collect_unique_ports(
        idx.out(node, "has_input").iter().map(|p| port_to_record(idx, p)),
    );
    let outs = collect_unique_ports(
        idx.out(node, "has_output").iter().map(|p| port_to_record(idx, p)),
    );
    (ins, outs)
}

/// Deduplicate ports declared via multiple ``has_input`` / ``has_output``
/// triples for the same node. The DSL allows side-by-side curated
/// declarations: a port can appear once in ``interfaces.kb`` (full
/// signature, ``has position N``) and again in ``annotations.kb``
/// (semantic metadata only — ``on split train`` etc., no position).
/// Each statement creates a fresh anonymous UUID and a fresh
/// ``has_input`` edge, so the same port name surfaces twice — once
/// at its real position, once at the default 0 — corrupting
/// downstream method-io lookups (e.g. ``Sklearn Estimator.fit``
/// would render with ``[X@0, y@0, X@1, y@2]`` instead of the
/// authoritative ``[X@1, y@2]``).
///
/// Merge rule: keep the richest entry per ``(name, position)`` pair,
/// preferring entries with a non-default dtype. When the same name
/// appears at multiple positions, all of them are retained — that's
/// a legitimate fan-out, not a duplicate.
fn collect_unique_ports<I: IntoIterator<Item = IoSpec>>(records: I) -> Vec<IoSpec> {
    let collected: Vec<IoSpec> = records.into_iter().collect();
    if collected.len() <= 1 {
        return collected;
    }

    // Bucket by name; collapse the per-name list to one entry per
    // distinct position, preferring the richest dtype per position.
    let mut by_name: BTreeMap<String, Vec<IoSpec>> = BTreeMap::new();
    let mut order: Vec<String> = Vec::new();
    for rec in collected {
        if !by_name.contains_key(&rec.name) {
            order.push(rec.name.clone());
        }
        by_name.entry(rec.name.clone()).or_default().push(rec);
    }

    // For each name: collapse same-position entries; if the name
    // appears at exactly one position OR at multiple positions where
    // one is the default 0 and another is non-zero, keep only the
    // non-default-position entry (it's the same logical port with
    // metadata-only siblings whose position fell back to 0).
    let mut out: Vec<IoSpec> = Vec::new();
    for name in order {
        let mut entries = by_name.remove(&name).unwrap_or_default();
        // Merge same-position entries (richer dtype wins). Position
        // is now a String (kwarg-style ports carry their kwarg name);
        // a stable ordering puts numeric positions first, then
        // kwarg names, mirroring the previous numeric-first sort.
        entries.sort_by(|a, b| position_sort_key(&a.position).cmp(&position_sort_key(&b.position)));
        let mut by_pos: BTreeMap<String, IoSpec> = BTreeMap::new();
        for e in entries {
            match by_pos.get(&e.position) {
                None => {
                    by_pos.insert(e.position.clone(), e);
                }
                Some(existing) if existing.dtype == "any" && e.dtype != "any" => {
                    by_pos.insert(e.position.clone(), e);
                }
                _ => {}
            }
        }
        // If there's a non-default-zero position AND a position-"0" entry,
        // the "0" one is a metadata-only declaration — drop it.
        let has_non_zero = by_pos.keys().any(|p| p != "0");
        if has_non_zero {
            by_pos.remove("0");
        }
        for (_, spec) in by_pos {
            out.push(spec);
        }
    }
    out
}

/// Stable sort key for IoSpec.position. Numeric positions come
/// first (in numeric order), followed by kwarg names alphabetically.
fn position_sort_key(p: &str) -> (u8, i64, String) {
    match p.parse::<i64>() {
        Ok(n) => (0, n, String::new()),
        Err(_) => (1, 0, p.to_string()),
    }
}

fn port_to_record(idx: &IndexedTriples, port_node: &str) -> IoSpec {
    let props = idx.chain_props(port_node);
    let dtype = props
        .get("type")
        .cloned()
        .unwrap_or_else(|| "any".to_string());
    // Keep position as a String so kwarg-style positions
    // (``has position random_state``) survive verbatim — the
    // previous i32 parse silently collapsed every kwarg port
    // onto position ``0``, which is why the SPA rendered every
    // function-style operator with numeric handles regardless
    // of the KB's curated kwarg names.
    let position = props
        .get("position")
        .cloned()
        .unwrap_or_else(|| "0".to_string());
    let name = idx.display(port_node);
    IoSpec {
        name,
        dtype,
        position,
    }
}

fn walk_method_sequence(idx: &IndexedTriples, iface: &str) -> Vec<String> {
    let mut seen_uuids: FxHashSet<String> = FxHashSet::default();
    let mut seen_names: FxHashSet<String> = FxHashSet::default();
    let mut out: Vec<String> = Vec::new();
    let mut frontier: Vec<String> = idx.out(iface, "calls").to_vec();
    while let Some(uuid) = frontier_pop(&mut frontier) {
        if !seen_uuids.insert(uuid.clone()) {
            continue;
        }
        let name = idx.display(&uuid);
        // Include ``__init__`` — the python compound-operator
        // expansion (``dorian/pipeline/transforms.py``) anchors
        // class-style chains on it (creates the constructor node
        // from the operator's class, routes default-method-less
        // params here). Filtering it out left every Sklearn
        // Estimator / Transformer / Supervised Transformer with a
        // chain like ``["fit", "predict"]`` — the python expander
        // then raised ``KeyError('__init__')`` on every Run.
        // ``walk_method_io`` below still skips ``__init__`` because
        // ``__init__`` has no per-method I/O declarations to
        // surface (it consumes operator parameters, not data).
        if !name.is_empty() && seen_names.insert(name.clone()) {
            out.push(name);
        }
        for child in idx.out(&uuid, "calls") {
            frontier.push(child.clone());
        }
    }
    out
}

fn walk_method_io(
    idx: &IndexedTriples,
    iface: &str,
) -> FxHashMap<String, (Vec<IoSpec>, Vec<IoSpec>)> {
    let mut out: FxHashMap<String, (Vec<IoSpec>, Vec<IoSpec>)> = FxHashMap::default();
    let mut seen: FxHashSet<String> = FxHashSet::default();
    let mut frontier: Vec<String> = idx.out(iface, "calls").to_vec();
    while let Some(uuid) = frontier_pop(&mut frontier) {
        if !seen.insert(uuid.clone()) {
            continue;
        }
        let name = idx.display(&uuid);
        if name != "__init__" {
            let (ins, outs) = port_records(idx, &uuid);
            if !ins.is_empty() || !outs.is_empty() {
                out.insert(name, (ins, outs));
            }
        }
        for child in idx.out(&uuid, "calls") {
            frontier.push(child.clone());
        }
    }
    out
}

fn frontier_pop(frontier: &mut Vec<String>) -> Option<String> {
    if frontier.is_empty() {
        None
    } else {
        Some(frontier.remove(0))
    }
}

fn collect_operator_parameters(idx: &IndexedTriples, op: &str) -> Vec<ParameterSpec> {
    /// Annotation-richness score — used to break priority ties when
    /// the same parameter is declared from two source files (e.g.
    /// ``sklearn.py`` defines ``has parameter C`` plain; ``tuning.py``
    /// adds the typed chain).
    fn richness(p: &ParameterSpec) -> usize {
        let mut n = 0;
        if p.dtype != "any" {
            n += 1;
        }
        if p.default.is_some() {
            n += 1;
        }
        if p.low.is_some() {
            n += 1;
        }
        if p.high.is_some() {
            n += 1;
        }
        if p.choices.is_some() {
            n += 1;
        }
        if p.log_scale.is_some() {
            n += 1;
        }
        n
    }

    let priority = |level: &str| match level {
        "operator" => 0,
        "method" => 1,
        "interface" => 2,
        _ => 99,
    };

    let mut seen: HashMap<String, (usize, ParameterSpec)> = HashMap::new();

    let mut add = |level: &str, param_node: &str, method_name: Option<String>| {
        let props = idx.chain_props(param_node);
        let name = idx.display(param_node);
        let dtype = props.get("type").cloned().unwrap_or_else(|| "any".to_string());
        let parse_f = |k: &str| -> Option<f64> {
            props.get(k).and_then(|v| v.parse::<f64>().ok())
        };
        let log_scale = props.get("log_scale").map(|v| {
            matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on")
        });
        let choices = props.get("choices").map(|c| {
            c.split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        });
        let entry = ParameterSpec {
            name: name.clone(),
            dtype,
            default: props.get("default").cloned(),
            low: parse_f("low"),
            high: parse_f("high"),
            choices,
            log_scale,
            method: method_name,
        };
        let prio = priority(level);
        match seen.get(&name) {
            None => {
                seen.insert(name, (prio, entry));
            }
            Some((existing_prio, _)) if prio < *existing_prio => {
                seen.insert(name, (prio, entry));
            }
            Some((existing_prio, existing))
                if prio == *existing_prio && richness(&entry) > richness(existing) =>
            {
                seen.insert(name, (prio, entry));
            }
            _ => {}
        }
    };

    // 1. Direct
    for p in idx.out(op, "has_parameter") {
        add("operator", p, None);
    }
    // 2. Interface
    let iface = first_with_class(idx, op, "is_a", "Interface");
    if let Some(iface) = iface {
        for p in idx.out(&iface, "has_parameter") {
            add("interface", p, None);
        }
        // 3. Methods reachable via calls*
        let mut seen_methods: FxHashSet<String> = FxHashSet::default();
        let mut frontier: Vec<String> = idx.out(&iface, "calls").to_vec();
        while let Some(m) = frontier_pop(&mut frontier) {
            if !seen_methods.insert(m.clone()) {
                continue;
            }
            let m_name = idx.display(&m);
            for p in idx.out(&m, "has_parameter") {
                let label = if m_name == "__init__" {
                    None
                } else {
                    Some(m_name.clone())
                };
                add("method", p, label);
            }
            for child in idx.out(&m, "calls") {
                frontier.push(child.clone());
            }
        }
    }

    seen.into_values().map(|(_, p)| p).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_statement() {
        let mut id_factory = || "uuid".to_string();
        let triples = parse_statement("Resampling is a Mitigation", &mut id_factory).unwrap();
        assert_eq!(triples.len(), 1);
        assert_eq!(triples[0].subject, "Resampling");
        assert_eq!(triples[0].predicate, "is_a");
        assert_eq!(triples[0].object, "Mitigation");
    }

    #[test]
    fn parses_chained_statement() {
        let mut counter = 0;
        let mut id_factory = || {
            counter += 1;
            format!("u{counter}")
        };
        let triples = parse_statement(
            "sklearn.svm.SVC has parameter C; is of type float; with default 1.0",
            &mut id_factory,
        )
        .unwrap();
        // svc → has_parameter → u1
        // u1 → has_name → C
        // u1 → is_of_type → u2
        // u2 → has_name → float
        // u2 → with_default → 1.0
        assert_eq!(triples.len(), 5);
        assert_eq!(triples[0].subject, "sklearn.svm.SVC");
        assert_eq!(triples[0].predicate, "has_parameter");
        assert_eq!(triples[1].predicate, "has_name");
        assert_eq!(triples[1].object, "C");
        assert_eq!(triples[2].predicate, "is_of_type");
        assert_eq!(triples[3].object, "float");
        assert_eq!(triples[4].predicate, "with_default");
        assert_eq!(triples[4].object, "1.0");
    }

    #[test]
    fn parser_collects_errors() {
        let blob = "Good is a Risk\nBroken thing without predicate\nResampling is a Mitigation";
        let (triples, errors) = parse_statements(blob, "<test>");
        assert_eq!(triples.len(), 2);
        assert_eq!(errors.len(), 1);
        assert_eq!(errors[0].line_no, 2);
        assert!(errors[0].message.contains("unknown"));
    }

    #[test]
    fn skips_comments_and_blank_lines() {
        let blob = "# this is a comment\n\n  \nResampling is a Mitigation";
        let (triples, errors) = parse_statements(blob, "<test>");
        assert_eq!(triples.len(), 1);
        assert!(errors.is_empty());
    }

    #[test]
    fn build_snapshot_minimal() {
        let blob = "
            Sklearn Estimator is an Interface
            Sklearn Estimator calls fit_uuid
            fit_uuid has name fit
            sklearn.svm.SVC is an Operator
            sklearn.svm.SVC is a Sklearn Estimator
            sklearn.svm.SVC has parameter C; is of type float; with default 1.0; with low 0.0001; with high 25.0
            Resampling is a Mitigation
            Resampling might mitigate Class Imbalance
        ";
        let (snap, errors) = build_snapshot(&[("<test>", blob)]);
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
        let svc = snap.operators.get("sklearn.svm.SVC").expect("svc record");
        assert_eq!(svc.interface.as_deref(), Some("Sklearn Estimator"));
        assert_eq!(svc.parameters.len(), 1);
        let p = &svc.parameters[0];
        assert_eq!(p.name, "C");
        assert_eq!(p.dtype, "float");
        assert_eq!(p.default.as_deref(), Some("1.0"));
        assert_eq!(p.low, Some(0.0001));
        assert_eq!(p.high, Some(25.0));

        let iface = snap.interfaces.get("Sklearn Estimator").expect("iface");
        assert_eq!(iface.method_sequence, vec!["fit"]);

        let mit = snap.mitigations.get("Resampling").expect("mitigation");
        assert_eq!(mit.risks, vec!["Class Imbalance".to_string()]);
    }

    #[test]
    fn method_sequence_includes_init() {
        // Regression: ``walk_method_sequence`` used to filter
        // ``__init__`` out of the chain, which left the python
        // compound-operator expansion raising ``KeyError('__init__')``
        // on every Run for Sklearn Estimator / Transformer / Supervised
        // Transformer / LLM Chat Completion / Guardrail / etc. The
        // python expander expects ``__init__`` as the first method
        // (it's the constructor anchor for class-style operators).
        let blob = "
            Sklearn Estimator is an Interface
            Sklearn Estimator calls init_uuid
            init_uuid has name __init__
            init_uuid calls fit_uuid
            fit_uuid has name fit
            fit_uuid calls predict_uuid
            predict_uuid has name predict
        ";
        let (snap, errors) = build_snapshot(&[("<test>", blob)]);
        assert!(errors.is_empty(), "unexpected errors: {errors:?}");
        let iface = snap.interfaces.get("Sklearn Estimator").expect("iface");
        assert_eq!(
            iface.method_sequence,
            vec!["__init__".to_string(), "fit".to_string(), "predict".to_string()]
        );
    }
}
