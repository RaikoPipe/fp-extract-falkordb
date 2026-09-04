"""Direct-callable ingestion pipeline for the Chainlit toolbar.

This orchestrator runs the full extraction pipeline (preprocess → chunk →
LLM-extract → write to FalkorDB) *without* going through the deep agent, so
the Chainlit "Ingest" action button can trigger a one-press ingestion that
streams progress to the UI as :class:`chainlit.Step` entries.

It reuses the same library code the agent's ``extract_and_write`` tool calls
(:mod:`knowledge.chunking`, :mod:`knowledge.llm_extract`,
:meth:`FalkorDBBackend.write_extraction`, and the ``docprep`` entrypoint) so
extraction behaviour is identical to the agent path — only the orchestration
layer differs. The mandatory PRE-INGESTION REVIEW ROUTINE (the agent's
discover → metadata → excerpt → summarize → confirm loop) is intentionally
skipped: the user explicitly pressed the button, which is the confirmation.

The runner relies on the per-session backend installed by ``build_agent``
(:func:`falkordb_harness.backend.set_session_backend`) so ingestion targets
the graph the user selected in the sidebar. The caller must ensure the
session backend is installed in the current context before invoking
:func:`run_ingestion` (the Chainlit ``on_message`` handler restores it from
``cl.user_session``; the action callback does the same — see
:func:`_ensure_session_backend`).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

# File extensions that are already LLM-ready text. These are NOT routed
# through docprep (no VLM/OCR call); instead they are copied verbatim into
# ``preprocessed/<thread_id>/<stem>.md`` so the document registry records a
# ``preprocessedPath`` and the Chainlit sidebar marks them Preprocessed ✓.
# Everything else (PDF, DOCX, PPTX, images, Excel, ...) is routed through
# docprep first.
_PLAIN_EXTS = {".txt", ".md", ".csv", ".json", ".html", ".py"}


# Discriminated progress events emitted to the UI callback. Each call to
# ``progress`` carries a ``details`` dict whose ``kind`` field is one of:
#
#   - ``stage_start``  : a top-level pipeline stage began. ``stage`` names it
#     (``stage``/``preprocess``/``chunk``/``extract``/``write``); ``total`` is
#     the item count when known.
#   - ``stage_end``    : that stage finished. Carries the same ``stage`` plus
#     a result count (``files``/``chunks``/``extractions``/``statements``).
#   - ``file_start``   : an individual file began a stage (preprocess/chunk/
#     read). ``file`` is the name; ``stage`` is the stage running on it.
#   - ``file_end``     : that file finished its stage. ``file`` + ``stage``
#     plus optional metrics (``chars``/``pages``/``chunks``/``cached``).
#   - ``progress``     : a granular within-stage tick for long stages
#     (extract / write). Carries ``stage``, ``completed``, ``total``,
#     and optional ``label`` (a pre-rendered tqdm-style string built by
#     the UI layer from its :class:`TimeEstimator`). Emitted once per
#     chunk (extract) or per extraction (write) so the UI can show
#     ``n/total  xx%  elapsed  ETA  rate`` between stage_start and
#     stage_end.
#   - ``error``        : a per-file/per-stage failure. ``file`` and/or
#     ``stage`` plus ``error`` message.
#   - ``info``         : a free-form informational note (cached skips, plain
#     text, etc.).
#
# The UI (Chainlit ``on_ingest_documents``) interprets these to drive a live
# ``cl.TaskList``. Callers that ignore ``details`` still get a readable
# ``label`` in the first positional argument, preserving backwards
# compatibility with the previous Step-emitting callback shape.
ProgressFn = Callable[[str, dict[str, Any] | None], Awaitable[None]]


async def _noop_progress(_label: str, _details: dict[str, Any] | None = None) -> None:
    """Default no-op progress sink so callers can omit ``progress`` safely.

    Several call sites in this module invoke ``progress(...)`` without an
    ``if progress`` guard (notably the per-file chunk loop and the
    ``_preprocess_one`` helper). When the Chainlit progress factory fails
    to build a panel — or in any non-UI runtime — ``progress`` arrives as
    ``None`` and those unguarded calls raise ``'NoneType' object is not
    callable``. Routing every call through this no-op (set at the top of
    :func:`run_ingestion` / :func:`_preprocess_one`) keeps the call sites
    unconditional without forcing callers to pass a callback.
    """
    return


def _ensure_session_backend() -> None:
    """Re-install the per-session FalkorDB backend in the current context.

    Chainlit runs ``on_chat_start`` / ``on_settings_update`` /
    ``on_message`` / action callbacks as separate asyncio tasks, so a
    contextvar set during ``build_agent`` does not survive across handlers.
    The live backend is persisted in ``cl.user_session`` (which is keyed by
    session id and does survive); this restores it so the tools — and this
    runner — see the user's selected graph.
    """
    import chainlit as cl

    from falkordb_harness.backend import set_session_backend

    session_backend = cl.user_session.get("session_backend")
    if session_backend is not None:
        set_session_backend(session_backend)


def _needs_preprocessing(path: Path) -> bool:
    """Return True if ``path`` is a binary/non-text format requiring docprep.

    Plain-text formats (``_PLAIN_EXTS``) are copied to ``preprocessed/``
    verbatim instead of going through docprep — see :func:`_copy_plain_one`.
    """
    return path.suffix.lower() not in _PLAIN_EXTS


def _session_thread_id() -> str | None:
    """Return the current Chainlit thread id, or ``None`` outside Chainlit.

    ``run_ingestion`` is called from both the Chainlit UI (action button +
    ``extract_and_write`` tool) and the CLI / tests. Only the Chainlit path
    has a thread context; the document registry accepts ``thread_id=None``
    for the non-UI path, so a missing context degrades gracefully.
    """
    try:
        import chainlit as cl

        return cl.context.session.thread_id  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 — not in a Chainlit context
        return None


def _session_user_id() -> str | None:
    """Return the current Chainlit user identifier, or ``None``."""
    try:
        import chainlit as cl

        return cl.user_session.get("user_identifier")  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        return None


def _build_docprep_config(yaml_path: str = ""):
    """Build a docprep PipelineConfig, reusing the tools' resolution logic."""
    from falkordb_harness.tools.preprocess_tools import _build_config

    return _build_config(yaml_path)


