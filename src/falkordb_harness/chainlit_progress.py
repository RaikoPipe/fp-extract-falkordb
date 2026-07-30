"""Shared TaskList-based progress UI for ingestion runs.

Both ingestion entry points — the ``ingest_documents`` action button
(:func:`falkordb_harness.chainlit_app.on_ingest_documents`) and the agent's
``extract_and_write`` tool (when invoked through the Chainlit UI) — drive the
same live ``cl.TaskList`` panel via :func:`make_ingestion_progress`.

The factory returns a ``(progress, finalize)`` pair plus the underlying
``TaskList`` so callers can attach the panel to a specific Chainlit message
(button path attaches it to a standalone chat message; agent path attaches it
to the in-flight assistant message so it renders inline with the streamed
reply). ``progress`` matches :data:`ingest_runner.ProgressFn` and switches on
the ``details["kind"]`` discriminant emitted by :func:`run_ingestion`.
``finalize`` marks any still-running tasks DONE/FAILED and sets the panel
status — call it from the caller's success/exception handlers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from chainlit.element import Task, TaskList, TaskStatus

from falkordb_harness.i18n import t
from falkordb_harness.ingest_runner import ProgressFn

logger = logging.getLogger("falkordb_harness.chainlit_progress")


async def make_ingestion_progress() -> (
    tuple[TaskList, ProgressFn, Callable[[bool], Awaitable[None]]]
):
    """Build a live ``TaskList`` progress panel + matching ``ProgressFn``.

    The panel is sent as a standalone chat element (Chainlit's
    ``TaskList.send`` hardcodes ``for_id=""`` so it can't be nested inside a
    specific message/Step). It renders in the chat timeline and updates live
    as the pipeline advances; both the action-button path and the
    agent-driven ``extract_and_write`` path share this UX.

    Returns:
        ``(tasklist, progress, finalize)`` where:

        - ``tasklist`` is the live :class:`cl.TaskList` (already sent).
        - ``progress`` is an :data:`ingest_runner.ProgressFn` that updates
          the panel as the pipeline advances.
        - ``finalize(success: bool)`` marks any still-running tasks
          DONE (``success=True``) or FAILED (``success=False``) and sets
          the panel status to ``"Done"``/``"Failed"``; await it from the
          caller's success/exception handlers.
    """
    tasklist = TaskList()
    tasklist.status = "Running"
    await tasklist.send()

    _stage_titles = {
        "stage": t("ingest.stage.stage"),
        "preprocess": t("ingest.stage.preprocess"),
        "chunk": t("ingest.stage.chunk"),
        "extract": t("ingest.stage.extract"),
        "write": t("ingest.stage.write"),
    }
    stage_tasks: dict[str, Task] = {}
    file_tasks: dict[tuple[str, str], Task] = {}

    async def _get_stage_task(stage: str) -> Task:
        task = stage_tasks.get(stage)
        if task is None:
            task = Task(title=_stage_titles.get(stage, stage), status=TaskStatus.RUNNING)
            stage_tasks[stage] = task
            await tasklist.add_task(task)
            await tasklist.update()
        return task

    async def progress(label: str, details: dict | None = None) -> None:
        kind = (details or {}).get("kind", "info")
        stage = (details or {}).get("stage", "")
        fname = (details or {}).get("file")

        if kind == "stage_start":
            await _get_stage_task(stage)
        elif kind == "stage_end":
            task = stage_tasks.get(stage)
            if task:
                task.status = TaskStatus.DONE
                await tasklist.update()
        elif kind == "file_start" and fname:
            await _get_stage_task(stage)
            ftask = Task(title=f"{stage}: {fname}", status=TaskStatus.RUNNING)
            file_tasks[(stage, fname)] = ftask
            await tasklist.add_task(ftask)
            await tasklist.update()
        elif kind == "file_end" and fname:
            ftask = file_tasks.pop((stage, fname), None)
            if ftask:
                ftask.status = TaskStatus.DONE
                await tasklist.update()
        elif kind == "error" and fname:
            ftask = file_tasks.pop((stage, fname), None)
            if ftask:
                ftask.status = TaskStatus.FAILED
                await tasklist.update()
            err = (details or {}).get("error", "")
            err_task = Task(
                title=t("ingest.failed.file", stage=stage, file=fname, err=err[:160]),
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        elif kind == "error":
            err = (details or {}).get("error", "")
            err_task = Task(
                title=t(
                    "ingest.failed.stage",
                    stage=stage or "pipeline",
                    err=err[:160],
                ),
                status=TaskStatus.FAILED,
            )
            await tasklist.add_task(err_task)
            await tasklist.update()
        # info events: no task change; the label still surfaces in chat if needed.

    async def finalize(success: bool) -> None:
        final_status = TaskStatus.DONE if success else TaskStatus.FAILED
        for task in stage_tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.status = final_status
        for task in file_tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.status = final_status
        tasklist.status = "Done" if success else "Failed"
        await tasklist.update()

    return tasklist, progress, finalize


__all__ = ["make_ingestion_progress"]