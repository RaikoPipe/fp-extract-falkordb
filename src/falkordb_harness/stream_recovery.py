"""Recovery of in-flight assistant streams across UI reconnects.

Problem
-------
When the user reloads the page or switches to another thread and back
*while the assistant is still generating an answer*, the Chainlit
websocket disconnects. The ``on_message`` asyncio task is **not**
cancelled by a disconnect (only the ``stop`` socket event cancels
``current_task``), so the agent keeps streaming tokens in the
background. Two gaps make the UI lose the in-flight text on reconnect:

1. ``cl.Message.stream_token`` only emits to the *live* socket — it
   never persists. The step row written by the initial
   ``response_msg.send()`` carries an empty ``output``, so the
   ``resume_thread`` payload rendered on reconnect shows a blank
   assistant message.
2. Tokens generated *during* the disconnection window are emitted to a
   dead socket and vanish; they are neither in the DB nor re-sent.

On reconnect, ``connection_successful`` (chainlit/socket.py) runs
``on_chat_resume`` and **then** emits ``resume_thread`` — which replaces
the frontend's message list with the persisted steps (assistant message
``output=""``). The background task's later ``stream_token`` calls do
reach the reconnected socket (``WebsocketSession.restore`` rebinds
``session.emit`` to the new socket id), so *new* tokens append, but the
already-generated prefix is missing until the final ``response_msg.update()``
persists + emits the full text.

Recovery design (single-process, phase 1)
-----------------------------------------
A module-level registry maps ``thread_id`` -> :class:`ActiveStream`
(the live ``cl.Message`` handle + a snapshot of its accumulated
content + a ``threading.Lock`` for atomic content reads).

* ``on_message`` registers the stream before the first token and
  deregisters it in a ``finally`` once the stream concludes.
* ``on_chat_resume`` schedules a one-shot task that, after
  ``resume_thread`` has rendered the persisted (empty) assistant
  placeholder, emits an ``update_message`` for that step id carrying
  the accumulated content so far. Subsequent live ``stream_token``
  calls from the background task then append to the now-populated
  message, restoring seamless streaming.

Single-process only: the registry lives in the memory of the process
that owns the generating task. Multi-host hosting (phase 2+) would
require a shared fan-out layer (e.g. Redis Pub/Sub) plus a shared
LangGraph checkpointer; see the plan discussion.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("falkordb_harness.stream_recovery")


@dataclass
class ActiveStream:
    """An in-flight assistant stream recoverable across reconnects.

    Attributes:
        message: The live ``cl.Message`` instance (carries the stable
            step ``id`` and is the object the background task keeps
            calling ``stream_token`` / ``update`` on). Its ``.content``
            is kept in sync with the streamed text by ``stream_token``
            itself (``self.content += token``), so the recovery task
            reads it directly as the source of truth for the replay —
            no separate snapshot is maintained.
    """

    message: Any


# Module-level registry: thread_id -> ActiveStream. A single Chainlit
# process owns every generating task, so this is the authoritative map.
# Protected by _REGISTRY_LOCK so concurrent on_message / on_chat_resume
# / deregister calls don't race.
_active_streams: Dict[str, ActiveStream] = {}
_REGISTRY_LOCK = threading.Lock()


def register_stream(thread_id: str, message: Any) -> ActiveStream:
    """Record that ``message`` is streaming for ``thread_id``.

    Overwrites any prior entry for the thread (a prior stream should
    already have deregistered; if not, the new stream supersedes it).
    Returns the :class:`ActiveStream` for the caller to update as
    tokens arrive.
    """
    stream = ActiveStream(message=message)
    with _REGISTRY_LOCK:
        _active_streams[thread_id] = stream
    return stream


def deregister_stream(thread_id: str) -> None:
    """Remove the stream for ``thread_id`` (call when the stream ends)."""
    with _REGISTRY_LOCK:
        _active_streams.pop(thread_id, None)


def get_active_stream(thread_id: str) -> Optional[ActiveStream]:
    """Return the active stream for ``thread_id`` or ``None``."""
    with _REGISTRY_LOCK:
        return _active_streams.get(thread_id)


async def replay_inflight_stream(thread_id: str) -> None:
    """Re-emit the in-flight assistant message to the reconnected socket.

    Called from ``on_chat_resume``. Waits one event-loop tick so that
    Chainlit's ``resume_thread`` socket event (emitted immediately
    *after* ``on_chat_resume`` returns, see chainlit/socket.py
    ``connection_successful``) has finished replacing the frontend's
    message list with the persisted (empty-output) assistant placeholder.
    Then, if a stream is still active for ``thread_id``, emits an
    ``update_message`` for that step id carrying the accumulated content
    so far — so the placeholder is filled with the real prefix and the
    background task's subsequent live ``stream_token`` calls append to
    it seamlessly.

    Safe to call when no stream is active (e.g. resuming a fully
    settled thread): it's a no-op then.
    """
    stream = get_active_stream(thread_id)
    if stream is None:
        return

    # Yield so connection_successful can finish emitting resume_thread
    # (which replaces the frontend message list) before our update
    # lands. A single tick is sufficient because resume_thread's emit
    # is synchronous and awaited in the same coroutine that called
    # on_chat_resume; a short bounded sleep adds robustness against
    # scheduling jitter without delaying the user-visible recovery.
    await asyncio.sleep(0.05)

    # Re-fetch: the stream may have concluded during the sleep.
    stream = get_active_stream(thread_id)
    if stream is None:
        return
    message = stream.message
    # message.content is kept in sync with the streamed text by
    # stream_token (self.content += token); a rebind is atomic under
    # CPython so reading it here from a different task is safe.
    content = message.content or ""
    if not content:
        return

    try:
        import chainlit as cl

        # Force streaming=True on the snapshot we emit so the frontend
        # renders the message as an active stream (the background task
        # will keep sending stream_token events for the same id). Then
        # emit an update_message with the accumulated prefix; the
        # frontend's update_message handler merges by id, replacing the
        # empty-output placeholder rendered by resume_thread.
        message.streaming = True
        step_dict = message.to_dict()
        await cl.context.emitter.update_step(step_dict)
        logger.info(
            "Replayed in-flight stream for thread %s (%d chars)",
            thread_id,
            len(content),
        )
    except Exception as exc:  # noqa: BLE001 — never break the reconnect
        logger.warning("Stream replay failed for thread %s: %s", thread_id, exc)