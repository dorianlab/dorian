//! Mitigation generation strategies.
//!
//! A *mitigation* is a concrete actionable suggestion the AI Debugger
//! presents on the canvas. A *strategy* is a producer that, given an
//! ``(operator, risk)`` context, generates zero or more concrete
//! mitigations.
//!
//! Two strategies ship today:
//!
//!   * [`KbMitigationStrategy`]   — emits one [`MitigationAction`] per
//!     mitigation declared in the KB via ``might_mitigate``. These are
//!     the curated, named mitigations like "Robust Scaling",
//!     "Outlier Detection", "Data Augmentation".
//!
//!   * [`DirectAlternativeStrategy`] — emits one [`MitigationAction`]
//!     **per same-task operator** that doesn't introduce the risk.
//!     i.e. it expands the search space by replacing the risky
//!     operator with each viable alternative. This is a search/
//!     generation strategy, not a mitigation type — historically the
//!     python implementation collapsed the whole expansion into a
//!     single "Direct Alternative" card with an ``alternatives: [...]``
//!     list, but the user-facing semantics are clearer when each
//!     alternative is its own card.
//!
//! Adding a new strategy is one impl of [`MitigationStrategy`] +
//! one [`builtin_strategies`] entry. Strategies stay stateless and
//! receive every input as arguments so the trait stays pure-function.

use crate::kb::KbSnapshot;
use serde::{Deserialize, Serialize};

/// One concrete actionable mitigation rendered on the canvas. A
/// strategy may emit any number of these (zero, one, or many).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MitigationAction {
    /// User-facing label rendered as the suggestion card heading.
    pub name: String,
    /// Short description (one-liner under the heading).
    pub short: String,
    /// Long description (full text in the expanded card).
    pub long: String,
    /// Which strategy generated this action — frontend can group /
    /// badge by this. ``"kb"`` for the KB-declared catalog,
    /// ``"direct_alternative"`` for same-task replacements, etc.
    pub source: String,
    /// Optional ``target_operator`` field for replacement-style
    /// strategies (Direct Alternative). ``None`` for catalog
    /// mitigations.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_operator: Option<String>,
    /// Optional task hint — useful for replacement-style cards
    /// ("Replace within task: Classification").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task: Option<String>,
}

/// A pluggable mitigation generator. Stateless; the trait object is
/// constructed once at startup and reused.
pub trait MitigationStrategy: Send + Sync {
    /// Strategy id, recorded as ``MitigationAction.source`` and used
    /// for telemetry / frontend grouping.
    fn id(&self) -> &'static str;

    /// Display name (used in logs / UI badges).
    fn display_name(&self) -> &'static str;

    /// Generate zero or more mitigation actions for the
    /// ``(operator, risk)`` pair. The KB snapshot is provided so the
    /// strategy can do its own queries.
    fn generate(
        &self,
        operator: &str,
        risk: &str,
        kb: &KbSnapshot,
    ) -> Vec<MitigationAction>;
}

// ---------------------------------------------------------------------------
// Builtin strategies
// ---------------------------------------------------------------------------

/// Curated KB mitigations — emits one [`MitigationAction`] per row in
/// ``mitigations_for_risk`` (excluding ``Direct Alternative``, which is
/// itself a strategy and handled separately).
pub struct KbMitigationStrategy;

impl MitigationStrategy for KbMitigationStrategy {
    fn id(&self) -> &'static str {
        "kb"
    }
    fn display_name(&self) -> &'static str {
        "KB-declared mitigation"
    }

    fn generate(
        &self,
        operator: &str,
        risk: &str,
        kb: &KbSnapshot,
    ) -> Vec<MitigationAction> {
        let mut out = Vec::new();
        for spec in kb.mitigations_for_risk(risk) {
            if spec.name == "Direct Alternative" {
                // Marker, not a mitigation — handled by
                // ``DirectAlternativeStrategy``.
                continue;
            }
            let (short_t, long_t) = kb
                .mitigation_description(&spec.name)
                .unwrap_or_default();
            let short = render_template(&short_t, operator, risk, "", "");
            let long = render_template(&long_t, operator, risk, "", "");
            out.push(MitigationAction {
                name: spec.name,
                short,
                long,
                source: self.id().to_string(),
                target_operator: None,
                task: None,
            });
        }
        out
    }
}

