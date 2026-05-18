"""
dorian.mcp.draft_store
-----------------------
In-memory staging area for draft rules and mitigations.

Nothing touches the KB, Redis, or the active rule set until an explicit
``commit`` call.  This gives the LLM agent a safe sandbox to iterate:

    create draft → validate → test → revise → test again → commit

Design
------
- Drafts are ephemeral: they live only for the duration of the MCP server
  process.  Restarting the server clears all drafts.
- Each draft has a unique ID (short hex string).
- Draft rules carry both the JSON spec *and* the compiled ``RewriteRule``
  (or compilation errors).
- Draft mitigations carry the proposed KB entry *and* optional rewrite
  annotations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from dorian.dag import DAG


def _draft_id() -> str:
    return uuid4().hex[:12]


# ═══════════════════════════════════════════════════════════════════════════
# Draft Rule
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DraftRule:
    """A rule that has been created but not yet committed to the active set."""
    id: str
    spec: dict                              # the JSON rule spec from the agent
    description: str = ""
    compiled: object | None = None          # RewriteRule if compilation succeeded
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    test_results: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_valid(self) -> bool:
        return self.compiled is not None and not self.errors


# ═══════════════════════════════════════════════════════════════════════════
# Draft Mitigation
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Provenance:
    """Where a mitigation idea came from."""
    source_type: str = ""           # "paper" | "policy" | "url" | "user_idea" | "llm_generated"
    source_ref: str = ""            # URL, DOI, document path
    source_title: str = ""
    source_excerpt: str = ""        # the specific passage that inspired this
    extracted_by: str = ""          # LLM model ID
    confidence: float = 0.0


@dataclass
class DraftMitigation:
    """A mitigation proposal that has not yet been committed to the KB."""
    id: str
    name: str
    short_description: str
    long_description_template: str
    risks: list[str] = field(default_factory=list)
    provenance: Provenance | None = None

    # Rewrite annotation (filled via mitigation/annotate)
    rewrite_type: str | None = None         # replace_operator | add_parameter | insert_before | insert_after
    rewrite_target: str | None = None
    rewrite_param: str | None = None
    rewrite_value: str | None = None

    # Validation state
    validation: dict = field(default_factory=dict)
    test_results: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def has_rewrite(self) -> bool:
        return self.rewrite_type is not None


# ═══════════════════════════════════════════════════════════════════════════
# Store
# ═══════════════════════════════════════════════════════════════════════════

class DraftStore:
    """In-memory staging area for draft rules and mitigations."""

    def __init__(self) -> None:
        self.rules: dict[str, DraftRule] = {}
        self.mitigations: dict[str, DraftMitigation] = {}

    # ── Rules ──────────────────────────────────────────────────────────

    def create_rule(self, spec: dict) -> DraftRule:
        draft = DraftRule(id=_draft_id(), spec=spec, description=spec.get("description", ""))
        self.rules[draft.id] = draft
        return draft

    def get_rule(self, draft_id: str) -> DraftRule | None:
        return self.rules.get(draft_id)

    def remove_rule(self, draft_id: str) -> None:
        self.rules.pop(draft_id, None)

    # ── Mitigations ───────────────────────────────────────────────────

    def create_mitigation(
        self,
        name: str,
        short_description: str,
        long_description_template: str,
        risks: list[str] | None = None,
        provenance: dict | None = None,
    ) -> DraftMitigation:
        prov = None
        if provenance:
            prov = Provenance(**{k: v for k, v in provenance.items() if k in Provenance.__dataclass_fields__})

        draft = DraftMitigation(
            id=_draft_id(),
            name=name,
            short_description=short_description,
            long_description_template=long_description_template,
            risks=risks or [],
            provenance=prov,
        )
        self.mitigations[draft.id] = draft
        return draft

    def get_mitigation(self, draft_id: str) -> DraftMitigation | None:
        return self.mitigations.get(draft_id)

    def remove_mitigation(self, draft_id: str) -> None:
        self.mitigations.pop(draft_id, None)

    # ── Bulk ──────────────────────────────────────────────────────────

    def list_rules(self) -> list[dict]:
        return [
            {"id": d.id, "description": d.description, "valid": d.is_valid, "errors": d.errors}
            for d in self.rules.values()
        ]

    def list_mitigations(self) -> list[dict]:
        return [
            {
                "id": d.id, "name": d.name, "short_description": d.short_description,
                "risks": d.risks, "has_rewrite": d.has_rewrite,
            }
            for d in self.mitigations.values()
        ]

    def clear(self) -> None:
        self.rules.clear()
        self.mitigations.clear()
