"""Unified Trial type — single shape every consumer reads + writes.

A Trial is one (pipeline, dataset, config) evaluation. The historical
``evaluations`` table is a per-metric row store; one logical Trial
produces N rows when a pipeline emits N metrics. This module wraps
that with a Trial-level API so AutoML, RL, the cross-product engine,
and user-driven runs all speak the same vocabulary.

Read path
---------
* AutoML's BO surrogate trains on every Trial regardless of
  source — a cross-product trial run on dataset D feeds the same
  model that AutoML's targeted trials on D feed. One source of truth.
* RL's MemoryPolicy reads dataset-similarity neighbours from this
  table — same query, no parallel "rl_trials" path.

Write path
----------
* RL trainer calls :func:`record_trial_from_executor` with an
  ``ExecutorResult``.
* AutoML BO calls :func:`record_trial_from_bo` after each ask/tell
  iteration.
* Cross-product engine writes via the same record_trial helper.
* User pipelines flow through the existing
  ``observability/collector`` path which already calls
  ``upsert_evaluation`` on the experiment store.

Shape
-----
Every Trial decomposes into N evaluation rows (one per metric)
when persisted, sharing run_id / source / status / config. The
Trial dataclass below is the in-memory representation; the
serialisation layer (``store_trial``) splits it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


# Sources distinguish who created the trial — drives surrogate
# weighting, UI filtering, and provenance tracking. Keep this list
# in sync with the DB CHECK constraint (when we add one) and the
# admin /trials endpoint's filter.
SOURCE_RL = "rl"
SOURCE_AUTOML = "automl"
SOURCE_XPRODUCT = "xproduct"
SOURCE_USER = "user"
ALL_SOURCES = (SOURCE_RL, SOURCE_AUTOML, SOURCE_XPRODUCT, SOURCE_USER)

# Statuses cover terminal trial states. failed / timeout trials still
# get persisted so surrogates can learn from them (a config that
# failed at fit-time tells the optimizer "this region is dangerous").
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
ALL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED)


@dataclass
class Trial:
    """One pipeline-on-dataset evaluation outcome.

    ``metrics`` is the canonical record of every per-metric value
    (e.g. ``{"accuracy": 0.85, "f1": 0.81}``). The first key, when
    iteration order is stable, is the "primary" — RL uses it for
    reward, AutoML uses it for the surrogate target, the leaderboard
    sorts on it. For consumers that only know about a primary scalar,
    ``score`` returns ``metrics`` collapsed to its first value.
    """

    pipeline_id: str
    dataset_id: str
    run_id: str
    source: str = SOURCE_USER
    status: str = STATUS_SUCCESS
    metrics: dict[str, float] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    eval_config: dict[str, Any] | None = None
    wall_clock_s: float | None = None
    error_message: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.source not in ALL_SOURCES:
            raise ValueError(f"unknown source {self.source!r}; expected one of {ALL_SOURCES}")
        if self.status not in ALL_STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {ALL_STATUSES}")
        # Truncate huge error messages so a recursive traceback
        # doesn't blow up the JSONB column.
        if self.error_message and len(self.error_message) > 4096:
            self.error_message = self.error_message[:4093] + "..."

    @property
    def score(self) -> float | None:
        """Primary metric value, or ``None`` for failed trials.

        For RL: the reward channel reads this; tie-break to the first
        metric in ``metrics`` if multiple are present (eval_template
        emits classification metrics in a stable order: accuracy,
        f1, precision, recall).
        """
        if not self.metrics:
            return None
        return next(iter(self.metrics.values()))

    @property
    def primary_metric(self) -> str | None:
        if not self.metrics:
            return None
        return next(iter(self.metrics.keys()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "dataset_id": self.dataset_id,
            "run_id": self.run_id,
            "source": self.source,
            "status": self.status,
            "metrics": self.metrics,
            "config": self.config,
            "eval_config": self.eval_config,
            "wall_clock_s": self.wall_clock_s,
            "error_message": self.error_message,
            "created_at": (self.created_at.isoformat() if self.created_at else None),
        }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def store_trial(pool, trial: Trial) -> None:
    """Persist ``trial`` to the unified evaluations table. One row
    per metric (mirrors the legacy schema); each row carries the
    full source / status / config / wall_clock_s / error_message
    fields. Rows for the same trial share ``run_id``.

    Failed / timeout trials still write at least one row (with
    ``metric_name='__none__'`` and ``metric_value=NaN``) so the
    surrogate can see the failure and the leaderboard knows this
    pipeline-dataset pair was attempted.
    """
    cfg_json = json.dumps(trial.config) if trial.config else None
    eval_cfg_json = json.dumps(trial.eval_config) if trial.eval_config else None

    if trial.metrics:
        rows = [
            (
                trial.pipeline_id,
                trial.dataset_id,
                trial.run_id,
                metric_name,
                _coerce_metric_value(metric_value),
                eval_cfg_json,
                trial.source,
                trial.status,
                trial.wall_clock_s,
                trial.error_message,
                cfg_json,
            )
            for metric_name, metric_value in trial.metrics.items()
        ]
    else:
        # Failed-without-metrics — still record the attempt.
        rows = [
            (
                trial.pipeline_id, trial.dataset_id, trial.run_id,
                "__none__", float("nan"),
                eval_cfg_json,
                trial.source, trial.status, trial.wall_clock_s,
                trial.error_message, cfg_json,
            )
        ]
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO evaluations (
                pipeline_id, dataset_id, run_id,
                metric_name, metric_value, eval_config,
                source, status, wall_clock_s, error_message, config
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11::jsonb)
            """,
            rows,
        )


