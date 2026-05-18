//! Pathway evaluation — cross-view DQ metric → model risk connections.
//!
//! Ports the `evaluate_pathways` function from `risk_events.py`.
//! Pathways link data quality metrics to model risks and suggest
//! mitigations based on operator families.
//!
//! A pathway fires when:
//! 1. A DQ metric value breaches a threshold (direction + threshold).
//! 2. The pipeline contains operators belonging to target families.
//! 3. The pipeline task matches the pathway's task filter (if specified).
//!
//! Example pathway:
//! - Metric: ValueCompleteness < 0.90
//! - Families: ["Ensemble", "Neural Network"]
//! - Risk: "Missing Value Sensitivity"
//! - Description: "{operator} ({family}) is sensitive to missing values.
//!   ValueCompleteness is {metric_value_pct}% (below 90% threshold)."

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// A pathway rule from the knowledge base.
///
/// Pathways are the bridge between data quality metrics and model-level
/// risks. They encode domain knowledge like "if completeness is low,
/// ensemble models are at risk of missing value sensitivity."
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PathwayRule {
    /// Pathway identifier / name.
    pub name: String,
    /// DQ metric name that triggers this pathway.
    pub metric: String,
    /// Direction of the threshold breach ("above" or "below").
    pub direction: String,
    /// Threshold value for the metric.
    pub threshold: f64,
    /// Model families sensitive to this pathway (empty = all families).
    pub families: Vec<String>,
    /// Task context filter (None = any task).
    pub task: Option<String>,
    /// Description template with placeholders.
    pub description: String,
    /// Risk name this pathway addresses.
    pub risk: String,
    /// Preprocessing step suggestion (optional).
    pub preprocessing: Option<String>,
    /// Replacement operator suggestion (optional).
    pub replacement: Option<String>,
}

/// A matched pathway with context about which operator triggered it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PathwayMatch {
    /// The pathway rule that matched.
    pub pathway: PathwayRule,
    /// Operator on the canvas that belongs to a target family.
    pub target_operator: String,
    /// The operator's family (e.g., "Ensemble").
    pub family: String,
    /// Current metric value that triggered the breach.
    pub metric_value: f64,
    /// Rendered description with placeholders filled in.
    pub rendered_description: String,
    /// Check message summarizing the breach.
    pub check_message: String,
}

// ---------------------------------------------------------------------------
// Pathway evaluator
// ---------------------------------------------------------------------------

/// Evaluates pathway rules against current DQ metrics and canvas operators.
pub struct PathwayEvaluator;

