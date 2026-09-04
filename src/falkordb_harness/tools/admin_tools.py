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

    REQUIRES PRIOR CONFIRMATION: you MUST call ``request_graph_switch(name)``
    first and wait for the user to confirm. This tool refuses to switch if
    no confirmation stamp for ``name`` is set. (The UI sidebar dropdown
    path sets the stamp itself, so it is not blocked.)
    """
    return with_retry(lambda: _use_graph_impl(name))


def _use_graph_impl(name: str) -> str:
    from falkordb_harness.tools.graph_admin_tools import get_switch_approval

    backend = get_backend()
    # Enforce the confirmation gate: refuse unless a prior
    # request_graph_switch (or the UI dropdown) set the approval stamp.
    approved = get_switch_approval()
    if approved != name:
        return json.dumps(
            {
                "error": (
                    f"Graph switch to '{name}' was not confirmed by the user. "
                    "Call request_graph_switch(name) first and wait for "
                    "confirmation before calling use_graph."
                ),
                "error_type": "NotConfirmed",
                "active_graph": backend.graph_name,
            },
            ensure_ascii=False,
        )
    try:
        backend.set_active_graph(name)
    except ValueError as exc:
        return json.dumps(
            {"error": str(exc), "error_type": "ValueError", "active_graph": backend.graph_name},
            ensure_ascii=False,
        )
    # Clear the stamp after a successful switch so a later unconfirmed
    # switch to a different graph is still blocked.
    try:
        import chainlit as cl

        cl.user_session.set("graph_switch_approved", None)
    except Exception:  # noqa: BLE001 — CLI path
        pass
    # Persist the new active graph as the user's last-used graph and sync
    # the session selection so the UI badge / sidebar stay accurate. Uses
    # the SYNC sqlite writer because this tool is sync and runs inside the
    # Chainlit event loop (no await / nested-loop possible).
    try:
        from falkordb_harness.tools.graph_admin_tools import _sync_session_selection

        _sync_session_selection(backend.graph_name, backend.allowed_graphs or [backend.graph_name])
    except Exception:  # noqa: BLE001
        pass
    try:
        import chainlit as cl

        ident = cl.user_session.get("user_identifier")
    except Exception:  # noqa: BLE001
        ident = None
    if ident:
        try:
            from falkordb_harness.graph_descriptions import set_last_graph_sync

            set_last_graph_sync(ident, backend.graph_name)
        except Exception:  # noqa: BLE001 — never block the switch on persistence
            pass
    return json.dumps(
        {"active_graph": backend.graph_name, "allowed_graphs": backend.allowed_graphs},
        ensure_ascii=False,
    )
