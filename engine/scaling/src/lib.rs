//! dorian-scaling — Elastic scaling controller.
//!
//! Monitors host/container resources and makes scaling decisions for runtime
//! worker pools. Works identically on bare metal and in containers:
//! - Bare metal: reads /proc for physical resource limits
//! - Container: reads cgroup v2/v1 for container resource limits
//!
//! Same code path, same thresholds — only the resource ceiling differs.

pub mod config;
pub mod monitor;
pub mod policy;
