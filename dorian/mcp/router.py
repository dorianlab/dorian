"""
dorian.mcp.router
------------------
FastAPI router that exposes the MCP toolkit as REST endpoints on the
existing Dorian backend.  No separate process needed — agents call
these over HTTP just like any other Dorian API.

Mount in ``main.py``::

    from dorian.mcp import router as mcp
    app.include_router(mcp.router)

All endpoints live under ``/mcp/...`` and return JSON.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Query

from dorian.mcp.draft_store import DraftStore

router = APIRouter(prefix="/mcp", tags=["mcp"])

# In-memory draft store — lives for the lifetime of the backend process.
_store = DraftStore()


# ═══════════════════════════════════════════════════════════════════════════
# KB endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/kb/query")
async def kb_query(
    cypher: str = Body(..., description="Read-only Cypher query"),
    parameters: dict[str, Any] = Body(default_factory=dict),
):
    """Execute a read-only Cypher query against the knowledge base."""
    from dorian.mcp.kb_tools import kb_query as _impl
    return _impl(cypher, parameters)


@router.get("/kb/risks")
async def kb_list_risks():
    """List all risks defined in the knowledge base."""
    from dorian.mcp.kb_tools import kb_list_risks as _impl
    return _impl()


@router.get("/kb/mitigations")
async def kb_list_mitigations():
    """List all mitigations with their risk mappings and rewrite annotations."""
    from dorian.mcp.kb_tools import kb_list_mitigations as _impl
    return _impl()


@router.get("/kb/operators")
async def kb_list_operators(task: str = Query(default="", description="Filter by task name")):
    """List operators, optionally filtered by task (e.g. 'Classification')."""
    from dorian.mcp.kb_tools import kb_list_operators as _impl
    return _impl(task=task or None)


@router.get("/kb/operator/{operator_name}/interface")
async def kb_get_operator_interface(operator_name: str):
    """Get the interface and method sequence for an operator."""
    from dorian.mcp.kb_tools import kb_get_operator_interface as _impl
    return _impl(operator_name)


@router.get("/kb/operator/{operator_name}/risks")
async def kb_risks_for_operator(operator_name: str):
    """Get all risks linked to a specific operator."""
    from dorian.mcp.kb_tools import kb_risks_for_operator as _impl
    return _impl(operator_name)


@router.get("/kb/risk/{risk_name}/mitigations")
async def kb_mitigations_for_risk(risk_name: str):
    """Get all mitigations for a specific risk."""
    from dorian.mcp.kb_tools import kb_mitigations_for_risk as _impl
    return _impl(risk_name)


@router.get("/kb/mitigation/{mitigation_name}/rewrite")
async def kb_rewrite_annotations(mitigation_name: str):
    """Get rewrite annotations for a mitigation."""
    from dorian.mcp.kb_tools import kb_rewrite_annotations as _impl
    return _impl(mitigation_name)


@router.get("/kb/search")
async def kb_search(keyword: str = Query(...), limit: int = Query(default=20)):
    """Search KB nodes by keyword (case-insensitive substring match)."""
    from dorian.mcp.kb_tools import kb_search_text as _impl
    return _impl(keyword, limit)


# ═══════════════════════════════════════════════════════════════════════════
# DAG endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/dag/inspect")
async def dag_inspect(pipeline: dict = Body(..., description="Pipeline DAG JSON")):
    """Parse and describe a pipeline DAG structure."""
    from dorian.mcp.dag_tools import dag_inspect as _impl
    return _impl(pipeline)


@router.post("/dag/diff")
async def dag_diff(
    before: dict = Body(..., description="Original DAG"),
    after: dict = Body(..., description="Modified DAG"),
):
    """Diff two DAG states — added/removed/modified nodes and edges."""
    from dorian.mcp.dag_tools import dag_diff as _impl
    return _impl(before, after)


@router.post("/dag/validate")
async def dag_validate(pipeline: dict = Body(..., description="Pipeline DAG JSON")):
    """Validate a pipeline DAG for structural issues."""
    from dorian.mcp.dag_tools import dag_validate as _impl
    return _impl(pipeline)


@router.post("/dag/match")
async def dag_match_pattern(
    pipeline: dict = Body(..., description="Pipeline DAG JSON"),
    pattern: dict = Body(..., description="Pattern spec (nodes + edges)"),
):
    """Test whether a pattern matches anywhere in a pipeline DAG."""
    from dorian.mcp.dag_tools import dag_match_pattern as _impl
    return _impl(pipeline, pattern)


@router.post("/dag/rewrite")
async def dag_apply_rewrite(
    pipeline: dict = Body(..., description="Pipeline DAG JSON"),
    rule_spec: dict = Body(..., description="JSON rule spec"),
):
    """Dry-run a rewrite rule on a DAG. Returns the transformed DAG and diff."""
    from dorian.mcp.dag_tools import dag_apply_rewrite as _impl
    return _impl(pipeline, rule_spec=rule_spec)


# ═══════════════════════════════════════════════════════════════════════════
# Rule authoring endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/rule/create")
async def rule_create(spec: dict = Body(..., description="JSON rule spec")):
    """Create a draft rewrite rule. Compiles immediately and returns validity."""
    from dorian.mcp.rule_tools import rule_create as _impl
    return _impl(_store, spec)


@router.put("/rule/{draft_id}")
async def rule_update(draft_id: str, spec: dict = Body(..., description="Updated rule spec")):
    """Update an existing draft rule with a new spec."""
    from dorian.mcp.rule_tools import rule_update as _impl
    return _impl(_store, draft_id, spec)


@router.post("/rule/{draft_id}/test")
async def rule_test(
    draft_id: str,
    test_dag: dict = Body(..., description="Pipeline DAG to test against"),
):
    """Test a draft rule against a sample pipeline DAG."""
    from dorian.mcp.rule_tools import rule_test as _impl
    return _impl(_store, draft_id, test_dag)


@router.get("/rule/drafts")
async def rule_list_drafts():
    """List all draft rules in the staging area."""
    from dorian.mcp.rule_tools import rule_list_drafts as _impl
    return _impl(_store)


@router.get("/rule/{draft_id}")
async def rule_get_draft(draft_id: str):
    """Get full details of a draft rule."""
    from dorian.mcp.rule_tools import rule_get_draft as _impl
    return _impl(_store, draft_id)


@router.delete("/rule/{draft_id}")
async def rule_delete_draft(draft_id: str):
    """Delete a draft rule."""
    from dorian.mcp.rule_tools import rule_delete_draft as _impl
    return _impl(_store, draft_id)


@router.get("/rule/active/list")
async def rule_list_active():
    """List all currently active rewrite rules."""
    from dorian.mcp.rule_tools import rule_list_active as _impl
    return _impl()


@router.post("/rule/{draft_id}/commit")
async def rule_commit(draft_id: str):
    """Commit a validated, tested draft rule to the active rule set."""
    from dorian.mcp.rule_tools import rule_commit as _impl
    return _impl(_store, draft_id)


# ═══════════════════════════════════════════════════════════════════════════
# Mitigation curation endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/mitigation/propose")
async def mitigation_propose(
    name: str = Body(...),
    short_description: str = Body(...),
    long_description_template: str = Body(...),
    risks: list[str] = Body(default_factory=list),
    provenance: dict | None = Body(default=None),
):
    """Propose a new mitigation action as a draft."""
    from dorian.mcp.mitigation_tools import mitigation_propose as _impl
    return _impl(
        _store, name, short_description, long_description_template,
        risks=risks or None,
        provenance=provenance,
    )


@router.put("/mitigation/{draft_id}/annotate")
async def mitigation_annotate(
    draft_id: str,
    rewrite_type: str = Body(default=""),
    rewrite_target: str = Body(default=""),
    rewrite_param: str = Body(default=""),
    rewrite_value: str = Body(default=""),
):
    """Annotate a draft mitigation with graph rewrite instructions."""
    from dorian.mcp.mitigation_tools import mitigation_annotate as _impl
    return _impl(
        _store, draft_id,
        rewrite_type=rewrite_type or None,
        rewrite_target=rewrite_target or None,
        rewrite_param=rewrite_param or None,
        rewrite_value=rewrite_value or None,
    )


@router.post("/mitigation/{draft_id}/test")
async def mitigation_test(
    draft_id: str,
    test_dag: dict = Body(..., description="Pipeline DAG to test against"),
    operator_fqn: str = Body(..., description="Operator FQN to apply mitigation to"),
):
    """Test a draft mitigation's rewrite on a sample pipeline."""
    from dorian.mcp.mitigation_tools import mitigation_test as _impl
    return _impl(_store, draft_id, test_dag, operator_fqn)


