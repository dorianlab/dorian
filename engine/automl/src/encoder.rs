//! Config → numeric vector encoder.
//!
//! The surrogate works on dense `f64` vectors. Configs arrive as
//! per-slot operator choices + per-operator hyperparameter
//! bindings. The encoder canonicalises this into a fixed-shape
//! vector using:
//!
//!   * One-hot for the operator-choice dimension per slot.
//!   * Min-max normalised value for `Float` / `Int` params (log
//!     scale honoured for log-spaced floats).
//!   * One-hot for `Categorical` / `Bool` params.
//!   * Constants don't enter the vector — they're fixed by
//!     definition.
//!
//! The encoder is stateful: it records column positions per
//! (slot, operator, param) tuple and reuses them across `tell` /
//! `ask` calls so the surrogate sees a consistent feature set as
//! more configs arrive. New (slot, operator) pairs add new
//! columns; old surrogate weights for unchanged columns stay
//! valid (the surrogate just treats new columns as zero-feature
//! observations until refitted).

use rustc_hash::FxHashMap;

use crate::config::{Bounds, ParamDomain, ParamValue};
use crate::optimizer::{SlotBinding, SlotSpec, Suggestion};

/// Mapping from (slot_path, op_fqn) to that operator's column
/// span in the encoded vector. Stored as a flat list of
/// per-feature descriptors so we can encode a Suggestion in O(N)
/// without dictionary lookups per param.
pub struct ConfigEncoder {
    features: Vec<Feature>,
    /// Column index of each feature, by (slot, op, param) name.
    /// Used during fit() / encode() to find where each binding
    /// lands.
    index: FxHashMap<FeatureKey, usize>,
}

#[derive(Debug, Clone)]
enum Feature {
    /// One-hot indicator for "slot uses this operator". Active
    /// when the binding's op_fqn matches the descriptor.
    OperatorChoice { slot: String, op_fqn: String },
    /// Continuous numeric feature — min-max normalized to
    /// [0, 1]. Log scale honoured.
    Float {
        slot: String, op_fqn: String, param: String,
        bounds: Bounds<f64>,
    },
    /// Discrete numeric feature — min-max normalized.
    Int {
        slot: String, op_fqn: String, param: String,
        bounds: Bounds<i64>,
    },
    /// One-hot indicator for a categorical option.
    Categorical {
        slot: String, op_fqn: String, param: String, option: String,
    },
    /// Bool — encoded as 0.0 / 1.0 in a single column.
    Bool { slot: String, op_fqn: String, param: String },
}

#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct FeatureKey {
    slot: String,
    op_fqn: String,
    param: String,
    /// For Categorical features the option string is part of the
    /// key (each option gets its own column). For other dtypes
    /// it's empty.
    option: String,
}

impl ConfigEncoder {
    /// Build the feature schema from the slot definitions. Each
    /// slot's candidate operators contribute their param domains;
    /// the overall column count is `sum over slots of (#operators +
    /// per-operator-param-columns)`.
    pub fn from_slots(slots: &[SlotSpec]) -> Self {
        let mut features: Vec<Feature> = Vec::new();
        let mut index: FxHashMap<FeatureKey, usize> = FxHashMap::default();
        for slot in slots {
            for cand in &slot.candidates {
                // Operator-choice indicator (one column per
                // candidate operator).
                features.push(Feature::OperatorChoice {
                    slot: slot.task_path.clone(),
                    op_fqn: cand.op_fqn.clone(),
                });
                index.insert(
                    FeatureKey {
                        slot: slot.task_path.clone(),
                        op_fqn: cand.op_fqn.clone(),
                        param: String::new(),
                        option: String::new(),
                    },
                    features.len() - 1,
                );
                for (param_name, dom) in &cand.params {
                    match dom {
                        ParamDomain::Float(b) => {
                            features.push(Feature::Float {
                                slot: slot.task_path.clone(),
                                op_fqn: cand.op_fqn.clone(),
                                param: param_name.clone(),
                                bounds: b.clone(),
                            });
                            index.insert(
                                FeatureKey {
                                    slot: slot.task_path.clone(),
                                    op_fqn: cand.op_fqn.clone(),
                                    param: param_name.clone(),
                                    option: String::new(),
                                },
                                features.len() - 1,
                            );
                        }
                        ParamDomain::Int(b) => {
                            features.push(Feature::Int {
                                slot: slot.task_path.clone(),
                                op_fqn: cand.op_fqn.clone(),
                                param: param_name.clone(),
                                bounds: b.clone(),
                            });
                            index.insert(
                                FeatureKey {
                                    slot: slot.task_path.clone(),
                                    op_fqn: cand.op_fqn.clone(),
                                    param: param_name.clone(),
                                    option: String::new(),
                                },
                                features.len() - 1,
                            );
                        }
                        ParamDomain::Categorical(c) => {
                            for opt in &c.options {
                                features.push(Feature::Categorical {
                                    slot: slot.task_path.clone(),
                                    op_fqn: cand.op_fqn.clone(),
                                    param: param_name.clone(),
                                    option: opt.clone(),
                                });
                                index.insert(
                                    FeatureKey {
                                        slot: slot.task_path.clone(),
                                        op_fqn: cand.op_fqn.clone(),
                                        param: param_name.clone(),
                                        option: opt.clone(),
                                    },
                                    features.len() - 1,
                                );
                            }
                        }
                        ParamDomain::Bool => {
                            features.push(Feature::Bool {
                                slot: slot.task_path.clone(),
                                op_fqn: cand.op_fqn.clone(),
                                param: param_name.clone(),
                            });
                            index.insert(
                                FeatureKey {
                                    slot: slot.task_path.clone(),
                                    op_fqn: cand.op_fqn.clone(),
                                    param: param_name.clone(),
                                    option: String::new(),
                                },
                                features.len() - 1,
                            );
                        }
                        ParamDomain::Constant(_) => {
                            // Constants don't enter the vector.
                        }
                    }
                }
            }
        }
        ConfigEncoder { features, index }
    }

