"""Interactive UI prompts backed by Chainlit Ask*Message flows.

These tools let the agent ask the user a yes/no or free-text question during
a run. In the Chainlit UI they emit an ``AskActionMessage`` /
``AskUserMessage`` and block until the user responds; in the CLI (or any
non-Chainlit runtime) they fall back to ``input()`` so the same agent graph
works in both frontends.

The indirection exists because LangGraph tools are plain async functions
that run inside the agent's event loop, while the Chainlit ``Ask*Message``
API must be called from a Chainlit handler task. We avoid that coupling by
having the Chainlit layer install an async callback into a module-level
registry at startup; the tool calls the registered callback (UI path) or
falls back to stdin (CLI path).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

from falkordb_harness.i18n import t

logger = logging.getLogger("falkordb_harness.ui_prompts")

# A UI callback takes a prompt string (and optional actions) and returns the
# user's choice. The Chainlit layer installs one per session via
# ``set_ui_callback``; tools retrieve it via ``get_ui_callback``.
UiCallback = Callable[..., Awaitable[str]]


def _session_key() -> str:
    """Return a per-session key for the callback registry.

    Uses Chainlit's user_session id when available; falls back to a
    module-level single-slot (CLI path) so the CLI doesn't need Chainlit.
    """
    try:
        import chainlit as cl

        sid = cl.user_session.get("id") if cl.user_session else None
        if sid:
            return str(sid)
    except Exception:  # noqa: BLE001 — not in a Chainlit context
        logger.debug("not in a Chainlit context; using CLI session key")
    return "__cli__"


_CALLBACKS: dict[str, UiCallback] = {}


def set_ui_callback(cb: UiCallback) -> None:
    """Install the UI prompt callback for the current session (Chainlit)."""
    _CALLBACKS[_session_key()] = cb


def get_ui_callback() -> UiCallback | None:
    """Return the UI prompt callback for the current session, if any."""
    return _CALLBACKS.get(_session_key())


def clear_ui_callback() -> None:
    """Remove the callback for the current session (called on chat end)."""
    _CALLBACKS.pop(_session_key(), None)


async def prompt_confirm(summary: str) -> str:
    """Ask the user to confirm an action. Returns ``"confirmed"`` or ``"cancelled"``.

    Uses the installed UI callback (Chainlit ``AskActionMessage``) when
    available; otherwise reads a yes/no from stdin (CLI path).
    """
    cb = get_ui_callback()
    if cb is not None:
        try:
            return await cb(kind="confirm", summary=summary)
        except Exception as exc:  # noqa: BLE001 — never strand the agent
            logger.warning("UI confirm callback failed: %s", exc)
            return f"error: {exc}"
    # CLI fallback.
    return await _stdin_confirm(summary)


async def prompt_question(question: str) -> str:
    """Ask the user a free-text question and return their answer.

    Uses the installed UI callback (Chainlit ``AskUserMessage``) when
    available; otherwise reads a line from stdin (CLI path).
    """
    cb = get_ui_callback()
    if cb is not None:
        try:
            return await cb(kind="question", question=question)
        except Exception as exc:  # noqa: BLE001 — never strand the agent
            logger.warning("UI question callback failed: %s", exc)
            return f"error: {exc}"
    return await _stdin_question(question)


async def _stdin_confirm(summary: str) -> str:
    """Block on a yes/no stdin prompt (CLI fallback)."""
    print(f"\n{summary}\n", file=sys.stderr)
    loop = asyncio.get_event_loop()
    while True:
        resp = await loop.run_in_executor(None, input, t("cli.confirm.prompt"))
        resp = (resp or "").strip().lower()
        if resp in ("y", "yes", "j", "ja"):
            return "confirmed"
        if resp in ("", "n", "no", "cancel", "nein"):
            return "cancelled"


async def _stdin_question(question: str) -> str:
    """Block on a free-text stdin prompt (CLI fallback)."""
    print(f"\n{question}\n> ", file=sys.stderr, end="", flush=True)
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, input, "")
    return (resp or "").strip() or t("cli.question.no_answer")


__all__ = [
    "clear_ui_callback",
    "get_ui_callback",
    "prompt_confirm",
    "prompt_question",
    "set_ui_callback",
]