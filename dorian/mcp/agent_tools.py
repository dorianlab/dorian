"""
dorian/mcp/agent_tools.py
-------------------------
Bounded-agent tools for the LLM rule-suggestion loop.

Every tool is a deterministic read-side operation (or an explicit
terminal submit) that the LLM can call while scoped to a single
``propose_rule`` session. The orchestrator in
``dorian/code/rule_learning.py`` owns control flow — these tools only
reveal state or accept a single terminal submission.

See (internal design note; not in public repo) for the hybrid architecture.
"""
from __future__ import annotations

from typing import Any

from dorian.mcp.rule_compiler import compile_rule
from dorian.mcp.rule_schema import validate_rule_spec
from dorian.mcp.dag_tools import (
    _parse_dag, dag_diff, graph_edit_distance,
)
from dorian.pipeline.transforms import sync_apply


# ---------------------------------------------------------------------------
# Inspection tools (read-only)
# ---------------------------------------------------------------------------

def inspect_region(dag_json: str | dict, center_id: str, radius: int = 2) -> dict:
    """Return the ``radius``-hop neighbourhood around ``center_id``.

    Bounded-agent tool. Caps radius at 5 to prevent the LLM from pulling
    the whole DAG via one inspect call — the orchestrator surfaces the
    full DAG separately in the session state.

    Returns ``{center, nodes: {id: {...}}, edges: [...], hops: int}``.
    """
    radius = max(0, min(5, int(radius)))

    def _parse(x):
        if isinstance(x, str):
            import json as _json
            return _json.loads(x)
        return x

    dag = _parse(dag_json) or {}
    nodes = dag.get("nodes") or {}
    edges = dag.get("edges") or []

    if center_id not in nodes:
        return {"error": f"node {center_id!r} not in dag", "center": center_id}

    adj: dict[str, set[str]] = {nid: set() for nid in nodes}
    for e in edges:
        s, d = str(e.get("source")), str(e.get("destination"))
        if s in adj and d in adj:
            adj[s].add(d)
            adj[d].add(s)

    visited = {center_id}
    frontier = {center_id}
    for _ in range(radius):
        nxt: set[str] = set()
        for nid in frontier:
            nxt |= adj.get(nid, set())
        nxt -= visited
        if not nxt:
            break
        visited |= nxt
        frontier = nxt

    kept_nodes = {nid: nodes[nid] for nid in visited}
    kept_edges = [
        e for e in edges
        if str(e.get("source")) in visited and str(e.get("destination")) in visited
    ]
    return {
        "center": center_id,
        "nodes": kept_nodes,
        "edges": kept_edges,
        "hops": radius,
    }


def show_rule(rules_list: list[dict], position: int) -> dict:
    """Return the full JSON spec of the rule at ``position`` in ``rules_list``."""
    if not isinstance(rules_list, list):
        return {"error": "rules_list must be a list"}
    if position < 0 or position >= len(rules_list):
        return {"error": f"position {position} out of range [0, {len(rules_list)})"}
    return {"position": position, "rule": rules_list[position]}


# ---------------------------------------------------------------------------
# Non-terminal exploration — the LLM uses this to probe candidates
# ---------------------------------------------------------------------------

