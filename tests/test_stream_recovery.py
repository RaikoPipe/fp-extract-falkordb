"""Tests for in-flight stream recovery across UI reconnects.

Covers the pure registry logic in ``falkordb_harness.stream_recovery``:
register/update/deregister and the ``replay_inflight_stream`` no-op +
snapshot behaviour. The actual socket emit is exercised against a fake
emitter so the test doesn't need a running Chainlit server.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class _FakeMessage:
    """Minimal stand-in for ``cl.Message`` carrying the fields replay reads."""

    def __init__(self, content: str = "", thread_id: str = "t1", mid: str = "m1"):
        self.content = content
        self.thread_id = thread_id
        self.id = mid
        self.streaming = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "threadId": self.thread_id,
            "output": self.content,
            "streaming": self.streaming,
        }


class _FakeEmitter:
    def __init__(self) -> None:
        self.updated: list[dict] = []

    async def update_step(self, step_dict: dict) -> None:
        self.updated.append(step_dict)


class _FakeContext:
    def __init__(self, emitter: _FakeEmitter) -> None:
        self.emitter = emitter


def _install_fake_context(monkeypatch, emitter: _FakeEmitter) -> None:
    import chainlit as cl

    fake = _FakeContext(emitter)
    monkeypatch.setattr(cl, "context", fake)


# ---------------------------------------------------------------------------
# register / update / deregister
# ---------------------------------------------------------------------------
def test_register_returns_stream_with_message():
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        get_active_stream,
        register_stream,
    )

    msg = _FakeMessage()
    try:
        stream = register_stream("t1", msg)
        assert stream.message is msg
        assert get_active_stream("t1") is stream
    finally:
        deregister_stream("t1")


def test_deregister_removes_stream():
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        get_active_stream,
        register_stream,
    )

    register_stream("t1", _FakeMessage())
    deregister_stream("t1")
    assert get_active_stream("t1") is None


def test_deregister_idempotent_for_unregistered_thread():
    from falkordb_harness.stream_recovery import deregister_stream

    deregister_stream("never-registered")  # must not raise


def test_register_overwrites_prior_entry():
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        get_active_stream,
        register_stream,
    )

    register_stream("t1", _FakeMessage(mid="first"))
    register_stream("t1", _FakeMessage(mid="second"))  # supersedes
    try:
        stream = get_active_stream("t1")
        assert stream.message.id == "second"
    finally:
        deregister_stream("t1")


# ---------------------------------------------------------------------------
# replay_inflight_stream
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_replay_noop_when_no_active_stream():
    from falkordb_harness.stream_recovery import replay_inflight_stream

    # No register -> must complete without raising and without emits.
    await replay_inflight_stream("unregistered")


@pytest.mark.asyncio
async def test_replay_emits_accumulated_content(monkeypatch):
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        register_stream,
        replay_inflight_stream,
    )

    # message.content is the source of truth (kept in sync by stream_token
    # in production); the replay reads it directly.
    msg = _FakeMessage(content="Partial answer so far", thread_id="t1", mid="m1")
    register_stream("t1", msg)
    emitter = _FakeEmitter()
    _install_fake_context(monkeypatch, emitter)
    try:
        await replay_inflight_stream("t1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(emitter.updated) == 1
        emitted = emitter.updated[0]
        assert emitted["id"] == "m1"
        assert emitted["output"] == "Partial answer so far"
        assert emitted["streaming"] is True
    finally:
        deregister_stream("t1")


@pytest.mark.asyncio
async def test_replay_skips_when_content_empty(monkeypatch):
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        register_stream,
        replay_inflight_stream,
    )

    register_stream("t1", _FakeMessage(content="", thread_id="t1", mid="m1"))
    emitter = _FakeEmitter()
    _install_fake_context(monkeypatch, emitter)
    try:
        await replay_inflight_stream("t1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert emitter.updated == []  # nothing to replay yet
    finally:
        deregister_stream("t1")


@pytest.mark.asyncio
async def test_replay_noop_after_stream_concluded(monkeypatch):
    """If the background stream finished (deregistered) before the replay
    runs, there's nothing to replay — the persisted step carries the final
    text and a normal resume renders it."""
    from falkordb_harness.stream_recovery import (
        deregister_stream,
        register_stream,
        replay_inflight_stream,
    )

    register_stream("t1", _FakeMessage(content="done", thread_id="t1", mid="m1"))
    deregister_stream("t1")  # stream concluded before reconnect
    emitter = _FakeEmitter()
    _install_fake_context(monkeypatch, emitter)
    await replay_inflight_stream("t1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert emitter.updated == []


# ---------------------------------------------------------------------------
# wiring into chainlit_app
# ---------------------------------------------------------------------------
def test_chainlit_app_imports_recovery_api():
    """on_message / on_chat_resume must use the stream-recovery registry.

    Guards against an accidental removal of the register/deregister/replay
    calls that would silently reintroduce the lost-stream-on-reconnect bug.
    """
    import falkordb_harness.chainlit_app as app

    assert hasattr(app, "register_stream")
    assert hasattr(app, "deregister_stream")
    assert hasattr(app, "replay_inflight_stream")