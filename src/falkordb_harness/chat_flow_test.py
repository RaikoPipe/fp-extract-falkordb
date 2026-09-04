"""Mock chat-flow test scenario for verifying chronological ordering in ``on_message``.

Provides a synthetic LangGraph v3 event stream that mirrors the likely tool
chain of the showcase prompt (see :mod:`falkordb_harness.showcase`). The
stream is consumed by the real ``on_message`` handler in
:mod:`falkordb_harness.chainlit_app` — with the agent replaced by a mock — so
every chronological-ordering invariant (lazy answer messages, thinking Step
routing, same-tool chain aggregation, parallel batch history, Claude-style
``chat_history``) is exercised against the real code paths without a live
LLM, FalkorDB, or tool execution.

Usage in the UI: an admin sends ``__chat_flow_test__:showcase_flow`` in the
chat. ``on_message`` detects the prefix, checks the user's role, swaps the
real agent for :class:`MockAgent`, and lets the event stream flow through
the unchanged handler logic.

Unit tests (:mod:`tests.test_chat_flow_test`) drive the same path directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, ToolMessage

# ---------------------------------------------------------------------------
# Scenario metadata
# ---------------------------------------------------------------------------

SHOWCASE_FLOW_NAME = "showcase_flow"

SHOWCASE_FLOW_DESCRIPTION = (
    "Mock event stream mirroring the showcase tool chain: write_todos → "
    "create_graph → file_metadata × 2 + read_excerpt → get_schema + "
    "list_graphs → nl_query + search × 2 → get_reconciliations + "
    "update_graph_description → final answer report. Exercises every "
    "chronological-ordering invariant in on_message."
)


@dataclass(frozen=True)
class Scenario:
    """A named mock event scenario with a description and event generator."""

    name: str
    description: str
    events: Any  # callable returning a timeline list of (stream, event)


SCENARIOS: dict[str, Scenario] = {}


def _register(scenario: Scenario) -> None:
    SCENARIOS[scenario.name] = scenario


# ---------------------------------------------------------------------------
# V3 protocol event helpers
# ---------------------------------------------------------------------------

_NS: list[str] = []
_MODEL_NODE = "model"
_TOOLS_NODE = "tools"

# Counter for unique task ids.
_TASK_COUNTER = 0


def _next_task_id(prefix: str = "task") -> str:
    global _TASK_COUNTER
    _TASK_COUNTER += 1
    return f"{prefix}-{_TASK_COUNTER}"


def _raw_event(method: str, data: Any) -> dict:
    """Build a v3 raw protocol event.

    Raw events have shape ``{"type": "event", "method": <stream_mode>,
    "params": {"namespace": [...], "timestamp": ..., "data": ...}}``.
    """
    return {
        "type": "event",
        "method": method,
        "params": {
            "namespace": list(_NS),
            "timestamp": 0,
            "data": data,
        },
    }


def _msg_event(ai_message: AIMessage, node: str = _MODEL_NODE) -> dict:
    """Build a raw ``messages`` event carrying a whole AIMessage.

    In v3, when a model node returns a finalized message (non-streaming
    or checkpoint replay), ``MessagesTransformer`` emits it as a
    ``(message, metadata)`` tuple on the ``messages`` stream mode.
    """
    return _raw_event("messages", (
        ai_message,
        {
            "langgraph_node": node,
            "langgraph_step": 0,
            "run_id": ai_message.id or "",
        },
    ))


def _values_event(messages: list) -> dict:
    """Build a raw ``values`` event (state snapshot)."""
    return _raw_event("values", {"messages": messages})


def _task_start(task_id: str, name: str, input_data: dict) -> dict:
    """Build a task-start payload for the ``run.tasks`` projection."""
    return {
        "id": task_id,
        "name": name,
        "input": input_data,
        "triggers": [],
        "metadata": {},
    }


def _task_result(task_id: str, name: str, result_data: dict) -> dict:
    """Build a task-result payload for the ``run.tasks`` projection."""
    return {
        "id": task_id,
        "name": name,
        "error": None,
        "interrupts": [],
        "result": result_data,
    }


# ---------------------------------------------------------------------------
# Showcase-flow event generator
# ---------------------------------------------------------------------------

_SHOWCASE_TODOS = [
    {"content": "Create throwaway graph", "status": "in_progress"},
    {"content": "Discover and inspect files", "status": "pending"},
    {"content": "Preprocess if needed", "status": "pending"},
    {"content": "Chunk preview", "status": "pending"},
    {"content": "Ingest", "status": "pending"},
    {"content": "Inspect graph schema", "status": "pending"},
    {"content": "Query the graph", "status": "pending"},
    {"content": "Search", "status": "pending"},
    {"content": "Reconciliation", "status": "pending"},
    {"content": "Update description", "status": "pending"},
    {"content": "Report", "status": "pending"},
]

# Mid-flow todo update: after create_graph completes, mark it done and
# advance the next task to in_progress. Exercises the bug where the
# AgentTodos panel must be re-updated on a subsequent write_todos call
# (the v3 tools task-start branch alone is unreliable for this — see
# chainlit_app.py _consume_tasks model-task-result branch).
_SHOWCASE_TODOS_UPDATED = [
    {"content": "Create throwaway graph", "status": "completed"},
    {"content": "Discover and inspect files", "status": "in_progress"},
    {"content": "Preprocess if needed", "status": "pending"},
    {"content": "Chunk preview", "status": "pending"},
    {"content": "Ingest", "status": "pending"},
    {"content": "Inspect graph schema", "status": "pending"},
    {"content": "Query the graph", "status": "pending"},
    {"content": "Search", "status": "pending"},
    {"content": "Reconciliation", "status": "pending"},
    {"content": "Update description", "status": "pending"},
    {"content": "Report", "status": "pending"},
]

_PRE_TOOL_TEXT = "I'll start by planning the showcase steps."

_R2_TEXT = "Creating a throwaway graph for the showcase."
_R3_TEXT = "Let me inspect the fixture file's metadata and read an excerpt."
_R4_TEXT = "Now let me check the graph schema and list available graphs."
_R5_TEXT = "Running a natural-language query and two searches."
_R6_TEXT = "Checking for reconciliation links and updating the graph description."

_FINAL_ANSWER = (
    "## Showcase Report\n\n"
    "| Step | Status | Notes |\n"
    "|---|---|---|\n"
    "| 0 | PASS | Graph `_debug_smoke_...` created |\n"
    "| 1 | PASS | 2 files discovered |\n"
    "| 2 | PASS | Metadata + excerpt read |\n"
    "| 4 | PASS | 42 chunks previewed |\n"
    "| 5 | PASS | 38 extractions, 19 nodes, 0 conflicts |\n"
    "| 6 | PASS | Schema: Resource, Machine, Transport |\n"
    "| 7 | PASS | 19 nodes, 3 entity types |\n"
    "| 8 | PASS | Top results: CNC machine (0.95), Hall A (0.88) |\n"
    "| 9 | PASS | No duplicates found |\n"
    "| 10 | PASS | Description updated |\n"
)


def _json_output(**kwargs) -> str:
    import json

    return json.dumps(kwargs, ensure_ascii=False)


def _ai(text: str, tool_calls: list[dict] | None = None) -> AIMessage:
    """Build an AIMessage with optional tool calls."""
    return AIMessage(
        content=text,
        tool_calls=tool_calls or [],
    )


def _tool_msg(name: str, tool_call_id: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, name=name, tool_call_id=tool_call_id)


async def _showcase_flow() -> list[tuple[str, dict]]:
    """Yield the showcase-flow mock v3 event stream.

    Returns a single interleaved ``timeline`` list of ``(stream, event)``
    tuples where ``stream`` is ``"raw"`` or ``"tasks"``. The
    :class:`MockRunStream` distributes events to the raw and tasks
    projections via a shared cursor so the two concurrent consumers in
    ``on_message`` (driven by ``asyncio.gather``) observe events in
    faithful protocol order.

    Seven model runs, twelve tool calls across six parallel batches.

    The task stream mirrors the real LangGraph v3 Send-per-call shape:

    - A ``model`` task result (carrying the AIMessage with ``tool_calls``)
      opens each batch — ``_consume_tasks`` keys tool calls to the batch
      by ``tool_call_id``.
    - Each tool call is its own ``tools`` task with ``input`` set to a
      one-element list ``[{"name", "args", "id", "type": "tool_call"}]``
      (matching ``create_agent``'s ``Send("tools", [tool_call])``
      dispatch).
    - Each ``tools`` task result carries a single ``ToolMessage``
      (one per call), not the whole batch's results at once.
    """

    raw_events: list[dict] = []
    task_events: list[dict] = []
    all_messages: list = []

    def _add_msg(ai: AIMessage, node: str = _MODEL_NODE) -> None:
        raw_events.append(_msg_event(ai, node))
        all_messages.append(ai)

    def _add_values() -> None:
        raw_events.append(_values_event(list(all_messages)))

    def _add_model_task_result(ai: AIMessage) -> None:
        """Emit a ``model`` task result carrying the AIMessage.

        ``on_message``'s ``_consume_tasks`` opens a tool-call batch from
        each ``model`` task result whose AIMessage has ``tool_calls`` —
        mirroring the real LangGraph v3 protocol where the model task
        completes before its tool calls are dispatched.
        """
        tid = _next_task_id("model")
        task_events.append(_task_start(tid, "model", {
            "messages": list(all_messages),
        }))
        task_events.append(_task_result(tid, "model", {
            "messages": [ai],
        }))

    def _add_tool_batch(
        ai_with_calls: AIMessage,
        tool_results: list[tuple[str, str, str]],
    ) -> None:
        """Add one ``tools`` task per tool call (Send-per-call shape).

        Mirrors the real LangGraph v3 protocol: ``create_agent`` dispatches
        each tool call as its own ``Send("tools", [tool_call])`` task, so
        each ``tools`` task's ``input`` is a list with a single tool-call
        dict and each task result carries a single ``ToolMessage``. The
        ``model`` task result (emitted by :func:`_add_model_task_result`)
        opens the batch; the per-call ``tools`` tasks fill it in.

        ``tool_results`` is a list of ``(tool_name, tool_call_id, output)``
        tuples — one per tool call in the batch, ordered to match
        ``ai_with_calls.tool_calls``.
        """
        all_messages.append(ai_with_calls)
        # Emit the model task result so _consume_tasks opens the batch.
        _add_model_task_result(ai_with_calls)
        # One tools task per call: start (input=[tool_call_dict]) + result
        # (single ToolMessage). Pregel dispatches all starts before any
        # results, so we emit all starts first, then all results.
        starts: list[tuple[str, dict, ToolMessage]] = []
        for tc, (tname, tc_id, output) in zip(ai_with_calls.tool_calls, tool_results):
            tool_call_dict = {
                "name": tc["name"],
                "args": tc.get("args", {}),
                "id": tc["id"],
                "type": "tool_call",
            }
            tid = _next_task_id("tools")
            task_events.append(_task_start(tid, "tools", [tool_call_dict]))
            tm = _tool_msg(tname, tc_id, output)
            starts.append((tid, tool_call_dict, tm))
        for tid, _tcd, tm in starts:
            all_messages.append(tm)
            task_events.append(_task_result(tid, "tools", {
                "messages": [tm],
            }))

    # --- R1: pre-tool text → write_todos --------------------------
    r1 = _ai(_PRE_TOOL_TEXT, tool_calls=[
        {"name": "write_todos", "args": {"todos": _SHOWCASE_TODOS}, "id": "call-wt", "type": "tool_call"},
    ])
    _add_msg(r1)
    _add_values()

    # T1: write_todos
    _add_tool_batch(r1, [("write_todos", "call-wt", "Todos written.")])

    # --- R2: create_graph -----------------------------------------
    r2 = _ai(_R2_TEXT, tool_calls=[
        {"name": "create_graph", "args": {
            "name": "_debug_smoke_2026-08-13T12:00:00Z",
            "description": "Throwaway graph for end-to-end showcase run.",
        }, "id": "call-cg", "type": "tool_call"},
    ])
    _add_msg(r2)
    _add_values()

    # T2: create_graph
    _add_tool_batch(r2, [(
        "create_graph", "call-cg",
        _json_output(
            active_graph="_debug_smoke_2026-08-13T12:00:00Z",
            allowed_graphs=["_debug_smoke_2026-08-13T12:00:00Z"],
            created=True,
            description="Throwaway graph for end-to-end showcase run.",
        ),
    )])

    # --- R2b: write_todos (status update) -------------------------
    # The agent re-emits write_todos with updated statuses after
    # create_graph completed. This exercises the model-task-result
    # branch in _consume_tasks that forwards write_todos args to the
    # AgentTodos panel — without it, the panel would stay frozen on
    # the initial all-pending plan (the bug this test now guards
    # against).
    r2b = _ai("Graph created. Updating the plan.", tool_calls=[
        {"name": "write_todos", "args": {"todos": _SHOWCASE_TODOS_UPDATED}, "id": "call-wt2", "type": "tool_call"},
    ])
    _add_msg(r2b)
    _add_values()

    # T2b: write_todos (second call)
    _add_tool_batch(r2b, [("write_todos", "call-wt2", "Todos written.")])

    # --- R3: file_metadata × 2 + read_excerpt ---------------------
    r3 = _ai(_R3_TEXT, tool_calls=[
        {"name": "file_metadata", "args": {"path": "originals/t1/showcase_fixture.md"}, "id": "call-fm1", "type": "tool_call"},
        {"name": "file_metadata", "args": {"path": "originals/t1/scan.pdf"}, "id": "call-fm2", "type": "tool_call"},
        {"name": "read_excerpt", "args": {
            "path": "originals/t1/showcase_fixture.md",
            "mode": "lines", "offset": 0, "limit": 20,
        }, "id": "call-re1", "type": "tool_call"},
    ])
    _add_msg(r3)
    _add_values()

    # T3-T5: file_metadata × 2 + read_excerpt (one batch)
    _add_tool_batch(r3, [
        ("file_metadata", "call-fm1", _json_output(
            path="originals/t1/showcase_fixture.md", name="showcase_fixture.md",
            extension=".md", file_type="text", size_bytes=512, size_human="512 B",
            page_count=None, char_count=512, word_count=80, line_count=20,
            encoding="utf-8",
        )),
        ("file_metadata", "call-fm2", _json_output(
            path="originals/t1/scan.pdf", name="scan.pdf",
            extension=".pdf", file_type="pdf", size_bytes=2048, size_human="2.0 KB",
            page_count=3, char_count=None, word_count=None, line_count=None,
            encoding=None,
        )),
        ("read_excerpt", "call-re1", "[lines 1-20 of 20]\n1: # Showcase Fixture\n2: This is a test document.\n..."),
    ])

    # --- R4: get_schema + list_graphs (parallel) ------------------
    r4 = _ai(_R4_TEXT, tool_calls=[
        {"name": "get_schema", "args": {}, "id": "call-gs", "type": "tool_call"},
        {"name": "list_graphs", "args": {}, "id": "call-lg", "type": "tool_call"},
    ])
    _add_msg(r4)
    _add_values()

    # T6-T7: get_schema + list_graphs
    _add_tool_batch(r4, [
        ("get_schema", "call-gs", _json_output(
            labels=["Resource", "Machine", "Transport", "Shift"],
            relationships=["USES_RESOURCE", "LOCATED_IN", "RUNS_DURING"],
            properties=["name", "id", "quantity", "location"],
        )),
        ("list_graphs", "call-lg", '["_debug_smoke_2026-08-13T12:00:00Z", "factory_planning"]'),
    ])

    # --- R5: nl_query + search × 2 --------------------------------
    r5 = _ai(_R5_TEXT, tool_calls=[
        {"name": "nl_query", "args": {"question": "How many nodes are in the graph?"}, "id": "call-nq", "type": "tool_call"},
        {"name": "search", "args": {"query": "machine", "mode": "fulltext"}, "id": "call-s1", "type": "tool_call"},
        {"name": "search", "args": {"query": "factory equipment", "mode": "vector"}, "id": "call-s2", "type": "tool_call"},
    ])
    _add_msg(r5)
    _add_values()

    # T8-T10: nl_query + search × 2
    _add_tool_batch(r5, [
        ("nl_query", "call-nq", "The graph contains 19 nodes across 3 entity types."),
        ("search", "call-s1", '[{"name": "CNC Machine", "score": 1.0}, {"name": "Lathe", "score": 0.85}]'),
        ("search", "call-s2", '[{"name": "CNC Machine", "score": 0.95}, {"name": "Hall A", "score": 0.88}]'),
    ])

    # --- R6: get_reconciliations + update_graph_description --------
    r6 = _ai(_R6_TEXT, tool_calls=[
        {"name": "get_reconciliations", "args": {}, "id": "call-gr", "type": "tool_call"},
        {"name": "update_graph_description", "args": {
            "description": "Showcase graph with 19 nodes (Resource, Machine, Transport) extracted from 2 source files.",
        }, "id": "call-ugd", "type": "tool_call"},
    ])
    _add_msg(r6)
    _add_values()

    # T11-T12: get_reconciliations + update_graph_description
    _add_tool_batch(r6, [
        ("get_reconciliations", "call-gr", "[]"),
        ("update_graph_description", "call-ugd", _json_output(
            updated=True,
            active_graph="_debug_smoke_2026-08-13T12:00:00Z",
            description="Showcase graph with 19 nodes (Resource, Machine, Transport) extracted from 2 source files.",
        )),
    ])

    # --- R7: final answer (no tool calls) -------------------------
    r7 = _ai(_FINAL_ANSWER)
    _add_msg(r7)
    _add_values()
    # Emit a model task result for R7 (no tool_calls → no batch opened,
    # but keeps the task stream faithful to the real protocol).
    _add_model_task_result(r7)

    # Build a single interleaved timeline so the raw and tasks consumers
    # (driven by asyncio.gather) observe events in faithful protocol order.
    # The real LangGraph v3 protocol has ONE underlying event stream that the
    # mux dispatches to projections; a model task result (tasks) and the
    # corresponding AIMessage (raw) arrive in the same Pregel step, and a
    # later model message cannot arrive before an earlier batch's tool
    # results. Interleaving here prevents _consume_raw from racing ahead of
    # _consume_tasks and creating the final answer message before earlier
    # tool steps are rendered.
    #
    # The scenario call sequence appends to raw_events and task_events in
    # strict protocol order per batch:
    #   raw: msg, values
    #   tasks: model_start, model_result, (tools_start × N), (tools_result × N)
    # We merge by walking raw_events in order and, after each "values" event
    # (the batch boundary), draining the corresponding task batch.
    timeline: list[tuple[str, dict]] = []
    ri = 0
    ti = 0
    while ri < len(raw_events):
        rev = raw_events[ri]
        timeline.append(("raw", rev))
        ri += 1
        # After a "values" event, drain the corresponding task batch.
        if rev.get("method") == "values":
            while ti < len(task_events):
                tev = task_events[ti]
                tname = tev.get("name", "")
                has_result = "result" in tev
                if tname == "model" and not has_result:
                    timeline.append(("tasks", tev))
                    ti += 1
                elif tname == "model" and has_result:
                    timeline.append(("tasks", tev))
                    ti += 1
                    while ti < len(task_events) and task_events[ti].get("name") == "tools":
                        timeline.append(("tasks", task_events[ti]))
                        ti += 1
                    break
                else:
                    timeline.append(("tasks", tev))
                    ti += 1
                    if has_result:
                        break

    return timeline


# ---------------------------------------------------------------------------
# MockRunStream — mimics AsyncGraphRunStream
# ---------------------------------------------------------------------------


class _MockTasksProjection:
    """Mimics the ``run.tasks`` async iterable projection.

    Shares a single timeline cursor with the raw projection so the two
    consumers (driven by ``asyncio.gather`` in ``on_message``) observe
    events in faithful protocol order — a later raw message cannot arrive
    before an earlier batch's task events have been consumed.
    """

    def __init__(self, timeline: list[tuple[str, dict]], cursor: "_SharedCursor") -> None:
        self._timeline = timeline
        self._cursor = cursor

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._cursor.iterate("tasks", self._timeline)


class _SharedCursor:
    """Shared timeline cursor coordinating the raw and tasks consumers.

    Both consumers call :meth:`iterate` with their stream name; each
    advances the shared index over the timeline, skipping events that
    belong to the other stream. An ``asyncio.Lock`` serializes index
    advancement so the two consumers see events in true timeline order
    regardless of which ``asyncio.gather`` task wakes first.
    """

    def __init__(self) -> None:
        self._index = 0
        self._lock = asyncio.Lock()

    async def iterate(self, stream: str, timeline: list[tuple[str, dict]]) -> AsyncIterator[dict]:
        while True:
            async with self._lock:
                if self._index >= len(timeline):
                    return
                s, ev = timeline[self._index]
                if s != stream:
                    # Not ours — let the other consumer advance. Yield
                    # control so asyncio.gather schedules the other task.
                    pass
                else:
                    self._index += 1
                    item = ev
            if s == stream:
                await asyncio.sleep(0)
                yield item
            else:
                # Yield control to the other consumer.
                await asyncio.sleep(0)


class MockRunStream:
    """Mimics ``AsyncGraphRunStream`` for the chat-flow test harness.

    Supports:
    - ``async with run:`` (async context manager)
    - ``async for event in run:`` (raw protocol events)
    - ``run.tasks`` (async-iterable task projection)

    The raw and tasks projections share a single timeline cursor so the
    two concurrent consumers (driven by ``asyncio.gather`` in
    ``on_message``) observe events in faithful protocol order — mirroring
    the real LangGraph v3 protocol where a single underlying event
    stream feeds both projections via the mux.
    """

    def __init__(self, timeline: list[tuple[str, dict]]) -> None:
        self._timeline = timeline
        self._cursor = _SharedCursor()
        self.tasks = _MockTasksProjection(timeline, self._cursor)

    async def __aenter__(self) -> "MockRunStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._cursor.iterate("raw", self._timeline)


# ---------------------------------------------------------------------------
# MockAgent — drop-in replacement for the LangGraph agent
# ---------------------------------------------------------------------------


class MockAgent:
    """Minimal agent stub whose ``astream_events`` yields v3 protocol events.

    ``on_message`` calls ``await agent.astream_events(agent_input,
    version="v3", config={...}, transformers=[...])`` and expects an
    awaitable that resolves to an ``AsyncGraphRunStream``-compatible
    object. We accept any input/config/transformers and return a
    :class:`MockRunStream` that yields pre-built v3 protocol events.
    """

    def __init__(self, events_fn: Any):
        self._events_fn = events_fn

    async def astream_events(
        self,
        agent_input: Any,
        version: str = "v3",
        config: Any = None,
        transformers: Any = None,
    ) -> MockRunStream:
        timeline = await self._events_fn()
        return MockRunStream(timeline)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_register(Scenario(
    name=SHOWCASE_FLOW_NAME,
    description=SHOWCASE_FLOW_DESCRIPTION,
    events=_showcase_flow,
))


def get_scenario(name: str) -> Scenario | None:
    """Return the scenario by name, or ``None`` if not found."""
    return SCENARIOS.get(name)


def build_mock_agent(scenario: Scenario) -> MockAgent:
    """Build a :class:`MockAgent` for the given scenario."""
    return MockAgent(scenario.events)