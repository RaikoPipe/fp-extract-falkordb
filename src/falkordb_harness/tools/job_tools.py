"""Agent tools for background jobs (ticket #77).

``run_in_background`` lets the orchestrating agent run ANY tool it has —
our own tools, deepagents built-ins (``execute``, ``task``, ...) and future
plugin tools — as a detached job, so the turn can end and the chat stays
responsive. Whether to background a call is the agent's decision (guided by
the BACKGROUND EXECUTION section of the system prompt).

The tool table is only complete after ``create_deep_agent`` has built the
deepagents built-ins, so ``run_in_background`` is created by
:func:`make_run_in_background` with a lookup that ``build_agent`` fills in
from the compiled graph's tools node.
"""

from __future__ import annotations

import inspect
import json
import typing
import uuid
from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState, InjectedStore
from pydantic import ValidationError

from falkordb_harness.background_jobs import (
    JobLimitError,
    current_thread_id,
    job_manager,
)

# Tools that need the live turn (interactive prompts, the todo panel, the
# session's graph selection) or would recurse into the job machinery.
NOT_BACKGROUNDABLE = frozenset({
    "write_todos",
    "ask_user",
    "request_ingestion_confirmation",
    "request_graph_switch",
    "use_graph",
    "run_in_background",
    "list_jobs",
    "get_job_status",
    "cancel_job",
})

ToolLookup = Callable[[], dict[str, BaseTool]]


def _active_graph() -> str | None:
    try:
        import chainlit as cl

        return (cl.user_session.get("graph_selection") or {}).get("active_graph")
    except Exception:  # noqa: BLE001 — not in a Chainlit context
        return None


def _detached_config(config: dict | None) -> dict:
    """Strip per-turn plumbing from the parent tool call's config.

    The parent run's callbacks belong to an event stream that closes when
    the turn ends, and ``__pregel_*`` keys bind to its graph step.
    """
    config = dict(config or {})
    config.pop("callbacks", None)
    configurable = {
        k: v for k, v in (config.get("configurable") or {}).items()
        if not k.startswith("__pregel")
    }
    config["configurable"] = configurable
    return config


def _injected_values(
    target: BaseTool, runtime: ToolRuntime, call_id: str
) -> dict[str, Any]:
    """Values for ``target``'s injected (non-LLM) arguments."""
    keys = target._injected_args_keys  # noqa: SLF001 — langchain-core public-ish API
    if not keys:
        return {}
    fn = getattr(target, "coroutine", None) or getattr(target, "func", None)
    try:
        hints = typing.get_type_hints(fn, include_extras=True) if fn else {}
    except Exception:  # noqa: BLE001 — unresolvable forward refs
        hints = {}
    values: dict[str, Any] = {}
    for key in keys:
        hint = hints.get(key)
        extras = getattr(hint, "__metadata__", ())
        if any(_is(e, InjectedToolCallId) for e in extras):
            values[key] = call_id
        elif any(_is(e, InjectedState) for e in extras):
            values[key] = runtime.state
        elif any(_is(e, InjectedStore) for e in extras):
            values[key] = runtime.store
        else:  # ToolRuntime (deepagents built-ins) and unknown injections
            values[key] = runtime
    return values


def _is(marker: Any, cls: type) -> bool:
    return marker is cls or isinstance(marker, cls) or (
        inspect.isclass(marker) and issubclass(marker, cls)
    )


def make_run_in_background(lookup: ToolLookup) -> BaseTool:
    """Build the ``run_in_background`` tool over the agent's tool table."""

    @tool
    async def run_in_background(
        tool_name: str,
        tool_args: dict,
        label: str,
        runtime: ToolRuntime,
    ) -> str:
        """Run one of your other tools as a background job and return at once.

        Use this for calls with a long execution time — see BACKGROUND
        EXECUTION in your instructions — so the user can keep working while
        the job runs. Returns ``{"job_id", "status": "started"}``; the
        result is NOT returned here. When the job finishes, the user sees a
        chat message and you receive a ``<background_job_updates>`` note at
        the start of the next user message. Do not wait for or poll the job.

        Args:
            tool_name: Name of the tool to run (e.g. ``extract_and_write``).
            tool_args: Arguments for that tool, exactly as you would pass
                them in a direct call.
            label: Short human-readable description shown to the user
                (e.g. "Ingest 5 files into plant_a").
        """
        if tool_name in NOT_BACKGROUNDABLE:
            return json.dumps({
                "error": f"'{tool_name}' cannot run in the background; call it directly.",
            })
        target = lookup().get(tool_name)
        if target is None:
            return json.dumps({"error": f"Unknown tool '{tool_name}'."})
        try:
            target.tool_call_schema.model_validate(tool_args)
        except ValidationError as exc:
            return json.dumps({
                "error": f"Invalid arguments for '{tool_name}': {exc}",
            })

        call_id = f"bg_{uuid.uuid4().hex[:12]}"
        inner_runtime = ToolRuntime(
            state=runtime.state,
            context=runtime.context,
            config=_detached_config(runtime.config),
            stream_writer=lambda *_a, **_k: None,
            tool_call_id=call_id,
            store=runtime.store,
        )
        call = {
            "type": "tool_call",
            "id": call_id,
            "name": tool_name,
            "args": {**tool_args, **_injected_values(target, inner_runtime, call_id)},
        }

        async def _invoke() -> str:
            out = await target.ainvoke(call, config=inner_runtime.config)
            if isinstance(out, ToolMessage):
                content = out.content
                return content if isinstance(content, str) else json.dumps(content)
            return out if isinstance(out, str) else str(out)

        try:
            job = job_manager.spawn(
                tool_name, _invoke, label=label, graph=_active_graph()
            )
        except JobLimitError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({
            "job_id": job.id,
            "status": "started",
            "label": job.label,
            "note": "Tell the user the job is running, then end your turn.",
        })

    return run_in_background


@tool
def list_jobs() -> str:
    """List this session's background jobs (running and finished)."""
    jobs = job_manager.list(current_thread_id())
    return json.dumps([j.to_dict(result_chars=200) for j in jobs], ensure_ascii=False)


@tool
def get_job_status(job_id: str) -> str:
    """Get the status and (when finished) full result of a background job.

    Only call this when the user asks about a job; finished jobs are
    reported to you automatically at the start of the next user message.
    """
    job = job_manager.get(current_thread_id(), job_id)
    if job is None:
        return json.dumps({"error": f"No background job '{job_id}' in this session."})
    return json.dumps(job.to_dict(), ensure_ascii=False)


@tool
def cancel_job(job_id: str) -> str:
    """Cancel a running background job of this session."""
    if job_manager.cancel(current_thread_id(), job_id):
        return json.dumps({"job_id": job_id, "status": "cancelling"})
    return json.dumps({"error": f"No running background job '{job_id}' in this session."})


job_query_tools = [list_jobs, get_job_status, cancel_job]
