"""Pipeline execution queue with tier-based priority and position tracking.

Lifecycle:
    bridge_logic(client)       — background loop, pops tasks by priority
    submit_for_execution(event) — enqueue a user-initiated pipeline run
    submit_background(payload)  — enqueue a background task (generation)

Queue position and time estimates:
    get_queue_status(uid, session) — returns {position, ahead, estimated_wait_s}
    _emit_queue_updates()          — periodic WS notifications to waiting users

Tier-aware priority:
    - User tier (free/standard/priority/enterprise) determines ZADD score
    - Lower score = higher priority (ZPOPMIN pops lowest first)
    - Within the same tier, submission order is preserved via timestamp tiebreaker
"""

from aenum import IntEnum
import asyncio
import json
import os
import time
import traceback

from .events import Event, aemit
from .envs import aioredis

from dorian.pipeline.execution import handle_pipeline_execution
from dorian.infra.tiers import get_user_tier, tier_priority, get_tier_config


TASK_QUEUE_KEY = "task_queue"
"""Redis sorted set key for the priority task queue."""

QUEUE_META_PREFIX = "queue:meta:"
"""Per-task metadata (uid, session, submit_ts).  TTL 1 hour."""


class Priority(IntEnum):
    BACKGROUND = -1
    USER = -10
    SYSTEM = -20


# In-process signal: replaces Redis PUBLISH/SUBSCRIBE so no pub/sub ACL is needed.
# bridge_logic and submit_for_execution always run in the same asyncio event loop.
_task_signal: asyncio.Event = asyncio.Event()

# Track active executions per uid for concurrency enforcement.
_active_runs: dict[str, int] = {}
_active_lock = asyncio.Lock()

# ── RL concurrency cap (dynamic) ──────────────────────────────────────────
# The RL generator (``source=rl_generator``) enqueues pipelines continuously.
# Without a cap on how many execute concurrently, the event bus floods with
# per-node events and user sessions starve.  ``DISABLE_CROSS_PRODUCT_TRIALS``
# and ``RL_GENERATION_ENABLED`` are on/off kill switches; this throttle is
# the graduated control.
#
# Capacity is **runtime-adjustable**.  ``asyncio.Semaphore`` has no resize
# operation, so we use a counter + condition variable.  The limit is stored
# as a plain int that can be changed via:
#
#   * initial value from ``RL_MAX_CONCURRENT`` env var (default 4)
#   * ``set_rl_concurrency(n)``  — from any coroutine at runtime
#   * HTTP endpoint (see ``dorian/api/routes/admin.py``)
#
# Raising the limit admits more work immediately; lowering it blocks further
# admissions until in-flight runs naturally drain below the new cap.
_rl_limit: int = int(os.environ.get("RL_MAX_CONCURRENT", "4"))
_rl_inflight: int = 0
_rl_cond: asyncio.Condition = asyncio.Condition()


def get_rl_concurrency() -> dict:
    """Snapshot of the RL concurrency gate — safe to call from anywhere."""
    return {"limit": _rl_limit, "inflight": _rl_inflight}


async def set_rl_concurrency(n: int) -> dict:
    """Change the RL concurrency limit at runtime.

    ``n`` must be ≥ 0.  n=0 stops new RL admissions (existing runs still
    complete).  The call wakes waiters so raises take effect immediately.
    Returns the updated snapshot.
    """
    global _rl_limit
    if n < 0:
        raise ValueError(f"RL concurrency must be >= 0, got {n}")
    async with _rl_cond:
        _rl_limit = int(n)
        _rl_cond.notify_all()  # raises let queued waiters proceed
    await aemit(Event("RLConcurrencyUpdated", {
        "limit": _rl_limit, "inflight": _rl_inflight,
    }))
    return get_rl_concurrency()


class _RLGate:
    """Async context manager: acquire one slot under the dynamic RL limit.

    Blocks when ``_rl_inflight >= _rl_limit``; re-checks on every notify so
    a raise in ``_rl_limit`` unblocks immediately.  Decrements on exit and
    notifies one waiter.
    """
    async def __aenter__(self):
        global _rl_inflight
        async with _rl_cond:
            while _rl_inflight >= _rl_limit:
                await _rl_cond.wait()
            _rl_inflight += 1
        return self

    async def __aexit__(self, *exc):
        global _rl_inflight
        async with _rl_cond:
            _rl_inflight = max(0, _rl_inflight - 1)
            _rl_cond.notify()
        return False


def _is_rl_payload(payload: dict) -> bool:
    """True when a queue payload comes from the RL generator.

    Matches both the submission-time tag (``_source=rl_generator``) and the
    session-name fallback (``rl:round-N:...``) in case the tag was stripped
    upstream.
    """
    if (payload.get("_source") or "").strip() == "rl_generator":
        return True
    session = payload.get("session") or ""
    if isinstance(session, str) and session.startswith("rl:"):
        return True
    return False


