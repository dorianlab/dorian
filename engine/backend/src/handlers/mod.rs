//! Event handlers — one module per logical concern (heartbeat,
//! session, pipeline, risk, ...). Each module exposes a ``register``
//! function that hooks its handlers into the registry. New ports
//! add a module here and a ``register`` call to ``build_default``
//! in ``registry.rs``.

pub mod auto_task;
pub mod cancel;
pub mod cross_product_trials;
pub mod custom_nodes;
pub mod data_science_task;
pub mod dataset_live;
pub mod datasets;
pub mod evaluation_procedure;
pub mod execute_pipeline;
pub mod experiment_store;
pub mod extraction;
pub mod feedback;
pub mod heartbeat;
pub mod interactions;
pub mod kb_changed;
pub mod listeners;
pub mod notify;
pub mod observability;
pub mod onboarding;
pub mod pathway_evaluator;
pub mod pattern_gating;
pub mod python_dispatch;
pub mod pipeline;
pub mod ranking_objective;
pub mod recommendation;
pub mod recommendations_engine;
pub mod risk_chain;
pub mod risk_scope;
pub mod session;
pub mod session_meta;
pub mod session_seed;
pub mod slack;
