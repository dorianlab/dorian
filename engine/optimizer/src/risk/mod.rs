//! AI Debugger — KB-driven risk identification, mitigation discovery, and
//! suggestion construction.
//!
//! Ports the orchestration logic from `dorian/event/handlers/risk_events.py`
//! to Rust. The Rust engine owns:
//!
//! - **Risk identification**: KB queries to discover potential risks for operators
//! - **Mitigation discovery**: KB queries for mitigations + direct alternatives
//! - **Description rendering**: Template interpolation for suggestion cards
//! - **Pathway evaluation**: Cross-view DQ metric → model risk connections
//! - **Suggestion construction**: Building the full suggestion payload
//!
//! The Python runtime still handles:
//! - Actual data check execution (pandas/scipy functions in `dorian.toolbox.checks`)
//! - Dataset mitigations (pandas DataFrame transformations)
//! - Pipeline rewrites (Python rewrite engine in `dorian/pipeline/`)
//!
//! Architecture (aligned with TRUSTIFAI position paper):
//!
//! ```text
//! PipelineNodeAdded ──> identify_operator_risks
//!                            │
//!                     identify_risks (KB: might_introduce)
//!                            │
//!                            ▼
//!                     PotentialRiskIdentified ──> identify_mitigations
//!                                                     │ (KB: might_mitigate)
//!                                                     │ (KB: direct alternatives)
//!                                                     ▼
//!                                           MitigationActionsIdentified
//!                                                     │
//!                                                render_suggestion ──> Redis XADD
//!                                                                       (status=potential)
//!
//! DataProfiled ──> run_data_checks (Python runtime)
//!                    │  for each operator in pipeline:
//!                    │    KB: operator → risks → checks
//!                    │    invoke check on data (Python)
//!                    │    if confirmed:
//!                    ▼
//!                RiskIdentified (status=actionable, severity=high)
//!                    │  → same identify_mitigations → render_suggestion chain
//!                    ▼
//!                Redis XADD (status=actionable)
//! ```

pub mod identification;
pub mod mitigation;
pub mod pathway;
pub mod strategies;
pub mod suggestion;

pub use identification::{RiskIdentifier, PotentialRisk};
pub use mitigation::MitigationDiscovery;
pub use pathway::{PathwayEvaluator, PathwayMatch};
pub use strategies::{
    builtin_strategies, DirectAlternativeStrategy, KbMitigationStrategy,
    MitigationAction, MitigationStrategy,
};
pub use suggestion::{SuggestionBuilder, SuggestionCard};
