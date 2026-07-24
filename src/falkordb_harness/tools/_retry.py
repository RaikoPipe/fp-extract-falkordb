"""Tool-boundary retry helpers for the FalkorDB agent tools.

These wrap a tool's core work so that transient errors (a dropped FalkorDB
connection, an LLM provider hiccup, a brief rate-limit) are retried within the
tool call, and — when retries are exhausted — surfaced to the agent as a
clean JSON ``{"error": ...}`` string instead of an exception traceback. The
agent can then decide to retry, report to the user, or try a different tool,
rather than the run aborting on a single transient blip.

This is deliberately thin: it does not change tool signatures, docstrings, or
return shapes on success, and it has no awareness of LangChain's ``@tool``
decorator (callers apply the wrapping inside the decorated function body).
"""

from __future__ import annotations

import json
import traceback
from typing import Awaitable, Callable, TypeVar

from loguru import logger

from knowledge.retry import is_transient, retry_async, retry_sync

T = TypeVar("T")


def _error_payload(exc: BaseException, attempts: int) -> str:
    """Build the JSON error string returned to the agent after exhaustion."""
    return json.dumps(
        {
            "error": str(exc) or exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
            "transient": is_transient(exc),
            "attempts": attempts,
        },
        ensure_ascii=False,
        default=str,
    )


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int | None = None,
) -> T | str:
    """Run a sync tool body with transient-error retry.

    Returns the function's result on success. After exhausting retries on a
    transient error, returns a JSON ``{"error": ...}`` string (so the agent
    gets a structured, recoverable error rather than a stack trace). A
    non-transient error is reported the same way without burning retry budget.
    """
    from knowledge.retry import RETRY_MAX_ATTEMPTS

    attempts = max_attempts if max_attempts is not None else RETRY_MAX_ATTEMPTS
    try:
        return retry_sync(fn, max_attempts=attempts)
    except BaseException as exc:
        logger.error(
            "Tool call failed after {} attempt(s): {}\n{}",
            attempts,
            exc,
            traceback.format_exc(),
        )
        return _error_payload(exc, attempts)


async def awith_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
) -> T | str:
    """Run an async tool body with transient-error retry.

    Async counterpart of :func:`with_retry`. ``fn`` is invoked fresh each
    attempt so reconnect-on-retry logic in the backend runs on every retry.
    """
    from knowledge.retry import RETRY_MAX_ATTEMPTS

    attempts = max_attempts if max_attempts is not None else RETRY_MAX_ATTEMPTS
    try:
        return await retry_async(fn, max_attempts=attempts)
    except BaseException as exc:
        logger.error(
            "Tool call failed after {} attempt(s): {}\n{}",
            attempts,
            exc,
            traceback.format_exc(),
        )
        return _error_payload(exc, attempts)


__all__ = ["with_retry", "awith_retry"]