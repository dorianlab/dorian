import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)

# Native-crash diagnostics: when a Python/C extension or the in-process Dask
# scheduler segfaults (SIGSEGV / SIGBUS / SIGFPE / SIGABRT), faulthandler
# writes a C-level stack for every thread to stderr before the process dies.
# The resulting log lines name the exact library (pyo3, pickle, dask comm,
# numpy, etc.) that triggered the fault.  Must be installed before any native
# code is imported, so this block sits above every other import.
import faulthandler
import sys
faulthandler.enable(file=sys.stderr, all_threads=True)

import asyncio
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,  # override any handlers uvicorn already installed
)
# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("dask").setLevel(logging.WARNING)
logging.getLogger("distributed").setLevel(logging.WARNING)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from dorian.api.routes import file, observability, vault, contact, admin, platform, catalog
# catalog routes (operators/tasks/objectives/evals/operator-params/catalog)
# ported to engine/gateway/src/catalog.rs — KB-snapshot reads, no python.
# session routes ported to engine/gateway/src/session.rs.
from dorian.mcp import router as mcp
from dorian.api.websocket import websocket_endpoint
from dorian.event.registry import register_event_handlers
from dorian.event.service_bridge import start_service_bridge

from backend.config import config
from backend.envs import executor, cluster, aioredis, redis, close_pg_pool
from backend.hmac_auth import HMACAuthMiddleware
from backend.events import start_workers, stop_workers
from backend.queue import bridge_logic
from dorian.observability import start_sampler, stop_sampler
from dorian.observability.reaper import start_reaper, stop_reaper
from dorian.experiment.store import init_experiment_store, shutdown_experiment_store
from dorian.knowledge.queries import get_all_kb_operator_params
from dorian.event.handlers.session import warm_catalog_cache
from dorian.vault.storage import recover_vault_from_store

