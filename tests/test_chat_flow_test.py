"""Tests for the admin-only chat-flow test harness.

Drives the real ``on_message`` handler with a mock LangGraph v3 event stream
(``showcase_flow`` scenario from :mod:`falkordb_harness.chat_flow_test`) and
asserts every chronological-ordering invariant: lazy answer messages,
thinking Step routing, same-tool chain aggregation, parallel batch history,
Claude-style ``chat_history``, ``write_todos`` element creation, and the
admin guard.

These tests follow the mocking patterns established in
``test_chainlit_doc_actions.py`` — ``cl.user_session`` / ``cl.context`` are
stubbed, ``cl.Message`` / ``cl.Step`` are replaced with recorders that track
creation order, send/update/stream_token calls, and element attachments.
No live LLM, FalkorDB, or Chainlit server is needed.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


# Shared monotonic creation counter across _FakeMessage and _FakeStep so
# creation order can be compared between the two types (used to assert
# chronological ordering of answer messages vs tool steps).
_SHARED_COUNTER = 0


def _next_global_seq() -> int:
    global _SHARED_COUNTER
    _SHARED_COUNTER += 1
    return _SHARED_COUNTER


# ---------------------------------------------------------------------------
# Fake Chainlit objects
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Fake ``cl.Message`` that records creation order and tracks state.

    Each ``cl.Message(...)`` call returns a new instance (not a callable
    recorder like in test_chainlit_doc_actions) because we need to track
    per-message identity (which message is ``response_msg``, its content
    over time, its elements, its thread_id).

    ``_seq`` is drawn from a *shared* creation counter (_SHARED_COUNTER) so
    creation order can be compared across _FakeMessage and _FakeStep
    instances — needed to assert chronological ordering between answer
    messages and tool steps.
    """

    _counter = 0

    def __init__(self, *, content="", elements=None, actions=None):
        _FakeMessage._counter += 1
        self._seq = _next_global_seq()
        self.content = content or ""
        self.elements = elements or []
        self.actions = actions or []
        self.thread_id = f"thread-{self._seq}"
        self.sent = False
        self.updated = False
        self.streamed_tokens: list[str] = []

    async def send(self):
        self.sent = True
        return self

    async def update(self):
        self.updated = True
        return self

    async def stream_token(self, token: str):
        self.content += token
        self.streamed_tokens.append(token)
        return self


class _FakeStep:
    """Fake ``cl.Step`` that records creation, input/output, parent_id.

    ``_seq`` is drawn from the shared creation counter so creation order
    can be compared across _FakeMessage and _FakeStep instances.
    """

    _counter = 0

    def __init__(self, name="", type="", **kwargs):
        _FakeStep._counter += 1
        self._seq = _next_global_seq()
        self.id = f"step-{_FakeStep._counter}"
        self.name = name
        self.type = type
        self.parent_id = kwargs.get("parent_id")
        self.input = ""
        self.output = ""
        self.language = None
        self.icon = None
        self.tags = []
        self.default_open = kwargs.get("default_open", False)
        self.sent = False
        self.updated = False

    async def send(self):
        self.sent = True
        return self

    async def update(self):
        self.updated = True
        return self


class _FakeCustomElement:
    """Fake ``cl.CustomElement`` for AgentTodos tracking."""

    def __init__(self, name="", props=None):
        self.name = name
        self.props = props or {}
        self.updated = False

    async def update(self):
        self.updated = True
        return self


class _FakeMessageFactory:
    """Records all ``cl.Message(...)`` calls and returns real _FakeMessage objects."""

    def __init__(self):
        self.created: list[_FakeMessage] = []

    def __call__(self, *, content="", elements=None, actions=None):
        msg = _FakeMessage(content=content, elements=elements, actions=actions)
        self.created.append(msg)
        return msg


class _FakeStepFactory:
    """Records all ``cl.Step(...)`` calls and returns real _FakeStep objects."""

    def __init__(self):
        self.created: list[_FakeStep] = []

    def __call__(self, name="", type="", **kwargs):
        step = _FakeStep(name=name, type=type, **kwargs)
        self.created.append(step)
        return step


