"""
dorian.mcp.extraction
----------------------
Knowledge extraction pipeline stages, ported from the KBExtraction project.

This module provides the individual pipeline stages as standalone functions
that can be called from MCP tools. Each stage is decoupled from the others
so that an LLM agent can drive the pipeline step-by-step via tool calls.

Pipeline stages:
1. **decompose** — Break text into atomic "quality" statements
2. **similarity_filter** — Filter qualities against existing KB via embeddings
3. **novelty_classify** — Classify quality as EXISTING / PARTIALLY_NEW / NEW
4. **extract_triplets** — Extract (subject, predicate, object) from text

LLM backend:
    Uses a pluggable responder protocol matching the KBExtraction pattern.
    Configuration is read from ``config.mcp.extraction`` in config.yaml.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from string import Template
from typing import Any, Protocol, Sequence

from backend.config import config
from backend.events import Event, emit


# ═══════════════════════════════════════════════════════════════════════════
# LLM Responder — now lives in dorian.llm. These re-exports keep the
# pre-split call sites (7 of them across mcp + rule_learning + router +
# tabular quality) working without churn. New code should import from
# ``dorian.llm`` directly.
# ═══════════════════════════════════════════════════════════════════════════

from dorian.llm import Responder as LLMResponder, spawn as _spawn_llm
from dorian.llm.backends import GroqResponder, OpenAICompatibleResponder


def _get_responder() -> LLMResponder:
    """Back-compat shim — prefer ``dorian.llm.spawn(purpose=...)`` in new code."""
    return _spawn_llm(purpose="extraction")


# ═══════════════════════════════════════════════════════════════════════════
# Prompt rendering
# ═══════════════════════════════════════════════════════════════════════════

def _render_prompt(template: str, **kwargs: str) -> str:
    """Render a prompt template with $variable substitution."""
    return Template(template).safe_substitute(**kwargs)


def _parse_json_response(text: str) -> Any:
    """Extract JSON from an LLM response (handles markdown code blocks)."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding a JSON array or object
    for pattern in [r"\[.*\]", r"\{.*\}"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Stage 1: Text → Qualities (Decompose)
# ═══════════════════════════════════════════════════════════════════════════

def decompose_text(text: str, responder: LLMResponder | None = None) -> list[str]:
    """Extract atomic quality statements from a text passage.

    Parameters
    ----------
    text : str
        The source text (from a document, article, or user input).
    responder : LLMResponder, optional
        LLM backend. If None, built from config.

    Returns
    -------
    list[str]
        List of atomic quality statements.
    """
    from dorian.mcp.prompts import DECOMPOSE_PROMPT

    if responder is None:
        responder = _get_responder()

    prompt = _render_prompt(DECOMPOSE_PROMPT, text=text)
    raw = responder.invoke(prompt)
    result = _parse_json_response(raw)

    if isinstance(result, list):
        return [str(q) for q in result if q]
    if isinstance(result, dict) and "qualities" in result:
        return [str(q) for q in result["qualities"] if q]

    emit(Event("DecomposeUnexpectedShape", {"result_type": str(type(result))}))
    return []


# ═══════════════════════════════════════════════════════════════════════════
# Stage 2: Similarity Filter
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SimilarityResult:
    """Result of comparing a quality against KB via vector similarity."""
    quality: str
    max_score: float
    neighbors: list[dict]  # [{"text": "...", "score": float}]
    kept: bool             # above threshold?


def filter_by_similarity(
    qualities: list[str],
    kb_texts: list[str],
    threshold: float | None = None,
) -> list[SimilarityResult]:
    """Filter qualities by vector similarity against existing KB entries.

    Uses SentenceTransformer embeddings and cosine similarity.

    Parameters
    ----------
    qualities : list[str]
        The extracted quality statements.
    kb_texts : list[str]
        Existing KB relation/node texts for comparison.
    threshold : float, optional
        Similarity threshold. Defaults from config.

    Returns
    -------
    list[SimilarityResult]
        Each quality annotated with similarity score and neighbors.
    """
    if threshold is None:
        threshold = config.mcp.extraction.similarity_threshold

    if not qualities or not kb_texts:
        return [
            SimilarityResult(quality=q, max_score=0.0, neighbors=[], kept=True)
            for q in qualities
        ]

    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np

        model_name = config.mcp.extraction.embedding_model
        model = SentenceTransformer(model_name)

        q_embeddings = model.encode(qualities, normalize_embeddings=True)
        kb_embeddings = model.encode(kb_texts, normalize_embeddings=True)

        # Cosine similarity matrix (already normalized → dot product)
        sim_matrix = np.dot(q_embeddings, kb_embeddings.T)

        results = []
        for i, quality in enumerate(qualities):
            scores = sim_matrix[i]
            top_indices = np.argsort(scores)[::-1][:5]  # top 5 neighbors

            neighbors = [
                {"text": kb_texts[j], "score": float(scores[j])}
                for j in top_indices
                if scores[j] > 0.1  # minimum relevance
            ]
            max_score = float(scores[top_indices[0]]) if len(top_indices) > 0 else 0.0

            results.append(SimilarityResult(
                quality=quality,
                max_score=max_score,
                neighbors=neighbors,
                kept=max_score >= threshold,
            ))

        return results

    except ImportError:
        emit(Event("SimilarityFilterSkipped", {"reason": "sentence-transformers not installed"}))
        return [
            SimilarityResult(quality=q, max_score=0.0, neighbors=[], kept=True)
            for q in qualities
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3: Novelty Classification
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class NoveltyResult:
    """Result of novelty classification for a quality."""
    quality: str
    decision: str         # EXISTING | PARTIALLY_NEW | NEW
    rationale: str
    matched_neighbor: str
    confidence: float


def classify_novelty(
    quality: str,
    neighbors: list[dict],
    responder: LLMResponder | None = None,
) -> NoveltyResult:
    """Classify a single quality as EXISTING, PARTIALLY_NEW, or NEW.

    Parameters
    ----------
    quality : str
        The quality statement to classify.
    neighbors : list[dict]
        Similar KB entries (from similarity filter), each ``{"text": ..., "score": ...}``.
    responder : LLMResponder, optional
        LLM backend.

    Returns
    -------
    NoveltyResult
    """
    from dorian.mcp.prompts import NOVELTY_COMPARATOR_PROMPT

    if responder is None:
        responder = _get_responder()

    neighbor_text = "\n".join(
        f"- [{n.get('score', 0):.2f}] {n['text']}" for n in neighbors
    ) if neighbors else "(no similar entries found)"

    prompt = _render_prompt(
        NOVELTY_COMPARATOR_PROMPT,
        quality=quality,
        neighbors=neighbor_text,
    )

    raw = responder.invoke(prompt)
    result = _parse_json_response(raw)

    if isinstance(result, dict):
        decision = result.get("decision", "NEW").upper()
        if decision not in ("EXISTING", "PARTIALLY_NEW", "NEW"):
            decision = "NEW"
        return NoveltyResult(
            quality=quality,
            decision=decision,
            rationale=result.get("rationale", ""),
            matched_neighbor=result.get("matched_neighbor", ""),
            confidence=float(result.get("confidence", 0.5)),
        )

    return NoveltyResult(
        quality=quality,
        decision="NEW",
        rationale="Failed to parse LLM response",
        matched_neighbor="",
        confidence=0.0,
    )


def classify_novelty_batch(
    qualities_with_neighbors: list[tuple[str, list[dict]]],
    responder: LLMResponder | None = None,
) -> list[NoveltyResult]:
    """Classify multiple qualities for novelty.

    Parameters
    ----------
    qualities_with_neighbors : list[tuple[str, list[dict]]]
        Each item is ``(quality, neighbors)``.
    responder : LLMResponder, optional
        LLM backend.

    Returns
    -------
    list[NoveltyResult]
    """
    if responder is None:
        responder = _get_responder()

    return [
        classify_novelty(quality, neighbors, responder)
        for quality, neighbors in qualities_with_neighbors
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Stage 4: Triplet Extraction
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Triplet:
    """A knowledge graph triplet (subject, predicate, object)."""
    subject: str
    predicate: str
    object: str


def extract_triplets(
    text: str,
    responder: LLMResponder | None = None,
) -> list[Triplet]:
    """Extract knowledge graph triplets from text.

    Parameters
    ----------
    text : str
        The source text to extract triplets from.
    responder : LLMResponder, optional
        LLM backend.

    Returns
    -------
    list[Triplet]
    """
    from dorian.mcp.prompts import TRIPLET_EXTRACTION_PROMPT

    if responder is None:
        responder = _get_responder()

    prompt = _render_prompt(TRIPLET_EXTRACTION_PROMPT, text=text)
    raw = responder.invoke(prompt)
    result = _parse_json_response(raw)

    triplets = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and all(k in item for k in ("subject", "predicate", "object")):
                triplets.append(Triplet(
                    subject=item["subject"],
                    predicate=item["predicate"],
                    object=item["object"],
                ))
    elif isinstance(result, dict) and "triplets" in result:
        for item in result["triplets"]:
            if isinstance(item, dict) and all(k in item for k in ("subject", "predicate", "object")):
                triplets.append(Triplet(
                    subject=item["subject"],
                    predicate=item["predicate"],
                    object=item["object"],
                ))

    return triplets


# ═══════════════════════════════════════════════════════════════════════════
# Keyword synonym generation
# ═══════════════════════════════════════════════════════════════════════════

def generate_synonyms(
    keyword: str,
    responder: LLMResponder | None = None,
) -> list[str]:
    """Generate keyword synonyms for semantic search.

    Parameters
    ----------
    keyword : str
        The keyword to generate synonyms for.
    responder : LLMResponder, optional
        LLM backend.

    Returns
    -------
    list[str]
    """
    from dorian.mcp.prompts import KEYWORD_SYNONYMS_PROMPT

    if responder is None:
        responder = _get_responder()

    prompt = _render_prompt(KEYWORD_SYNONYMS_PROMPT, keyword=keyword)
    raw = responder.invoke(prompt)
    result = _parse_json_response(raw)

    if isinstance(result, list):
        return [str(s) for s in result if s]

    return []


# ═══════════════════════════════════════════════════════════════════════════
# Full pipeline (convenience — agents typically call stages individually)
# ═══════════════════════════════════════════════════════════════════════════

def run_extraction_pipeline(
    text: str,
    keyword: str,
    kb_texts: list[str] | None = None,
    responder: LLMResponder | None = None,
) -> dict:
    """Run the full extraction pipeline on a text passage.

    This is a convenience function; agents will typically call each
    stage via individual MCP tools for better control.

    Parameters
    ----------
    text : str
        Source text.
    keyword : str
        Search keyword for KB context.
    kb_texts : list[str], optional
        Existing KB texts for similarity comparison.
    responder : LLMResponder, optional
        LLM backend.

    Returns
    -------
    dict
        Pipeline results with qualities, novelty, and triplets.
    """
    if responder is None:
        responder = _get_responder()

    # Stage 1: Decompose
    qualities = decompose_text(text, responder)

    # Stage 2: Similarity filter (if KB texts provided)
    similarity_results = []
    if kb_texts:
        similarity_results = filter_by_similarity(qualities, kb_texts)
        kept_qualities = [r.quality for r in similarity_results if r.kept]
    else:
        kept_qualities = qualities

    # Stage 3: Novelty classification
    novelty_results = []
    for quality in kept_qualities:
        neighbors = []
        for sr in similarity_results:
            if sr.quality == quality:
                neighbors = sr.neighbors
                break
        nr = classify_novelty(quality, neighbors, responder)
        novelty_results.append(nr)

    # Stage 4: Triplet extraction (on novel qualities)
    novel_texts = [
        nr.quality for nr in novelty_results
        if nr.decision in ("NEW", "PARTIALLY_NEW")
    ]
    triplets = []
    if novel_texts:
        combined = "\n".join(f"- {t}" for t in novel_texts)
        triplets = extract_triplets(combined, responder)

    return {
        "qualities_extracted": len(qualities),
        "qualities_kept": len(kept_qualities),
        "novelty": [
            {
                "quality": nr.quality,
                "decision": nr.decision,
                "rationale": nr.rationale,
                "confidence": nr.confidence,
            }
            for nr in novelty_results
        ],
        "triplets": [
            {"subject": t.subject, "predicate": t.predicate, "object": t.object}
            for t in triplets
        ],
    }
