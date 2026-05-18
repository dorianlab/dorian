"""
dorian/exec/checks.py
---------------------
Data-quality check kinds running inside the execution engine.

Each ``@register("dq_check:<name>")`` function takes ``inputs`` (dict
from the submitter) and returns a dict that becomes the completion
event's ``result`` payload. Inputs typically carry at least:

    {
        "dataset_id": str,      # canonical dataset identifier
        "fpath": str,            # absolute CSV path (resolved by the
                                 # submitter so workers don't hit the DB directly)
        "uid": str, "session": str,  # propagated to completion event
    }

The compute itself is whatever pandas / numpy / sklearn work needs
doing. That work lives HERE, never in an event-bus handler, because
it is precisely the GIL-blocking workload that the coordination layer
must not see.

Gray-box vs unwrapped operators (design note)
---------------------------------------------
Dorian's default is to treat operators (including DQ checks) as gray
boxes — the pipeline engine schedules them, a pandas/sklearn call
runs them, the engine doesn't peer inside. That's the simplifying
assumption. The cost is missed fusion opportunities: running N DQ
checks separately means N passes over the same CSV.

Rejected shortcut: hardcoding a batch kind like
``dq_check:basic_stats`` that bakes several checks into one body.
That's the wrong level — it freezes a particular fusion into code,
skips the engine, and loses the information that the caller actually
wanted THREE logical checks (not a single proprietary one).

Correct path: each check stays a distinct LOGICAL operator kind.
User composition remains a pipeline. Fusion — when the pipeline
engine sees three `dq_check:*` ops reading the same input — is the
engine's job, not this file's. That engine-side fusion arrives with
the Petri-net / Ptolemy-II-style execution model migration already
on the roadmap.

Near-term mitigation: every check reads through
``backend.cache.cached_read_csv``, a content-addressable LRU that
parses via ``pyarrow.csv.read_csv`` once per file-content-hash and
memoises both the ``pa.Table`` and the pandas view. Multiple
``dq_check:*`` kinds on the same fpath therefore share a single
parse / materialisation without the checks themselves knowing about
one another — the cache achieves the "N passes → 1 parse"
optimisation without freezing a fusion into code.
"""
from __future__ import annotations

from typing import Any

from dorian.exec.registry import register


@register("dq_check:missing_values")
async def missing_values(inputs: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    """Per-column null count and ratio for a CSV dataset.

    This is the simplest-possible DQ check, chosen as the proof-of-
    pattern for phase I: pandas read + ``isna().sum()``. The check
    itself is trivial; the point is that the *entire* compute runs in
    the exec worker, not the event bus.
    """
    from backend.cache import cached_read_csv

    fpath = inputs.get("fpath")
    if not fpath:
        return {"error": "missing fpath"}

    # Shared content-addressable cache: a subsequent
    # ``dq_check:uniqueness`` / ``numeric_range`` on the same fpath
    # sees the parsed DataFrame hot, and the parse itself goes
    # through pyarrow (see ``cached_read_csv``). The coroutine slot
    # here is still the GIL-blocker we keep off the event loop;
    # we just no longer pay the CSV cost N times for N kinds.
    df = cached_read_csv(fpath)

    counts = df.isna().sum()
    n = int(len(df)) or 1

    columns = []
    for col in df.columns:
        missing = int(counts[col])
        columns.append({
            "column": str(col),
            "missing_count": missing,
            "missing_ratio": round(missing / n, 6),
        })

    total_missing = int(counts.sum())
    return {
        "dataset_id": inputs.get("dataset_id"),
        "n_rows": n,
        "n_cols": int(len(df.columns)),
        "total_missing": total_missing,
        "total_missing_ratio": round(total_missing / (n * max(1, len(df.columns))), 6),
        "columns": columns,
    }


@register("dq_check:uniqueness")
async def uniqueness(inputs: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    """Per-column distinct-value count and ratio.

    Sibling of ``dq_check:missing_values``. Kept as a separate kind
    (a separate LOGICAL operator) so a pipeline composed of
    uniqueness + missing_values + range remains three distinct ops
    the future engine can reason about and fuse if it wants to.
    """
    from backend.cache import cached_read_csv

    fpath = inputs.get("fpath")
    if not fpath:
        return {"error": "missing fpath"}

    df = cached_read_csv(fpath)
    n = int(len(df)) or 1

    nunique = df.nunique(dropna=True)
    missing = df.isna().sum()

    columns = []
    for col in df.columns:
        distinct = int(nunique[col])
        non_null = n - int(missing[col])
        columns.append({
            "column": str(col),
            "distinct_count": distinct,
            "distinct_ratio": round(distinct / max(1, non_null), 6),
        })

    return {
        "dataset_id": inputs.get("dataset_id"),
        "n_rows": n,
        "n_cols": int(len(df.columns)),
        "columns": columns,
    }


@register("dq_check:numeric_range")
async def numeric_range(inputs: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    """Min / max / mean for every numeric column.

    Non-numeric columns are skipped (including them would risk
    raising on mixed dtypes). Returns an empty ``columns`` list if
    the dataset has no numeric columns.
    """
    import pandas as pd

    from backend.cache import cached_read_csv

    fpath = inputs.get("fpath")
    if not fpath:
        return {"error": "missing fpath"}

    df = cached_read_csv(fpath)
    n = int(len(df)) or 1

    columns = []
    for col in df.columns:
        col_data = df[col]
        if not pd.api.types.is_numeric_dtype(col_data):
            continue
        clean = col_data.dropna()
        if len(clean) == 0:
            continue
        columns.append({
            "column": str(col),
            "min": float(clean.min()),
            "max": float(clean.max()),
            "mean": float(clean.mean()),
        })

    return {
        "dataset_id": inputs.get("dataset_id"),
        "n_rows": n,
        "n_cols": int(len(df.columns)),
        "columns": columns,
    }
