"""Tests for the tqdm-style time estimator and Chainlit progress renderer.

The :class:`TimeEstimator` is a pure-Python dependency-free helper that
mirrors tqdm's elapsed / rate-EMA / ETA computation; the
:func:`_render_tqdm_line` helper in :mod:`falkordb_harness.chainlit_progress`
formats it into a localized one-liner for the live ``cl.TaskList`` panel.

The estimator / formatter / ``extract_from_chunks`` callback tests are
pure-Python and run without Chainlit installed. The
``make_ingestion_progress`` integration tests need ``chainlit.element`` and
are skipped when the package isn't importable (matching the repo's existing
"all mocked, no live Chainlit needed" test posture — see AGENTS.md).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness import i18n
from falkordb_harness.i18n import set_lang
from falkordb_harness.progress_eta import TimeEstimator, format_duration

# _render_tqdm_line lives in chainlit_progress which imports chainlit.element
# at module load. Defer the import to keep this test module importable
# without chainlit installed; the chainlit-dependent tests below use a
# module-level skip via pytest.importorskip.

chainlit_available = True
try:
    import chainlit.element  # noqa: F401
except ImportError:
    chainlit_available = False


@pytest.fixture(autouse=True)
def _reset_cli_lang():
    i18n._CLI_LANG = None
    yield
    i18n._CLI_LANG = None


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------

def test_format_duration_seconds_under_minute():
    assert format_duration(0.5) == "0:00"
    assert format_duration(0) == "0:00"
    assert format_duration(59.4) == "0:59"  # rounded down
    assert format_duration(59.6) == "1:00"


def test_format_duration_minutes():
    assert format_duration(65) == "1:05"
    assert format_duration(125) == "2:05"


def test_format_duration_hours():
    assert format_duration(3700) == "1:01:40"
    assert format_duration(3600) == "1:00:00"


def test_format_duration_unknown_sentinels():
    assert format_duration(-1) == "?"
    assert format_duration(None) == "?"
    assert format_duration(float("nan")) == "?"
    assert format_duration(float("inf")) == "?"


# ---------------------------------------------------------------------------
# TimeEstimator — basic accounting
# ---------------------------------------------------------------------------

def test_estimator_starts_at_zero():
    est = TimeEstimator(total=10)
    assert est.n == 0
    assert est.total == 10
    assert est.remaining == 10
    assert est.percent == 0.0
    assert est.elapsed >= 0.0


def test_estimator_update_advances_n():
    est = TimeEstimator(total=10)
    est.update(1)
    assert est.n == 1
    est.update(3)
    assert est.n == 4
    assert est.remaining == 6
    assert est.percent == 40.0


def test_estimator_clamps_percent_at_100():
    est = TimeEstimator(total=4)
    est.update(10)  # overshoot
    assert est.n == 10
    assert est.percent == 100.0
    assert est.remaining == 0


def test_estimator_negative_n_clamped():
    est = TimeEstimator(total=4)
    est.n = 2
    est.update(-5)
    assert est.n == 0


def test_estimator_total_zero_percent_zero():
    est = TimeEstimator(total=0)
    est.update(5)
    assert est.percent == 0.0
    assert est.eta == -1.0


# ---------------------------------------------------------------------------
# TimeEstimator — rate / ETA
# ---------------------------------------------------------------------------

def test_estimator_eta_unknown_until_first_tick():
    est = TimeEstimator(total=10)
    assert est.rate == 0.0
    assert est.eta == -1.0


def test_estimator_eta_becomes_estimable_after_first_tick():
    est = TimeEstimator(total=10)
    # Fake the monotonic clock so the rate is deterministic.
    t0 = [100.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 100.0
        t0[0] = 101.0  # advance 1s
        est.update(1)  # rate ~1 it/s
        assert est.rate > 0
        assert est.eta >= 0
        # remaining 9 / rate ~1 -> ETA ~9s (within a tolerance)
        assert 8.0 <= est.eta <= 10.0


def test_estimator_eta_zero_when_complete():
    est = TimeEstimator(total=2)
    est.update(2)
    assert est.n >= est.total
    assert est.eta == 0.0


def test_estimator_rate_robust_to_concurrent_clustering():
    """Cumulative-average rate must not saturate under clustered ticks.

    ``extract_from_chunks`` runs LLM calls at bounded concurrency, so
    completion ticks arrive in clusters: several within microseconds,
    then a long pause. A per-tick EMA (tqdm's ``1/dt``) saturates at the
    ``_MIN_DT`` clamp during each cluster and reports millions of it/s,
    collapsing the ETA to 0:00. The cumulative average ``n / elapsed``
    is immune because both ``n`` and ``elapsed`` are monotone.
    """
    est = TimeEstimator(total=8)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        # Two clusters of 4 completions each (mimics concurrent LLM calls) 5s apart.
        t0[0] = 5.0
        est.update(4)
        first_rate = est.rate
        t0[0] = 10.0
        est.update(4)
        final_rate = est.rate
        final_eta = est.eta
    # True rate is 8 items / 10s = 0.8 it/s; ETA = 0/0.8 = 0 (complete).
    # Critical: rate must NOT be in the millions (the old EMA bug).
    assert first_rate < 1.5  # 4/5 = 0.8, not ~4e6
    assert final_rate < 1.5  # 8/10 = 0.8, not ~4e6
    assert final_rate > 0.5
    assert final_eta == 0.0  # n >= total


def test_estimator_rate_cumulative_average_matches_n_over_elapsed():
    """``rate`` is exactly ``n / elapsed`` once elapsed > _MIN_DT."""
    est = TimeEstimator(total=100)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        t0[0] = 20.0
        est.update(5)
        # 5 items in 20s -> 0.25 it/s; remaining 95 / 0.25 = 380s.
        assert abs(est.rate - 0.25) < 1e-6
        assert abs(est.eta - 380.0) < 1e-3


def test_estimator_reset_reuses_total_when_none():
    est = TimeEstimator(total=10)
    est.update(3)
    est.reset()
    assert est.n == 0
    assert est.total == 10
    assert est.rate == 0.0


def test_estimator_reset_changes_total_when_given():
    est = TimeEstimator(total=10)
    est.update(3)
    est.reset(total=50)
    assert est.n == 0
    assert est.total == 50


# ---------------------------------------------------------------------------
# TimeEstimator.render
# ---------------------------------------------------------------------------

def test_estimator_render_keys():
    est = TimeEstimator(total=4)
    est.update(1)
    rendered = est.render()
    for key in ("n", "total", "percent", "elapsed", "elapsed_str",
                "eta", "eta_str", "rate", "remaining"):
        assert key in rendered
    assert rendered["n"] == 1
    assert rendered["total"] == 4
    assert rendered["remaining"] == 3
    assert isinstance(rendered["elapsed_str"], str)
    assert isinstance(rendered["eta_str"], str)


# ---------------------------------------------------------------------------
# _render_tqdm_line (requires chainlit for the import chain)
# ---------------------------------------------------------------------------

pytestmark_render = pytest.mark.skipif(
    not chainlit_available, reason="chainlit not installed"
)


@pytestmark_render
def test_render_tqdm_line_en_includes_counter_and_percent():
    set_lang("en")
    from falkordb_harness.chainlit_progress import _render_tqdm_line

    est = TimeEstimator(total=40)
    est.update(12)
    line = _render_tqdm_line("LLM entity extraction", est)
    assert "LLM entity extraction" in line
    assert "12/40" in line
    assert "30%" in line
    assert "elapsed" in line
    assert "ETA" in line
    assert "it/s" in line


@pytestmark_render
def test_render_tqdm_line_de_uses_german_label():
    set_lang("de")
    from falkordb_harness.chainlit_progress import _render_tqdm_line

    est = TimeEstimator(total=10)
    est.update(2)
    line = _render_tqdm_line("LLM-Entitäten-Extraktion", est)
    assert "verstrichen" in line
    assert "2/10" in line
    assert "20%" in line


@pytestmark_render
def test_render_tqdm_line_unknown_rate_when_no_ticks():
    set_lang("en")
    from falkordb_harness.chainlit_progress import _render_tqdm_line

    est = TimeEstimator(total=40)
    line = _render_tqdm_line("Extract", est)
    assert "? it/s" in line
    assert "ETA ?" in line
    assert "0/40" in line


@pytestmark_render
def test_render_tqdm_line_zero_total_shows_unknown_percent():
    set_lang("en")
    from falkordb_harness.chainlit_progress import _render_tqdm_line

    est = TimeEstimator(total=0)
    est.update(5)
    line = _render_tqdm_line("Extract", est)
    assert "5" in line
    assert "?" in line


@pytestmark_render
def test_render_tqdm_line_known_rate_after_tick():
    set_lang("en")
    from falkordb_harness.chainlit_progress import _render_tqdm_line

    est = TimeEstimator(total=10)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        t0[0] = 2.0
        est.update(4)  # 4 items in 2s -> rate 2 it/s
    line = _render_tqdm_line("Write", est)
    assert "it/s" in line
    assert "? it/s" not in line


# ---------------------------------------------------------------------------
# make_ingestion_progress — progress event discriminant (integration-style)
# ---------------------------------------------------------------------------
#
# We mock ``TaskList`` / ``Task`` so we can drive the async ``progress``
# callback without a live Chainlit contextvar and assert the stage task's
# title gets rewritten with the tqdm line. The factory still calls
# ``TaskList().send()`` which we stub to a no-op. Skipped entirely when
# chainlit isn't importable (the repo's test posture is "all mocked", but
# the module-level import of ``chainlit.element`` in ``chainlit_progress``
# still requires the package present).

pytestmark_chainlit = pytest.mark.skipif(
    not chainlit_available, reason="chainlit not installed"
)


@pytest.fixture
def _stub_tasklist(monkeypatch):
    """Replace chainlit.element.TaskList / Task with in-memory fakes."""
    import chainlit.element as element

    class FakeTask:
        def __init__(self, title="", status=None):
            self.title = title
            self.status = status

    class FakeTaskList:
        def __init__(self):
            self.status = None
            self.tasks = []

        async def send(self):
            return None

        async def add_task(self, task):
            self.tasks.append(task)

        async def update(self):
            return None

    monkeypatch.setattr(element, "TaskList", FakeTaskList)
    monkeypatch.setattr(element, "Task", FakeTask)
    # Also patch the symbols already imported into chainlit_progress.
    import falkordb_harness.chainlit_progress as cp

    monkeypatch.setattr(cp, "TaskList", FakeTaskList, raising=False)
    monkeypatch.setattr(cp, "Task", FakeTask, raising=False)
    return FakeTaskList, FakeTask


def _drive_progress(events):
    """Drive the shared progress factory through a list of (label, details)."""
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    tasklist, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        for label, details in events:
            await progress(label, details)

    asyncio.run(_run())
    return tasklist


@pytestmark_chainlit
def test_progress_event_creates_stage_task_and_renders_tqdm_line(_stub_tasklist):
    set_lang("en")
    FakeTaskList, _ = _stub_tasklist  # type: ignore[misc]
    # stage_start first so the task exists with the plain base title
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 40}),
        ("Extracting 10/40…", {"kind": "progress", "stage": "extract",
                                "completed": 10, "total": 40}),
        ("Extracting 20/40…", {"kind": "progress", "stage": "extract",
                                "completed": 20, "total": 40}),
    ]
    tasklist = _drive_progress(events)
    assert len(tasklist.tasks) == 1
    task = tasklist.tasks[0]
    assert "20/40" in task.title
    assert "50%" in task.title
    assert "elapsed" in task.title
    assert "ETA" in task.title


@pytestmark_chainlit
def test_progress_event_creates_stage_task_if_stage_start_missing(_stub_tasklist):
    """A ``progress`` event for an unseen stage still produces a task row."""
    set_lang("en")
    events = [
        ("Extracting 1/4…", {"kind": "progress", "stage": "extract",
                              "completed": 1, "total": 4}),
    ]
    tasklist = _drive_progress(events)
    assert len(tasklist.tasks) == 1
    assert "1/4" in tasklist.tasks[0].title


@pytestmark_chainlit
def test_stage_end_restores_base_title(_stub_tasklist):
    set_lang("en")
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 4}),
        ("1/4…", {"kind": "progress", "stage": "extract",
                   "completed": 4, "total": 4}),
        ("Done", {"kind": "stage_end", "stage": "extract", "extractions": 4}),
    ]
    tasklist = _drive_progress(events)
    task = tasklist.tasks[0]
    # After stage_end the title should drop the tqdm decoration.
    assert "LLM entity extraction" in task.title
    assert "4/4" not in task.title
    assert "elapsed" not in task.title


@pytestmark_chainlit
def test_progress_event_unknown_total_shows_unknown_percent(_stub_tasklist):
    set_lang("en")
    events = [
        ("stage", {"kind": "stage_start", "stage": "extract", "total": 0}),
        ("1/?", {"kind": "progress", "stage": "extract",
                  "completed": 1, "total": 0}),
    ]
    tasklist = _drive_progress(events)
    # total=0 -> counter is bare "1", percent is "?"
    title = tasklist.tasks[0].title
    assert "1" in title
    assert "?" in title


@pytestmark_chainlit
def test_finalize_marks_running_tasks_done(_stub_tasklist):
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    tasklist, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        await finalize(True)

    asyncio.run(_run())
    # All stage tasks should be DONE after a successful finalize.
    from chainlit.element import TaskStatus

    for task in tasklist.tasks:
        assert task.status == TaskStatus.DONE
    assert tasklist.status == "Done"


@pytestmark_chainlit
def test_finalize_failed_marks_running_tasks_failed(_stub_tasklist):
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    tasklist, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        await finalize(False)

    asyncio.run(_run())
    from chainlit.element import TaskStatus

    for task in tasklist.tasks:
        assert task.status == TaskStatus.FAILED
    assert tasklist.status == "Failed"


@pytestmark_chainlit
def test_finalize_swallows_update_exception(_stub_tasklist, monkeypatch):
    """``finalize`` must not strand the panel if ``tasklist.update`` raises.

    The cleanup runs from the ingestion caller's ``finally`` block, which
    may execute while the on-message task is being cancelled by the
    Chainlit stop button. A second ``CancelledError`` (or any other
    exception from a dead socket) during the final ``tasklist.update()``
    must be swallowed so the panel state is still flipped to a terminal
    status in-memory.
    """
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    tasklist, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _boom():
        raise RuntimeError("socket gone")

    monkeypatch.setattr(tasklist, "update", _boom)

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        await finalize(True)

    # Should not raise despite the failing update.
    asyncio.run(_run())
    from chainlit.element import TaskStatus

    # In-memory state still flipped to terminal.
    assert tasklist.status == "Done"
    for task in tasklist.tasks:
        assert task.status == TaskStatus.DONE


def test_ingestion_finalize_called_on_cancellation():
    """The ingest-tools ``try/finally`` must call ``finalize`` on CancelledError.

    ``CancelledError`` inherits from ``BaseException`` (Python 3.8+), so a
    plain ``except Exception`` would skip cleanup and leave the TaskList
    pinned at "Running" when the user hits the Chainlit stop button. This
    test exercises the structural pattern used by ``_extract_and_write_impl``
    (``try/finally`` with a ``success`` flag) to confirm finalize fires.
    """
    finalized = []

    async def fake_run_ingestion(*args, **kwargs):
        raise asyncio.CancelledError()

    async def finalize(success):
        finalized.append(success)

    async def _extract_and_write_impl_shape():
        success = False
        try:
            await fake_run_ingestion()
            success = True
            return "ok"
        finally:
            if finalize is not None:
                await finalize(success)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_extract_and_write_impl_shape())
    assert finalized == [False]


# ---------------------------------------------------------------------------
# extract_from_chunks on_progress callback
# ---------------------------------------------------------------------------

def test_extract_from_chunks_invokes_progress_callback():
    """The new on_progress callback should fire once per chunk completion."""
    from knowledge.llm_extract import extract_from_chunks

    calls = []

    async def on_progress(completed, total):
        calls.append((completed, total))

    chunks = [
        {"source": "a.md", "chunk_index": i, "text": "x"} for i in range(3)
    ]

    async def _run():
        with patch("knowledge.llm_extract.extract_from_chunk", new=AsyncMock(return_value=None)):
            await extract_from_chunks(chunks, on_progress=on_progress)

    asyncio.run(_run())
    # 3 ticks, in monotonically increasing completed counts, total=3
    assert len(calls) == 3
    completeds = [c for c, _ in calls]
    assert completeds == sorted(completeds)
    assert all(total == 3 for _, total in calls)


def test_extract_from_chunks_callback_exception_swallowed():
    """A raising callback must not break extraction."""
    from knowledge.llm_extract import extract_from_chunks

    async def bad_callback(completed, total):
        raise RuntimeError("boom")

    chunks = [{"source": "a.md", "chunk_index": 0, "text": "x"}]

    async def _run():
        with patch("knowledge.llm_extract.extract_from_chunk", new=AsyncMock(return_value=None)):
            result = await extract_from_chunks(chunks, on_progress=bad_callback)
            return result

    result = asyncio.run(_run())
    # No extraction produced (mock returns None) but the call didn't raise.
    assert result == []


def test_extract_from_chunks_no_callback_keeps_old_behaviour():
    """Omitting on_progress must behave exactly as before."""
    from knowledge.llm_extract import extract_from_chunks

    chunks = [{"source": "a.md", "chunk_index": 0, "text": "x"}]

    async def _run():
        with patch("knowledge.llm_extract.extract_from_chunk", new=AsyncMock(return_value=None)):
            return await extract_from_chunks(chunks)

    result = asyncio.run(_run())
    assert result == []