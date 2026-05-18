//! Random-sampling baseline. Useful as an A/B benchmark target —
//! a real BO engine should beat random search on every dataset.
//! Set ``DORIAN_AUTOML_OPTIMIZER=random`` to compare.

use rand::SeedableRng;
use rand::rngs::StdRng;
use rustc_hash::FxHashMap;

use crate::optimizer::{Optimizer, OperatorCandidate, SlotBinding, SlotSpec, Suggestion};
use crate::trial::Trial;

pub struct RandomOptimizer {
    rng: StdRng,
}

impl RandomOptimizer {
    pub fn new() -> Self {
        Self::with_seed(0xC0FFEE)
    }
    pub fn with_seed(seed: u64) -> Self {
        Self { rng: StdRng::seed_from_u64(seed) }
    }
}

impl Default for RandomOptimizer {
    fn default() -> Self {
        Self::new()
    }
}

impl Optimizer for RandomOptimizer {
    fn ask(&mut self, slots: &[SlotSpec], k: usize) -> Vec<Suggestion> {
        let mut out = Vec::with_capacity(k);
        for _ in 0..k {
            let mut bindings: FxHashMap<String, SlotBinding> = FxHashMap::default();
            for slot in slots {
                if slot.candidates.is_empty() {
                    continue;
                }
                let cand = pick_candidate(&slot.candidates, &mut self.rng);
                let mut params = FxHashMap::default();
                for (name, dom) in &cand.params {
                    params.insert(name.clone(), dom.sample(&mut self.rng));
                }
                bindings.insert(
                    slot.task_path.clone(),
                    SlotBinding { op_fqn: cand.op_fqn.clone(), params },
                );
            }
            out.push(Suggestion { bindings });
        }
        out
    }

    fn tell(&mut self, _trials: &[Trial]) {
        // Random doesn't learn — but real engines do. The trait
        // signature stays the same so callers can swap.
    }
}

fn pick_candidate<'a>(
    candidates: &'a [OperatorCandidate], rng: &mut StdRng,
) -> &'a OperatorCandidate {
    use rand::seq::SliceRandom;
    candidates.choose(rng).expect("non-empty candidates")
}


#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Bounds, Choice, ParamDomain, ParamValue};
    use crate::optimizer::{OperatorCandidate, SlotSpec};

    fn classifier_slot() -> SlotSpec {
        SlotSpec {
            task_path: "Modeling.Classification".into(),
            candidates: vec![
                OperatorCandidate {
                    op_fqn: "sklearn.ensemble.RandomForestClassifier".into(),
                    params: vec![
                        ("n_estimators".into(), ParamDomain::Int(Bounds {
                            low: 10, high: 200, log_scale: false,
                        })),
                        ("criterion".into(), ParamDomain::Categorical(Choice {
                            options: vec!["gini".into(), "entropy".into()],
                            default: Some(0),
                        })),
                    ],
                },
                OperatorCandidate {
                    op_fqn: "sklearn.linear_model.LogisticRegression".into(),
                    params: vec![
                        ("C".into(), ParamDomain::Float(Bounds {
                            low: 1e-3, high: 10.0, log_scale: true,
                        })),
                    ],
                },
            ],
        }
    }

    #[test]
    fn ask_returns_k_suggestions_per_slot() {
        let mut opt = RandomOptimizer::with_seed(42);
        let slots = vec![classifier_slot()];
        let suggestions = opt.ask(&slots, 5);
        assert_eq!(suggestions.len(), 5);
        for s in &suggestions {
            assert!(s.bindings.contains_key("Modeling.Classification"));
        }
    }

    #[test]
    fn ask_picks_among_candidates() {
        let mut opt = RandomOptimizer::with_seed(0);
        let slots = vec![classifier_slot()];
        let mut seen_ops = std::collections::HashSet::new();
        for _ in 0..50 {
            let s = &opt.ask(&slots, 1)[0];
            let op = &s.bindings["Modeling.Classification"].op_fqn;
            seen_ops.insert(op.clone());
        }
        // Across 50 random samples we expect both candidates.
        assert!(seen_ops.contains("sklearn.ensemble.RandomForestClassifier"));
        assert!(seen_ops.contains("sklearn.linear_model.LogisticRegression"));
    }

    #[test]
    fn log_scale_float_stays_in_range() {
        let mut opt = RandomOptimizer::with_seed(1);
        let slots = vec![classifier_slot()];
        for _ in 0..100 {
            let s = &opt.ask(&slots, 1)[0];
            for binding in s.bindings.values() {
                if binding.op_fqn == "sklearn.linear_model.LogisticRegression" {
                    let c = binding.params.get("C").unwrap();
                    if let ParamValue::Float(v) = c {
                        assert!(*v >= 1e-3 && *v <= 10.0, "C={v} out of bounds");
                    } else {
                        panic!("C should be Float");
                    }
                }
            }
        }
    }
}
