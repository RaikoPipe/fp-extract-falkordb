"""Tests for background jobs (ticket #77): JobManager + run_in_background.

No Chainlit, LLM, or FalkorDB needed: jobs run on the test's event loop,
tools are plain LangChain tools, and the completion notifier is either
disabled (``announce=False``) or a no-op outside a Chainlit context.
"""

import asyncio
import contextvars
import json
import sys
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness import agent as agent_mod
from falkordb_harness.background_jobs import (
    JobLimitError,
    JobManager,
    current_job,
    format_pending_notes,
)
from falkordb_harness.tools import job_tools
from falkordb_harness.tools.job_tools import make_run_in_background


async def _settle(job) -> None:
    await asyncio.wait([job.task], timeout=5)
    assert job.task.done()


# --- JobManager ------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_returns_immediately_and_queues_note_on_completion():
    jm = JobManager()
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return "42 nodes"

    job = jm.spawn("extract_and_write", work, label="Ingest", thread_id="t1", announce=False)
    await asyncio.sleep(0)
    assert job.status == "running"
    assert jm.has_active("t1")
    assert jm.drain_pending("t1") == []

    gate.set()
    await _settle(job)
    assert job.status == "done"
    assert job.result == "42 nodes"
    notes = jm.drain_pending("t1")
    assert len(notes) == 1
    assert job.id in notes[0] and "done" in notes[0] and "42 nodes" in notes[0]
    assert jm.drain_pending("t1") == []  # drained exactly once


@pytest.mark.asyncio
async def test_failed_job_records_error():
    jm = JobManager()

    async def work():
        raise RuntimeError("docprep exploded")

    job = jm.spawn("preprocess_document", work, thread_id="t1", announce=False)
    await _settle(job)
    assert job.status == "failed"
    assert "docprep exploded" in job.error
    assert "docprep exploded" in jm.drain_pending("t1")[0]


@pytest.mark.asyncio
async def test_cancel_running_job():
    jm = JobManager()

    async def work():
        await asyncio.sleep(60)

    job = jm.spawn("execute", work, thread_id="t1", announce=False)
    await asyncio.sleep(0)
    assert jm.cancel("t1", job.id) is True
    await _settle(job)
    assert job.status == "cancelled"
    assert jm.cancel("t1", job.id) is False  # already finished
    assert jm.cancel("other-thread", job.id) is False  # scoped per thread
    assert "cancelled" in jm.drain_pending("t1")[0]


@pytest.mark.asyncio
async def test_cancel_before_first_step_still_finalizes_job():
    jm = JobManager()

    async def work():
        return "never"

    job = jm.spawn("execute", work, thread_id="t1", announce=False)
    assert jm.cancel("t1", job.id) is True  # no await: task not started yet
    await _settle(job)
    assert job.status == "cancelled"
    assert not jm.has_active("t1")
    assert len(jm.drain_pending("t1")) == 1


@pytest.mark.asyncio
async def test_limit_counts_only_active_jobs_per_thread():
    jm = JobManager(max_active_per_thread=1)
    gate = asyncio.Event()

    async def work():
        await gate.wait()

    first = jm.spawn("a", work, thread_id="t1", announce=False)
    with pytest.raises(JobLimitError):
        jm.spawn("b", work, thread_id="t1", announce=False)
    jm.spawn("c", work, thread_id="t2", announce=False)  # other thread unaffected
    gate.set()
    await _settle(first)
    jm.spawn("d", work, thread_id="t1", announce=False)  # slot freed


@pytest.mark.asyncio
async def test_job_inherits_spawning_context_and_sees_current_job():
    """Contextvars (e.g. the session FalkorDBBackend) are pinned at spawn."""
    jm = JobManager()
    graph = contextvars.ContextVar("graph", default=None)
    graph.set("plant_a")
    seen = {}

    async def work():
        await asyncio.sleep(0)
        seen["graph"] = graph.get()
        seen["job"] = current_job()

    job = jm.spawn("x", work, thread_id="t1", announce=False)
    graph.set("plant_b")  # a later graph switch must not affect the job
    await _settle(job)
    assert seen == {"graph": "plant_a", "job": job}
    assert current_job() is None


@pytest.mark.asyncio
async def test_notifier_called_with_finished_job():
    jm = JobManager()
    notified = []

    async def notifier(job):
        notified.append((job.id, job.status))

    async def work():
        return "ok"

    job = jm.spawn("x", work, thread_id="t1", notifier=notifier)
    await _settle(job)
    await asyncio.sleep(0)
    assert notified == [(job.id, "done")]


def test_format_pending_notes_wraps_block():
    block = format_pending_notes(["a", "b"])
    assert block.startswith("<background_job_updates>")
    assert block.endswith("</background_job_updates>")
    assert "a\n\nb" in block


# --- run_in_background -----------------------------------------------------


@tool
async def slow_tool(n: int) -> str:
    """Pretend to do long work."""
    await asyncio.sleep(0)
    return f"processed {n}"


