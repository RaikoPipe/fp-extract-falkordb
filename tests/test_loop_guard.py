"""Tests for the RepeatGuardMiddleware loop-breaker.

No LLM / graph compilation required: the middleware's ``before_model`` hook
is exercised directly against synthetic message histories. This validates
the detection logic that prevents the GraphRecursionError seen when the
agent re-called ``file_metadata`` / ``ls`` / ``glob`` with identical args.
"""

import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness._loop_guard import (
    RepeatGuardMiddleware,
    _trailing_repeat_count,
)


def _ai_with_tool(name: str, args: dict, tid: str = "1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": tid, "type": "tool"}])


def _tool_result(tid: str = "1") -> ToolMessage:
    return ToolMessage(content='{"error": "not found"}', tool_call_id=tid)


def test_no_repeat_returns_none():
    mw = RepeatGuardMiddleware(max_repeats=3)
    state = {"messages": [HumanMessage(content="hi"), AIMessage(content="hello")]}
    assert mw.before_model(state, None) is None


def test_single_tool_call_not_flagged():
    mw = RepeatGuardMiddleware(max_repeats=3)
    state = {
        "messages": [
            HumanMessage(content="check the file"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}),
            _tool_result(),
        ]
    }
    assert mw.before_model(state, None) is None


def test_below_threshold_not_flagged():
    mw = RepeatGuardMiddleware(max_repeats=3)
    state = {
        "messages": [
            HumanMessage(content="check"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}, "1"),
            _tool_result("1"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}, "2"),
            _tool_result("2"),
        ]
    }
    assert _trailing_repeat_count(state["messages"]) == 2
    assert mw.before_model(state, None) is None


def test_at_threshold_injects_system_message():
    mw = RepeatGuardMiddleware(max_repeats=3)
    state = {
        "messages": [
            HumanMessage(content="check"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}, "1"),
            _tool_result("1"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}, "2"),
            _tool_result("2"),
            _ai_with_tool("file_metadata", {"path": "originals/x.pdf"}, "3"),
            _tool_result("3"),
        ]
    }
    assert _trailing_repeat_count(state["messages"]) == 3
    out = mw.before_model(state, None)
    assert out is not None
    msgs = out["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], SystemMessage)
    assert "STOP calling tools" in msgs[0].content


def test_different_args_dont_count_as_repeat():
    mw = RepeatGuardMiddleware(max_repeats=2)
    state = {
        "messages": [
            HumanMessage(content="check"),
            _ai_with_tool("file_metadata", {"path": "originals/a.pdf"}, "1"),
            _tool_result("1"),
            _ai_with_tool("file_metadata", {"path": "originals/b.pdf"}, "2"),
            _tool_result("2"),
        ]
    }
    assert _trailing_repeat_count(state["messages"]) == 1
    assert mw.before_model(state, None) is None


def test_different_tool_names_dont_count_as_repeat():
    state = {
        "messages": [
            HumanMessage(content="check"),
            _ai_with_tool("file_metadata", {"path": "x"}, "1"),
            _tool_result("1"),
            _ai_with_tool("read_excerpt", {"path": "x"}, "2"),
            _tool_result("2"),
        ]
    }
    assert _trailing_repeat_count(state["messages"]) == 1


def test_repeat_count_stops_at_non_tool_message():
    """A HumanMessage between tool calls breaks the repeat streak."""
    msgs = [
        HumanMessage(content="check"),
        _ai_with_tool("file_metadata", {"path": "x"}, "1"),
        _tool_result("1"),
        HumanMessage(content="again"),
        _ai_with_tool("file_metadata", {"path": "x"}, "2"),
        _tool_result("2"),
    ]
    assert _trailing_repeat_count(msgs) == 1


def test_async_hook_matches_sync():
    import asyncio

    mw = RepeatGuardMiddleware(max_repeats=2)
    state = {
        "messages": [
            HumanMessage(content="check"),
            _ai_with_tool("ls", {"path": "originals"}, "1"),
            _tool_result("1"),
            _ai_with_tool("ls", {"path": "originals"}, "2"),
            _tool_result("2"),
        ]
    }
    out = asyncio.run(mw.abefore_model(state, None))
    assert out is not None
    assert isinstance(out["messages"][0], SystemMessage)


def test_attribute_style_state():
    """State may be an object with a .messages attribute, not just a dict."""
    mw = RepeatGuardMiddleware(max_repeats=2)

    class _State:
        def __init__(self, messages):
            self.messages = messages

    state = _State(
        [
            HumanMessage(content="check"),
            _ai_with_tool("glob", {"pattern": "**/x.md"}, "1"),
            _tool_result("1"),
            _ai_with_tool("glob", {"pattern": "**/x.md"}, "2"),
            _tool_result("2"),
        ]
    )
    assert mw.before_model(state, None) is not None


def test_max_repeats_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_REPEATS", "5")
    mw = RepeatGuardMiddleware()
    assert mw.max_repeats == 5


def test_max_repeats_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TOOL_REPEATS", "notanint")
    mw = RepeatGuardMiddleware()
    assert mw.max_repeats == 3