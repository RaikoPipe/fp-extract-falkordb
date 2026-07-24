"""Administrative tools for the knowledge graph."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._retry import with_retry


@tool
def reset_graph() -> str:
    """Delete all nodes and relationships from the knowledge graph.

    WARNING: This is destructive and cannot be undone. Only use when
    explicitly asked to reset or clear the graph.
    """
    return with_retry(lambda: _reset_graph_impl())


def _reset_graph_impl() -> str:
    get_backend().reset()
    return "Graph reset complete — all nodes and relationships deleted."


@tool
def use_graph(name: str) -> str:
    """Switch the active knowledge graph to ``name``.

    The agent operates against one graph at a time. This tool switches the
    backend's bound graph so subsequent queries, inspections, and ingestion
    target ``name``. ``name`` must be in the session's enabled (checked)
    graph set — out-of-scope names are rejected. Use ``list_graphs`` first
    to discover available names.
    """
    return with_retry(lambda: _use_graph_impl(name))


def _use_graph_impl(name: str) -> str:
    backend = get_backend()
    try:
        backend.set_active_graph(name)
    except ValueError as exc:
        return json.dumps(
            {"error": str(exc), "error_type": "ValueError", "active_graph": backend.graph_name},
            ensure_ascii=False,
        )
    return json.dumps(
        {"active_graph": backend.graph_name, "allowed_graphs": backend.allowed_graphs},
        ensure_ascii=False,
    )