@tool
def runtime_tool(path: str, runtime: ToolRuntime) -> str:
    """A deepagents-style tool with an injected ToolRuntime."""
    return f"{path}|{runtime.tool_call_id}|{runtime.state.get('marker')}"


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [], "marker": "S"},
        context=None,
        config={"callbacks": ["parent-cb"], "configurable": {"__pregel_send": 1, "thread_id": "t"}},
        stream_writer=lambda *_: None,
        tool_call_id="outer",
        store=None,
    )


@pytest.fixture
def jm(monkeypatch):
    manager = JobManager()
    monkeypatch.setattr(job_tools, "job_manager", manager)
    return manager


async def _call_rib(rib, **args):
    msg = await rib.ainvoke({
        "type": "tool_call",
        "id": "c1",
        "name": "run_in_background",
        "args": {**args, "runtime": _runtime()},
    })
    return json.loads(msg.content)


def _only_job(jm):
    (job,) = jm.list("_unscoped")
    return job


@pytest.mark.asyncio
async def test_run_in_background_runs_tool_detached(jm):
    rib = make_run_in_background(lambda: {"slow_tool": slow_tool})
    out = await _call_rib(rib, tool_name="slow_tool", tool_args={"n": 3}, label="Slow")
    assert out["status"] == "started"
    job = _only_job(jm)
    assert out["job_id"] == job.id and job.label == "Slow"
    await _settle(job)
    assert job.status == "done"
    assert job.result == "processed 3"


@pytest.mark.asyncio
async def test_run_in_background_injects_runtime_into_builtin_style_tools(jm):
    rib = make_run_in_background(lambda: {"runtime_tool": runtime_tool})
    await _call_rib(rib, tool_name="runtime_tool", tool_args={"path": "/a.md"}, label="R")
    job = _only_job(jm)
    await _settle(job)
    path, call_id, marker = job.result.split("|")
    assert path == "/a.md"
    assert call_id.startswith("bg_")
    assert marker == "S"


def test_detached_config_drops_parent_plumbing():
    cfg = job_tools._detached_config(_runtime().config)
    assert "callbacks" not in cfg
    assert cfg["configurable"] == {"thread_id": "t"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(job_tools.NOT_BACKGROUNDABLE))
async def test_run_in_background_refuses_interactive_tools(jm, name):
    rib = make_run_in_background(lambda: {name: slow_tool})
    out = await _call_rib(rib, tool_name=name, tool_args={}, label="x")
    assert "cannot run in the background" in out["error"]
    assert jm.list("_unscoped") == []


@pytest.mark.asyncio
async def test_run_in_background_rejects_unknown_tool_and_bad_args(jm):
    rib = make_run_in_background(lambda: {"slow_tool": slow_tool})
    out = await _call_rib(rib, tool_name="nope", tool_args={}, label="x")
    assert "Unknown tool" in out["error"]
    out = await _call_rib(rib, tool_name="slow_tool", tool_args={"n": "many"}, label="x")
    assert "Invalid arguments" in out["error"]
    assert jm.list("_unscoped") == []


@pytest.mark.asyncio
async def test_run_in_background_reports_limit(jm):
    jm.max_active_per_thread = 0
    rib = make_run_in_background(lambda: {"slow_tool": slow_tool})
    out = await _call_rib(rib, tool_name="slow_tool", tool_args={"n": 1}, label="x")
    assert "limit 0" in out["error"]


@pytest.mark.asyncio
async def test_job_query_tools(jm):
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return "r"

    job = jm.spawn("x", work, label="L", thread_id="_unscoped", announce=False)
    listed = json.loads(job_tools.list_jobs.invoke({}))
    assert [j["job_id"] for j in listed] == [job.id]
    status = json.loads(job_tools.get_job_status.invoke({"job_id": job.id}))
    assert status["status"] == "running"
    assert "error" in json.loads(job_tools.get_job_status.invoke({"job_id": "zzz"}))
    assert json.loads(job_tools.cancel_job.invoke({"job_id": job.id}))["status"] == "cancelling"
    await _settle(job)
    assert "error" in json.loads(job_tools.cancel_job.invoke({"job_id": job.id}))


# --- build_agent wiring ----------------------------------------------------


def test_build_agent_exposes_builtins_to_run_in_background(monkeypatch, tmp_path):
    """run_in_background's lookup must include deepagents built-ins that
    only exist after create_deep_agent (e.g. ``execute``, ``read_file``)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PYTHON_RUNNER_ENABLE", raising=False)
    fake = GenericFakeChatModel(messages=iter([AIMessage(content="hi")]))
    monkeypatch.setattr(agent_mod, "resolve_model", lambda *_a, **_k: fake)

    graph = agent_mod.build_agent({"configurable": {"role": "user"}})
    tools = graph.nodes["tools"].bound.tools_by_name
    rib = tools["run_in_background"]
    lookup = next(
        c.cell_contents for c in rib.coroutine.__closure__
        if getattr(c.cell_contents, "__name__", "") == "<lambda>"
    )
    table = lookup()
    for name in ("execute", "read_file", "extract_and_write", "list_jobs"):
        assert name in table
    assert "reset_graph" not in table  # role gating still applies
