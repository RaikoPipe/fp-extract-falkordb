"""In-memory background job manager (ticket #77).

Problem
-------
Chainlit wraps every ``on_message`` / action callback in a
``task_start``/``task_end`` pair: while the handler task runs, the chat
input is in its busy state and the user can only press stop. A long tool
call (ingestion, docprep, a heavy ``execute``) therefore blocks the whole
session until it returns.

Design
------
:class:`JobManager` runs a coroutine as a detached ``asyncio`` task so the
handler (and with it the agent turn) can return immediately. The detached
task inherits a *copy* of the spawning context, which gives us for free:

* the Chainlit session/emitter (``cl.context``), so the job can post a
  completion message after the turn has ended;
* the per-session ``FalkorDBBackend`` contextvar, so a job stays pinned to
  the graph that was active when it was spawned even if the user switches
  graphs afterwards.

On completion the job (a) posts a chat message (unless the caller handles
its own UI, ``announce=False``) and (b) queues a short note in the
per-thread pending queue. ``on_message`` drains that queue at the start of
the next turn and prepends it to the agent input, so the agent learns about
finished jobs without polling. Jobs never touch ``chat_history`` directly,
which would race with a concurrently running turn.

Scope (P1): single-process, in-memory only. Jobs do not survive a server
restart. Jobs keep running across websocket disconnects (like in-flight
agent streams, see ``stream_recovery.py``); the stop button only cancels
the current turn, never a background job — use ``cancel``.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

_UNSCOPED = "_unscoped"

# Max characters of a job result kept for the agent's pending note and the
# completion message. Full results stay on the Job record (get_job_status).
_NOTE_RESULT_CHARS = 4000
_MESSAGE_RESULT_CHARS = 1500

def current_thread_id() -> str:
    """Return the Chainlit thread id of the caller, or ``_unscoped``."""
    try:
        import chainlit as cl

        return cl.context.session.thread_id or _UNSCOPED
    except Exception:  # noqa: BLE001 — not in a Chainlit context (CLI / tests)
        return _UNSCOPED


class JobLimitError(RuntimeError):
    """Raised when a thread already runs the maximum number of jobs."""


@dataclass
class Job:
    id: str
    thread_id: str
    name: str
    label: str
    graph: str | None = None
    status: str = "running"  # running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def is_active(self) -> bool:
        return self.status == "running"

    def to_dict(self, *, result_chars: int | None = None) -> dict[str, Any]:
        end = self.finished_at or time.time()
        result = self.result
        if result is not None and result_chars is not None:
            result = _truncate(result, result_chars)
        return {
            "job_id": self.id,
            "tool": self.name,
            "label": self.label,
            "graph": self.graph,
            "status": self.status,
            "elapsed_s": round(end - self.created_at, 1),
            "result": result,
            "error": self.error,
        }


# Set inside a running job's context; lets tools detect they run detached
# from an agent turn (e.g. extract_and_write building its own progress UI).
_CURRENT_JOB: contextvars.ContextVar[Job | None] = contextvars.ContextVar(
    "falkordb_harness_current_job", default=None
)


def current_job() -> Job | None:
    """Return the job whose task is executing the caller, or ``None``."""
    return _CURRENT_JOB.get()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more chars]"


Notifier = Callable[[Job], Awaitable[None]]


class JobManager:
    """Registry + runner for background jobs, keyed by Chainlit thread id."""

    def __init__(self, max_active_per_thread: int | None = None) -> None:
        if max_active_per_thread is None:
            # ``or``: docker-compose passes unset vars as "" (``${X:-}``).
            max_active_per_thread = int(os.getenv("BACKGROUND_MAX_JOBS") or "3")
        self.max_active_per_thread = max_active_per_thread
        self._jobs: dict[str, dict[str, Job]] = {}
        self._pending: dict[str, list[str]] = {}

    # --- lifecycle ---------------------------------------------------------

    def spawn(
        self,
        name: str,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        label: str = "",
        graph: str | None = None,
        thread_id: str | None = None,
        announce: bool = True,
        notifier: Notifier | None = None,
    ) -> Job:
        """Start ``coro_factory()`` as a detached task and return its Job.

        ``announce=False`` skips the generic completion chat message (for
        callers that post their own summary, e.g. the toolbar buttons); the
        pending note for the agent is queued either way.
        """
        thread_id = thread_id or current_thread_id()
        active = [j for j in self.list(thread_id) if j.is_active]
        if len(active) >= self.max_active_per_thread:
            raise JobLimitError(
                f"{len(active)} background job(s) already running in this "
                f"session (limit {self.max_active_per_thread}); wait for one "
                "to finish or cancel it."
            )
        job = Job(
            id=uuid.uuid4().hex[:8],
            thread_id=thread_id,
            name=name,
            label=label or name,
            graph=graph,
        )
        self._jobs.setdefault(thread_id, {})[job.id] = job
        notify = notifier or (_post_completion_message if announce else None)
        job.task = asyncio.create_task(
            self._run(job, coro_factory, notify), name=f"bgjob-{job.id}"
        )
        job.task.add_done_callback(lambda _t: self._on_task_done(job))
        logger.info("background job {} started: {} ({})", job.id, job.label, name)
        return job

    async def _run(
        self,
        job: Job,
        coro_factory: Callable[[], Awaitable[Any]],
        notify: Notifier | None,
    ) -> None:
        _CURRENT_JOB.set(job)
        _detach_from_turn_steps()
        try:
            result = await coro_factory()
            job.result = result if isinstance(result, str) else str(result)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 — a job failure must not crash the loop
            logger.exception("background job {} failed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = time.time()
        self._pending.setdefault(job.thread_id, []).append(_agent_note(job))
        if notify is not None:
            try:
                await notify(job)
            except Exception as exc:  # noqa: BLE001 — notification is best-effort
                logger.warning("background job {} notify failed: {}", job.id, exc)

    def _on_task_done(self, job: Job) -> None:
        # A task cancelled before its first step never enters ``_run``, so
        # its job would stay "running" forever without this fallback.
        if job.is_active:
            job.status = "cancelled"
            job.finished_at = time.time()
            self._pending.setdefault(job.thread_id, []).append(_agent_note(job))

    def cancel(self, thread_id: str, job_id: str) -> bool:
        """Request cancellation; ``False`` if unknown or already finished."""
        job = self.get(thread_id, job_id)
        if job is None or not job.is_active or job.task is None:
            return False
        job.task.cancel()
        return True

    # --- queries -----------------------------------------------------------

    def get(self, thread_id: str, job_id: str) -> Job | None:
        return self._jobs.get(thread_id, {}).get(job_id)

    def list(self, thread_id: str) -> list[Job]:
        return list(self._jobs.get(thread_id, {}).values())

    def has_active(self, thread_id: str) -> bool:
        return any(j.is_active for j in self.list(thread_id))

    def drain_pending(self, thread_id: str) -> list[str]:
        """Pop the notes of jobs that finished since the last turn."""
        return self._pending.pop(thread_id, [])


def _agent_note(job: Job) -> str:
    head = f"[Background job {job.id} ({job.label}, tool {job.name}) {job.status}"
    if job.graph:
        head += f", graph {job.graph}"
    head += f", {job.to_dict()['elapsed_s']}s]"
    if job.status == "done":
        return f"{head}\nResult:\n{_truncate(job.result or '', _NOTE_RESULT_CHARS)}"
    if job.status == "failed":
        return f"{head}\nError: {job.error}"
    return head


def format_pending_notes(notes: list[str]) -> str:
    """Render drained notes as a block to prepend to the user's message."""
    return (
        "<background_job_updates>\n"
        + "\n\n".join(notes)
        + "\n</background_job_updates>"
    )


def _detach_from_turn_steps() -> None:
    """Make messages posted by the job top-level chat messages.

    The job context is a copy of the agent turn's context, whose
    ``local_steps`` still holds the ``on_message`` run step; without this
    reset a completion message would be nested under a finished turn.
    """
    try:
        from chainlit.context import local_steps

        local_steps.set(None)
    except Exception:  # noqa: BLE001 — chainlit not installed / no context
        pass


async def _post_completion_message(job: Job) -> None:
    """Post the generic completion chat message (Chainlit only)."""
    try:
        import chainlit as cl

        cl.context.session  # noqa: B018 — raises outside a Chainlit context
    except Exception:  # noqa: BLE001
        return
    from falkordb_harness.i18n import t

    if job.status == "done":
        content = t("job.done", label=job.label, id=job.id)
        if job.result:
            content += f"\n\n```\n{_truncate(job.result, _MESSAGE_RESULT_CHARS)}\n```"
    elif job.status == "failed":
        content = t("job.failed", label=job.label, id=job.id, err=job.error or "")
    else:
        content = t("job.cancelled", label=job.label, id=job.id)
    await cl.Message(content=content).send()


job_manager = JobManager()