async def _preprocess_one(
    src: Path,
    out_dir: Path,
    yaml_path: str,
    overwrite: bool,
    progress: ProgressFn | None,
) -> Path | None:
    """Preprocess a single binary file to Markdown via docprep.

    Returns the path to the produced ``.md`` file, or ``None`` on failure
    (the error is logged + reported via the progress callback). Skips files
    that are already plain text — those are ingested directly.
    """
    out_path = out_dir / (src.stem + ".md")
    if out_path.exists() and not overwrite:
        await progress(
            f"Preprocessed `{src.name}` (cached)",
            {
                "kind": "file_end",
                "stage": "preprocess",
                "file": src.name,
                "cached": True,
                "output": str(out_path),
            },
        )
        return out_path

    try:
        from docprep.entrypoint import convert
    except ImportError as exc:
        await progress(
            f"Cannot preprocess `{src.name}`: docprep not installed ({exc})",
            {"kind": "error", "stage": "preprocess", "file": src.name, "error": str(exc)},
        )
        return None

    try:
        config = _build_docprep_config(yaml_path)
    except Exception as exc:  # noqa: BLE001 — surface config errors to UI
        await progress(
            f"Cannot preprocess `{src.name}`: docprep config error ({exc})",
            {"kind": "error", "stage": "preprocess", "file": src.name, "error": str(exc)},
        )
        return None

    await progress(
        f"Preprocessing `{src.name}` via docprep…",
        {"kind": "file_start", "stage": "preprocess", "file": src.name},
    )
    try:
        result = convert(src, config)
    except Exception as exc:  # noqa: BLE001 — includes UnsupportedFormatError
        await progress(
            f"Preprocessing `{src.name}` failed: {exc}",
            {"kind": "error", "stage": "preprocess", "file": src.name, "error": str(exc)},
        )
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.markdown, encoding="utf-8")
    await progress(
        f"Preprocessed `{src.name}` → `{out_path.name}` "
        f"({len(result.markdown):,} chars, {result.page_count} page(s), "
        f"{result.processing_time_seconds:.1f}s)",
        {
            "kind": "file_end",
            "stage": "preprocess",
            "file": src.name,
            "output": str(out_path),
            "pipeline": result.pipeline_used,
            "pages": result.page_count,
            "chars": len(result.markdown),
        },
    )
    return out_path


