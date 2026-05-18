"""Public platform routes — no auth, served to every user.

``/stats`` is the welcome-screen's counter block (datasets, pipelines,
sessions, …). Previously mounted under ``/admin/stats`` for historical
reasons, which was confusing because it's not an admin-only endpoint —
every signed-in user sees these numbers on their first screen.

This module exposes the same handler under ``/stats`` AND keeps the
legacy ``/admin/stats`` working via ``admin.py`` so bookmarks and
front-end clients that still call the old path don't break during the
rename.
"""
from __future__ import annotations

from fastapi import APIRouter

from dorian.api.routes import admin as _admin

router = APIRouter(tags=["platform"])


@router.get("/stats")
async def stats():
    """Aggregate platform counts for the welcome screen.

    Thin delegator to the same handler the legacy ``/admin/stats``
    endpoint serves. See ``dorian.api.routes.admin.platform_stats``
    for the underlying implementation (cached, timeout-bounded,
    per-counter fail-open).
    """
    return await _admin.platform_stats()
