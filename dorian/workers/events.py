"""Event name constants for the workers subsystem.

All worker events flow through the Dorian event bus when available,
falling back to local logging in standalone mode.
"""

# Emitted by Monitor on each collection cycle.
WORKER_METRICS_COLLECTED = "WorkerMetricsCollected"

# Emitted by ScalingPolicy when thresholds are breached.
WORKER_SCALE_UP = "WorkerScaleUp"
WORKER_SCALE_DOWN = "WorkerScaleDown"

# Emitted by Supervisor after successfully spawning/retiring a worker.
WORKER_SPAWNED = "WorkerSpawned"
WORKER_RETIRED = "WorkerRetired"

# Lifecycle events.
WORKER_SUPERVISOR_STARTED = "WorkerSupervisorStarted"
WORKER_SUPERVISOR_STOPPED = "WorkerSupervisorStopped"
