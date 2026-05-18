"""
dorian.mcp.stores
-----------------
Shared singleton instances for cross-module state.

Import ``draft_store`` wherever MCP tools, REST endpoints, or event
handlers need access to the in-memory draft staging area.
"""
from dorian.mcp.draft_store import DraftStore

draft_store = DraftStore()
