"""Tools for inspecting graph schema, nodes, and edges."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._retry import with_retry


@tool
def get_schema() -> str:
    """Return the graph schema: node labels, relationship types, and property keys.

    Call this before querying to understand what's in the graph.
    """
    return with_retry(lambda: _get_schema_impl())


def _get_schema_impl() -> str:
    schema = get_backend().get_schema_info()
    return json.dumps(schema, indent=2, ensure_ascii=False)


@tool
def list_nodes(limit: int = 50) -> str:
    """List nodes in the knowledge graph.

    Returns up to `limit` nodes with their labels and properties.
    Use a small limit to avoid overwhelming output.
    """
    return with_retry(lambda: _list_nodes_impl(limit))


def _list_nodes_impl(limit: int) -> str:
    nodes = get_backend().get_all_nodes()
    return json.dumps(nodes[:limit], indent=2, ensure_ascii=False, default=str)


@tool
def list_edges(limit: int = 50) -> str:
    """List edges (relationships) in the knowledge graph.

    Each edge is [source_name, target_name, relationship_type, properties].
    Returns up to `limit` edges.
    """
    return with_retry(lambda: _list_edges_impl(limit))


def _list_edges_impl(limit: int) -> str:
    edges = get_backend().get_all_edges()
    result = [
        {"source": src, "target": tgt, "type": rel, "properties": props}
        for src, tgt, rel, props in edges[:limit]
    ]
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@tool
def node_count() -> str:
    """Return the total number of nodes in the knowledge graph."""
    return with_retry(lambda: _node_count_impl())


def _node_count_impl() -> str:
    count = get_backend().node_count()
    return str(count)


@tool
def list_graphs() -> str:
    """List all knowledge graphs available in the FalkorDB instance.

    Returns the graph names known to the FalkorDB server (``GRAPH.LIST``),
    as a JSON array of strings. This is an instance-level listing — it is
    NOT restricted to the graphs the user has enabled for this session
    (the session's enabled set is surfaced in the system prompt). Use this
    to discover what graphs exist before switching with ``use_graph``.
    """
    return with_retry(lambda: _list_graphs_impl())


def _list_graphs_impl() -> str:
    names = get_backend().list_graphs()
    return json.dumps(names, ensure_ascii=False)
