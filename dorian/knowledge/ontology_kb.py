"""
dorian/knowledge/ontology_kb.py
-------------------------------
In-memory adjacency view over the parsed KB ontology.

Parsing is rust-side (``dorian_native.kb_parse``) — same parser the
snapshot exporter uses. This module wraps the resulting triples
in a python-side index so MCP tools can do predicate-level lookups
the snapshot abstraction doesn't expose directly:

    >>> kb = load_kb()
    >>> kb.adj["sklearn.svm.SVC"]["might_introduce"]
    ["Algorithmic Bias"]
    >>> kb.display(uuid_C)
    "C"

Every node-id stays distinct (no rename collapse): two operators
each declaring a parameter named ``n_estimators`` keep their own
identity, mirroring the rust builder's invariant.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_DIR = _PROJECT_ROOT / "dorian" / "knowledge" / "sources"
_IO_CRAWLER_EXTRAS = _PROJECT_ROOT / "volumes" / "io_crawler_extras.kb"


class OntologyKB:
    """Adjacency index over parsed KB triples.

    ``adj[node][predicate]`` returns the list of destinations.
    ``rev_adj[node][predicate]`` returns the list of subjects.
    ``display(node)`` returns the human-readable name (collapsing
    ``has_name`` chains) without altering node identity.
    """

    def __init__(self, triples: list[dict[str, str]]):
        self._display_name: dict[str, str] = {}
        self.adj: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.rev_adj: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for t in triples:
            pred = t["predicate"]
            if pred in ("has_name", "hasname"):
                self._display_name.setdefault(t["subject"], t["object"])
                continue
            self.adj[t["subject"]][pred].append(t["object"])
            self.rev_adj[t["object"]][pred].append(t["subject"])

    def display(self, node: str) -> str:
        return self._display_name.get(node, node)

    def out(self, node: str, predicate: str) -> list[str]:
        return self.adj.get(node, {}).get(predicate, [])

    def incoming(self, node: str, predicate: str) -> list[str]:
        return self.rev_adj.get(node, {}).get(predicate, [])

    def nodes_classified_as(self, label: str) -> list[str]:
        """Subjects of ``is_a`` or ``is_an`` edges to ``label``."""
        out: set[str] = set()
        for n, edges in self.adj.items():
            for pred in ("is_a", "is_an"):
                if label in edges.get(pred, []):
                    out.add(n)
        return sorted(out)

    def chain_description(self, node: str) -> str | None:
        """Return ``with_description`` text for ``node``, chain-walked."""
        for v in self.adj.get(node, {}).get("with_description", []):
            return self.display(v)
        return None


def load_kb() -> OntologyKB:
    """Parse the curated ``.kb`` files (and io-crawler extras) via the
    rust parser; return an indexed view.

    The MCP tools and any other predicate-level walker should use
    this — runtime catalog queries should prefer the rust snapshot
    accessors in ``dorian.knowledge.queries``.
    """
    import dorian_native  # type: ignore

    sources: list[tuple[str, str]] = []
    for path in sorted(_SOURCES_DIR.glob("*.kb")):
        sources.append((str(path), path.read_text()))
    if _IO_CRAWLER_EXTRAS.is_file() and _IO_CRAWLER_EXTRAS.stat().st_size > 0:
        sources.append((str(_IO_CRAWLER_EXTRAS), _IO_CRAWLER_EXTRAS.read_text()))

    payload = json.loads(dorian_native.kb_parse(sources))
    triples = payload.get("triples", [])
    errors = payload.get("errors", [])
    if errors:
        _log.warning("ontology_kb: %d KB parse error(s) ignored", len(errors))
        for e in errors[:5]:
            _log.warning(
                "  %s:%s — %s", e["source"], e["line_no"], e["message"]
            )
    return OntologyKB(triples)
