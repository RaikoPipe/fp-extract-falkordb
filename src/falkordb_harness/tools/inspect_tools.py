"""Tools for inspecting graph schema and listing available graphs."""

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
