//! Suggestion card construction — builds the full payload sent to the frontend.
//!
//! Ports the `render_suggestion` function from `risk_events.py`.
//! Suggestion cards are the primary output of the AI Debugger — they represent
//! actionable advice shown in the suggestion bar.
//!
//! Each suggestion card contains:
//! - The risk and operator it relates to
//! - A mitigation action (with descriptions)
//! - EU AI Act principles the risk threatens
//! - Available data checks for the risk
//! - Severity and status indicators
//! - Whether a rewrite rule exists (enables one-click apply)

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use super::identification::{RiskSource, RiskStatus, Severity};
use super::mitigation::MitigationAction;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// A fully-constructed suggestion card ready for the frontend.
///
/// This maps to the Redis XADD payload in the Python `render_suggestion`
/// function. Each field corresponds to a key in the Redis stream message.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuggestionCard {
    /// Unique suggestion ID.
    pub sid: String,
    /// Target operator FQN.
    pub task: String,
    /// Risk name.
    pub risk: String,
    /// Mitigation action name.
    pub action: String,
    /// Short description for the card.
    pub description_short: String,
    /// Long description for expanded view.
    pub description_long: String,
    /// Alternative operators (JSON array string for Redis).
    pub alternatives: Vec<String>,
    /// EU AI Act principles this risk threatens.
    pub principles: Vec<String>,
    /// Available data checks for this risk.
    pub checks: Vec<String>,
    /// Severity level.
    pub severity: Severity,
    /// Risk status (potential or actionable).
    pub status: RiskStatus,
    /// Source of the risk identification.
    pub source: RiskSource,
    /// Pipeline label (for recommendation-scoped suggestions).
    pub pipeline_label: String,
    /// Pipeline ID (for recommendation-scoped suggestions).
    pub pipeline_id: String,
    /// Check message (human-readable summary from data check).
    pub check_message: String,
    /// Whether a rewrite rule exists for one-click apply.
    pub has_rewrite: bool,
}

/// Context for building suggestion cards.
#[derive(Debug, Clone)]
pub struct SuggestionContext {
    /// User ID.
    pub uid: String,
    /// Session ID.
    pub session: String,
    /// Target operator FQN.
    pub operator: String,
    /// Risk name.
    pub risk: String,
    /// Risk status.
    pub status: RiskStatus,
    /// Pipeline label (for recommendation-scoped suggestions).
    pub pipeline_label: String,
    /// Pipeline ID.
    pub pipeline_id: String,
    /// Check message from data check.
    pub check_message: String,
}

// ---------------------------------------------------------------------------
// Suggestion builder
// ---------------------------------------------------------------------------

/// Parameters for building a pathway suggestion card.
pub struct PathwayCardParams<'a> {
    pub uid: &'a str,
    pub session: &'a str,
    pub target_operator: &'a str,
    pub risk: &'a str,
    pub pathway_name: &'a str,
    pub rendered_description: &'a str,
    pub check_message: &'a str,
    pub preprocessing: Option<&'a str>,
    pub replacement: Option<&'a str>,
}

/// Builds suggestion cards from identified risks and mitigations.
pub struct SuggestionBuilder;

