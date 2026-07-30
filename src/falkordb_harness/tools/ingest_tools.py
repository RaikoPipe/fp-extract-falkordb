"""Tools for document ingestion into the knowledge graph."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from langchain_core.tools import tool

from falkordb_harness.tools._paths import resolve as _resolve
from falkordb_harness.tools._retry import awith_retry, with_retry

logger = logging.getLogger("falkordb_harness.tools.ingest")


def _resolve_data_dir(data_dir: str) -> Path | str:
    """Resolve the data_dir argument through the shared filesystem backend.

    An empty ``data_dir`` defaults to the ``preprocessed/`` tree. Otherwise
    the path is resolved under ``DATA_DIR`` (same containment as
    ``file_metadata`` / ``read_excerpt``), so the agent can pass
    ``preprocessed`` / ``originals`` / a subdirectory and get consistent
    resolution. Returns an error string on traversal failure.
    """
    target = data_dir or "preprocessed"
    resolved = _resolve(target)
    if isinstance(resolved, str):
        return resolved
    # ``preprocessed/`` is auto-created by _paths.preprocessed_dir(); ensure
    # arbitrary subdirs exist too so load_and_chunk doesn't fail on missing
    # dirs the agent referenced.
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


@tool
def chunk_documents(
    data_dir: str = "",
    chunk_size: int = 4000,
    overlap: int = 200,
) -> str:
    """Load and chunk documents from a directory without ingesting them.

    Use this to preview what documents and chunks would be produced
    before running the full extraction pipeline. Returns a summary
    with file count, chunk count, and a preview of the first chunk.

    ``data_dir`` defaults to the ``preprocessed/`` tree (under DATA_DIR) and
    is resolved through the same containment as ``file_metadata``; pass e.g.
    ``preprocessed`` or ``originals`` (or a subdirectory) rather than an
    absolute path.
    """
    return with_retry(lambda: _chunk_documents_impl(data_dir, chunk_size, overlap))


def _chunk_documents_impl(data_dir: str, chunk_size: int, overlap: int) -> str:
    from knowledge.chunking import load_and_chunk

    resolved = _resolve_data_dir(data_dir)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)
    chunks = load_and_chunk(resolved, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return "No documents found or no chunks produced."

    sources = sorted(set(c["source"] for c in chunks))
    preview = chunks[0]["text"][:300] + "..." if len(chunks[0]["text"]) > 300 else chunks[0]["text"]
    return json.dumps({
        "file_count": len(sources),
        "files": sources,
        "chunk_count": len(chunks),
        "first_chunk_preview": preview,
    }, indent=2, ensure_ascii=False)


@tool
async def extract_and_write(
    data_dir: str = "",
    chunk_size: int = 4000,
    concurrency: int = 4,
) -> str:
    """Ingest documents: chunk, extract entities via LLM, and write to FalkorDB.

    This runs the full pipeline. Provide data_dir as a path to a directory
    containing documents (.txt, .md, .pdf, .docx, .csv, .json, .html), under
    DATA_DIR (e.g. ``preprocessed`` or ``originals``); defaults to the
    ``preprocessed/`` tree. Returns a summary with statement count, node
    count, and conflicts detected.
    """
    return await awith_retry(
        lambda: _extract_and_write_impl(data_dir, chunk_size, concurrency)
    )


async def _extract_and_write_impl(
    data_dir: str, chunk_size: int, concurrency: int
) -> str:
    from falkordb_harness.ingest_runner import run_ingestion

    resolved = _resolve_data_dir(data_dir)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)
    # Discover every supported file under the resolved tree so the unified
    # ``run_ingestion`` pipeline (which stages → preprocess → chunk → extract
    # → write) operates on the same files the previous direct path would
    # have chunked via ``load_and_chunk``. Binary formats are routed through
    # docprep; plain-text files are ingested as-is.
    from knowledge.chunking import discover_files

    files = discover_files(resolved)
    if not files:
        return "No documents found or no chunks produced."

    # UI progress bridge: when invoked from the Chainlit UI, ``on_message``
    # installs a zero-arg async factory into ``cl.user_session`` that builds a
    # live ``cl.TaskList`` panel and returns ``(progress, finalize)``. The
    # tool consumes it so the agent-driven path gets the same per-stage /
    # per-file progress UI as the "Ingest documents" action button. In any
    # non-Chainlit runtime (CLI, tests) no factory is installed and
    # ``run_ingestion`` runs with ``progress=None`` silently.
    progress = None
    finalize = None
    try:
        import chainlit as cl

        factory = cl.user_session.get("ingest_progress_factory")
    except Exception:  # noqa: BLE001 — not in a Chainlit context
        factory = None
    if factory is not None:
        try:
            # ``make_ingestion_progress`` returns a 3-tuple
            # ``(tasklist, progress, finalize)``; the tasklist is sent as a
            # standalone chat element by the factory, so we only need the
            # progress/finalize callbacks here.
            _tasklist, progress, finalize = await factory()
        except Exception as exc:  # noqa: BLE001 — never strand ingestion
            logger.warning("ingest progress factory failed: %s", exc)
            progress, finalize = None, None

    result: dict = {}
    try:
        result = await run_ingestion(
            files,
            chunk_size=chunk_size,
            overlap=200,
            concurrency=concurrency,
            docprep_yaml=os.getenv("DOCPREP_YAML", ""),
            overwrite_preprocessed=False,
            progress=progress,
        )
    except Exception:
        if finalize is not None:
            try:
                await finalize(False)
            except Exception as exc:  # noqa: BLE001 — never strand the UI
                logger.warning("ingest progress finalize failed: %s", exc)
        raise
    else:
        if finalize is not None:
            try:
                await finalize(bool(result.get("errors") or []) is False)
            except Exception as exc:  # noqa: BLE001 — never strand the UI
                logger.warning("ingest progress finalize failed: %s", exc)

    return json.dumps(result, indent=2, ensure_ascii=False)