@router.get("/mitigation/drafts")
async def mitigation_list_drafts():
    """List all draft mitigations."""
    from dorian.mcp.mitigation_tools import mitigation_list_drafts as _impl
    return _impl(_store)


@router.get("/mitigation/{draft_id}")
async def mitigation_get_draft(draft_id: str):
    """Get full details of a draft mitigation."""
    from dorian.mcp.mitigation_tools import mitigation_get_draft as _impl
    return _impl(_store, draft_id)


@router.delete("/mitigation/{draft_id}")
async def mitigation_delete_draft(draft_id: str):
    """Delete a draft mitigation."""
    from dorian.mcp.mitigation_tools import mitigation_delete_draft as _impl
    return _impl(_store, draft_id)


@router.post("/mitigation/{draft_id}/commit")
async def mitigation_commit(draft_id: str):
    """Commit a draft mitigation to the knowledge base."""
    from dorian.mcp.mitigation_tools import mitigation_commit as _impl
    return _impl(_store, draft_id)


@router.get("/mitigation/search")
async def mitigation_search_similar(
    text: str = Query(...),
    limit: int = Query(default=10),
):
    """Search for existing KB mitigations similar to the given text."""
    from dorian.mcp.mitigation_tools import mitigation_search_similar as _impl
    return _impl(text, limit)


