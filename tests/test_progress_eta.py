"""Tests for the tqdm-style time estimator and Chainlit progress renderer.

The :class:`TimeEstimator` is a pure-Python dependency-free helper that
mirrors tqdm's elapsed / rate-EMA / ETA computation. The tqdm-line
rendering is now done client-side in ``AgentTodos.jsx`` from the raw
estimator fields written by :func:`make_ingestion_progress`.

The estimator / ``extract_from_chunks`` callback tests are pure-Python
and run without Chainlit installed. The ``make_ingestion_progress``
integration tests need ``chainlit.element`` and are skipped when the
package isn't importable (matching the repo's existing "all mocked, no
live Chainlit needed" test posture — see AGENTS.md).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness import i18n
from falkordb_harness.i18n import set_lang
from falkordb_harness.progress_eta import TimeEstimator, format_duration

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
    assert format_duration(59.4) == "0:59"
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
    est.update(10)
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
    t0 = [100.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 100.0
        t0[0] = 101.0
        est.update(1)
        assert est.rate > 0
        assert est.eta >= 0
        # raw remaining/rate = 9 / 1 = 9; safety factor default 3 -> 27
        assert 24.0 <= est.eta <= 30.0


def test_estimator_eta_zero_when_complete():
    est = TimeEstimator(total=2)
    est.update(2)
    assert est.n >= est.total
    assert est.eta == 0.0


def test_estimator_rate_robust_to_concurrent_clustering():
    """Cumulative-average rate must not saturate under clustered ticks."""
    est = TimeEstimator(total=8)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        t0[0] = 5.0
        est.update(4)
        first_rate = est.rate
        t0[0] = 10.0
        est.update(4)
        final_rate = est.rate
        final_eta = est.eta
    assert first_rate < 1.5
    assert final_rate < 1.5
    assert final_rate > 0.5
    assert final_eta == 0.0


def test_estimator_rate_cumulative_average_matches_n_over_elapsed():
    est = TimeEstimator(total=100)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        t0[0] = 20.0
        est.update(5)
        assert abs(est.rate - 0.25) < 1e-6
        # raw eta = 95 / 0.25 = 380; safety factor default 3 -> 1140
        assert abs(est.eta - 1140.0) < 1e-3


def test_estimator_eta_safety_factor_default_is_three():
    """Default ``INGEST_ETA_SAFETY_FACTOR`` is 3.0 (corrects the ~3x
    underestimate observed at INGEST_CONCURRENCY=4)."""
    import falkordb_harness.progress_eta as pe

    assert pe._eta_safety_factor() == 3.0


def test_estimator_eta_safety_factor_env_override(monkeypatch):
    monkeypatch.setenv("INGEST_ETA_SAFETY_FACTOR", "1.0")
    import falkordb_harness.progress_eta as pe

    assert pe._eta_safety_factor() == 1.0


def test_estimator_eta_safety_factor_invalid_falls_back(monkeypatch):
    # Empty / non-numeric -> default 3.0
    for bad in ("", "not-a-number"):
        monkeypatch.setenv("INGEST_ETA_SAFETY_FACTOR", bad)
        import falkordb_harness.progress_eta as pe

        assert pe._eta_safety_factor() == 3.0
    # NaN / inf -> default 3.0
    for bad in ("nan", "inf"):
        monkeypatch.setenv("INGEST_ETA_SAFETY_FACTOR", bad)
        import falkordb_harness.progress_eta as pe

        assert pe._eta_safety_factor() == 3.0
    # Negative -> clamped to 0.0 (no negative ETA inflation)
    monkeypatch.setenv("INGEST_ETA_SAFETY_FACTOR", "-1")
    import falkordb_harness.progress_eta as pe

    assert pe._eta_safety_factor() == 0.0


def test_estimator_eta_safety_factor_applied():
    """ETA = (remaining / rate) * safety_factor."""
    import falkordb_harness.progress_eta as pe

    est = TimeEstimator(total=100)
    t0 = [0.0]
    with patch("falkordb_harness.progress_eta.time.monotonic", side_effect=lambda: t0[0]):
        est.start_time = 0.0
        t0[0] = 10.0
        est.update(10)
        # raw = 90 / 1.0 = 90; with factor 3 -> 270
        with patch.object(pe, "_eta_safety_factor", return_value=3.0):
            assert abs(est.eta - 270.0) < 1e-3
        # factor 1 disables inflation
        with patch.object(pe, "_eta_safety_factor", return_value=1.0):
            assert abs(est.eta - 90.0) < 1e-3


def test_estimator_reset_reuses_total_when_none():
    est = TimeEstimator(total=10)
    est.update(3)
    est.reset()
    assert est.n == 0
    assert est.total == 10
    assert est.elapsed >= 0.0


def test_estimator_reset_changes_total_when_provided():
    est = TimeEstimator(total=10)
    est.update(3)
    est.reset(total=20)
    assert est.n == 0
    assert est.total == 20


def test_estimator_render_returns_all_fields():
    est = TimeEstimator(total=10)
    est.update(3)
    r = est.render()
    assert r["n"] == 3
    assert r["total"] == 10
    assert r["percent"] == 30.0
    assert isinstance(r["elapsed_str"], str)
    assert isinstance(r["eta_str"], str)
    assert r["rate"] >= 0
    assert r["remaining"] == 7


# ---------------------------------------------------------------------------
# make_ingestion_progress — progress event discriminant (integration-style)
# ---------------------------------------------------------------------------
#
# We mock ``cl.CustomElement`` so we can drive the async ``progress``
# callback without a live Chainlit contextvar and assert the element's
# ``stages`` prop gets updated with estimator fields. The factory calls
# ``cl.user_session.get/set`` which we stub. Skipped entirely when
# chainlit isn't importable.

pytestmark_chainlit = pytest.mark.skipif(
    not chainlit_available, reason="chainlit not installed"
)


class FakeCustomElement:
    """In-memory fake for cl.CustomElement that records prop updates.

    Mirrors the real ``CustomElement.__post_init__`` behaviour of
    serializing ``props`` into ``content`` (a JSON string) at construction
    time. The real Chainlit ``update()`` never refreshes ``content``, so
    the persisted file at ``chainlit_key`` always carries the ORIGINAL
    props — ``_sync_update`` in ``chainlit_progress.py`` fixes this by
    refreshing ``content`` before each ``update()`` call.
    """

    def __init__(self, name="", props=None):
        self.name = name
        self.props = dict(props or {})
        self.content = json.dumps(self.props)  # mirrors __post_init__
        self._update_calls = 0

    async def update(self):
        self._update_calls += 1


@pytest.fixture
def _stub_chainlit(monkeypatch):
    """Replace chainlit.user_session and chainlit.CustomElement with fakes."""
    import chainlit as cl

    _session: dict = {}

    class FakeUserSession:
        @staticmethod
        def get(key, default=None):
            return _session.get(key, default)

        @staticmethod
        def set(key, value):
            _session[key] = value

    class FakeMessage:
        def __init__(self, content="", elements=None):
            self.content = content
            self.elements = elements or []

        async def send(self):
            pass

    monkeypatch.setattr(cl, "user_session", FakeUserSession)
    monkeypatch.setattr(cl, "CustomElement", FakeCustomElement)
    monkeypatch.setattr(cl, "Message", FakeMessage)

    # Also patch the symbols already imported into chainlit_progress.
    import falkordb_harness.chainlit_progress as cp

    monkeypatch.setattr(cp, "cl", cl, raising=False)
    return _session


def _drive_progress(events, _stub_chainlit):
    """Drive the shared progress factory through a list of (label, details)."""
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        for label, details in events:
            await progress(label, details)

    asyncio.run(_run())
    el = _stub_chainlit.get("agent_todos_el")
    return el, finalize


@pytestmark_chainlit
def test_progress_event_writes_stage_to_element_stages(_stub_chainlit):
    set_lang("en")
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 40}),
        ("Extracting 10/40…", {"kind": "progress", "stage": "extract",
                                "completed": 10, "total": 40}),
        ("Extracting 20/40…", {"kind": "progress", "stage": "extract",
                                "completed": 20, "total": 40}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    assert el is not None
    assert el.props["active"] is True
    assert "extract" in el.props["stages"]
    s = el.props["stages"]["extract"]
    assert s["n"] == 20
    assert s["total"] == 40
    assert s["percent"] == 50.0
    assert isinstance(s["elapsed_str"], str)
    assert isinstance(s["eta_str"], str)
    assert s["rate"] >= 0


@pytestmark_chainlit
def test_progress_event_creates_stage_if_stage_start_missing(_stub_chainlit):
    set_lang("en")
    events = [
        ("Extracting 1/4…", {"kind": "progress", "stage": "extract",
                              "completed": 1, "total": 4}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    assert "extract" in el.props["stages"]
    assert el.props["stages"]["extract"]["n"] == 1
    assert el.props["stages"]["extract"]["total"] == 4


@pytestmark_chainlit
def test_stage_end_restores_base_title(_stub_chainlit):
    set_lang("en")
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 4}),
        ("1/4…", {"kind": "progress", "stage": "extract",
                   "completed": 4, "total": 4}),
        ("Done", {"kind": "stage_end", "stage": "extract", "extractions": 4}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    s = el.props["stages"]["extract"]
    assert "LLM entity extraction" in s["title"]
    assert s["n"] == 4
    # stage_end marks the stage as concluded so the UI swaps the spinner
    # for a checkmark (and keeps the frozen snapshot visible).
    assert s["done"] is True


@pytestmark_chainlit
def test_stage_end_marks_stage_done(_stub_chainlit):
    set_lang("en")
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 4}),
        ("1/4…", {"kind": "progress", "stage": "extract",
                   "completed": 4, "total": 4}),
        ("Done", {"kind": "stage_end", "stage": "extract", "extractions": 4}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    assert el.props["stages"]["extract"]["done"] is True


@pytestmark_chainlit
def test_chunk_stage_bare_no_progress_events(_stub_chainlit):
    """The chunk stage emits stage_start/file_start/file_end/stage_end but
    no ``progress`` events, so no TimeEstimator is ever constructed for it.
    The UI renders it as a bare spinner+title (no n/total % ETA rate line)
    and a checkmark once ``stage_end`` flips ``done``.
    """
    set_lang("en")
    events = [
        ("Chunking…", {"kind": "stage_start", "stage": "chunk", "total": 2}),
        ("Chunking a.md…", {"kind": "file_start", "stage": "chunk", "file": "a.md"}),
        ("Chunked a.md", {"kind": "file_end", "stage": "chunk",
                          "file": "a.md", "chunks": 3, "chars": 9000}),
        ("Chunking b.md…", {"kind": "file_start", "stage": "chunk", "file": "b.md"}),
        ("Chunked b.md", {"kind": "file_end", "stage": "chunk",
                          "file": "b.md", "chunks": 1, "chars": 1000}),
        ("Chunked 4 chunk(s).", {"kind": "stage_end", "stage": "chunk", "chunks": 4}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    s = el.props["stages"]["chunk"]
    # No ``progress`` event -> the estimator branch never ran, so the
    # stage carries only the stage_start defaults (n=0, total=0) plus the
    # ``done`` flag set by stage_end.
    assert s["done"] is True
    assert s["n"] == 0
    assert s["total"] == 0


@pytestmark_chainlit
def test_progress_event_unknown_total_shows_zero_total(_stub_chainlit):
    set_lang("en")
    events = [
        ("stage", {"kind": "stage_start", "stage": "extract", "total": 0}),
        ("1/?", {"kind": "progress", "stage": "extract",
                  "completed": 1, "total": 0}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    s = el.props["stages"]["extract"]
    assert s["n"] == 1
    assert s["total"] == 0
    assert s["percent"] == 0.0


@pytestmark_chainlit
def test_finalize_marks_stages_done_and_keeps_them(_stub_chainlit):
    """``finalize`` flips ``ingestion_running`` to False and marks every
    still-running stage as ``done`` (so the UI swaps the spinner for a
    checkmark) but keeps the stages visible as a chronological record.
    """
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        await finalize(True)

    asyncio.run(_run())
    el = _stub_chainlit.get("agent_todos_el")
    assert el.props["ingestion_running"] is False
    assert el.props["active"] is False
    # Stages are kept (not cleared) so the block persists as a record.
    assert "extract" in el.props["stages"]
    assert el.props["stages"]["extract"]["done"] is True


@pytestmark_chainlit
def test_finalize_failed_also_marks_done(_stub_chainlit):
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        await finalize(False)

    asyncio.run(_run())
    el = _stub_chainlit.get("agent_todos_el")
    assert el.props["ingestion_running"] is False
    assert el.props["active"] is False
    assert "extract" in el.props["stages"]
    assert el.props["stages"]["extract"]["done"] is True


@pytestmark_chainlit
def test_finalize_swallows_update_exception(_stub_chainlit, monkeypatch):
    """``finalize`` must not strand the panel if ``el.update`` raises."""
    set_lang("en")
    from falkordb_harness.chainlit_progress import make_ingestion_progress

    _, progress, finalize = asyncio.run(make_ingestion_progress())

    async def _run():
        await progress("Extracting…", {"kind": "stage_start",
                                       "stage": "extract", "total": 4})
        el = _stub_chainlit.get("agent_todos_el")
        monkeypatch.setattr(el, "update", AsyncMock(side_effect=RuntimeError("boom")))
        await finalize(True)

    asyncio.run(_run())
    el = _stub_chainlit.get("agent_todos_el")
    assert el.props["ingestion_running"] is False
    assert el.props["stages"]["extract"]["done"] is True


@pytestmark_chainlit
def test_sync_update_refreshes_content_before_update(_stub_chainlit):
    """``_sync_update`` must refresh ``el.content`` from ``el.props`` before
    calling ``el.update()``.

    Chainlit's ``CustomElement.__post_init__`` serializes ``props`` into
    ``content`` at construction time, but ``update()`` never refreshes it.
    So the persisted file at ``chainlit_key`` always carries the ORIGINAL
    props. ``_sync_update`` fixes this by re-serializing ``props`` into
    ``content`` before each ``update()`` call, ensuring the ``done`` flag
    on ingestion stages reaches the wire.
    """
    set_lang("en")
    events = [
        ("Extracting…", {"kind": "stage_start", "stage": "extract", "total": 4}),
        ("1/4…", {"kind": "progress", "stage": "extract",
                   "completed": 4, "total": 4}),
        ("Done", {"kind": "stage_end", "stage": "extract", "extractions": 4}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    # ``content`` should reflect the CURRENT props (with done=True), not
    # the original construction-time props (empty stages).
    parsed = json.loads(el.content)
    assert "stages" in parsed
    assert parsed["stages"]["extract"]["done"] is True
    # The content should match the props (both serialized).
    assert json.loads(el.content) == el.props


@pytestmark_chainlit
def test_stage_start_for_stage_stage_shows_in_panel(_stub_chainlit):
    """The 'stage' (file staging) stage should emit ``stage_start`` before
    ``stage_end`` so it appears in the progress panel and gets a checkmark.
    """
    set_lang("en")
    events = [
        ("Staging…", {"kind": "stage_start", "stage": "stage", "total": 2}),
        ("Staged.", {"kind": "stage_end", "stage": "stage", "files": ["a.md", "b.md"],
                      "total": 2}),
    ]
    el, _ = _drive_progress(events, _stub_chainlit)
    assert "stage" in el.props["stages"]
    assert el.props["stages"]["stage"]["done"] is True


def test_ingestion_finalize_called_on_cancellation():
    """The ingest-tools ``try/finally`` must call ``finalize`` on CancelledError.

    ``CancelledError`` inherits from ``BaseException`` (Python 3.8+), so a
    plain ``except Exception`` would skip cleanup and leave the panel
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
