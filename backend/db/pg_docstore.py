"""Postgres document-store facade.

Per-collection document tables (``doc_<collection>``) exposing the docstore
subset Dorian historically used:

    db.<collection>.find_one(filter)
    db.<collection>.find(filter).sort(...).limit(...).to_list(...)
    db.<collection>.insert_one(doc)
    db.<collection>.insert_many(docs)
    db.<collection>.update_one(filter, update, upsert=False)
    db.<collection>.update_many(filter, update)
    db.<collection>.delete_one(filter)
    db.<collection>.delete_many(filter)
    db.<collection>.count_documents(filter)
    db.<collection>.create_index(spec, unique=..., sparse=...)
    db.<collection>.drop()
    db.list_collection_names()
    db.command("ping")

Filter and update shapes supported:
    equality:            {"uid": "x"}
    dotted paths:        {"source.type": "upload"}
    operators:           $eq, $ne, $in, $nin, $exists, $and, $or, $gte, $gt, $lte, $lt, $regex
    update operators:    $set, $unset, $inc, $push, $pull, $setOnInsert

Aggregation pipelines: NOT supported. The one historical caller
(``dorian.pipeline.recommendation._sample_candidates``) was rewritten
as direct SQL on 2026-04-28; the AggregateCursor + 150 lines of
stage translation that defended against partial support are gone
with it. Add new SQL inline for new query shapes — every
``$match + $sample/$sort/$limit`` pipeline maps to a single SELECT.

``_id`` is the Postgres ``id`` column (TEXT). If a document arrives without
``_id`` it is generated as ``uuid.uuid4().hex``.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

import asyncpg


# ``get_pg_pool`` lives in backend.envs; the import is deferred to avoid a
# circular import (envs.py exposes ``expdb = Database()``).
async def _get_pg_pool() -> asyncpg.Pool:
    from backend.envs import get_pg_pool
    return await get_pg_pool()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = ""  # No global DDL — each Collection ensures its own
           # ``doc_<name>`` table on first use.


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """One-shot rename for legacy ``mongo_*`` tables → ``doc_*``.

    Older deploys created the per-collection tables under a
    ``mongo_<name>`` prefix (a holdover from the docstore era). The
    canonical name is ``doc_<name>``. Run an idempotent ALTER TABLE
    rename for any leftover rows so the next writer hits the right
    table without a manual migration.

    Otherwise a no-op (per-collection tables are created lazily by
    each ``Collection._ensure_table`` call).
    """
    async with pool.acquire() as conn:
        legacy = await conn.fetch(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename LIKE 'mongo_%'
            """
        )
        for row in legacy:
            old = row["tablename"]
            new = "doc_" + old[len("mongo_"):]
            # If the new table already exists we keep it (newer
            # writers landed first); otherwise rename in place.
            existing = await conn.fetchval(
                "SELECT 1 FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename = $1",
                new,
            )
            if existing:
                continue
            await conn.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')


# ---------------------------------------------------------------------------
# Result types (mimic docstore shape — callsites read .inserted_id etc.)
# ---------------------------------------------------------------------------

@dataclass
class InsertOneResult:
    inserted_id: str
    acknowledged: bool = True


@dataclass
class InsertManyResult:
    inserted_ids: list[str]
    acknowledged: bool = True


@dataclass
class UpdateResult:
    matched_count: int
    modified_count: int
    upserted_id: str | None = None
    acknowledged: bool = True


@dataclass
class DeleteResult:
    deleted_count: int
    acknowledged: bool = True


# ---------------------------------------------------------------------------
# Filter translation: filter dict → SQL WHERE fragment
# ---------------------------------------------------------------------------

_MONGO_OPS = {
    "$eq": "=",
    "$ne": "<>",
    "$gt": ">",
    "$gte": ">=",
    "$lt": "<",
    "$lte": "<=",
}


def _json_path(field: str) -> str:
    """Translate a dotted JSON path (e.g. ``source.type``) into a JSONB
    text-accessor expression: ``data #>> ARRAY['source','type']``.

    Single-segment fields stay as ``data->>'field'`` since the text operator
    is faster and is what the expression indexes use.
    """
    parts = field.split(".")
    if len(parts) == 1:
        return f"data->>'{parts[0]}'"
    arr = ",".join(f"'{p}'" for p in parts)
    return f"data #>> ARRAY[{arr}]"