shutdown = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_workers()
    await start_sampler(interval=5.0)
    await start_reaper()  # clean up stale result pickle files hourly

    if executor is not None:
        print("Dask:", executor.dashboard_link)

    bridge = asyncio.create_task(
        bridge_logic(executor, aioredis)
    )

    # Queue position broadcaster — sends real-time position/ETA updates
    # to users waiting in the pipeline execution queue.
    from backend.queue import start_queue_notifier, stop_queue_notifier
    await start_queue_notifier()

    # Subscribe to events:service:* — inbound WS events from the Go gateway.
    # Without this, the Go WS proxy publishes to a namespace nothing in
    # Python listens on, and seed_session never fires.
    service_bridge_task = await start_service_bridge(aioredis)

    # Experiment Store — Postgres schema + in-memory similarity indices
    await init_experiment_store()

    # Pre-warm KB caches in the background so we don't block lifespan.
    # Both steps are CPU-heavy (Neo4j round-trips + Python aggregation) and
    # take several minutes on a cold image. seed_session calls
    # _catalog_cache.ensure_built() which awaits the same asyncio.Lock, so
    # the first WS client transparently waits if the warm-up is still in
    # flight; subsequent clients get the pre-built cache for free.
    async def _ensure_kb_snapshot():
        """Generate the rust KB snapshot if it's missing.

        The snapshot is the in-memory KB the rust path reads. It's
        produced from the same Neo4j seed via ``scripts.export_kb_snapshot``
        — equivalent data, different consumer. We only build it when
        missing on disk; operators rerun the script manually after a
        KB re-seed if they need a fresh snapshot. Failure is non-fatal
        — ``_kb_rust()`` falls back to Bolt when the snapshot is
        unloadable.
        """
        from pathlib import Path
        snap_env = os.environ.get("DORIAN_KB_SNAPSHOT", "/app/volumes/kb_snapshot.json")
        snap_path = Path(snap_env)
        # Regenerate when the snapshot is older than the rust binary
        # that produces it. ``dorian_native``'s .so mtime advances on
        # every wheel reinstall; an older snapshot means the rust
        # builder logic has changed since the cached snapshot was
        # written and the snapshot must be rebuilt or it'll keep
        # serving stale chains (e.g. the 2026-04-28 missing-__init__
        # bug where the snapshot was cached against the pre-fix
        # walker). File-presence alone is too weak a freshness check.
        if snap_path.is_file() and snap_path.stat().st_size > 0:
            try:
                import dorian_native as _dn
                native_mtime = Path(_dn.__file__).stat().st_mtime
                if snap_path.stat().st_mtime >= native_mtime:
                    return
            except Exception:
                # If we can't locate dorian_native, fall back to
                # honoring the cached file rather than infinite-loop
                # the bootstrap.
                return
        try:
            from scripts.export_kb_snapshot import _emit_snapshot
            import json as _json
            snap = await asyncio.to_thread(_emit_snapshot)
            snap_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                snap_path.write_text, _json.dumps(snap, indent=2, sort_keys=True)
            )
            from backend.events import aemit as _aemit, Event as _Event
            await _aemit(_Event("KBSnapshotGenerated", {
                "path": str(snap_path),
                "operators": len(snap.get("operators", {})),
                "interfaces": len(snap.get("interfaces", {})),
            }))
        except Exception as exc:
            from backend.events import aemit as _aemit, Event as _Event
            await _aemit(_Event("KBSnapshotFailed", {"error": repr(exc)}))

    async def _warm_kb_caches_background():
        try:
            await _ensure_kb_snapshot()
            await asyncio.to_thread(get_all_kb_operator_params)
            await warm_catalog_cache()
            # Pre-build the KB rule index — single ``RuleIndex`` of
            # every rewrite in ``expdb.rewrites`` so the AI Debugger
            # and MCP can do O(1) "what rewrites apply to this
            # operator" lookups instead of compiling rules per-call.
            try:
                from dorian.pipeline.mitigation_rewrites import (
                    get_kb_rule_index,
                )
                idx = await get_kb_rule_index()
                from backend.events import aemit as _aemit, Event as _Event
                if idx is not None:
                    await _aemit(_Event("KbRuleIndexBuilt", {
                        "rule_count": len(idx),
                    }))
            except Exception as exc:
                from backend.events import aemit as _aemit, Event as _Event
                await _aemit(_Event("KbRuleIndexFailed", {"error": repr(exc)}))
        except Exception as exc:
            from backend.events import aemit as _aemit, Event as _Event
            await _aemit(_Event("KBWarmupFailed", {"error": repr(exc)}))

    kb_warmup_task = asyncio.create_task(
        _warm_kb_caches_background(), name="kb-warmup-background"
    )

    # Clean up any stale active connections from a previous crash.
    await aioredis.delete("dorian:active_connections")

    # Recover vault secrets that may have been lost from Redis
    n_recovered = await recover_vault_from_store()
    if n_recovered:
        print(f"Vault: recovered {n_recovered} secret(s) from docstore")

    # ── Periodic backup task ─────────────────────────────────────────────
    async def _periodic_backup():
        """Run a system backup every 6 hours."""
        from dorian.api.routes.admin import run_backup
        while True:
            try:
                await asyncio.sleep(6 * 60 * 60)  # 6 hours
                result = await run_backup(triggered_by="periodic")
                status = "ok" if result["ok"] else f"errors: {result['errors']}"
                print(f"Periodic backup: {status} → {result['path']}")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"Periodic backup failed: {exc}")

    periodic_backup_task = asyncio.create_task(_periodic_backup())

    # ── RL pipeline generator (background) ──────────────────────────────
    # Config-gated continuous pipeline generation.  When enabled, a
    # background `GenerationScheduler` loop generates pipelines for all
    # public datasets at BACKGROUND priority — user traffic always takes
    # precedence via the bridge queue.  Pipeline runs emit metrics via
    # `record_evaluation_batch`, which is what populates the leaderboard.
    scheduler_task = None
    scheduler_instance = None
    try:
        gen_enabled = bool(getattr(config.generation, "enabled", False))
    except Exception:
        gen_enabled = False

    # Environment-variable kill switch: overrides config so operators can
    # flip RL generation off without editing configs and redeploying. Useful
    # when the handler pool is saturated by RL traffic and user sessions are
    # getting starved (set RL_GENERATION_ENABLED=0 and restart — or set in
    # the compose file to persist).
    _rl_env = os.environ.get("RL_GENERATION_ENABLED", "").strip().lower()
    if _rl_env in ("0", "false", "off", "no"):
        gen_enabled = False
        print("RL generation scheduler: DISABLED via RL_GENERATION_ENABLED env")

    if gen_enabled:
        from dorian.pipeline.generation.scheduler import GenerationScheduler

        gen_cfg = config.generation
        scheduler_instance = GenerationScheduler(
            batch_size=int(getattr(gen_cfg, "batch_size", 10)),
            cooldown=float(getattr(gen_cfg, "cooldown", 30.0)),
            max_rounds=getattr(gen_cfg, "max_rounds", None),
            seed=getattr(gen_cfg, "seed", None),
        )

        async def _run_scheduler():
            try:
                await scheduler_instance.run()
            except asyncio.CancelledError:
                scheduler_instance.cancel()
                raise
            except Exception as exc:
                from backend.events import aemit as _aemit, Event as _Event
                await _aemit(_Event("GenerationSchedulerCrashed", {
                    "error": repr(exc),
                    "trace": __import__("traceback").format_exc(),
                }))

        scheduler_task = asyncio.create_task(
            _run_scheduler(), name="rl-generation-scheduler",
        )
        print("RL generation scheduler: ENABLED")
    else:
        print("RL generation scheduler: disabled (config.generation.enabled=false)")

    # ── Graceful shutdown listener ───────────────────────────────────────
    async def _shutdown_listener():
        """Wait for the shutdown event to be set (by admin endpoint) then
        raise SystemExit to trigger the lifespan teardown."""
        await shutdown.wait()
        import signal, os
        os.kill(os.getpid(), signal.SIGTERM)

    shutdown_listener_task = asyncio.create_task(_shutdown_listener())

    # ── Worker-pool watchdog (THREADED, not asyncio) ─────────────────────
    # Runs in a daemon OS thread so it survives a fully-stalled event
    # loop. An earlier asyncio-based version sat behind
    # ``await asyncio.sleep(...)`` — when the event-bus stall happens
    # because a handler blocks the loop (CPU-bound work without
    # yielding, threading.Lock contention pulled into a coroutine,
    # etc.), the watchdog's sleep was also pinned and the self-restart
    # never fired. Running the watchdog in a thread sidesteps that —
    # the OS scheduler keeps it alive even if the event loop is
    # entirely frozen.
    #
    # The watchdog polls ``backend.events.eventbus_healthy`` every 15s,
    # tracks how long it's been unhealthy, and ``os.kill(pid, SIGTERM)``
    # after ``SELF_RESTART_AFTER_S``. ``restart: on-failure:5`` in
    # docker-compose then recycles the container. SIGTERM lets uvicorn's
    # lifespan teardown run cleanly; if that itself stalls, the
    # ``stop_grace_period: 60s`` upgrades to SIGKILL.
    import threading as _threading_wd
    import time as _time_wd

    def _worker_pool_watchdog_thread():
        from backend.events import eventbus_healthy as _eb_healthy
        SELF_RESTART_AFTER_S = 90.0
        unhealthy_since: float | None = None
        notified_pending = False
        while True:
            _time_wd.sleep(15.0)
            try:
                ok, detail = _eb_healthy()
            except Exception:
                # Module imports / state still warming — skip this tick.
                continue
            if ok:
                unhealthy_since = None
                notified_pending = False
                continue
            now = _time_wd.monotonic()
            if unhealthy_since is None:
                unhealthy_since = now
                # Best-effort log — print directly because aemit may
                # be the thing that's stuck.
                print(
                    f"[watchdog] worker pool unhealthy: {detail}",
                    flush=True,
                )
                notified_pending = True
                continue
            stalled_for = now - unhealthy_since
            if not notified_pending and stalled_for > 30:
                print(
                    f"[watchdog] worker pool still unhealthy after "
                    f"{stalled_for:.0f}s: {detail}",
                    flush=True,
                )
            if stalled_for >= SELF_RESTART_AFTER_S:
                import signal as _signal, os as _os
                print(
                    f"[watchdog] worker pool stalled for "
                    f"{stalled_for:.0f}s — sending SIGTERM to recycle "
                    f"container (detail={detail})",
                    flush=True,
                )
                _os.kill(_os.getpid(), _signal.SIGTERM)
                # SIGTERM escalation: if uvicorn's lifespan shutdown
                # hangs (a real failure mode — stop_workers awaits
                # the same queue drain that stalled in the first
                # place), the process never exits and
                # ``restart: on-failure`` never triggers. Wait 30s
                # then SIGKILL outright. The OS reaps PID 1, podman
                # records a non-zero exit, restart policy recycles.
                _time_wd.sleep(30.0)
                print(
                    "[watchdog] SIGTERM didn't take effect within 30s — "
                    "escalating to SIGKILL",
                    flush=True,
                )
                try:
                    _os.kill(_os.getpid(), _signal.SIGKILL)
                except Exception:
                    pass
                return

    worker_pool_watchdog_thread = _threading_wd.Thread(
        target=_worker_pool_watchdog_thread,
        name="worker-pool-watchdog",
        daemon=True,
    )
    worker_pool_watchdog_thread.start()

    yield

    shutdown_listener_task.cancel()
    # Watchdog thread is a daemon — it dies with the process. No
    # explicit cancel needed (and we can't cleanly cancel a sleeping
    # OS thread without a flag, which the daemon-on-shutdown semantics
    # make unnecessary).

    if scheduler_task is not None:
        if scheduler_instance is not None:
            scheduler_instance.cancel()
        scheduler_task.cancel()

    periodic_backup_task.cancel()

    kb_warmup_task.cancel()

    # ── Shutdown backup ──────────────────────────────────────────────────
    try:
        from dorian.api.routes.admin import run_backup
        result = await run_backup(triggered_by="shutdown")
        status = "ok" if result["ok"] else f"errors: {result['errors']}"
        print(f"Shutdown backup: {status} → {result['path']}")
    except Exception as exc:
        print(f"Shutdown backup failed: {exc}")

    service_bridge_task.cancel()
    bridge.cancel()
    await stop_queue_notifier()
    await stop_reaper()
    await stop_sampler()
    await stop_workers()

    # Aggressively cancel all pending/running Dask tasks, then tear down the
    # client and cluster so background executor.get() calls don't block shutdown.
    # When DORIAN_USE_RUST_RUNNER=1 (default), no cluster was created — skip.
    if executor is not None:
        try:
            executor.cancel(list(executor.futures))
        except Exception:
            pass
        executor.close()
    if cluster is not None:
        cluster.close()

    await shutdown_experiment_store()
    await close_pg_pool()

    redis.close()
    await aioredis.close()


