"""
dorian/pipeline/printout.py
----------------------------
Expansion rule for the ``dorian.io.printout`` platform operator.

``dorian.io.printout`` is a terminal (sink) node that formats the output of
an upstream operator for display.  At expansion time it is replaced by a
``Snippet`` whose inline code detects the data type and returns a structured
dict suitable for the frontend VisualizerNode.

Supported output formats:
  - **LLM response** — OpenAI-compatible ChatCompletion objects
    (``choices[0].message.content``, model, usage stats)
  - **JSON / dict** — arbitrary dicts or lists
  - **DataFrame** — pandas DataFrames (first 100 rows + shape/columns)
  - **ndarray** — numpy arrays (first 100 elements + shape/dtype)
  - **Scalar** — int, float, bool
  - **Text** — plain strings (with JSON auto-parse attempt)

Usage in the pipeline DAG::

    Operator(name="dorian.io.printout", language="python")

The node accepts **one positional input** (position 0) — the data to display.
It produces no meaningful downstream output (terminal node).
"""
from __future__ import annotations

from dorian.code.parsing.rule import Apply, RewriteRule
from dorian.dag import DAG, Edge, Node, Snippet


# ---------------------------------------------------------------------------
# Snippet code that runs at execution time
# ---------------------------------------------------------------------------

_PRINTOUT_SNIPPET_CODE = '''def foo(data):
    """Format pipeline output for display.

    Auto-detects the data type and returns a structured dict:
        {type: str, content: ..., ...metadata}

    Handles OpenAI-compatible ChatCompletion responses, dicts,
    DataFrames, numpy arrays, scalars, and strings.
    """
    import json as _json

    # -- Pydantic model (OpenRouter SDK, OpenAI SDK, etc.) --
    if hasattr(data, "model_dump"):
        return {"type": "json", "content": data.model_dump()}

    # -- object with .to_dict() (older SDKs) --
    if hasattr(data, "to_dict") and not hasattr(data, "columns"):
        return {"type": "json", "content": data.to_dict()}

    # -- dict --
    if isinstance(data, dict):
        return {"type": "json", "content": data}

    # -- DataFrame (pandas) --
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        rows = data.head(100).to_dict(orient="records")
        return {
            "type": "dataframe",
            "content": rows,
            "shape": list(data.shape),
            "columns": list(data.columns),
        }

    # -- ndarray (numpy) --
    if hasattr(data, "tolist") and hasattr(data, "shape") and hasattr(data, "dtype"):
        flat = data.flatten().tolist()[:100]
        return {
            "type": "array",
            "content": flat,
            "shape": list(data.shape),
            "dtype": str(data.dtype),
        }

    # -- list / tuple --
    if isinstance(data, (list, tuple)):
        items = list(data)[:100]
        return {"type": "json", "content": items}

    # -- scalar --
    if isinstance(data, (int, float, bool)):
        return {"type": "scalar", "content": data}

    # -- string (try JSON parse) --
    if isinstance(data, str):
        try:
            parsed = _json.loads(data)
            return {"type": "json", "content": parsed}
        except (ValueError, TypeError):
            pass
        return {"type": "text", "content": data}

    # -- fallback --
    return {"type": "text", "content": str(data)}
'''


# ---------------------------------------------------------------------------
# Expansion function
# ---------------------------------------------------------------------------

def _expand_printout(dag: DAG, mapping: dict, meta: dict) -> DAG:
    """Replace ``dorian.io.printout`` with a Snippet that formats the output.

    Incoming edges are rewired to the Snippet; outgoing edges (if any) are
    preserved for chaining, though printout is typically a terminal node.
    """
    nid = mapping["n"]

    incoming = [
        (e.source, e.position, e.output)
        for e in dag.edges if e.destination == nid
    ]
    outgoing = [
        (e.destination, e.position, e.output)
        for e in dag.edges if e.source == nid
    ]

    snippet_id = f"printout_{nid}"

    new_nodes = {k: v for k, v in dag.nodes.items() if k != nid}
    new_nodes[snippet_id] = Snippet(
        name="dorian.io.printout",
        code=_PRINTOUT_SNIPPET_CODE,
        language="python",
    )

    new_edges = [
        e for e in dag.edges if e.source != nid and e.destination != nid
    ]
    for src, pos, out in incoming:
        new_edges.append(Edge(src, snippet_id, position=pos, output=out))
    for dst, pos, out in outgoing:
        new_edges.append(Edge(snippet_id, dst, position=pos, output=out))

    return DAG(nodes=new_nodes, edges=new_edges)


# ---------------------------------------------------------------------------
# Rewrite rule
# ---------------------------------------------------------------------------

PRINTOUT_EXPANSION_RULE = RewriteRule(
    pattern=DAG(
        nodes={"n": Node(type="Operator", text=r"dorian\.io\.printout")},
        edges=[],
    ),
    description="expand dorian.io.printout to a type-detecting display Snippet",
    transformations=[Apply(f=_expand_printout)],
)


# ---------------------------------------------------------------------------
# Public entry point (synchronous)
# ---------------------------------------------------------------------------

def expand_printout_nodes(pipeline: DAG, session: str) -> DAG:
    """Expand all ``dorian.io.printout`` nodes before the Dask graph is built.

    Called from ``run_pipeline`` after compound operator expansion and before
    the platform-operator guard.

    Set ``DORIAN_USE_RUST_EXPAND_PRINTOUT=1`` to route through the
    rust port; the python ``sync_apply`` path stays as the fallback.
    """
    import os as _os
    if _os.environ.get("DORIAN_USE_RUST_EXPAND_PRINTOUT", "").lower() in ("1", "true", "yes", "on"):
        try:
            import json as _json
            import dorian_native  # type: ignore
            expanded = dorian_native.expand_printout_nodes(
                _json.dumps(pipeline.to_json_dict())
            )
            return DAG.from_json_dict(_json.loads(expanded))
        except Exception as exc:  # noqa: BLE001
            try:
                from backend.events import Event, emit
                emit(Event("ExpandPrintoutRustFallback", {"error": str(exc)}))
            except Exception:
                pass

    from dorian.pipeline.transforms import sync_apply
    return sync_apply(PRINTOUT_EXPANSION_RULE, pipeline, {"session": session})
