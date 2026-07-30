"""Agent tools that delegate to interactive UI prompts.

``request_ingestion_confirmation`` replaces the agent's free-text "ask the
user to confirm" step in the PRE-INGESTION REVIEW ROUTINE with an explicit
Confirm/Cancel action button. ``ask_user`` is a general clarifying-question
tool. Both route through :mod:`falkordb_harness.ui_prompts`, which uses a
Chainlit ``AskActionMessage`` / ``AskUserMessage`` in the UI and falls back
to stdin in the CLI.
"""

from __future__ import annotations

from langchain_core.tools import tool

from falkordb_harness.ui_prompts import prompt_confirm, prompt_question


@tool
async def request_ingestion_confirmation(files_summary: str) -> str:
    """Ask the user to confirm ingestion after the pre-ingestion review.

    Call this AFTER you have discovered, inspected, and summarized the
    candidate files (steps 1-4 of the PRE-INGESTION REVIEW ROUTINE) and
    BEFORE calling ``extract_and_write``. Present your summary — file
    name, type, size, a 1-3 sentence content description per file, and
    anything that looks like noise — as ``files_summary``. The user will
    be shown a Confirm/Cancel prompt; their choice is returned as the
    tool result so you know whether to proceed.
    """
    return await prompt_confirm(files_summary)


@tool
async def ask_user(question: str) -> str:
    """Ask the user a clarifying question and wait for their answer.

    Use this when you need information from the user to proceed — e.g.
    which file to focus on, which entity a ambiguous name refers to, or
    which of several options they prefer. Do NOT use this for the
    pre-ingestion confirmation; use ``request_ingestion_confirmation``
    for that.
    """
    return await prompt_question(question)