//! Mitigation discovery — KB-driven mitigation actions and description rendering.
//!
//! Ports the `identify_mitigations` function from `risk_events.py`.
//! Queries the KB for mitigations (`might_mitigate`) and direct alternatives
//! (same task, no risk link), then renders description templates.
//!
//! Description templates from the KB use `{operator}`, `{risk}`, `{task}`,
//! `{alternatives}` placeholders that are interpolated at render time.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// A mitigation action discovered from the KB.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MitigationAction {
    /// Mitigation name (e.g., "Cross-Validation", "Data Augmentation").
    pub name: String,
    /// Short description (one-liner for suggestion cards).
    pub short_description: String,
    /// Long description (detailed explanation for expanded view).
    pub long_description: String,
    /// Alternative operators (only for "Direct Alternative" mitigation).
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub alternatives: Vec<String>,
    /// Task name (only for "Direct Alternative" mitigation).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task: Option<String>,
    /// Whether a rewrite rule exists in docstore for this mitigation.
    pub has_rewrite: bool,
    /// Preprocessing step (from pathway-based mitigations).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub preprocessing: Option<String>,
    /// Replacement operator (from pathway-based mitigations).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub replacement: Option<String>,
}

/// Pre-fetched KB data for a mitigation name.
///
/// The caller queries the KB for these fields, then passes them to the
/// description renderer.
#[derive(Debug, Clone)]
pub struct MitigationKbData {
    /// Mitigation name.
    pub name: String,
    /// Short description template from KB (`with_description` relationship).
    pub short_template: String,
    /// Long description template from KB (`with_long_description` relationship).
    pub long_template: String,
}

/// Direct alternatives found via the KB `performs` pathway.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DirectAlternatives {
    /// Task that both the operator and alternatives perform.
    pub task_name: String,
    /// Alternative operator FQNs that do NOT introduce the risk.
    pub alternatives: Vec<String>,
}

// ---------------------------------------------------------------------------
// Mitigation discovery
// ---------------------------------------------------------------------------

/// Discovers and renders mitigation actions for risks.
///
/// Stateless — all KB data is passed in by the caller. This keeps the
/// discovery logic testable without a live KB connection.
pub struct MitigationDiscovery;

impl MitigationDiscovery {
    /// Build mitigation actions from pre-fetched KB data.
    ///
    /// This is the pure logic of `identify_mitigations` without any I/O.
    ///
    /// # Arguments
    /// - `operator`: The operator FQN that introduced the risk.
    /// - `risk`: The risk name.
    /// - `kb_mitigations`: Mitigation names + description templates from KB.
    /// - `direct_alts`: Direct alternatives (same task, no risk).
    /// - `rewrite_available`: Map of mitigation name → whether a rewrite rule exists.
    pub fn build_mitigation_actions(
        operator: &str,
        risk: &str,
        kb_mitigations: &[MitigationKbData],
        direct_alts: Option<&DirectAlternatives>,
        rewrite_available: &HashMap<String, bool>,
    ) -> Vec<MitigationAction> {
        let mut actions = Vec::new();

        // Build template context for placeholder interpolation.
        let op_short = short_name(operator);

        // 1. Standard mitigations from KB.
        for mit in kb_mitigations {
            if mit.name == "Direct Alternative" {
                continue; // Handled separately with alternatives list.
            }

            let short = render_template(
                &mit.short_template,
                operator,
                risk,
                "",
                "",
            );
            let long = render_template(
                &mit.long_template,
                operator,
                risk,
                "",
                "",
            );

            actions.push(MitigationAction {
                name: mit.name.clone(),
                short_description: short,
                long_description: long,
                alternatives: Vec::new(),
                task: None,
                has_rewrite: *rewrite_available.get(&mit.name).unwrap_or(&false),
                preprocessing: None,
                replacement: None,
            });
        }

        // 2. Direct alternatives — operators that perform the same task without the risk.
        if let Some(alts) = direct_alts {
            if !alts.alternatives.is_empty() {
                let alt_display: String = alts
                    .alternatives
                    .iter()
                    .take(5)
                    .map(|a| short_name(a))
                    .collect::<Vec<_>>()
                    .join(", ");

                let da_templates = kb_mitigations
                    .iter()
                    .find(|m| m.name == "Direct Alternative");

                let (short_tpl, long_tpl) = match da_templates {
                    Some(t) => (t.short_template.as_str(), t.long_template.as_str()),
                    None => ("", ""),
                };

                let short = if short_tpl.is_empty() {
                    format!(
                        "Consider using {} instead of {} to avoid {}",
                        alt_display, op_short, risk,
                    )
                } else {
                    render_template(
                        short_tpl,
                        op_short,
                        risk,
                        &alts.task_name,
                        &alt_display,
                    )
                };

                let long = if long_tpl.is_empty() {
                    format!(
                        "The operator {} might introduce {}. Alternative operators that \
                         perform the same task ({}) without this risk: {}.",
                        op_short, risk, alts.task_name, alt_display,
                    )
                } else {
                    render_template(
                        long_tpl,
                        op_short,
                        risk,
                        &alts.task_name,
                        &alt_display,
                    )
                };

                actions.push(MitigationAction {
                    name: "Direct Alternative".to_string(),
                    short_description: short,
                    long_description: long,
                    alternatives: alts.alternatives.clone(),
                    task: Some(alts.task_name.clone()),
                    has_rewrite: false, // Direct alternatives are manual replacements.
                    preprocessing: None,
                    replacement: None,
                });
            }
        }

        actions
    }
}

