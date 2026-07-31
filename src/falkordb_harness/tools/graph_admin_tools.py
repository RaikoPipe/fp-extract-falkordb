"""Knowledge-graph lifecycle tools: create, switch-with-confirm, describe.

These tools implement the revised KG policy:

- ``create_graph(name, description)`` — create a new KG and make it active.
  Only allowed when the session started with no graph selected. The LLM
  picks both the name and an initial 1-3 sentence description from the
  user's ingestion intent. No user confirmation (decided on user sentiment).
- ``request_graph_switch(name)`` — ask the user to confirm switching the
  active graph via an AskActionMessage (reuses the ui_prompts bridge).
  Sets a per-session approval stamp that ``use_graph`` checks before
  performing the switch.
- ``describe_graph(name="")`` — read one graph's description, or all
  descriptions when ``name`` is empty. The LLM's first-contact point for
  understanding existing KGs.
- ``update_graph_description(description)`` — revise the active graph's
  description. The LLM calls this after every successful ingestion so the
  description reflects the graph's current contents.

The per-session ``graph_switch_approved`` stamp (stored in
``cl.user_session``) enforces the confirmation gate: ``use_graph`` (in
``admin_tools.py``) refuses to switch unless a prior
``request_graph_switch`` set the stamp to the same name. The UI sidebar
dropdown path (``on_settings_update``) is a direct user action and sets
the stamp itself, so it is not blocked.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.i18n import t
from falkordb_harness.tools._retry import awith_retry

logger = logging.getLogger("falkordb_harness.tools.graph_admin")


# ---------------------------------------------------------------------------
# create_graph
# ---------------------------------------------------------------------------


@tool
async def create_graph(name: str, description: str) -> str:
    """Create a new knowledge graph and make it the active graph.

    Only allowed when NO graph is currently selected for the session (the
    preamble will say so). ``name`` is the new graph's name (must not
    already exist on the FalkorDB instance). ``description`` is a concise
    1-3 sentence summary of the graph's intended scope, derived from the
    user's ingestion intent — this is stored as the graph's description
    and refined after each ingestion via ``update_graph_description``.

    No user confirmation is requested — decide from user sentiment. After
    this call succeeds, the new graph is active and ingestion can proceed.
    """
    return await awith_retry(lambda: _create_graph_impl(name, description))


async def _create_graph_impl(name: str, description: str) -> str:
    name = (name or "").strip()
    description = (description or "").strip()
    if not name:
        return json.dumps(
            {"error": "Graph name must be a non-empty string.", "created": False},
            ensure_ascii=False,
        )
    backend = get_backend()
    # Enforce the no-graph-only rule: refuse if a graph is already active.
    active = backend.graph_name
    if active:
        return json.dumps(
            {
                "error": (
                    f"A graph is already active ('{active}'). create_graph is "
                    "only allowed when no graph is selected. Use "
                    "request_graph_switch + use_graph to switch instead."
                ),
                "created": False,
                "active_graph": active,
            },
            ensure_ascii=False,
        )
    # Materialize the graph on the instance (rejects duplicates).
    backend.create_graph(name)
    # Activate it and ensure it's in the allowlist (create_graph appends).
    backend.set_active_graph(name)
    # Seed the description row.
    try:
        from falkordb_harness.graph_descriptions import set_description

        await set_description(name, description)
    except Exception as exc:  # noqa: BLE001 — never block creation on desc write
        logger.warning("set_description failed for new graph %r: %s", name, exc)
    # Persist as the user's last-used graph + update session state.
    await _persist_last_graph(name)
    _sync_session_selection(name, backend.allowed_graphs or [name])
    _set_switch_approval(name)
    return json.dumps(
        {
            "active_graph": backend.graph_name,
            "allowed_graphs": backend.allowed_graphs,
            "created": True,
            "description": description,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# request_graph_switch
# ---------------------------------------------------------------------------


@tool
async def request_graph_switch(name: str) -> str:
    """Ask the user to confirm switching the active knowledge graph to ``name``.

    Call this BEFORE ``use_graph``. The user is shown a Confirm/Cancel
    prompt; on confirm, a per-session approval stamp is set so the
    subsequent ``use_graph(name)`` call succeeds. On cancel (or timeout)
    the stamp is NOT set and ``use_graph`` will refuse the switch.

    ``name`` must exist on the FalkorDB instance — call ``list_graphs``
    first to discover available names. Reading graph descriptions via
    ``describe_graph`` can help the user decide.
    """
    return await awith_retry(lambda: _request_graph_switch_impl(name))


async def _request_graph_switch_impl(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return json.dumps(
            {"confirmed": False, "requested_graph": "", "error": "Empty graph name."},
            ensure_ascii=False,
        )
    backend = get_backend()
    # Validate the graph exists on the instance.
    try:
        available = backend.list_graphs()
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"confirmed": False, "requested_graph": name, "error": str(exc)},
            ensure_ascii=False,
        )
    if name not in available:
        return json.dumps(
            {
                "confirmed": False,
                "requested_graph": name,
                "error": (
                    f"Graph '{name}' does not exist on the FalkorDB instance. "
                    "Available graphs: " + ", ".join(sorted(available))
                ),
            },
            ensure_ascii=False,
        )
    # Ask the user to confirm via the UI bridge.
    from falkordb_harness.ui_prompts import prompt_confirm

    summary = t("graph.switch.confirm", name=name)
    decision = await prompt_confirm(summary)
    confirmed = decision == "confirmed"
    if confirmed:
        _set_switch_approval(name)
    return json.dumps(
        {"confirmed": confirmed, "requested_graph": name},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# describe_graph
# ---------------------------------------------------------------------------


@tool
async def describe_graph(name: str = "") -> str:
    """Read knowledge-graph description(s).

    With no ``name`` argument, returns ALL graph descriptions as a JSON
    array of ``{name, description, updatedAt}`` — call this as your
    FIRST step when you need to understand the existing knowledge graphs
    (before switching or answering "what's in this graph?"). With a
    ``name``, returns just that graph's description (or ``null`` if it
    has no description row yet).
    """
    return await awith_retry(lambda: _describe_graph_impl(name))


async def _describe_graph_impl(name: str) -> str:
    from falkordb_harness.graph_descriptions import (
        get_description_row,
        list_descriptions,
    )

    n = (name or "").strip()
    if n:
        row = await get_description_row(n)
        if row is None:
            return json.dumps(
                {"name": n, "description": None, "updatedAt": None},
                ensure_ascii=False,
            )
        return json.dumps(row, ensure_ascii=False)
    all_descs = await list_descriptions()
    return json.dumps(all_descs, ensure_ascii=False)


# ---------------------------------------------------------------------------
# update_graph_description
# ---------------------------------------------------------------------------


@tool
async def update_graph_description(description: str) -> str:
    """Revise the active knowledge graph's description.

    Call this AFTER every successful ingestion so the description
    accurately reflects the graph's current contents (entities, source
    documents, scope). ``description`` is a concise 1-3 sentence summary
    that REPLACES the previous description. The description is the first
    thing read (via ``describe_graph``) when understanding the graph.
    """
    return await awith_retry(lambda: _update_graph_description_impl(description))


async def _update_graph_description_impl(description: str) -> str:
    description = (description or "").strip()
    if not description:
        return json.dumps(
            {"error": "description must be a non-empty string.", "updated": False},
            ensure_ascii=False,
        )
    backend = get_backend()
    active = backend.graph_name
    if not active:
        return json.dumps(
            {"error": "No active graph to update.", "updated": False},
            ensure_ascii=False,
        )
    from falkordb_harness.graph_descriptions import set_description

    await set_description(active, description)
    return json.dumps(
        {"updated": True, "active_graph": active, "description": description},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Shared session helpers
# ---------------------------------------------------------------------------


def _set_switch_approval(name: str) -> None:
    """Set the per-session stamp that ``use_graph`` checks before switching."""
    try:
        import chainlit as cl

        cl.user_session.set("graph_switch_approved", name)
    except Exception:  # noqa: BLE001 — not in a Chainlit context (CLI)
        pass


def get_switch_approval() -> str | None:
    """Return the currently-approved graph name, or ``None``."""
    try:
        import chainlit as cl

        return cl.user_session.get("graph_switch_approved")
    except Exception:  # noqa: BLE001
        return None


def _sync_session_selection(active: str, allowed: list[str]) -> None:
    """Stash the new graph selection into ``cl.user_session`` (Chainlit path)."""
    try:
        import chainlit as cl

        cl.user_session.set(
            "graph_selection",
            {"active_graph": active, "allowed_graphs": allowed},
        )
        cl.user_session.set("graph_selection_dirty", True)
    except Exception:  # noqa: BLE001
        pass


async def _persist_last_graph(graph: str) -> None:
    """Persist the graph as the user's last-used graph (Chainlit path)."""
    try:
        import chainlit as cl

        ident = cl.user_session.get("user_identifier")
    except Exception:  # noqa: BLE001
        return
    if not ident:
        return
    try:
        from falkordb_harness.graph_descriptions import set_last_graph

        await set_last_graph(ident, graph)
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_last_graph failed: %s", exc)