"""
dorian.mcp.kb_tools
--------------------
Knowledge base query tools for the MCP server.

Read-only access for LLM agents over the Dorian KB. Routed through
``dorian.knowledge.queries`` (rust snapshot) and the Postgres
``kb_overlay`` collection for LLM-curated additions; no direct
Neo4j access.

Generic Cypher (``kb_query``) was retired with the rust port —
agents now use the structured tools below. Each tool returns a
JSON-shaped dict matching the previous Neo4j wire format so MCP
prompt templates don't need rewiring.
"""
from __future__ import annotations

from typing import Any

from dorian.knowledge import queries as Q
from dorian.knowledge.ontology_kb import OntologyKB, load_kb


# ═══════════════════════════════════════════════════════════════════
# Snapshot-backed in-memory KB
# ═══════════════════════════════════════════════════════════════════
#
# The MCP tools want predicate-level queries (``with_description``,
# ``might_introduce``, …) the rust snapshot doesn't expose directly.
# We back them with the same ``OntologyKB`` the snapshot exporter
# uses: cheap to build (~hundreds of ms) and lru-cached for the
# process lifetime.

_kb_cache: OntologyKB | None = None


def _kb_ontology() -> OntologyKB:
    global _kb_cache
    if _kb_cache is None:
        _kb_cache = load_kb()
    return _kb_cache


def _node_description(kb: OntologyKB, node: str) -> str | None:
    """Return ``with_description`` text (chain-walked) or ``None``."""
    for v in kb.adj[node].get("with_description", []):
        return kb.display(v)
    return None


def _node_long_description(kb: OntologyKB, node: str) -> str | None:
    for v in kb.adj[node].get("with_long_description", []):
        return kb.display(v)
    return None


# ═══════════════════════════════════════════════════════════════════
# Tool implementations
# ═══════════════════════════════════════════════════════════════════

def kb_query(cypher: str, parameters: dict[str, Any] | None = None) -> dict:  # noqa: ARG001
    """Generic-Cypher endpoint — retired with the Neo4j retirement.

    Returns an explanatory error so existing prompts surface the
    breakage instead of silently returning empty results. Use the
    structured tools (``kb_list_risks``, ``kb_list_mitigations``,
    ``kb_list_operators``, ``kb_get_operator_interface``,
    ``kb_risks_for_operator``, ``kb_mitigations_for_risk``,
    ``kb_search_text``, ``kb_rewrite_annotations``).
    """
    return {
        "error": (
            "Generic Cypher queries are no longer supported — Neo4j "
            "was retired in favour of the in-memory KB snapshot. "
            "Use the structured tools (kb_list_*, kb_get_*) instead."
        ),
    }


def kb_list_risks() -> dict:
    """List all risks in the KB."""
    kb = _kb_ontology()
    out = []
    for n in sorted(kb.adj):
        if "Risk" not in kb.adj[n].get("is_a", []):
            continue
        out.append({
            "name": n,
            "description": _node_description(kb, n),
        })
    return {"risks": out}


def kb_list_mitigations() -> dict:
    """List all mitigations with their risk mappings + rewrite type."""
    kb = _kb_ontology()
    out = []
    for n in sorted(kb.adj):
        if "Mitigation" not in kb.adj[n].get("is_a", []):
            continue
        risks = sorted(set(kb.adj[n].get("might_mitigate", [])))
        # ``with_rewrite_type`` isn't a curated predicate today —
        # MCP-curated rewrite annotations land in the postgres
        # overlay and are exposed via ``kb_rewrite_annotations``.
        out.append({
            "name": n,
            "description": _node_description(kb, n),
            "risks": risks,
            "rewrite_type": None,
        })
    return {"mitigations": out}


def kb_list_operators(task: str | None = None) -> dict:
    """List operators, optionally filtered by ``performs`` task."""
    operators = Q.get_all_operators()
    if task:
        keep = set(Q.get_operators_for_task(task))
        rows = [
            {
                "name": op["name"],
                "interface": op.get("interface"),
                "task": task,
                "family": op.get("family"),
            }
            for op in operators
            if op["name"] in keep
        ]
    else:
        rows = [
            {
                "name": op["name"],
                "interface": op.get("interface"),
                "tasks": list(op.get("tasks") or []),
                "family": op.get("family"),
            }
            for op in operators
        ]
    rows.sort(key=lambda r: r["name"])
    return {"operators": rows}


def kb_get_operator_interface(operator_name: str) -> dict:
    """Interface + method sequence for an operator."""
    iface = Q.get_operator_interface(operator_name)
    methods = Q.get_method_sequence(iface) if iface else []
    return {
        "operator": operator_name,
        "interface": iface,
        "methods": methods,
    }


def kb_risks_for_operator(operator_name: str) -> dict:
    """Risks an operator ``might_introduce`` (with descriptions)."""
    kb = _kb_ontology()
    risks: list[dict] = []
    for r in kb.adj.get(operator_name, {}).get("might_introduce", []):
        risks.append({
            "name": r,
            "description": _node_description(kb, r),
        })
    risks.sort(key=lambda x: x["name"])
    return {"operator": operator_name, "risks": risks}


def kb_mitigations_for_risk(risk_name: str) -> dict:
    """Mitigations targeting a risk (with descriptions)."""
    kb = _kb_ontology()
    mits: list[dict] = []
    for m, dsts in kb.adj.items():
        if risk_name in dsts.get("might_mitigate", []) and \
                "Mitigation" in dsts.get("is_a", []):
            mits.append({
                "name": m,
                "description": _node_description(kb, m),
                "rewrite_type": None,
            })
    mits.sort(key=lambda x: x["name"])
    return {"risk": risk_name, "mitigations": mits}


def kb_rewrite_annotations(mitigation_name: str) -> dict:
    """Overlay statements attached to a mitigation.

    Reads from ``expdb.kb_overlay`` (the postgres overlay) — every
    DSL statement whose subject is ``mitigation_name``. Callers
    that need the *executable* rule body should still consult
    ``expdb.rewrites``; the overlay carries ontological metadata
    (descriptions, risk mappings, parameter signatures) curated
    via MCP / UI / API.
    """
    import asyncio
    from dorian.knowledge import overlay

    rows = asyncio.run(overlay.find_for_subject(mitigation_name))
    return {
        "mitigation": mitigation_name,
        "statements": [
            {
                "statement": r["statement"],
                "status": r["validation"]["status"],
                "namespace": r.get("namespace"),
                "source": r.get("source"),
            }
            for r in rows
        ],
    }


def kb_search_text(keyword: str, limit: int = 20) -> dict:
    """Substring search over node names.

    Skips synthetic crawler-generated ports (``__crawled__...``)
    and pure UUID intermediaries — agents always want named entities.
    """
    kb = _kb_ontology()
    needle = keyword.lower()
    matches: list[dict] = []
    for n in sorted(kb.adj):
        if n.startswith("__crawled__"):
            continue
        if len(n) == 32 and n.replace("-", "").isalnum() and n.islower():
            # uuid hex — skip
            continue
        if needle not in n.lower():
            continue
        labels: list[str] = []
        for label in (
            "Risk", "Mitigation", "Operator", "Interface",
            "ModelFamily", "Pathway",
        ):
            if label in kb.adj[n].get("is_a", []) or label in kb.adj[n].get("is_an", []):
                labels.append(label)
        matches.append({"name": n, "labels": labels})
        if len(matches) >= limit:
            break
    return {"keyword": keyword, "results": matches}
