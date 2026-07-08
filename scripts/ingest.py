"""FalkorDB knowledge graph extraction pipeline.

Drives the full document lifecycle:
  - file ingestion from a local data directory
  - LLM-based entity extraction using a Pydantic graph model
  - Cypher MERGE writes to FalkorDB (built-in entity deduplication)
  - interactive search REPL:
      * graph mode     (raw Cypher or natural language -> Cypher) [default]
      * fulltext mode  (RediSearch full-text query)               [--search --fulltext]
      * vector mode    (embedding similarity search)              [--search --vector]

Visualization is handled by FalkorDB's built-in web UI (http://localhost:3000).

Usage:
    python scripts/ingest.py --ingest --data-dir ./data
    python scripts/ingest.py --search
    python scripts/ingest.py --search --fulltext
    python scripts/ingest.py --search --vector
    python scripts/ingest.py --reset
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))


async def do_ingest(
    backend,
    data_dir: Path,
    chunk_size: int,
    concurrency: int,
    llm_model: str | None,
    api_base: str | None,
) -> None:
    """Full ingestion pipeline: read -> chunk -> extract -> write."""
    from knowledge.chunking import load_and_chunk
    from knowledge.llm_extract import extract_from_chunks

    print(f"\n[+] Loading and chunking documents from {data_dir.resolve()}...")
    chunks = load_and_chunk(data_dir, chunk_size=chunk_size)
    if not chunks:
        print("[!] No chunks produced — check your data directory.")
        return

    print(f"    {len(chunks)} chunk(s) from {len(set(c['source'] for c in chunks))} file(s)")

    print(f"\n[+] Extracting entities via LLM (concurrency={concurrency})...")
    t0 = time.time()
    extractions = await extract_from_chunks(
        chunks,
        llm_model=llm_model,
        api_base=api_base,
        concurrency=concurrency,
    )
    elapsed = time.time() - t0
    print(f"    {len(extractions)} extraction(s) in {elapsed:.1f}s")

    print("\n[+] Writing to FalkorDB...")
    total_statements = 0
    total_conflicts = 0
    for graph, source, chunk_index in extractions:
        statements, conflicts = backend.write_extraction(
            graph, source=source, chunk_index=chunk_index
        )
        total_statements += statements
        total_conflicts += len(conflicts)

    node_count = backend.node_count()
    print(f"    {total_statements} Cypher statements executed")
    print(f"    {node_count} node(s) in graph '{backend.graph_name}'")
    print(f"    merge mode: {backend.merge_mode.value}")
    if backend.merge_mode.value == "conflict":
        print(f"    {total_conflicts} property conflict(s) logged to {backend.conflicts_log_path}")


async def do_search(
    backend,
    llm_model: str | None,
    api_base: str | None,
    *,
    fulltext: bool = False,
    vector: bool = False,
) -> None:
    """Launch the interactive search REPL.

    By default uses graph mode (raw Cypher / NL -> Cypher). ``--fulltext``
    or ``--vector`` switch the search mode accordingly; if both are given,
    ``--vector`` takes precedence.
    """
    from knowledge.search import GraphSearcher

    if vector:
        mode = "vector"
    elif fulltext:
        mode = "fulltext"
    else:
        mode = "graph"

    searcher = GraphSearcher(
        backend, llm_model=llm_model, api_base=api_base, mode=mode
    )
    await searcher.search_loop()


async def run(
    *,
    do_reset: bool,
    do_delete: bool,
    do_ingest_flag: bool,
    do_search_flag: bool,
    data_dir: Path,
    graph_name: str | None,
    chunk_size: int,
    concurrency: int,
    llm_model: str | None,
    api_base: str | None,
    search_fulltext: bool,
    search_vector: bool,
    merge_mode: str | None,
    conflicts_log: str | None,
) -> None:
    from knowledge.falkordb_backend import FalkorDBBackend

    backend = FalkorDBBackend(
        graph_name=graph_name,
        merge_mode=merge_mode,
        conflicts_log_path=conflicts_log,
    )

    if do_reset or do_delete:
        print(f"[+] Resetting graph '{backend.graph_name}'...")
        backend.reset()
        print("    Done.")

    if do_ingest_flag:
        if not data_dir.exists():
            print(f"[!] Data directory not found: {data_dir.resolve()}")
            print("    Create the directory and add your documents to it.")
            return
        await do_ingest(backend, data_dir, chunk_size, concurrency, llm_model, api_base)

    if do_search_flag:
        await do_search(
            backend,
            llm_model,
            api_base,
            fulltext=search_fulltext,
            vector=search_vector,
        )

    print("\n[+] Done.")


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="FalkorDB knowledge graph extraction pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        dest="data_dir",
        help=f"Directory of documents to ingest (default: '{DATA_DIR}').",
    )
    parser.add_argument(
        "--graph-name",
        default=None,
        dest="graph_name",
        help="FalkorDB graph name (default: from FALKORDB_GRAPH env or 'factory_planning').",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=4000,
        dest="chunk_size",
        help="Chunk size in characters (default: 4000).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Max parallel LLM extraction calls (default: 4).",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        dest="llm_model",
        help="LLM model string for litellm (default: from LLM_MODEL env).",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        dest="api_base",
        help="API base URL for the LLM provider (default: from OLLAMA_BASE_URL env).",
    )
    parser.add_argument(
        "--merge-mode",
        choices=["overwrite", "conflict"],
        default=None,
        dest="merge_mode",
        help=(
            "How to reconcile property values when MERGE matches an existing "
            "node. 'overwrite' (default): last-write-wins. 'conflict': "
            "first-writer-wins, disagreements recorded as conflicts "
            "(in-graph `conflicts` property + JSONL log). "
            "Also readable from the MERGE_MODE env var."
        ),
    )
    parser.add_argument(
        "--conflicts-log",
        default=None,
        dest="conflicts_log",
        help=(
            "Path to the append-only JSONL conflict log written in "
            "--merge-mode=conflict (default: ./data/conflicts.jsonl). "
            "Also readable from the CONFLICTS_LOG env var. The log survives "
            "--reset."
        ),
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Run full pipeline: read -> chunk -> extract -> write to FalkorDB.",
    )
    parser.add_argument(
        "--search",
        action="store_true",
        help="Enter interactive search REPL (graph mode: raw Cypher / NL -> Cypher).",
    )
    parser.add_argument(
        "--fulltext",
        action="store_true",
        help="Search mode: RediSearch full-text query over node properties. "
        "Requires --search. Configurable via FULLTEXT_LABEL / FULLTEXT_PROPERTY env.",
    )
    parser.add_argument(
        "--vector",
        action="store_true",
        help="Search mode: nearest-neighbour vector similarity search using "
        "embeddings of the query. Requires --search. Configurable via "
        "VECTOR_LABEL / VECTOR_PROPERTY / VECTOR_DIM / EMBEDDING_MODEL env.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all graph data.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Alias for --reset.",
    )
    return parser


def main() -> None:
    """Parse CLI arguments and run the requested steps."""
    parser = build_parser()
    args = parser.parse_args()

    if args.fulltext and not args.search:
        print("[!] --fulltext requires --search.")
        return
    if args.vector and not args.search:
        print("[!] --vector requires --search.")
        return
    if args.fulltext and args.vector:
        print("[!] --fulltext and --vector are mutually exclusive; using --vector.")
        args.fulltext = False

    if not any([args.reset, args.delete, args.ingest, args.search]):
        parser.print_help()
        return

    asyncio.run(
        run(
            do_reset=args.reset,
            do_delete=args.delete,
            do_ingest_flag=args.ingest,
            do_search_flag=args.search,
            data_dir=args.data_dir,
            graph_name=args.graph_name,
            chunk_size=args.chunk_size,
            concurrency=args.concurrency,
            llm_model=args.llm_model,
            api_base=args.api_base,
            search_fulltext=args.fulltext,
            search_vector=args.vector,
            merge_mode=args.merge_mode,
            conflicts_log=args.conflicts_log,
        )
    )


if __name__ == "__main__":
    main()
