"""Seed the ``exception_patterns`` collection from the canonical
in-code library.

The seed library in ``dorian.pipeline.exceptions.registry.seed_patterns``
is the author-curated source of truth during development; this
module serialises each pattern into JSON-storable documents and
upserts them into expdb. Runtime loads from expdb first, falls back
to the in-code library when expdb is unreachable — same pattern as
``seed_rewrites.py`` + ``expdb.rewrites``.

Adding a new exception pattern = new seed entry in ``registry.py``
+ re-seed. Down the road the seed library can be emptied and the
only canonical source becomes expdb (seeded once, mutated at
runtime from LLM proposals via ``pattern_discovery``).
"""
from __future__ import annotations


def _to_doc(pattern) -> dict:
    """Serialise an ``ExceptionPattern`` into a storable dict.

    ``message_regex`` drops to its source string; the loader
    recompiles on read so the in-memory ``re.Pattern`` object
    doesn't need pickling."""
    regex_source = None
    if pattern.message_regex is not None:
        regex_source = pattern.message_regex.pattern
    mitigations = [
        {"rewrite_id": m.rewrite_id, "weight": float(m.weight)}
        for m in pattern.mitigations
    ]
    # Stable ID derived from signature hash (leaf) or the bucket's
    # synthetic hash that ``_bucket`` assigns. Patterns promoted
    # from LLM proposals come with fresh hashes too.
    return {
        "_id": pattern.signature_hash,
        "exception_type": pattern.exception_type,
        "operator_fqn": pattern.operator_fqn,
        "site_library": pattern.site_library,
        "message_template": pattern.message_template,
        "user_frame_depth": int(pattern.user_frame_depth),
        "mitigations": mitigations,
        "source": pattern.source,
        "status": pattern.status,
        "scope": pattern.scope,
        "message_regex": regex_source,
    }


async def seed_exception_patterns(db) -> int:
    """Upsert all in-code seed patterns into expdb. Returns the
    number of documents touched. Idempotent — safe to re-seed on
    every deploy."""
    from dorian.pipeline.exceptions.registry import seed_patterns

    patterns = seed_patterns()
    if not patterns:
        return 0
    touched = 0
    for p in patterns:
        doc = _to_doc(p)
        result = await db.exception_patterns.update_one(
            {"_id": doc["_id"]},
            {"$set": doc},
            upsert=True,
        )
        if result.upserted_id is not None or result.modified_count:
            touched += 1
    return touched


async def main() -> None:
    from backend.db import get_pg_db
    db = await get_pg_db()
    n = await seed_exception_patterns(db)
    print(f"Seeded {n} exception pattern(s).")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
