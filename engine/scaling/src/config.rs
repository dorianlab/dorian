//! Scaling configuration — thresholds, limits, and tuning knobs.
//!
//! Mirrors `WorkerConfig` from `dorian/workers/config.py`.

use serde::{Deserialize, Serialize};

/// Configuration for the elastic scaling controller.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScalingConfig {
    // -- Worker pool bounds --
    /// Minimum workers in the pool.
    pub min_workers: u32,
    /// Maximum workers in the pool.
    pub max_workers: u32,

    // -- CPU thresholds (0.0..1.0) --
    /// CPU utilization above this triggers scale-up.
    pub cpu_high: f64,
    /// CPU utilization below this allows scale-down.
    pub cpu_low: f64,

    // -- RAM thresholds (0.0..1.0) --
    /// RAM utilization above this triggers scale-up.
    pub ram_high: f64,
    /// RAM utilization below this allows scale-down.
    pub ram_low: f64,

    // -- Disk safety (0.0..1.0) --
    /// Disk utilization above this triggers emergency scale-down to minimum.
    pub disk_high: f64,

    // -- Scaling behavior --
    /// Workers added/removed per scaling action.
    pub scale_step: u32,
    /// Cooldown period between scaling actions (seconds).
    pub cooldown_secs: f64,
    /// Resource metrics collection interval (seconds).
    pub monitor_interval_secs: f64,

    // -- Per-worker limits --
    /// Memory limit per worker (bytes, 0 = unlimited).
    pub worker_memory_limit: u64,
    /// Threads per worker.
    pub worker_threads: u32,
}

impl Default for ScalingConfig {
    fn default() -> Self {
        ScalingConfig {
            min_workers: 1,
            max_workers: 8,
            cpu_high: 0.85,
            cpu_low: 0.30,
            ram_high: 0.80,
            ram_low: 0.30,
            disk_high: 0.90,
            scale_step: 1,
            cooldown_secs: 30.0,
            monitor_interval_secs: 5.0,
            worker_memory_limit: 4 * 1024 * 1024 * 1024, // 4GB
            worker_threads: 1,
        }
    }
}

impl ScalingConfig {
    /// Load from environment variables (DORIAN_WORKERS_*).
    pub fn from_env() -> Self {
        let mut cfg = Self::default();

        if let Ok(v) = std::env::var("DORIAN_WORKERS_MIN") {
            if let Ok(n) = v.parse() {
                cfg.min_workers = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_MAX") {
            if let Ok(n) = v.parse() {
                cfg.max_workers = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_CPU_HIGH") {
            if let Ok(n) = v.parse() {
                cfg.cpu_high = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_CPU_LOW") {
            if let Ok(n) = v.parse() {
                cfg.cpu_low = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_RAM_HIGH") {
            if let Ok(n) = v.parse() {
                cfg.ram_high = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_RAM_LOW") {
            if let Ok(n) = v.parse() {
                cfg.ram_low = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_COOLDOWN") {
            if let Ok(n) = v.parse() {
                cfg.cooldown_secs = n;
            }
        }
        if let Ok(v) = std::env::var("DORIAN_WORKERS_MONITOR_INTERVAL") {
            if let Ok(n) = v.parse() {
                cfg.monitor_interval_secs = n;
            }
        }

        cfg
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let cfg = ScalingConfig::default();
        assert_eq!(cfg.min_workers, 1);
        assert_eq!(cfg.max_workers, 8);
        assert!((cfg.cpu_high - 0.85).abs() < f64::EPSILON);
        assert!((cfg.cpu_low - 0.30).abs() < f64::EPSILON);
        assert!((cfg.ram_high - 0.80).abs() < f64::EPSILON);
        assert!((cfg.cooldown_secs - 30.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_config_serialization() {
        let cfg = ScalingConfig::default();
        let json = serde_json::to_string(&cfg).unwrap();
        let parsed: ScalingConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.min_workers, cfg.min_workers);
        assert_eq!(parsed.max_workers, cfg.max_workers);
    }

    #[test]
    fn test_hysteresis_invariant() {
        let cfg = ScalingConfig::default();
        // High thresholds must be greater than low thresholds (hysteresis).
        assert!(cfg.cpu_high > cfg.cpu_low);
        assert!(cfg.ram_high > cfg.ram_low);
    }
}
