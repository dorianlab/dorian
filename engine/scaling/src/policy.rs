//! Scaling policy — decision engine for elastic worker pool sizing.
//!
//! Ports `dorian/workers/scaling.py` to Rust.
//!
//! The policy evaluates resource metrics and produces scaling decisions:
//! - Scale up: CPU ≥ high OR RAM ≥ high OR queue backlog
//! - Scale down: CPU ≤ low AND RAM ≤ low AND queue empty
//! - Emergency: disk ≥ high → scale to minimum
//! - Cooldown: no action within cooldown period after last action
//! - Hysteresis: separate high/low thresholds prevent oscillation

use crate::config::ScalingConfig;
use crate::monitor::ResourceMetrics;
use serde::{Deserialize, Serialize};
use std::time::Instant;

// ---------------------------------------------------------------------------
// Scaling decisions
// ---------------------------------------------------------------------------

/// Direction of a scaling action.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ScaleDirection {
    Up,
    Down,
}

/// A scaling decision produced by the policy.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScalingDecision {
    /// Scale up or down.
    pub direction: ScaleDirection,
    /// Number of workers to add (up) or remove (down).
    pub count: u32,
    /// Human-readable reason.
    pub reason: String,
    /// Epoch seconds when the decision was made.
    pub timestamp: f64,
}

/// Result of evaluating metrics against the policy.
#[derive(Debug, Clone)]
pub enum PolicyResult {
    /// A scaling action should be taken.
    Scale(ScalingDecision),
    /// No action needed.
    NoAction,
    /// In cooldown period, cannot scale.
    InCooldown,
}

// ---------------------------------------------------------------------------
// Scaling policy
// ---------------------------------------------------------------------------

/// Evaluates resource metrics and decides whether to scale.
pub struct ScalingPolicy {
    config: ScalingConfig,
    last_scale_time: Option<Instant>,
}

impl ScalingPolicy {
    /// Create a new policy with the given configuration.
    pub fn new(config: ScalingConfig) -> Self {
        ScalingPolicy {
            config,
            last_scale_time: None,
        }
    }

    /// Whether we are within the cooldown period after a scaling action.
    pub fn in_cooldown(&self) -> bool {
        if let Some(last) = self.last_scale_time {
            last.elapsed().as_secs_f64() < self.config.cooldown_secs
        } else {
            false
        }
    }

    /// Record that a scaling action was performed (resets cooldown).
    pub fn record_action(&mut self) {
        self.last_scale_time = Some(Instant::now());
    }

