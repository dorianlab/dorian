"""Tests for the RL UX-isolation contract."""
from __future__ import annotations

import pytest

from rl.jobs.isolation import (
    BackpressureState,
    RL_BACKPRESSURE_THRESHOLD,
    RL_GROUP,
    RL_JOBS_STREAM,
    RLJob,
    submit_rl_job,
)


def test_defaults_follow_convention():
    assert RL_JOBS_STREAM == "rl:jobs"
    assert RL_GROUP == "exec-rl"
    assert RL_BACKPRESSURE_THRESHOLD >= 1


def test_rl_job_is_immutable_and_tagged_low_priority_by_default():
    job = RLJob(kind="rl:eval", inputs={}, run_id="r1", episode=1)
    assert job.priority == "low"
    assert job.emitted_at_ms > 0
    with pytest.raises(Exception):
        # frozen=True -> can't mutate
        job.priority = "high"  # type: ignore[misc]


def test_backpressure_engages_above_threshold():
    bp = BackpressureState(threshold=100, release_ratio=0.5)
    assert not bp.engaged
    assert not bp.observe(50)
    assert not bp.engaged
    assert bp.observe(150)
    assert bp.engaged


def test_backpressure_has_hysteresis():
    """Release only when depth drops below threshold * release_ratio."""
    bp = BackpressureState(threshold=100, release_ratio=0.5)
    bp.observe(200)  # engage
    assert bp.engaged
    # Drop to 80 (still above release=50) -> stays engaged.
    assert bp.observe(80)
    assert bp.engaged
    # Drop to 40 (below release=50) -> releases.
    assert not bp.observe(40)
    assert not bp.engaged


def test_submit_rl_job_refuses_when_backpressure_engaged():
    bp = BackpressureState(threshold=100)
    bp.observe(500)  # engage
    ok = submit_rl_job(
        "rl:eval", {}, run_id="r1", episode=1, backpressure=bp
    )
    assert ok is False


def test_submit_rl_job_succeeds_when_backpressure_clear():
    bp = BackpressureState(threshold=100)
    # bp is not engaged (no observation) -> submission allowed.
    assert submit_rl_job(
        "rl:eval", {}, run_id="r1", episode=1, backpressure=bp
    ) is True


def test_submit_rl_job_without_backpressure_always_allowed():
    # No backpressure handle at all -- fail-open path for clients
    # that choose not to wire the observability feed yet.
    assert submit_rl_job(
        "rl:eval", {}, run_id="r1", episode=1
    ) is True
