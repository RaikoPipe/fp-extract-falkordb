"""Shared progress UI for ingestion runs, driven through the AgentTodos CustomElement.

Both ingestion entry points — the ``ingest_documents`` action button
(:func:`falkordb_harness.chainlit_app.on_ingest_documents`) and the agent's
``extract_and_write`` tool (when invoked through the Chainlit UI) — update the
same ``AgentTodos`` CustomElement via :func:`make_ingestion_progress`. The
element is created lazily (by the agent's ``write_todos`` tool or by this
factory) and stored in ``cl.user_session``.

The factory returns a ``(None, progress, finalize)`` tuple where ``progress``
matches :data:`ingest_runner.ProgressFn` and writes per-stage
:class:`TimeEstimator` snapshots into the element's ``stages`` prop.
``finalize`` flips ``ingestion_running`` to ``False`` and marks every
still-running stage as ``done``; it does **not** clear the ``stages`` prop,
so the panel persists as a chronological record of the run (the
``AgentTodos`` card is intentionally not collapsed/removed — see the
comment in ``AgentTodos.jsx`` about the ``Node.removeChild`` error that
motivated the stable-DOM approach).

Long-running stages (``extract`` and ``write``) emit granular ``progress``
events (``details = {"kind": "progress", "stage", "completed", "total"}``)
once per chunk / extraction. The handler feeds those into a per-stage
:class:`TimeEstimator` and writes the rendered fields (``n``, ``total``,
``percent``, ``elapsed_str``, ``eta_str``, ``rate``) into the element's
``stages`` prop. The ``AgentTodos.jsx`` component formats the tqdm-style line
client-side from those fields.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import chainlit as cl

from falkordb_harness.i18n import t
from falkordb_harness.ingest_runner import ProgressFn
from falkordb_harness.progress_eta import TimeEstimator

logger = logging.getLogger("falkordb_harness.chainlit_progress")


async def _sync_update(el: cl.CustomElement) -> None:
    """Refresh ``el.content`` from ``el.props`` then call ``el.update()``.

    Chainlit's ``CustomElement.__post_init__`` serializes ``props`` into
    ``content`` (a JSON string) at construction time. ``update()`` calls
    ``super().send()`` which re-persists the file at ``chainlit_key`` using
    ``self.content`` — but ``content`` is never refreshed after construction,
    so every ``update()`` re-persists the ORIGINAL props. If the frontend
    re-fetches content via ``chainlitKey`` (e.g. on re-render or when the
    WebSocket event is dropped), it sees stale props — the ``done`` flag on
    ingestion stages never reaches the wire, so checkmarks never appear.

    Refreshing ``content`` here ensures both the WebSocket payload
    (``to_dict()`` reads ``self.props`` directly) and the persisted file
    (``_create`` uses ``self.content``) carry the current props.
    """
    el.content = json.dumps(el.props)
    await el.update()


async def _get_or_create_todos_element(
    *,
    initial_todos: list | None = None,
    ingestion_running: bool = True,
) -> cl.CustomElement:
    """Return the session's AgentTodos element, creating it if absent.

    Both the agent path (first ``write_todos`` call, via ``on_tool_start``
    in ``chainlit_app.on_message``) and the action-button path (no
    ``write_todos``) route through this factory. The element is **always
    hosted by its own standalone ``cl.Message``** — never attached to the
    turn's ``response_msg``. This makes it immune to later
    ``response_msg.elements = [...]`` overwrites (e.g. the
    ``pending_elements`` reassignment at the end of ``on_message``),
    which previously raced with the element's ``el.update()`` and caused
    ``Node.removeChild`` errors in Chainlit's React tree.

    Inside ``on_message``, Chainlit's ``local_steps`` contextvar
    auto-parents the host message to the ``on_message`` run step so it
    nests in chronological order. In the ``on_ingest_documents`` action
    callback (no ``local_steps`` entry) the message is top-level, which
    is correct for that standalone path.

    The ``agent_todos.js`` custom_js portals the rendered card into the
    Chainlit composer (``#message-composer``) while ``active`` is true so
    it pins above the chat input regardless of chat history length, then
    restores it to its in-chat-flow position when the run concludes.
    """
    el = cl.user_session.get("agent_todos_el")
    if el is not None:
        if initial_todos is not None:
            el.props["todos"] = initial_todos
        el.props["ingestion_running"] = ingestion_running
        el.props["active"] = bool(el.props.get("todos")) or ingestion_running
        await _sync_update(el)
        return el
    el = cl.CustomElement(name="AgentTodos", props={
        "todos": list(initial_todos or []),
        "stages": {},
        "ingestion_running": ingestion_running,
        "active": bool(initial_todos) or ingestion_running,
        "lang": cl.user_session.get("lang", "en"),
    })
    cl.user_session.set("agent_todos_el", el)
    msg = cl.Message(content="", elements=[el])
    await msg.send()
    return el


async def make_ingestion_progress() -> (
    tuple[None, ProgressFn, Callable[[bool], Awaitable[None]]]
):
    """Build a live progress callback that updates the AgentTodos element.

    Returns:
        ``(None, progress, finalize)`` where:

        - The first element is ``None`` (kept for backward compatibility
          with callers that unpack three values; the ``TaskList`` is gone).
        - ``progress`` is an :data:`ingest_runner.ProgressFn` that writes
          per-stage estimator snapshots into the element's ``stages`` prop.
        - ``finalize(success: bool)`` flips ``ingestion_running`` to
          ``False`` and marks every still-running stage as ``done`` so the
          panel persists as a chronological record of the run (the
          ``AgentTodos`` card is intentionally not collapsed/removed — see
          the comment in ``AgentTodos.jsx`` about the ``Node.removeChild``
          error that motivated the stable-DOM approach).
    """
    el = await _get_or_create_todos_element()

    _stage_titles = {
        "stage": t("ingest.stage.stage"),
        "preprocess": t("ingest.stage.preprocess"),
        "chunk": t("ingest.stage.chunk"),
        "extract": t("ingest.stage.extract"),
        "write": t("ingest.stage.write"),
    }
    stage_estimators: dict[str, TimeEstimator] = {}
    stage_base_titles: dict[str, str] = {}
    current_stages: dict[str, dict] = {}

    async def progress(label: str, details: dict | None = None) -> None:
        kind = (details or {}).get("kind", "info")
        stage = (details or {}).get("stage", "")

        if kind == "stage_start":
            base_title = _stage_titles.get(stage, stage)
            stage_base_titles[stage] = base_title
            current_stages[stage] = {
                "title": base_title,
                "n": 0,
                "total": 0,
                "percent": 0,
                "elapsed_str": "0:00",
                "eta_str": "?",
                "rate": 0,
            }
            el.props["stages"] = dict(current_stages)
            await _sync_update(el)
        elif kind == "stage_end":
            stage_estimators.pop(stage, None)
            if stage in current_stages:
                current_stages[stage]["title"] = stage_base_titles.get(stage, stage)
                # Mark the stage as concluded so ``AgentTodos.jsx`` swaps
                # the animated spinner for a checkmark. Keep the frozen
                # snapshot (n/total/percent/timing) in the panel so the
                # block persists as a record of what ran.
                current_stages[stage]["done"] = True
                el.props["stages"] = dict(current_stages)
                await _sync_update(el)
        elif kind == "progress":
            completed = int((details or {}).get("completed", 0) or 0)
            total = int((details or {}).get("total", 0) or 0)
            est = stage_estimators.get(stage)
            if est is None:
                est = TimeEstimator(total=total)
                stage_estimators[stage] = est
            elif est.total != total and total > 0:
                est.total = total
            delta = completed - est.n
            if delta > 0:
                est.update(delta)
            base = stage_base_titles.get(stage, stage)
            rendered = est.render()
            current_stages[stage] = {"title": base, **rendered}
            el.props["stages"] = dict(current_stages)
            await _sync_update(el)

    async def finalize(success: bool) -> None:
        # Keep the concluded stages visible (the block persists as a
        # chronological record). Only flip the run flag and mark any
        # stage that never sent ``stage_end`` (e.g. cancelled mid-run)
        # as done so its spinner is replaced by a checkmark.
        el.props["ingestion_running"] = False
        el.props["active"] = bool(el.props.get("todos")) or False
        for s in current_stages.values():
            s["done"] = True
        if current_stages:
            el.props["stages"] = dict(current_stages)
        try:
            await _sync_update(el)
        except Exception as exc:  # noqa: BLE001 — never strand the panel
            logger.warning("ingest progress finalize update failed: %s", exc)

    return None, progress, finalize


__all__ = ["make_ingestion_progress", "_sync_update"]
