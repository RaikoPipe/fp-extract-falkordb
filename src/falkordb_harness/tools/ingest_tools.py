"""Tools for document ingestion into the knowledge graph."""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_core.tools import tool

from falkordb_harness.backend import get_backend


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
    """
    from knowledge.chunking import load_and_chunk

    resolved = Path(data_dir) if data_dir else Path(os.getenv("DATA_DIR", "./data"))
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
    containing documents (.txt, .md, .pdf, .docx, .csv, .json, .html).
    Returns a summary with statement count, node count, and conflicts detected.
    """
    from knowledge.chunking import load_and_chunk
    from knowledge.llm_extract import extract_from_chunks

    backend = get_backend()
    resolved = Path(data_dir) if data_dir else Path(os.getenv("DATA_DIR", "./data"))
    chunks = load_and_chunk(resolved, chunk_size=chunk_size)
    if not chunks:
        return "No documents found or no chunks produced."

    extractions = await extract_from_chunks(
        chunks,
        llm_model=os.getenv("LLM_MODEL"),
        api_base=os.getenv("OLLAMA_BASE_URL"),
        concurrency=concurrency,
    )

    total_stmts, total_conflicts = 0, 0
    for graph, source, chunk_index in extractions:
        stmts, conflicts = backend.write_extraction(
            graph, source=source, chunk_index=chunk_index
        )
        total_stmts += stmts
        total_conflicts += len(conflicts)

    return json.dumps({
        "chunks_processed": len(chunks),
        "extractions": len(extractions),
        "cypher_statements": total_stmts,
        "nodes_in_graph": backend.node_count(),
        "conflicts_detected": total_conflicts,
        "merge_mode": backend.merge_mode.value,
    }, indent=2, ensure_ascii=False)