// ---------------------------------------------------------------------------
// Template rendering
// ---------------------------------------------------------------------------

/// Render a description template by replacing placeholders.
///
/// Supports: `{operator}`, `{risk}`, `{task}`, `{alternatives}`.
/// Unknown placeholders are left as-is (matching Python's `defaultdict(str)`
/// behavior where unknown keys return empty string).
fn render_template(
    template: &str,
    operator: &str,
    risk: &str,
    task: &str,
    alternatives: &str,
) -> String {
    if template.is_empty() {
        return String::new();
    }

    template
        .replace("{operator}", operator)
        .replace("{risk}", risk)
        .replace("{task}", task)
        .replace("{alternatives}", alternatives)
}

/// Extract the short name from a fully-qualified operator name.
///
/// `sklearn.preprocessing.StandardScaler` → `StandardScaler`
fn short_name(fqn: &str) -> &str {
    fqn.rsplit('.').next().unwrap_or(fqn)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_render_template() {
        let result = render_template(
            "Consider {operator} for {risk} in {task}",
            "StandardScaler",
            "Feature Scaling Bias",
            "Preprocessing",
            "",
        );
        assert_eq!(
            result,
            "Consider StandardScaler for Feature Scaling Bias in Preprocessing",
        );
    }

    #[test]
    fn test_render_template_empty() {
        assert_eq!(render_template("", "op", "risk", "", ""), "");
    }

    #[test]
    fn test_render_template_all_placeholders() {
        let result = render_template(
            "{operator} might cause {risk}. Try {alternatives} for {task}.",
            "DecisionTree",
            "Overfitting",
            "Classification",
            "RandomForest, XGBoost",
        );
        assert_eq!(
            result,
            "DecisionTree might cause Overfitting. Try RandomForest, XGBoost for Classification.",
        );
    }

    #[test]
    fn test_short_name() {
        assert_eq!(short_name("sklearn.svm.SVC"), "SVC");
        assert_eq!(short_name("pandas.read_csv"), "read_csv");
        assert_eq!(short_name("SVC"), "SVC");
    }

    #[test]
    fn test_build_standard_mitigations() {
        let kb_data = vec![
            MitigationKbData {
                name: "Cross-Validation".to_string(),
                short_template: "Use cross-validation for {operator}".to_string(),
                long_template: "Cross-validation helps detect {risk} in {operator}".to_string(),
            },
            MitigationKbData {
                name: "Regularization".to_string(),
                short_template: "Add regularization to {operator}".to_string(),
                long_template: "Regularization reduces {risk}".to_string(),
            },
        ];

        let mut rewrites = HashMap::new();
        rewrites.insert("Cross-Validation".to_string(), false);
        rewrites.insert("Regularization".to_string(), true);

        let actions = MitigationDiscovery::build_mitigation_actions(
            "sklearn.tree.DecisionTreeClassifier",
            "Overfitting",
            &kb_data,
            None,
            &rewrites,
        );

        assert_eq!(actions.len(), 2);
        assert_eq!(actions[0].name, "Cross-Validation");
        assert_eq!(
            actions[0].short_description,
            "Use cross-validation for sklearn.tree.DecisionTreeClassifier",
        );
        assert!(!actions[0].has_rewrite);
        assert_eq!(actions[1].name, "Regularization");
        assert!(actions[1].has_rewrite);
    }

    #[test]
    fn test_build_with_direct_alternatives() {
        let kb_data = vec![
            MitigationKbData {
                name: "Direct Alternative".to_string(),
                short_template: "Use {alternatives} instead of {operator} for {task}".to_string(),
                long_template: "{operator} might introduce {risk}. Consider: {alternatives}".to_string(),
            },
        ];

        let alts = DirectAlternatives {
            task_name: "Classification".to_string(),
            alternatives: vec![
                "sklearn.ensemble.RandomForestClassifier".to_string(),
                "sklearn.ensemble.GradientBoostingClassifier".to_string(),
            ],
        };

        let actions = MitigationDiscovery::build_mitigation_actions(
            "sklearn.tree.DecisionTreeClassifier",
            "Overfitting",
            &kb_data,
            Some(&alts),
            &HashMap::new(),
        );

        assert_eq!(actions.len(), 1);
        assert_eq!(actions[0].name, "Direct Alternative");
        assert_eq!(actions[0].alternatives.len(), 2);
        assert_eq!(actions[0].task, Some("Classification".to_string()));
        assert!(actions[0].short_description.contains("RandomForestClassifier"));
        assert!(actions[0].short_description.contains("GradientBoostingClassifier"));
    }

    #[test]
    fn test_build_with_no_mitigations() {
        let actions = MitigationDiscovery::build_mitigation_actions(
            "sklearn.svm.SVC",
            "Unknown Risk",
            &[],
            None,
            &HashMap::new(),
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_direct_alternative_skips_in_standard_list() {
        // "Direct Alternative" in kb_mitigations should be handled separately,
        // not appear as a standard mitigation.
        let kb_data = vec![
            MitigationKbData {
                name: "Direct Alternative".to_string(),
                short_template: "Use alternatives".to_string(),
                long_template: "".to_string(),
            },
            MitigationKbData {
                name: "Cross-Validation".to_string(),
                short_template: "Use CV".to_string(),
                long_template: "".to_string(),
            },
        ];

        // No direct alternatives provided → "Direct Alternative" not included.
        let actions = MitigationDiscovery::build_mitigation_actions(
            "op",
            "risk",
            &kb_data,
            None,
            &HashMap::new(),
        );

        assert_eq!(actions.len(), 1);
        assert_eq!(actions[0].name, "Cross-Validation");
    }

    #[test]
    fn test_direct_alternatives_capped_at_five() {
        let alts = DirectAlternatives {
            task_name: "Classification".to_string(),
            alternatives: (0..10)
                .map(|i| format!("sklearn.model_{}", i))
                .collect(),
        };

        let actions = MitigationDiscovery::build_mitigation_actions(
            "op",
            "risk",
            &[],
            Some(&alts),
            &HashMap::new(),
        );

        assert_eq!(actions.len(), 1);
        // Short description should only show first 5 alternatives.
        let desc = &actions[0].short_description;
        assert!(desc.contains("model_0"));
        assert!(desc.contains("model_4"));
        assert!(!desc.contains("model_5"));
    }

    #[test]
    fn test_mitigation_action_serialization() {
        let action = MitigationAction {
            name: "Cross-Validation".to_string(),
            short_description: "Use CV".to_string(),
            long_description: "Details...".to_string(),
            alternatives: Vec::new(),
            task: None,
            has_rewrite: true,
            preprocessing: None,
            replacement: None,
        };

        let json = serde_json::to_string(&action).unwrap();
        assert!(!json.contains("alternatives")); // skip_serializing_if empty
        assert!(!json.contains("task")); // skip_serializing_if None
        assert!(json.contains("\"has_rewrite\":true"));

        let decoded: MitigationAction = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.name, "Cross-Validation");
        assert!(decoded.has_rewrite);
    }
}
