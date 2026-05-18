import asyncio
import os
import shutil
from pathlib import Path

import pendulum
from dynaconf import Dynaconf
import redis.asyncio as aioredis
# neo4j: retired with the rust KB snapshot port. The async driver
# was the only consumer and ``init_neo4j`` is now a no-op.

# --- Configuration Mapping ---
# Single source of truth: ``config/config.yaml``. See ``backend/config.py``
# for the canonical loader; this module mirrors the same contract so the
# infra-side scripts can be invoked without importing the FastAPI app.
base = Path(__file__).parents[2]
_config_file = base / 'config' / 'config.yaml'
if not _config_file.is_file():
    raise RuntimeError(
        f"missing {_config_file}; "
        f"run `cp config/config.yaml.example config/config.yaml` and "
        f"populate every required field before starting the stack"
    )
config = Dynaconf(settings_files=[str(_config_file)])
cfg = config[config.type]

SOURCE_DIR = Path(base / cfg.fs.data) # / 'containers'
BACKUP_DEST = Path(base / cfg.fs.backup)
DATABASES_TO_BACKUP = ["redis"]

# --- Utilities ---

async def wait_for_conn(name: str, func, retries: int = 2, delay: int = 5):
    """Retries a connection logic until the service is ready."""
    for i in range(retries):
        try:
            return await func()
        except Exception as e:
            if i == 0:
                print(f"[{pendulum.now()}] {name}: Waiting for service to stabilize...")
            else:
                print(f"[{pendulum.now()}] {name}: {e}")
            await asyncio.sleep(delay)
    print(f"!! {name}: Timeout after {retries} retries.")
    return None

# --- Database Provisioning ---

# List of document collections the application uses. Each gets its
# own ``doc_<collection>`` Postgres table plus partial expression
# indexes (created lazily by ``Collection._ensure_table``).
#
# NOTE on retired collections:
#   * ``sessions``, ``snippets``, ``rule_extraction_feedback`` — declared
#     by provisioning but currently unused in the codebase. Kept in the
#     list so the index set stays stable for future use.
COLLECTIONS: list[str] = [
    "pipelines", "sessions", "snippets", "extractions", "contact_submissions",
    "extraction_rule_versions", "rule_suggestions", "rule_extraction_feedback",
    "datasets", "rewrites", "vault_secrets", "feedback", "ranking_objectives",
    "evaluation_procedures", "onboarding", "generation_errors",
    "execution_error_instances", "rl_mitigation_attempts",
]


async def init_document_store():
    """Provision the Postgres-backed document store.

    Per-collection ``doc_<name>`` tables are created lazily by each
    ``Collection._ensure_table`` call. ``ensure_schema`` here is a
    one-shot rename for any leftover ``mongo_*`` tables from older
    deploys. Also installs the full set of partial expression indexes
    covering every access pattern the app uses, and seeds the
    ``rewrites`` collection.
    """
    # Import lazily: the facade imports backend.envs on first use, which
    # this script is already imported *from* in container bootstrap paths.
    from backend.db import get_pg_db
    from backend.db.pg_docstore import ensure_schema

    async def _logic():
        db = await get_pg_db()
        # ensure_schema already ran inside get_pg_db → Database._pool; explicit
        # call here keeps the provisioning output visible in logs.
        pool = await db._pool()
        await ensure_schema(pool)
        print("  - PG document store: schema ensured (per-collection doc_* tables).")

        # Indexes — one per (collection, keys) pair. Partial expression
        # indexes over the JSONB ``data`` column: cheap to maintain, serve
        # the common equality + range queries.

        # contact_submissions
        await db["contact_submissions"].create_index("uid")
        await db["contact_submissions"].create_index("type")
        await db["contact_submissions"].create_index([("submitted_at", -1)])
        await db["contact_submissions"].create_index([("uid", 1), ("type", 1)])

        # extraction_rule_versions
        await db["extraction_rule_versions"].create_index(
            [("uid", 1), ("isValid", 1), ("createdAt", -1)]
        )

        # rule_suggestions
        await db["rule_suggestions"].create_index([("extractionId", 1)])
        await db["rule_suggestions"].create_index([("uid", 1), ("createdAt", -1)])

        # datasets — sparse unique on (source.type, source.originalId)
        await db["datasets"].create_index(
            [("source.type", 1), ("source.originalId", 1)],
            unique=True,
            sparse=True,
        )
        await db["datasets"].create_index([("ownerId", 1), ("updatedAt", -1)])

        # vault_secrets
        await db["vault_secrets"].create_index(
            [("uid", 1), ("var_name", 1)], unique=True
        )

        # feedback
        await db["feedback"].create_index(
            [("uid", 1), ("session", 1), ("ts", -1)]
        )
        await db["feedback"].create_index(
            [("uid", 1), ("session", 1), ("requestId", 1)],
            unique=True,
            sparse=True,
        )

        # ranking_objectives
        await db["ranking_objectives"].create_index([("sessionId", 1), ("name", 1)])

        # rewrites
        await db["rewrites"].create_index("name", unique=True, sparse=True)

        # pipelines
        await db["pipelines"].create_index([("session", 1), ("createdAt", -1)])

        # generation_errors
        await db["generation_errors"].create_index([("dataset_id", 1), ("createdAt", -1)])
        await db["generation_errors"].create_index([("source", 1), ("createdAt", -1)])

        # execution_error_instances
        await db["execution_error_instances"].create_index(
            [("dataset_id", 1), ("created_at", -1), ("operator", 1)]
        )
        await db["execution_error_instances"].create_index([("run_id", 1)])
        await db["execution_error_instances"].create_index(
            [("pattern_id", 1), ("created_at", -1)]
        )

        # rl_mitigation_attempts
        await db["rl_mitigation_attempts"].create_index(
            [("parent_pipeline_id", 1), ("pattern_id", 1)]
        )

        # kb_overlay — runtime-curated KB statements (MCP / UI / API)
        # Index by (namespace, status) for the snapshot builder's
        # "give me every validated statement" query, and by
        # (statement) for the dedup check on insert.
        await db["kb_overlay"].create_index(
            [("namespace", 1), ("validation.status", 1)]
        )
        await db["kb_overlay"].create_index("statement")
        await db["kb_overlay"].create_index([("source.uid", 1)])

        print("  - PG document store: indexes ensured.")

        # Seed rewrites
        from backend.infra.dbs.expdb.seed_rewrites import seed_rewrites
        n = await seed_rewrites(db)
        if n:
            print(f"  - PG document store: seeded {n} rewrite rule(s).")

        # Seed built-in ranking objectives + evaluation procedures.
        # The Rust session_seed handler reads from doc_ranking_objectives /
        # doc_evaluation_procedures; those tables must be non-empty for
        # objectives/evals to appear on the frontend. The authoritative list
        # comes from the Neo4j KB (same source as /catalog/objectives).
        await _seed_objectives_and_evals(db)

    await wait_for_conn("Postgres (document store)", _logic)