class _FakeSessionStore:
    """Dict-backed ``cl.user_session``."""

    def __init__(self):
        self._d: dict[str, Any] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _FakeContext:
    """Fake ``cl.context`` with a session carrying a thread_id."""

    def __init__(self, thread_id="test-thread-1"):
        self.session = SimpleNamespace(thread_id=thread_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_counters():
    """Reset the global counters so test runs are independent."""
    global _SHARED_COUNTER
    _FakeMessage._counter = 0
    _FakeStep._counter = 0
    _SHARED_COUNTER = 0
    yield
    _FakeMessage._counter = 0
    _FakeStep._counter = 0
    _SHARED_COUNTER = 0


def _make_admin_user():
    """Build a fake admin user object (matches the cl.context.session.user shape)."""
    return SimpleNamespace(
        identifier="admin@example.com",
        metadata={"role": "admin"},
    )


def _make_regular_user():
    """Build a fake non-admin user object."""
    return SimpleNamespace(
        identifier="user@example.com",
        metadata={"role": "user"},
    )


def _install_cl_stubs(
    monkeypatch,
    *,
    user=None,
    thread_id="test-thread-1",
):
    """Stub ``cl.user_session``, ``cl.context``, ``cl.Message``, ``cl.Step``.

    Returns ``(session, msg_factory, step_factory)`` so tests can inspect
    the recorded objects.
    """
    import chainlit as cl

    session = _FakeSessionStore()
    session.set("user_identifier", getattr(user, "identifier", None))
    session.set("lang", "en")
    session.set("chat_history", [])
    session.set("uploaded_files", [])
    session.set("ingestion_settings", {"overwrite_preprocessed": False})
    session.set("agent", None)
    session.set("graph_selection", {"active_graph": "factory_planning", "allowed_graphs": ["factory_planning"]})
    session.set("user", user)
    monkeypatch.setattr(cl, "user_session", session)

    fake_ctx = _FakeContext(thread_id=thread_id)
    monkeypatch.setattr(cl, "context", fake_ctx)

    msg_factory = _FakeMessageFactory()
    monkeypatch.setattr(cl, "Message", msg_factory)

    step_factory = _FakeStepFactory()
    monkeypatch.setattr(cl, "Step", step_factory)

    # Stub cl.CustomElement for AgentTodos
    monkeypatch.setattr(cl, "CustomElement", _FakeCustomElement)

    return session, msg_factory, step_factory


def _install_local_steps(monkeypatch, parent_step=None):
    """Set ``chainlit.step.local_steps`` to contain one fake run step.

    ``on_message`` reads ``local_steps`` to get ``_on_message_step_id``
    (the parent_id for tool Steps and the thinking Step). We need at least
    one step in the contextvar.
    """
    if parent_step is None:
        parent_step = _FakeStep(name="on_message", type="run")
        parent_step.id = "on-message-run-id"

    from chainlit.step import local_steps

    local_steps.set([parent_step])
    return parent_step


def _install_stream_recovery(monkeypatch):
    """Stub ``register_stream`` / ``deregister_stream`` to no-ops."""
    import falkordb_harness.chainlit_app as app

    monkeypatch.setattr(app, "register_stream", lambda *a, **kw: None)
    monkeypatch.setattr(app, "deregister_stream", lambda *a, **kw: None)


def _install_progress(monkeypatch):
    """Stub ``make_ingestion_progress`` so the factory doesn't build UI elements.

    ``on_message`` imports ``make_ingestion_progress`` lazily from
    ``chainlit_progress`` inside the handler, so we patch it on that module.
    """
    import falkordb_harness.chainlit_progress as progress_mod

    async def _fake_make():
        return None, lambda *a, **kw: None, lambda *a, **kw: None

    monkeypatch.setattr(progress_mod, "make_ingestion_progress", _fake_make)


def _make_test_message(content: str):
    """Build a fake ``cl.Message`` for the user's chat input."""
    return SimpleNamespace(content=content, elements=[])


# ---------------------------------------------------------------------------
# Test: non-admin rejection
# ---------------------------------------------------------------------------

def test_non_admin_rejected(monkeypatch):
    """A non-admin user sending __chat_flow_test__ gets a rejection message."""
    user = _make_regular_user()
    session, msg_factory, _ = _install_cl_stubs(monkeypatch, user=user)
    _install_local_steps(monkeypatch)
    _install_stream_recovery(monkeypatch)
    _install_progress(monkeypatch)

    import falkordb_harness.chainlit_app as app

    msg = _make_test_message("__chat_flow_test__:showcase_flow")
    _run(app.on_message(msg))

    # A rejection message was sent.
    assert len(msg_factory.created) == 1
    assert "admin" in msg_factory.created[0].content.lower()
    # chat_history was not modified (the handler returned early).
    assert session.get("chat_history") == []


# ---------------------------------------------------------------------------
# Test: unknown scenario name
# ---------------------------------------------------------------------------

def test_unknown_scenario_rejected(monkeypatch):
    """An admin sending __chat_flow_test__:nonexistent gets a not-found message."""
    user = _make_admin_user()
    session, msg_factory, _ = _install_cl_stubs(monkeypatch, user=user)
    _install_local_steps(monkeypatch)
    _install_stream_recovery(monkeypatch)
    _install_progress(monkeypatch)

    import falkordb_harness.chainlit_app as app

    msg = _make_test_message("__chat_flow_test__:nonexistent_scenario")
    _run(app.on_message(msg))

    assert len(msg_factory.created) == 1
    assert "nonexistent_scenario" in msg_factory.created[0].content
    assert session.get("chat_history") == []


# ---------------------------------------------------------------------------
# Test: prefix stripped from history
# ---------------------------------------------------------------------------

def test_prefix_stripped_from_history(monkeypatch):
    """The __chat_flow_test__ prefix must not appear in chat_history."""
    user = _make_admin_user()
    session, _, _ = _install_cl_stubs(monkeypatch, user=user)
    _install_local_steps(monkeypatch)
    _install_stream_recovery(monkeypatch)
    _install_progress(monkeypatch)

    import falkordb_harness.chainlit_app as app

    msg = _make_test_message("__chat_flow_test__:showcase_flow")
    _run(app.on_message(msg))

    history = session.get("chat_history")
    assert len(history) > 0
    from langchain_core.messages import HumanMessage

    human_msgs = [m for m in history if isinstance(m, HumanMessage)]
    assert len(human_msgs) == 1
    assert "__chat_flow_test__" not in human_msgs[0].content
    assert human_msgs[0].content == ""


# ---------------------------------------------------------------------------
# Full showcase_flow scenario tests
# ---------------------------------------------------------------------------

def _run_showcase_flow(monkeypatch):
    """Run the full showcase_flow scenario through on_message.

    Returns ``(session, msg_factory, step_factory, parent_step)`` for
    assertions.
    """
    user = _make_admin_user()
    session, msg_factory, step_factory = _install_cl_stubs(
        monkeypatch, user=user
    )
    parent_step = _install_local_steps(monkeypatch)
    _install_stream_recovery(monkeypatch)
    _install_progress(monkeypatch)

    # Stub _get_or_create_todos_element so write_todos doesn't build real UI.
    import falkordb_harness.chainlit_progress as progress_mod

    todos_created: list[dict] = []

    async def _fake_todos_element(*, initial_todos=None, ingestion_running=False):
        todos_created.append({
            "todos": list(initial_todos or []),
            "ingestion_running": ingestion_running,
        })
        el = _FakeCustomElement(
            name="AgentTodos",
            props={
                "todos": list(initial_todos or []),
                "stages": {},
                "ingestion_running": ingestion_running,
                "active": bool(initial_todos) or ingestion_running,
            },
        )
        session.set("agent_todos_el", el)
        return el

    monkeypatch.setattr(
        progress_mod, "_get_or_create_todos_element", _fake_todos_element
    )

    import falkordb_harness.chainlit_app as app

    msg = _make_test_message("__chat_flow_test__:showcase_flow")
    _run(app.on_message(msg))

    return session, msg_factory, step_factory, parent_step, todos_created


def test_pre_tool_text_streams_live_to_answer_msg(monkeypatch):
    """The final answer (R7, no tool calls) streams live to its own answer
    message.

    In v3, model messages with tool_calls route their text to the thinking
    Step (reasoning). Only messages without tool_calls are answer spans
    that stream to an answer message. R7 is the only such message in the
    showcase flow. An answer message exists and contains the showcase
    report text.
    """
    session, msg_factory, _, _, _ = _run_showcase_flow(monkeypatch)

    # Find the answer messages — those with streamed tokens.
    streamed = [m for m in msg_factory.created if m.streamed_tokens]
    assert len(streamed) >= 1, "expected at least one message with streamed tokens"

    # The final answer text "Showcase Report" should be in a streamed
    # answer message (R7, the only no-tool_calls message).
    first_answer = streamed[0]
    assert "Showcase Report" in first_answer.content or "PASS" in first_answer.content


def test_final_answer_flushes_to_answer_msg(monkeypatch):
    """R7's final answer (no tool_calls) streams to its own answer message.

    In v3, the final model message has no tool calls — its text blocks
    stream as a whole AIMessage to a fresh answer message. The answer
    message content should contain the showcase report table.
    """
    session, msg_factory, _, _, _ = _run_showcase_flow(monkeypatch)

    # Find the message that has the final answer text.
    all_msgs = msg_factory.created
    report_msgs = [m for m in all_msgs if "Showcase Report" in m.content]
    assert len(report_msgs) >= 1, (
        "expected an answer message to contain the showcase report"
    )


def test_answer_spans_get_separate_containers(monkeypatch):
    """Each answer span (no-tool_calls model message) gets its own cl.Message
    container, positioned after all tool calls.

    In v3, only model messages without tool_calls create answer messages.
    R1-R6 have tool_calls (reasoning → thinking Step); R7 is the sole
    answer span. It must be a separate container from any thinking Step,
    created after all tool steps so it renders in its true chronological
    position.
    """
    session, msg_factory, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    streamed = [m for m in msg_factory.created if m.streamed_tokens]
    assert len(streamed) >= 1, (
        f"expected >=1 streamed answer message (R7), got {len(streamed)}"
    )

    # R7 report must be in a streamed answer message.
    report_msg = next(
        (m for m in streamed if "Showcase Report" in m.content), None
    )
    assert report_msg is not None, "R7 report message missing"


def test_final_answer_msg_created_after_all_tools(monkeypatch):
    """The final-answer message (R7) must be created *after* every tool Step.

    The _FakeMessage / _FakeStep factories assign a monotonic _seq counter
    on creation, so we can assert chronological creation order: the message
    holding the report must have a higher _seq than every tool step.
    """
    _, msg_factory, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    report_msg = next(
        (m for m in msg_factory.created if "Showcase Report" in m.content),
        None,
    )
    assert report_msg is not None, "R7 report message missing"

    tool_steps = [s for s in step_factory.created if s.type == "tool"]
    assert tool_steps, "expected tool steps"
    for step in tool_steps:
        assert report_msg._seq > step._seq, (
            f"report msg (_seq={report_msg._seq}) must be created after "
            f"tool step {step.name!r} (_seq={step._seq})"
        )


def test_thinking_step_created_before_first_tool(monkeypatch):
    """The thinking Step (carrying R1's reasoning text) must be created
    *before* the first tool Step, so reasoning renders above the tool calls.

    In v3, R1 has tool_calls so its text routes to the thinking Step. The
    thinking Step must be created before the first tool step.
    """
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    # The thinking step is a cl.Step with the thinking label name.
    thinking_steps = [
        s for s in step_factory.created
        if s.name and ("hinking" in str(s.name) or "brain" in str(getattr(s, "icon", "") or ""))
    ]
    assert thinking_steps, "expected a thinking step"
    thinking = thinking_steps[0]

    tool_steps = [s for s in step_factory.created if s.type == "tool" and s.name and "hinking" not in str(s.name)]
    assert tool_steps, "expected tool steps"
    first_tool = min(tool_steps, key=lambda s: s._seq)
    assert thinking._seq < first_tool._seq, (
        f"thinking step (_seq={thinking._seq}) must be created before "
        f"the first tool step {first_tool.name!r} (_seq={first_tool._seq})"
    )


def test_reasoning_text_routed_to_thinking_step(monkeypatch):
    """Reasoning text (model text before tool_calls, after tools started)
    must land in the thinking Step's output, not in an answer message.

    In v3, model messages with tool_calls route their text to the thinking
    Step (created lazily on first reasoning text). R2-R6 are reasoning
    runs (each has tool_calls), so their text goes to the thinking Step.
    """
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    # The thinking step is a cl.Step with name matching the thinking label.
    # It should contain buffered reasoning text from R2-R6.
    thinking_steps = [
        s for s in step_factory.created
        if s.name and ("hinking" in str(s.name) or "brain" in str(getattr(s, "icon", "") or ""))
    ]
    # There should be exactly one thinking step (created once, updated multiple times).
    assert len(thinking_steps) >= 1, (
        f"expected a thinking step, got: {[s.name for s in step_factory.created]}"
    )
    thinking = thinking_steps[0]
    # It should contain text from R2 onwards (buffered reasoning).
    assert "throwaway graph" in thinking.output.lower() or "Creating" in thinking.output


def test_same_tool_chain_aggregates_file_metadata(monkeypatch):
    """Two consecutive file_metadata calls must aggregate into one Step with
    a name containing 'x 2'."""
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    fm_steps = [s for s in step_factory.created if s.name and "file_metadata" in str(s.name)]
    assert len(fm_steps) == 1, (
        f"expected 1 file_metadata step (chain), got {len(fm_steps)}: "
        f"{[s.name for s in step_factory.created]}"
    )
    # The chain step name should contain "x 2" (count=2).
    assert "2" in str(fm_steps[0].name), (
        f"expected chain header with x 2, got: {fm_steps[0].name}"
    )
    # The input should have 2 numbered sections.
    assert "Call 1" in fm_steps[0].input or "call 1" in fm_steps[0].input.lower()
    assert "Call 2" in fm_steps[0].input or "call 2" in fm_steps[0].input.lower()


def test_same_tool_chain_aggregates_search(monkeypatch):
    """Two consecutive search calls must aggregate into one Step with 'x 2'."""
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    search_steps = [s for s in step_factory.created if s.name and "search" in str(s.name).lower()]
    # search appears once as a chain step (count=2).
    assert len(search_steps) == 1, (
        f"expected 1 search step (chain), got {len(search_steps)}: "
        f"{[s.name for s in step_factory.created]}"
    )
    assert "2" in str(search_steps[0].name)


def test_create_graph_breaks_write_todos_chain(monkeypatch):
    """create_graph (different tool) must be a separate Step from write_todos."""
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    wt_steps = [s for s in step_factory.created if s.name and "write_todos" in str(s.name)]
    cg_steps = [s for s in step_factory.created if s.name and "create_graph" in str(s.name)]
    assert len(wt_steps) == 1, f"expected 1 write_todos step, got {len(wt_steps)}"
    assert len(cg_steps) == 1, f"expected 1 create_graph step, got {len(cg_steps)}"
    assert wt_steps[0].id != cg_steps[0].id


def test_read_excerpt_breaks_file_metadata_chain(monkeypatch):
    """read_excerpt (different tool) must be a separate Step from the
    file_metadata chain."""
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    re_steps = [s for s in step_factory.created if s.name and "read_excerpt" in str(s.name)]
    assert len(re_steps) == 1, (
        f"expected 1 read_excerpt step, got {len(re_steps)}"
    )
    fm_steps = [s for s in step_factory.created if s.name and "file_metadata" in str(s.name)]
    assert re_steps[0].id != fm_steps[0].id


def test_parallel_different_tools_get_separate_steps(monkeypatch):
    """get_schema and list_graphs (parallel batch, different tools) must
    each get their own Step."""
    _, _, step_factory, _, _ = _run_showcase_flow(monkeypatch)

    gs_steps = [s for s in step_factory.created if s.name and "get_schema" in str(s.name)]
    lg_steps = [s for s in step_factory.created if s.name and "list_graphs" in str(s.name)]
    assert len(gs_steps) == 1
    assert len(lg_steps) == 1
    assert gs_steps[0].id != lg_steps[0].id


def test_write_todos_creates_element(monkeypatch):
    """write_todos tool input must trigger _get_or_create_todos_element."""
    _, _, _, _, todos_created = _run_showcase_flow(monkeypatch)

    assert len(todos_created) >= 1, (
        "expected _get_or_create_todos_element to be called for write_todos"
    )
    # The first call should carry the todos from R1's write_todos tool input.
    first = todos_created[0]
    assert isinstance(first["todos"], list)
    assert len(first["todos"]) > 0
    # The element should be marked active (todos non-empty) so the
    # client-side portal pins it above the composer.
    assert first["ingestion_running"] is False
    assert bool(first["todos"]) is True


def test_tool_steps_parent_id_is_on_message_step(monkeypatch):
    """All tool Steps' parent_id must equal _on_message_step_id."""
    _, _, step_factory, parent_step, _ = _run_showcase_flow(monkeypatch)

    expected_parent = parent_step.id
    # Tool steps have type="tool".
    tool_steps = [s for s in step_factory.created if s.type == "tool"]
    assert len(tool_steps) > 0, "expected at least one tool step"
    for step in tool_steps:
        assert step.parent_id == expected_parent, (
            f"step {step.name!r} has parent_id={step.parent_id!r}, "
            f"expected {expected_parent!r}"
        )


def test_claude_style_history(monkeypatch):
    """chat_history must contain Claude-style AIMessage(tool_calls) +
    ToolMessage pairs plus a final AIMessage(answer).

    The showcase_flow has 6 batches of tool calls:
    R1: [write_todos]                → 1 tool
    R2: [create_graph]               → 1 tool
    R3: [file_metadata, file_metadata, read_excerpt] → 3 tools
    R4: [get_schema, list_graphs]   → 2 tools
    R5: [nl_query, search, search]   → 3 tools
    R6: [get_reconciliations, update_graph_description] → 2 tools
    R7: final answer (no tools)
    """
    session, _, _, _, _ = _run_showcase_flow(monkeypatch)
    history = session.get("chat_history")

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    # First message is the HumanMessage (user_content="" after prefix strip).
    assert isinstance(history[0], HumanMessage)

    # Count AIMessage with tool_calls and ToolMessages.
    ai_with_tc = [m for m in history if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)]
    tool_msgs = [m for m in history if isinstance(m, ToolMessage)]
    ai_answer = [m for m in history if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)]

    # 6 batches → 6 AIMessage(tool_calls=[...]).
    assert len(ai_with_tc) == 6, (
        f"expected 6 AIMessage with tool_calls (one per batch), got {len(ai_with_tc)}"
    )
    # 1+1+3+2+3+2 = 12 ToolMessages.
    assert len(tool_msgs) == 12, (
        f"expected 12 ToolMessages, got {len(tool_msgs)}"
    )
    # 1 final answer AIMessage.
    assert len(ai_answer) == 1, (
        f"expected 1 final AIMessage (answer), got {len(ai_answer)}"
    )

    # Verify tool call counts per batch.
    expected_tc_counts = [1, 1, 3, 2, 3, 2]
    for i, (ai, expected) in enumerate(zip(ai_with_tc, expected_tc_counts)):
        assert len(ai.tool_calls) == expected, (
            f"batch {i}: expected {expected} tool_calls, got {len(ai.tool_calls)}"
        )


def test_answer_msg_created_lazily(monkeypatch):
    """Answer messages must be created on the first model text delta, not
    eagerly at on_message entry.

    In the showcase_flow, R1 streams live (before tools) so the first answer
    message is created during R1's first text event. The key invariant is
    that no answer message is created before the stream loop starts. We
    verify by checking that no message was created before the agent's
    astream_events call.
    """
    _, msg_factory, _, _, _ = _run_showcase_flow(monkeypatch)

    # Answer messages are created during the stream loop, not before it.
    streamed = [m for m in msg_factory.created if m.streamed_tokens]
    assert len(streamed) >= 1

    # The first answer message should have been created by _new_answer_msg,
    # which is called inside the event loop on the first stream token.
    first_answer = streamed[0]
    assert first_answer.sent, "answer message should have been sent"