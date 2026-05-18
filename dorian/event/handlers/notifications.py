"""
dorian/event/handlers/notifications.py
--------------------------------------
Event handlers that forward key events to Slack.

Subscribes to error events, feedback, session lifecycle, backup completion,
and onboarding tooltip feedback.  All handlers are fire-and-forget safe —
Slack failures never propagate to the caller.
"""
from __future__ import annotations

from backend.events import Event
from dorian.notifications.slack import (
    notify_error,
    notify_feedback,
    notify_session_event,
    notify_backup,
    notify_onboarding_feedback,
    notify_contact_submission,
)


# ---------------------------------------------------------------------------
# Error notifications — covers all handler failures and system errors
# ---------------------------------------------------------------------------

async def slack_on_error(event: Event) -> None:
    """Forward error events to Slack with full stack trace.

    Many backend call sites store the full ``traceback.format_exc()``
    directly under the ``error`` key and leave ``trace`` empty.  When
    that happens, we split: the first line of the error becomes the
    summary, and the full text becomes the trace — so Slack always
    shows the complete backtrace in the code block.

    Engine-driven `PipelineRunFailed` (AutoML BO trials, RL rollouts,
    xproduct cross-product evaluations) are EXPECTED to fail
    frequently — they're the search/exploration loops trying many
    bad configurations on purpose. Routing every one of those to
    Slack drowns out the actual user-facing errors. Filter them out
    here based on the session prefix, which is the canonical
    "this is a synthetic engine session" marker the engines use
    (``automl:…``, ``rl:…``, ``xproduct:…``).
    """
    d = event.data
    if event.type == "PipelineRunFailed":
        sess = (d.get("session") or "")
        if isinstance(sess, str) and ":" in sess:
            prefix = sess.split(":", 1)[0]
            if prefix in {"automl", "rl", "xproduct"}:
                return
    raw_error = d.get("error", "unknown error")
    trace = d.get("trace", "")
    # Fallback: if trace is empty but error looks like a formatted
    # traceback (multi-line with "Traceback" or "File "), promote it.
    if not trace and isinstance(raw_error, str) and "\n" in raw_error:
        trace = raw_error
        first_line = raw_error.strip().splitlines()[-1] or raw_error.strip().splitlines()[0]
        raw_error = first_line
    await notify_error(
        source=d.get("source", "unknown"),
        event_type=d.get("event", event.type),
        error=raw_error,
        trace=trace,
        uid=d.get("uid"),
        session=d.get("session"),
    )


# ---------------------------------------------------------------------------
# Feedback notifications
# ---------------------------------------------------------------------------

async def slack_on_feedback(event: Event) -> None:
    """Forward feedback submissions to Slack with full answer content."""
    d = event.data
    await notify_feedback(
        uid=d.get("uid", "?"),
        session=d.get("session", "?"),
        request_id=d.get("requestId", "?"),
        answers=d.get("answers", {}),
    )


# ---------------------------------------------------------------------------
# Session lifecycle notifications
# ---------------------------------------------------------------------------

async def slack_on_session_created(event: Event) -> None:
    """Notify Slack when a new session is created."""
    d = event.data
    await notify_session_event(
        "SessionCreated",
        uid=d.get("uid", "?"),
        session=d.get("session_id"),
        details={"name": d.get("name", "")},
    )


async def slack_on_session_init(event: Event) -> None:
    """Notify Slack when a user connects (InitSession)."""
    d = event.data
    await notify_session_event(
        "UserConnected",
        uid=d.get("uid", "?"),
        session=d.get("session"),
    )


# ---------------------------------------------------------------------------
# Backup notifications
# ---------------------------------------------------------------------------

async def slack_on_backup(event: Event) -> None:
    """Notify Slack when a system backup completes."""
    d = event.data
    await notify_backup(
        path=d.get("path", "?"),
        triggered_by=d.get("triggered_by", "?"),
        errors=d.get("errors", []),
    )


# ---------------------------------------------------------------------------
# Onboarding tooltip feedback
# ---------------------------------------------------------------------------

async def slack_on_contact_form(event: Event) -> None:
    """Forward contact form submissions (bug, feedback, contact) to Slack."""
    d = event.data
    # Contact-us has first_name/last_name; bug/feedback have optional name field
    name = d.get("name", "")
    if not name and d.get("first_name"):
        name = f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
    await notify_contact_submission(
        submission_type=d.get("type", "unknown"),
        uid=d.get("uid", "?"),
        submission_id=d.get("_id", d.get("submission_id", "?")),
        title=d.get("title", ""),
        subject=d.get("subject", ""),
        details=d.get("details", d.get("description", d.get("message", ""))),
        severity=d.get("severity", ""),
        name=name,
    )


async def slack_on_tooltip_feedback(event: Event) -> None:
    """Forward onboarding tooltip votes to Slack."""
    d = event.data
    await notify_onboarding_feedback(
        uid=d.get("uid", "?"),
        tooltip_id=d.get("tooltip_id", "?"),
        vote=d.get("vote", "?"),
        dwell_ms=d.get("dwell_ms", 0),
    )
