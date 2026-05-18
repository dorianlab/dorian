//! Risk identification — KB-driven discovery of potential and actionable risks.
//!
//! Ports the `identify_risks` and `identify_operator_risks` functions from
//! `risk_events.py`. Queries the KB for risks that an operator `might_introduce`
//! and emits `PotentialRiskIdentified` events.
//!
//! Risk statuses:
//! - **potential**: Discovered from KB structure alone (operator → risk link).
//!   These represent what COULD go wrong based on the operator's nature.
//! - **actionable**: Confirmed by a data check on the user's actual dataset.
//!   These are HIGH severity — the risk is present in the data.

use serde::{Deserialize, Serialize};
use std::collections::HashSet;

use super::mitigation::MitigationAction;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// Risk status — potential (KB-only) or actionable (data-confirmed).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RiskStatus {
    /// Discovered from KB structure alone.
    Potential,
    /// Confirmed by data check — HIGH severity.
    Actionable,
}

/// Severity level for a risk.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Low,
    Medium,
    High,
    Critical,
}

/// A potential risk identified for an operator from the KB.
///
/// Represents the KB relationship `(op)-[:might_introduce]->(risk)`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PotentialRisk {
    /// The operator that might introduce this risk.
    pub operator: String,
    /// The risk name from the KB (e.g., "Class Imbalance", "Overfitting").
    pub risk_name: String,
    /// Whether this risk is potential (KB-only) or confirmed (data check).
    pub status: RiskStatus,
    /// Severity — medium for potential, high for actionable.
    pub severity: Severity,
    /// Source of the risk identification.
    pub source: RiskSource,
    /// Check name that confirmed the risk (only for actionable risks).
    pub check_name: Option<String>,
    /// Human-readable message from the check (only for actionable risks).
    pub check_message: Option<String>,
}

/// Where the risk was identified.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RiskSource {
    /// Discovered from KB graph structure.
    Kb,
    /// Confirmed by running a data quality check.
    DataCheck,
    /// Discovered via pathway evaluation.
    Pathway,
}

/// Result of a data check execution (returned from Python runtime).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckResult {
    /// True = risk is present in the data.
    pub confirmed: bool,
    /// Human-readable summary for the frontend.
    pub message: String,
}

/// A risk with its discovered mitigations, ready to be rendered as suggestion cards.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IdentifiedRisk {
    /// The risk itself.
    pub risk: PotentialRisk,
    /// Available mitigation actions for this risk.
    pub mitigations: Vec<MitigationAction>,
    /// EU AI Act principles this risk threatens.
    pub principles: Vec<String>,
    /// Data checks that can confirm/deny this risk.
    pub available_checks: Vec<String>,
}

// ---------------------------------------------------------------------------
// Risk identifier
// ---------------------------------------------------------------------------

/// Stateless risk identifier — queries KB for operator risks.
///
/// In the Rust engine, KB queries are async and go through the KbClient.
/// This struct provides the orchestration logic; actual KB queries are
/// passed in as closures or pre-fetched data to keep the identifier
/// testable without a live KB connection.
pub struct RiskIdentifier {
    /// Checks that require train/test splits (deferred to execution time).
    checks_needing_splits: HashSet<String>,
    /// Checks that require transformed data (deferred to execution time).
    checks_needing_transform: HashSet<String>,
    /// LLM-specific checks (always "confirmed" — mock triggers guardrail suggestions).
    llm_checks: HashSet<String>,
}

