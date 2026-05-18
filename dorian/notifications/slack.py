"""
dorian/notifications/slack.py
-----------------------------
Lightweight Slack webhook client for sending notifications.

All notifications go through ``send_slack_message()`` which is a no-op
when the webhook URL is empty (disabled in config).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import traceback
from typing import Any

import httpx

from backend.config import config


# Singleton async HTTP client — avoids creating a new TCP connection per notification.
_http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Error dedup / rate limiter
# ---------------------------------------------------------------------------
#
# Identical errors fired in a tight loop (e.g. an RL scheduler hitting the same
# code path 100x/minute) would otherwise drown the Slack channel.  This layer
# fingerprints each error by ``(source, event_type, first_line_of_error)`` and
# suppresses repeats within ``_DEDUP_WINDOW`` seconds.  The first occurrence is
# sent immediately; subsequent identical errors are counted and flushed as a
# single "+N repeats in Ws" summary either when the window expires or when
# the next distinct variant of the same fingerprint arrives.

_DEDUP_WINDOW: float = 60.0  # seconds — short enough to catch bursts, long enough to matter
_DEDUP_MAX_KEYS: int = 512   # hard cap on the in-memory map to prevent unbounded growth

_dedup_state: dict[str, dict[str, Any]] = {}
_dedup_lock = asyncio.Lock()


def _error_fingerprint(source: str, event_type: str, error: str) -> str:
    """Stable fingerprint for an error notification.

    Uses the first non-empty line of the error text — traceback line numbers
    and variable memory addresses past that point don't affect the fingerprint.
    """
    first_line = ""
    for line in (error or "").splitlines():
        line = line.strip()
        if line:
            first_line = line
            break
    payload = f"{source}|{event_type}|{first_line}".encode("utf-8", errors="replace")
    return hashlib.sha1(payload).hexdigest()[:16]


async def _should_send_error(
    source: str, event_type: str, error: str
) -> tuple[bool, int]:
    """Check the dedup layer. Returns ``(should_send, suppressed_count)``.

    - ``should_send=True`` on first occurrence or after window expiry.
    - ``suppressed_count`` is non-zero only when we're flushing a burst —
      the caller should append ``"(+N repeats in Ws)"`` to the outgoing
      message so the reader knows noise was compressed.
    """
    key = _error_fingerprint(source, event_type, error)
    now = time.monotonic()

    async with _dedup_lock:
        entry = _dedup_state.get(key)
        if entry is None:
            # First occurrence — send and arm the window.
            _dedup_state[key] = {"last_sent": now, "suppressed": 0, "first_seen": now}
            # Opportunistic cleanup to bound memory.
            if len(_dedup_state) > _DEDUP_MAX_KEYS:
                cutoff = now - _DEDUP_WINDOW * 2
                for k in [k for k, v in _dedup_state.items() if v["last_sent"] < cutoff]:
                    _dedup_state.pop(k, None)
            return True, 0

        if now - entry["last_sent"] < _DEDUP_WINDOW:
            # Still inside the suppression window — count and drop.
            entry["suppressed"] += 1
            return False, 0

        # Window expired — flush with the accumulated burst count.
        suppressed = entry["suppressed"]
        entry["last_sent"] = now
        entry["suppressed"] = 0
        entry["first_seen"] = now
        return True, suppressed


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10)
    return _http_client


def _webhook_url() -> str:
    try:
        return config.slack.webhook_url or ""
    except (AttributeError, KeyError):
        return ""


def _is_enabled() -> bool:
    return bool(_webhook_url())


def _notify_on(category: str) -> bool:
    """Check if a notification category is enabled in config."""
    try:
        return bool(getattr(config.slack.notify_on, category, True))
    except (AttributeError, KeyError):
        return True


async def send_slack_message(text: str, blocks: list[dict] | None = None) -> None:
    """Post a message to the configured Slack webhook.

    No-op if ``slack.webhook_url`` is empty.  Errors are swallowed
    (notifications must never break the main application).
    """
    url = _webhook_url()
    if not url:
        return

    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        client = _get_http_client()
        await client.post(url, json=payload)
    except Exception:
        # Notification failure must never propagate — swallow silently.
        pass


# ---------------------------------------------------------------------------
# Pre-built notification formatters
# ---------------------------------------------------------------------------

async def notify_error(
    source: str,
    event_type: str,
    error: str,
    trace: str,
    uid: str | None = None,
    session: str | None = None,
) -> None:
    """Send a Slack notification for a backend error event."""
    if not _notify_on("errors"):
        return

    # Dedup identical errors within a short window — otherwise a hot loop
    # hitting the same code path will drown the channel.
    should_send, suppressed = await _should_send_error(source, event_type, error)
    if not should_send:
        return

    # Truncate trace to avoid Slack's 3000-char block limit
    trace_truncated = trace[-2500:] if len(trace) > 2500 else trace

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Error: {event_type}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Source:*\n`{source}`"},
                {"type": "mrkdwn", "text": f"*Error:*\n{error}"},
            ],
        },
    ]

    if uid or session:
        context_fields = []
        if uid:
            context_fields.append({"type": "mrkdwn", "text": f"*User:* `{uid}`"})
        if session:
            context_fields.append({"type": "mrkdwn", "text": f"*Session:* `{session[:12]}...`"})
        blocks.append({"type": "section", "fields": context_fields})

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"```{trace_truncated}```"},
    })

    text = f"[ERROR] {event_type} in {source}: {error}"
    if suppressed:
        burst_note = (
            f"_Suppressed {suppressed} identical repeat(s) in the last "
            f"{int(_DEDUP_WINDOW)}s_"
        )
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": burst_note}],
        })
        text += f" (+{suppressed} repeats in {int(_DEDUP_WINDOW)}s)"

    await send_slack_message(text, blocks)


async def notify_feedback(
    uid: str,
    session: str,
    request_id: str,
    answers: dict,
) -> None:
    """Send a Slack notification when feedback is submitted."""
    if not _notify_on("feedback"):
        return

    # Format answers readably
    answer_lines = []
    for key, value in answers.items():
        val_str = json.dumps(value, default=str) if not isinstance(value, str) else value
        answer_lines.append(f"  *{key}:* {val_str}")
    answers_text = "\n".join(answer_lines[:20])  # cap at 20 entries
    if len(answers) > 20:
        answers_text += f"\n  ... and {len(answers) - 20} more"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Feedback Received", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*User:*\n`{uid}`"},
                {"type": "mrkdwn", "text": f"*Session:*\n`{session[:12]}...`"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Answers:*\n{answers_text}"},
        },
    ]

    text = f"[FEEDBACK] from {uid} — {len(answers)} answer(s)"
    await send_slack_message(text, blocks)


async def notify_session_event(
    event_type: str,
    uid: str,
    session: str | None = None,
    details: dict | None = None,
) -> None:
    """Send a one-line ping for session lifecycle events.

    Kept deliberately minimal — these events fire constantly and
    shouldn't eat channel real estate with formatted blocks. Just
    the event name, user, and short session id on a single line.
    """
    if not _notify_on("sessions"):
        return

    parts = [f"[{event_type}] uid=`{uid}`"]
    if session:
        parts.append(f"session=`{session[:12]}`")
    if details:
        extras = " ".join(f"{k}={v}" for k, v in list(details.items())[:4] if v)
        if extras:
            parts.append(extras)
    await send_slack_message(" ".join(parts))


async def notify_backup(path: str, triggered_by: str, errors: list[str]) -> None:
    """Send a one-line ping when a system backup completes.

    On success: a single line with path and triggering user.
    On failure: the same line plus an appended code block with the
    error list — failures still need debugging detail.
    """
    status = "ok" if not errors else f"errors={len(errors)}"
    text = f"[BACKUP] {status} by=`{triggered_by}` path=`{path}`"
    if errors:
        text += f"\n```{chr(10).join(errors)}```"
    await send_slack_message(text)


async def notify_contact_submission(
    submission_type: str,
    uid: str,
    submission_id: str,
    title: str = "",
    subject: str = "",
    details: str = "",
    severity: str = "",
    name: str = "",
) -> None:
    """Send a Slack notification when a contact form is submitted (bug, feedback, contact)."""
    if not _notify_on("feedback"):
        return

    emoji_map = {"bug": ":bug:", "feedback": ":speech_balloon:", "contact": ":envelope:"}
    emoji = emoji_map.get(submission_type, ":memo:")
    heading = {
        "bug": "Bug Report Submitted",
        "feedback": "Feedback Submitted",
        "contact": "Contact Form Submitted",
    }.get(submission_type, "Contact Form Submitted")

    # Truncate details to avoid Slack block limits
    display_details = details[:500] + "..." if len(details) > 500 else details
    display_title = title or subject or "(no title)"

    user_display = f"`{uid}`"
    if name:
        user_display = f"{name} (`{uid}`)"

    fields = [
        {"type": "mrkdwn", "text": f"*From:*\n{user_display}"},
        {"type": "mrkdwn", "text": f"*Type:*\n{emoji} {submission_type}"},
        {"type": "mrkdwn", "text": f"*Title/Subject:*\n{display_title}"},
    ]
    if severity:
        fields.append({"type": "mrkdwn", "text": f"*Severity:*\n{severity}"})

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": heading, "emoji": True},
        },
        {"type": "section", "fields": fields},
    ]
    if display_details:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Details:*\n{display_details}"},
        })

    text = f"[{submission_type.upper()}] from {uid}: {display_title}"
    await send_slack_message(text, blocks)


async def notify_onboarding_feedback(
    uid: str,
    tooltip_id: str,
    vote: str,
    dwell_ms: int,
) -> None:
    """Send a one-line ping when a user votes on an onboarding tooltip."""
    if not _notify_on("feedback"):
        return

    emoji = "thumbsup" if vote == "up" else "thumbsdown"
    text = (
        f"[TOOLTIP] uid=`{uid}` tooltip=`{tooltip_id}` "
        f"vote=:{emoji}: dwell={dwell_ms / 1000:.1f}s"
    )
    await send_slack_message(text)