# ═══════════════════════════════════════════════════════════════════════════
# Extraction pipeline endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/extraction/decompose")
async def extraction_decompose(
    text: str = Body(..., description="Source text to decompose"),
    keyword: str = Body(default="", description="Optional keyword for context"),
):
    """Extract mitigation-relevant qualities from text (Stage 1: decompose)."""
    from dorian.mcp.mitigation_tools import mitigation_extract_from_text as _impl
    return _impl(text, keyword or None)


@router.post("/extraction/novelty")
async def extraction_classify_novelty(
    qualities: list[str] = Body(..., description="Quality strings to classify"),
    keyword: str = Body(default="", description="KB keyword for comparison"),
):
    """Classify qualities for novelty against the KB (Stages 2+3: similarity + novelty)."""
    from dorian.mcp.mitigation_tools import mitigation_classify_novelty as _impl
    return _impl(qualities, keyword or None)


@router.post("/extraction/triplets")
async def extraction_extract_triplets(
    text: str = Body(..., description="Text to extract triplets from"),
):
    """Extract knowledge graph triplets from text (Stage 4: triplet extraction)."""
    from dorian.mcp.mitigation_tools import mitigation_extract_triplets as _impl
    return _impl(text)


@router.post("/extraction/pipeline")
async def extraction_full_pipeline(
    text: str = Body(..., description="Source text"),
    keyword: str = Body(..., description="Search keyword"),
):
    """Run the full extraction pipeline (decompose → similarity → novelty → triplets)."""
    from dorian.mcp.extraction import run_extraction_pipeline as _impl
    # Fetch KB texts for similarity comparison
    from dorian.mcp.kb_tools import kb_search_text
    search = kb_search_text(keyword, limit=50)
    kb_texts = [r["name"] for r in search.get("results", [])]
    return _impl(text, keyword, kb_texts=kb_texts)


