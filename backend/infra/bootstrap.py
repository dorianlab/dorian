"""
backend/infra/bootstrap.py
--------------------------
One-shot provisioner + seeder. Runs the four formerly-separate
one-shot containers (db-init, seed-expdb, seed-neo4j, seed-rewrites)
sequentially in a single Python process so docker-compose only spawns
ONE container for the whole bootstrap phase.

Each step is idempotent; failures are fatal for that step. Steps after
a failure still run (non-blocking) so the logs show the full picture
of what broke.

Usage (docker-compose ``bootstrap`` service):

    uv run python -m backend.infra.bootstrap

Each step can be skipped via env:

    DORIAN_BOOTSTRAP_PROVISION=0
    DORIAN_BOOTSTRAP_SEED_TRIAL_CONFIGS=0
    DORIAN_BOOTSTRAP_SEED_KB=0
    DORIAN_BOOTSTRAP_SEED_REWRITES=0
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Awaitable, Callable

_log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


async def _step(name: str, fn: Callable[[], Awaitable[None] | None]) -> bool:
    """Run one bootstrap step. Returns True on success, False on failure.

    ``fn`` can be sync or async. Timing + outcome is logged with a
    prefix so the operator can scan the log.
    """
    print(f"==> bootstrap[{name}] starting", flush=True)
    t0 = time.time()
    try:
        out = fn()
        if asyncio.iscoroutine(out):
            await out
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"!! bootstrap[{name}] FAILED after {elapsed:.1f}s: {exc}", flush=True)
        _log.exception("bootstrap step %s failed", name)
        return False
    elapsed = time.time() - t0
    print(f"==> bootstrap[{name}] OK in {elapsed:.1f}s", flush=True)
    return True


async def _step_provision():
    # Import at call-time so failures don't abort the whole file.
    from backend.infra.provision import main as provision_main
    r = provision_main()
    if asyncio.iscoroutine(r):
        await r


async def _step_seed_trial_configs():
    # Idempotency guard — import_trial_configs uses ``insert_many``,
    # not upsert, so re-running the full seeder on a populated
    # ``pipelines`` collection would duplicate all 500 entries on
    # every restart.
    #
    # Two stores must be seeded: the document store (``expdb.pipelines``)
    # and the relational table (``Postgres.pipelines`` — BK-Tree backing).
    # If only the doc store is populated (legacy deployments that pre-date
    # the relational seed), reuse the already-seeded DAGs to fill the
    # relational table without duplicating the doc store.
    try:
        from backend.envs import expdb
        doc_count = await expdb.pipelines.count_documents({})
    except Exception as exc:
        print(
            f"-- bootstrap[seed-trial-configs] probe failed ({exc}); "
            "attempting seed anyway",
            flush=True,
        )
        doc_count = 0

    rel_count = 0
    try:
        from backend.envs import get_pg_pool
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rel_count = await conn.fetchval("SELECT COUNT(*) FROM pipelines")
    except Exception as exc:
        print(
            f"-- bootstrap[seed-trial-configs] relational probe failed "
            f"({exc}); will attempt relational backfill",
            flush=True,
        )

    if doc_count > 0 and rel_count > 0:
        print(
            f"-- bootstrap[seed-trial-configs] skipped "
            f"(doc={doc_count}, relational={rel_count})",
            flush=True,
        )
        return

    if doc_count > 0 and rel_count == 0:
        # Doc store populated, relational empty — backfill just the
        # relational table from existing docs, avoiding doc dupes.
        print(
            f"-- bootstrap[seed-trial-configs] relational backfill "
            f"(doc={doc_count}, relational=0)",
            flush=True,
        )
        try:
            from backend.envs import expdb
            from backend.infra.dbs.expdb.import_trial_configs import (
                _seed_relational_pipelines,
            )
            cursor = expdb.pipelines.find({})
            docs = await cursor.to_list(length=None)
            inserted = await _seed_relational_pipelines(docs)
            print(
                f"-- bootstrap[seed-trial-configs] backfilled {inserted} "
                "rows into Postgres.pipelines",
                flush=True,
            )
        except Exception as exc:
            print(
                f"!! bootstrap[seed-trial-configs] backfill FAILED: {exc}",
                flush=True,
            )
        return

    # Full seed (neither store populated).
    try:
        from backend.infra.dbs.expdb.import_trial_configs import main as fn
        r = fn()
        if asyncio.iscoroutine(r):
            await r
    except (ImportError, AttributeError):
        import runpy
        runpy.run_module(
            "backend.infra.dbs.expdb.import_trial_configs",
            run_name="__main__",
        )


async def _step_relational_schema():
    """Run the relational-schema migration before any service that
    queries the relational tables starts.

    The python backend's lifespan calls ``init_experiment_store``
    which in turn runs ``create_schema``, but that's too late for
    the rust ``dorian-engines`` container — it's brought up in
    Phase 4 of the deployment sequence alongside the python backend
    and starts polling ``evaluations`` immediately. Without this
    step, every fresh deploy logs `column "status" does not exist`
    until the python backend gets around to ALTER-TABLE-ing.

    Idempotent (CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD
    COLUMN IF NOT EXISTS).
    """
    from backend.envs import get_pg_pool
    from dorian.experiment.schema import create_schema

    pool = await get_pg_pool()
    await create_schema(pool)


async def _step_seed_kb():
    # KB seeding is now snapshot-only: rerun the io-crawler to
    # refresh ``volumes/io_crawler_extras.kb``, then build the
    # snapshot the rust runtime consumes. Neo4j was retired with
    # the rust parser port (see ``engine/optimizer/src/kb/builder.rs``).
    import os
    import sys
    from dorian.knowledge.io_crawler import crawl

    crawl()

    # Rebuild snapshot from sources via the rust builder. The
    # exporter's ``main`` reads sys.argv for ``--out`` etc.; pass
    # an explicit argv so bootstrap's own args don't leak in.
    out_path = os.environ.get(
        "DORIAN_KB_SNAPSHOT", "/app/volumes/kb_snapshot.json"
    )
    saved = sys.argv
    try:
        sys.argv = ["export_kb_snapshot", "--out", out_path]
        from scripts.export_kb_snapshot import main as export_main
        rc = export_main()
    finally:
        sys.argv = saved
    if rc != 0:
        raise RuntimeError(f"snapshot export failed with rc={rc}")


async def _step_seed_rewrites():
    try:
        from backend.infra.dbs.expdb.seed_rewrites import main as fn
        r = fn()
        if asyncio.iscoroutine(r):
            await r
    except (ImportError, AttributeError):
        import runpy
        runpy.run_path(
            os.path.join(
                os.path.dirname(__file__), "dbs", "expdb", "seed_rewrites.py",
            ),
            run_name="__main__",
        )


async def _step_seed_catalog():
    """Seed ranking objectives and evaluation procedures into Postgres.

    Runs *after* seed-kb so the KB snapshot is guaranteed to exist.
    Idempotent (ON CONFLICT DO NOTHING).
    """
    from backend.infra import _seed_objectives_and_evals
    from backend.db import get_pg_db
    db = await get_pg_db()
    await _seed_objectives_and_evals(db)


async def _step_seed_exception_patterns():
    """Upsert the canonical exception-pattern library into
    ``expdb.exception_patterns`` so the RL env's debugger reads
    patterns from a mutable DB collection rather than only from
    in-code seeds. See
    ``backend.infra.dbs.expdb.seed_exception_patterns``."""
    from backend.infra.dbs.expdb.seed_exception_patterns import main as fn
    r = fn()
    if asyncio.iscoroutine(r):
        await r


async def run_bootstrap() -> int:
    """Return exit code: 0 if every enabled step succeeded, 1 otherwise."""
    ok = True
    if _env_bool("DORIAN_BOOTSTRAP_PROVISION"):
        ok &= await _step("provision", _step_provision)
    else:
        print("-- bootstrap[provision] skipped (DORIAN_BOOTSTRAP_PROVISION=0)", flush=True)

    # Relational schema MUST land before any service that queries
    # ``evaluations`` / ``pipelines`` / ``datasets`` starts. Engines
    # in Phase 4 query these tables on first tick (~1s after start);
    # leaving the schema migration to the python backend's lifespan
    # races on every fresh deploy.
    if _env_bool("DORIAN_BOOTSTRAP_RELATIONAL_SCHEMA"):
        ok &= await _step("relational-schema", _step_relational_schema)
    else:
        print("-- bootstrap[relational-schema] skipped (DORIAN_BOOTSTRAP_RELATIONAL_SCHEMA=0)", flush=True)

    # Trial-config seeding (the 500 auto-sklearn pipelines) is
    # retired — the FLAML seeder daemon and the RL trainer are now
    # the canonical sources of pipelines. Keeping the trial-config
    # path here would wipe FLAML's contributions on every boot
    # (``import_trial_configs`` deletes everything before
    # re-inserting). Set ``DORIAN_BOOTSTRAP_SEED_TRIAL_CONFIGS=1``
    # to opt back in for one-off bring-up of an empty store.
    if _env_bool("DORIAN_BOOTSTRAP_SEED_TRIAL_CONFIGS", default=False):
        ok &= await _step("seed-trial-configs", _step_seed_trial_configs)
    else:
        print("-- bootstrap[seed-trial-configs] skipped (retired; use FLAML)", flush=True)

    if _env_bool("DORIAN_BOOTSTRAP_SEED_KB"):
        # FORCE_SEED mirrors the old ``seed-neo4j`` container's env.
        os.environ.setdefault("FORCE_SEED", "1")
        ok &= await _step("seed-kb", _step_seed_kb)
    else:
        print("-- bootstrap[seed-kb] skipped", flush=True)

    # Seed objectives/evals after KB snapshot is built (needs load_kb()).
    if _env_bool("DORIAN_BOOTSTRAP_SEED_CATALOG", default=True):
        ok &= await _step("seed-catalog", _step_seed_catalog)
    else:
        print("-- bootstrap[seed-catalog] skipped (DORIAN_BOOTSTRAP_SEED_CATALOG=0)", flush=True)

    if _env_bool("DORIAN_BOOTSTRAP_SEED_REWRITES"):
        ok &= await _step("seed-rewrites", _step_seed_rewrites)
    else:
        print("-- bootstrap[seed-rewrites] skipped", flush=True)

    if _env_bool("DORIAN_BOOTSTRAP_SEED_EXCEPTION_PATTERNS"):
        ok &= await _step(
            "seed-exception-patterns", _step_seed_exception_patterns
        )
    else:
        print("-- bootstrap[seed-exception-patterns] skipped", flush=True)

    return 0 if ok else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # See ``scripts/openml_loader.py`` for rationale — Dask Nannies +
    # the scheduler each emit Batched/CommClosedError tracebacks on
    # clean shutdown; those drown out the bootstrap-step progress.
    for _name in ("distributed.batched", "distributed.scheduler",
                  "distributed.nanny", "distributed.core"):
        logging.getLogger(_name).setLevel(logging.WARNING)
    rc = asyncio.run(run_bootstrap())
    sys.exit(rc)


if __name__ == "__main__":
    main()