impl PathwayEvaluator {
    /// Evaluate all pathway rules against current state.
    ///
    /// Pure logic — all data is passed in by the caller.
    ///
    /// # Arguments
    /// - `pathways`: All pathway rules from the KB.
    /// - `metric_values`: Current DQ metric values (metric name → value).
    /// - `operator_families`: Canvas operators and their families (op → family).
    /// - `tasks_on_canvas`: Tasks that operators on the canvas perform.
    pub fn evaluate(
        pathways: &[PathwayRule],
        metric_values: &HashMap<String, f64>,
        operator_families: &HashMap<String, Option<String>>,
        tasks_on_canvas: &HashSet<String>,
    ) -> Vec<PathwayMatch> {
        let families_on_canvas: HashSet<&str> = operator_families
            .values()
            .filter_map(|f| f.as_deref())
            .collect();

        let mut matches = Vec::new();

        for pathway in pathways {
            // 1. Check metric condition.
            let current_value = match metric_values.get(&pathway.metric) {
                Some(v) => *v,
                None => continue,
            };

            let breach = match pathway.direction.as_str() {
                "below" => current_value < pathway.threshold,
                "above" => current_value > pathway.threshold,
                _ => false,
            };
            if !breach {
                continue;
            }

            // 2. Check family filter.
            if !pathway.families.is_empty() {
                let pathway_families: HashSet<&str> =
                    pathway.families.iter().map(|f| f.as_str()).collect();
                if families_on_canvas.is_disjoint(&pathway_families) {
                    continue;
                }
            }

            // 3. Check task filter.
            if let Some(ref task) = pathway.task {
                if !tasks_on_canvas.contains(task.as_str()) {
                    continue;
                }
            }

            // 4. Find target operators on canvas that belong to target families.
            let target_ops: Vec<(&str, &str)> = if pathway.families.is_empty() {
                // No family filter → pick first operator.
                operator_families
                    .iter()
                    .take(1)
                    .map(|(op, fam)| (op.as_str(), fam.as_deref().unwrap_or("")))
                    .collect()
            } else {
                operator_families
                    .iter()
                    .filter(|(_, fam)| {
                        fam.as_ref()
                            .map(|f| pathway.families.contains(f))
                            .unwrap_or(false)
                    })
                    .map(|(op, fam)| (op.as_str(), fam.as_deref().unwrap_or("")))
                    .collect()
            };

            // If no target ops matched, use "dataset" as the fallback target.
            let effective_targets: Vec<(&str, &str)> = if target_ops.is_empty() {
                vec![("dataset", "")]
            } else {
                target_ops
            };

            for (target_op, family) in effective_targets {
                let short_name = target_op.rsplit('.').next().unwrap_or(target_op);
                let rendered = pathway
                    .description
                    .replace("{operator}", short_name)
                    .replace("{family}", family)
                    .replace(
                        "{metric_value}",
                        &format!("{:.2}", current_value),
                    )
                    .replace(
                        "{metric_value_pct}",
                        &format!("{:.0}", current_value * 100.0),
                    );

                let check_message = format!(
                    "{} = {:.2} ({} threshold {})",
                    pathway.metric,
                    current_value,
                    if pathway.direction == "below" {
                        "below"
                    } else {
                        "above"
                    },
                    pathway.threshold,
                );

                matches.push(PathwayMatch {
                    pathway: pathway.clone(),
                    target_operator: target_op.to_string(),
                    family: family.to_string(),
                    metric_value: current_value,
                    rendered_description: rendered,
                    check_message,
                });
            }
        }

        matches
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_pathway() -> PathwayRule {
        PathwayRule {
            name: "MissingValueSensitivity".to_string(),
            metric: "ValueCompleteness".to_string(),
            direction: "below".to_string(),
            threshold: 0.90,
            families: vec!["Ensemble".to_string()],
            task: None,
            description: "{operator} ({family}) is sensitive to missing values. Completeness: {metric_value_pct}%".to_string(),
            risk: "Missing Value Sensitivity".to_string(),
            preprocessing: Some("sklearn.impute.SimpleImputer".to_string()),
            replacement: None,
        }
    }

    fn sample_above_pathway() -> PathwayRule {
        PathwayRule {
            name: "HighCorrelation".to_string(),
            metric: "FeatureCorrelation".to_string(),
            direction: "above".to_string(),
            threshold: 0.95,
            families: vec![],
            task: None,
            description: "High correlation detected: {metric_value}".to_string(),
            risk: "Multicollinearity".to_string(),
            preprocessing: None,
            replacement: None,
        }
    }

    #[test]
    fn test_pathway_fires_below_threshold() {
        let pathways = vec![sample_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.85);

        let mut op_families = HashMap::new();
        op_families.insert(
            "sklearn.ensemble.RandomForestClassifier".to_string(),
            Some("Ensemble".to_string()),
        );

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].target_operator, "sklearn.ensemble.RandomForestClassifier");
        assert_eq!(matches[0].family, "Ensemble");
        assert_eq!(matches[0].metric_value, 0.85);
        assert!(matches[0].rendered_description.contains("RandomForestClassifier"));
        assert!(matches[0].rendered_description.contains("85%"));
    }

    #[test]
    fn test_pathway_does_not_fire_above_threshold() {
        let pathways = vec![sample_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.95); // Above threshold.

        let mut op_families = HashMap::new();
        op_families.insert("op".to_string(), Some("Ensemble".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert!(matches.is_empty());
    }

    #[test]
    fn test_pathway_above_direction() {
        let pathways = vec![sample_above_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("FeatureCorrelation".to_string(), 0.98);

        let mut op_families = HashMap::new();
        op_families.insert("sklearn.svm.SVC".to_string(), None);

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0].pathway.risk, "Multicollinearity");
    }

    #[test]
    fn test_pathway_family_filter() {
        let pathways = vec![sample_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.85);

        // SVC has family "SVM" — not in the pathway's "Ensemble" filter.
        let mut op_families = HashMap::new();
        op_families.insert("sklearn.svm.SVC".to_string(), Some("SVM".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert!(matches.is_empty());
    }

    #[test]
    fn test_pathway_task_filter() {
        let mut pathway = sample_pathway();
        pathway.task = Some("Classification".to_string());

        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.85);

        let mut op_families = HashMap::new();
        op_families.insert("op".to_string(), Some("Ensemble".to_string()));

        // No classification task on canvas → should not fire.
        let matches = PathwayEvaluator::evaluate(
            &[pathway.clone()],
            &metrics,
            &op_families,
            &HashSet::new(),
        );
        assert!(matches.is_empty());

        // With classification task → should fire.
        let mut tasks = HashSet::new();
        tasks.insert("Classification".to_string());

        let matches = PathwayEvaluator::evaluate(
            &[pathway],
            &metrics,
            &op_families,
            &tasks,
        );
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn test_pathway_missing_metric() {
        let pathways = vec![sample_pathway()];
        let metrics = HashMap::new(); // No metrics at all.
        let op_families = HashMap::new();

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert!(matches.is_empty());
    }

    #[test]
    fn test_pathway_fallback_dataset_target() {
        let mut pathway = sample_pathway();
        pathway.families = vec!["Neural Network".to_string()]; // No NN on canvas.

        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.85);

        // Canvas has Ensemble operators, but pathway wants Neural Network.
        let mut op_families = HashMap::new();
        op_families.insert("op".to_string(), Some("Ensemble".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &[pathway],
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        // No match because the family filter doesn't match.
        assert!(matches.is_empty());
    }

    #[test]
    fn test_pathway_no_family_filter() {
        let mut pathway = sample_above_pathway();
        pathway.families = vec![]; // Empty = no filter.

        let mut metrics = HashMap::new();
        metrics.insert("FeatureCorrelation".to_string(), 0.98);

        let mut op_families = HashMap::new();
        op_families.insert("op1".to_string(), Some("SVM".to_string()));
        op_families.insert("op2".to_string(), Some("Ensemble".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &[pathway],
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        // Should match exactly one operator (first one, no family filter).
        assert_eq!(matches.len(), 1);
    }

    #[test]
    fn test_pathway_check_message_format() {
        let pathways = vec![sample_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.82);

        let mut op_families = HashMap::new();
        op_families.insert("op".to_string(), Some("Ensemble".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        assert_eq!(matches.len(), 1);
        assert_eq!(
            matches[0].check_message,
            "ValueCompleteness = 0.82 (below threshold 0.9)",
        );
    }

    #[test]
    fn test_pathway_rule_serialization() {
        let rule = sample_pathway();
        let json = serde_json::to_string(&rule).unwrap();
        let decoded: PathwayRule = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.name, rule.name);
        assert_eq!(decoded.threshold, rule.threshold);
    }

    #[test]
    fn test_multiple_operators_match() {
        let pathways = vec![sample_pathway()];
        let mut metrics = HashMap::new();
        metrics.insert("ValueCompleteness".to_string(), 0.85);

        let mut op_families = HashMap::new();
        op_families.insert("sklearn.ensemble.RandomForestClassifier".to_string(), Some("Ensemble".to_string()));
        op_families.insert("sklearn.ensemble.GradientBoostingClassifier".to_string(), Some("Ensemble".to_string()));

        let matches = PathwayEvaluator::evaluate(
            &pathways,
            &metrics,
            &op_families,
            &HashSet::new(),
        );

        // Both operators match the "Ensemble" family filter.
        assert_eq!(matches.len(), 2);
    }
}
