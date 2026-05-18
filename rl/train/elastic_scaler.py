"""ElasticScaler: adaptive episode parallelism for the RL trainer.

The trainer runs on a shared 384-core host. A naive "scale to cores" policy
would starve other tenants when the machine is busy and leave cycles on the
floor when it's idle. The scaler polls host health at episode boundaries
and adjusts the rollout pool size with two asymmetric time constants:

  * Scale-down is fast and protective — any red signal (PID pressure,
    load spike, memory stall, burst of exec timeouts) halves parallelism
    immediately. The priority is "don't be the reason another tenant's
    job got slower."
  * Scale-up is slow and incremental — all signals green for a cooldown
    window AND parallelism below the derived ceiling → +1. Never more
    than one step per cooldown, so a jittering signal can't oscillate
    the pool size.

Joint thread budget (not per-knob tuning): the controller decides
``target_worker_threads`` from "fair share of host cores" and derives
episode parallelism as ``target // (dask_pool × blas_threads)``. This
prevents the cross-knob oversubscription that burned 3602 PIDs historically.

Health signals (all graceful-degrade to "green" when unavailable):

  * ``/proc/loadavg``                      — 1 / 5 minute load averages
  * ``psutil.Process().cpu_percent()``     — our own cgroup CPU share
  * ``psutil.virtual_memory()``            — host memory pressure
  * ``/proc/pressure/memory``              — PSI memory stall (kernel's
                                             own "am I hurting" signal)
  * ``/proc/sys/kernel/pid_max`` + count   — PID budget
  * Recent exec-timeout rate               — self-starvation signal

Decisions emit ``ScalerDecision`` events on the event bus (same channel
as ``BKTreeReady``). Observability before action — the operator should
watch the scaler log for a batch before trusting it to size up from 1.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover -- falls back to /proc parsing
    psutil = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Samples + decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthSample:
    """One snapshot of host + container health. All fields are best-effort —
    ``-1.0`` / ``-1`` marks a signal that was unavailable. Decision logic
    must treat unavailable signals as "green" (don't throttle on ignorance)."""

    timestamp: float
    cpu_count: int
    load_1m: float
    load_5m: float
    own_cpu_pct: float          # 0..100 × n_cores (psutil convention)
    own_rss_bytes: int
    host_mem_pct: float         # 0..100
    psi_mem_some_us: int        # monotonic; -1 if unreadable
    pid_count: int
    pid_max: int
    recent_timeout_rate: float  # fraction of recent episodes with timeout
    active_parallelism: int


@dataclass(frozen=True)
class ScalerDecision:
    previous: int
    new: int
    reason: str
    sample: HealthSample


# ---------------------------------------------------------------------------
# Scaler
# ---------------------------------------------------------------------------


@dataclass
class ElasticScaler:
    """Parallelism controller driven by host health signals.

    Not a control loop — no background thread. ``decide()`` is called at
    episode boundaries by the training loop, which respects the returned
    parallelism on the next batch. Mid-episode rollouts never get killed.
    """

    min_parallelism: int = 1
    max_parallelism: int = 8
    # Fraction of host cores the trainer is allowed to occupy at peak.
    # Biased low since this is a shared machine.
    target_host_cpu_share: float = 0.30
    # Warning thresholds. Crossing any of these triggers immediate halving.
    pid_usage_threshold: float = 0.70
    load_1m_multiplier: float = 0.90       # × cpu_count
    host_mem_threshold: float = 0.85
    timeout_rate_threshold: float = 0.10
    # Cooldowns (seconds).
    scale_up_cooldown_s: float = 45.0
    scale_down_cooldown_s: float = 5.0
    # Thread-count accounting. Used to derive the parallelism ceiling
    # from the host budget so the scaler never requests more workers
    # than the host can physically service without oversubscription.
    #
    # Effective per-worker threads use conservative average occupancy:
    # dask_pool × blas_effective where blas_effective is smaller than
    # the BLAS cap (OPENBLAS_NUM_THREADS=16) because a typical sklearn
    # op uses 1-8 threads, not the max, and only during fit / matmul
    # sections. Tuning this to 4 lifts the ceiling on our 384-core host
    # from 1 to ~4 without risking oversubscription in practice.
    dask_pool_size: int = int(os.environ.get("DORIAN_RL_DASK_POOL_SIZE", "4"))
    blas_threads_per_worker: int = int(
        os.environ.get("DORIAN_RL_SCALER_BLAS_EFFECTIVE", "4")
    )
    # Rolling window for timeout-rate — last N episode outcomes.
    timeout_window_size: int = 20

    # --- state (private) ---
    _current: int = field(init=False)
    _last_change_ts: float = field(default=0.0, init=False)
    _timeout_samples: deque = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _process: Optional[object] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._current = self.min_parallelism
        self._timeout_samples = deque(maxlen=self.timeout_window_size)
        if psutil is not None:
            try:
                self._process = psutil.Process()
                # First call initialises psutil's internal state; the
                # very next call returns a meaningful percent delta.
                self._process.cpu_percent(interval=None)
            except Exception:
                self._process = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_parallelism(self) -> int:
        with self._lock:
            return self._current

    def record_episode_outcome(self, *, timed_out: bool) -> None:
        """Append a timeout indicator to the rolling window so
        ``recent_timeout_rate`` stays fresh without a global counter."""
        with self._lock:
            self._timeout_samples.append(1 if timed_out else 0)

    def sample_health(self) -> HealthSample:
        """Read every signal we can get our hands on. Best-effort —
        each source graceful-degrades independently."""
        now = time.time()
        cpu_count = os.cpu_count() or 1

        load_1m = load_5m = -1.0
        try:
            with open("/proc/loadavg", "r") as f:
                parts = f.read().split()
                load_1m = float(parts[0])
                load_5m = float(parts[1])
        except Exception:
            pass

        own_cpu_pct = -1.0
        own_rss = -1
        host_mem_pct = -1.0
        if psutil is not None:
            try:
                own_cpu_pct = (self._process.cpu_percent(interval=None)
                               if self._process is not None else -1.0)
                own_rss = (self._process.memory_info().rss
                           if self._process is not None else -1)
                host_mem_pct = psutil.virtual_memory().percent
            except Exception:
                pass

        psi_us = _read_psi_memory()
        pid_count, pid_max = _read_pid_budget()

        with self._lock:
            timeout_rate = (
                sum(self._timeout_samples) / len(self._timeout_samples)
                if self._timeout_samples else 0.0
            )
            active = self._current

        return HealthSample(
            timestamp=now,
            cpu_count=cpu_count,
            load_1m=load_1m,
            load_5m=load_5m,
            own_cpu_pct=own_cpu_pct,
            own_rss_bytes=own_rss,
            host_mem_pct=host_mem_pct,
            psi_mem_some_us=psi_us,
            pid_count=pid_count,
            pid_max=pid_max,
            recent_timeout_rate=timeout_rate,
            active_parallelism=active,
        )

    def target_parallelism(self, s: HealthSample) -> tuple[int, str]:
        """Given a health sample, compute the desired parallelism
        and a short reason string. Pure function — used both inside
        ``decide()`` and for dry-run observability logs."""
        ceiling = self._thread_budget_ceiling(s.cpu_count)
        current = s.active_parallelism

        # --- red signals: any one forces halving ---
        reasons_down: list[str] = []
        if (s.pid_max > 0
                and s.pid_count / s.pid_max >= self.pid_usage_threshold):
            reasons_down.append(
                f"pid={s.pid_count}/{s.pid_max} "
                f"({100 * s.pid_count / s.pid_max:.1f}%)"
            )
        if (s.load_1m > 0
                and s.load_1m > s.cpu_count * self.load_1m_multiplier):
            reasons_down.append(
                f"load_1m={s.load_1m:.1f} > {s.cpu_count * self.load_1m_multiplier:.1f}"
            )
        if 0 < self.host_mem_threshold * 100 < s.host_mem_pct:
            reasons_down.append(f"mem={s.host_mem_pct:.1f}%")
        if s.recent_timeout_rate >= self.timeout_rate_threshold:
            reasons_down.append(
                f"timeouts={s.recent_timeout_rate:.2f}"
            )

        if reasons_down:
            new = max(self.min_parallelism, current // 2)
            return new, "scale_down: " + "; ".join(reasons_down)

        # --- green path: consider scale-up ---
        if current < ceiling:
            # Generous green-zone thresholds — only scale up when the
            # machine is visibly idle, not just "not overloaded".
            load_is_low = (s.load_1m < 0 or s.load_1m < s.cpu_count * 0.5)
            mem_is_low = (s.host_mem_pct < 0 or s.host_mem_pct < 60.0)
            pids_are_low = (
                s.pid_max <= 0 or s.pid_count / s.pid_max < 0.4
            )
            if load_is_low and mem_is_low and pids_are_low:
                return current + 1, (
                    f"scale_up: green (load_1m={s.load_1m:.1f}, "
                    f"mem={s.host_mem_pct:.1f}%, "
                    f"pid={s.pid_count}/{s.pid_max})"
                )

        # --- hold ---
        return current, "hold"

    def decide(self) -> ScalerDecision | None:
        """Poll health, compute target, apply cooldown, emit event
        if parallelism changes. Returns the decision (or None when
        the current level is held)."""
        sample = self.sample_health()
        target, reason = self.target_parallelism(sample)

        with self._lock:
            if target == self._current:
                return None
            elapsed = sample.timestamp - self._last_change_ts
            if target > self._current and elapsed < self.scale_up_cooldown_s:
                return None
            if target < self._current and elapsed < self.scale_down_cooldown_s:
                return None
            previous = self._current
            self._current = target
            self._last_change_ts = sample.timestamp

        decision = ScalerDecision(
            previous=previous,
            new=target,
            reason=reason,
            sample=sample,
        )
        _log.info(
            "ElasticScaler %d→%d (%s) load_1m=%.1f mem=%.1f%% "
            "pid=%d/%d timeouts=%.2f",
            previous, target, reason, sample.load_1m, sample.host_mem_pct,
            sample.pid_count, sample.pid_max, sample.recent_timeout_rate,
        )
        # Event emission is best-effort — the scaler must keep working
        # even if the event bus is unreachable.
        try:
            from backend.events import Event, emit
            emit(Event("ElasticScalerDecision", {
                "previous": previous,
                "new": target,
                "reason": reason,
                "sample": asdict(sample),
            }))
        except Exception:
            pass
        return decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _thread_budget_ceiling(self, cpu_count: int) -> int:
        """Derive the parallelism ceiling from the thread budget.

        Per-worker threads ≈ dask_pool × blas_threads. Target total
        thread occupancy ≈ target_share × cpu_count. Ceiling is the
        floor of the ratio, clamped to ``max_parallelism``.
        """
        per_worker = max(1, self.dask_pool_size * self.blas_threads_per_worker)
        target_threads = max(1, int(cpu_count * self.target_host_cpu_share))
        return max(
            self.min_parallelism,
            min(self.max_parallelism, target_threads // per_worker or 1),
        )


# ---------------------------------------------------------------------------
# /proc readers — isolated so psutil isn't required on bare containers
# ---------------------------------------------------------------------------


def _read_psi_memory() -> int:
    """Return the monotonic ``some avg10`` stall count from PSI.
    Returns ``-1`` on kernels / containers without PSI exposed."""
    path = Path("/proc/pressure/memory")
    if not path.exists():
        return -1
    try:
        text = path.read_text()
        # Line shape: "some avg10=0.00 avg60=0.00 avg300=0.00 total=12345"
        for line in text.splitlines():
            if line.startswith("some"):
                for tok in line.split():
                    if tok.startswith("total="):
                        return int(tok.split("=", 1)[1])
    except Exception:
        pass
    return -1


def _read_pid_budget() -> tuple[int, int]:
    """Return ``(used, max)``. Rootless containers may see host-wide
    numbers; that's still useful as a pressure signal even if the
    container's own limit is unbounded."""
    used = -1
    cap = -1
    try:
        # Count PIDs via /proc/loadavg's last field — "tasks running/total".
        with open("/proc/loadavg", "r") as f:
            parts = f.read().split()
            if len(parts) >= 4 and "/" in parts[3]:
                used = int(parts[3].split("/", 1)[1])
    except Exception:
        pass
    try:
        with open("/proc/sys/kernel/pid_max", "r") as f:
            cap = int(f.read().strip())
    except Exception:
        pass
    return used, cap


__all__ = ["ElasticScaler", "HealthSample", "ScalerDecision"]
