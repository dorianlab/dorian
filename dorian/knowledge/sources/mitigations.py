"""Compatibility shim — the bulk of the catalog moved to
``dorian/knowledge/sources/mitigations.kb`` (loaded by the rust
parser into the snapshot).

What stays here:
  * ``MITIGATION_CATALOG``: empty dict for legacy lookups. The
    KB-backed lookup is the source of truth (``risk_debugger`` and
    ``risk_checks`` already prefer KB-supplied short/long templates
    and only fall back here when the KB is unreachable).
  * ``render_description``: minimal fallback renderer used by the
    ``risk_debugger`` / ``risk_checks`` "if kb_template is None"
    branches. Returns a generic-but-readable string so the suggestion
    UI doesn't show ``None``.
"""
from __future__ import annotations

# Empty by design — KB snapshot owns the actual descriptions. Kept
# as a name so legacy imports don't crash module load.
MITIGATION_CATALOG: dict = {}


def render_description(
    mitigation: str,
    *,
    operator: str = "",
    risk: str = "",
    task: str = "",
    alternatives: str = "",
    long: bool = False,
) -> str:
    """Last-resort fallback when the KB has no template for ``mitigation``.

    All format-time variables are accepted so callers can keep their
    keyword-only invocation pattern without branching.
    """
    if operator and risk:
        return f"Apply {mitigation} to address {risk} on {operator}."
    if risk:
        return f"Apply {mitigation} to address {risk}."
    return mitigation
