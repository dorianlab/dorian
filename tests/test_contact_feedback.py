"""
tests/test_contact_feedback.py
------------------------------
E2E tests for the contact/feedback submission pipeline:

1. Submissions are persisted in docstore ``contact_submissions``.
2. Each submission emits a ``ContactFormSubmitted`` event.
3. The Slack notification handler formats and sends the notification.
4. Backup can be triggered and produces Redis + docstore snapshots.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    """Run an async coroutine in a fresh event loop (same pattern as test_data_pathways)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Contact form submissions → docstore persistence + event emission
# ---------------------------------------------------------------------------

class TestBugReportSubmission:
    """POST /contact/bug persists to docstore and emits ContactFormSubmitted."""

    def test_bug_report_persisted(self):
        async def _test():
            from dorian.api.routes.contact import submit_bug_report

            result = await submit_bug_report(
                uid="user-1",
                title="Button crash",
                description="The run button crashes on click",
                steps="1. Click run\n2. App crashes",
                expected="Pipeline should execute",
                device="Chrome / Windows",
                severity="high",
                files=[],
            )

            assert result["status"] == "ok"
            submission_id = result["submission_id"]
            assert submission_id

            from backend.envs import expdb
            doc = await expdb["contact_submissions"].find_one({"_id": submission_id})
            assert doc is not None
            assert doc["type"] == "bug"
            assert doc["uid"] == "user-1"
            assert doc["title"] == "Button crash"
            assert doc["severity"] == "high"
            assert doc["description"] == "The run button crashes on click"

        _run(_test())

    def test_bug_report_emits_event(self):
        async def _test():
            from dorian.api.routes.contact import submit_bug_report
            from backend.events import aemit

            aemit.reset_mock()

            await submit_bug_report(
                uid="user-2",
                title="Layout broken",
                description="Sidebar overlaps canvas",
                files=[],
            )

            aemit.assert_called_once()
            event = aemit.call_args[0][0]
            assert event.type == "ContactFormSubmitted"
            assert event.data["type"] == "bug"
            assert event.data["uid"] == "user-2"
            assert event.data["title"] == "Layout broken"

        _run(_test())


class TestFeedbackSubmission:
    """POST /contact/feedback persists and emits event."""

    def test_feedback_persisted(self):
        async def _test():
            from dorian.api.routes.contact import submit_feedback

            result = await submit_feedback(
                uid="user-3",
                feedback_type="usability",
                subject="Search is slow",
                details="The operator search takes 5+ seconds to filter",
                rating="3",
            )

            assert result["status"] == "ok"
            sid = result["submission_id"]

            from backend.envs import expdb
            doc = await expdb["contact_submissions"].find_one({"_id": sid})
            assert doc is not None
            assert doc["type"] == "feedback"
            assert doc["feedback_type"] == "usability"
            assert doc["subject"] == "Search is slow"
            assert doc["rating"] == "3"

        _run(_test())

    def test_feedback_emits_event(self):
        async def _test():
            from dorian.api.routes.contact import submit_feedback
            from backend.events import aemit

            aemit.reset_mock()

            await submit_feedback(
                uid="user-3",
                feedback_type="feature",
                subject="Add dark mode",
                details="Would love a dark theme",
            )

            aemit.assert_called_once()
            event = aemit.call_args[0][0]
            assert event.type == "ContactFormSubmitted"
            assert event.data["type"] == "feedback"
            assert event.data["subject"] == "Add dark mode"

        _run(_test())