/// Same-task-replacement strategy. For every operator in the KB that
/// performs the same task as ``operator`` and does NOT also introduce
/// ``risk``, emit one [`MitigationAction`] proposing the replacement.
///
/// The ``Direct Alternative`` description templates from the KB
/// ("Replace {operator} with one of {alternatives}") are rendered
/// per-instance with the specific alternative — the
/// ``{alternatives}`` placeholder collapses to the single
/// alternative's short name.
pub struct DirectAlternativeStrategy;

impl MitigationStrategy for DirectAlternativeStrategy {
    fn id(&self) -> &'static str {
        "direct_alternative"
    }
    fn display_name(&self) -> &'static str {
        "Direct Alternative"
    }

    fn generate(
        &self,
        operator: &str,
        risk: &str,
        kb: &KbSnapshot,
    ) -> Vec<MitigationAction> {
        let (task, alternatives) = kb.direct_alternatives(operator, risk);
        if alternatives.is_empty() {
            return Vec::new();
        }
        let (short_t, long_t) = kb
            .mitigation_description("Direct Alternative")
            .unwrap_or_default();
        let op_short = short_name(operator);
        alternatives
            .into_iter()
            .map(|alt| {
                let alt_short = short_name(&alt).to_string();
                let short =
                    render_template(&short_t, op_short, risk, &task, &alt_short);
                let long =
                    render_template(&long_t, op_short, risk, &task, &alt_short);
                MitigationAction {
                    name: format!("Replace with {alt_short}"),
                    short,
                    long,
                    source: self.id().to_string(),
                    target_operator: Some(alt),
                    task: if task.is_empty() {
                        None
                    } else {
                        Some(task.clone())
                    },
                }
            })
            .collect()
    }
}

/// Default strategy registry — the rust AI Debugger handler runs every
/// strategy returned here in order. Add a new strategy by appending an
/// entry; no other code needs to change.
pub fn builtin_strategies() -> Vec<Box<dyn MitigationStrategy>> {
    vec![
        Box::new(KbMitigationStrategy),
        Box::new(DirectAlternativeStrategy),
    ]
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Last segment of a dotted FQN.
fn short_name(fqn: &str) -> &str {
    fqn.rsplit_once('.').map(|(_, t)| t).unwrap_or(fqn)
}

/// Replace the four placeholders the KB description templates use.
/// Mirrors python's ``defaultdict(str)`` semantics: any placeholder
/// not in this set is simply left untouched in the output (no error,
/// no panic).
fn render_template(
    tmpl: &str,
    operator: &str,
    risk: &str,
    task: &str,
    alternatives: &str,
) -> String {
    if tmpl.is_empty() {
        return String::new();
    }
    tmpl.replace("{operator}", operator)
        .replace("{risk}", risk)
        .replace("{task}", task)
        .replace("{alternatives}", alternatives)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_template_substitutes_known_placeholders() {
        let out = render_template(
            "Replace {operator} with {alternatives} for {task}",
            "StandardScaler",
            "Outlier Bias",
            "Data Normalization",
            "RobustScaler",
        );
        assert_eq!(
            out,
            "Replace StandardScaler with RobustScaler for Data Normalization"
        );
    }

    #[test]
    fn render_template_leaves_unknown_placeholder_alone() {
        let out = render_template("Use {what}", "", "", "", "");
        assert_eq!(out, "Use {what}");
    }

    #[test]
    fn short_name_handles_trailing_segment() {
        assert_eq!(short_name("sklearn.preprocessing.StandardScaler"), "StandardScaler");
        assert_eq!(short_name("nodot"), "nodot");
    }
}
