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

Long-running stages (``extract`` and ``write``) emit granular ``progress``
events (``details = {"kind": "progress", "stage", "completed", "total"}``)
once per chunk / extraction. The handler feeds those into a per-stage
:class:`TimeEstimator` and rewrites the stage task's title live with a
tqdm-style line::

    LLM entity extraction  —  12/40  30%  [0:42 elapsed, ETA 1:38, 0.28 it/s]

This mirrors the tqdm display: the elapsed time starts at the first tick
(:meth:`TimeEstimator.reset` is called on the first ``progress`` event for a
stage, not at ``stage_start`` — the gap between ``stage_start`` and the first
chunk completing is setup, not extraction, and we don't want it inflating
the ETA). The ETA uses the cumulative-average rate ``n / elapsed`` (see
:mod:`progress_eta`), which is stable under the clustered-completion pattern
of concurrent extraction where a per-tick EMA would saturate.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from chainlit.element import Task, TaskList, TaskStatus

from falkordb_harness.i18n import t
from falkordb_harness.ingest_runner import ProgressFn
from falkordb_harness.progress_eta import TimeEstimator

logger = logging.getLogger("falkordb_harness.chainlit_progress")


def _render_tqdm_line(base_title: str, est: TimeEstimator) -> str:
    """Build a tqdm-style progress line for a stage task title.

    Format (EN)::

        <base title>  —  <n>/<total> <percent>%  [elapsed elapsed, ETA <eta>, <rate> it/s]

    Mirrors tqdm's default bar-less summary line. When the ETA is not yet
    estimable (no items completed / zero rate), shows ``ETA ?`` instead of a
    formatted duration so the user sees a clear "warming up" state rather
    than a misleading 0:00. The line is localized via
    :data:`falkordb_harness.i18n.STRINGS` ``ingest.progress.line``.
    """
    n = est.n
    total = est.total
    pct = est.percent
    rendered = est.render()
    elapsed_str = rendered["elapsed_str"]
    eta_str = rendered["eta_str"]
    rate = est.rate
    if total > 0:
        counter = f"{n}/{total}"
        percent = f"{pct:.0f}%"
    else:
        counter = f"{n}"
        percent = t("ingest.progress.percent_unknown")
    if rate > 0:
        rate_str = f"{rate:.2f} it/s"
    else:
        rate_str = t("ingest.progress.rate_unknown")
    return t(
        "ingest.progress.line",
        title=base_title,
        counter=counter,
        percent=percent,
        elapsed=elapsed_str,
        eta=eta_str,
        rate=rate_str,
    )


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
    # Per-stage tqdm-style time estimators. Created lazily on the first
    # ``progress`` event for a stage (not at ``stage_start``) so the elapsed
    # clock measures only the steady-state per-item work, not the stage
    # setup. Only ``extract`` and ``write`` currently emit ``progress``
    # events; other stages don't get an estimator (their tasks stay as
    # plain RUNNING rows).
    stage_estimators: dict[str, TimeEstimator] = {}
    # Original (un-decorated) stage titles so we can rebuild the tqdm line
    # from the base title each tick without re-looking-up the i18n key.
    stage_base_titles: dict[str, str] = {}

    async def _get_stage_task(stage: str) -> Task:
        task = stage_tasks.get(stage)
        if task is None:
            base_title = _stage_titles.get(stage, stage)
            task = Task(title=base_title, status=TaskStatus.RUNNING)
            stage_tasks[stage] = task
            stage_base_titles[stage] = base_title
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
                # Restore the plain base title (drops the last tqdm tick line).
                base = stage_base_titles.get(stage)
                if base is not None:
                    task.title = base
                await tasklist.update()
            stage_estimators.pop(stage, None)
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
        elif kind == "progress":
            # Within-stage tick: feed the estimator + rewrite the title with
            # a tqdm-style line. stage_start always fires before the first tick.
            completed = int((details or {}).get("completed", 0) or 0)
            total = int((details or {}).get("total", 0) or 0)
            task = stage_tasks.get(stage)
            if task is None:
                task = await _get_stage_task(stage)
            est = stage_estimators.get(stage)
            if est is None:
                est = TimeEstimator(total=total)
                stage_estimators[stage] = est
            elif est.total != total and total > 0:
                # Re-baseline when the real total arrives (chunk count is
                # unknown until chunking finishes) without resetting elapsed.
                est.total = total
            # ``completed`` is absolute, not a delta.
            delta = completed - est.n
            if delta > 0:
                est.update(delta)
            base = stage_base_titles.get(stage, stage)
            task.title = _render_tqdm_line(base, est)
            await tasklist.update()

    async def finalize(success: bool) -> None:
        final_status = TaskStatus.DONE if success else TaskStatus.FAILED
        for task in stage_tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.status = final_status
        for task in file_tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.status = final_status
        tasklist.status = "Done" if success else "Failed"
        # ``finalize`` runs from a ``finally``; a sibling CancelledError can
        # land during the await (Chainlit stop). Guard the emit so the panel
        # still flips to a terminal status.
        try:
            await tasklist.update()
        except Exception as exc:  # noqa: BLE001 — never strand the panel
            logger.warning("ingest progress finalize update failed: %s", exc)

    return tasklist, progress, finalize


__all__ = ["make_ingestion_progress"]