impl SuggestionBuilder {
    /// Build suggestion cards for a risk with its mitigations.
    ///
    /// One card per mitigation action — the frontend displays them
    /// in the suggestion bar.
    ///
    /// # Arguments
    /// - `ctx`: Suggestion context (uid, session, operator, risk, etc.).
    /// - `actions`: Mitigation actions discovered from the KB.
    /// - `principles`: EU AI Act principles this risk threatens.
    /// - `checks`: Data checks available for this risk.
    pub fn build_cards(
        ctx: &SuggestionContext,
        actions: &[MitigationAction],
        principles: &[String],
        checks: &[String],
    ) -> Vec<SuggestionCard> {
        let severity = match ctx.status {
            RiskStatus::Actionable => Severity::High,
            RiskStatus::Potential => Severity::Medium,
        };

        let source = match ctx.status {
            RiskStatus::Actionable => RiskSource::DataCheck,
            RiskStatus::Potential => RiskSource::Kb,
        };

        actions
            .iter()
            .map(|action| SuggestionCard {
                sid: Uuid::new_v4().to_string(),
                task: ctx.operator.clone(),
                risk: ctx.risk.clone(),
                action: action.name.clone(),
                description_short: action.short_description.clone(),
                description_long: action.long_description.clone(),
                alternatives: action.alternatives.clone(),
                principles: principles.to_vec(),
                checks: checks.to_vec(),
                severity: severity.clone(),
                status: ctx.status.clone(),
                source: source.clone(),
                pipeline_label: ctx.pipeline_label.clone(),
                pipeline_id: ctx.pipeline_id.clone(),
                check_message: ctx.check_message.clone(),
                has_rewrite: action.has_rewrite,
            })
            .collect()
    }

