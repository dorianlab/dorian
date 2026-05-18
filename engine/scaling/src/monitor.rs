//! Resource monitoring — cgroup-aware CPU, RAM, disk metrics.
//!
//! Ports `dorian/workers/monitor.py` to Rust.
//!
//! The monitor is container-aware:
//! - Inside a container: reads cgroup v2/v1 for memory limits
//! - On bare metal: reads /proc or platform-native APIs
//! - Same `ResourceMetrics` struct regardless of deployment mode

use serde::{Deserialize, Serialize};
use std::path::Path;

// ---------------------------------------------------------------------------
// Resource source detection
// ---------------------------------------------------------------------------

/// Where resource limits come from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ResourceSource {
    /// Physical machine resources.
    BareMetal,
    /// cgroup v2 container limits.
    CgroupV2,
    /// cgroup v1 container limits.
    CgroupV1,
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

/// Collected resource metrics at a point in time.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceMetrics {
    /// Epoch seconds when metrics were collected.
    pub timestamp: f64,
    /// CPU utilization (0.0..1.0).
    pub cpu_percent: f64,
    /// RAM utilization (0.0..1.0).
    pub ram_percent: f64,
    /// RAM used in bytes.
    pub ram_used: u64,
    /// RAM total in bytes.
    pub ram_total: u64,
    /// Disk utilization (0.0..1.0).
    pub disk_percent: f64,
    /// Disk used in bytes.
    pub disk_used: u64,
    /// Disk total in bytes.
    pub disk_total: u64,
    /// How resource limits were detected.
    pub source: ResourceSource,
    /// Number of active runtime workers.
    pub active_workers: u32,
    /// Number of tasks currently being processed.
    pub processing_tasks: u32,
    /// Sum of all runtime queue depths.
    pub total_queue_depth: u32,
}

impl Default for ResourceMetrics {
    fn default() -> Self {
        ResourceMetrics {
            timestamp: 0.0,
            cpu_percent: 0.0,
            ram_percent: 0.0,
            ram_used: 0,
            ram_total: 0,
            disk_percent: 0.0,
            disk_used: 0,
            disk_total: 0,
            source: ResourceSource::BareMetal,
            active_workers: 0,
            processing_tasks: 0,
            total_queue_depth: 0,
        }
    }
}

// ---------------------------------------------------------------------------
// cgroup detection (Linux only)
// ---------------------------------------------------------------------------

/// Sentinel value: cgroup reports ≥ 2^62 means "unbounded".
const NO_LIMIT: u64 = 1 << 62;

/// cgroup v2 memory limit path.
const CGROUP_V2_LIMIT: &str = "/sys/fs/cgroup/memory.max";
/// cgroup v2 memory usage path.
const CGROUP_V2_USAGE: &str = "/sys/fs/cgroup/memory.current";
/// cgroup v1 memory limit path.
const CGROUP_V1_LIMIT: &str = "/sys/fs/cgroup/memory/memory.limit_in_bytes";
/// cgroup v1 memory usage path.
const CGROUP_V1_USAGE: &str = "/sys/fs/cgroup/memory/memory.usage_in_bytes";

/// Read a cgroup integer file. Returns None if:
/// - File doesn't exist
/// - Value is "max" (unlimited)
/// - Value ≥ 2^62 (sentinel for unbounded)
fn read_cgroup_int(path: &str) -> Option<u64> {
    let content = std::fs::read_to_string(path).ok()?;
    let trimmed = content.trim();

    if trimmed == "max" {
        return None;
    }

    let value: u64 = trimmed.parse().ok()?;
    if value >= NO_LIMIT {
        return None;
    }

    Some(value)
}

