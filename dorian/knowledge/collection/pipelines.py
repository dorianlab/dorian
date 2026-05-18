"""
Scrapy pipelines for Dorian knowledge collection.

Three pipelines:
- ``SeparateJsonExportPipeline`` — saves each item as a JSON file (data archival)
- ``KBSourcePipeline``   — generates ``sources/{library}_generated.py`` with KB triple strings
- ``Neo4JPipeline``      — writes directly to Neo4j using the same KB schema as ``base.py``

Both ``KBSourcePipeline`` and ``Neo4JPipeline`` share ``item_to_triples()`` for
KB triple generation, ensuring a single source of truth for the graph schema.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from itemadapter import ItemAdapter
# Neo4j writer pipeline retired with the rust KB snapshot port.
# Scraped items now flow through ``KBSourcePipeline`` which writes
# DSL text appended to ``volumes/scraped_extras.kb`` (added on
# demand by the operator). Direct Bolt writes are no longer
# supported here.

from dorian.knowledge.collection.items import LibraryItem

if TYPE_CHECKING:
    import scrapy


# =====================================================================
# Shared: convert a LibraryItem to KB triple strings
# =====================================================================

def item_to_triples(item: dict, spider_name: str) -> list[str]:
    """Convert a scraped ``LibraryItem`` to a list of KB triple strings.

    Uses the same predicates and chain syntax as ``dorian/knowledge/base.py``
    so that both pipeline outputs (source files and direct Neo4j writes) produce
    an identical graph schema.

    KB triple patterns:
    - ``X is an Operator``
    - ``X implements Interface``
    - ``X has parameter name; is of type T; with default D``
    - ``X has input name; is of type T; has position N``
    - ``X has output name; is of type T; has position N``
    - ``X has attribute name; is of type T``
    - ``X has method {uuid}; has name M``
    - ``{uuid} has input name; is of type T; has position N``
    - ``{uuid} has output name; is of type T; has position N``
    - ``{uuid} has parameter name; is of type T; with default D``
    """
    class_name = item.get("name", "")
    item_type = item.get("type", "Class")
    if not class_name:
        return []

    lines: list[str] = []

    # ── Operator declaration ──
    lines.append(f"{class_name} is an Operator")

    # ── Interface inference (sklearn-specific) ──
    if spider_name.startswith("sklearn") and item_type == "Class":
        methods = set(item.get("functions", {}).keys())
        if "transform" in methods or "fit_transform" in methods:
            lines.append(f"{class_name} implements Sklearn Transformer")
        if "predict" in methods:
            lines.append(f"{class_name} implements Sklearn Estimator")
    elif item_type == "Function":
        lines.append(f"{class_name} implements Function")

    # ── Constructor parameters and inputs ──
    params = item.get("hyperParameters", {})
    input_position = 0
    for param_name, param_data in params.items():
        default = param_data.get("default")
        dtype = param_data.get("type") or ""
        # Sanitize values for KB triple syntax (strip semicolons)
        dtype = _sanitize(dtype)
        param_name = _sanitize(param_name)

        if default is not None:
            # Has default → parameter (hyperparameter)
            default = _sanitize(str(default))
            chain = f"{class_name} has parameter {param_name}"
            if dtype:
                chain += f"; is of type {dtype}"
            chain += f"; with default {default}"
            lines.append(chain)
        else:
            # No default → input
            chain = f"{class_name} has input {param_name}"
            if dtype:
                chain += f"; is of type {dtype}"
            chain += f"; has position {input_position}"
            lines.append(chain)
            input_position += 1

    # ── Class-level outputs ──
    for i, output in enumerate(item.get("outputs") or []):
        out_name = _sanitize(output.get("name", ""))
        out_type = _sanitize(output.get("type", ""))
        if not out_name and not out_type:
            continue
        chain = f"{class_name} has output {out_name or f'output_{i}'}"
        if out_type:
            chain += f"; is of type {out_type}"
        chain += f"; has position {i}"
        lines.append(chain)

    # ── Attributes ──
    for attr in item.get("attributes") or []:
        attr_name = _sanitize(attr.get("name", ""))
        attr_type = _sanitize(attr.get("type", ""))
        if not attr_name:
            continue
        chain = f"{class_name} has attribute {attr_name}"
        if attr_type:
            chain += f"; is of type {attr_type}"
        lines.append(chain)

    # ── Methods ──
    for method_name, method_data in (item.get("functions") or {}).items():
        method_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{class_name}.{method_name}"))
        lines.append(f"{class_name} has method {method_uuid}; has name {method_name}")

        # Method inputs
        method_input_pos = 0
        for arg in method_data.get("args", []):
            arg_name = _sanitize(arg.get("name", ""))
            arg_type = _sanitize(arg.get("type", "") or "")
            arg_default = arg.get("default")

            if arg_default is not None:
                # Optional → method parameter
                arg_default = _sanitize(str(arg_default))
                chain = f"{method_uuid} has parameter {arg_name}"
                if arg_type:
                    chain += f"; is of type {arg_type}"
                chain += f"; with default {arg_default}"
                lines.append(chain)
            else:
                # Mandatory → method input
                chain = f"{method_uuid} has input {arg_name}"
                if arg_type:
                    chain += f"; is of type {arg_type}"
                chain += f"; has position {method_input_pos}"
                lines.append(chain)
                method_input_pos += 1

        # Method outputs
        for j, out in enumerate(method_data.get("outputs", [])):
            out_name = _sanitize(out.get("name", ""))
            out_type = _sanitize(out.get("type", ""))
            if not out_name and not out_type:
                continue
            chain = f"{method_uuid} has output {out_name or f'output_{j}'}"
            if out_type:
                chain += f"; is of type {out_type}"
            chain += f"; has position {j}"
            lines.append(chain)

    return lines


def _sanitize(value: str) -> str:
    """Remove characters that would break KB triple parsing."""
    return value.replace(";", ",").replace("\n", " ").strip()


# =====================================================================
# Pipeline 1: JSON export (unchanged)
# =====================================================================

class ScrapyItemEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "items"):
            return dict(obj)
        if isinstance(obj, (list, tuple)):
            return list(obj)
        return super().default(obj)


class SeparateJsonExportPipeline:
    def process_item(self, item, spider):
        identifier = item.get("name")
        version = item.get("version")
        if not identifier:
            return item

        output_dir = Path(
            f"dorian/knowledge/collection/data/{spider.language}/{spider.name.split('_')[0]}"
        )
        if version:
            output_dir = output_dir / version
        output_dir.mkdir(parents=True, exist_ok=True)

        with open(output_dir / f"{identifier}.json", "w", encoding="utf-8") as f:
            json.dump(dict(item), f, ensure_ascii=False, cls=ScrapyItemEncoder, indent=4)

        return item


# =====================================================================
# Pipeline 2: KB source file generation
# =====================================================================

class KBSourcePipeline:
    """Generate ``sources/{library}_generated.py`` with KB triple strings.

    Accumulates all items per spider, then writes the file when the spider
    closes.  The generated file exports a ``knowledge`` string that
    ``dorian/knowledge/base.py`` loads automatically.
    """

    def __init__(self):
        self._triples: dict[str, list[str]] = {}  # spider_name → triples

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_item(self, item, spider):
        triples = item_to_triples(dict(item), spider.name)
        self._triples.setdefault(spider.name, []).extend(triples)
        return item

    def close_spider(self, spider):
        triples = self._triples.get(spider.name, [])
        if not triples:
            return

        library = spider.name.split("_")[0]  # "sklearn_spider" → "sklearn"
        output_path = (
            Path(__file__).resolve().parent.parent / "sources" / f"{library}_generated.py"
        )

        content = _build_source_file(triples, library)
        output_path.write_text(content, encoding="utf-8")


def _build_source_file(triples: list[str], library: str) -> str:
    """Build a ``sources/{library}_generated.py`` file from KB triples."""
    header = (
        f'"""Auto-generated KB triples for {library}. Do not edit manually."""\n\n'
        'knowledge = """\n'
    )
    body = "\n".join(triples)
    footer = '\n"""\n'
    return header + body + footer


# =====================================================================
# Pipeline 3: Direct Neo4j (same KB schema as base.py)
# =====================================================================

# Neo4JPipeline removed — see file header. To re-enable scrape →
# rust-snapshot ingestion, add a pipeline that appends KB DSL
# strings to a ``.kb`` file the snapshot exporter reads alongside
# the curated sources.
