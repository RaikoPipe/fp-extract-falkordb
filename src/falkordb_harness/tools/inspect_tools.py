"""Tools for inspecting graph schema, nodes, and edges."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend


@tool
def get_schema() -> str:
    """Return the graph schema: node labels, relationship types, and property keys.

    Call this before querying to understand what's in the graph.
    """
    schema = get_backend().get_schema_info()
    return json.dumps(schema, indent=2, ensure_ascii=False)


@tool
def list_nodes(limit: int = 50) -> str:
    """List nodes in the knowledge graph.

    Returns up to `limit` nodes with their labels and properties.
    Use a small limit to avoid overwhelming output.
    """
    nodes = get_backend().get_all_nodes()
    return json.dumps(nodes[:limit], indent=2, ensure_ascii=False, default=str)


@tool
def list_edges(limit: int = 50) -> str:
    """List edges (relationships) in the knowledge graph.

    Each edge is [source_name, target_name, relationship_type, properties].
    Returns up to `limit` edges.
    """
    edges = get_backend().get_all_edges()
    result = [
        {"source": src, "target": tgt, "type": rel, "properties": props}
        for src, tgt, rel, props in edges[:limit]
    ]
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


@tool
def node_count() -> str:
    """Return the total number of nodes in the knowledge graph."""
    count = get_backend().node_count()
    return str(count)
