"""
dorian.mcp.retrieval — few-shot retrieval from the extraction corpus.

Surfaces the N most similar past extractions to a given (code, dag) query,
partitioned into positive examples (auto-accepted) and negative examples
(corrected by a user). Consumed by the orchestrator in
``dorian/code/rule_learning.py`` when assembling the LLM prompt.

Retrieval is deliberately lightweight — Jaccard over tokenised code +
operator-set symmetric difference on DAGs. Good enough at corpus sizes
we're likely to see pre-production. When corpus > 10k, swap in
``dorian_native.BKTree`` for the DAG half.
"""
from __future__ import annotations

import re
from typing import Any


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def code_tokens(code: str) -> set[str]:
    """Bag-of-identifiers over a source string.

    Keeps alphanumeric identifiers; discards keywords (built into
    Python/R/etc. parsers — not discriminative here); case-sensitive.
    """
    _KWS = {
        "def", "return", "import", "from", "as", "for", "while", "if",
        "else", "elif", "try", "except", "finally", "with", "in", "not",
        "and", "or", "is", "None", "True", "False", "class", "lambda",
        "yield", "pass", "break", "continue", "global", "nonlocal",
        "raise", "assert", "del",
    }
    return {t for t in _TOKEN_RE.findall(code or "") if t not in _KWS}


def operator_names(dag: dict) -> set[str]:
    names: set[str] = set()
    for nid, node in (dag or {}).get("nodes", {}).items():
        t = (node or {}).get("type")
        text = (node or {}).get("text")
        if t in ("Operator", "Snippet") and text:
            names.add(text)
    return names


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return 1.0 - (inter / union) if union else 1.0


async def retrieve_few_shots(
    code: str | None = None,
    dag: dict | None = None,
    *,
    k_pos: int = 3,
    k_neg: int = 3,
    corpus_cap: int = 500,
) -> dict:
    """Return ``{positives: [...], negatives: [...]}``.

    Each entry has ``{extraction_id, code, auto_dag, corrected_dag,
    rules_version, distance}``. ``distance`` is a combined (code, dag)
    score — lower = more similar.
    """
    from dorian.code.extraction_store import get_regression_set

    records = await get_regression_set()
    records = records[-corpus_cap:]

    query_tokens = code_tokens(code or "")
    query_ops = operator_names(dag or {})

    scored: list[tuple[float, dict, bool]] = []
    for rec in records:
        rec_code = rec.get("code") or ""
        auto = rec.get("autoDag") or {}
        corrected = rec.get("correctedDag")
        accepted = corrected or auto
        is_negative = corrected is not None

        d_code = _jaccard(query_tokens, code_tokens(rec_code)) if query_tokens else 1.0
        d_dag = (
            _jaccard(query_ops, operator_names(accepted))
            if (query_ops and accepted) else 1.0
        )

        # Both signals when both queries provided, else whichever is present.
        if query_tokens and (query_ops and accepted):
            dist = 0.5 * d_code + 0.5 * d_dag
        elif query_tokens:
            dist = d_code
        else:
            dist = d_dag

        scored.append((
            dist,
            {
                "extraction_id": rec.get("_id"),
                "code": rec_code,
                "auto_dag": auto,
                "corrected_dag": corrected,
                "rules_version": rec.get("rulesVersion"),
                "distance": round(dist, 4),
            },
            is_negative,
        ))

    scored.sort(key=lambda t: t[0])

    positives = [entry for (_d, entry, neg) in scored if not neg][:k_pos]
    negatives = [entry for (_d, entry, neg) in scored if neg][:k_neg]

    return {
        "positives": positives,
        "negatives": negatives,
        "corpus_size": len(records),
    }
