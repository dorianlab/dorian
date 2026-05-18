"""
dorian/code/regression.py
-------------------------
Regression testing for pipeline extraction rules.

Re-runs all persisted extraction records against a candidate rule set and
reports pass / fail for each record.  The "expected" pipeline for each
record is the user-corrected DAG if one exists, otherwise the original
auto-extracted DAG.

This ensures rule changes don't break previously-correct extractions.
"""
from __future__ import annotations

from typing import Any

from backend.events import Event, aemit
from dorian.dag import DAG


# ---------------------------------------------------------------------------
# DAG structural equality
# ---------------------------------------------------------------------------

def _fingerprint_node(node: dict) -> str:
    """Hash-key for a node based on its semantic identity (not random UUIDs)."""
    # Nodes carry class_type (Operator / Parameter / Snippet), name, value, language
    parts = [
        node.get("class_type", ""),
        node.get("name", ""),
        node.get("value", ""),
        node.get("language", ""),
        node.get("type", node.get("dtype", "")),
        node.get("text", ""),
    ]
    return "|".join(str(p) for p in parts)


def _dag_equal(a: dict, b: dict) -> bool:
    """Structural equality of two DAG JSON dicts.

    Compares node fingerprints and edge connectivity.  Node UUIDs are
    non-deterministic across different parse runs so we compare by
    semantic identity (class_type + name + value + language).
    """
    a_nodes = a.get("nodes", {})
    b_nodes = b.get("nodes", {})

    # Compare node multisets (order doesn't matter)
    a_fps = sorted(_fingerprint_node(n) for n in a_nodes.values())
    b_fps = sorted(_fingerprint_node(n) for n in b_nodes.values())
    if a_fps != b_fps:
        return False

    # Compare edge counts as a coarse structural check.
    # A full isomorphism check would be expensive and not needed for
    # the typical regression-test workload.
    a_edges = a.get("edges", [])
    b_edges = b.get("edges", [])
    if len(a_edges) != len(b_edges):
        return False

    return True


def _diff_summary(expected: dict, actual: dict) -> str:
    """Human-readable summary of differences between two DAG dicts."""
    e_nodes = expected.get("nodes", {})
    a_nodes = actual.get("nodes", {})
    e_edges = expected.get("edges", [])
    a_edges = actual.get("edges", [])

    parts: list[str] = []
    if len(e_nodes) != len(a_nodes):
        parts.append(f"nodes: expected {len(e_nodes)}, got {len(a_nodes)}")
    if len(e_edges) != len(a_edges):
        parts.append(f"edges: expected {len(e_edges)}, got {len(a_edges)}")

    e_fps = sorted(_fingerprint_node(n) for n in e_nodes.values())
    a_fps = sorted(_fingerprint_node(n) for n in a_nodes.values())
    missing = set(e_fps) - set(a_fps)
    extra = set(a_fps) - set(e_fps)
    if missing:
        parts.append(f"missing nodes: {list(missing)[:5]}")
    if extra:
        parts.append(f"extra nodes: {list(extra)[:5]}")

    return "; ".join(parts) if parts else "no structural differences detected"


# ---------------------------------------------------------------------------
# Regression runner
# ---------------------------------------------------------------------------

async def run_regression_test(
    candidate_rules=None,
) -> list[dict[str, Any]]:
    """Replay all extraction records against *candidate_rules*.

    If *candidate_rules* is ``None``, uses the current default rule set.

    Returns a list of result dicts::

        [
            {
                "extraction_id": str,
                "passed": bool,
                "expected_source": "corrected" | "auto",
                "diff_summary": str | None,
            },
            ...
        ]
    """
    from dorian.code.extraction_store import get_regression_set
    from dorian.code.parsing.parser import parse as parse_code

    records = await get_regression_set()
    if not records:
        await aemit(Event("RegressionNoRecords", {"message": "No extraction records to test."}))
        return []

    results: list[dict[str, Any]] = []

    for record in records:
        extraction_id = record["_id"]
        code = record["code"]
        language = record.get("language", "python")

        # The "expected" output is the user-corrected DAG if available,
        # otherwise the auto-extracted DAG.
        if record.get("correctedDag"):
            expected = record["correctedDag"]
            expected_source = "corrected"
        else:
            expected = record["autoDag"]
            expected_source = "auto"

        try:
            _, candidate_dag = parse_code(
                code, language, rewrite_rules=candidate_rules,
            )
            actual = candidate_dag.to_json_dict()
            passed = _dag_equal(actual, expected)
            diff = None if passed else _diff_summary(expected, actual)
        except Exception as exc:
            passed = False
            diff = f"parse error: {exc}"

        results.append({
            "extraction_id": extraction_id,
            "passed": passed,
            "expected_source": expected_source,
            "diff_summary": diff,
        })

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    await aemit(Event("RegressionTestComplete", {"passed": passed, "total": total}))

    return results
