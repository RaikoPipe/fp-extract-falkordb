"""Small shared text helpers used across the extraction pipeline."""

from __future__ import annotations


def strip_code_fence(text: str) -> str:
    """Remove a single surrounding markdown code fence, if present.

    Handles both ```` ```json ... ``` ```` and ```` ``` ... ``` ````. Returns
    the text unchanged when it does not start with a fence.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")
    lines = lines[1:]  # drop opening fence
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision and a Z suffix."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["strip_code_fence", "utc_now_iso"]