_DEFAULT_DISPATCH_CAP = int(
    os.environ.get("DORIAN_DISPATCH_CAP", str((os.cpu_count() or 8) * 2))
)


async def get_elastic_limit(client=None, multiplier: float = 2.0):
    """Capacity for concurrently in-flight pipeline runs.

    Rust runner path (default): caller passes ``client=None`` and the
    cap is the env-driven ``DORIAN_DISPATCH_CAP`` (defaults to
    ``cpu_count() * 2``). The Rust runner enforces its own per-pipeline
    parallelism — this gate just bounds how many pipelines can be
    actively dispatched at once.

    Legacy Dask path: the Dask client's ``scheduler_info`` reports
    worker thread counts; capacity is ``total_threads * multiplier``.
    Falls back to ``DORIAN_DISPATCH_CAP`` on errors so a transient
    scheduler hiccup doesn't stall the queue.
    """
    if client is None:
        return _DEFAULT_DISPATCH_CAP
    try:
        status = client.scheduler_info()
        workers = status.get('workers', {})
        total_threads = sum(w.get('nthreads', 0) for w in workers.values())
        if total_threads == 0:
            return 5
        return int(total_threads * multiplier)
    except Exception:
        return _DEFAULT_DISPATCH_CAP


# ── Time estimation from observability ────────────────────────────────────────

def _estimate_pipeline_duration() -> float:
    """Estimate pipeline duration from recent observability data.

    Uses the median of completed pipeline durations from the last 30 minutes.
    Falls back to 30s if no data is available.
    """
    try:
        from dorian.observability.collector import collector
        records = collector.get_pipeline_stats(since_s=1800)
        durations = [
            r["duration_s"]
            for r in records
            if r["status"] == "completed" and r["duration_s"] is not None
        ]
        if not durations:
            return 30.0
        durations.sort()
        mid = len(durations) // 2
        if len(durations) % 2 == 0:
            return (durations[mid - 1] + durations[mid]) / 2
        return durations[mid]
    except Exception:
        return 30.0


async def get_queue_status(uid: str, session: str) -> dict:
    """Return the current queue position and ETA for this user's session.

    Returns::

        {
            "queued": True/False,
            "position": 3,          # 1-based position (0 = not queued)
            "ahead": 2,             # number of tasks ahead
            "estimated_wait_s": 60, # estimated wall-clock seconds
            "queue_depth": 5,       # total tasks in queue
        }
    """
    try:
        # Get all tasks in the queue, sorted by score (lowest first = highest priority)
        tasks = await aioredis.zrange(TASK_QUEUE_KEY, 0, -1, withscores=True)

        if not tasks:
            return {
                "queued": False,
                "position": 0,
                "ahead": 0,
                "estimated_wait_s": 0,
                "queue_depth": 0,
            }

        queue_depth = len(tasks)

        # Find this user/session in the queue
        user_position = 0
        for i, (data, _score) in enumerate(tasks):
            try:
                payload = json.loads(data)
                if payload.get("uid") == uid and payload.get("session") == session:
                    user_position = i + 1  # 1-based
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if user_position == 0:
            return {
                "queued": False,
                "position": 0,
                "ahead": 0,
                "estimated_wait_s": 0,
                "queue_depth": queue_depth,
            }

        ahead = user_position - 1
        per_task = _estimate_pipeline_duration()

        return {
            "queued": True,
            "position": user_position,
            "ahead": ahead,
            "estimated_wait_s": round(ahead * per_task, 1),
            "queue_depth": queue_depth,
        }
    except Exception:
        return {
            "queued": False,
            "position": 0,
            "ahead": 0,
            "estimated_wait_s": 0,
            "queue_depth": 0,
        }


# ── Queue status broadcaster ─────────────────────────────────────────────────

_queue_notifier_task: asyncio.Task | None = None


async def start_queue_notifier() -> None:
    """Start the periodic queue-status broadcaster.

    Emits ``queue/status`` WS events every 3 seconds to all sessions that
    have a task in the queue, so users see real-time position updates.
    """
    global _queue_notifier_task
    _queue_notifier_task = asyncio.create_task(
        _queue_notifier_loop(), name="queue-notifier"
    )


async def stop_queue_notifier() -> None:
    """Stop the queue-status broadcaster."""
    global _queue_notifier_task
    if _queue_notifier_task is not None:
        _queue_notifier_task.cancel()
        try:
            await _queue_notifier_task
        except asyncio.CancelledError:
            pass
        _queue_notifier_task = None