async def _copy_plain_one(
    src: Path,
    out_dir: Path,
    progress: ProgressFn | None,
) -> Path | None:
    """Copy a plain-text source to ``preprocessed/<stem>.md`` verbatim.

    Plain-text formats (``_PLAIN_EXTS``) are already LLM-ready and do not
    need docprep. They are copied into the ``preprocessed/`` tree as
    ``<stem>.md`` so the document registry records a ``preprocessedPath``
    and the Chainlit document sidebar marks the file Preprocessed ✓. The
    content is copied byte-for-byte (no re-encoding) so non-UTF-8 text
    survives intact; the ``.md`` extension is cosmetic — ingestion reads
    via :func:`knowledge.chunking.read_document`, which decodes text.

    Returns the path to the produced ``.md`` file, or ``None`` on failure
    (the error is logged + reported via the progress callback).
    """
    out_path = out_dir / (src.stem + ".md")
    if out_path.exists():
        await progress(
            f"Preprocessed `{src.name}` (cached)",
            {
                "kind": "file_end",
                "stage": "preprocess",
                "file": src.name,
                "cached": True,
                "output": str(out_path),
            },
        )
        return out_path

    await progress(
        f"Copying plain-text `{src.name}` → `preprocessed/`…",
        {"kind": "file_start", "stage": "preprocess", "file": src.name},
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out_path)
    except OSError as exc:
        await progress(
            f"Copying `{src.name}` failed: {exc}",
            {"kind": "error", "stage": "preprocess", "file": src.name, "error": str(exc)},
        )
        return None

    await progress(
        f"Copied `{src.name}` → `{out_path.name}` (plain text, no docprep)",
        {
            "kind": "file_end",
            "stage": "preprocess",
            "file": src.name,
            "output": str(out_path),
            "pipeline": "plain-copy",
        },
    )
    return out_path