def dry_run_rule(
    spec: dict,
    dag_json: str | dict,
    *,
    target_dag_json: str | dict | None = None,
) -> dict:
    """Compile ``spec`` and apply it once against ``dag_json``.

    Returns ``{ok, changed, new_dag, diff, ged_before, ged_after, errors}``.
    Does NOT persist anything; safe for the LLM to call repeatedly.

    If ``target_dag_json`` is provided, GED is measured against it so the
    agent can see whether its candidate moves the DAG closer to the
    target. When omitted, ``ged_before``/``ged_after`` are ``None``.
    """
    validated, schema_errors = validate_rule_spec(spec)
    if schema_errors:
        return {"ok": False, "errors": schema_errors, "stage": "schema"}

    compiled, errors, warnings = compile_rule(validated)
    if compiled is None or errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "stage": "compile"}

    try:
        dag = _parse_dag(dag_json)
    except Exception as exc:
        return {"ok": False, "errors": [f"parse dag: {exc}"], "stage": "parse"}

    try:
        new_dag_obj = sync_apply(compiled, dag, {})
    except Exception as exc:
        return {"ok": False, "errors": [f"apply: {exc}"], "stage": "apply"}

    before = dag.to_json_dict()
    after = new_dag_obj.to_json_dict()
    diff = dag_diff(before, after)
    changed = diff.get("summary", "") != "No changes"

    ged_before = ged_after = None
    if target_dag_json is not None:
        try:
            ged_before = graph_edit_distance(before, target_dag_json)
            ged_after = graph_edit_distance(after, target_dag_json)
        except Exception:
            pass

    return {
        "ok": True,
        "changed": changed,
        "new_dag": after,
        "diff": diff,
        "ged_before": ged_before,
        "ged_after": ged_after,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Terminal submissions
# ---------------------------------------------------------------------------

def submit_rule(
    spec: dict,
    insert_at: int,
    rationale: str = "",
    *,
    closes_edit_path_ops: list[int] | None = None,
) -> dict:
    """Terminal: the LLM's single rule submission for this session.

    The orchestrator evaluates the submission (apply + GED trend +
    backward-compat check) and decides whether to accept, retry, or
    surface the candidate to the user. The tool itself only validates
    the spec's schema — it does not mutate persistent state.
    """
    validated, errors = validate_rule_spec(spec)
    if errors:
        return {
            "ok": False,
            "terminal": True,
            "errors": errors,
            "stage": "schema",
        }
    return {
        "ok": True,
        "terminal": True,
        "spec": validated,
        "insert_at": int(insert_at),
        "rationale": rationale or "",
        "closes_edit_path_ops": list(closes_edit_path_ops or []),
    }


def submit_reorder(
    moves: list[list[int]],
    rationale: str = "",
) -> dict:
    """Terminal: propose a reorder of the existing rules list.

    ``moves`` is a list of ``[old_pos, new_pos]`` pairs applied in order.
    """
    clean: list[tuple[int, int]] = []
    for m in moves or []:
        if not (isinstance(m, (list, tuple)) and len(m) == 2):
            return {"ok": False, "terminal": True, "errors": [f"bad move: {m!r}"]}
        try:
            clean.append((int(m[0]), int(m[1])))
        except (TypeError, ValueError):
            return {"ok": False, "terminal": True, "errors": [f"non-integer position in {m!r}"]}
    return {
        "ok": True,
        "terminal": True,
        "moves": clean,
        "rationale": rationale or "",
    }


def agent_abort(reason: str = "") -> dict:
    """Terminal: the LLM gives up on this session.

    Orchestrator treats this the same way as turn-budget exhaustion —
    the rejection is persisted with ``reason`` so future sessions see
    the negative signal.
    """
    return {"ok": True, "terminal": True, "aborted": True, "reason": reason or "no_submission"}


# ---------------------------------------------------------------------------
# Backward-compatibility validator
# ---------------------------------------------------------------------------

async def rules_validate_backward_compat(
    candidate_rules_src: str,
    *,
    corpus_cap: int = 500,
) -> dict:
    """Check that ``candidate_rules_src`` doesn't regress any past extraction.

    Parameters
    ----------
    candidate_rules_src:
        The Python source for the candidate rules list (as a ``return [...]``
        expression) — the format ``get_rules()`` accepts. The JSON-schema
        path compiles through ``compile_rule`` and writes the same source
        format before persistence; any user-facing entry point that
        mutates the list converges here.
    corpus_cap:
        Maximum extractions to check synchronously. Above this, the
        handler should fan out to the exec-worker via an
        ``rules_bc_check`` job kind (future work — see
        (internal design note; not in public repo) §9).

    Returns
    -------
    dict
        ``{ok, corpus_size, checked, elapsed_ms, regressions: [...]}``
        where each regression is
        ``{extraction_id, diff_summary, missing_ops, extra_ops}``.
    """
    import time
    from dorian.code.extraction_store import get_regression_set
    from dorian.code.parsing.parser import parse as parse_code
    from dorian.code.parsing.rules import get_rules
    from dorian.mcp.dag_tools import semantic_dag_diff

    t0 = time.monotonic()
    try:
        rules = get_rules(candidate_rules_src)
    except Exception as exc:
        return {
            "ok": False,
            "corpus_size": 0,
            "checked": 0,
            "elapsed_ms": (time.monotonic() - t0) * 1000,
            "regressions": [],
            "errors": [f"rules load: {exc}"],
        }

    records = await get_regression_set()
    corpus_size = len(records)
    records = records[-corpus_cap:]  # most recent N within the cap

    regressions: list[dict] = []
    for rec in records:
        accepted = rec.get("correctedDag") or rec.get("autoDag")
        if not accepted:
            continue
        try:
            dag = await parse_code(rec["code"], rec.get("language", "python"), rules)
        except Exception:
            regressions.append({
                "extraction_id": rec["_id"],
                "diff_summary": "parse failure under candidate rules",
                "missing_ops": [],
                "extra_ops": [],
            })
            continue
        replayed = dag.to_json_dict()
        diff = semantic_dag_diff(replayed, accepted)
        if diff.get("summary") and "0 differences" not in diff.get("summary", ""):
            regressions.append({
                "extraction_id": rec["_id"],
                "diff_summary": diff.get("summary"),
                "missing_ops": diff.get("missing_operators", []),
                "extra_ops": diff.get("extra_operators", []),
            })

    return {
        "ok": not regressions,
        "corpus_size": corpus_size,
        "checked": len(records),
        "elapsed_ms": (time.monotonic() - t0) * 1000,
        "regressions": regressions,
        "capped": corpus_size > corpus_cap,
    }