class TestContactUsSubmission:
    """POST /contact/us persists and emits event."""

    def test_contact_us_persisted(self):
        async def _test():
            from dorian.api.routes.contact import submit_contact_us

            result = await submit_contact_us(
                uid="user-4",
                first_name="Jane",
                last_name="Doe",
                email="jane@example.com",
                subject="Partnership inquiry",
                message="We'd like to discuss integration options",
            )

            assert result["status"] == "ok"
            sid = result["submission_id"]

            from backend.envs import expdb
            doc = await expdb["contact_submissions"].find_one({"_id": sid})
            assert doc is not None
            assert doc["type"] == "contact"
            assert doc["first_name"] == "Jane"
            assert doc["email"] == "jane@example.com"

        _run(_test())

    def test_contact_us_emits_event(self):
        async def _test():
            from dorian.api.routes.contact import submit_contact_us
            from backend.events import aemit

            aemit.reset_mock()

            await submit_contact_us(
                uid="user-4",
                first_name="Jane",
                last_name="Doe",
                email="jane@example.com",
                subject="Partnership",
                message="Hello",
            )

            aemit.assert_called_once()
            event = aemit.call_args[0][0]
            assert event.type == "ContactFormSubmitted"
            assert event.data["type"] == "contact"

        _run(_test())


# ---------------------------------------------------------------------------
# 2. Slack notification handler — formats and dispatches correctly
# ---------------------------------------------------------------------------

class TestSlackContactNotification:
    """The slack_on_contact_form handler calls notify_contact_submission."""

    def test_slack_handler_for_bug(self):
        async def _test():
            from backend.events import Event
            from dorian.event.handlers.notifications import slack_on_contact_form

            with patch(
                "dorian.event.handlers.notifications.notify_contact_submission",
                new_callable=AsyncMock,
            ) as mock_notify:
                event = Event("ContactFormSubmitted", data={
                    "type": "bug",
                    "uid": "user-1",
                    "_id": "sub-123",
                    "title": "Crash on run",
                    "description": "Pipeline execution fails",
                    "severity": "critical",
                })

                await slack_on_contact_form(event)

                mock_notify.assert_called_once_with(
                    submission_type="bug",
                    uid="user-1",
                    submission_id="sub-123",
                    title="Crash on run",
                    subject="",
                    details="Pipeline execution fails",
                    severity="critical",
                    name="",
                )

        _run(_test())

    def test_slack_handler_for_feedback(self):
        async def _test():
            from backend.events import Event
            from dorian.event.handlers.notifications import slack_on_contact_form

            with patch(
                "dorian.event.handlers.notifications.notify_contact_submission",
                new_callable=AsyncMock,
            ) as mock_notify:
                event = Event("ContactFormSubmitted", data={
                    "type": "feedback",
                    "uid": "user-2",
                    "_id": "sub-456",
                    "subject": "Feature request",
                    "details": "Add export to PDF",
                })

                await slack_on_contact_form(event)

                mock_notify.assert_called_once_with(
                    submission_type="feedback",
                    uid="user-2",
                    submission_id="sub-456",
                    title="",
                    subject="Feature request",
                    details="Add export to PDF",
                    severity="",
                    name="",
                )

        _run(_test())

    def test_slack_handler_for_contact(self):
        async def _test():
            from backend.events import Event
            from dorian.event.handlers.notifications import slack_on_contact_form

            with patch(
                "dorian.event.handlers.notifications.notify_contact_submission",
                new_callable=AsyncMock,
            ) as mock_notify:
                event = Event("ContactFormSubmitted", data={
                    "type": "contact",
                    "uid": "user-3",
                    "_id": "sub-789",
                    "subject": "Hello",
                    "message": "Interested in your tool",
                })

                await slack_on_contact_form(event)

                mock_notify.assert_called_once_with(
                    submission_type="contact",
                    uid="user-3",
                    submission_id="sub-789",
                    title="",
                    subject="Hello",
                    details="Interested in your tool",
                    severity="",
                    name="",
                )

        _run(_test())


