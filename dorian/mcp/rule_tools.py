"""
dorian.mcp.rule_tools
----------------------
Rewrite rule authoring tools for the MCP server.

These tools give LLM agents the ability to create, test, iterate, and commit
DAG rewrite rules using the JSON rule spec format (no eval, no lambdas).
"""
from __future__ import annotations

import json
from typing import Any

from dorian.dag import DAG
from dorian.mcp.draft_store import DraftStore, DraftRule
from dorian.mcp.rule_compiler import compile_rule
from dorian.mcp.dag_tools import dag_inspect, dag_diff, _parse_dag
from dorian.pipeline.transforms import sync_apply



# ═══════════════════════════════════════════════════════════════════════════
# Tool implementations
# ═══════════════════════════════════════════════════════════════════════════

def rule_create(store: DraftStore, spec: dict) -> dict:
    """Create a draft rewrite rule from a JSON spec.

    The spec is compiled immediately. If compilation succeeds, the draft
    is valid and ready for testing. If it fails, the errors are returned
    so the agent can fix the spec and retry.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    spec : dict
        JSON rule spec (see ``dorian.mcp.rule_compiler`` for format).

    Returns
    -------
    dict
        ``{"draft_id": "...", "valid": bool, "errors": [...], "warnings": [...],
          "description": "..."}``
    """
    draft = store.create_rule(spec)

    # Compile
    rule, errors, warnings = compile_rule(spec)
    draft.compiled = rule
    draft.errors = errors
    draft.warnings = warnings

    return {
        "draft_id": draft.id,
        "valid": draft.is_valid,
        "errors": draft.errors,
        "warnings": draft.warnings,
        "description": draft.description,
    }


def rule_update(store: DraftStore, draft_id: str, spec: dict) -> dict:
    """Update an existing draft rule with a new spec.

    Re-compiles the spec. Clears previous test results.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft rule ID to update.
    spec : dict
        The new JSON rule spec.

    Returns
    -------
    dict
        Same shape as ``rule_create``.
    """
    draft = store.get_rule(draft_id)
    if draft is None:
        return {"error": f"Draft rule '{draft_id}' not found"}

    # Update spec and re-compile
    draft.spec = spec
    draft.description = spec.get("description", draft.description)
    draft.test_results = []  # clear old test results

    rule, errors, warnings = compile_rule(spec)
    draft.compiled = rule
    draft.errors = errors
    draft.warnings = warnings

    return {
        "draft_id": draft.id,
        "valid": draft.is_valid,
        "errors": draft.errors,
        "warnings": draft.warnings,
        "description": draft.description,
    }


def rule_test(store: DraftStore, draft_id: str, test_dag_json: str | dict) -> dict:
    """Test a draft rule against a sample pipeline DAG.

    Applies the rule and returns before/after diff so the agent can verify
    the transformation is correct.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft rule ID to test.
    test_dag_json : str | dict
        The pipeline DAG to test against.

    Returns
    -------
    dict
        ``{"success": bool, "matched": bool, "before": {...}, "after": {...},
          "diff": {...}, "error": "..."}``
    """
    draft = store.get_rule(draft_id)
    if draft is None:
        return {"success": False, "error": f"Draft rule '{draft_id}' not found"}

    if not draft.is_valid:
        return {"success": False, "error": f"Draft rule has compilation errors: {draft.errors}"}

    try:
        dag = _parse_dag(test_dag_json)
    except Exception as e:
        return {"success": False, "error": f"Failed to parse test DAG: {e}"}

    # Apply the rule
    try:
        result_dag = sync_apply(draft.compiled, dag, {})
    except Exception as e:
        return {"success": False, "error": f"Rule application failed: {e}"}

    before_dict = dag.to_json_dict()
    after_dict = result_dag.to_json_dict()
    diff = dag_diff(before_dict, after_dict)

    # Check if any transformation actually happened
    matched = diff.get("summary", "") != "No changes"

    # Record test result
    test_result = {
        "matched": matched,
        "diff_summary": diff.get("summary", ""),
        "node_count_before": len(dag.nodes),
        "node_count_after": len(result_dag.nodes),
    }
    draft.test_results.append(test_result)

    return {
        "success": True,
        "matched": matched,
        "before": dag_inspect(before_dict),
        "after": dag_inspect(after_dict),
        "diff": diff,
        "result_pipeline_json": after_dict,
    }