def _json_value_path(field: str) -> str:
    """As :func:`_json_path` but returning the raw JSONB value, not text.

    Used for ``$in`` / ``$nin`` comparisons where we need to keep the
    original type (int, bool, array) rather than coerce to string.
    """
    parts = field.split(".")
    if len(parts) == 1:
        return f"data->'{parts[0]}'"
    arr = ",".join(f"'{p}'" for p in parts)
    return f"data #> ARRAY[{arr}]"


class _Translator:
    """Accumulates positional args while rendering a filter to SQL."""

    def __init__(self, start_index: int = 1):
        self.args: list[Any] = []
        self._next = start_index

    def _param(self, value: Any) -> str:
        # asyncpg wants native Python types for most fields, but JSONB
        # comparisons need json-encoded strings when going through ::jsonb.
        self.args.append(value)
        p = f"${self._next}"
        self._next += 1
        return p

    def _compare_scalar(self, field: str, op_sql: str, value: Any) -> str:
        # Numeric and boolean comparisons need the raw JSONB value; string
        # equality is fine via the text accessor (faster, index-friendly).
        if isinstance(value, (int, float, bool)):
            return f"({_json_value_path(field)})::jsonb {op_sql} {self._param(json.dumps(value))}::jsonb"
        return f"{_json_path(field)} {op_sql} {self._param(str(value))}"

    def _field_clause(self, field: str, clause: Any) -> str:
        if field == "_id":
            return self._id_clause(clause)

        if not isinstance(clause, Mapping):
            # equality shortcut — use JSONB containment so nested shapes match too
            # ``_rebuild_nested`` already produces the full nested dict
            # for both dotted and non-dotted paths — wrapping it in an
            # outer ``{head: ...}`` would double-up the head key
            # (e.g. ``source.type='openml'`` would generate
            # ``{"source": {"source": "openml"}}`` and silently match
            # zero rows, which broke openml-loader's idempotent
            # ``find_one({"source.originalId": ...})`` dedup path on
            # every re-run).
            return f"data @> {self._param(json.dumps(_rebuild_nested(field, clause)))}::jsonb"

        parts: list[str] = []
        for op, val in clause.items():
            if op == "$eq":
                parts.append(self._compare_scalar(field, "=", val))
            elif op == "$ne":
                parts.append(self._compare_scalar(field, "<>", val))
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                parts.append(self._compare_scalar(field, _MONGO_OPS[op], val))
            elif op == "$in":
                if not val:
                    parts.append("FALSE")
                else:
                    placeholders = []
                    for v in val:
                        placeholders.append(f"{self._param(json.dumps(v))}::jsonb")
                    parts.append(f"({_json_value_path(field)})::jsonb IN ({', '.join(placeholders)})")
            elif op == "$nin":
                if not val:
                    parts.append("TRUE")
                else:
                    placeholders = []
                    for v in val:
                        placeholders.append(f"{self._param(json.dumps(v))}::jsonb")
                    parts.append(f"({_json_value_path(field)})::jsonb NOT IN ({', '.join(placeholders)})")
            elif op == "$exists":
                present = f"data ? {self._param(field.split('.')[0])}" if "." not in field else f"({_json_value_path(field)}) IS NOT NULL"
                parts.append(present if val else f"NOT ({present})")
            elif op == "$regex":
                flags = clause.get("$options", "")
                operator = "~*" if "i" in flags else "~"
                parts.append(f"{_json_path(field)} {operator} {self._param(val)}")
            elif op == "$options":
                continue  # consumed by $regex
            else:
                raise NotImplementedError(f"Operator {op!r} not supported")
        return " AND ".join(parts) if parts else "TRUE"

    def _id_clause(self, clause: Any) -> str:
        if not isinstance(clause, Mapping):
            return f"id = {self._param(_coerce_id(clause))}"
        parts: list[str] = []
        for op, val in clause.items():
            if op == "$in":
                if not val:
                    parts.append("FALSE")
                else:
                    placeholders = [self._param(_coerce_id(v)) for v in val]
                    parts.append(f"id IN ({', '.join(placeholders)})")
            elif op == "$nin":
                if not val:
                    parts.append("TRUE")
                else:
                    placeholders = [self._param(_coerce_id(v)) for v in val]
                    parts.append(f"id NOT IN ({', '.join(placeholders)})")
            elif op == "$eq":
                parts.append(f"id = {self._param(_coerce_id(val))}")
            elif op == "$ne":
                parts.append(f"id <> {self._param(_coerce_id(val))}")
            else:
                raise NotImplementedError(f"_id operator {op!r} not supported")
        return " AND ".join(parts) if parts else "TRUE"

    def translate(self, filt: Mapping[str, Any] | None) -> str:
        if not filt:
            return "TRUE"
        parts: list[str] = []
        for key, val in filt.items():
            if key == "$and":
                sub = [self.translate(c) for c in val]
                parts.append("(" + " AND ".join(sub) + ")")
            elif key == "$or":
                sub = [self.translate(c) for c in val]
                parts.append("(" + " OR ".join(sub) + ")")
            elif key == "$nor":
                sub = [self.translate(c) for c in val]
                parts.append("NOT (" + " OR ".join(sub) + ")")
            else:
                parts.append(self._field_clause(key, val))
        return " AND ".join(parts) if parts else "TRUE"