async def _queue_notifier_loop() -> None:
    """Periodically send queue position updates to waiting users."""
    from dorian.infra.keys import RedisKeys, STREAM_MAXLEN

    while True:
        try:
            tasks = await aioredis.zrange(TASK_QUEUE_KEY, 0, -1, withscores=True)

            if tasks:
                per_task = _estimate_pipeline_duration()

                for i, (data, _score) in enumerate(tasks):
                    try:
                        payload = json.loads(data)
                        uid = payload.get("uid")
                        session = payload.get("session")
                        if not uid or not session:
                            continue

                        position = i + 1
                        ahead = i
                        estimated_wait_s = round(ahead * per_task, 1)

                        status_payload = json.dumps({
                            "type": "queue/status",
                            "value": json.dumps({
                                "queued": True,
                                "position": position,
                                "ahead": ahead,
                                "estimated_wait_s": estimated_wait_s,
                                "queue_depth": len(tasks),
                                "per_task_estimate_s": round(per_task, 1),
                            }),
                        })

                        stream_key = RedisKeys.stream(uid, session)
                        await aioredis.xadd(
                            stream_key,
                            {"data": status_payload},
                            maxlen=STREAM_MAXLEN,
                            approximate=True,
                        )
                    except Exception:
                        continue

            await asyncio.sleep(3)

        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)


# ── Bridge logic (main execution loop) ────────────────────────────────────────

async def bridge_logic(client=None, _redis=None):
    """
    Background loop that manages backpressure and pipeline submission.

    Pipelines are popped from the Redis sorted-set task queue and executed
    locally as async tasks.  handle_pipeline_execution() is an async
    coordinator that reads from expdb / Redis and then offloads the heavy
    pipeline DAG execution to a thread via asyncio.to_thread(run_pipeline, ...).

    Backpressure: cap on simultaneously-dispatched pipelines comes from
    ``get_elastic_limit``. Rust runner path (default): asyncio inflight
    count vs ``DORIAN_DISPATCH_CAP``. Legacy Dask path: Dask
    ``scheduler_info`` worker load.

    Signaling: uses an in-process asyncio.Event (_task_signal) instead of
    Redis pub/sub, avoiding ACL channel-permission requirements.
    """
    async def drain_queue():
        while True:
            current_limit = await get_elastic_limit(client)

            if client is not None:
                info = client.scheduler_info()
                inflight = sum(
                    len(w.get('processing', {}))
                    for w in info['workers'].values()
                )
            else:
                # Asyncio-native inflight count — the same per-uid run
                # tracking ``_run_safely`` maintains for the per-user
                # concurrency cap. Sum over uids gives the global
                # in-flight pipeline count without a Dask round-trip.
                inflight = sum(_active_runs.values())

            if inflight >= current_limit:
                await asyncio.sleep(1)
                continue

            task_raw = await aioredis.zpopmin(TASK_QUEUE_KEY)
            if not task_raw:
                break

            data, _priority = task_raw[0]
            payload = json.loads(data)

            # Track active runs for concurrency limits
            uid = payload.get("uid", "")
            asyncio.create_task(_run_safely(payload, uid))

    # Poll interval for the engine-driven path. Rust engines (xproduct,
    # automl) push directly into Redis ZADD and can't set the in-process
    # ``_task_signal``, so the bridge would otherwise sleep forever after
    # its startup drain. The interval is short enough that a steady
    # 16-30 trials/min producer keeps the consumer warm without polling
    # cost dominating idle CPU.
    _ENGINE_POLL_SECS = float(os.environ.get("DORIAN_BRIDGE_POLL_SECS", "2"))

    while True:
        try:
            await drain_queue()

            while True:
                # Race the in-process signal (user-driven submits) against
                # a short timeout so engine-driven enqueues that bypass
                # the signal still get drained.
                try:
                    await asyncio.wait_for(
                        _task_signal.wait(), timeout=_ENGINE_POLL_SECS,
                    )
                except asyncio.TimeoutError:
                    pass
                _task_signal.clear()
                await drain_queue()

        except asyncio.CancelledError:
            raise  # propagate shutdown cancellation
        except Exception as exc:
            await aemit(Event("BridgeError", {
                "source": "queue.bridge_logic",
                "error": str(exc),
                "trace": traceback.format_exc(),
            }))
            await asyncio.sleep(1)


class _NullCtx:
    """Awaitable-async context manager that does nothing — fast-path for
    non-RL payloads that should not pay the gate cost."""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return False


_NULL_CTX = _NullCtx()