    /// Evaluate resource metrics and produce a scaling decision.
    ///
    /// Arguments:
    /// - `metrics`: current resource measurements
    /// - `current_workers`: active worker count
    pub fn evaluate(&mut self, metrics: &ResourceMetrics, current_workers: u32) -> PolicyResult {
        // Check cooldown.
        if self.in_cooldown() {
            return PolicyResult::InCooldown;
        }

        // Clone config to avoid borrow conflict with self.record_action().
        let cfg = self.config.clone();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();

        // -- Emergency: disk critical → scale to minimum ---
        if metrics.disk_percent >= cfg.disk_high && current_workers > cfg.min_workers {
            let remove = current_workers - cfg.min_workers;
            self.record_action();
            return PolicyResult::Scale(ScalingDecision {
                direction: ScaleDirection::Down,
                count: remove,
                reason: format!(
                    "disk critical ({:.0}% >= {:.0}%)",
                    metrics.disk_percent * 100.0,
                    cfg.disk_high * 100.0
                ),
                timestamp: now,
            });
        }

        // -- Scale up ---
        if current_workers < cfg.max_workers {
            let cpu_hot = metrics.cpu_percent >= cfg.cpu_high;
            let ram_hot = metrics.ram_percent >= cfg.ram_high;
            let queue_backlog = metrics.processing_tasks > 0
                && metrics.processing_tasks >= current_workers;

            if cpu_hot || ram_hot || queue_backlog {
                let add = std::cmp::min(cfg.scale_step, cfg.max_workers - current_workers);
                let reason = if cpu_hot {
                    format!(
                        "CPU high ({:.0}% >= {:.0}%)",
                        metrics.cpu_percent * 100.0,
                        cfg.cpu_high * 100.0
                    )
                } else if ram_hot {
                    format!(
                        "RAM high ({:.0}% >= {:.0}%)",
                        metrics.ram_percent * 100.0,
                        cfg.ram_high * 100.0
                    )
                } else {
                    format!(
                        "queue backlog ({} tasks >= {} workers)",
                        metrics.processing_tasks, current_workers
                    )
                };

                self.record_action();
                return PolicyResult::Scale(ScalingDecision {
                    direction: ScaleDirection::Up,
                    count: add,
                    reason,
                    timestamp: now,
                });
            }
        }

        // -- Scale down ---
        if current_workers > cfg.min_workers {
            let cpu_cool = metrics.cpu_percent <= cfg.cpu_low;
            let ram_cool = metrics.ram_percent <= cfg.ram_low;
            let idle = metrics.processing_tasks == 0;

            if cpu_cool && ram_cool && idle {
                let remove = std::cmp::min(cfg.scale_step, current_workers - cfg.min_workers);
                self.record_action();
                return PolicyResult::Scale(ScalingDecision {
                    direction: ScaleDirection::Down,
                    count: remove,
                    reason: format!(
                        "idle (CPU {:.0}% <= {:.0}%, RAM {:.0}% <= {:.0}%, queue empty)",
                        metrics.cpu_percent * 100.0,
                        cfg.cpu_low * 100.0,
                        metrics.ram_percent * 100.0,
                        cfg.ram_low * 100.0
                    ),
                    timestamp: now,
                });
            }
        }

        PolicyResult::NoAction
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::monitor::ResourceSource;

    fn make_metrics(cpu: f64, ram: f64, disk: f64, processing: u32) -> ResourceMetrics {
        ResourceMetrics {
            timestamp: 1000.0,
            cpu_percent: cpu,
            ram_percent: ram,
            disk_percent: disk,
            processing_tasks: processing,
            source: ResourceSource::BareMetal,
            ..Default::default()
        }
    }

    fn default_config() -> ScalingConfig {
        ScalingConfig::default()
    }

    #[test]
    fn test_no_action_in_normal_range() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.50, 0.50, 0.40, 1);
        let result = policy.evaluate(&metrics, 4);
        assert!(matches!(result, PolicyResult::NoAction));
    }

    #[test]
    fn test_scale_up_on_high_cpu() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.90, 0.50, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Up);
                assert_eq!(d.count, 1);
                assert!(d.reason.contains("CPU"));
            }
            _ => panic!("expected scale up"),
        }
    }

    #[test]
    fn test_scale_up_on_high_ram() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.50, 0.85, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Up);
                assert!(d.reason.contains("RAM"));
            }
            _ => panic!("expected scale up"),
        }
    }

    #[test]
    fn test_scale_up_on_queue_backlog() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.50, 0.50, 0.40, 4);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Up);
                assert!(d.reason.contains("queue"));
            }
            _ => panic!("expected scale up"),
        }
    }

    #[test]
    fn test_scale_down_when_idle() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.10, 0.10, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Down);
                assert_eq!(d.count, 1);
                assert!(d.reason.contains("idle"));
            }
            _ => panic!("expected scale down"),
        }
    }

    #[test]
    fn test_no_scale_down_at_minimum() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.10, 0.10, 0.40, 0);
        // Already at min_workers (1).
        let result = policy.evaluate(&metrics, 1);
        assert!(matches!(result, PolicyResult::NoAction));
    }

    #[test]
    fn test_no_scale_up_at_maximum() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.95, 0.95, 0.40, 10);
        // Already at max_workers (8).
        let result = policy.evaluate(&metrics, 8);
        // Should not scale up.
        assert!(!matches!(
            result,
            PolicyResult::Scale(ScalingDecision {
                direction: ScaleDirection::Up,
                ..
            })
        ));
    }

    #[test]
    fn test_disk_emergency_scales_to_minimum() {
        let mut policy = ScalingPolicy::new(default_config());
        let metrics = make_metrics(0.50, 0.50, 0.95, 5);
        let result = policy.evaluate(&metrics, 6);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Down);
                // Should remove all but min_workers.
                assert_eq!(d.count, 5); // 6 - 1 = 5
                assert!(d.reason.contains("disk"));
            }
            _ => panic!("expected emergency scale down"),
        }
    }

    #[test]
    fn test_cooldown_prevents_action() {
        let mut policy = ScalingPolicy::new(default_config());

        // First evaluation triggers scale-up.
        let metrics = make_metrics(0.90, 0.50, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        assert!(matches!(result, PolicyResult::Scale(_)));

        // Second evaluation should be blocked by cooldown.
        let result2 = policy.evaluate(&metrics, 5);
        assert!(matches!(result2, PolicyResult::InCooldown));
    }

    #[test]
    fn test_cooldown_expires() {
        let mut cfg = default_config();
        cfg.cooldown_secs = 0.001; // 1ms cooldown
        let mut policy = ScalingPolicy::new(cfg);

        let metrics = make_metrics(0.90, 0.50, 0.40, 0);
        policy.evaluate(&metrics, 4); // trigger cooldown

        std::thread::sleep(std::time::Duration::from_millis(5));

        // Cooldown should have expired.
        let result = policy.evaluate(&metrics, 5);
        assert!(!matches!(result, PolicyResult::InCooldown));
    }

    #[test]
    fn test_hysteresis_prevents_oscillation() {
        let mut policy = ScalingPolicy::new(ScalingConfig {
            cooldown_secs: 0.0, // disable cooldown for this test
            ..default_config()
        });

        // CPU at 0.50: between low (0.30) and high (0.85).
        // Should not trigger scale up OR scale down (with processing > 0).
        let metrics = make_metrics(0.50, 0.50, 0.40, 1);
        let result = policy.evaluate(&metrics, 4);
        assert!(matches!(result, PolicyResult::NoAction));
    }

    #[test]
    fn test_scale_step_limits_change() {
        let cfg = ScalingConfig {
            scale_step: 2,
            max_workers: 10,
            cooldown_secs: 0.0,
            ..default_config()
        };
        let mut policy = ScalingPolicy::new(cfg);

        let metrics = make_metrics(0.90, 0.50, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.direction, ScaleDirection::Up);
                assert_eq!(d.count, 2); // scale_step = 2
            }
            _ => panic!("expected scale up"),
        }
    }

    #[test]
    fn test_scale_step_capped_at_boundary() {
        let cfg = ScalingConfig {
            scale_step: 5,
            max_workers: 6,
            cooldown_secs: 0.0,
            ..default_config()
        };
        let mut policy = ScalingPolicy::new(cfg);

        let metrics = make_metrics(0.90, 0.50, 0.40, 0);
        let result = policy.evaluate(&metrics, 4);
        match result {
            PolicyResult::Scale(d) => {
                assert_eq!(d.count, 2); // can only add 2 (6 - 4)
            }
            _ => panic!("expected scale up"),
        }
    }

    #[test]
    fn test_scaling_decision_serialization() {
        let d = ScalingDecision {
            direction: ScaleDirection::Up,
            count: 2,
            reason: "CPU high".to_string(),
            timestamp: 1000.0,
        };
        let json = serde_json::to_string(&d).unwrap();
        let parsed: ScalingDecision = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.direction, ScaleDirection::Up);
        assert_eq!(parsed.count, 2);
    }

    #[test]
    fn test_not_scale_down_while_processing() {
        let mut policy = ScalingPolicy::new(ScalingConfig {
            cooldown_secs: 0.0,
            ..default_config()
        });

        // Low CPU/RAM but still processing tasks — should NOT scale down.
        let metrics = make_metrics(0.10, 0.10, 0.40, 1);
        let result = policy.evaluate(&metrics, 4);
        assert!(matches!(result, PolicyResult::NoAction));
    }
}
