//! AutoML BO core. Modular `Optimizer` trait so callers can swap
//! surrogates (SMAC3 RF, TPE, BORE) without changing the trial
//! loop. Default: [`SmacOptimizer`] — a SMAC3-style random forest
//! surrogate with expected-improvement acquisition. Picked at
//! runtime via `DORIAN_AUTOML_OPTIMIZER=smac|tpe|bore|random`.
//!
//! KB-as-ConfigSpace
//! -----------------
//! Per the design directive (memory + spec discussion): there is
//! no separate `ConfigSpace` data type. Templates contain
//! `LogicalTask` placeholders that name a canonical hierarchical
//! task. At sample-time the optimizer queries the KB
//! (``kb_operators_for_task(canonical_path)``) to enumerate
//! candidate concrete operators, and ``kb_operator_parameters`` to
//! enumerate their hyperparameter domains. The configuration
//! space is therefore **derived** from the template + the live
//! KB, not pre-materialised.
//!
//! Trial integration
//! -----------------
//! The optimizer's `tell` method consumes a `Trial` (the unified
//! shape from `dorian.experiment.trial`). The same struct is also
//! the surrogate's training row: `(config, dataset_features) →
//! score`. RL, AutoML, cross-product all write Trials with their
//! own `source` tag; the surrogate trains across all of them.
//!
//! Cross-dataset surrogate
//! -----------------------
//! The surrogate's input vector is the concatenation of the
//! config-feature vector and the dataset's profile vector. A
//! never-seen dataset gets meaningful `predict()` output via
//! interpolation through profile-space neighbours that have been
//! evaluated. This is the FSBO/MetaBO pattern (Wistuba & Grabocka
//! 2021); the existing 51-metafeature profile dict already
//! provides the right per-dataset features.

pub mod config;
pub mod driver;
pub mod encoder;
pub mod materialise;
pub mod optimizer;
pub mod random;
pub mod smac;
pub mod surrogate;
pub mod trial;

pub use config::{Bounds, Choice, ParamDomain, ParamValue};
pub use driver::{Driver, DriverConfig, TemplateTarget, run_automl_driver};
pub use encoder::ConfigEncoder;
pub use materialise::materialise;
pub use optimizer::{Optimizer, OptimizerKind, build_optimizer};
pub use random::RandomOptimizer;
pub use smac::SmacOptimizer;
pub use surrogate::{ForestConfig, RandomForest};
pub use trial::{ConfigVec, Trial};
