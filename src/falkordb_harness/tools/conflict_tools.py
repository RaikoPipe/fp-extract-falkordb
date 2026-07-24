"""Tools for inspecting and clearing merge conflicts."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._retry import with_retry


@tool
def get_conflicts(label: str = "") -> str:
    """List nodes that have merge conflicts.

    When documents are ingested with merge_mode='conflict', disagreements
    between sources are recorded. This tool retrieves those conflict records.
    Optionally filter by node label (e.g. 'Resource', 'Product').
    """
    return with_retry(lambda: _get_conflicts_impl(label))


def _get_conflicts_impl(label: str) -> str:
    backend = get_backend()
    conflicts = backend.get_conflicts(label=label or None)
    return json.dumps(conflicts, indent=2, ensure_ascii=False, default=str)


@tool
def clear_conflicts(label: str = "", name: str = "") -> str:
    """Dismiss reviewed conflicts by clearing the conflicts property.

    Optionally filter by label and/or entity name. Returns the number
    of nodes updated.
    """
    return with_retry(lambda: _clear_conflicts_impl(label, name))


def _clear_conflicts_impl(label: str, name: str) -> str:
    backend = get_backend()
    count = backend.clear_conflicts(
        label=label or None,
        name=name or None,
    )
    return f"{count} node(s) updated"
