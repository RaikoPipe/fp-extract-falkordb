"""Reusable transient-error retry helpers.

This module centralizes the bespoke retry/backoff pattern first introduced
in ``knowledge.llm_extract.extract_from_chunk`` so it can be applied at the
agent-tool boundary (and the FalkorDB backend) without pulling in a new
dependency. The goal is to let transient errors — a dropped Redis/FalkorDB
connection, an LLM provider hiccup, a brief rate-limit — resolve themselves
within a tool call so the calling agent never sees them, while still
surfacing genuinely non-transient failures immediately.

Public surface:
    - :func:`is_transient`  — classify an exception as transient.
    - :func:`retry_async`    — retry an async callable with backoff + jitter.
    - :func:`retry_sync`     — retry a sync callable with backoff + jitter.
    - :exc:`TransientError`  — marker base for callers that want to opt in
      explicitly (raised exception types may subclass this to bypass the
      heuristic classifier).

Configuration (read once at import, override via env):
    - ``RETRY_MAX_ATTEMPTS`` (default ``4``)  — total attempts including the first.
    - ``RETRY_BASE_DELAY``   (default ``0.5``) seconds; first backoff = base_delay * 2**0.
    - ``RETRY_MAX_DELAY``    (default ``8.0``) seconds; backoff is capped at this.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Awaitable, Callable, TypeVar

from loguru import logger

T = TypeVar("T")


class TransientError(Exception):
    """Marker base class for exceptions that should always be retried.

    Callers may subclass this for their own known-transient failure modes to
    bypass the heuristic :func:`is_transient` classifier.
    """


# ---------------------------------------------------------------------------
# Configuration (env-driven, fixed at import time)
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


RETRY_MAX_ATTEMPTS = _env_int("RETRY_MAX_ATTEMPTS", 4)
RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY", 0.5)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY", 8.0)


# ---------------------------------------------------------------------------
# Transient-error classification
# ---------------------------------------------------------------------------
# Substrings in the lowercased exception message that indicate a transient
# failure (used as a fallback when the exception type isn't a known transient
# class, e.g. a bare ``RuntimeError("connection reset by peer")`` raised by a
# client library that wraps the real OS error).
_TRANSIENT_MESSAGE_FRAGMENTS: tuple[str, ...] = (
    "connection",
    "connection refused",
    "connection reset",
    "broken pipe",
    "timeout",
    "timed out",
    "busy",
    "loading",  # Redis BUSYLOAD
    "eof",
    "service unavailable",
    "bad gateway",
    "overloaded",
    "rate limit",
    "too many requests",
    "try again",
    "temporarily unavailable",
    "retry",
    "deadlocked",
)

# Exception types that are always transient (checked via isinstance, so
# subclasses are covered). Populated lazily/defensively to avoid import
# failures when an optional dependency is absent.
_ALWAYS_TRANSIENT_TYPES: tuple[type, ...] = (
    ConnectionError,
    TimeoutError,
    OSError,  # covers ConnectionRefusedError, ConnectionResetError, BrokenPipeError, etc.
    TransientError,
    asyncio.TimeoutError,
)


def _collect_optional_transient_types() -> tuple[type, ...]:
    """Build a tuple of transient exception types from optional deps.

    Imported defensively so this module has no hard dependency on redis,
    openai, or httpx. Missing packages simply contribute no types.
    """
    types: list[type] = []
    try:
        import redis.exceptions as _redis_exc

        types.extend(
            [
                _redis_exc.ConnectionError,
                _redis_exc.TimeoutError,
                _redis_exc.BusyLoadingError,
                _redis_exc.TryAgainError,
                _redis_exc.ReadOnlyError,
                _redis_exc.ClusterDownError,
                _redis_exc.MasterDownError,
            ]
        )
    except Exception:  # pragma: no cover - import-time guard
        pass

    try:
        import openai as _openai

        # List only the *specific* transient subclasses — NOT the broad
        # ``APIError`` base, since non-transient errors like ``BadRequestError``
        # also subclass it and would be misclassified as retryable.
        types.extend(
            [
                _openai.APIConnectionError,
                _openai.APITimeoutError,
                _openai.RateLimitError,
                _openai.InternalServerError,
            ]
        )
    except Exception:  # pragma: no cover - import-time guard
        pass

    try:
        import httpx

        types.extend(
            [
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
            ]
        )
    except Exception:  # pragma: no cover - import-time guard
        pass

    # Deduplicate while preserving order.
    seen: set[type] = set()
    unique: list[type] = []
    for t in types:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return tuple(unique)


_OPTIONAL_TRANSIENT_TYPES: tuple[type, ...] = _collect_optional_transient_types()


def is_transient(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a transient (retryable) failure.

    Classification is deliberately conservative: when in doubt we err on the
    side of *not* retrying, since retrying a non-transient error burns time
    and can mask a real bug. Specifically:

    - Known-transient exception *types* (ConnectionError, openai
      APIConnectionError, etc.) → True.
    - The FalkorDB "index already exists" error → **False** (it's already
      handled idempotently by the backend and retrying would just re-hit the
      same guard).
    - Otherwise, the exception message is matched against a curated list of
      transient fragments.
    - ``ValueError``, ``TypeError``, ``KeyError``, ``AttributeError``,
      ``SyntaxError`` → always False regardless of message, since these almost
      always indicate a programming bug rather than a transient fault.
    """
    # Explicit non-transient types take precedence over message sniffing so a
    # ``ValueError("connection is bad")`` from a misused API is not retried.
    _NEVER_TRANSIENT_TYPES = (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        SyntaxError,
        ArithmeticError,
        NotImplementedError,
        ImportError,
    )
    if isinstance(exc, _NEVER_TRANSIENT_TYPES):
        return False

    # FalkorDB index-already-exists is idempotently handled by the backend;
    # treat it as a successful no-op, not a retryable transient.
    msg = str(exc).lower()
    if "already exists" in msg or "already indexed" in msg:
        return False

    if isinstance(exc, _ALWAYS_TRANSIENT_TYPES):
        return True
    if _OPTIONAL_TRANSIENT_TYPES and isinstance(exc, _OPTIONAL_TRANSIENT_TYPES):
        return True

    return any(frag in msg for frag in _TRANSIENT_MESSAGE_FRAGMENTS)


