"""Tools for document ingestion into the knowledge graph."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

from langchain_core.tools import tool

from falkordb_harness.tools._paths import resolve as _resolve
from falkordb_harness.tools._retry import awith_retry, with_retry

logger = logging.getLogger("falkordb_harness.tools.ingest")


# --- Conservative per-stage rate defaults (env-overridable) -----------------
# These feed ``estimate_ingestion_time``. Defaults are deliberately
# conservative so the user sees an upper-bound estimate before confirming;
# deployers can calibrate them from observed run times via the env vars.
_SECS_PER_CHUNK = float(os.getenv("INGEST_SECS_PER_CHUNK", "15"))
_SECS_PER_PREPROCESS = float(os.getenv("INGEST_SECS_PER_PREPROCESS", "30"))
_SECS_PER_WRITE = float(os.getenv("INGEST_SECS_PER_WRITE", "0.5"))
_ESTIMATE_MARGIN = float(os.getenv("INGEST_ESTIMATE_MARGIN", "1.2"))


def _human_duration(seconds: float) -> str:
    """Render a duration as a short ``≈ Xm Ys`` / ``≈ Ys`` string."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"≈ {s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"≈ {m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"≈ {h}h {m:02d}m"


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
def estimate_ingestion_time(
    data_dir: str = "",
    chunk_size: int = 4000,
    overlap: int = 200,
    concurrency: int = 4,
) -> str:
    """Estimate how long ``extract_and_write`` would take for a directory.

    Call this BEFORE ``request_ingestion_confirmation`` during the
    PRE-INGESTION REVIEW ROUTINE, with the same ``data_dir`` / ``chunk_size``
    / ``concurrency`` you intend to pass to ``extract_and_write``. Returns a
    JSON summary with file/chunk counts, a per-stage breakdown (extract /
    preprocess / write), and a human-readable ``estimated_human`` string
    (e.g. ``"≈ 3m 20s"``) to report to the user.

    The estimate is deliberately conservative (extraction dominates at
    ~15s per chunk at the configured concurrency, plus ~30s per binary
    file needing docprep, plus a +20% safety margin). Rates are env-
    overridable (``INGEST_SECS_PER_CHUNK`` / ``INGEST_SECS_PER_PREPROCESS``
    / ``INGEST_SECS_PER_WRITE`` / ``INGEST_ESTIMATE_MARGIN``).
    """
    return with_retry(
        lambda: _estimate_ingestion_time_impl(data_dir, chunk_size, overlap, concurrency)
    )


def _estimate_ingestion_time_impl(
    data_dir: str, chunk_size: int, overlap: int, concurrency: int
) -> str:
    from knowledge.chunking import (
        TEXT_EXTENSIONS,
        chunk_text,
        discover_files,
        read_document,
    )

    resolved = _resolve_data_dir(data_dir)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)

    files = discover_files(resolved)
    if not files:
        return json.dumps(
            {"error": "No documents found or no chunks produced."},
            ensure_ascii=False,
        )

    plain_files: list[str] = []
    binary_files: list[str] = []
    chunk_count = 0
    for f in files:
        if f.suffix.lower() in TEXT_EXTENSIONS:
            plain_files.append(f.name)
            try:
                text = read_document(f)
            except Exception as exc:  # noqa: BLE001 — keep estimating
                logger.warning("estimate: failed to read %s: %s", f.name, exc)
                # Fall back to a size-based chunk estimate.
                chunk_count += max(
                    1, math.ceil(f.stat().st_size / max(1, chunk_size))
                )
                continue
            chunk_count += len(chunk_text(text, chunk_size=chunk_size, overlap=overlap))
        else:
            binary_files.append(f.name)
            # docprep output length is unknown pre-conversion; estimate
            # chunks from raw byte size against ``chunk_size``.
            chunk_count += max(1, math.ceil(f.stat().st_size / max(1, chunk_size)))

    concurrency = max(1, concurrency)

    extract_s = _SECS_PER_CHUNK * chunk_count / concurrency
    preprocess_s = _SECS_PER_PREPROCESS * len(binary_files) / concurrency
    write_s = _SECS_PER_WRITE * chunk_count
    base_s = extract_s + preprocess_s + write_s
    margin_s = base_s * (_ESTIMATE_MARGIN - 1.0)
    total_s = base_s + margin_s

    return json.dumps(
        {
            "file_count": len(files),
            "plain_files": plain_files,
            "binary_files": binary_files,
            "chunk_count": chunk_count,
            "concurrency": concurrency,
            "breakdown": {
                "extract_s": round(extract_s, 1),
                "preprocess_s": round(preprocess_s, 1),
                "write_s": round(write_s, 1),
                "margin_s": round(margin_s, 1),
            },
            "estimated_seconds": round(total_s, 1),
            "estimated_human": _human_duration(total_s),
        },
        indent=2,
        ensure_ascii=False,
    )


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
    from knowledge.chunking import discover_files

    files = discover_files(resolved)
    if not files:
        return "No documents found or no chunks produced."

    # UI progress bridge: consume the factory on_message installed in
    # user_session so the agent path gets the same TaskList UI as the
    # Ingest button. Non-Chainlit runtimes get progress=None.
    progress = None
    finalize = None
    try:
        import chainlit as cl

        factory = cl.user_session.get("ingest_progress_factory")
    except Exception:  # noqa: BLE001 — not in a Chainlit context
        factory = None
    if factory is not None:
        try:
            _, progress, finalize = await factory()
        except Exception as exc:  # noqa: BLE001 — never strand ingestion
            logger.warning("ingest progress factory failed: %s", exc)
            progress, finalize = None, None

    result: dict = {}
    # ``finalize`` runs from a ``finally`` so it also fires on
    # ``CancelledError`` (stop button) — a ``BaseException`` since Py 3.8.
    success = False
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
        success = not (result.get("errors") or [])
        return json.dumps(result, indent=2, ensure_ascii=False)
    finally:
        if finalize is not None:
            try:
                await finalize(success)
            except Exception as exc:  # noqa: BLE001 — never strand the UI
                logger.warning("ingest progress finalize failed: %s", exc)