def _coerce_metric_value(v: Any) -> float:
    """Best-effort conversion to float. NaN passes through; non-numeric
    values become NaN so the column constraint stays satisfied."""
    if v is None:
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# Construction helpers — typed shortcuts the trial loops call
# ---------------------------------------------------------------------------

def trial_from_metrics(
    *,
    pipeline_id: str,
    dataset_id: str,
    run_id: str,
    metrics: Mapping[str, float],
    source: str,
    config: Mapping[str, Any] | None = None,
    eval_config: Mapping[str, Any] | None = None,
    wall_clock_s: float | None = None,
) -> Trial:
    """Construct a successful Trial. Most common entry point — RL,
    AutoML, and cross-product all reach for this after a pipeline
    completes successfully and they have a metrics dict in hand."""
    return Trial(
        pipeline_id=pipeline_id,
        dataset_id=dataset_id,
        run_id=run_id,
        source=source,
        status=STATUS_SUCCESS,
        metrics=dict(metrics),
        config=dict(config or {}),
        eval_config=dict(eval_config) if eval_config else None,
        wall_clock_s=wall_clock_s,
        created_at=datetime.now(timezone.utc),
    )


def failed_trial(
    *,
    pipeline_id: str,
    dataset_id: str,
    run_id: str,
    source: str,
    error_message: str,
    config: Mapping[str, Any] | None = None,
    wall_clock_s: float | None = None,
    status: str = STATUS_FAILED,
) -> Trial:
    """Construct a failed Trial. AutoML's surrogate still learns
    "this region is bad" from these — they're not silently dropped."""
    return Trial(
        pipeline_id=pipeline_id,
        dataset_id=dataset_id,
        run_id=run_id,
        source=source,
        status=status,
        metrics={},
        config=dict(config or {}),
        wall_clock_s=wall_clock_s,
        error_message=error_message,
        created_at=datetime.now(timezone.utc),
    )


__all__ = [
    "Trial",
    "store_trial",
    "trial_from_metrics",
    "failed_trial",
    "SOURCE_RL", "SOURCE_AUTOML", "SOURCE_XPRODUCT", "SOURCE_USER",
    "STATUS_SUCCESS", "STATUS_FAILED", "STATUS_TIMEOUT", "STATUS_CANCELLED",
    "ALL_SOURCES", "ALL_STATUSES",
]
