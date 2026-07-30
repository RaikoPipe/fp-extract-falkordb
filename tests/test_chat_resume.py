"""Tests for chat-thread resume logic.

Covers ``_history_from_thread`` — the pure helper that reconstructs
the agent's in-memory ``chat_history`` from a persisted Chainlit
``ThreadDict`` when a user reopens a past chat. Tool-call steps are
skipped (not LLM context); user/assistant messages map to
``HumanMessage`` / ``AIMessage``; long threads are capped at
``MAX_HISTORY_PAIRS`` pairs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _step(step_type: str, text: str, *, input_text: str | None = None) -> dict:
    """Build a minimal StepDict mirroring what Chainlit's SQL layer returns.

    Chainlit's Message.to_dict() stores the text of both user and assistant
    messages in the "output" field; "input" is only set for Step objects
    whose show_input is enabled, which messages never are. The SQL layer
    (sql_alchemy.get_all_user_threads) further gates "input" on showInput
    not in [None, "false"], so persisted user_message rows always return
    input="" and output=<text>. ``input_text`` is accepted only to model a
    legacy step persisted the old way.
    """
    step = {"type": step_type, "output": text}
    if input_text is not None:
        step["input"] = input_text
    return step


# ---------------------------------------------------------------------------
# _history_from_thread
# ---------------------------------------------------------------------------
def test_history_from_thread_reconstructs_conversation():
    from langchain_core.messages import AIMessage, HumanMessage

    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            _step("user_message", "hello"),
            _step("assistant_message", "hi there"),
            _step("user_message", "second q"),
            _step("assistant_message", "second a"),
        ]
    }
    h = _history_from_thread(thread)
    assert [type(m).__name__ for m in h] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
    ]
    assert [m.content for m in h] == ["hello", "hi there", "second q", "second a"]
    assert all(isinstance(m, (HumanMessage, AIMessage)) for m in h)


def test_history_from_thread_skips_tool_steps():
    """Only user_message/assistant_message are LLM context; tools are noise."""
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            _step("user_message", "q"),
            _step("tool", "tool output"),
            _step("run", "run output"),
            _step("assistant_message", "a"),
        ]
    }
    h = _history_from_thread(thread)
    assert len(h) == 2
    assert h[0].content == "q"
    assert h[1].content == "a"


def test_history_from_thread_skips_empty_messages():
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            _step("user_message", ""),
            _step("assistant_message", ""),
            _step("user_message", "real"),
            _step("assistant_message", "reply"),
        ]
    }
    h = _history_from_thread(thread)
    assert len(h) == 2
    assert h[0].content == "real"


def test_history_from_thread_empty_thread():
    from falkordb_harness.chainlit_app import _history_from_thread

    assert _history_from_thread({}) == []
    assert _history_from_thread({"steps": []}) == []


def test_history_from_thread_missing_keys():
    """Malformed steps (no type / no input+output) are skipped gracefully."""
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            {"type": "user_message"},
            {"type": "assistant_message"},
            {"no_type": "x"},
            {"type": "user_message", "output": "ok"},
        ]
    }
    h = _history_from_thread(thread)
    assert len(h) == 1
    assert h[0].content == "ok"


def test_history_from_thread_caps_at_max_pairs():
    """Long threads are truncated to MAX_HISTORY_PAIRS (most recent kept)."""
    from falkordb_harness.chainlit_app import MAX_HISTORY_PAIRS, _history_from_thread

    n = MAX_HISTORY_PAIRS * 2 + 4
    steps = []
    for i in range(n // 2):
        steps.append(_step("user_message", f"u{i}"))
        steps.append(_step("assistant_message", f"a{i}"))
    h = _history_from_thread({"steps": steps})
    assert len(h) == MAX_HISTORY_PAIRS * 2
    # Most recent kept: the last MAX_HISTORY_PAIRS pairs.
    last_user_idx = n // 2 - 1
    assert h[-2].content == f"u{last_user_idx}"
    assert h[-1].content == f"a{last_user_idx}"


def test_history_from_thread_reads_output_for_user_message():
    """user_message steps carry text in 'output' (as Chainlit persists them).

    Chainlit's Message.to_dict() stores message text in 'output' for both
    user and assistant messages; the SQL layer gates 'input' on showInput,
    so a persisted user_message always has input="" and output=<text>.
    Reading 'input' (the old behaviour) dropped every user turn on resume.
    """
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            {"type": "user_message", "input": "", "output": "the question"},
            {"type": "assistant_message", "input": "", "output": "the answer"},
        ]
    }
    h = _history_from_thread(thread)
    assert h[0].content == "the question"
    assert h[1].content == "the answer"


def test_history_from_thread_falls_back_to_input_for_legacy_user_steps():
    """Older persisted user steps may carry text in 'input' — still recovered."""
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            {"type": "user_message", "input": "legacy question", "output": ""},
            {"type": "assistant_message", "output": "the answer"},
        ]
    }
    h = _history_from_thread(thread)
    assert h[0].content == "legacy question"
    assert h[1].content == "the answer"


def test_history_from_thread_user_message_prefers_output_over_input():
    """When both fields are present, 'output' wins (matches Chainlit storage)."""
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            {"type": "user_message", "input": "ignored", "output": "the question"},
            {"type": "assistant_message", "output": "the answer", "input": "ignored"},
        ]
    }
    h = _history_from_thread(thread)
    assert h[0].content == "the question"
    assert h[1].content == "the answer"


def test_history_from_thread_realistic_persisted_shape():
    """Reproduce the exact shape returned by sql_alchemy.get_all_user_threads.

    Mirrors a real row from data/chainlit.db: user_message with input="",
    showInput=None, output=<text>; assistant_message with output=<text>.
    Before the fix, every user turn was dropped (input="" -> skipped).
    """
    from falkordb_harness.chainlit_app import _history_from_thread

    thread = {
        "steps": [
            {
                "id": "s1",
                "type": "user_message",
                "name": "rrai",
                "input": "",
                "output": "Welche Dokumenttypen kann ich hochladen?",
                "showInput": None,
            },
            {
                "id": "s2",
                "type": "assistant_message",
                "name": "FalkorDB KG Agent",
                "input": "",
                "output": "Die Pipeline unterstützt folgende Formate …",
                "showInput": None,
            },
        ]
    }
    h = _history_from_thread(thread)
    assert [type(m).__name__ for m in h] == ["HumanMessage", "AIMessage"]
    assert h[0].content == "Welche Dokumenttypen kann ich hochladen?"
    assert h[1].content == "Die Pipeline unterstützt folgende Formate …"


def test_on_chat_resume_is_registered():
    """The @cl.on_chat_resume handler must register so threads are resumable.

    Without it Chainlit reports ``threadResumable: false`` and clicking a
    past thread falls through to on_chat_start, wiping chat_history.
    """
    from chainlit.config import config

    import falkordb_harness.chainlit_app  # noqa: F401  (side-effect import)

    assert config.code.on_chat_resume is not None


def test_on_chat_start_still_registered():
    """on_chat_start must remain registered (new chats still work)."""
    from chainlit.config import config

    import falkordb_harness.chainlit_app  # noqa: F401

    assert config.code.on_chat_start is not None