async def _run_safely(payload: dict, uid: str = "") -> None:
    """Execute handle_pipeline_execution with top-level error handling.

    RL-generator payloads are gated behind ``_RLGate`` (dynamic limit, see
    ``set_rl_concurrency``) so at most N generated pipelines execute
    simultaneously.  User-initiated runs bypass this gate — the per-tier
    limit in ``submit_for_execution`` is the appropriate control for them.
    """
    rl_gate = _RLGate() if _is_rl_payload(payload) else _NULL_CTX

    async with rl_gate:
        async with _active_lock:
            _active_runs[uid] = _active_runs.get(uid, 0) + 1
        try:
            await handle_pipeline_execution(payload)
        except Exception as exc:
            await aemit(Event("PipelineExecutionCrash", {
                "source": "queue._run_safely",
                "error": str(exc),
                "trace": traceback.format_exc(),
                "payload": payload,
            }))
        finally:
            async with _active_lock:
                count = _active_runs.get(uid, 1) - 1
                if count <= 0:
                    _active_runs.pop(uid, None)
                else:
                    _active_runs[uid] = count


# ── Submission ────────────────────────────────────────────────────────────────

async def submit_for_execution(event: Event) -> None:
    """Push a pipeline execution request into the priority task queue.

    The ZADD score is derived from the user's tier:
        enterprise: -40, priority: -30, standard: -20, free: -10

    Within the same tier, a fractional timestamp tiebreaker preserves
    submission order (earlier submissions run first).
    """
    uid = event.data.get("uid", "")
    session = event.data.get("session", "")

    # Look up user tier for priority scoring
    user_tier = await get_user_tier(uid)
    tier_cfg = get_tier_config(user_tier)
    base_score = tier_cfg.queue_priority

    # Check concurrency limit for this tier
    async with _active_lock:
        current_active = _active_runs.get(uid, 0)
    if current_active >= tier_cfg.max_concurrent_pipelines:
        from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
        # Notify user they're at their concurrent limit
        limit_payload = json.dumps({
            "type": "queue/concurrency-limit",
            "value": json.dumps({
                "current": current_active,
                "max": tier_cfg.max_concurrent_pipelines,
                "tier": user_tier,
                "tier_label": tier_cfg.label,
            }),
        })
        stream_key = RedisKeys.stream(uid, session)
        await aioredis.xadd(
            stream_key,
            {"data": limit_payload},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        # Still enqueue — it will run when a slot opens
        pass

    # Fractional tiebreaker: lower (earlier) timestamp = higher priority within tier.
    # Normalize to a 0-1 range so it doesn't overpower the tier gap (10 points each).
    tiebreaker = (time.time() % 86400) / 86400  # fraction of day
    score = base_score + tiebreaker

    payload_data = {**event.data, "_tier": user_tier, "_submit_ts": time.time()}

    await aemit(Event(type="PipelineQueued", data=event.data))
    await aioredis.zadd(TASK_QUEUE_KEY, {json.dumps(payload_data): score})
    _task_signal.set()

    # Immediately send queue status to the user
    queue_depth = await aioredis.zcard(TASK_QUEUE_KEY)
    if queue_depth > 1:
        status = await get_queue_status(uid, session)
        if status["queued"]:
            from dorian.infra.keys import RedisKeys, STREAM_MAXLEN
            status_payload = json.dumps({
                "type": "queue/status",
                "value": json.dumps(status),
            })
            stream_key = RedisKeys.stream(uid, session)
            await aioredis.xadd(
                stream_key,
                {"data": status_payload},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )


async def submit_background(
    payload: dict | None = None,
    *,
    uid: str = "system",
    session: str = "",
    pipeline_id: str = "",
    dataset_id: str = "",
    source: str = "background",
) -> None:
    """Submit a background-priority pipeline execution.

    Background tasks have the lowest priority (``Priority.BACKGROUND = -1``)
    and are only popped from the queue after all USER and SYSTEM tasks have
    been processed.  The payload shape matches what ``handle_pipeline_execution``
    expects so background and foreground pipelines share the same execution
    path.

    Can be called in two ways:

    1. **Direct payload** (legacy)::

           await submit_background({"uid": "...", "session": "...", "pipelineId": "..."})

    2. **Structured** (preferred for RL generator / system tasks)::

           await submit_background(
               uid="system",
               session="rl:batch-42",
               pipeline_id="6839...",
               source="rl_generator",
           )
    """
    if payload is None:
        payload = {
            "uid": uid,
            "session": session,
            "pipelineId": pipeline_id,
            "datasetId": dataset_id,
            "_source": source,
            "_tier": "system",
            "_submit_ts": time.time(),
        }

    await aioredis.zadd(TASK_QUEUE_KEY, {json.dumps(payload): float(Priority.BACKGROUND)})
    _task_signal.set()
    await aemit(Event("BackgroundTaskQueued", {
        "source": payload.get("_source", source),
        "pipeline_id": payload.get("pipelineId", pipeline_id),
        "session": payload.get("session", session),
    }))