impl RiskIdentifier {
    /// Create a new risk identifier with the standard check classifications.
    pub fn new() -> Self {
        Self {
            checks_needing_splits: [
                "covariate_shift",
                "selection_bias",
                "sampling_bias",
                "domain_shift_bias",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect(),

            checks_needing_transform: [
                "feature_scaling_bias",
                "outlier_bias",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect(),

            llm_checks: [
                "prompt_injection_scan",
                "toxicity_scan",
                "pii_leak_scan",
                "hallucination_check",
                "sexual_content_scan",
                "discrimination_scan",
            ]
            .iter()
            .map(|s| s.to_string())
            .collect(),
        }
    }

    /// Build potential risks for an operator from pre-fetched KB data.
    ///
    /// This is the pure logic of `identify_risks` without any I/O.
    /// The caller fetches KB data and passes it in.
    pub fn identify_potential_risks(
        &self,
        operator: &str,
        kb_risks: &[String],
    ) -> Vec<PotentialRisk> {
        kb_risks
            .iter()
            .map(|risk_name| PotentialRisk {
                operator: operator.to_string(),
                risk_name: risk_name.clone(),
                status: RiskStatus::Potential,
                severity: Severity::Medium,
                source: RiskSource::Kb,
                check_name: None,
                check_message: None,
            })
            .collect()
    }

    /// Classify a check as runnable, deferred, or LLM-specific.
    ///
    /// Returns the check classification used by `_debug_pipeline` to decide
    /// whether to invoke the check at profiling time.
    pub fn classify_check(&self, check_name: &str) -> CheckClassification {
        if self.llm_checks.contains(check_name) {
            CheckClassification::LlmAlwaysTrue
        } else if self.checks_needing_splits.contains(check_name) {
            CheckClassification::DeferredSplits
        } else if self.checks_needing_transform.contains(check_name) {
            CheckClassification::DeferredTransform
        } else {
            CheckClassification::Runnable
        }
    }

    /// Build an actionable risk from a confirmed check result.
    pub fn build_actionable_risk(
        &self,
        operator: &str,
        risk_name: &str,
        check_name: &str,
        check_message: &str,
    ) -> PotentialRisk {
        PotentialRisk {
            operator: operator.to_string(),
            risk_name: risk_name.to_string(),
            status: RiskStatus::Actionable,
            severity: Severity::High,
            source: RiskSource::DataCheck,
            check_name: Some(check_name.to_string()),
            check_message: Some(check_message.to_string()),
        }
    }

    /// Check if an operator name is a valid FQN (contains a dot).
    ///
    /// Parameters, snippets, and custom nodes are skipped in risk identification.
    pub fn is_operator_fqn(name: &str) -> bool {
        !name.is_empty() && name.contains('.')
    }

    /// Extract the short name from a fully-qualified name.
    ///
    /// `sklearn.preprocessing.StandardScaler` → `StandardScaler`
    pub fn short_name(fqn: &str) -> &str {
        fqn.rsplit('.').next().unwrap_or(fqn)
    }
}

impl Default for RiskIdentifier {
    fn default() -> Self {
        Self::new()
    }
}

/// Check classification — determines when a check can be invoked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckClassification {
    /// Can be run at profiling time with the available DataFrame.
    Runnable,
    /// Needs train/test split — deferred to execution time.
    DeferredSplits,
    /// Needs transformed data — deferred to execution time.
    DeferredTransform,
    /// LLM content check — always returns true (mock that triggers guardrail suggestions).
    LlmAlwaysTrue,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_identify_potential_risks() {
        let identifier = RiskIdentifier::new();
        let risks = identifier.identify_potential_risks(
            "sklearn.ensemble.RandomForestClassifier",
            &["Overfitting".to_string(), "Class Imbalance".to_string()],
        );

        assert_eq!(risks.len(), 2);
        assert_eq!(risks[0].operator, "sklearn.ensemble.RandomForestClassifier");
        assert_eq!(risks[0].risk_name, "Overfitting");
        assert_eq!(risks[0].status, RiskStatus::Potential);
        assert_eq!(risks[0].severity, Severity::Medium);
        assert_eq!(risks[0].source, RiskSource::Kb);
        assert!(risks[0].check_name.is_none());
    }

    #[test]
    fn test_identify_empty_risks() {
        let identifier = RiskIdentifier::new();
        let risks = identifier.identify_potential_risks("sklearn.svm.SVC", &[]);
        assert!(risks.is_empty());
    }

    #[test]
    fn test_build_actionable_risk() {
        let identifier = RiskIdentifier::new();
        let risk = identifier.build_actionable_risk(
            "sklearn.tree.DecisionTreeClassifier",
            "Class Imbalance",
            "class_imbalance",
            "Chi-squared test detected significant class imbalance in 'target'",
        );

        assert_eq!(risk.status, RiskStatus::Actionable);
        assert_eq!(risk.severity, Severity::High);
        assert_eq!(risk.source, RiskSource::DataCheck);
        assert_eq!(risk.check_name.as_deref(), Some("class_imbalance"));
    }

    #[test]
    fn test_classify_check_runnable() {
        let identifier = RiskIdentifier::new();
        assert_eq!(
            identifier.classify_check("class_imbalance"),
            CheckClassification::Runnable,
        );
        assert_eq!(
            identifier.classify_check("group_bias"),
            CheckClassification::Runnable,
        );
    }

    #[test]
    fn test_classify_check_deferred_splits() {
        let identifier = RiskIdentifier::new();
        assert_eq!(
            identifier.classify_check("covariate_shift"),
            CheckClassification::DeferredSplits,
        );
        assert_eq!(
            identifier.classify_check("selection_bias"),
            CheckClassification::DeferredSplits,
        );
    }

    #[test]
    fn test_classify_check_deferred_transform() {
        let identifier = RiskIdentifier::new();
        assert_eq!(
            identifier.classify_check("feature_scaling_bias"),
            CheckClassification::DeferredTransform,
        );
    }

    #[test]
    fn test_classify_check_llm() {
        let identifier = RiskIdentifier::new();
        assert_eq!(
            identifier.classify_check("prompt_injection_scan"),
            CheckClassification::LlmAlwaysTrue,
        );
        assert_eq!(
            identifier.classify_check("toxicity_scan"),
            CheckClassification::LlmAlwaysTrue,
        );
    }

    #[test]
    fn test_is_operator_fqn() {
        assert!(RiskIdentifier::is_operator_fqn("sklearn.ensemble.RandomForestClassifier"));
        assert!(RiskIdentifier::is_operator_fqn("pandas.read_csv"));
        assert!(!RiskIdentifier::is_operator_fqn("n_estimators")); // Parameter
        assert!(!RiskIdentifier::is_operator_fqn("my_code")); // Snippet
        assert!(!RiskIdentifier::is_operator_fqn("")); // Empty
    }

    #[test]
    fn test_short_name() {
        assert_eq!(
            RiskIdentifier::short_name("sklearn.preprocessing.StandardScaler"),
            "StandardScaler",
        );
        assert_eq!(RiskIdentifier::short_name("pandas.read_csv"), "read_csv");
        assert_eq!(RiskIdentifier::short_name("SVC"), "SVC");
    }

    #[test]
    fn test_risk_status_serialization() {
        let json = serde_json::to_string(&RiskStatus::Potential).unwrap();
        assert_eq!(json, "\"potential\"");
        let json = serde_json::to_string(&RiskStatus::Actionable).unwrap();
        assert_eq!(json, "\"actionable\"");
    }

    #[test]
    fn test_severity_serialization() {
        let json = serde_json::to_string(&Severity::High).unwrap();
        assert_eq!(json, "\"high\"");
    }

    #[test]
    fn test_potential_risk_roundtrip() {
        let risk = PotentialRisk {
            operator: "sklearn.svm.SVC".to_string(),
            risk_name: "Overfitting".to_string(),
            status: RiskStatus::Potential,
            severity: Severity::Medium,
            source: RiskSource::Kb,
            check_name: None,
            check_message: None,
        };
        let json = serde_json::to_string(&risk).unwrap();
        let decoded: PotentialRisk = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.operator, risk.operator);
        assert_eq!(decoded.risk_name, risk.risk_name);
        assert_eq!(decoded.status, risk.status);
    }

    #[test]
    fn test_check_result_serialization() {
        let result = CheckResult {
            confirmed: true,
            message: "Class imbalance detected".to_string(),
        };
        let json = serde_json::to_string(&result).unwrap();
        assert!(json.contains("\"confirmed\":true"));
    }
}
