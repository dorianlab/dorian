//! Shared types for KB query results.
//!
//! These mirror the Python return types from `dorian/knowledge/queries.py`
//! but use Rust-native structures. All types are serializable for gRPC transport.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// An operator's parameter specification from the knowledge base.
///
/// Parameters can be defined at three levels:
/// - Operator level (highest priority)
/// - Interface level
/// - Method level (lowest priority, but method-specific params are most specific)
///
/// When the same parameter name appears at multiple levels, the richer
/// definition (more annotations like low/high/choices) wins.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ParameterSpec {
    /// Parameter name (e.g., "n_estimators", "learning_rate").
    pub name: String,
    /// Python type name (e.g., "int", "float", "string", "bool", "categorical").
    pub dtype: String,
    /// Default value as string (e.g., "100", "0.1", "True").
    pub default: Option<String>,
    /// Lower bound for numeric parameters.
    pub low: Option<f64>,
    /// Upper bound for numeric parameters.
    pub high: Option<f64>,
    /// Allowed values for categorical parameters.
    pub choices: Option<Vec<String>>,
    /// Whether the parameter uses log-scale sampling.
    pub log_scale: Option<bool>,
    /// Which method this parameter belongs to (None = constructor).
    pub method: Option<String>,
}

impl ParameterSpec {
    /// Count of annotation fields that are non-None (for priority tie-breaking).
    pub fn richness(&self) -> usize {
        let mut count = 0;
        if self.default.is_some() { count += 1; }
        if self.low.is_some() { count += 1; }
        if self.high.is_some() { count += 1; }
        if self.choices.is_some() { count += 1; }
        if self.log_scale.is_some() { count += 1; }
        count
    }
}

/// I/O port specification for an interface or method.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct IoSpec {
    /// Port name (e.g., "X", "y", "sample_weight").
    pub name: String,
    /// Port type (e.g., "DataFrame", "Series", "any").
    pub dtype: String,
    /// Edge position. Either a numeric index ("0", "1", …) for
    /// positional ports or a kwarg name ("n_estimators",
    /// "random_state", …) for keyword arguments. Stored as a
    /// String so the variable kwarg/positional distinction the
    /// KB carries — ``has position 0`` vs ``has position
    /// random_state`` — survives the i32 parse that was silently
    /// collapsing every kwarg port to ``0``.
    #[serde(deserialize_with = "deserialize_position")]
    pub position: String,
}

/// Tolerant position deserialiser. Accepts either:
///   * a plain string (``"0"``, ``"random_state"``)
///   * a JSON number (``0``) — converted to its string form
/// to keep older snapshot files (where position landed as i32)
/// readable after the type widening.
fn deserialize_position<'de, D>(de: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    let v = serde_json::Value::deserialize(de)?;
    match v {
        serde_json::Value::String(s) => Ok(s),
        serde_json::Value::Number(n) => Ok(n.to_string()),
        other => Err(D::Error::custom(format!(
            "IoSpec.position must be a string or number, got {other}"
        ))),
    }
}

/// Full operator metadata from the knowledge base catalog.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OperatorInfo {
    /// Fully-qualified operator name (e.g., "sklearn.ensemble.RandomForestClassifier").
    pub name: String,
    /// Interface name (e.g., "Sklearn Transformer", "Guardrail").
    pub interface: Option<String>,
    /// Tasks this operator can perform (e.g., ["classification", "regression"]).
    pub tasks: Vec<String>,
    /// Operator family (e.g., "Ensemble", "Preprocessing").
    pub family: Option<String>,
}

/// Mitigation specification from the knowledge base.
///
/// Used by the AI Debugger to construct rewrite rules for risk mitigation.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MitigationSpec {
    /// Interface name for the mitigation operator.
    pub interface_name: String,
    /// Anchor inputs that the mitigation connects to.
    pub anchor_inputs: Vec<String>,
}

/// A data pathway specification for risk identification.
///
/// Pathways link data quality metrics to model risks and suggest
/// mitigations based on operator families.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Pathway {
    /// Pathway identifier.
    pub name: String,
    /// Metric operator FQN that triggers this pathway.
    pub metric: String,
    /// Direction the metric breach occurs ("above" or "below").
    pub direction: String,
    /// Threshold value for the metric.
    pub threshold: f64,
    /// Model families sensitive to this pathway.
    pub families: Vec<String>,
    /// Task context (e.g., "classification").
    pub task: Option<String>,
    /// Preprocessing step for the metric data.
    pub preprocessing: Option<String>,
    /// Replacement operator when mitigation is needed.
    pub replacement: Option<String>,
    /// Human-readable description.
    pub description: Option<String>,
    /// Risk name this pathway addresses.
    pub risk: Option<String>,
}

/// Method I/O specification: inputs and outputs per method.
pub type MethodIo = HashMap<String, (Vec<IoSpec>, Vec<IoSpec>)>;