# ═══════════════════════════════════════════════════════════════════════════
# Drafts management
# ═══════════════════════════════════════════════════════════════════════════

@router.delete("/drafts")
async def drafts_clear():
    """Clear all draft rules and mitigations."""
    _store.clear()
    return {"cleared": True}


# ═══════════════════════════════════════════════════════════════════════════
# Schema / catalog resources (static reference data)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/schema/rule-spec")
async def schema_rule_spec():
    """JSON schema for the rule specification format."""
    return {
        "description": {
            "type": "string",
            "purpose": "Human-readable description of what this rule does",
        },
        "pattern": {
            "nodes": {
                "<id>": {
                    "type": "regex for node type (e.g. 'Operator', '.*')",
                    "text": "regex for node name (e.g. 'sklearn\\\\.svm\\\\.SVC')",
                    "language": "literal language match (e.g. 'python', '.*')",
                },
            },
            "edges": [
                {"source": "<node_id>", "destination": "<node_id>"},
            ],
        },
        "transformations": [
            {
                "type": "delete | update_attribute | replace_operator | add_parameter | insert_before | insert_after",
                "target": "<pattern_node_id>",
                "...": "type-specific fields",
            },
        ],
    }


@router.get("/schema/rewrite-types")
async def schema_rewrite_types():
    """Available rewrite types and their required fields."""
    return {
        "replace_operator": {
            "required": ["target", "new_name"],
            "example": {"type": "replace_operator", "target": "0", "new_name": "sklearn.preprocessing.RobustScaler"},
        },
        "add_parameter": {
            "required": ["target", "param_name", "param_value"],
            "example": {"type": "add_parameter", "target": "0", "param_name": "class_weight", "param_value": "balanced"},
        },
        "insert_before": {
            "required": ["target", "new_operator"],
            "example": {"type": "insert_before", "target": "0", "new_operator": "sklearn.ensemble.IsolationForest"},
        },
        "insert_after": {
            "required": ["target", "new_operator"],
            "example": {"type": "insert_after", "target": "0", "new_operator": "aif360.metrics.ClassificationMetric"},
        },
        "delete": {
            "required": ["nodes"],
            "example": {"type": "delete", "nodes": ["1"]},
        },
        "update_attribute": {
            "required": ["target", "attribute", "value"],
            "example": {"type": "update_attribute", "target": "0", "attribute": "text", "value": "new_value"},
            "value_expressions": {
                "literal": '"some_string"',
                "reference": '{"ref": "0", "attr": "text"}',
                "concat": '{"concat": ["prefix_", {"ref": "0", "attr": "text"}]}',
            },
        },
    }


@router.get("/catalog/mitigation-rewrites")
async def catalog_mitigation_rewrites():
    """Mitigation rewrite rules stored in ``doc_rewrites`` collection."""
    from backend.envs import expdb
    cursor = expdb.rewrites.find({}, {"_id": 1, "name": 1, "description": 1})
    return [doc async for doc in cursor]


# ═══════════════════════════════════════════════════════════════════════════
# Prompts (workflow guidance for agents)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/prompts/rule-authoring")
async def prompt_rule_authoring(context: str = Query(default="")):
    """Get the rule-authoring workflow prompt for an LLM agent."""
    from dorian.mcp.prompts import RULE_AUTHORING_PROMPT
    return {"prompt": RULE_AUTHORING_PROMPT.format(context=context)}


@router.get("/prompts/mitigation-curation")
async def prompt_mitigation_curation(context: str = Query(default="")):
    """Get the mitigation-curation workflow prompt for an LLM agent."""
    from dorian.mcp.prompts import MITIGATION_CURATION_PROMPT
    return {"prompt": MITIGATION_CURATION_PROMPT.format(context=context)}
