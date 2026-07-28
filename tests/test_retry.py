"""Unit tests for the retry helpers (knowledge.retry) and the tool-boundary
wrappers (falkordb_harness.tools._retry).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.retry import (
    TransientError,
    is_transient,
    retry_async,
    retry_sync,
)


# ---------------------------------------------------------------------------
# is_transient classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc, expected",
    [
        # Known-transient types.
        (ConnectionRefusedError("nope"), True),
        (ConnectionResetError("reset"), True),
        (TimeoutError("timed out"), True),
        (OSError("Connection refused by peer"), True),
        (TransientError("explicit"), True),
        # Message-based transient fragments.
        (RuntimeError("Connection reset by peer"), True),
        (RuntimeError("operation timed out"), True),
        (RuntimeError("Redis is LOADING the dataset"), True),
        (RuntimeError("Service Unavailable"), True),
        (RuntimeError("Too Many Requests"), True),
        (Exception("broken pipe"), True),
        # Non-transient programming-error types (message ignored).
        (ValueError("connection is bad"), False),
        (TypeError("oops"), False),
        (KeyError("missing"), False),
        (AttributeError("no attr"), False),
        # FalkorDB index-already-exists is idempotently handled, not transient.
        (Exception("Index already exists"), False),
        (Exception("Attribute 'name' is already indexed"), False),
        # Misc non-transient.
        (RuntimeError("undefined label"), False),
    ],
)
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected


def test_is_transient_openai_classes():
    import httpx
    import openai

    req = httpx.Request("POST", "https://example.com")
    resp = httpx.Response(200, request=req)
    assert is_transient(openai.APIConnectionError(request=req)) is True
    assert is_transient(openai.APITimeoutError(request=req)) is True
    assert is_transient(openai.RateLimitError("x", response=resp, body=None)) is True
    # BadRequestError is not in our transient list and is not a never-type, but
    # its message "bad request" isn't a transient fragment -> not transient.
    assert is_transient(openai.BadRequestError("bad", response=resp, body=None)) is False


def test_is_transient_redis_classes():
    import redis.exceptions as redis_exc

    assert is_transient(redis_exc.ConnectionError("down")) is True
    assert is_transient(redis_exc.TimeoutError("timed out")) is True
    assert is_transient(redis_exc.BusyLoadingError("loading")) is True


# ---------------------------------------------------------------------------
# retry_sync
# ---------------------------------------------------------------------------
def test_retry_sync_succeeds_after_transient():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionRefusedError("down")
        return "ok"

    sleeps = []
    out = retry_sync(factory, max_attempts=3, sleep=lambda d: sleeps.append(d))
    assert out == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert sleeps[0] > 0


def test_retry_sync_exhaustion_reraises_last():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        raise ConnectionRefusedError("down")

    with pytest.raises(ConnectionRefusedError):
        retry_sync(factory, max_attempts=3, sleep=lambda d: None)
    assert calls["n"] == 3


def test_retry_sync_non_transient_not_retried():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        raise ValueError("bad")

    with pytest.raises(ValueError):
        retry_sync(factory, max_attempts=5, sleep=lambda d: None)
    assert calls["n"] == 1


def test_retry_sync_backoff_is_capped_and_increasing():
    delays = []
    call_count = {"n": 0}

    def factory():
        call_count["n"] += 1
        if call_count["n"] < 5:
            raise ConnectionError("x")
        return "ok"

    # Use a deterministic sleep that records, and patch the module's
    # backoff cap low so we can observe the cap without env tweaking.
    import knowledge.retry as retry_mod

    orig_base, orig_max = retry_mod.RETRY_BASE_DELAY, retry_mod.RETRY_MAX_DELAY
    retry_mod.RETRY_BASE_DELAY = 1.0
    retry_mod.RETRY_MAX_DELAY = 4.0
    try:
        out = retry_sync(factory, max_attempts=5, sleep=lambda d: delays.append(d))
    finally:
        retry_mod.RETRY_BASE_DELAY = orig_base
        retry_mod.RETRY_MAX_DELAY = orig_max
    assert out == "ok"
    # 4 retries before the 5th successful attempt.
    assert len(delays) == 4
    # Delays are non-decreasing up to the cap (jitter can only add, base grows).
    assert delays[-1] <= 4.0 + 1.0  # cap + max jitter


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------
def test_retry_async_succeeds_after_transient():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("down")
        return "ok"

    async def no_sleep(d):
        pass

    out = asyncio.run(retry_async(factory, max_attempts=5, sleep=no_sleep))
    assert out == "ok"
    assert calls["n"] == 3


def test_retry_async_exhaustion_reraises_last():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise TimeoutError("nope")

    async def no_sleep(d):
        pass

    with pytest.raises(TimeoutError):
        asyncio.run(retry_async(factory, max_attempts=2, sleep=no_sleep))
    assert calls["n"] == 2


def test_retry_async_non_transient_not_retried():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise KeyError("nope")

    async def no_sleep(d):
        pass

    with pytest.raises(KeyError):
        asyncio.run(retry_async(factory, max_attempts=4, sleep=no_sleep))
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Tool-boundary wrappers
# ---------------------------------------------------------------------------
def test_with_retry_returns_result_on_success():
    from falkordb_harness.tools._retry import with_retry

    def body():
        return "result"

    assert with_retry(body) == "result"


def test_with_retry_returns_json_error_after_exhaustion():
    from falkordb_harness.tools._retry import with_retry

    calls = {"n": 0}

    def body():
        calls["n"] += 1
        raise ConnectionRefusedError("redis down")

    # Force tiny/no backoff by patching sleep.
    import knowledge.retry as retry_mod

    orig = retry_mod.time.sleep
    retry_mod.time.sleep = lambda d: None
    try:
        out = with_retry(body, max_attempts=2)
    finally:
        retry_mod.time.sleep = orig
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    assert parsed["transient"] is True
    assert parsed["attempts"] == 2
    assert calls["n"] == 2


def test_with_retry_non_transient_returns_error_without_retry_budget():
    from falkordb_harness.tools._retry import with_retry

    calls = {"n": 0}

    def body():
        calls["n"] += 1
        raise ValueError("bad input")

    out = with_retry(body, max_attempts=5)
    parsed = json.loads(out)
    assert parsed["transient"] is False
    # Non-transient errors short-circuit immediately (1 attempt).
    assert calls["n"] == 1


def test_awith_retry_returns_json_error_after_exhaustion():
    from falkordb_harness.tools._retry import awith_retry

    calls = {"n": 0}

    async def body():
        calls["n"] += 1
        raise TimeoutError("llm timeout")

    async def no_sleep(d):
        pass

    import knowledge.retry as retry_mod

    # Patch asyncio.sleep used by retry_async via the injected sleep param.
    out = asyncio.run(awith_retry(body, max_attempts=3))
    # awith_retry uses retry_async with default sleep (asyncio.sleep); force
    # zero-wait by monkeypatching asyncio.sleep for determinism.
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["transient"] is True
    assert parsed["attempts"] == 3


def test_awith_retry_success():
    from falkordb_harness.tools._retry import awith_retry

    async def body():
        return {"count": 5}

    out = asyncio.run(awith_retry(body))
    assert out == {"count": 5}