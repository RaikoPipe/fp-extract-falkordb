"""Tools for inspecting file metadata and reading bounded excerpts.

These complement the deepagents built-in ``read_file``/``ls`` tools (which only
expose line-range slicing and strip ``size`` from directory listings) by
reporting per-file metadata — byte size, page count (PDF/DOCX), character /
word / line counts — and reading excerpts in three modes: by line, by page,
or by byte range.

The tools resolve paths through the shared ``FilesystemBackend`` rooted at
``DATA_DIR`` (see :mod:`falkordb_harness.tools._paths`), so both the
``originals/`` raw-source tree and the ``preprocessed/`` Markdown tree are
visible. The pre-ingestion review routine inspects files under
``originals/``; after ``preprocess_document`` converts them, the resulting
``.md`` under ``preprocessed/`` is readable by the same tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from falkordb_harness.tools._paths import fs_backend as _fs_backend  # noqa: F401
from falkordb_harness.tools._paths import resolve as _resolve
from falkordb_harness.tools._retry import with_retry
from knowledge.chunking import SUPPORTED_EXTENSIONS, TEXT_EXTENSIONS

# Cap on a single excerpt's returned characters. Prevents large bodies from
# flooding the LLM context during pre-ingestion review.
_EXCERPT_CHAR_CAP = 8000

# Cap on raw bytes returned in "bytes" mode.
_BYTE_MODE_CAP = 512


def _classify(path: Path) -> str:
    """Classify a file by extension into a coarse type bucket."""
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in SUPPORTED_EXTENSIONS:
        return "text"
    return "binary"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def _pdf_page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _docx_page_count(path: Path) -> int | None:
    try:
        from unstructured.partition.auto import partition
    except ImportError:
        return None
    try:
        pages = {
            el.metadata.page_number
            for el in partition(filename=str(path))
            if el.metadata.page_number is not None
        }
        return max(pages) if pages else None
    except Exception:
        return None


@tool
def file_metadata(path: str) -> str:
    """Return metadata for a file without loading its full body into context.

    Reports: size_bytes, size_human, extension, file_type (text/pdf/docx/
    csv/json/html/binary), page_count (PDF/DOCX only), char_count, word_count,
    line_count (text only), and detected encoding.

    Call this BEFORE reading excerpts or ingesting, to understand what a file
    contains at a glance. Missing files return a JSON ``{"error": ...}`` object
    rather than raising.
    """
    return with_retry(lambda: _file_metadata_impl(path))


def _file_metadata_impl(path: str) -> str:
    resolved = _resolve(path)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)

    if not resolved.exists() or not resolved.is_file():
        return json.dumps(
            {"error": f"File not found: {path}"}, ensure_ascii=False
        )

    ftype = _classify(resolved)
    size_bytes = resolved.stat().st_size
    ext = resolved.suffix.lower()

    meta: dict[str, Any] = {
        "path": str(path),
        "name": resolved.name,
        "extension": ext,
        "file_type": ftype,
        "size_bytes": size_bytes,
        "size_human": _human_size(size_bytes),
    }

    # Page count: PDF/DOCX only.
    if ftype == "pdf":
        meta["page_count"] = _pdf_page_count(resolved)
    elif ftype == "docx":
        meta["page_count"] = _docx_page_count(resolved)
    else:
        meta["page_count"] = None

    # Text counts: only for genuinely text-like files.
    if ftype == "text":
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
            meta["char_count"] = len(text)
            meta["word_count"] = len(text.split())
            meta["line_count"] = text.count("\n") + 1
            meta["encoding"] = "utf-8"
        except OSError as exc:
            meta["error"] = f"Failed to read text: {exc}"
    else:
        meta["char_count"] = None
        meta["word_count"] = None
        meta["line_count"] = None
        meta["encoding"] = None

    return json.dumps(meta, indent=2, ensure_ascii=False)


def _truncate(text: str) -> str:
    if len(text) <= _EXCERPT_CHAR_CAP:
        return text
    return text[:_EXCERPT_CHAR_CAP] + "\n... [truncated, call again with a new offset]"


def _excerpt_lines(path_obj: Path, offset: int, limit: int) -> str:
    text = path_obj.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if offset < 0:
        offset = 0
    if offset >= len(lines):
        return f"Line offset {offset} exceeds file length ({len(lines)} lines)"
    end = min(offset + limit, len(lines))
    chunk = "".join(lines[offset:end])
    # cat -n style formatting, 1-indexed.
    rendered = "".join(
        f"{i + 1}: {ln}" for i, ln in enumerate(lines[offset:end], start=offset)
    )
    header = f"[lines {offset + 1}-{end} of {len(lines)}]\n"
    return _truncate(header + rendered)


def _excerpt_pages(path_obj: Path, offset: int, limit: int) -> str:
    if offset < 1:
        offset = 1
    try:
        from pypdf import PdfReader
    except ImportError:
        return json.dumps(
            {"error": "pypdf not installed; cannot read PDF pages"},
            ensure_ascii=False,
        )
    try:
        reader = PdfReader(str(path_obj))
    except Exception as exc:
        return json.dumps({"error": f"Failed to open PDF: {exc}"}, ensure_ascii=False)
    total = len(reader.pages)
    if offset > total:
        return f"Page offset {offset} exceeds page count ({total})"
    end = min(offset + limit, total + 1)
    parts: list[str] = []
    for page_no in range(offset, end):
        try:
            body = reader.pages[page_no - 1].extract_text() or ""
        except Exception as exc:
            body = f"[error extracting page {page_no}: {exc}]"
        parts.append(f"--- page {page_no} ---\n{body}")
    header = f"[pages {offset}-{end - 1} of {total}]\n"
    return _truncate(header + "\n\n".join(parts))


def _excerpt_docx_pages(path_obj: Path, offset: int, limit: int) -> str:
    if offset < 1:
        offset = 1
    try:
        from unstructured.partition.auto import partition
    except ImportError:
        return json.dumps(
            {"error": "unstructured not installed; cannot read DOCX pages"},
            ensure_ascii=False,
        )
    try:
        elements = partition(filename=str(path_obj))
    except Exception as exc:
        return json.dumps({"error": f"Failed to open DOCX: {exc}"}, ensure_ascii=False)
    by_page: dict[int, list[str]] = {}
    for el in elements:
        pg = el.metadata.page_number or 1
        by_page.setdefault(pg, []).append(str(el))
    total = max(by_page) if by_page else 0
    if offset > total:
        return f"Page offset {offset} exceeds page count ({total})"
    end = min(offset + limit, total + 1)
    parts: list[str] = []
    for page_no in range(offset, end):
        body = "\n".join(by_page.get(page_no, [])) or "[empty page]"
        parts.append(f"--- page {page_no} ---\n{body}")
    header = f"[pages {offset}-{end - 1} of {total}]\n"
    return _truncate(header + "\n\n".join(parts))


def _excerpt_bytes(path_obj: Path, offset: int, limit: int) -> str:
    if limit > _BYTE_MODE_CAP:
        limit = _BYTE_MODE_CAP
    if offset < 0:
        offset = 0
    raw = path_obj.read_bytes()[offset:offset + limit]
    hex_part = raw.hex(" ")
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
    header = f"[bytes {offset}-{offset + len(raw) - 1} of {path_obj.stat().st_size}]"
    return _truncate(f"{header}\nhex: {hex_part}\nascii: {ascii_part}")


@tool
def read_excerpt(
    path: str,
    mode: str = "auto",
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Read a bounded excerpt of a file without loading the whole thing.

    mode:
    - "lines" (text files): offset = 0-indexed line, limit = line count.
      Output is cat -n formatted.
    - "pages" (PDF/DOCX): offset = 1-indexed page, limit = page count.
      Pages are separated by ``--- page N ---`` markers.
    - "bytes" (other binary): offset = byte, limit = byte count (capped).
      Output is a hex + ascii preview.
    - "auto": resolved from the file extension (call file_metadata first).

    Prefer a few small excerpts (e.g. pages 1, 3, and the last page) to
    understand content cheaply rather than reading the whole file. Output is
    capped at 8000 characters. Missing files return a JSON ``{"error": ...}``.
    """
    return with_retry(lambda: _read_excerpt_impl(path, mode, offset, limit))


def _read_excerpt_impl(
    path: str, mode: str, offset: int, limit: int
) -> str:
    resolved = _resolve(path)
    if isinstance(resolved, str):
        return json.dumps({"error": resolved}, ensure_ascii=False)

    if not resolved.exists() or not resolved.is_file():
        return json.dumps(
            {"error": f"File not found: {path}"}, ensure_ascii=False
        )

    effective_mode = mode
    if effective_mode == "auto":
        effective_mode = {
            "text": "lines", "pdf": "pages", "docx": "pages", "binary": "bytes"
        }[_classify(resolved)]

    if effective_mode == "lines":
        return _excerpt_lines(resolved, offset, limit)
    if effective_mode == "pages":
        if _classify(resolved) == "docx":
            return _excerpt_docx_pages(resolved, offset, limit)
        return _excerpt_pages(resolved, offset, limit)
    if effective_mode == "bytes":
        return _excerpt_bytes(resolved, offset, limit)
    return json.dumps(
        {"error": f"Unknown mode '{mode}'. Use lines|pages|bytes|auto."},
        ensure_ascii=False,
    )