/// Try to read container memory from cgroup v2, then v1.
/// Returns `(used_bytes, total_bytes, source)` or None if not containerized.
pub fn container_ram() -> Option<(u64, u64, ResourceSource)> {
    // Try cgroup v2 first.
    if let (Some(limit), Some(usage)) = (
        read_cgroup_int(CGROUP_V2_LIMIT),
        read_cgroup_int(CGROUP_V2_USAGE),
    ) {
        return Some((usage, limit, ResourceSource::CgroupV2));
    }

    // Try cgroup v1.
    if let (Some(limit), Some(usage)) = (
        read_cgroup_int(CGROUP_V1_LIMIT),
        read_cgroup_int(CGROUP_V1_USAGE),
    ) {
        return Some((usage, limit, ResourceSource::CgroupV1));
    }

    None
}

/// Detect which resource source to use.
pub fn detect_resource_source() -> ResourceSource {
    if Path::new(CGROUP_V2_LIMIT).exists()
        && read_cgroup_int(CGROUP_V2_LIMIT).is_some()
    {
        return ResourceSource::CgroupV2;
    }
    if Path::new(CGROUP_V1_LIMIT).exists()
        && read_cgroup_int(CGROUP_V1_LIMIT).is_some()
    {
        return ResourceSource::CgroupV1;
    }
    ResourceSource::BareMetal
}

/// Collect resource metrics.
///
/// On Linux: reads /proc for CPU, cgroup for RAM if available, statfs for disk.
/// On other platforms: returns defaults (metrics collection will be enhanced
/// when sysinfo crate is integrated).
///
/// NOTE: Full platform metrics (CPU sampling via /proc/stat, disk via statfs)
/// will be implemented when the `sysinfo` crate is added. For now this
/// provides the cgroup-aware RAM detection and the metric structure.
pub fn collect_host_metrics() -> ResourceMetrics {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();

    let mut metrics = ResourceMetrics {
        timestamp: now,
        ..Default::default()
    };

    // RAM: try cgroup first, fall back to defaults.
    if let Some((used, total, source)) = container_ram() {
        metrics.ram_used = used;
        metrics.ram_total = total;
        metrics.ram_percent = if total > 0 {
            used as f64 / total as f64
        } else {
            0.0
        };
        metrics.source = source;
    }
    // NOTE: Bare-metal RAM and CPU require sysinfo crate (Phase 1.5+).

    metrics
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resource_metrics_default() {
        let m = ResourceMetrics::default();
        assert_eq!(m.cpu_percent, 0.0);
        assert_eq!(m.ram_percent, 0.0);
        assert_eq!(m.source, ResourceSource::BareMetal);
    }

    #[test]
    fn test_collect_host_metrics() {
        // Should not panic, returns metrics with timestamp > 0.
        let m = collect_host_metrics();
        assert!(m.timestamp > 0.0);
    }

    #[test]
    fn test_detect_resource_source() {
        // On most dev machines (not in container), this should be BareMetal.
        let source = detect_resource_source();
        // Just verify it doesn't panic.
        assert!(matches!(
            source,
            ResourceSource::BareMetal | ResourceSource::CgroupV1 | ResourceSource::CgroupV2
        ));
    }

    #[test]
    fn test_read_cgroup_int_nonexistent() {
        assert!(read_cgroup_int("/nonexistent/path").is_none());
    }

    #[test]
    fn test_resource_metrics_serialization() {
        let m = ResourceMetrics {
            timestamp: 1000.0,
            cpu_percent: 0.75,
            ram_percent: 0.60,
            ram_used: 4_000_000_000,
            ram_total: 8_000_000_000,
            disk_percent: 0.50,
            disk_used: 100_000_000_000,
            disk_total: 200_000_000_000,
            source: ResourceSource::CgroupV2,
            active_workers: 4,
            processing_tasks: 2,
            total_queue_depth: 5,
        };

        let json = serde_json::to_string(&m).unwrap();
        let parsed: ResourceMetrics = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.active_workers, 4);
        assert!((parsed.cpu_percent - 0.75).abs() < f64::EPSILON);
    }

    #[test]
    fn test_container_ram_on_host() {
        // On a dev machine without cgroups, should return None.
        // In a container, should return Some(...).
        let result = container_ram();
        // Just verify no panic — can't assert specific value without knowing env.
        if let Some((used, total, _)) = result {
            assert!(total > 0);
            assert!(used <= total);
        }
    }
}