class TestSlackNotificationFormatter:
    """notify_contact_submission builds the right Slack payload."""

    def test_builds_bug_payload(self):
        async def _test():
            with patch("dorian.notifications.slack._webhook_url", return_value="https://hooks.slack.com/test"), \
                 patch("dorian.notifications.slack._notify_on", return_value=True), \
                 patch("dorian.notifications.slack.send_slack_message", new_callable=AsyncMock) as mock_send:

                from dorian.notifications.slack import notify_contact_submission

                await notify_contact_submission(
                    submission_type="bug",
                    uid="tester",
                    submission_id="abc",
                    title="Button broken",
                    details="Clicking run does nothing",
                    severity="high",
                )

                mock_send.assert_called_once()
                text, blocks = mock_send.call_args[0]
                assert "[BUG]" in text
                assert "tester" in text
                assert "Button broken" in text

        _run(_test())

    def test_truncates_long_details(self):
        async def _test():
            with patch("dorian.notifications.slack._webhook_url", return_value="https://hooks.slack.com/test"), \
                 patch("dorian.notifications.slack._notify_on", return_value=True), \
                 patch("dorian.notifications.slack.send_slack_message", new_callable=AsyncMock) as mock_send:

                from dorian.notifications.slack import notify_contact_submission

                long_details = "x" * 1000
                await notify_contact_submission(
                    submission_type="feedback",
                    uid="user",
                    submission_id="abc",
                    details=long_details,
                )

                mock_send.assert_called_once()
                _, blocks = mock_send.call_args[0]
                # Find the details block
                details_block = [
                    b for b in blocks
                    if b.get("text", {}).get("text", "").startswith("*Details:*")
                ]
                assert details_block
                # Should be truncated to 500 + "..."
                assert len(details_block[0]["text"]["text"]) < 600

        _run(_test())

    def test_noop_when_disabled(self):
        async def _test():
            with patch("dorian.notifications.slack._notify_on", return_value=False), \
                 patch("dorian.notifications.slack.send_slack_message", new_callable=AsyncMock) as mock_send:

                from dorian.notifications.slack import notify_contact_submission

                await notify_contact_submission(
                    submission_type="bug",
                    uid="user",
                    submission_id="abc",
                    title="test",
                )

                mock_send.assert_not_called()

        _run(_test())


# ---------------------------------------------------------------------------
# 3. Multiple submissions listed correctly
# ---------------------------------------------------------------------------

class TestSubmissionListing:
    """Submissions are independently persisted and retrievable."""

    def test_list_submissions_returns_all(self):
        async def _test():
            from dorian.api.routes.contact import submit_bug_report, submit_feedback

            await submit_bug_report(
                uid="user-1", title="Bug 1", description="desc", files=[],
            )
            await submit_feedback(
                uid="user-1", feedback_type="general", subject="Feedback 1", details="detail",
            )

            from backend.envs import expdb
            docs = []
            async for doc in expdb["contact_submissions"].find({"uid": "user-1"}):
                docs.append(doc)
            assert len(docs) == 2
            types = {d["type"] for d in docs}
            assert types == {"bug", "feedback"}

        _run(_test())


# ---------------------------------------------------------------------------
# 4. Event type registration
# ---------------------------------------------------------------------------

class TestEventTypeExists:
    """ContactFormSubmitted and GracefulShutdownRequested exist in EventType enum."""

    def test_contact_form_event_type(self):
        from dorian.event.types import EventType
        assert hasattr(EventType, "ContactFormSubmitted")
        assert EventType.ContactFormSubmitted == "ContactFormSubmitted"

    def test_graceful_shutdown_event_type(self):
        from dorian.event.types import EventType
        assert hasattr(EventType, "GracefulShutdownRequested")
        assert EventType.GracefulShutdownRequested == "GracefulShutdownRequested"


# ---------------------------------------------------------------------------
# 5. Event registry wiring — ContactFormSubmitted → slack_on_contact_form
# ---------------------------------------------------------------------------

class TestEventRegistryWiring:
    """ContactFormSubmitted is subscribed to slack_on_contact_form in the registry."""

    def test_contact_form_handler_is_importable(self):
        """Verify the handler exists and is importable from the notifications module."""
        from dorian.event.handlers.notifications import slack_on_contact_form
        assert callable(slack_on_contact_form)

    def test_event_type_in_registry_module(self):
        """Verify the registry module references ContactFormSubmitted."""
        import dorian.event.registry as reg
        import inspect
        source = inspect.getsource(reg)
        assert "ContactFormSubmitted" in source
        assert "slack_on_contact_form" in source