def rule_list_drafts(store: DraftStore) -> dict:
    """List all draft rules in the store.

    Returns
    -------
    dict
        ``{"drafts": [{"id": "...", "description": "...", "valid": bool, ...}, ...]}``
    """
    return {"drafts": store.list_rules()}


def rule_get_draft(store: DraftStore, draft_id: str) -> dict:
    """Get detailed information about a specific draft rule.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft rule ID.

    Returns
    -------
    dict
        Full draft details including spec, errors, warnings, and test results.
    """
    draft = store.get_rule(draft_id)
    if draft is None:
        return {"error": f"Draft rule '{draft_id}' not found"}

    return {
        "id": draft.id,
        "description": draft.description,
        "valid": draft.is_valid,
        "spec": draft.spec,
        "errors": draft.errors,
        "warnings": draft.warnings,
        "test_results": draft.test_results,
        "created_at": draft.created_at,
    }


def rule_delete_draft(store: DraftStore, draft_id: str) -> dict:
    """Delete a draft rule.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft rule ID to delete.

    Returns
    -------
    dict
        ``{"deleted": True}`` or ``{"error": "..."}``
    """
    if store.get_rule(draft_id) is None:
        return {"error": f"Draft rule '{draft_id}' not found"}

    store.remove_rule(draft_id)
    return {"deleted": True, "draft_id": draft_id}


def rule_list_active() -> dict:
    """List currently active rewrite rules from the code-parsing module.

    Returns
    -------
    dict
        ``{"rules": [{"description": "...", "pattern_types": [...], "transformations": [...]}, ...]}``
    """
    from dorian.code.parsing.rules import _rules

    active = []
    for rule in _rules:
        pattern_types = []
        for node in rule.pattern.nodes.values():
            if hasattr(node, "type"):
                pattern_types.append(getattr(node, "type", ""))

        active.append({
            "description": rule.description,
            "pattern_types": pattern_types,
            "transformation_count": len(rule.transformations),
            "transformation_types": [t.__class__.__name__ for t in rule.transformations],
        })

    return {"rules": active, "count": len(active)}


def rule_commit(store: DraftStore, draft_id: str) -> dict:
    """Commit a draft rule to the active rule set.

    The rule is added to the code-parsing rules list so it will be applied
    during future pipeline transformations.

    Parameters
    ----------
    store : DraftStore
        The draft store instance.
    draft_id : str
        The draft rule ID to commit.

    Returns
    -------
    dict
        ``{"committed": True, "description": "..."}`` or ``{"error": "..."}``
    """
    draft = store.get_rule(draft_id)
    if draft is None:
        return {"error": f"Draft rule '{draft_id}' not found"}

    if not draft.is_valid:
        return {"error": f"Cannot commit invalid rule. Errors: {draft.errors}"}

    if not draft.test_results:
        return {"error": "Rule has not been tested. Use rule/test before committing."}

    # Check that at least one test showed a match
    any_matched = any(tr.get("matched") for tr in draft.test_results)
    if not any_matched:
        return {
            "error": "No test showed a pattern match. The rule may be too restrictive. "
                     "Test with a DAG that contains matching nodes before committing."
        }

    # Add to the active rule set
    from dorian.code.parsing.rules import _rules
    _rules.append(draft.compiled)

    description = draft.description
    store.remove_rule(draft_id)

    return {
        "committed": True,
        "description": description,
        "active_rule_count": len(_rules),
    }