async def run_ingestion(
    file_paths: list[Path],
    *,
    chunk_size: int = 4000,
    overlap: int = 200,
    concurrency: int = 4,
    docprep_yaml: str = "",
    overwrite_preprocessed: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the full ingestion pipeline on ``file_paths``.

    Steps:
      1. Normalize each uploaded path into ``originals/`` (copy if the
         source is outside the data tree — e.g. Chainlit's ``.files/``
         staging dir).
      2. Preprocess binary/non-text files via docprep → ``preprocessed/``.
         Plain-text files are used as-is.
      3. Chunk all resulting text/Markdown.
      4. LLM-extract entities (:func:`knowledge.llm_extract.extract_from_chunks`).
      5. Write each extraction to FalkorDB via the session backend.
      6. Return a summary dict.

    The session backend (installed via ``set_session_backend``) determines
    the target graph — the caller must ensure it is set in the current
    context (see :func:`_ensure_session_backend`).

    Args:
        file_paths: Absolute paths to uploaded/source files. They are copied
            into ``originals/`` if not already there.
        chunk_size / overlap: Chunking parameters (match the agent's
            ``extract_and_write`` defaults).
        concurrency: Max parallel LLM extraction calls.
        docprep_yaml: Optional path to a docprep YAML config. Empty falls
            back to ``./docprep.yaml`` (same resolution as the
            ``preprocess_document`` tool).
        overwrite_preprocessed: Re-run docprep even if the ``.md`` exists.
        progress: Optional ``async (label, details_dict) -> None`` callback
            for streaming progress to the UI. ``details`` carries a
            ``kind`` field (``stage_start``/``stage_end``/``file_start``/
            ``file_end``/``error``/``info``) the UI switches on to drive a
            live progress panel; ``label`` is always a human-readable string.

    Returns:
        Summary dict with ``files_staged``, ``files_preprocessed``,
        ``chunks_processed``, ``extractions``, ``cypher_statements``,
        ``nodes_in_graph``, ``conflicts_detected``, ``merge_mode``, and
        ``errors`` (list of per-file error strings; empty on full success).
    """
    from falkordb_harness.backend import get_backend
    from falkordb_harness.tools._paths import (
        thread_originals_dir,
        thread_preprocessed_dir,
    )
    from knowledge.chunking import chunk_text, read_document
    from knowledge.llm_extract import extract_from_chunks

    errors: list[str] = []

    # Normalize ``progress`` to a no-op so the unconditional call sites below
    # (the per-file chunk loop, ``_preprocess_one``) are safe even when no
    # UI callback was supplied or the Chainlit factory failed. Callers that
    # pass a real callback still drive the live TaskList panel as before.
    if progress is None:
        progress = _noop_progress

    # The per-session thread id drives on-disk isolation: each session's
    # originals/preprocessed files live under originals/<thread_id>/ and
    # preprocessed/<thread_id>/ respectively. None (CLI / no Chainlit
    # context) lands files in the _unscoped/ subdirectory.
    thread_id = _session_thread_id()

    # --- Stage 1: stage files into originals/<thread_id>/ ---
    originals = thread_originals_dir(thread_id)
    staged: list[Path] = []
    if progress:
        await progress(
            f"Staging {len(file_paths)} file(s) into `originals/`…",
            {"kind": "stage_start", "stage": "stage", "total": len(file_paths)},
        )
    for src in file_paths:
        src = Path(src)
        if not src.exists() or not src.is_file():
            errors.append(f"File not found: {src}")
            continue
        # If the file is already under this session's originals tree, use it
        # in place; otherwise copy into originals/<thread_id>/.
        try:
            src.resolve().relative_to(originals)
            staged.append(src)
            continue
        except ValueError:
            pass
        dest = originals / src.name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        staged.append(dest)
    if progress:
        await progress(f"Staged {len(staged)} file(s) into `originals/`.", {
            "kind": "stage_end",
            "stage": "stage",
            "files": [p.name for p in staged],
            "total": len(staged),
        })

    if not staged:
        return {
            "files_staged": 0,
            "files_preprocessed": 0,
            "chunks_processed": 0,
            "extractions": 0,
            "cypher_statements": 0,
            "nodes_in_graph": 0,
            "conflicts_detected": 0,
            "merge_mode": os.getenv("MERGE_MODE", "overwrite"),
            "errors": errors or ["No files to ingest."],
        }

    # --- Stage 2: preprocess files -> preprocessed/<thread_id>/*.md ---
    # Binary/non-text files go through docprep; plain-text files are copied
    # verbatim. Both produce a ``preprocessed/<stem>.md`` that is registered
    # in the document registry so the sidebar marks them Preprocessed ✓.
    pre_out = thread_preprocessed_dir(thread_id)
    preprocessed_count = 0
    ingest_paths: list[Path] = []  # files to chunk (preprocessed .md output)
    if progress and staged:
        await progress(
            f"Preprocessing {len(staged)} file(s)…",
            {"kind": "stage_start", "stage": "preprocess", "total": len(staged)},
        )
    for src in staged:
        if _needs_preprocessing(src):
            md = await _preprocess_one(
                src, pre_out, docprep_yaml, overwrite_preprocessed, progress
            )
        else:
            md = await _copy_plain_one(src, pre_out, progress)
        if md is not None:
            ingest_paths.append(md)
            preprocessed_count += 1
            # Register the preprocessed output in the document registry
            # (single source of truth for the sidebar). Best-effort:
            # errors are swallowed inside register_preprocessed, so a
            # registry failure never breaks ingestion.
            try:
                from falkordb_harness.document_registry import (
                    register_preprocessed,
                )

                await register_preprocessed(
                    thread_id=_session_thread_id(),
                    user_identifier=_session_user_id(),
                    name=src.name,
                    original_path=str(src),
                    preprocessed_path=str(md),
                )
            except Exception as exc:  # noqa: BLE001 — never block ingestion
                logger.debug("register_preprocessed failed: {}", exc)
        else:
            errors.append(f"Preprocessing failed for `{src.name}`")
    if progress and staged:
        await progress(
            f"Preprocessed {preprocessed_count}/{len(staged)} file(s).",
            {"kind": "stage_end", "stage": "preprocess", "files": preprocessed_count},
        )

    if not ingest_paths:
        return {
            "files_staged": len(staged),
            "files_preprocessed": preprocessed_count,
            "chunks_processed": 0,
            "extractions": 0,
            "cypher_statements": 0,
            "nodes_in_graph": 0,
            "conflicts_detected": 0,
            "merge_mode": os.getenv("MERGE_MODE", "overwrite"),
            "errors": errors or ["No files survived preprocessing."],
        }

    # --- Stage 3: chunk ---
    if progress:
        await progress(
            f"Chunking {len(ingest_paths)} file(s)…",
            {"kind": "stage_start", "stage": "chunk", "total": len(ingest_paths)},
        )
    chunks: list[dict] = []
    for path in ingest_paths:
        await progress(
            f"Chunking `{path.name}`…",
            {"kind": "file_start", "stage": "chunk", "file": path.name},
        )
        try:
            text = read_document(path)
        except Exception as exc:  # noqa: BLE001 — keep going on per-file errors
            errors.append(f"Could not read `{path.name}`: {exc}")
            if progress:
                await progress(
                    f"Could not read `{path.name}`: {exc}",
                    {"kind": "error", "stage": "chunk", "file": path.name, "error": str(exc)},
                )
            continue
        file_chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        for i, chunk in enumerate(file_chunks):
            chunks.append({"source": path.name, "chunk_index": i, "text": chunk})
        if progress:
            await progress(
                f"Chunked `{path.name}` → {len(file_chunks)} chunk(s) ({len(text):,} chars).",
                {
                    "kind": "file_end",
                    "stage": "chunk",
                    "file": path.name,
                    "chunks": len(file_chunks),
                    "chars": len(text),
                },
            )
    if progress:
        await progress(
            f"Chunked {len(chunks)} chunk(s) from {len(ingest_paths)} file(s).",
            {"kind": "stage_end", "stage": "chunk", "chunks": len(chunks)},
        )

    if not chunks:
        return {
            "files_staged": len(staged),
            "files_preprocessed": preprocessed_count,
            "chunks_processed": 0,
            "extractions": 0,
            "cypher_statements": 0,
            "nodes_in_graph": 0,
            "conflicts_detected": 0,
            "merge_mode": os.getenv("MERGE_MODE", "overwrite"),
            "errors": errors or ["No chunks produced from the files."],
        }

    if progress:
        await progress(
            f"Extracting entities from {len(chunks)} chunk(s) via LLM…",
            {
                "kind": "stage_start",
                "stage": "extract",
                "total": len(chunks),
                "concurrency": concurrency,
            },
        )

    # --- Stage 4: LLM extraction ---
    async def _extract_progress(completed: int, total: int) -> None:
        # Forward as a granular ``progress`` event so the UI's TimeEstimator
        # can render a tqdm-style line (n/total, %, elapsed, ETA, rate).
        # Skip the no-op sink entirely to avoid needless await overhead.
        if progress is _noop_progress:
            return
        await progress(
            f"Extracting {completed}/{total} chunk(s)…",
            {
                "kind": "progress",
                "stage": "extract",
                "completed": completed,
                "total": total,
            },
        )

    extractions = await extract_from_chunks(
        chunks,
        llm_model=os.getenv("LLM_MODEL"),
        api_base=os.getenv("OLLAMA_BASE_URL"),
        concurrency=concurrency,
        on_progress=_extract_progress,
    )
    if progress:
        await progress(
            f"LLM extraction complete: {len(extractions)} successful extraction(s).",
            {"kind": "stage_end", "stage": "extract", "extractions": len(extractions)},
        )

    # --- Stage 5: write to FalkorDB via the session backend ---
    if progress:
        await progress(
            f"Writing {len(extractions)} extraction(s) to FalkorDB…",
            {"kind": "stage_start", "stage": "write", "total": len(extractions)},
        )
    backend = get_backend()
    total_stmts = 0
    total_conflicts = 0
    total_reconciliations = 0
    _write_total = len(extractions)
    _write_completed = 0
    ingested_sources: set[str] = set()
    source_to_path: dict[str, Path] = {}
    for p in ingest_paths:
        source_to_path.setdefault(p.name, p)
    original_by_stem: dict[str, Path] = {p.stem: p for p in staged}
    for graph, source, chunk_index in extractions:
        try:
            stmts, conflicts, reconciliations = await backend.write_extraction(
                graph, source=source, chunk_index=chunk_index
            )
            total_stmts += stmts
            total_conflicts += len(conflicts)
            total_reconciliations += len(reconciliations)
            ingested_sources.add(source)
        except Exception as exc:  # noqa: BLE001 — per-extraction resilience
            errors.append(f"Write failed for `{source}` chunk {chunk_index}: {exc}")
            logger.error("Ingestion write error for {} chunk {}: {}", source, chunk_index, exc)
        _write_completed += 1
        if progress is not _noop_progress:
            await progress(
                f"Writing {_write_completed}/{_write_total} extraction(s)…",
                {
                    "kind": "progress",
                    "stage": "write",
                    "completed": _write_completed,
                    "total": _write_total,
                },
            )

    nodes_in_graph = backend.node_count()
    if progress:
        await progress(
            f"Wrote {total_stmts} Cypher statement(s) to graph "
            f"`{backend.graph_name}` ({nodes_in_graph} nodes now, "
            f"{total_conflicts} conflict(s), "
            f"{total_reconciliations} potential duplicate(s) detected).",
            {
                "kind": "stage_end",
                "stage": "write",
                "graph": backend.graph_name,
                "statements": total_stmts,
                "nodes": nodes_in_graph,
                "conflicts": total_conflicts,
                "reconciliations": total_reconciliations,
            },
        )

    # Register ingested files in the document registry (best-effort).
    if ingested_sources:
        graph_name = backend.graph_name
        user_id = _session_user_id()
        try:
            from falkordb_harness.document_registry import register_ingested

            for src_name in sorted(ingested_sources):
                staged_path = source_to_path.get(src_name)
                # Link preprocessed .md back to its original for provenance.
                original_path: str | None = None
                if staged_path is not None:
                    if staged_path.suffix.lower() == ".md":
                        original = original_by_stem.get(staged_path.stem)
                        if original is not None:
                            original_path = str(original)
                    else:
                        original_path = str(staged_path)
                await register_ingested(
                    graph_name=graph_name,
                    user_identifier=user_id,
                    name=src_name,
                    source=src_name,
                    original_path=original_path,
                    preprocessed_path=(
                        str(staged_path)
                        if staged_path is not None
                        and staged_path.suffix.lower() == ".md"
                        else None
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — never block ingestion
            logger.debug("register_ingested failed: {}", exc)

    return {
        "files_staged": len(staged),
        "files_preprocessed": preprocessed_count,
        "chunks_processed": len(chunks),
        "extractions": len(extractions),
        "cypher_statements": total_stmts,
        "nodes_in_graph": nodes_in_graph,
        "conflicts_detected": total_conflicts,
        "reconciliations_found": total_reconciliations,
        "merge_mode": backend.merge_mode.value,
        "errors": errors,
    }


__all__ = ["_ensure_session_backend", "run_ingestion"]