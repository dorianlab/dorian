"""OpenML Dataset Crawler — ingests OpenML benchmark suites into Dorian's
experiment database.

Crawled datasets become **public datasets** (``isPublic=True``, ``ownerId=None``).
Each dataset document carries:

- OpenML provenance (``source.type == "openml"``, ``source.originalId``,
  ``source.url``)
- Target column metadata resolved from ``default_target_attribute``
- ``itemCount`` (row count)
- Feature and target column lists
- A full metafeature profile (same one the interactive upload flow builds)

Persistence paths:

1. ``expdb.datasets`` — the Postgres-backed document store (``doc_*``
   table, ``datasets`` collection). This is where the rest of Dorian reads
   dataset docs from.
2. ``ExperimentStore.upsert_dataset`` — Postgres relational tables + the
   in-memory KD-Tree for dataset similarity. Keeps the similarity index in
   lockstep with the catalogue so the RL generator and recommendation engine
   see freshly-seeded datasets immediately.

Usage::

    uv run python -m scripts.openml_loader
    uv run python -m scripts.openml_loader --suite OpenML-CC18
    uv run python -m scripts.openml_loader --dataset-id 31   # single dataset
    uv run python -m scripts.openml_loader --no-store        # skip KD-Tree index
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path for both direct and -m invocation
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import openml
import pandas as pd

from dorian.tabular.data.profiling.column_profile import compute_column_profiles
from dorian.tabular.data.profiling.profile_dataset import profile_dataframe

_log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Crawler
# ═══════════════════════════════════════════════════════════════════════════

class OpenMLCrawler:
    """Crawls OpenML datasets and persists them to ``expdb.datasets`` plus
    ``ExperimentStore`` (Postgres + KD-Tree).

    Upserts go through ``ExperimentStore.upsert_dataset()`` so the KD-Tree
    stays in sync with the backing store — the same contract user-uploaded
    datasets follow.
    """

    def __init__(
        self,
        storage_root: str = "./data",
        *,
        persist_to_store: bool = True,
    ):
        from backend.envs import expdb  # local import — avoids Dask side-effects on import

        self.expdb = expdb
        self.datasets_col = expdb.datasets
        self.storage_root = Path(storage_root)
        self._persist_to_store = persist_to_store
        self._store_initialized = False

        self._stats = {"crawled": 0, "skipped": 0, "failed": 0}

    # ------------------------------------------------------------------
    # One-shot provisioning
    # ------------------------------------------------------------------

    async def ensure_provisioned(self) -> None:
        """Provision the document-store schema + uniqueness index.

        Runs the same schema hook the app's bootstrap uses so a bare
        ``python -m scripts.openml_loader`` call works against a fresh
        Postgres instance (no backend lifespan required). Idempotent.
        """
        # Materialises ``doc_*`` + expression indexes.
        await self.expdb._pool()
        # Sparse-unique index on (source.type, source.originalId) — what lets
        # re-crawling the same OpenML id round-trip to the same row.
        await self.datasets_col.create_index(
            [("source.type", 1), ("source.originalId", 1)],
            unique=True,
            sparse=True,
        )

    # ------------------------------------------------------------------
    # ExperimentStore lifecycle
    # ------------------------------------------------------------------

    async def _ensure_store(self) -> None:
        """Lazily initialise the ExperimentStore singleton.

        In standalone script context (outside FastAPI lifespan) the store
        and Postgres pool aren't yet created. We init them once on first
        use so every ``upsert_dataset`` call goes through the proper
        facade (Postgres + KD-Tree).
        """
        if self._store_initialized:
            return
        from dorian.experiment.store import init_experiment_store
        await init_experiment_store()
        self._store_initialized = True
        _log.info("ExperimentStore initialised (Postgres schema + KD-Tree).")

    # ------------------------------------------------------------------
    # Single dataset
    # ------------------------------------------------------------------

    async def crawl_dataset(self, openml_id: int) -> str | None:
        """Crawl a single OpenML dataset. Returns the doc's ``_id`` or ``None``."""
        _log.info("Processing OpenML dataset %d …", openml_id)

        # Idempotency — skip when already ingested, unless the profile needs repair.
        existing = await self.datasets_col.find_one({
            "source.type": "openml",
            "source.originalId": str(openml_id),
        })
        if existing:
            needs_reprofile = False
            profile = existing.get("profile")
            if not profile:
                needs_reprofile = True
            elif isinstance(profile, dict):
                nulls = [k for k, v in profile.items() if v is None and k != "__errors__"]
                has_errors_without_capture = "__errors__" not in profile and nulls
                if has_errors_without_capture:
                    needs_reprofile = True
                    _log.info(
                        "Dataset %d has %d null metafeatures without error capture. Re-profiling.",
                        openml_id, len(nulls),
                    )

            if needs_reprofile:
                await self._reprofile_existing(existing, openml_id)
                return str(existing["_id"])

            _log.info(
                "Dataset %d already exists (id=%s). Skipping.",
                openml_id, existing["_id"],
            )
            self._stats["skipped"] += 1
            return str(existing["_id"])

        # ── Fetch from OpenML ────────────────────────────────────────────
        try:
            oml_dataset = openml.datasets.get_dataset(
                openml_id, download_data=True, download_qualities=True,
            )
        except Exception as exc:
            _log.error("Failed to download dataset %d from OpenML: %s", openml_id, exc)
            self._stats["failed"] += 1
            return None

        # ── Get data as DataFrame ────────────────────────────────────────
        target_attr = oml_dataset.default_target_attribute
        try:
            X, y, _, _ = oml_dataset.get_data(
                target=target_attr, dataset_format="dataframe",
            )
            df = pd.concat([X, y], axis=1) if y is not None else X
        except Exception as exc:
            _log.error("Failed to get data for dataset %d: %s", openml_id, exc)
            self._stats["failed"] += 1
            return None

        # ── Allocate TEXT id + persist CSV ───────────────────────────────
        # Postgres document store keys on TEXT; generate a uuid4 hex.
        did = uuid.uuid4().hex
        safe_name = "".join(
            c for c in oml_dataset.name if c.isalnum() or c in (" ", "_")
        ).rstrip()
        file_name = f"{safe_name.replace(' ', '_')}_{openml_id}.csv"
        relative_path = Path("datasets") / did / file_name
        full_path = self.storage_root / relative_path

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(full_path, index=False)
        except Exception as exc:
            _log.error("Failed to save CSV for dataset %d: %s", openml_id, exc)
            self._stats["failed"] += 1
            return None

        # ── Resolve columns ──────────────────────────────────────────────
        target_columns: list[str] = []
        if target_attr:
            # OpenML may list multiple comma-separated targets.
            target_columns = [
                t.strip() for t in target_attr.split(",") if t.strip() in df.columns
            ]
        feature_columns = [c for c in df.columns if c not in target_columns]
        item_count = len(df)

        # ── Provenance ───────────────────────────────────────────────────
        openml_url = oml_dataset.url or f"https://www.openml.org/d/{openml_id}"

        # ── Task info ────────────────────────────────────────────────────
        task_info = None
        if target_columns:
            # OpenML-CC18 is classification; other suites may be mixed. When we
            # crawl a non-CC18 suite we can resolve this per-dataset from the
            # OpenML task metadata instead of hard-coding it.
            task_info = {
                "type": "classification",
                "target": {"target": target_columns},
            }

        # ── Column profiles ──────────────────────────────────────────────
        column_profiles = compute_column_profiles(df)

        # ── Metafeature profile ──────────────────────────────────────────
        # Public datasets MUST land with a profile — every downstream
        # cue (auto-task ranking, recommendation scoring, eval-procedure
        # picker readiness) reads it. Failure is fatal for this dataset
        # so a half-seeded doc doesn't ship to operators looking healthy.
        try:
            profile = profile_dataframe(
                df,
                feature_columns=feature_columns,
                target_columns=target_columns,
            )
            _log.info(
                "Profiled dataset %d: %d metafeatures computed.",
                openml_id,
                sum(1 for v in profile.values() if v is not None),
            )
        except Exception as exc:
            _log.error("Profiling failed for dataset %d: %s", openml_id, exc)
            raise

        # ── Content hash for upload dedup ───────────────────────────────
        # Lets user uploads short-circuit against the public catalogue.
        # blake2b(digest_size=16) is fast enough that crawling N datasets
        # doesn't noticeably slow the seed phase.
        content_hash: str | None = None
        try:
            import hashlib as _hashlib
            h = _hashlib.blake2b(digest_size=16)
            with open(full_path, "rb") as _f:
                while True:
                    _chunk = _f.read(65_536)
                    if not _chunk:
                        break
                    h.update(_chunk)
            content_hash = h.hexdigest()
        except Exception as exc:  # pragma: no cover — best-effort
            _log.warning(
                "content hash failed for dataset %d (non-fatal): %s",
                openml_id, exc,
            )

        # ── Build + insert document ──────────────────────────────────────
        now = datetime.now(timezone.utc)
        dataset_doc = {
            "_id": did,
            "ownerId": None,            # public dataset
            "isPublic": True,
            "name": oml_dataset.name,
            "description": oml_dataset.description,
            "schemaVersion": 3,
            "dataType": "tabular",
            "itemCount": item_count,
            "contentHash": content_hash,
            "source": {
                "type": "openml",
                "originalId": str(oml_dataset.id),
                "url": openml_url,
            },
            "storage": {
                "format": "csv",
                "location": {
                    "type": "localfs",
                    "path": str(relative_path.as_posix()),
                },
                "formatSpecific": {"separator": ","},
            },
            "task": task_info,
            "columns": {
                "features": feature_columns,
                "targets": target_columns,
                "profiles": column_profiles,
            },
            "profile": profile,
            "analysis": None,
            "createdAt": now,
            "updatedAt": now,
        }

        try:
            await self.datasets_col.insert_one(dataset_doc)
            _log.info(
                "Inserted dataset %d (%s) id=%s  [%d rows, %d features]",
                openml_id, oml_dataset.name, did, item_count, len(feature_columns),
            )
        except Exception as exc:
            _log.error("Document-store insert failed for dataset %d: %s", openml_id, exc)
            if full_path.exists():
                full_path.unlink()
            self._stats["failed"] += 1
            return None

        # ── Mirror into ExperimentStore (Postgres + KD-Tree) ─────────────
        if self._persist_to_store and profile:
            try:
                await self._ensure_store()
                from dorian.experiment.store import get_experiment_store
                store = await get_experiment_store()
                await store.upsert_dataset(
                    did=did,
                    session="openml-crawler",
                    profile=profile,
                )
                _log.info("Dataset %d indexed in ExperimentStore (KD-Tree).", openml_id)
            except Exception as exc:
                _log.warning(
                    "ExperimentStore upsert failed for dataset %d (non-fatal): %s",
                    openml_id, exc,
                )

        self._stats["crawled"] += 1
        return did

    # ------------------------------------------------------------------
    # Suite / batch
    # ------------------------------------------------------------------

    async def crawl_suite(self, suite_name: str = "OpenML-CC18") -> list[str]:
        """Crawl every dataset in an OpenML benchmark suite.

        Returns the list of document ids (sorted by ``itemCount`` ascending).
        """
        _log.info("Fetching OpenML suite '%s' …", suite_name)
        try:
            suite = openml.study.get_suite(suite_name)
        except Exception as exc:
            _log.error("Failed to fetch suite '%s': %s", suite_name, exc)
            return []

        dataset_ids = sorted(set(suite.data))
        _log.info(
            "Suite '%s': %d tasks, %d unique datasets.",
            suite_name, len(suite.tasks), len(dataset_ids),
        )

        ids: list[str] = []
        for i, did in enumerate(dataset_ids, 1):
            _log.info("── [%d/%d] Dataset %d ──", i, len(dataset_ids), did)
            try:
                result = await self.crawl_dataset(did)
                if result:
                    ids.append(result)
            except Exception as exc:
                _log.error("Unexpected error for dataset %d: %s", did, exc)
                self._stats["failed"] += 1

        _log.info(
            "Suite '%s' done: %d crawled, %d skipped, %d failed.",
            suite_name,
            self._stats["crawled"],
            self._stats["skipped"],
            self._stats["failed"],
        )
        return ids

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_datasets_sorted_by_size(self) -> list[dict]:
        """Return all OpenML datasets from the store, sorted by itemCount asc."""
        return await self.datasets_col.find(
            {"source.type": "openml"},
        ).sort("itemCount", 1).to_list(None)

    async def get_dataset(self, did: str) -> dict | None:
        """Fetch a single dataset document by id."""
        return await self.datasets_col.find_one({"_id": did})

    async def load_dataframe(self, did: str) -> tuple[pd.DataFrame, pd.Series | None]:
        """Load a dataset's CSV, split into ``(X, y)``.

        Returns ``(full_df, None)`` when the dataset has no declared target.
        """
        doc = await self.get_dataset(did)
        if not doc:
            raise ValueError(f"Dataset {did} not found")

        csv_path = self.storage_root / doc["storage"]["location"]["path"]
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        df = pd.read_csv(csv_path)

        targets = (doc.get("columns") or {}).get("targets") or []
        if not targets and doc.get("task"):
            targets = (doc["task"].get("target") or {}).get("target") or []

        if targets:
            target_col = targets[0]
            if target_col in df.columns:
                return df.drop(columns=targets), df[target_col]

        return df, None

    # ------------------------------------------------------------------
    # Re-profiling
    # ------------------------------------------------------------------

    async def _reprofile_existing(self, doc: dict, openml_id: int) -> None:
        """Re-profile an existing dataset to capture error messages."""
        did = str(doc["_id"])
        csv_path = self.storage_root / doc["storage"]["location"]["path"]
        if not csv_path.exists():
            _log.warning(
                "CSV missing for dataset %d at %s. Cannot re-profile.",
                openml_id, csv_path,
            )
            return

        df = pd.read_csv(csv_path)
        cols = doc.get("columns") or {}
        feature_columns = cols.get("features") or [c for c in df.columns]
        target_columns = cols.get("targets") or []

        try:
            profile = profile_dataframe(
                df,
                feature_columns=feature_columns,
                target_columns=target_columns,
            )
        except Exception as exc:
            _log.warning("Re-profiling failed for dataset %d: %s", openml_id, exc)
            return

        errors = profile.get("__errors__", {})
        nulls = [k for k, v in profile.items() if v is None and k != "__errors__"]
        _log.info(
            "Re-profiled dataset %d (%s): %d errors captured, %d still null.",
            openml_id, doc.get("name", did), len(errors), len(nulls),
        )

        await self.datasets_col.update_one(
            {"_id": did},
            {"$set": {"profile": profile, "updatedAt": datetime.now(timezone.utc)}},
        )
        self._stats["crawled"] += 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close the ExperimentStore + Postgres pool if we initialised them."""
        if self._store_initialized:
            from dorian.experiment.store import shutdown_experiment_store
            from backend.envs import close_pg_pool

            await shutdown_experiment_store()
            await close_pg_pool()
            _log.info("ExperimentStore + Postgres pool shut down.")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    # Suppress Dask LocalCluster shutdown noise — every clean exit
    # produces a flurry of ``Batched Comm Closed`` ``CommClosedError``
    # tracebacks at INFO level when Nannies close before the scheduler
    # finishes flushing. These are not failures; they're how
    # ``distributed`` reports a normal shutdown race. Demote them so
    # the seeder's actual progress lines stay legible. Equivalent
    # ``distributed`` shutdown noise from bootstrap is silenced the
    # same way in ``backend/infra/bootstrap.py``.
    for _name in ("distributed.batched", "distributed.scheduler",
                  "distributed.nanny", "distributed.core"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Crawl OpenML datasets into Dorian's experiment database",
    )
    parser.add_argument(
        "--storage-root", default="./data",
        help="Root directory for CSV storage (default: ./data)",
    )
    parser.add_argument(
        "--suite", default="OpenML-CC18",
        help="OpenML benchmark suite name (default: OpenML-CC18)",
    )
    parser.add_argument(
        "--dataset-id", type=int, default=None,
        help="Crawl a single dataset by OpenML ID",
    )
    parser.add_argument(
        "--no-store", action="store_true",
        help="Skip ExperimentStore persistence (document store only, no KD-Tree index)",
    )
    args = parser.parse_args()

    crawler = OpenMLCrawler(
        storage_root=args.storage_root,
        persist_to_store=not args.no_store,
    )

    try:
        await crawler.ensure_provisioned()
        if args.dataset_id:
            did = await crawler.crawl_dataset(args.dataset_id)
            if did:
                _log.info("Done. Document id: %s", did)
            else:
                _log.error("Failed to crawl dataset %d.", args.dataset_id)
                sys.exit(1)
        else:
            await crawler.crawl_suite(args.suite)

            datasets = await crawler.get_datasets_sorted_by_size()
            _log.info("Datasets by size (ascending):")
            for ds in datasets:
                _log.info(
                    "  %6d rows | %-30s | openml:%s | %s",
                    ds.get("itemCount", 0),
                    (ds.get("name") or "")[:30],
                    (ds.get("source") or {}).get("originalId", ""),
                    (ds.get("source") or {}).get("url", ""),
                )
    finally:
        await crawler.shutdown()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
