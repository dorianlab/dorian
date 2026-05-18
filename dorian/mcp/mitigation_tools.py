"""
dorian.mcp.mitigation_tools
-----------------------------
Mitigation curation tools for the MCP server.

These tools let LLM agents propose, annotate, test, and commit mitigation
actions. They also wrap the extraction pipeline stages (decompose, similarity,
novelty, triplets) from ``dorian.mcp.extraction``.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from dorian.dag import DAG
from dorian.mcp.draft_store import DraftStore, DraftMitigation
from dorian.mcp.dag_tools import dag_inspect, dag_diff, _parse_dag
from backend.events import Event, emit


# ═══════════════════════════════════════════════════════════════════════════
# Mitigation lifecycle tools
# ═══════════════════════════════════════════════════════════════════════════

def mitigation_propose(
    store: DraftStore,
    name: str,
    short_description: str,
    long_description_template: str,
    risks: list[str] | None = None,
    provenance: dict | None = None,
) -> dict:
    """Propose a new mitigation action as a draft.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    name : str
        Short mitigation name (e.g. "Feature Drift Detection").
    short_description : str
        One-line summary.
    long_description_template : str
        Full description with {operator}, {risk}, {task} placeholders.
    risks : list[str], optional
        KB risk names this mitigates (e.g. ["Class Imbalance", "Sampling Bias"]).
    provenance : dict, optional
        Source info: source_type, source_ref, source_title, source_excerpt,
        extracted_by, confidence.

    Returns
    -------
    dict
        ``{"draft_id": "...", "name": "...", "status": "proposed"}``
    """
    draft = store.create_mitigation(
        name=name,
        short_description=short_description,
        long_description_template=long_description_template,
        risks=risks,
        provenance=provenance,
    )
    return {
        "draft_id": draft.id,
        "name": draft.name,
        "short_description": draft.short_description,
        "risks": draft.risks,
        "has_provenance": draft.provenance is not None,
        "status": "proposed",
    }


def mitigation_annotate(
    store: DraftStore,
    draft_id: str,
    rewrite_type: str | None = None,
    rewrite_target: str | None = None,
    rewrite_param: str | None = None,
    rewrite_value: str | None = None,
) -> dict:
    """Annotate a draft mitigation with graph rewrite instructions.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft mitigation ID.
    rewrite_type : str
        One of: replace_operator, add_parameter, insert_before, insert_after.
    rewrite_target : str, optional
        FQN of the new/replacement operator.
    rewrite_param : str, optional
        Parameter name (for add_parameter).
    rewrite_value : str, optional
        Parameter value (for add_parameter).

    Returns
    -------
    dict
        Updated draft summary.
    """
    draft = store.get_mitigation(draft_id)
    if draft is None:
        return {"error": f"Draft mitigation '{draft_id}' not found"}

    valid_types = {"replace_operator", "add_parameter", "insert_before", "insert_after"}
    if rewrite_type and rewrite_type not in valid_types:
        return {"error": f"Invalid rewrite_type '{rewrite_type}'. Must be one of: {valid_types}"}

    # Validate required fields per type
    if rewrite_type == "replace_operator" and not rewrite_target:
        return {"error": "replace_operator requires rewrite_target"}
    if rewrite_type == "add_parameter" and (not rewrite_param or not rewrite_value):
        return {"error": "add_parameter requires rewrite_param and rewrite_value"}
    if rewrite_type in ("insert_before", "insert_after") and not rewrite_target:
        return {"error": f"{rewrite_type} requires rewrite_target"}

    draft.rewrite_type = rewrite_type
    draft.rewrite_target = rewrite_target
    draft.rewrite_param = rewrite_param
    draft.rewrite_value = rewrite_value

    return {
        "draft_id": draft.id,
        "name": draft.name,
        "rewrite_type": draft.rewrite_type,
        "rewrite_target": draft.rewrite_target,
        "rewrite_param": draft.rewrite_param,
        "rewrite_value": draft.rewrite_value,
        "has_rewrite": draft.has_rewrite,
        "status": "annotated",
    }


def mitigation_test(
    store: DraftStore,
    draft_id: str,
    test_dag_json: str | dict,
    operator_fqn: str,
) -> dict:
    """Test a draft mitigation's rewrite on a sample pipeline.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft mitigation ID.
    test_dag_json : str | dict
        The pipeline DAG to test against.
    operator_fqn : str
        The FQN of the operator to apply the mitigation to.

    Returns
    -------
    dict
        Test result with before/after diff.
    """
    from dorian.pipeline.mitigation_rewrites import compile_rewrite_rule
    from dorian.pipeline.transforms import sync_apply

    draft = store.get_mitigation(draft_id)
    if draft is None:
        return {"success": False, "error": f"Draft mitigation '{draft_id}' not found"}

    if not draft.has_rewrite:
        return {
            "success": True,
            "diagnostic_only": True,
            "message": f"'{draft.name}' is a diagnostic-only mitigation (no rewrite annotation). "
                       "It will show as an instruction/toast, not a graph transformation.",
        }

    try:
        dag = _parse_dag(test_dag_json)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse test DAG: {e}"}

    # Build rewrite function from the draft's JSON-spec rule doc
    rule_doc = draft.to_rewrite_doc() if hasattr(draft, "to_rewrite_doc") else None
    if rule_doc is None:
        return {"success": False, "error": "Draft has no serialised rewrite rule"}

    try:
        rule = compile_rewrite_rule(rule_doc, operator_fqn)
        result_dag = sync_apply(rule, dag, {})
    except Exception as e:
        return {"success": False, "error": f"Rewrite failed: {e}"}

    before_dict = dag.to_json_dict()
    after_dict = result_dag.to_json_dict()
    diff = dag_diff(before_dict, after_dict)

    # Record test result
    test_result = {
        "operator": operator_fqn,
        "diff_summary": diff.get("summary", ""),
        "applied": diff.get("summary", "") != "No changes",
    }
    draft.test_results.append(test_result)

    return {
        "success": True,
        "applied": test_result["applied"],
        "before": dag_inspect(before_dict),
        "after": dag_inspect(after_dict),
        "diff": diff,
        "result_pipeline_json": after_dict,
    }


def mitigation_list_drafts(store: DraftStore) -> dict:
    """List all draft mitigations.

    Returns
    -------
    dict
        ``{"drafts": [{"id": "...", "name": "...", ...}, ...]}``
    """
    return {"drafts": store.list_mitigations()}


def mitigation_get_draft(store: DraftStore, draft_id: str) -> dict:
    """Get detailed information about a specific draft mitigation.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft mitigation ID.

    Returns
    -------
    dict
        Full draft details.
    """
    draft = store.get_mitigation(draft_id)
    if draft is None:
        return {"error": f"Draft mitigation '{draft_id}' not found"}

    result = {
        "id": draft.id,
        "name": draft.name,
        "short_description": draft.short_description,
        "long_description_template": draft.long_description_template,
        "risks": draft.risks,
        "rewrite_type": draft.rewrite_type,
        "rewrite_target": draft.rewrite_target,
        "rewrite_param": draft.rewrite_param,
        "rewrite_value": draft.rewrite_value,
        "has_rewrite": draft.has_rewrite,
        "test_results": draft.test_results,
        "created_at": draft.created_at,
    }
    if draft.provenance:
        result["provenance"] = asdict(draft.provenance)
    return result


def mitigation_delete_draft(store: DraftStore, draft_id: str) -> dict:
    """Delete a draft mitigation.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft mitigation ID to delete.

    Returns
    -------
    dict
        ``{"deleted": True}`` or ``{"error": "..."}``
    """
    if store.get_mitigation(draft_id) is None:
        return {"error": f"Draft mitigation '{draft_id}' not found"}

    store.remove_mitigation(draft_id)
    return {"deleted": True, "draft_id": draft_id}


def mitigation_commit(store: DraftStore, draft_id: str) -> dict:
    """Commit a draft mitigation to the knowledge base.

    Writes the mitigation node, descriptions, risk mappings, and rewrite
    annotations to Neo4j using the same predicate structure as
    ``dorian/knowledge/sources/mitigations.py``.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft mitigation ID to commit.

    Returns
    -------
    dict
        ``{"committed": True, "name": "...", "statements_written": N}``
        or ``{"error": "..."}``
    """
    draft = store.get_mitigation(draft_id)
    if draft is None:
        return {"error": f"Draft mitigation '{draft_id}' not found"}

    if not draft.risks:
        return {"error": "Mitigation must have at least one associated risk. Use mitigation/annotate to set risks."}

    # Build KB knowledge text (same format as mitigations.py)
    statements: list[str] = []

    # Node + descriptions
    statements.append(f"{draft.name} is a Mitigation")
    statements.append(f"{draft.name} with description {draft.short_description}")
    statements.append(f'{draft.name} with long description "{draft.long_description_template}"')

    # Risk mappings
    for risk in draft.risks:
        statements.append(f"{draft.name} might mitigate {risk}")

    # Parameter signature (ontological predicates — rule body lives in the docstore)
    if draft.rewrite_param:
        statements.append(f"{draft.name} has parameter {draft.rewrite_param}")
        statements.append(f"{draft.rewrite_param} is of type str")

    # Write to the postgres overlay (``expdb.kb_overlay``). Each
    # statement lands as ``proposed`` and waits for validation
    # before joining the next snapshot build. Curated promotion to
    # a ``sources/*.py`` file is a separate (manual) step.
    try:
        import asyncio
        asyncio.run(_commit_to_kb(
            statements,
            uid=getattr(draft, "author_uid", None),
            session=getattr(draft, "session", None),
            draft_id=draft_id,
        ))
    except Exception as e:
        return {"error": f"KB overlay commit failed: {e}"}

    name = draft.name
    count = len(statements)
    store.remove_mitigation(draft_id)

    emit(Event("MitigationCommitted", {"name": name, "statements": count}))

    # KB was mutated — emit KBChanged so the catalog cache rebuilds and
    # all connected clients receive fresh catalogs immediately.
    emit(Event("KBChanged", {"source": "mitigation_commit", "name": name}))

    return {
        "committed": True,
        "name": name,
        "statements_written": count,
        "statements": statements,
    }


async def _commit_to_kb(
    statements: list[str],
    *,
    uid: str | None = None,
    session: str | None = None,
    draft_id: str | None = None,
) -> None:
    """Persist statements to the postgres KB overlay.

    Statements land as ``proposed`` rows; the validation lifecycle
    (see ``dorian/knowledge/overlay.py``) decides when they enter
    the next snapshot build. Same DSL as
    ``dorian/knowledge/sources/*.py`` so the snapshot's predicate
    parser handles them unchanged.
    """
    from dorian.knowledge import overlay

    await overlay.add_statements(
        statements,
        namespace="core",
        tool="mcp",
        uid=uid,
        session=session,
        draft_id=draft_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Extraction pipeline tools (wrapping dorian.mcp.extraction)
# ═══════════════════════════════════════════════════════════════════════════

def mitigation_extract_from_text(text: str, keyword: str | None = None) -> dict:
    """Extract mitigation-relevant qualities from a text passage.

    Wraps Stage 1 (decompose) of the extraction pipeline.

    Parameters
    ----------
    text : str
        Source text (document passage, article, user idea).
    keyword : str, optional
        Optional keyword for context.

    Returns
    -------
    dict
        ``{"qualities": [...], "count": N, "keyword": "..."}``
    """
    from dorian.mcp.extraction import decompose_text

    qualities = decompose_text(text)
    return {
        "qualities": qualities,
        "count": len(qualities),
        "keyword": keyword or "",
    }


def mitigation_search_similar(text: str, limit: int = 10) -> dict:
    """Search for existing KB mitigations similar to the given text.

    Uses the KB keyword search + optional vector similarity.

    Parameters
    ----------
    text : str
        The text to search for.
    limit : int
        Maximum results.

    Returns
    -------
    dict
        ``{"results": [{"name": "...", "similarity": ...}, ...]}``
    """
    from dorian.mcp.kb_tools import kb_search_text

    # Simple keyword search against KB
    words = text.lower().split()[:3]  # take first 3 words
    results = []
    for word in words:
        if len(word) > 3:
            r = kb_search_text(word, limit=limit)
            results.extend(r.get("results", []))

    # Deduplicate
    seen = set()
    unique = []
    for r in results:
        if r["name"] not in seen:
            seen.add(r["name"])
            unique.append(r)
            if len(unique) >= limit:
                break

    return {"query": text, "results": unique}


def mitigation_classify_novelty(
    qualities: list[str],
    keyword: str | None = None,
) -> dict:
    """Classify extracted qualities for novelty against the existing KB.

    Wraps Stages 2 (similarity) + 3 (novelty) of the extraction pipeline.

    Parameters
    ----------
    qualities : list[str]
        Quality statements to classify.
    keyword : str, optional
        KB keyword for retrieving comparison texts.

    Returns
    -------
    dict
        ``{"results": [{"quality": "...", "decision": "NEW"|"EXISTING"|"PARTIALLY_NEW", ...}, ...]}``
    """
    from dorian.mcp.extraction import filter_by_similarity, classify_novelty_batch
    from dorian.mcp.kb_tools import kb_search_text

    # Get KB texts for comparison
    kb_texts = []
    if keyword:
        search_result = kb_search_text(keyword, limit=50)
        kb_texts = [r["name"] for r in search_result.get("results", [])]

    # Stage 2: Similarity filter
    sim_results = filter_by_similarity(qualities, kb_texts)

    # Stage 3: Novelty classification (only for kept qualities)
    novelty_inputs = [
        (sr.quality, sr.neighbors)
        for sr in sim_results
        if sr.kept
    ]
    novelty_results = classify_novelty_batch(novelty_inputs) if novelty_inputs else []

    # Combine results
    combined = []
    novelty_map = {nr.quality: nr for nr in novelty_results}

    for sr in sim_results:
        entry = {
            "quality": sr.quality,
            "max_similarity": sr.max_score,
            "kept_after_filter": sr.kept,
            "top_neighbors": sr.neighbors[:3],
        }
        if sr.quality in novelty_map:
            nr = novelty_map[sr.quality]
            entry.update({
                "decision": nr.decision,
                "rationale": nr.rationale,
                "confidence": nr.confidence,
            })
        else:
            entry["decision"] = "SKIPPED" if not sr.kept else "UNCLASSIFIED"

        combined.append(entry)

    return {
        "total": len(qualities),
        "kept": len(novelty_inputs),
        "results": combined,
    }


def mitigation_extract_triplets(text: str) -> dict:
    """Extract knowledge graph triplets from text.

    Wraps Stage 4 (triplet extraction) of the extraction pipeline.

    Parameters
    ----------
    text : str
        Source text to extract triplets from.

    Returns
    -------
    dict
        ``{"triplets": [{"subject": "...", "predicate": "...", "object": "..."}, ...], "count": N}``
    """
    from dorian.mcp.extraction import extract_triplets

    triplets = extract_triplets(text)
    return {
        "triplets": [
            {"subject": t.subject, "predicate": t.predicate, "object": t.object}
            for t in triplets
        ],
        "count": len(triplets),
    }