def _rebuild_nested(dotted: str, value: Any) -> Any:
    """Turn ``"a.b.c"`` + ``v`` into ``{"a": {"b": {"c": v}}}`` for containment.

    Always wraps the leaf — ``"a"`` + ``v`` yields ``{"a": v}``, NOT bare
    ``v``. The previous shape skipped wrapping at the top level; the
    only caller then added its own outer ``{head: ...}`` which
    double-wrapped dotted paths and silently broke every dotted-key
    ``find_one``. Now both call patterns share one rule.
    """
    if "." not in dotted:
        return {dotted: value}
    head, rest = dotted.split(".", 1)
    return {head: _rebuild_nested(rest, value)}


def _coerce_id(v: Any) -> str:
    """Normalise an id input to the TEXT form Postgres stores.

    Strings pass through. Bytes are hex-encoded (rare — only used when
    callers pass raw digest values as ids).
    """
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


# ---------------------------------------------------------------------------
# Update translation: docstore $set/$unset/etc → jsonb expression
# ---------------------------------------------------------------------------

def _apply_update(doc: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a update expression to a document dict (Python-side).

    We pull the row, mutate in Python, write it back. Simpler than writing
    every operator as a JSONB expression — the round trip is cheap for the
    sizes Dorian stores, and correctness is trivial to verify.
    """
    new = dict(doc)
    for op, payload in update.items():
        if op == "$set":
            for k, v in payload.items():
                _set_path(new, k, v)
        elif op == "$unset":
            for k in payload:
                _unset_path(new, k)
        elif op == "$inc":
            for k, v in payload.items():
                cur = _get_path(new, k) or 0
                _set_path(new, k, cur + v)
        elif op == "$push":
            for k, v in payload.items():
                lst = _get_path(new, k)
                if lst is None:
                    lst = []
                elif not isinstance(lst, list):
                    raise TypeError(f"$push on non-array field {k}")
                if isinstance(v, dict) and "$each" in v:
                    lst.extend(v["$each"])
                else:
                    lst.append(v)
                _set_path(new, k, lst)
        elif op == "$pull":
            for k, v in payload.items():
                lst = _get_path(new, k)
                if isinstance(lst, list):
                    _set_path(new, k, [x for x in lst if x != v])
        elif op == "$addToSet":
            for k, v in payload.items():
                lst = _get_path(new, k)
                if lst is None:
                    lst = []
                if v not in lst:
                    lst.append(v)
                _set_path(new, k, lst)
        elif op == "$setOnInsert":
            # handled separately by update_one upsert path
            continue
        elif op in ("$currentDate",):
            # Not used in this codebase; explicit NotImplemented keeps surprises loud.
            raise NotImplementedError(f"Update operator {op!r} not supported")
        else:
            # Implicit full-document replacement (docstore quirk)
            new[op] = payload
    return new


def _set_path(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _unset_path(d: dict, path: str) -> None:
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)


def _get_path(d: dict, path: str) -> Any:
    parts = path.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


# ---------------------------------------------------------------------------
# Document <-> row helpers
# ---------------------------------------------------------------------------

def _row_to_doc(row: asyncpg.Record) -> dict[str, Any]:
    data = row["data"]
    if isinstance(data, str):
        doc = json.loads(data)
    else:
        doc = dict(data) if data else {}
    doc["_id"] = row["id"]
    return doc


def _doc_to_row(doc: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(id, data_json)`` — ``_id`` is stripped from the stored blob."""
    d = dict(doc)
    _id = d.pop("_id", None)
    if _id is None:
        _id = uuid.uuid4().hex
    return _coerce_id(_id), json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Cursor — mirrors motor's find() cursor enough for async-for / to_list / etc.
# ---------------------------------------------------------------------------

class Cursor:
    def __init__(self, coll: "Collection", filt: Mapping[str, Any] | None):
        self._coll = coll
        self._filt = filt or {}
        self._sort: list[tuple[str, int]] = []
        self._limit: int | None = None
        self._skip: int = 0
        self._projection: Mapping[str, int] | None = None

    # chaining — both sync and async variants (docstore cursors are sync)
    def sort(self, key_or_list, direction: int | None = None) -> "Cursor":
        if isinstance(key_or_list, str):
            self._sort.append((key_or_list, direction if direction is not None else 1))
        else:
            for item in key_or_list:
                if isinstance(item, tuple):
                    self._sort.append(item)
                else:
                    self._sort.append((item, 1))
        return self

    def limit(self, n: int) -> "Cursor":
        self._limit = n
        return self

    def skip(self, n: int) -> "Cursor":
        self._skip = n
        return self

    def project(self, projection: Mapping[str, int] | None) -> "Cursor":
        self._projection = projection
        return self

    async def _run(self) -> list[asyncpg.Record]:
        sql, args = self._coll._build_select(
            self._filt, sort=self._sort, limit=self._limit, skip=self._skip
        )
        pool = await self._coll._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        self._iter_rows: list[asyncpg.Record] | None = None
        self._iter_idx = 0
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._iter_rows is None:
            self._iter_rows = await self._run()
            self._iter_idx = 0
        if self._iter_idx >= len(self._iter_rows):
            raise StopAsyncIteration
        row = self._iter_rows[self._iter_idx]
        self._iter_idx += 1
        doc = _row_to_doc(row)
        return _project(doc, self._projection)

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if length is not None and (self._limit is None or length < self._limit):
            self._limit = length
        rows = await self._run()
        return [_project(_row_to_doc(r), self._projection) for r in rows]


def _project(doc: dict[str, Any], projection: Mapping[str, int] | None) -> dict[str, Any]:
    if not projection:
        return doc
    include = {k: v for k, v in projection.items() if v == 1 or v is True}
    exclude = {k: v for k, v in projection.items() if v == 0 or v is False}
    if include and exclude and set(exclude) != {"_id"}:
        raise ValueError("projection cannot mix include and exclude (except _id)")
    if include:
        keep_id = include.pop("_id", 1) != 0
        out: dict[str, Any] = {}
        for k in include:
            if k in doc:
                out[k] = doc[k]
        if keep_id and "_id" in doc:
            out["_id"] = doc["_id"]
        return out
    if exclude:
        out = dict(doc)
        for k in exclude:
            out.pop(k, None)
        return out
    return doc


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

class Collection:
    def __init__(self, db: "Database", name: str):
        self._db = db
        self.name = name
        # Each collection has its own dedicated table named
        # ``doc_<collection>``. The unified-table era's
        # ``WHERE collection = $1`` filter is gone; the table name
        # itself disambiguates. See ``_ensure_table`` for the
        # on-the-fly DDL path.
        self._table = f"doc_{name}"
        self._table_ensured = False

    async def _get_pool(self) -> asyncpg.Pool:
        return await self._db._pool()

    async def _ensure_table(self) -> None:
        """Create the per-collection table if it doesn't exist yet.
        Idempotent — runs at-most-once per Collection instance per
        process. Cheap because the CREATE TABLE IF NOT EXISTS path
        is a single round-trip when the table is already there."""
        if self._table_ensured:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id         TEXT PRIMARY KEY,
                    data       JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
        self._table_ensured = True

    # ---- SELECT builder shared by find/find_one/count ----

    def _build_select(
        self,
        filt: Mapping[str, Any] | None,
        *,
        sort: Sequence[tuple[str, int]] | None = None,
        limit: int | None = None,
        skip: int = 0,
        columns: str = "id, data",
    ) -> tuple[str, list[Any]]:
        t = _Translator(start_index=1)
        where = t.translate(filt)
        sql = f"SELECT {columns} FROM {self._table} WHERE ({where})"
        if sort:
            order_parts = []
            for field, direction in sort:
                if field == "_id":
                    expr = "id"
                else:
                    expr = _json_path(field)
                order_parts.append(f"{expr} {'DESC' if direction < 0 else 'ASC'}")
            sql += " ORDER BY " + ", ".join(order_parts)
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if skip:
            sql += f" OFFSET {int(skip)}"
        return sql, list(t.args)

    # ---- Read ----

    def find(self, filter: Mapping[str, Any] | None = None, projection: Mapping[str, int] | None = None) -> Cursor:
        cur = Cursor(self, filter)
        if projection is not None:
            cur.project(projection)
        return cur

    async def find_one(
        self,
        filter: Mapping[str, Any] | None = None,
        projection: Mapping[str, int] | None = None,
        *,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        await self._ensure_table()
        sql, args = self._build_select(filter, sort=sort, limit=1)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, *args)
        if row is None:
            return None
        return _project(_row_to_doc(row), projection)

    async def count_documents(self, filter: Mapping[str, Any] | None = None) -> int:
        await self._ensure_table()
        t = _Translator(start_index=1)
        where = t.translate(filter)
        sql = f"SELECT COUNT(*) FROM {self._table} WHERE ({where})"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(sql, *t.args))

    async def estimated_document_count(self) -> int:
        return await self.count_documents({})

    async def distinct(self, field: str, filter: Mapping[str, Any] | None = None) -> list[Any]:
        await self._ensure_table()
        t = _Translator(start_index=1)
        where = t.translate(filter)
        sql = (
            f"SELECT DISTINCT ({_json_value_path(field) if field != '_id' else 'to_jsonb(id)'}) AS v "
            f"FROM {self._table} WHERE ({where})"
        )
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *t.args)
        out: list[Any] = []
        for r in rows:
            v = r["v"]
            if isinstance(v, str) and v.startswith(("[", "{", '"')):
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            out.append(v)
        return out

    # ---- Write ----

    async def insert_one(self, document: Mapping[str, Any]) -> InsertOneResult:
        await self._ensure_table()
        _id, data_json = _doc_to_row(document)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {self._table} (id, data, created_at, updated_at)
                    VALUES ($1, $2::jsonb, NOW(), NOW())""",
                _id, data_json,
            )
        return InsertOneResult(inserted_id=_id)

    async def insert_many(self, documents: Iterable[Mapping[str, Any]], ordered: bool = True) -> InsertManyResult:
        await self._ensure_table()
        rows = [_doc_to_row(d) for d in documents]
        if not rows:
            return InsertManyResult(inserted_ids=[])
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                f"""INSERT INTO {self._table} (id, data, created_at, updated_at)
                    VALUES ($1, $2::jsonb, NOW(), NOW())""",
                rows,
            )
        return InsertManyResult(inserted_ids=[r[0] for r in rows])

    async def update_one(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        return await self._update_many(filter, update, limit_one=True, upsert=upsert)

    async def update_many(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
    ) -> UpdateResult:
        return await self._update_many(filter, update, limit_one=False, upsert=False)

    async def _update_many(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        limit_one: bool,
        upsert: bool,
    ) -> UpdateResult:
        await self._ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                sql, args = self._build_select(filter, limit=1 if limit_one else None)
                sql += " FOR UPDATE"
                rows = await conn.fetch(sql, *args)
                matched = len(rows)
                modified = 0
                for row in rows:
                    old_doc = _row_to_doc(row)
                    new_doc = _apply_update(old_doc, update)
                    new_doc.pop("_id", None)
                    await conn.execute(
                        f"""UPDATE {self._table}
                            SET data = $2::jsonb, updated_at = NOW()
                            WHERE id = $1""",
                        row["id"], json.dumps(new_doc, default=str),
                    )
                    modified += 1
                upserted_id: str | None = None
                if matched == 0 and upsert:
                    base: dict[str, Any] = {}
                    for k, v in filter.items():
                        if k == "_id":
                            base["_id"] = v
                        elif k.startswith("$"):
                            continue
                        elif isinstance(v, Mapping) and any(kk.startswith("$") for kk in v):
                            continue
                        else:
                            _set_path(base, k, v)
                    if "$setOnInsert" in update:
                        for k, v in update["$setOnInsert"].items():
                            _set_path(base, k, v)
                    base = _apply_update(base, update)
                    _id, data_json = _doc_to_row(base)
                    await conn.execute(
                        f"""INSERT INTO {self._table} (id, data, created_at, updated_at)
                            VALUES ($1, $2::jsonb, NOW(), NOW())
                            ON CONFLICT (id) DO NOTHING""",
                        _id, data_json,
                    )
                    upserted_id = _id
                return UpdateResult(matched_count=matched, modified_count=modified, upserted_id=upserted_id)

    async def find_one_and_update(
        self,
        filter: Mapping[str, Any],
        update: Mapping[str, Any],
        *,
        return_document: str | bool = "before",
        upsert: bool = False,
        projection: Mapping[str, int] | None = None,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        await self._ensure_table()
        after = return_document is True or str(return_document).upper().endswith("AFTER")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                sql, args = self._build_select(filter, sort=sort, limit=1)
                sql += " FOR UPDATE"
                row = await conn.fetchrow(sql, *args)
                if row is None:
                    if not upsert:
                        return None
                    await self._update_many(filter, update, limit_one=True, upsert=True)
                    if not after:
                        return None
                    return await self.find_one(filter, projection)
                old_doc = _row_to_doc(row)
                new_doc = _apply_update(old_doc, update)
                new_doc.pop("_id", None)
                await conn.execute(
                    f"""UPDATE {self._table}
                        SET data = $2::jsonb, updated_at = NOW()
                        WHERE id = $1""",
                    row["id"], json.dumps(new_doc, default=str),
                )
                out = _row_to_doc({"id": row["id"], "data": json.dumps(new_doc)}) if after else old_doc
                return _project(out, projection)

    async def replace_one(
        self,
        filter: Mapping[str, Any],
        replacement: Mapping[str, Any],
        *,
        upsert: bool = False,
    ) -> UpdateResult:
        await self._ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                sql, args = self._build_select(filter, limit=1)
                sql += " FOR UPDATE"
                row = await conn.fetchrow(sql, *args)
                if row is not None:
                    new = dict(replacement)
                    new.pop("_id", None)
                    await conn.execute(
                        f"""UPDATE {self._table}
                            SET data = $2::jsonb, updated_at = NOW()
                            WHERE id = $1""",
                        row["id"], json.dumps(new, default=str),
                    )
                    return UpdateResult(matched_count=1, modified_count=1)
                if upsert:
                    _id, data_json = _doc_to_row(replacement)
                    await conn.execute(
                        f"""INSERT INTO {self._table} (id, data, created_at, updated_at)
                            VALUES ($1, $2::jsonb, NOW(), NOW())
                            ON CONFLICT (id) DO NOTHING""",
                        _id, data_json,
                    )
                    return UpdateResult(matched_count=0, modified_count=0, upserted_id=_id)
                return UpdateResult(matched_count=0, modified_count=0)

    async def delete_one(self, filter: Mapping[str, Any]) -> DeleteResult:
        return await self._delete(filter, limit_one=True)

    async def delete_many(self, filter: Mapping[str, Any]) -> DeleteResult:
        return await self._delete(filter, limit_one=False)

    async def _delete(self, filter: Mapping[str, Any], *, limit_one: bool) -> DeleteResult:
        await self._ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                sql, args = self._build_select(filter, limit=1 if limit_one else None, columns="id")
                sql += " FOR UPDATE"
                rows = await conn.fetch(sql, *args)
                if not rows:
                    return DeleteResult(deleted_count=0)
                ids = [r["id"] for r in rows]
                await conn.execute(
                    f"DELETE FROM {self._table} WHERE id = ANY($1::TEXT[])",
                    ids,
                )
                return DeleteResult(deleted_count=len(ids))

    # No ``aggregate(pipeline)`` — the one historical caller (random
    # sampling in ``dorian.pipeline.recommendation``) was rewritten as
    # direct SQL on 2026-04-28. If you're tempted to add JSON-filter
    # aggregation back, write the SQL inline instead — every stage we
    # need (``$match``, ``$sample``, ``$sort``, ``$limit``) maps
    # cleanly to a single SELECT, and the previous translator-and-
    # AggregateCursor pair was 150+ lines defending against partial
    # support that no caller actually used.

    # ---- DDL-ish ----

    async def create_index(
        self,
        keys,
        *,
        name: str | None = None,
        unique: bool = False,
        sparse: bool = False,
    ) -> str:
        """Create a partial expression index over this collection's JSONB fields.

        Index keys are filter-dict-style: ``[("uid", 1), ("session", 1)]`` or ``"uid"``.
        The resulting Postgres index is scoped to ``WHERE collection = '<name>'``
        so different collections' indexes don't collide.
        """
        if isinstance(keys, str):
            key_list = [(keys, 1)]
        elif isinstance(keys, (list, tuple)) and keys and isinstance(keys[0], str):
            key_list = [(keys[0], 1)]
        else:
            key_list = list(keys)

        exprs: list[str] = []
        for field, _direction in key_list:
            if field == "_id":
                exprs.append("id")
            else:
                exprs.append(f"({_json_path(field)})")

        idx_name = name or f"idx_pgc_{self.name}_{'_'.join(f.replace('.', '_') for f, _ in key_list)}"
        idx_name = idx_name[:63]  # Postgres identifier length cap

        where_clauses = [f"collection = '{self.name}'"]
        if sparse:
            # 'sparse' in the legacy semantics means "only index documents that have the field".
            for field, _ in key_list:
                if field == "_id":
                    continue
                top = field.split(".")[0]
                where_clauses.append(f"data ? '{top}'")
        where_clause = " AND ".join(where_clauses)

        unique_tok = "UNIQUE " if unique else ""
        # Per-collection table — drop the WHERE collection=$1
        # qualifier (only useful when one table held all
        # collections). Sparse-clause WHEREs survive.
        sql_where = ""
        sparse_clauses = []
        if sparse:
            for field, _ in key_list:
                if field == "_id":
                    continue
                top = field.split(".")[0]
                sparse_clauses.append(f"data ? '{top}'")
        if sparse_clauses:
            sql_where = " WHERE " + " AND ".join(sparse_clauses)
        sql = (
            f"CREATE {unique_tok}INDEX IF NOT EXISTS {idx_name} "
            f"ON {self._table} ({', '.join(exprs)})"
            f"{sql_where}"
        )
        await self._ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql)
        return idx_name

    async def drop(self) -> None:
        await self._ensure_table()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self._table}")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    def __init__(self):
        self._pool_obj: asyncpg.Pool | None = None
        self._schema_ensured = False

    async def _pool(self) -> asyncpg.Pool:
        if self._pool_obj is None:
            self._pool_obj = await _get_pg_pool()
        if not self._schema_ensured:
            await ensure_schema(self._pool_obj)
            self._schema_ensured = True
        return self._pool_obj

    def __getitem__(self, name: str) -> Collection:
        return Collection(self, name)

    def __getattr__(self, name: str) -> Collection:
        if name.startswith("_"):
            raise AttributeError(name)
        return Collection(self, name)

    async def list_collection_names(self) -> list[str]:
        # Per-collection tables named ``doc_<collection>``. Strip
        # the prefix to reconstruct the collection-name view callers
        # expect.
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename LIKE 'doc\\_%' ESCAPE '\\'
                ORDER BY tablename
                """
            )
        return [r["tablename"][len("doc_"):] for r in rows]

    async def create_collection(self, name: str) -> Collection:
        # No-op: collections appear on first insert. Matches the behaviour
        # where ``create_collection`` is mostly a metadata hint for validators
        # we don't use.
        _ = await self._pool()  # ensure schema
        return Collection(self, name)

    async def drop_collection(self, name: str) -> None:
        # Drops the entire doc_<name> table — destructive.
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS "doc_{name}"')

    async def command(self, *args, **kwargs):
        if args and args[0] == "ping":
            pool = await self._pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return {"ok": 1}
        raise NotImplementedError(f"Database.command({args!r}) not supported on the Postgres facade")


_db_singleton: Database | None = None


async def get_pg_db() -> Database:
    """Return the singleton ``Database`` instance.

    Lazy-inits the asyncpg pool + schema on first call. Safe to call from
    any coroutine; the pool itself is reused across callers.
    """
    global _db_singleton
    if _db_singleton is None:
        _db_singleton = Database()
    await _db_singleton._pool()  # ensure ready
    return _db_singleton