    /// Build a suggestion card from a pathway match.
    ///
    /// Pathway matches produce actionable suggestions with the pathway's
    /// rendered description.
    pub fn build_pathway_card(params: &PathwayCardParams<'_>) -> SuggestionCard {
        SuggestionCard {
            sid: Uuid::new_v4().to_string(),
            task: params.target_operator.to_string(),
            risk: params.risk.to_string(),
            action: params.pathway_name.to_string(),
            description_short: params.rendered_description.to_string(),
            description_long: String::new(),
            alternatives: Vec::new(),
            principles: Vec::new(),
            checks: Vec::new(),
            severity: Severity::High,
            status: RiskStatus::Actionable,
            source: RiskSource::Pathway,
            pipeline_label: "Canvas".to_string(),
            pipeline_id: String::new(),
            check_message: params.check_message.to_string(),
            has_rewrite: false,
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_context() -> SuggestionContext {
        SuggestionContext {
            uid: "user1".to_string(),
            session: "sess1".to_string(),
            operator: "sklearn.tree.DecisionTreeClassifier".to_string(),
            risk: "Overfitting".to_string(),
            status: RiskStatus::Potential,
            pipeline_label: "Current pipeline".to_string(),
            pipeline_id: "".to_string(),
            check_message: "".to_string(),
        }
    }

    fn sample_actions() -> Vec<MitigationAction> {
        vec![
            MitigationAction {
                name: "Cross-Validation".to_string(),
                short_description: "Use cross-validation".to_string(),
                long_description: "CV helps detect overfitting".to_string(),
                alternatives: Vec::new(),
                task: None,
                has_rewrite: true,
                preprocessing: None,
                replacement: None,
            },
            MitigationAction {
                name: "Direct Alternative".to_string(),
                short_description: "Try RandomForest".to_string(),
                long_description: "RF is more robust".to_string(),
                alternatives: vec![
                    "sklearn.ensemble.RandomForestClassifier".to_string(),
                ],
                task: Some("Classification".to_string()),
                has_rewrite: false,
                preprocessing: None,
                replacement: None,
            },
        ]
    }

    #[test]
    fn test_build_cards_potential() {
        let ctx = sample_context();
        let actions = sample_actions();
        let principles = vec!["Fairness".to_string(), "Transparency".to_string()];
        let checks = vec!["class_imbalance".to_string()];

        let cards = SuggestionBuilder::build_cards(&ctx, &actions, &principles, &checks);

        assert_eq!(cards.len(), 2);

        // First card — Cross-Validation.
        assert_eq!(cards[0].action, "Cross-Validation");
        assert_eq!(cards[0].severity, Severity::Medium); // Potential → Medium.
        assert_eq!(cards[0].status, RiskStatus::Potential);
        assert_eq!(cards[0].source, RiskSource::Kb);
        assert!(cards[0].has_rewrite);
        assert_eq!(cards[0].principles.len(), 2);
        assert_eq!(cards[0].checks.len(), 1);

        // Second card — Direct Alternative.
        assert_eq!(cards[1].action, "Direct Alternative");
        assert!(!cards[1].has_rewrite);
        assert_eq!(cards[1].alternatives.len(), 1);

        // Each card should have a unique SID.
        assert_ne!(cards[0].sid, cards[1].sid);
    }

    #[test]
    fn test_build_cards_actionable() {
        let mut ctx = sample_context();
        ctx.status = RiskStatus::Actionable;
        ctx.check_message = "Class imbalance detected".to_string();

        let actions = vec![sample_actions()[0].clone()];
        let cards = SuggestionBuilder::build_cards(&ctx, &actions, &[], &[]);

        assert_eq!(cards.len(), 1);
        assert_eq!(cards[0].severity, Severity::High); // Actionable → High.
        assert_eq!(cards[0].status, RiskStatus::Actionable);
        assert_eq!(cards[0].source, RiskSource::DataCheck);
        assert_eq!(cards[0].check_message, "Class imbalance detected");
    }

    #[test]
    fn test_build_cards_empty_actions() {
        let ctx = sample_context();
        let cards = SuggestionBuilder::build_cards(&ctx, &[], &[], &[]);
        assert!(cards.is_empty());
    }

    #[test]
    fn test_build_pathway_card() {
        let card = SuggestionBuilder::build_pathway_card(&PathwayCardParams {
            uid: "user1",
            session: "sess1",
            target_operator: "sklearn.ensemble.RandomForestClassifier",
            risk: "Missing Value Sensitivity",
            pathway_name: "MissingValueSensitivity",
            rendered_description: "RandomForestClassifier is sensitive to missing values. Completeness: 85%",
            check_message: "ValueCompleteness = 0.85 (below threshold 0.9)",
            preprocessing: Some("sklearn.impute.SimpleImputer"),
            replacement: None,
        });

        assert_eq!(card.risk, "Missing Value Sensitivity");
        assert_eq!(card.severity, Severity::High);
        assert_eq!(card.status, RiskStatus::Actionable);
        assert_eq!(card.source, RiskSource::Pathway);
        assert_eq!(card.pipeline_label, "Canvas");
        assert!(!card.sid.is_empty());
    }

    #[test]
    fn test_suggestion_card_serialization() {
        let card = SuggestionCard {
            sid: "test-id".to_string(),
            task: "op".to_string(),
            risk: "risk".to_string(),
            action: "action".to_string(),
            description_short: "short".to_string(),
            description_long: "long".to_string(),
            alternatives: vec!["alt1".to_string()],
            principles: vec!["fairness".to_string()],
            checks: vec!["check1".to_string()],
            severity: Severity::High,
            status: RiskStatus::Actionable,
            source: RiskSource::DataCheck,
            pipeline_label: "test".to_string(),
            pipeline_id: "".to_string(),
            check_message: "msg".to_string(),
            has_rewrite: true,
        };

        let json = serde_json::to_string(&card).unwrap();
        let decoded: SuggestionCard = serde_json::from_str(&json).unwrap();

        assert_eq!(decoded.sid, "test-id");
        assert_eq!(decoded.severity, Severity::High);
        assert!(decoded.has_rewrite);
        assert_eq!(decoded.alternatives, vec!["alt1"]);
        assert_eq!(decoded.principles, vec!["fairness"]);
    }

    #[test]
    fn test_unique_sids() {
        let ctx = sample_context();
        let actions = sample_actions();
        let cards1 = SuggestionBuilder::build_cards(&ctx, &actions, &[], &[]);
        let cards2 = SuggestionBuilder::build_cards(&ctx, &actions, &[], &[]);

        // SIDs should be unique across invocations.
        assert_ne!(cards1[0].sid, cards2[0].sid);
    }
}
