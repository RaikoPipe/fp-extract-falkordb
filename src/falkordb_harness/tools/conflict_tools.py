"""Tools for inspecting and clearing merge conflicts."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend


@tool
def get_conflicts(label: str = "") -> str:
    """List nodes that have merge conflicts.

    When documents are ingested with merge_mode='conflict', disagreements
    between sources are recorded. This tool retrieves those conflict records.
    Optionally filter by node label (e.g. 'Resource', 'Product').
    """
    backend = get_backend()
    conflicts = backend.get_conflicts(label=label or None)
    return json.dumps(conflicts, indent=2, ensure_ascii=False, default=str)


@tool
def clear_conflicts(label: str = "", name: str = "") -> str:
    """Dismiss reviewed conflicts by clearing the conflicts property.

    Optionally filter by label and/or entity name. Returns the number
    of nodes updated.
    """
    backend = get_backend()
    count = backend.clear_conflicts(
        label=label or None,
        name=name or None,
    )
    return f"{count} node(s) updated"