# ---------------------------------------------------------------------------
# Backoff helpers
# ---------------------------------------------------------------------------
def _backoff_delay(attempt: int) -> float:
    """Exponential backoff for ``attempt`` (0-indexed), capped + jittered.

    Delay = min(base * 2**attempt, max_delay), plus uniform jitter in
    ``[0, base]`` so retry storms from many concurrent callers don't all fire
    on the same wall-clock tick.
    """
    base = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    jitter = random.uniform(0.0, RETRY_BASE_DELAY)
    return min(base + jitter, RETRY_MAX_DELAY)


# ---------------------------------------------------------------------------
# Public retry functions
# ---------------------------------------------------------------------------
async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    retry_on: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``factory()`` and retry on transient errors.

    ``factory`` is invoked fresh each attempt (so it must rebuild the awaitable
    — this lets reconnect-on-retry logic run on each attempt). Returns the
    first successful result, or re-raises the last exception after
    ``max_attempts`` total tries.

    ``sleep`` is injectable for tests; defaults to :func:`asyncio.sleep`.
    ``on_retry(attempt, exc, delay)`` is called (if provided) before each
    sleep so callers can observe/log retries.
    """
    _sleep = sleep or asyncio.sleep
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await factory()
        except BaseException as exc:
            last_exc = exc
            if not retry_on(exc) or attempt >= max_attempts - 1:
                raise
            delay = _backoff_delay(attempt)
            logger.warning(
                "Transient error (attempt {}/{}): {} — retrying in {:.2f}s",
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await _sleep(delay)
    # Unreachable: loop either returns or raises on the final attempt.
    assert last_exc is not None  # pragma: no cover
    raise last_exc  # pragma: no cover


def retry_sync(
    factory: Callable[[], T],
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    retry_on: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Synchronous counterpart of :func:`retry_async`.

    ``sleep`` defaults to :func:`time.sleep`.
    """
    _sleep = sleep or time.sleep
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return factory()
        except BaseException as exc:
            last_exc = exc
            if not retry_on(exc) or attempt >= max_attempts - 1:
                raise
            delay = _backoff_delay(attempt)
            logger.warning(
                "Transient error (attempt {}/{}): {} — retrying in {:.2f}s",
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            _sleep(delay)
    assert last_exc is not None  # pragma: no cover
    raise last_exc  # pragma: no cover


__all__ = [
    "TransientError",
    "is_transient",
    "retry_async",
    "retry_sync",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_DELAY",
]