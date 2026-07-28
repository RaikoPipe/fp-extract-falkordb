"""Unit tests for the interactive UI prompt tools and ui_prompts module.

Covers the callback registry + CLI stdin fallback (the Chainlit
``AskActionMessage`` / ``AskUserMessage`` path requires a live Chainlit
server and is exercised manually). The tools delegate to
``ui_prompts.prompt_confirm`` / ``prompt_question``, so we test both the
fallback (no callback installed -> stdin) and the callback path directly.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness import ui_prompts
from falkordb_harness.tools.ui_prompt_tools import (
    ask_user,
    request_ingestion_confirmation,
)


@pytest.fixture(autouse=True)
def _clear_callbacks():
    """Ensure no leftover callbacks between tests (registry is module-level)."""
    ui_prompts._CALLBACKS.clear()
    yield
    ui_prompts._CALLBACKS.clear()


def test_get_ui_callback_none_when_uninstalled():
    assert ui_prompts.get_ui_callback() is None


@pytest.mark.asyncio
async def test_prompt_confirm_uses_callback_when_installed():
    async def cb(**kwargs):
        assert kwargs["kind"] == "confirm"
        assert kwargs["summary"] == "s"
        return "confirmed"

    ui_prompts.set_ui_callback(cb)
    assert await ui_prompts.prompt_confirm("s") == "confirmed"


@pytest.mark.asyncio
async def test_prompt_question_uses_callback_when_installed():
    async def cb(**kwargs):
        assert kwargs["kind"] == "question"
        assert kwargs["question"] == "q?"
        return "answer"

    ui_prompts.set_ui_callback(cb)
    assert await ui_prompts.prompt_question("q?") == "answer"


@pytest.mark.asyncio
async def test_prompt_confirm_returns_cancelled_on_callback_error(monkeypatch):
    async def cb(**kwargs):
        raise RuntimeError("boom")

    ui_prompts.set_ui_callback(cb)
    # Should fall through to the error message, not raise.
    result = await ui_prompts.prompt_confirm("s")
    assert result.startswith("error")


@pytest.mark.asyncio
async def test_prompt_confirm_falls_back_to_stdin_yes(monkeypatch):
    """CLI path: stdin 'y' -> confirmed."""
    monkeypatch.setattr("sys.stdin", _FakeStdin("y\n"))
    result = await ui_prompts.prompt_confirm("proceed?")
    assert result == "confirmed"


@pytest.mark.asyncio
async def test_prompt_confirm_falls_back_to_stdin_no(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("\n"))
    result = await ui_prompts.prompt_confirm("proceed?")
    assert result == "cancelled"


@pytest.mark.asyncio
async def test_prompt_question_falls_back_to_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("my answer\n"))
    result = await ui_prompts.prompt_question("what?")
    assert result == "my answer"


@pytest.mark.asyncio
async def test_prompt_question_empty_stdin_returns_no_answer(monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("\n"))
    result = await ui_prompts.prompt_question("what?")
    # Default UI language is German, so the "no answer" sentinel is localized.
    assert result == "(keine Antwort)"


@pytest.mark.asyncio
async def test_prompt_question_empty_stdin_returns_no_answer_en(monkeypatch):
    """When the UI language is English, the sentinel reads '(no answer)'."""
    from falkordb_harness.i18n import set_lang

    set_lang("en")
    try:
        monkeypatch.setattr("sys.stdin", _FakeStdin("\n"))
        result = await ui_prompts.prompt_question("what?")
        assert result == "(no answer)"
    finally:
        set_lang("de")


@pytest.mark.asyncio
async def test_request_ingestion_confirmation_tool_delegates(monkeypatch):
    """The tool routes through prompt_confirm -> callback."""
    captured: list[str] = []

    async def cb(**kwargs):
        captured.append(kwargs["summary"])
        return "confirmed"

    ui_prompts.set_ui_callback(cb)
    result = await request_ingestion_confirmation.ainvoke(
        {"files_summary": "3 files ready"}
    )
    assert result == "confirmed"
    assert captured == ["3 files ready"]


@pytest.mark.asyncio
async def test_ask_user_tool_delegates(monkeypatch):
    async def cb(**kwargs):
        assert kwargs["question"] == "which file?"
        return "the pdf"

    ui_prompts.set_ui_callback(cb)
    result = await ask_user.ainvoke({"question": "which file?"})
    assert result == "the pdf"


class _FakeStdin:
    """Minimal stdin replacement for CLI-fallback tests."""

    def __init__(self, text: str):
        self._buf = text

    def readline(self) -> str:
        # input() reads a line.
        line, _, rest = self._buf.partition("\n")
        self._buf = rest
        return line + "\n"