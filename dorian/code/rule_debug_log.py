"""
dorian/code/rule_debug_log.py
------------------------------
Structured debug log for the rule suggestion flow.

Writes one JSON object per event to ``data/rule_debug.jsonl``.
Each entry captures a stage of the suggestion pipeline so the full
context is available in one place for post-mortem debugging.

Temporary — remove once the suggestion flow is stable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "local" / "rule_debug.jsonl"


def _write(entry: dict) -> None:
    """Append a JSON line to the debug log file."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        _log.debug("Failed to write rule debug log", exc_info=True)


def log_suggestion_start(
    extraction_id: str,
    uid: str,
    session: str,
    auto_dag: dict,
    code_len: int,
) -> str:
    """Log the start of a suggestion request. Returns a run_id for correlation."""
    run_id = f"{extraction_id}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    _write({
        "run_id": run_id,
        "stage": "start",
        "ts": datetime.now(timezone.utc).isoformat(),
        "extraction_id": extraction_id,
        "uid": uid,
        "session": session,
        "code_len": code_len,
        "auto_dag": {
            "node_count": len(auto_dag.get("nodes", {})),
            "edge_count": len(auto_dag.get("edges", [])),
            "nodes": {
                nid: _summarize_node(n)
                for nid, n in auto_dag.get("nodes", {}).items()
            },
            "edges": [
                {"src": e.get("source"), "dst": e.get("destination")}
                for e in auto_dag.get("edges", [])
            ],
        },
    })
    return run_id


def log_llm_response(
    run_id: str,
    reasoning: str,
    rule_count: int,
    raw_spec: dict | None,
) -> None:
    """Log the LLM response and the rule spec it produced."""
    _write({
        "run_id": run_id,
        "stage": "llm_response",
        "ts": datetime.now(timezone.utc).isoformat(),
        "reasoning": reasoning,
        "rule_count": rule_count,
        "spec": raw_spec,
    })


def log_validation(
    run_id: str,
    valid: bool,
    schema_errors: list[str] | None = None,
    compile_errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Log schema + compile validation results."""
    _write({
        "run_id": run_id,
        "stage": "validation",
        "ts": datetime.now(timezone.utc).isoformat(),
        "valid": valid,
        "schema_errors": schema_errors or [],
        "compile_errors": compile_errors or [],
        "warnings": warnings or [],
    })


def log_test_result(
    run_id: str,
    draft_id: str,
    matched: bool,
    diff_summary: str,
    edges_before: int,
    edges_after: int,
    to_remove: list | None = None,
    dag_edges: list | None = None,
) -> None:
    """Log the rule_test result — did the rule match and change the DAG?"""
    _write({
        "run_id": run_id,
        "stage": "test",
        "ts": datetime.now(timezone.utc).isoformat(),
        "draft_id": draft_id,
        "matched": matched,
        "diff_summary": diff_summary,
        "edges_before": edges_before,
        "edges_after": edges_after,
        "to_remove": to_remove,
        "dag_edges_sample": dag_edges[:10] if dag_edges else [],
    })


def log_retry(
    run_id: str,
    attempt: int,
    error_type: str,
    diff_summary: str | None = None,
    failed_spec: dict | None = None,
) -> None:
    """Log a structural self-correction retry (pattern didn't match the DAG)."""
    _write({
        "run_id": run_id,
        "stage": "retry",
        "ts": datetime.now(timezone.utc).isoformat(),
        "attempt": attempt,
        "error_type": error_type,
        "diff_summary": diff_summary,
        "failed_spec": failed_spec,
    })


def log_score_progress(
    run_id: str,
    attempt: int,
    baseline_score: int,
    result_score: int,
    accepted_spec: dict | None = None,
) -> None:
    """Log that a rule passed the semantic gate and improved the score."""
    _write({
        "run_id": run_id,
        "stage": "score_progress",
        "ts": datetime.now(timezone.utc).isoformat(),
        "attempt": attempt,
        "baseline_score": baseline_score,
        "result_score": result_score,
        "score_delta": baseline_score - result_score,
        "accepted_spec": accepted_spec,
    })


def log_semantic_rejection(
    run_id: str,
    attempt: int,
    baseline_score: int,
    new_score: int,
    failed_spec: dict | None = None,
) -> None:
    """Log a semantic convergence gate rejection.

    The rule matched structurally but increased the semantic diff score
    vs ground truth — i.e. made the extraction worse.
    """
    _write({
        "run_id": run_id,
        "stage": "semantic_rejection",
        "ts": datetime.now(timezone.utc).isoformat(),
        "attempt": attempt,
        "baseline_score": baseline_score,
        "new_score": new_score,
        "score_delta": new_score - baseline_score,
        "failed_spec": failed_spec,
    })


def log_accept(
    run_id: str,
    rule_id: str,
    draft_id: str,
    commit_success: bool,
    error: str | None = None,
) -> None:
    """Log rule acceptance result."""
    _write({
        "run_id": run_id,
        "stage": "accept",
        "ts": datetime.now(timezone.utc).isoformat(),
        "rule_id": rule_id,
        "draft_id": draft_id,
        "commit_success": commit_success,
        "error": error,
    })


def log_reextract(
    run_id: str,
    uid: str,
    rules_loaded: int,
    custom_rules_count: int,
    dag_before_edges: int,
    dag_after_edges: int,
    dag_after: dict | None = None,
) -> None:
    """Log re-extraction with custom rules applied."""
    entry: dict[str, Any] = {
        "run_id": run_id,
        "stage": "reextract",
        "ts": datetime.now(timezone.utc).isoformat(),
        "uid": uid,
        "rules_loaded": rules_loaded,
        "custom_rules_count": custom_rules_count,
        "dag_before_edges": dag_before_edges,
        "dag_after_edges": dag_after_edges,
    }
    if dag_after:
        entry["dag_after_edges_list"] = [
            {"src": e.get("source"), "dst": e.get("destination")}
            for e in dag_after.get("edges", [])
        ]
    _write(entry)


def _summarize_node(n: dict) -> dict:
    """Compact summary of a node for logging."""
    ct = n.get("class_type", "?")
    if ct == "Operator":
        return {"class": ct, "name": n.get("name", "?")}
    if ct == "Parameter":
        return {"class": ct, "name": n.get("name", "?"), "value": n.get("value", "?")}
    return {"class": ct, "type": n.get("type", "?"), "text": (n.get("text", "") or "")[:80]}
