"""
dorian/exec/claimer.py
----------------------
XPENDING/XCLAIM loop for the exec workers — picks up jobs that were
read but never ACKed by a consumer that has since crashed AND moves
repeatedly-redelivered entries to a dead-letter stream so a poisonous
job can't loop forever.

Semantics:
  * Periodically (every ``_INTERVAL_S``) XAUTOCLAIM the first
    ``_BATCH`` pending entries whose idle time exceeds ``_CLAIM_IDLE_S``.
  * For each claimed entry, XPENDING gives its delivery count.
  * If delivery count > ``_MAX_DELIVERIES``: the entry is copied to
    the dead-letter stream (``exec:jobs:deadletter`` by default) and
    XACKed from the live stream. Operators can inspect the DLQ and
    either re-enqueue manually (XREADGROUP on the DLQ) or delete.
  * Otherwise the entry dispatches through the normal handler path
    via the ``on_claim`` callback.

The claimer is spawned by ``Worker.run()`` alongside the consume
slots. A single claimer per worker is enough — it enumerates pending
entries once per interval and claims what it can. Multiple workers
racing is fine; XCLAIM is atomic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)

# Env-var accessors read every call instead of at import time. Tests
# can monkeypatch these to tune the claimer without module reloads;
# production reads them once effectively (the loop iterates at
# interval-scale and the env doesn't change at runtime).
def _jobs_stream() -> str:
    return os.environ.get("DORIAN_EXEC_JOBS_STREAM", "exec:jobs")


def _group() -> str:
    return os.environ.get("DORIAN_EXEC_GROUP", "exec-workers")


def _claim_idle_s() -> float:
    return float(os.environ.get("DORIAN_EXEC_CLAIM_IDLE_S", "60"))


def _interval_s() -> float:
    return float(os.environ.get("DORIAN_EXEC_CLAIM_INTERVAL_S", "15"))


def _batch() -> int:
    return int(os.environ.get("DORIAN_EXEC_CLAIM_BATCH", "100"))


def _dlq_stream() -> str:
    return os.environ.get("DORIAN_EXEC_DLQ_STREAM", "exec:jobs:deadletter")


def _max_deliveries() -> int:
    return int(os.environ.get("DORIAN_EXEC_MAX_DELIVERIES", "5"))


def _dlq_maxlen() -> int:
    return int(os.environ.get("DORIAN_EXEC_DLQ_MAXLEN", "10000"))


async def run_claimer(
    redis_client,
    consumer_name: str,
    on_claim,
    on_deadletter=None,
) -> None:
    """Loop forever claiming stuck entries, dead-lettering poisonous ones.

    ``on_claim`` is called with ``(entry_id, fields)`` for each entry
    that is still within its delivery-count budget. Entries whose
    delivery count exceeds the threshold are copied to the DLQ
    stream and XACKed here — ``on_claim`` is not called for them.

    ``on_deadletter``, when provided, is called with ``(entry_id, fields,
    times)`` for each poison entry right after it lands in the DLQ.
    Workers use it to bump observability counters.
    """
    while True:
        try:
            await asyncio.sleep(_interval_s())
        except asyncio.CancelledError:
            return

        idle_ms = int(_claim_idle_s() * 1000)
        try:
            resp = await redis_client.xautoclaim(
                name=_jobs_stream(),
                groupname=_group(),
                consumername=consumer_name,
                min_idle_time=idle_ms,
                start_id="0-0",
                count=_batch(),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("[exec-claimer] xautoclaim failed: %s", exc)
            continue

        entries: list[tuple[Any, Any]] = []
        try:
            if isinstance(resp, (list, tuple)) and len(resp) >= 2:
                raw = resp[1]
                if isinstance(raw, list):
                    entries = raw
        except Exception:
            entries = []
        if not entries:
            continue

        # XPENDING in range returns delivery counts so we can spot
        # entries that have crossed the threshold. Without this, the
        # claimer would loop on the same poison entry every tick.
        count_by_id = await _delivery_counts(redis_client, entries)

        claimed = 0
        deadlettered = 0
        max_deliveries = _max_deliveries()
        for entry_id, fields in entries:
            times = count_by_id.get(_id_str(entry_id), 0)
            if times > max_deliveries:
                if await _deadletter(redis_client, entry_id, fields, times):
                    await _safe_ack(redis_client, entry_id)
                    deadlettered += 1
                    if on_deadletter is not None:
                        try:
                            await on_deadletter(entry_id, fields, times)
                        except Exception as exc:
                            _log.warning(
                                "[exec-claimer] on_deadletter callback failed: %s",
                                exc,
                            )
                continue
            try:
                await on_claim(entry_id, fields)
                claimed += 1
            except Exception as exc:
                _log.warning(
                    "[exec-claimer] on_claim failed for %s: %s",
                    entry_id, exc,
                )

        if claimed:
            _log.info(
                "[exec-claimer] reclaimed %d pending entries onto %s",
                claimed, consumer_name,
            )
        if deadlettered:
            _log.warning(
                "[exec-claimer] dead-lettered %d entries (over %d deliveries) to %s",
                deadlettered, max_deliveries, _dlq_stream(),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _id_str(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


async def _delivery_counts(redis_client, entries: list) -> dict[str, int]:
    """Return {entry_id_str: times_delivered} for the given entries.

    Uses a single ``XPENDING`` range call scoped to the entries we
    just claimed. redis-py returns a list of dicts with keys like
    ``message_id`` and ``times_delivered`` (older versions use
    ``delivery_count``); we tolerate both.
    """
    if not entries:
        return {}
    ids = [_id_str(e[0]) for e in entries]
    ids.sort()
    start, end = ids[0], ids[-1]
    try:
        rows = await redis_client.xpending_range(
            _jobs_stream(), _group(), start, end, count=len(ids),
        )
    except Exception as exc:
        _log.warning("[exec-claimer] xpending_range failed: %s", exc)
        return {}

    out: dict[str, int] = {}
    for row in rows or []:
        # redis-py >=4: row is a dict; <4 returns tuples. Handle both.
        if isinstance(row, dict):
            msg_id = _id_str(row.get("message_id") or row.get("id") or "")
            times = int(row.get("times_delivered") or row.get("delivery_count") or 0)
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            msg_id = _id_str(row[0])
            # tuple layout: (id, consumer, idle_ms, times_delivered)
            times = int(row[3] or 0)
        else:
            continue
        out[msg_id] = times
    return out


async def _deadletter(redis_client, entry_id: Any, fields: Any, times: int) -> bool:
    """Copy the poison entry into the DLQ stream. Returns True on success."""
    try:
        # Normalise fields to a plain dict[str, str] so DLQ consumers
        # see a uniform shape regardless of aioredis decode settings.
        flat: dict[str, str] = {}
        items = fields.items() if isinstance(fields, dict) else fields
        for k, v in items:
            flat[_id_str(k)] = _id_str(v)
        flat.setdefault("_original_id", _id_str(entry_id))
        flat.setdefault("_times_delivered", str(times))
        await redis_client.xadd(
            _dlq_stream(), flat,
            maxlen=_dlq_maxlen(), approximate=True,
        )
        return True
    except Exception as exc:
        _log.warning(
            "[exec-claimer] dead-letter XADD failed for %s: %s",
            _id_str(entry_id), exc,
        )
        return False


async def _safe_ack(redis_client, entry_id: Any) -> None:
    try:
        await redis_client.xack(_jobs_stream(), _group(), entry_id)
    except Exception as exc:
        _log.warning("[exec-claimer] XACK failed for %s: %s",
                     _id_str(entry_id), exc)
