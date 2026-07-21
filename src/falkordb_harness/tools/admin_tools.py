"""Administrative tools for the knowledge graph."""

from __future__ import annotations

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend


@tool
def reset_graph() -> str:
    """Delete all nodes and relationships from the knowledge graph.

    WARNING: This is destructive and cannot be undone. Only use when
    explicitly asked to reset or clear the graph.
    """
    get_backend().reset()
    return "Graph reset complete — all nodes and relationships deleted."
