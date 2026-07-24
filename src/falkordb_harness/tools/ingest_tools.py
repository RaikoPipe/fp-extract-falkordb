"""Tools for document ingestion into the knowledge graph."""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend
from falkordb_harness.tools._paths import resolve as _resolve
from falkordb_harness.tools._retry import awith_retry, with_retry


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
    from knowledge.chunking import load_and_chunk
    from knowledge.llm_extract import extract_from_chunks

    backend = get_backend()
    resolved = _resolve_data_dir(data_dir)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)
    chunks = load_and_chunk(resolved, chunk_size=chunk_size)
    if not chunks:
        return "No documents found or no chunks produced."

    extractions = await extract_from_chunks(
        chunks,
        llm_model=os.getenv("LLM_MODEL"),
        api_base=os.getenv("OLLAMA_API_BASE"),
        concurrency=concurrency,
    )

    total_stmts, total_conflicts, total_reconciliations = 0, 0, 0
    for graph, source, chunk_index in extractions:
        stmts, conflicts, reconciliations = await backend.write_extraction(
            graph, source=source, chunk_index=chunk_index
        )
        total_stmts += stmts
        total_conflicts += len(conflicts)
        total_reconciliations += len(reconciliations)

    return json.dumps({
        "chunks_processed": len(chunks),
        "extractions": len(extractions),
        "cypher_statements": total_stmts,
        "nodes_in_graph": backend.node_count(),
        "conflicts_detected": total_conflicts,
        "reconciliations": total_reconciliations,
        "merge_mode": backend.merge_mode.value,
    }, indent=2, ensure_ascii=False)