async def _seed_objectives_and_evals(db) -> None:
    """Upsert built-in ranking objectives and evaluation procedures into
    their Postgres tables so the Rust session_seed handler finds them on
    a fresh deploy.  Idempotent (ON CONFLICT DO NOTHING)."""
    import uuid
    try:
        from dorian.knowledge.ontology_kb import load_kb
        kb = load_kb()

        objectives = sorted(set(kb.incoming("Ranking Objective", "is_a")))
        evals = sorted(set(kb.incoming("Evaluation Procedure", "is_an")))

        obj_col = db["ranking_objectives"]
        eval_col = db["evaluation_procedures"]
        await obj_col._ensure_table()
        await eval_col._ensure_table()
        pool = await db._pool()
        async with pool.acquire() as conn:
            for name in objectives:
                uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"obj:{name}"))
                await conn.execute(
                    f"INSERT INTO {obj_col._table} (id, data) "
                    "VALUES ($1, $2::jsonb) ON CONFLICT (id) DO NOTHING",
                    uid,
                    f'{{"uuid":"{uid}","name":"{name}"}}',
                )
            for name in evals:
                uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"eval:{name}"))
                await conn.execute(
                    f"INSERT INTO {eval_col._table} (id, data) "
                    "VALUES ($1, $2::jsonb) ON CONFLICT (id) DO NOTHING",
                    uid,
                    f'{{"uuid":"{uid}","name":"{name}"}}',
                )
        print(f"  - PG document store: seeded {len(objectives)} objective(s), "
              f"{len(evals)} eval(s) from KB.")
    except Exception as exc:
        # KB may be unavailable during first-run provisioning (Neo4j not yet
        # seeded). Print a warning; the tables will be populated on next
        # provision run or when the KB seed completes.
        print(f"  - WARNING: could not seed objectives/evals from KB: {exc}")


async def init_redis():
    """Provision Redis: Set ACLs for 'dorian' user."""
    r = cfg.redis
    redis = aioredis.from_url(f"redis://{r.host}:{r.port}")

    async def _logic():
        # ACL SETUSER dorian on allkeys allchannels +@pubsub +get +set +xread +xadd +zadd +zpopmin >pswd
        acl_cmd = (
            f"SETUSER {r.user} on ~* &* "
            "+@read +@write +@pubsub +@connection "
            # f"+@pubsub +get +set +xread +xadd +zadd +zpopmin "
            f">{r.password}"
        )
        await redis.execute_command("ACL", *acl_cmd.split())
        print(f"  - Redis: ACL for '{r.user}' provisioned.")

    await wait_for_conn("Redis", _logic)
    await redis.aclose()


# init_neo4j removed — neo4j retired (#71). KB lives in the rust
# snapshot at volumes/kb_snapshot.json.


# --- Backup Logic ---

async def create_db_backup(db_name: str) -> Path:
    """Creates a gzipped tarball of a specific database directory."""
    db_path = SOURCE_DIR / db_name
    timestamp = pendulum.now().format("YYYY-MM-DD_HHmm")
    archive_name = BACKUP_DEST / f"{db_name}_{timestamp}"

    if not db_path.exists():
        print(f"  - Backup: Skipping {db_name} (path not found: {db_path})")
        return None

    # Run compression in executor to prevent blocking the event loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: shutil.make_archive(str(archive_name.resolve()), 'gztar', root_dir=db_path)
    )
    print(f"  - Backup: {db_name} archived successfully.")
    return archive_name

# --- Main Entry ---

async def main():
    print(f"--- Provisioning Stack: {pendulum.now().to_datetime_string()} ---")

    # Run Database Initializations concurrently
    await asyncio.gather(
        init_document_store(),
        init_redis(),
    )

    # Run Backups concurrently
    print(f"\n--- Starting Backup Sequence ---")
    BACKUP_DEST.mkdir(parents=True, exist_ok=True)
    backup_tasks = [create_db_backup(db) for db in DATABASES_TO_BACKUP]
    await asyncio.gather(*backup_tasks)

    print(f"\n--- All tasks complete: {pendulum.now().to_datetime_string()} ---")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
