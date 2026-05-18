"""
dorian/exec/worker.py
---------------------
Execution-engine worker loop.

A worker consumes jobs from the ``exec:jobs`` Redis stream via a shared
consumer group and runs the corresponding Python function. On success
it writes the result blob and emits a ``{Kind}Completed`` event on the
event bus (via direct XADD, same transport as every other event).

Concurrency model:

  * Each worker process runs ``run_forever()`` which schedules N
    concurrent job-handling tasks (``DORIAN_EXEC_CONCURRENCY``,
    default 4). Each task pulls one job at a time, runs it, ACKs.
  * Multiple processes can share the same group — Redis distributes
    entries across consumers automatically.
  * A job that raises is ACKed anyway (poison-event policy — the
    exception is recorded in the completion event's ``error`` field
    so callers see the failure without redelivery loops).

Crash recovery: XPENDING-replay is NOT implemented in this first
version. Unfinished jobs from a previous run-cycle are visible via
``XPENDING exec:jobs exec-workers`` and can be manually XCLAIMed.
Automating that is the next reliability task; left out of this
commit so the first cut is easy to review.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from dorian.exec import claimer as _claimer
from dorian.exec import registry as _registry

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_JOBS_STREAM = os.environ.get("DORIAN_EXEC_JOBS_STREAM", "exec:jobs")
_GROUP = os.environ.get("DORIAN_EXEC_GROUP", "exec-workers")
_EVENTS_STREAM_BG = os.environ.get("DORIAN_EVENTBUS_STREAM_BG", "events:bg")
_EVENTS_STREAM_USER = os.environ.get("DORIAN_EVENTBUS_STREAM_USER", "events:user")
_RESULT_KEY_PREFIX = os.environ.get("DORIAN_EXEC_RESULT_PREFIX", "exec:result")
_RESULT_TTL_S = int(os.environ.get("DORIAN_EXEC_RESULT_TTL_S", "3600"))
_CONCURRENCY = int(os.environ.get("DORIAN_EXEC_CONCURRENCY", "4"))
_BATCH = int(os.environ.get("DORIAN_EXEC_BATCH", "4"))
_BLOCK_MS = int(os.environ.get("DORIAN_EXEC_BLOCK_MS", "5000"))
_EVENTS_STREAM_MAXLEN = int(os.environ.get("DORIAN_EVENTBUS_STREAM_MAXLEN", "100000"))


@dataclass
class Stats:
    received: int = 0
    dispatched: int = 0
    succeeded: int = 0
    failed: int = 0
    unknown_kind: int = 0
    # Entries recovered from crashed consumers via XAUTOCLAIM.
    reclaimed: int = 0
    # Entries moved to the dead-letter stream after exceeding the
    # delivery-count threshold — ``_MAX_DELIVERIES`` in claimer.py.
    dead_lettered: int = 0
    last_error: str = ""
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class Worker:
    """One worker process. Run ``await worker.run()`` after ``start()``.

    The public surface is intentionally small; most tuning is via env
    vars so deployments can reconfigure without a code change.
    """

    def __init__(self, redis_client, *, name: str = ""):
        self.redis = redis_client
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.stats = Stats()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_group(self) -> None:
        """Create the consumer group on the jobs stream. Idempotent."""
        try:
            await self.redis.xgroup_create(_JOBS_STREAM, _GROUP, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" in str(exc):
                return
            _log.warning("[exec] xgroup_create failed: %s", exc)

    async def run(self) -> None:
        """Main loop — block until ``stop()`` is called."""
        await self._ensure_group()
        _log.info(
            "[exec] worker %s starting group=%s stream=%s concurrency=%d",
            self.name, _GROUP, _JOBS_STREAM, _CONCURRENCY,
        )
        # One consume task per concurrency slot. They share the stream
        # and the consumer group; Redis distributes entries fairly.
        tasks = [
            asyncio.create_task(self._consume_slot(i), name=f"exec-slot-{i}")
            for i in range(_CONCURRENCY)
        ]
        # Single claimer task — it picks up stuck entries from crashed
        # consumers and routes them through the normal dispatch path.
        # Dead-letter callback bumps our Stats so the counter surfaces
        # in the worker's own observability without a separate scrape
        # of the DLQ stream.
        claim_task = asyncio.create_task(
            _claimer.run_claimer(
                self.redis, self.name, self._on_claim, self._on_deadletter,
            ),
            name="exec-claimer",
        )
        tasks.append(claim_task)

        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _log.info("[exec] worker %s stopped", self.name)

    async def _on_claim(self, entry_id: Any, fields: Any) -> None:
        """Dispatch a reclaimed entry through the normal handler path.

        Counted separately so operators can distinguish fresh traffic
        from recovered-from-crash traffic.
        """
        self.stats.reclaimed += 1
        try:
            await self._handle_one(entry_id, fields)
        finally:
            await self._safe_xack(entry_id)

    async def _on_deadletter(self, entry_id: Any, fields: Any, times: int) -> None:
        """Invoked by the claimer after a poison entry is copied to
        the DLQ and XACKed. Only bumps the counter — the actual DLQ
        write and XACK already happened in the claimer."""
        self.stats.dead_lettered += 1

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Consume + dispatch
    # ------------------------------------------------------------------

    async def _consume_slot(self, slot: int) -> None:
        consumer = f"{self.name}-{slot}"
        while not self._stop.is_set():
            try:
                resp = await self.redis.xreadgroup(
                    groupname=_GROUP,
                    consumername=consumer,
                    streams={_JOBS_STREAM: ">"},
                    count=_BATCH,
                    block=_BLOCK_MS,
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.stats.last_error = f"XREADGROUP: {exc}"
                _log.warning("[exec] %s read failed: %s", consumer, exc)
                await asyncio.sleep(1.0)
                continue
            if not resp:
                continue
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    self.stats.received += 1
                    try:
                        await self._handle_one(entry_id, fields)
                    except Exception:
                        # Absolute last-resort — _handle_one already
                        # swallows its own exceptions and ACKs. This
                        # protects the consume loop from bugs in the
                        # handling code itself.
                        _log.exception("[exec] unhandled in _handle_one")
                    finally:
                        # Always ACK — poison job must never redeliver.
                        await self._safe_xack(entry_id)

    async def _handle_one(self, entry_id: Any, fields: Any) -> None:
        kind, inputs, job_id, submitted_at = _decode_job(fields)
        if not kind:
            self.stats.failed += 1
            self.stats.last_error = "missing kind"
            return
        fn = _registry.get(kind)
        if fn is None:
            self.stats.unknown_kind += 1
            self.stats.last_error = f"unknown kind: {kind}"
            _log.warning("[exec] unknown kind=%s job_id=%s", kind, job_id)
            await self._emit_completed(
                kind=kind, job_id=job_id, inputs=inputs,
                error=f"unknown kind: {kind}", result=None,
                submitted_at=submitted_at,
            )
            return

        self.stats.dispatched += 1
        started = time.time()
        error_msg: str | None = None
        result: dict[str, Any] | None = None
        try:
            result = await fn(inputs, job_id=job_id) or {}
            if not isinstance(result, dict):
                # Normalise non-dict return so the completion event
                # schema stays uniform.
                result = {"value": result}
            self.stats.succeeded += 1
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            self.stats.failed += 1
            self.stats.last_error = error_msg
            _log.exception("[exec] handler %s failed job=%s", kind, job_id)

        elapsed = time.time() - started

        # Store result blob with TTL so downstream consumers can fetch
        # it by job_id without re-running. Failures also persist (under
        # an ``error`` key) so debuggers can inspect.
        blob = json.dumps(
            {
                "job_id": job_id,
                "kind": kind,
                "result": result,
                "error": error_msg,
                "elapsed_s": round(elapsed, 3),
                "submitted_at": submitted_at,
                "completed_at": time.time(),
            },
            default=_json_default,
        )
        try:
            await self.redis.set(
                f"{_RESULT_KEY_PREFIX}:{job_id}", blob, ex=_RESULT_TTL_S,
            )
        except Exception as exc:
            _log.warning("[exec] store result failed job=%s: %s", job_id, exc)

        await self._emit_completed(
            kind=kind, job_id=job_id, inputs=inputs,
            error=error_msg, result=result,
            submitted_at=submitted_at, elapsed_s=elapsed,
        )

    async def _safe_xack(self, entry_id: Any) -> None:
        try:
            await self.redis.xack(_JOBS_STREAM, _GROUP, entry_id)
        except Exception as exc:
            _log.warning("[exec] XACK failed: %s", exc)

    # ------------------------------------------------------------------
    # Completion event emit (direct XADD to the bg lane — same transport
    # the rest of the system uses)
    # ------------------------------------------------------------------

    async def _emit_completed(
        self, *, kind: str, job_id: str, inputs: dict[str, Any],
        error: str | None, result: dict[str, Any] | None,
        submitted_at: float, elapsed_s: float = 0.0,
    ) -> None:
        # Event type follows the existing PascalCase past-tense rule.
        # kind "dq_check:missing_values" → "DQCheckMissingValuesCompleted".
        event_type = _completion_event_name(kind)
        # ``completed_at`` is carried in the payload so the Go-side
        # completion handler's compare-and-swap logic can skip
        # overwriting a NEWER cached result with an older one — the
        # TOCTOU window we flagged post-phase-L.
        completed_at = time.time()
        payload = {
            "job_id": job_id,
            "kind": kind,
            "inputs": inputs,
            "result": result,
            "error": error,
            "elapsed_s": round(elapsed_s, 3),
            "submitted_at": submitted_at,
            "completed_at": completed_at,
        }
        uid = str(inputs.get("uid") or "")
        session = str(inputs.get("session") or "")
        # Completion events are routed on the bg lane — callers that
        # need them on user-lane can pass ``lane: "user"`` in inputs.
        lane = "user" if str(inputs.get("lane") or "").lower() == "user" else "bg"
        stream = _EVENTS_STREAM_USER if lane == "user" else _EVENTS_STREAM_BG

        fields = {
            "type": event_type,
            "payload": json.dumps(payload, default=_json_default),
        }
        if uid:
            fields["uid"] = uid
        if session:
            fields["session"] = session
        fields["ts"] = repr(time.time())

        try:
            await self.redis.xadd(
                stream, fields,
                maxlen=_EVENTS_STREAM_MAXLEN, approximate=True,
            )
        except Exception as exc:
            _log.warning(
                "[exec] emit %s failed job=%s: %s", event_type, job_id, exc,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_job(fields: Any) -> tuple[str, dict[str, Any], str, float]:
    """Unpack the four fields the submitter puts on the stream.

    We tolerate both ``bytes`` and ``str`` representations of keys and
    values — aioredis behaviour depends on ``decode_responses``.
    """
    def _s(v) -> str:
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
        return str(v)

    flat: dict[str, str] = {}
    if isinstance(fields, dict):
        iter_items = fields.items()
    else:
        iter_items = fields
    for k, v in iter_items:
        flat[_s(k)] = _s(v)

    kind = flat.get("kind", "")
    job_id = flat.get("job_id", "")
    try:
        submitted_at = float(flat.get("submitted_at", "0") or "0")
    except ValueError:
        submitted_at = 0.0
    raw_inputs = flat.get("inputs", "")
    inputs: dict[str, Any] = {}
    if raw_inputs:
        try:
            parsed = json.loads(raw_inputs)
            inputs = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            inputs = {"_raw": raw_inputs}
    return kind, inputs, job_id, submitted_at


def _completion_event_name(kind: str) -> str:
    """``dq_check:missing_values`` → ``DQCheckMissingValuesCompleted``."""
    parts: list[str] = []
    for seg in kind.replace(":", "_").split("_"):
        if not seg:
            continue
        # Short tokens kept fully uppercase so the event name reads
        # naturally (avoid "Dq", "Kb", "Rl"). Covers the common
        # acronyms we've registered or likely will; extend when a new
        # kind embeds an acronym not already in the set.
        if seg.lower() in _UPPERCASE_TOKENS:
            parts.append(seg.upper())
        else:
            parts.append(seg[:1].upper() + seg[1:].lower())
    return "".join(parts) + "Completed"


_UPPERCASE_TOKENS = frozenset({
    # Computing / system
    "api", "cli", "cpu", "css", "db", "gpu", "html", "http", "id",
    "io", "json", "jsonl", "ram", "rpc", "sql", "ssl", "tcp", "tls",
    "udp", "url", "uri", "ws", "wss", "xml", "yaml",
    # ML / Dorian
    "ai", "dag", "dq", "fn", "kb", "llm", "ml", "nlp", "rl", "ui",
    # Data / file formats
    "csv", "tsv", "pdf", "png", "jpg",
})


def _json_default(o: Any) -> Any:
    """Fallback JSON encoder — same shape as eventbus_shadow's, kept
    local to avoid the cross-module import."""
    try:
        import datetime
        if isinstance(o, (datetime.datetime, datetime.date)):
            return o.isoformat()
    except Exception:
        pass
    return str(o)


# ---------------------------------------------------------------------------
# Top-level entrypoint for the container
# ---------------------------------------------------------------------------

async def run_forever() -> None:
    """Bind to Redis, import all registered job kinds, serve until killed.

    Intentionally does NOT import ``backend.envs`` — that module drags
    in the Dask cluster, Neo4j driver, and a handful of
    other infrastructure the exec worker doesn't need. Workers stay
    lean: one aioredis connection, import the checks registry, run.
    """
    logging.basicConfig(
        level=os.environ.get("DORIAN_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Importing this package triggers @register decorators for all
    # built-in kinds. Third-party / experimental kinds live under
    # their own module and must be imported separately.
    import dorian.exec.checks  # noqa: F401
    # dq_check:profile_and_quality -- the 70-node Dask graph that used
    # to run inline in the backend's DataExists handler (~10s inline
    # block). Pulls in pandas + dask lazily inside the handler body.
    import dorian.exec.profile  # noqa: F401
    # objective:validate — user-defined ranking objective compile check.
    # Submitted by the rust ``RankingObjectiveAdded`` handler; the rust
    # completion handler picks the result up off ``ObjectiveValidateCompleted``.
    import dorian.exec.objective  # noqa: F401
    # eval_procedure:validate — user-defined evaluation procedure compile
    # check. Submitted by the rust ``EvaluationProcedureAdded`` handler;
    # ``EvalProcedureValidateCompleted`` event triggers the rust WS xadd.
    import dorian.exec.eval_procedure  # noqa: F401
    # post:* — wrappers for what used to be event-bus handlers in
    # ``dorian/event/handlers/*``. Rust now subscribes to the
    # original events and submits a ``post:NAME`` job per event;
    # the worker pops + runs the wrapper, which calls the existing
    # python handler body. Each entry here is a former
    # ``subscribe(...)`` line, retired from the python event-bus and
    # restored over the job-submit interface.
    import dorian.exec.post_handlers  # noqa: F401

    # Build a local aioredis client rather than importing the shared
    # one from backend.envs. Env overrides:
    #   DORIAN_EXEC_REDIS_URL — explicit URL (wins if set)
    #   DORIAN_REDIS_URL      — shared URL used by other services
    # Both fall back to localhost for dev convenience.
    from redis.asyncio import Redis as _Redis, from_url as _from_url
    url = os.environ.get("DORIAN_EXEC_REDIS_URL") or os.environ.get("DORIAN_REDIS_URL")
    if url:
        redis_client = _from_url(url, decode_responses=True)
    else:
        # Fall back to the component-wise Redis config that backend.envs
        # already speaks. Don't import the whole module though — cheap
        # inline read via Dynaconf.
        from backend.config import config as _cfg  # small, no Dask
        redis_client = _Redis(
            host=_cfg.redis.host,
            port=_cfg.redis.port,
            username=str(getattr(_cfg.redis, "user", "") or "") or None,
            password=str(getattr(_cfg.redis, "password", "") or "") or None,
            decode_responses=True,
        )
    # Block until Redis is actually reachable. Without this, the
    # consume loop hits a DNS-resolution warning every second until
    # the network catches up — ugly and masks real Redis outages.
    # aardvark-dns (podman) sometimes lags behind the container's
    # network setup; in docker compose this rarely fires because
    # depends_on:condition:service_healthy gates us on Redis's own
    # healthcheck.
    for attempt in range(60):
        try:
            await redis_client.ping()
            if attempt > 0:
                _log.info("[exec] Redis reachable after %d attempts", attempt + 1)
            break
        except Exception as exc:
            if attempt == 0:
                _log.info("[exec] waiting for Redis: %s", exc)
            await asyncio.sleep(1.0)
    else:
        _log.error("[exec] Redis unreachable after 60s; starting anyway")

    worker = Worker(redis_client)

    loop = asyncio.get_running_loop()
    stopper_called = False

    def _request_stop(*_a):
        nonlocal stopper_called
        if stopper_called:
            return
        stopper_called = True
        worker.stop()

    # SIGINT/SIGTERM → graceful shutdown. Not using signal module's
    # default because we want to set the worker's stop event, not just
    # raise KeyboardInterrupt mid-XREADGROUP.
    try:
        import signal
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, _request_stop)
    except (NotImplementedError, AttributeError):
        # Windows / restricted environments — fall back to default.
        pass

    await worker.run()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
