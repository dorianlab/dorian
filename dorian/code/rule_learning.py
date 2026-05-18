"""
dorian/code/rule_learning.py
----------------------------
Rule learning loop — proposes new rewrite rules from extraction context.

Uses an LLM to analyse the source code and auto-extracted DAG, then suggests
JSON rule specs that would improve the extraction. Each suggestion is compiled
and validated before being returned.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.events import Event, aemit


@dataclass
class RuleSuggestion:
    """A single suggested rewrite rule."""
    rule_id: str
    description: str
    spec: dict[str, Any]
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    draft_id: str | None = None


@dataclass
class SuggestRulesResult:
    """Result of an LLM rule suggestion request."""
    suggestion_id: str
    extraction_id: str
    rules: list[RuleSuggestion]
    reasoning: str


async def suggest_rules(
    extraction_id: str,
    code: str,
    auto_dag_json: dict[str, Any],
    rules_summary: str,
    feedback: str = "",
    ground_truth_diff: str = "",
    edit_path: dict[str, Any] | None = None,
    few_shots: dict[str, Any] | None = None,
    mode: str = "add",
) -> SuggestRulesResult:
    """Ask the LLM to suggest rewrite rules for an extraction.

    Parameters
    ----------
    extraction_id:
        The extraction this suggestion is for.
    code:
        The original Python source code.
    auto_dag_json:
        The auto-extracted DAG (JSON dict).
    rules_summary:
        Human-readable summary of the current rule set.
    feedback:
        Structural self-correction feedback from a previous failed attempt.
        Empty string on first attempt; populated by the retry loop.
    ground_truth_diff:
        Semantic diff between the auto-extracted DAG and a curated ground
        truth DAG. Empty string when no ground truth is available.

    Returns
    -------
    SuggestRulesResult
        Contains the suggested rules with compiled Python code and validation.
    """
    import asyncio
    from dorian.mcp.extraction import _get_responder, _parse_json_response
    from dorian.mcp.prompts import pick_prompt
    from dorian.mcp.rule_compiler import compile_rule
    from string import Template

    suggestion_id = str(uuid4())

    # Render the prompt — mode-specific preamble routed via pick_prompt
    edit_path_block = ""
    if edit_path and edit_path.get("ops"):
        edit_path_block = json.dumps(edit_path, indent=2)
    few_shots_block = ""
    if few_shots and (few_shots.get("positives") or few_shots.get("negatives")):
        few_shots_block = json.dumps({
            "positives": few_shots.get("positives") or [],
            "negatives": few_shots.get("negatives") or [],
        }, indent=2)
    prompt = Template(pick_prompt(mode)).safe_substitute(
        code=code,
        auto_dag=json.dumps(auto_dag_json, indent=2),
        rules_summary=rules_summary,
        ground_truth_diff=ground_truth_diff,
        edit_path=edit_path_block,
        few_shots=few_shots_block,
        feedback=feedback,
    )

    # Call LLM (sync responder → run in thread)
    responder = _get_responder()
    raw = await asyncio.to_thread(responder.invoke, prompt, max_tokens=8192)

    # Parse LLM response
    parsed = _parse_json_response(raw)
    if not isinstance(parsed, dict):
        await aemit(Event("RuleSuggestionParseError", {"error": "LLM returned unparseable response"}))
        return SuggestRulesResult(
            suggestion_id=suggestion_id,
            extraction_id=extraction_id,
            rules=[],
            reasoning="Failed to parse LLM response.",
        )

    reasoning = parsed.get("reasoning", "")
    raw_rules = parsed.get("rules", [])
    if not isinstance(raw_rules, list):
        raw_rules = []
    # Take only the first rule — user can request more iteratively
    raw_rules = raw_rules[:1]

    # Compile and validate each suggested rule
    from dorian.mcp.rule_schema import validate_rule_spec

    suggestions: list[RuleSuggestion] = []
    for i, spec in enumerate(raw_rules):
        if not isinstance(spec, dict):
            continue

        rule_id = str(uuid4())
        description = spec.get("description") or "LLM-suggested rule"

        # Schema validation (shape + safety) before semantic compilation
        validated, schema_errors = validate_rule_spec(spec)
        if schema_errors:
            suggestions.append(RuleSuggestion(
                rule_id=rule_id,
                description=description,
                spec=spec,
                valid=False,
                errors=schema_errors,
            ))
            continue

        # Try to compile (semantic validation)
        compiled, errors, warnings = compile_rule(validated)
        valid = compiled is not None and not errors

        suggestions.append(RuleSuggestion(
            rule_id=rule_id,
            description=description,
            spec=spec,
            valid=valid,
            errors=errors,
            warnings=warnings,
        ))

    await aemit(Event("RuleSuggestionsGenerated", {
        "extraction_id": extraction_id,
        "suggested": len(suggestions),
        "valid": sum(1 for s in suggestions if s.valid),
    }))

    return SuggestRulesResult(
        suggestion_id=suggestion_id,
        extraction_id=extraction_id,
        rules=suggestions,
        reasoning=reasoning,
    )


async def propose_rule(
    code: str,
    rules_version: str,
    auto_dag_json: dict[str, Any],
    corrected_dag_json: dict[str, Any],
) -> dict | None:
    """Propose a new rewrite rule from a correction pair.

    Parameters
    ----------
    code:
        The original source code that was extracted.
    rules_version:
        Content hash of the rule set that produced *auto_dag_json*.
    auto_dag_json:
        The auto-extracted DAG (JSON dict) from the original extraction.
    corrected_dag_json:
        The user-corrected DAG (JSON dict) submitted via "Submit Correction".

    Returns
    -------
    dict | None
        The JSON rule spec dict, or ``None`` if no valid rule could be
        proposed.
    """
    await aemit(Event("RuleLearningProposeRule", {
        "status": "stub",
        "rules_version": rules_version,
        "auto_nodes": len(auto_dag_json.get("nodes", {})),
        "corrected_nodes": len(corrected_dag_json.get("nodes", {})),
    }))

    result = await suggest_rules(
        extraction_id="correction",
        code=code,
        auto_dag_json=auto_dag_json,
        rules_summary=f"Rules version: {rules_version}",
    )

    if result.rules:
        for rule in result.rules:
            if rule.valid:
                return rule.spec
    return None
