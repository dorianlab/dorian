"""
dorian.mcp — LLM agent toolkit for rewrite rules and mitigation curation.

Exposes four tool namespaces:

  kb/*          — Knowledge base queries (risks, mitigations, operators)
  dag/*         — DAG inspection, diffing, validation, dry-run rewrites
  rule/*        — Rewrite rule authoring (create, test, commit)
  mitigation/*  — Mitigation curation pipeline (ingest, extract, propose, test, commit)

Access modes
~~~~~~~~~~~~
**FastAPI router (primary)** — mounted on the existing backend at ``/mcp/...``::

    # Already wired in main.py:
    from dorian.mcp import router as mcp
    app.include_router(mcp.router)

    # Agents call: POST /mcp/rule/create, GET /mcp/kb/risks, etc.

**Standalone MCP server (optional)** — for agents that speak MCP natively::

    python -m dorian.mcp                 # stdio transport (local agents)
    python -m dorian.mcp --http          # streamable HTTP (remote agents)

**In-process** — import tool functions directly::

    from dorian.mcp.rule_tools import rule_create
    from dorian.mcp.dag_tools import dag_inspect

Architecture
~~~~~~~~~~~~
- ``router.py``          — FastAPI router (``/mcp/...``) on the existing backend
- ``server.py``          — Optional standalone FastMCP server
- ``draft_store.py``     — In-memory staging area (DraftRule, DraftMitigation)
- ``rule_compiler.py``   — JSON rule spec → RewriteRule compiler (replaces eval)
- ``kb_tools.py``        — Neo4j KB query implementations
- ``dag_tools.py``       — DAG inspection, diffing, validation
- ``rule_tools.py``      — Rule authoring lifecycle (create → test → commit)
- ``mitigation_tools.py``— Mitigation curation lifecycle + extraction wrappers
- ``extraction.py``      — KBExtraction pipeline stages (decompose, similarity, novelty, triplets)
- ``prompts.py``         — Prompt definitions for the two agent workflows

Configuration
~~~~~~~~~~~~~
All settings in ``config/config.yaml`` under the ``mcp`` key:

.. code-block:: yaml

    mcp:
      server:
        name: dorian-mcp
        transport: stdio
        http_port: 8765
      extraction:
        llm_backend: groq
        llm_model: llama-3.3-70b-versatile
        llm_api_key: ""             # or env DORIAN_MCP_EXTRACTION_LLM_API_KEY
        embedding_model: all-MiniLM-L6-v2
        similarity_threshold: 0.45
"""
