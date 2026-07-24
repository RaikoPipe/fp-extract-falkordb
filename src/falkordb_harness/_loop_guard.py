"""Repeat-guard middleware that breaks the agent out of tool-call loops.

LangGraph's default ``recursion_limit`` (25) aborts the run with a
``GraphRecursionError`` when the agent re-invokes the same tool with the
same arguments over and over — exactly what happened when the file-inspection
tools kept re-calling ``file_metadata`` / ``ls`` / ``glob`` on a path they
couldn't resolve. This middleware detects that pattern *before* the model is
called and injects a :class:`SystemMessage` instructing the model to stop
calling tools and report the situation to the user, so the run terminates
gracefully instead of burning the entire recursion budget.

Wired into ``build_agent`` via ``create_deep_agent(..., middleware=[...])``.
The repeat threshold is configurable via the ``AGENT_MAX_TOOL_REPEATS`` env
var (default 3).
"""

from __future__ import annotations

import json
import os
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

_DEFAULT_MAX_REPEATS = 3


def _max_repeats() -> int:
    raw = os.getenv("AGENT_MAX_TOOL_REPEATS", "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_MAX_REPEATS
    except ValueError:
        return _DEFAULT_MAX_REPEATS


def _tool_call_key(name: str, args: Any) -> str:
    """Stable key for a tool call: name + JSON-serialised args (sorted)."""
    try:
        return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name}:{args!r}"


def _trailing_repeat_count(messages: list[Any]) -> int:
    """Count how many times the *same* tool call repeats at the tail.

    Walks the message list backwards over (AIMessage(tool_calls) ->
    ToolMessage) pairs, tallying consecutive pairs whose tool call key is
    identical. Stops at the first different tool call or non-tool message.
    Returns 0 if the tail isn't a completed tool call.
    """
    if len(messages) < 2:
        return 0
    last_ai = messages[-2]
    last_tool = messages[-1]
    if not (isinstance(last_ai, AIMessage) and isinstance(last_tool, ToolMessage)):
        return 0
    calls = getattr(last_ai, "tool_calls", None) or []
    if not calls:
        return 0
    # Only consider single-tool AIMessages for the repeat pattern; multi-tool
    # messages are ambiguous so we fall back to the first call's key.
    key = _tool_call_key(calls[0]["name"], calls[0].get("args", {}))
    count = 1
    # Walk backwards over preceding (AIMessage, ToolMessage) pairs. The tail
    # pair is (messages[-2], messages[-1]); the previous pair is
    # (messages[-4], messages[-3]), so step by 2 starting at len-4.
    i = len(messages) - 4
    while i >= 0:
        ai = messages[i]
        tool = messages[i + 1]
        if not (isinstance(ai, AIMessage) and isinstance(tool, ToolMessage)):
            break
        c = getattr(ai, "tool_calls", None) or []
        if not c:
            break
        if _tool_call_key(c[0]["name"], c[0].get("args", {})) != key:
            break
        count += 1
        i -= 2
    return count


class RepeatGuardMiddleware(AgentMiddleware):
    """Inject a "stop repeating tools" system message when a loop is detected.

    Implements the ``before_model`` hook (sync + async) of the
    ``AgentMiddleware`` protocol. When the trailing message history shows the
    same tool call repeated ``max_repeats`` times consecutively, it appends a
    :class:`SystemMessage` directing the model to answer the user instead of
    calling tools again. The messages reducer appends it, so the model sees
    the nudge on its next (and ideally final) invocation.
    """

    def __init__(self, max_repeats: int | None = None) -> None:
        self.max_repeats = max_repeats if max_repeats is not None else _max_repeats()

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._check(state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._check(state)

    def _check(self, state: Any) -> dict[str, Any] | None:
        messages: list[Any] = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if _trailing_repeat_count(messages) >= self.max_repeats:
            return {
                "messages": [
                    SystemMessage(
                        content=(
                            "You are repeating the same tool call with identical "
                            "arguments without making progress. STOP calling tools. "
                            "Using the information you already have, write your final "
                            "answer to the user now: summarize what you found, explain "
                            "why you cannot proceed further, and ask the user how to "
                            "continue. Do not call any more tools in this turn."
                        )
                    )
                ]
            }
        return None