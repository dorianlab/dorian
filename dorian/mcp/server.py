"""
dorian.mcp.server
------------------
MCP server for LLM agent access to the Dorian rewrite rule system.

Start the server::

    python -m dorian.mcp.server          # stdio transport (local agents)
    python -m dorian.mcp.server --http   # streamable HTTP (remote agents)

The server exposes four tool namespaces via the Model Context Protocol:

    kb/*          — Knowledge base queries (risks, mitigations, operators)
    dag/*         — DAG inspection, diffing, validation, dry-run rewrites
    rule/*        — Rewrite rule authoring (create, test, commit)
    mitigation/*  — Mitigation curation pipeline (ingest, extract, propose, test, commit)

And two workflow prompts:

    rule-authoring      — Guided rule creation workflow
    mitigation-curation — Guided mitigation extraction + commit workflow
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from dorian.mcp.stores import draft_store as _store
from dorian.mcp.draft_store import DraftStore
from backend.events import Event, emit

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Server instance + shared state
# ═══════════════════════════════════════════════════════════════════════════

mcp = FastMCP(
    "dorian-mcp",
    instructions=(
        "Dorian MCP server — provides tools for authoring DAG rewrite rules "
        "and curating mitigation actions for trustworthy ML pipelines. "
        "Use kb/* tools to query the knowledge base, dag/* tools to inspect "
        "and transform pipeline graphs, rule/* tools to create and test "
        "rewrite rules, and mitigation/* tools to curate mitigation actions."
    ),
)


# ═══════════════════════════════════════════════════════════════════════════
# KB Tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def kb_query(cypher: str, parameters: str = "{}") -> str:
    """Execute a read-only Cypher query against the Dorian knowledge base.

    Use this for ad-hoc KB exploration. Only MATCH/RETURN queries are allowed.
    Parameters should be a JSON string of named params, e.g. '{"name": "SVC"}'.
    """
    from dorian.mcp.kb_tools import kb_query as _kb_query
    params = json.loads(parameters) if isinstance(parameters, str) else parameters
    return json.dumps(_kb_query(cypher, params), indent=2)


@mcp.tool()
def kb_list_risks() -> str:
    """List all risks defined in the knowledge base."""
    from dorian.mcp.kb_tools import kb_list_risks as _impl
    return json.dumps(_impl(), indent=2)


@mcp.tool()
def kb_list_mitigations() -> str:
    """List all mitigations with their risk mappings and rewrite annotations."""
    from dorian.mcp.kb_tools import kb_list_mitigations as _impl
    return json.dumps(_impl(), indent=2)


@mcp.tool()
def kb_list_operators(task: str = "") -> str:
    """List operators in the KB. Optionally filter by task (e.g. 'Classification')."""
    from dorian.mcp.kb_tools import kb_list_operators as _impl
    return json.dumps(_impl(task=task or None), indent=2)


@mcp.tool()
def kb_get_operator_interface(operator_name: str) -> str:
    """Get the interface and method sequence for an operator (e.g. 'sklearn.svm.SVC')."""
    from dorian.mcp.kb_tools import kb_get_operator_interface as _impl
    return json.dumps(_impl(operator_name), indent=2)


@mcp.tool()
def kb_risks_for_operator(operator_name: str) -> str:
    """Get all risks linked to a specific operator."""
    from dorian.mcp.kb_tools import kb_risks_for_operator as _impl
    return json.dumps(_impl(operator_name), indent=2)


@mcp.tool()
def kb_mitigations_for_risk(risk_name: str) -> str:
    """Get all mitigations for a specific risk (e.g. 'Class Imbalance')."""
    from dorian.mcp.kb_tools import kb_mitigations_for_risk as _impl
    return json.dumps(_impl(risk_name), indent=2)


@mcp.tool()
def kb_rewrite_annotations(mitigation_name: str) -> str:
    """Get the rewrite annotations (type, target, param, value) for a mitigation."""
    from dorian.mcp.kb_tools import kb_rewrite_annotations as _impl
    return json.dumps(_impl(mitigation_name), indent=2)


@mcp.tool()
def kb_search(keyword: str, limit: int = 20) -> str:
    """Search the KB for nodes whose name contains the keyword (case-insensitive)."""
    from dorian.mcp.kb_tools import kb_search_text as _impl
    return json.dumps(_impl(keyword, limit), indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# DAG Tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def dag_inspect(pipeline_json: str) -> str:
    """Parse and describe a pipeline DAG structure.

    Accepts the JSON format produced by DAG.to_json_dict(). Returns node/edge
    details, statistics, and topological info (roots, leaves).
    """
    from dorian.mcp.dag_tools import dag_inspect as _impl
    data = json.loads(pipeline_json) if isinstance(pipeline_json, str) else pipeline_json
    return json.dumps(_impl(data), indent=2)


@mcp.tool()
def dag_diff(before_json: str, after_json: str) -> str:
    """Diff two DAG states. Returns added/removed/modified nodes and edges."""
    from dorian.mcp.dag_tools import dag_diff as _impl
    before = json.loads(before_json) if isinstance(before_json, str) else before_json
    after = json.loads(after_json) if isinstance(after_json, str) else after_json
    return json.dumps(_impl(before, after), indent=2)


@mcp.tool()
def dag_validate(pipeline_json: str) -> str:
    """Validate a pipeline DAG for structural issues (dangling edges, self-loops, orphans)."""
    from dorian.mcp.dag_tools import dag_validate as _impl
    data = json.loads(pipeline_json) if isinstance(pipeline_json, str) else pipeline_json
    return json.dumps(_impl(data), indent=2)


@mcp.tool()
def dag_match_pattern(pipeline_json: str, pattern_json: str) -> str:
    """Test whether a pattern matches anywhere in a pipeline DAG.

    pattern_json follows the same format as the 'pattern' field in a rule spec.
    """
    from dorian.mcp.dag_tools import dag_match_pattern as _impl
    pipeline = json.loads(pipeline_json) if isinstance(pipeline_json, str) else pipeline_json
    pattern = json.loads(pattern_json) if isinstance(pattern_json, str) else pattern_json
    return json.dumps(_impl(pipeline, pattern), indent=2)


@mcp.tool()
def dag_apply_rewrite(pipeline_json: str, rule_spec_json: str) -> str:
    """Dry-run a rewrite rule on a pipeline DAG.

    Returns the transformed DAG and a diff. Does NOT modify the active pipeline.
    rule_spec_json is a JSON rule spec (same format as rule/create).
    """
    from dorian.mcp.dag_tools import dag_apply_rewrite as _impl
    pipeline = json.loads(pipeline_json) if isinstance(pipeline_json, str) else pipeline_json
    rule_spec = json.loads(rule_spec_json) if isinstance(rule_spec_json, str) else rule_spec_json
    return json.dumps(_impl(pipeline, rule_spec=rule_spec), indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Rule Authoring Tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def rule_create(spec_json: str) -> str:
    """Create a draft rewrite rule from a JSON spec.

    The spec is compiled immediately. Returns the draft ID, validity status,
    and any compilation errors/warnings. See the rule-authoring prompt for
    the full spec format.
    """
    from dorian.mcp.rule_tools import rule_create as _impl
    spec = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
    return json.dumps(_impl(_store, spec), indent=2)


@mcp.tool()
def rule_update(draft_id: str, spec_json: str) -> str:
    """Update an existing draft rule with a new spec. Re-compiles and clears test results."""
    from dorian.mcp.rule_tools import rule_update as _impl
    spec = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
    return json.dumps(_impl(_store, draft_id, spec), indent=2)


@mcp.tool()
def rule_test(draft_id: str, test_dag_json: str) -> str:
    """Test a draft rule against a sample pipeline DAG.

    Returns before/after diff so you can verify the transformation.
    """
    from dorian.mcp.rule_tools import rule_test as _impl
    dag = json.loads(test_dag_json) if isinstance(test_dag_json, str) else test_dag_json
    return json.dumps(_impl(_store, draft_id, dag), indent=2)


@mcp.tool()
def rule_list_drafts() -> str:
    """List all draft rules in the staging area."""
    from dorian.mcp.rule_tools import rule_list_drafts as _impl
    return json.dumps(_impl(_store), indent=2)


@mcp.tool()
def rule_get_draft(draft_id: str) -> str:
    """Get full details of a draft rule (spec, errors, test results)."""
    from dorian.mcp.rule_tools import rule_get_draft as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


@mcp.tool()
def rule_delete_draft(draft_id: str) -> str:
    """Delete a draft rule from the staging area."""
    from dorian.mcp.rule_tools import rule_delete_draft as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


@mcp.tool()
def rule_list_active() -> str:
    """List all currently active rewrite rules (from the code-parsing module)."""
    from dorian.mcp.rule_tools import rule_list_active as _impl
    return json.dumps(_impl(), indent=2)


@mcp.tool()
def rule_commit(draft_id: str) -> str:
    """Commit a validated, tested draft rule to the active rule set.

    The rule must have been tested (rule/test) with at least one matching result.
    """
    from dorian.mcp.rule_tools import rule_commit as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Mitigation Curation Tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def mitigation_propose(
    name: str,
    short_description: str,
    long_description_template: str,
    risks_json: str = "[]",
    provenance_json: str = "{}",
) -> str:
    """Propose a new mitigation action as a draft.

    risks_json: JSON array of risk names, e.g. '["Class Imbalance", "Sampling Bias"]'
    provenance_json: JSON object with source info (source_type, source_ref, source_title, etc.)
    long_description_template: Use {operator}, {risk}, {task} as template placeholders.
    """
    from dorian.mcp.mitigation_tools import mitigation_propose as _impl
    risks = json.loads(risks_json) if isinstance(risks_json, str) else risks_json
    provenance = json.loads(provenance_json) if isinstance(provenance_json, str) else provenance_json
    return json.dumps(_impl(
        _store, name, short_description, long_description_template,
        risks=risks or None,
        provenance=provenance or None,
    ), indent=2)


@mcp.tool()
def mitigation_annotate(
    draft_id: str,
    rewrite_type: str = "",
    rewrite_target: str = "",
    rewrite_param: str = "",
    rewrite_value: str = "",
) -> str:
    """Annotate a draft mitigation with graph rewrite instructions.

    rewrite_type: replace_operator | add_parameter | insert_before | insert_after
    rewrite_target: FQN of new/replacement operator (for replace/insert types)
    rewrite_param: Parameter name (for add_parameter)
    rewrite_value: Parameter value (for add_parameter)
    """
    from dorian.mcp.mitigation_tools import mitigation_annotate as _impl
    return json.dumps(_impl(
        _store, draft_id,
        rewrite_type=rewrite_type or None,
        rewrite_target=rewrite_target or None,
        rewrite_param=rewrite_param or None,
        rewrite_value=rewrite_value or None,
    ), indent=2)


@mcp.tool()
def mitigation_test(draft_id: str, test_dag_json: str, operator_fqn: str) -> str:
    """Test a draft mitigation's rewrite on a sample pipeline.

    operator_fqn: The operator to apply the mitigation to (e.g. 'sklearn.svm.SVC').
    """
    from dorian.mcp.mitigation_tools import mitigation_test as _impl
    dag = json.loads(test_dag_json) if isinstance(test_dag_json, str) else test_dag_json
    return json.dumps(_impl(_store, draft_id, dag, operator_fqn), indent=2)


@mcp.tool()
def mitigation_list_drafts() -> str:
    """List all draft mitigations in the staging area."""
    from dorian.mcp.mitigation_tools import mitigation_list_drafts as _impl
    return json.dumps(_impl(_store), indent=2)


@mcp.tool()
def mitigation_get_draft(draft_id: str) -> str:
    """Get full details of a draft mitigation."""
    from dorian.mcp.mitigation_tools import mitigation_get_draft as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


@mcp.tool()
def mitigation_delete_draft(draft_id: str) -> str:
    """Delete a draft mitigation from the staging area."""
    from dorian.mcp.mitigation_tools import mitigation_delete_draft as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


@mcp.tool()
def mitigation_commit(draft_id: str) -> str:
    """Commit a draft mitigation to the knowledge base.

    Writes the mitigation node, descriptions, risk mappings, and rewrite
    annotations to Neo4j.
    """
    from dorian.mcp.mitigation_tools import mitigation_commit as _impl
    return json.dumps(_impl(_store, draft_id), indent=2)


@mcp.tool()
def mitigation_search_similar(text: str, limit: int = 10) -> str:
    """Search for existing KB mitigations similar to the given text."""
    from dorian.mcp.mitigation_tools import mitigation_search_similar as _impl
    return json.dumps(_impl(text, limit), indent=2)


@mcp.tool()
def mitigation_extract_from_text(text: str, keyword: str = "") -> str:
    """Extract mitigation-relevant qualities from a text passage.

    Stage 1 of the extraction pipeline: decomposes text into atomic
    quality statements about ML pipeline trustworthiness.
    """
    from dorian.mcp.mitigation_tools import mitigation_extract_from_text as _impl
    return json.dumps(_impl(text, keyword or None), indent=2)


@mcp.tool()
def mitigation_classify_novelty(qualities_json: str, keyword: str = "") -> str:
    """Classify extracted qualities for novelty against the existing KB.

    qualities_json: JSON array of quality strings from extract_from_text.
    Returns EXISTING, PARTIALLY_NEW, or NEW for each quality.
    """
    from dorian.mcp.mitigation_tools import mitigation_classify_novelty as _impl
    qualities = json.loads(qualities_json) if isinstance(qualities_json, str) else qualities_json
    return json.dumps(_impl(qualities, keyword or None), indent=2)


@mcp.tool()
def mitigation_extract_triplets(text: str) -> str:
    """Extract knowledge graph triplets (subject-predicate-object) from text.

    Useful for turning mitigation descriptions into KB-ready relationships.
    """
    from dorian.mcp.mitigation_tools import mitigation_extract_triplets as _impl
    return json.dumps(_impl(text), indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Draft Store Management
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def drafts_clear() -> str:
    """Clear all draft rules and mitigations from the staging area."""
    _store.clear()
    return json.dumps({"cleared": True})


# ═══════════════════════════════════════════════════════════════════════════
# MCP Prompts
# ═══════════════════════════════════════════════════════════════════════════

@mcp.prompt()
def rule_authoring(context: str = "") -> str:
    """Guided workflow for creating DAG rewrite rules."""
    from dorian.mcp.prompts import RULE_AUTHORING_PROMPT
    return RULE_AUTHORING_PROMPT.format(context=context)


@mcp.prompt()
def mitigation_curation(context: str = "") -> str:
    """Guided workflow for extracting, classifying, and committing mitigation actions."""
    from dorian.mcp.prompts import MITIGATION_CURATION_PROMPT
    return MITIGATION_CURATION_PROMPT.format(context=context)


# ═══════════════════════════════════════════════════════════════════════════
# MCP Resources (read-only state)
# ═══════════════════════════════════════════════════════════════════════════

@mcp.resource("dorian://schema/rule-spec")
def rule_spec_schema() -> str:
    """JSON schema for the rule specification format."""
    return json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Dorian Rewrite Rule Spec",
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "description": {"type": "string"},
            "pattern": {
                "type": "object",
                "required": ["nodes"],
                "properties": {
                    "nodes": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "description": "Regex for node type"},
                                "text": {"type": "string", "description": "Regex for node text/name"},
                                "language": {"type": "string", "description": "Literal language match"},
                            },
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["source", "destination"],
                            "properties": {
                                "source": {"type": "string"},
                                "destination": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "transformations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "delete", "update_attribute", "replace_operator",
                                "add_parameter", "insert_before", "insert_after",
                                "add_edges", "add_edge", "redirect_edge",
                                "to_operator", "to_parameter",
                            ],
                        },
                    },
                },
            },
        },
    }, indent=2)


@mcp.resource("dorian://catalog/rewrite-types")
def rewrite_types_catalog() -> str:
    """Available rewrite types and their required fields."""
    return json.dumps({
        "replace_operator": {
            "description": "Swap an operator's fully-qualified name",
            "required": ["target", "new_name"],
            "example": {"type": "replace_operator", "target": "0", "new_name": "sklearn.preprocessing.RobustScaler"},
        },
        "add_parameter": {
            "description": "Add a keyword parameter to an operator",
            "required": ["target", "param_name", "param_value"],
            "example": {"type": "add_parameter", "target": "0", "param_name": "class_weight", "param_value": "balanced"},
        },
        "insert_before": {
            "description": "Insert a new operator upstream of the target",
            "required": ["target", "new_operator"],
            "example": {"type": "insert_before", "target": "0", "new_operator": "sklearn.ensemble.IsolationForest"},
        },
        "insert_after": {
            "description": "Insert a new operator downstream of the target",
            "required": ["target", "new_operator"],
            "example": {"type": "insert_after", "target": "0", "new_operator": "aif360.metrics.ClassificationMetric"},
        },
        "delete": {
            "description": "Remove matched nodes from the DAG",
            "required": ["nodes"],
            "example": {"type": "delete", "nodes": ["1"]},
        },
        "update_attribute": {
            "description": "Change a node attribute (type, text, language)",
            "required": ["target", "attribute", "value"],
            "example": {"type": "update_attribute", "target": "0", "attribute": "text", "value": "new_value"},
        },
    }, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# Session Tools — live session plug-in via short-lived token
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def session_info(token: str) -> str:
    """Return metadata for the session the token is bound to.

    Use this as the first call after issuing a token to confirm
    connectivity and see what extraction is active.

    Args:
      token: short-lived hex token issued via the frontend
             "Connect MCP" button.
    """
    from dorian.mcp.session_tools import session_info as _impl
    from dorian.mcp.token import McpAuthError
    try:
        return json.dumps(_impl(token), indent=2, default=str)
    except McpAuthError as e:
        return json.dumps({"error": "auth_failed", "detail": str(e)})


@mcp.tool()
def session_read_extraction(token: str) -> str:
    """Read the full active extraction for this session:
    code, auto_dag, corrected_dag, rules_snapshot, metadata.

    Use this to see what the user is working on. The returned
    corrected_dag is the user's hand-edit when present — treat that
    as the target your rule proposals should converge toward.

    Args:
      token: short-lived hex token.
    """
    from dorian.mcp.session_tools import session_read_extraction as _impl
    from dorian.mcp.token import McpAuthError
    try:
        return json.dumps(_impl(token), indent=2, default=str)
    except McpAuthError as e:
        return json.dumps({"error": "auth_failed", "detail": str(e)})


@mcp.tool()
def session_read_rules(token: str) -> str:
    """Return the user's ordered json_specs rules list.

    Each entry is ``{position, spec}``. Position 0 is applied first.
    Use this to understand the current rule set before proposing an
    insertion.

    Args:
      token: short-lived hex token.
    """
    from dorian.mcp.session_tools import session_read_rules as _impl
    from dorian.mcp.token import McpAuthError
    try:
        return json.dumps(_impl(token), indent=2, default=str)
    except McpAuthError as e:
        return json.dumps({"error": "auth_failed", "detail": str(e)})


@mcp.tool()
def rule_persist_to_session(
    token: str,
    spec: str,
    insert_at: int = -1,
    rationale: str = "",
    skip_compat_check: bool = False,
) -> str:
    """Commit a rule to the user's ``json_specs`` list.

    Validates the spec schema, compiles to a RewriteRule, runs a
    backward-compat replay against the extraction corpus, then (if
    clean) persists a new rules version and emits
    ``extraction/rules-updated`` so the UI refreshes.

    Args:
      token: short-lived hex token.
      spec: JSON string of the rule spec. Schema defined in
            dorian/mcp/rule_schema.py::RuleSpec.
      insert_at: position in the rules list (0-indexed). ``-1`` or
                 omitted appends at the end.
      rationale: short human-readable note persisted alongside the
                 rule. Shows up in telemetry + the audit log.
      skip_compat_check: when True, override a backward-compat
                         regression. Audit-logged. Default False.
    """
    from dorian.mcp.session_tools import rule_persist_to_session as _impl
    from dorian.mcp.token import McpAuthError
    try:
        spec_dict = json.loads(spec) if isinstance(spec, str) else spec
    except json.JSONDecodeError as e:
        return json.dumps({"status": "bad_spec_json", "error": str(e)})
    at = None if insert_at is None or insert_at < 0 else insert_at
    try:
        return json.dumps(
            _impl(token, spec_dict, at, rationale, skip_compat_check),
            indent=2, default=str,
        )
    except McpAuthError as e:
        return json.dumps({"error": "auth_failed", "detail": str(e)})


@mcp.tool()
def dry_run_rule(
    spec: str,
    dag_json: str,
    target_dag_json: str = "",
) -> str:
    """Compile + apply a rule spec to a DAG non-terminally.

    Returns ``{ok, changed, new_dag, diff, ged_before, ged_after,
    warnings}``. Does NOT persist anything. Safe to call repeatedly
    while iterating on a candidate.

    Args:
      spec: JSON rule spec string.
      dag_json: the DAG to apply the rule to (JSON string).
      target_dag_json: optional target DAG — when provided, GED is
                       measured against it so you can see whether
                       your candidate moves closer to the goal.
    """
    from dorian.mcp.agent_tools import dry_run_rule as _impl
    try:
        spec_dict = json.loads(spec) if isinstance(spec, str) else spec
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"bad spec json: {e}"})
    target = target_dag_json.strip() if target_dag_json else None
    return json.dumps(_impl(spec_dict, dag_json, target_dag_json=target), indent=2, default=str)


@mcp.tool()
def graph_edit_path(dag_a_json: str, dag_b_json: str, max_ops: int = 200) -> str:
    """Minimum-signal edit path turning dag_a into dag_b.

    Returns ``{ops: [...], strategy: "id_diff"|"astar_exact"|"name_diff",
    truncated: bool}``. Prefer this over raw distance when diagnosing a
    wrong extraction — the op sequence tells you exactly what to fix.

    Args:
      dag_a_json: starting DAG (e.g. auto_dag).
      dag_b_json: target DAG (e.g. corrected_dag).
      max_ops: cap on the returned op list (default 200).
    """
    from dorian.mcp.dag_tools import graph_edit_path as _impl
    return json.dumps(_impl(dag_a_json, dag_b_json, max_ops=max_ops), indent=2, default=str)


@mcp.resource("dorian://catalog/mitigation-rewrites")
def mitigation_rewrite_catalog() -> str:
    """Mitigation rewrite rules stored in the ``expdb.rewrites`` collection."""
    import asyncio
    from backend.envs import expdb

    async def _fetch():
        cursor = expdb.rewrites.find({}, {"_id": 1, "name": 1, "description": 1})
        return [doc async for doc in cursor]

    docs = asyncio.get_event_loop().run_until_complete(_fetch())
    return json.dumps(docs, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# RL-prior tools
# ═══════════════════════════════════════════════════════════════════════════
#
# External MCP clients (Claude Code, custom orchestrators) use these
# to bias the RL trainer's action mask without the trainer making
# outbound LLM calls. Flow:
#
#   1. Client calls ``rl_dataset_profile(name)`` to see what the
#      trainer measured about the dataset.
#   2. Client decides on a handful of catalog op_keys the agent
#      should prefer.
#   3. Client calls ``rl_prior_recommend(name, operators, reason)``
#      to push those into the trainer's per-episode prior queue.
#
# The trainer's MCPPriorSource reads the queue on the next
# ``env.reset()`` for that dataset name and folds the op_keys into
# the mask as ``suggestion_weight`` multipliers. If no client has
# injected anything, the queue stays empty and recommendations
# degrade to no-op. The same MCP server can advertise tools for
# mitigation curation, KB queries, etc. without conflicting.
#


@mcp.tool()
def rl_dataset_profile(dataset_name: str) -> str:
    """Return the measured profile of the dataset the RL trainer
    last observed for ``dataset_name``. The profile is computed
    from the actual CSV at episode reset — measured row count,
    numeric vs categorical column split, null fraction, class
    imbalance, etc. Use this before calling ``rl_prior_recommend``
    so your recommendations reflect what the trainer actually saw.

    Returns JSON; empty ``{}`` when the trainer hasn't profiled
    this dataset yet in the current session.
    """
    from rl.priors.mcp_source import get_shared_mcp_source
    source = get_shared_mcp_source()
    profile = source.get_profile(dataset_name)
    if profile is None:
        return "{}"
    return json.dumps(profile.to_prompt_dict(), indent=2)


@mcp.tool()
def rl_prior_recommend(
    dataset_name: str,
    operators: str,
    reason: str = "",
    weight: float = 5.0,
) -> str:
    """Recommend catalog op_keys for the next RL episode on
    ``dataset_name``. The trainer's mask will multiply each
    matching AddNode candidate's ``suggestion_weight`` by ``weight``
    so the policy's weighted draw prefers the recommended ops.

    Arguments:
      - ``dataset_name``: which dataset the recommendation applies to
        (matches the CC18Dataset.name field the trainer uses).
      - ``operators``: JSON array of exact catalog op_key strings.
        Invalid op_keys are silently dropped by the mask — call
        ``kb_list_operators`` first if you're unsure what's available.
      - ``reason``: short human-readable explanation (logged for
        observability; doesn't affect policy behaviour).
      - ``weight``: suggestion_weight multiplier. Clamped to
        [1.0, 20.0] by the source. 5.0 is neutral-confidence.

    Entries are consumed on the next episode reset — to persist a
    recommendation across multiple episodes, call this tool again
    after each reset.
    """
    from rl.priors.base import PriorRecommendation
    from rl.priors.mcp_source import get_shared_mcp_source
    try:
        ops = json.loads(operators) if isinstance(operators, str) else operators
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"operators must be JSON array: {exc}"})
    if not isinstance(ops, list):
        return json.dumps({"error": "operators must be a JSON array"})
    weight = max(1.0, min(20.0, float(weight)))
    recs = [
        PriorRecommendation(op_key=str(op), reason=reason, weight=weight)
        for op in ops if op
    ]
    get_shared_mcp_source().inject(dataset_name, recs)
    return json.dumps({
        "injected": len(recs),
        "dataset": dataset_name,
        "weight": weight,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="Dorian MCP Server")
    parser.add_argument(
        "--http", action="store_true",
        help="Use streamable HTTP transport instead of stdio",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="HTTP port (only with --http)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if args.http:
        emit(Event("McpServerStarting", {"transport": "http", "port": args.port}))
        mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        emit(Event("McpServerStarting", {"transport": "stdio"}))
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