app = FastAPI(lifespan=lifespan)


# HMAC-SHA256 request signing — added first so CORS wraps it (Starlette
# middleware stack is LIFO: last-added = outermost).  This ensures CORS
# headers appear even on HMAC rejection responses.
app.add_middleware(HMACAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(getattr(config.urls, "cors_origins", [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ])),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-HMAC-Signature", "X-HMAC-Timestamp", "X-HMAC-Nonce"],
)


# Register the routers
# session router moved to engine/gateway.
app.include_router(file.router)
app.include_router(mcp.router)
app.include_router(observability.router)
app.include_router(vault.router)
app.include_router(contact.router)
app.include_router(catalog.router)
app.include_router(admin.router)
app.include_router(platform.router)

# Send message to the frontend when a new event is emitted
app.add_api_websocket_route("/ws", websocket_endpoint)


# /healthz — worker-pool liveness probe used by docker-compose's
# healthcheck. Returns 200 only when the python event-bus workers are
# actually processing items (or the queues are idle); 503 when the pool
# has stalled — the silent-degradation pattern documented in
# ``project_python_eventbus_workers_degrade.md`` where uvicorn keeps
# serving HTTP but local handlers (seed_session, slack_on_*) never run.
# Compose's restart-on-unhealthy then recycles the container instead of
# leaving a half-dead backend in rotation. Path is in
# ``backend.hmac_auth._EXEMPT_PREFIXES`` so the docker probe doesn't
# need to sign requests.
@app.get("/healthz")
async def healthz():
    from fastapi.responses import JSONResponse
    from backend.events import eventbus_healthy
    ok, detail = eventbus_healthy()
    return JSONResponse(
        {"ok": ok, **detail},
        status_code=200 if ok else 503,
    )


register_event_handlers()

# Serve tracer output files (PNGs, SVGs, logs) generated at execution time.
import pathlib
from fastapi.staticfiles import StaticFiles
_outputs_dir = pathlib.Path("outputs")
_outputs_dir.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(_outputs_dir)), name="outputs")
