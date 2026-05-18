"""RL job submission -- UX-isolated routing contract.

The SLO is stated in internal design note section 5: "RL
background work must have zero impact on end-user UX responsiveness".
This module is the Python-side contract that enforces it at the
dispatch boundary.

Key design points:

  * Dedicated Redis stream (`rl:jobs`) separate from `exec:jobs`.
  * Dedicated consumer group + worker pool (`exec-worker-rl`).
  * Priority tag on each entry so a shared claimer can still
    preempt in favor of user-facing entries if a deployment
    chooses to share pools.
  * Backpressure gate: RL submission pauses when the user queue
    depth exceeds a configured threshold. Implemented as a simple
    check against the observability endpoint; the claimer side is
    the authoritative enforcement.

This module ships the contract -- dataclasses + env-var names + a
thin ``submit_rl_job`` helper. The live wiring (Redis XADD call,
backpressure read, metric emission) lives in
``backend/eventbus_shadow.py`` patterns and is a follow-up.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Env vars -- defaults match the production-ready convention
# ---------------------------------------------------------------------------

RL_JOBS_STREAM: str = os.environ.get(
    "DORIAN_RL_JOBS_STREAM", "rl:jobs"
)
RL_GROUP: str = os.environ.get(
    "DORIAN_RL_EXEC_GROUP", "exec-rl"
)
RL_BACKPRESSURE_THRESHOLD: int = int(
    os.environ.get("DORIAN_RL_BACKPRESSURE_USER_QUEUE", "256")
)
RL_BACKPRESSURE_POLL_INTERVAL_MS: int = int(
    os.environ.get("DORIAN_RL_BACKPRESSURE_POLL_MS", "500")
)
RL_BACKPRESSURE_RELEASE_RATIO: float = float(
    os.environ.get("DORIAN_RL_BACKPRESSURE_RELEASE_RATIO", "0.5")
)


Priority = Literal["low", "normal", "high"]


@dataclass(frozen=True)
class RLJob:
    """Payload submitted to the rl:jobs stream.

    Mirrors the shape of the existing `exec:jobs` envelope so the
    Go / Python claimers can share most of the dispatch code. The
    `priority` field lets future shared-pool deployments preempt
    low-priority RL work.
    """

    kind: str  # e.g. "rl:run_pipeline" / "rl:eval_candidate"
    inputs: dict  # actor inputs (pipeline_json, dataset_id, seed, ...)
    run_id: str
    episode: int
    priority: Priority = "low"
    emitted_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


class BackpressureState:
    """Sticky-threshold backpressure tracker.

    Engage when the user queue depth rises above ``threshold``;
    release only when it drops below ``threshold * release_ratio``
    (hysteresis prevents rapid flapping). The claimer on the RL
    side consults this before XREADGROUP to pause consumption.

    Purely in-memory -- authoritative state lives in Redis via
    `eventbus:rl:backpressure_engaged` set by the backend on
    queue-depth metric emission.
    """

    def __init__(
        self,
        *,
        threshold: int = RL_BACKPRESSURE_THRESHOLD,
        release_ratio: float = RL_BACKPRESSURE_RELEASE_RATIO,
    ) -> None:
        self._threshold = threshold
        self._release = int(threshold * release_ratio)
        self._engaged = False
        self._last_engaged_ms: int = 0
        self._last_released_ms: int = 0

    @property
    def engaged(self) -> bool:
        return self._engaged

    def observe(self, user_queue_depth: int) -> bool:
        """Update state from a fresh queue-depth observation. Returns
        the new `engaged` flag."""
        now = int(time.time() * 1000)
        if not self._engaged and user_queue_depth > self._threshold:
            self._engaged = True
            self._last_engaged_ms = now
        elif self._engaged and user_queue_depth < self._release:
            self._engaged = False
            self._last_released_ms = now
        return self._engaged

    def since_engaged_ms(self) -> int:
        if not self._engaged:
            return 0
        return int(time.time() * 1000) - self._last_engaged_ms


# ---------------------------------------------------------------------------
# Submission contract
# ---------------------------------------------------------------------------


def submit_rl_job(
    kind: str,
    inputs: dict,
    *,
    run_id: str,
    episode: int,
    priority: Priority = "low",
    backpressure: BackpressureState | None = None,
) -> bool:
    """Submit an RL job to the rl:jobs stream.

    Returns ``True`` if the job was queued, ``False`` if
    backpressure refused. Backpressure-refused jobs are dropped by
    design: the RL trainer re-proposes candidates each rollout, so
    a skipped submission is not a correctness issue -- the next
    rollout reconsiders. Missing user-queue-observability metrics
    means backpressure is treated as disengaged (fail-open on the
    observability path, fail-closed only on explicit backpressure
    state).

    v1 is a contract only: the actual ``redis.xadd`` call lives in
    the Python eventbus-shadow forwarder or the Go eventbus binary,
    routed by stream name. This function asserts the shape and
    returns True; once the live path lands, the XADD happens here.
    """
    if backpressure is not None and backpressure.engaged:
        return False
    job = RLJob(
        kind=kind,
        inputs=inputs,
        run_id=run_id,
        episode=episode,
        priority=priority,
    )
    _ = job  # live XADD comes with the integration commit
    return True


__all__ = [
    "BackpressureState",
    "Priority",
    "RL_BACKPRESSURE_POLL_INTERVAL_MS",
    "RL_BACKPRESSURE_RELEASE_RATIO",
    "RL_BACKPRESSURE_THRESHOLD",
    "RL_GROUP",
    "RL_JOBS_STREAM",
    "RLJob",
    "submit_rl_job",
]