    pub fn dim(&self) -> usize {
        self.features.len()
    }

    /// Encode one Suggestion into a dense `f64` vector. Columns
    /// for inactive operators (i.e. the slot's binding picked a
    /// different op) are zeroed.
    pub fn encode(&self, suggestion: &Suggestion) -> Vec<f64> {
        let mut out = vec![0.0; self.features.len()];
        for (slot_path, binding) in &suggestion.bindings {
            // Operator-choice indicator.
            let op_key = FeatureKey {
                slot: slot_path.clone(),
                op_fqn: binding.op_fqn.clone(),
                param: String::new(),
                option: String::new(),
            };
            if let Some(&col) = self.index.get(&op_key) {
                out[col] = 1.0;
            }
            // Each param's column.
            for (param, value) in &binding.params {
                self.encode_param(slot_path, &binding.op_fqn, param, value, &mut out);
            }
        }
        out
    }

    fn encode_param(
        &self,
        slot: &str, op_fqn: &str, param: &str, value: &ParamValue,
        out: &mut [f64],
    ) {
        match value {
            ParamValue::Float(v) => {
                let key = FeatureKey {
                    slot: slot.into(), op_fqn: op_fqn.into(),
                    param: param.into(), option: String::new(),
                };
                if let Some(&col) = self.index.get(&key) {
                    if let Feature::Float { bounds, .. } = &self.features[col] {
                        out[col] = normalise_float(*v, bounds);
                    }
                }
            }
            ParamValue::Int(v) => {
                let key = FeatureKey {
                    slot: slot.into(), op_fqn: op_fqn.into(),
                    param: param.into(), option: String::new(),
                };
                if let Some(&col) = self.index.get(&key) {
                    if let Feature::Int { bounds, .. } = &self.features[col] {
                        out[col] = normalise_int(*v, bounds);
                    }
                }
            }
            ParamValue::Bool(b) => {
                let key = FeatureKey {
                    slot: slot.into(), op_fqn: op_fqn.into(),
                    param: param.into(), option: String::new(),
                };
                if let Some(&col) = self.index.get(&key) {
                    out[col] = if *b { 1.0 } else { 0.0 };
                }
            }
            ParamValue::Str(s) => {
                let key = FeatureKey {
                    slot: slot.into(), op_fqn: op_fqn.into(),
                    param: param.into(), option: s.clone(),
                };
                if let Some(&col) = self.index.get(&key) {
                    out[col] = 1.0;
                }
            }
        }
    }
}

fn normalise_float(v: f64, b: &Bounds<f64>) -> f64 {
    if b.log_scale && b.low > 0.0 && b.high > b.low && v > 0.0 {
        ((v.ln() - b.low.ln()) / (b.high.ln() - b.low.ln())).clamp(0.0, 1.0)
    } else if b.high > b.low {
        ((v - b.low) / (b.high - b.low)).clamp(0.0, 1.0)
    } else {
        0.5
    }
}

fn normalise_int(v: i64, b: &Bounds<i64>) -> f64 {
    if b.high > b.low {
        ((v - b.low) as f64 / (b.high - b.low) as f64).clamp(0.0, 1.0)
    } else {
        0.5
    }
}
