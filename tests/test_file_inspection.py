"""Unit tests for file_metadata and read_excerpt tools.

No FalkorDB or LLM required. PDFs are synthesized with reportlab (skipped
if unavailable). The tools resolve paths through a FilesystemBackend rooted
at DATA_DIR, so each test points DATA_DIR at ``tmp_path`` and places files
under ``originals/`` (the raw-source subtree).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness.tools._paths import fs_backend as _fs_backend
from falkordb_harness.tools.file_inspection_tools import (
    file_metadata,
    read_excerpt,
)


@pytest.fixture(autouse=True)
def _scoped_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at tmp_path and clear the cached backend per test.

    The file-inspection tools resolve paths through a FilesystemBackend rooted
    at DATA_DIR, with ``originals/`` and ``preprocessed/`` as subdirectories.
    Both env vars are set so either resolution path stays contained within
    tmp_path.
    """
    originals = tmp_path / "originals"
    preprocessed = tmp_path / "preprocessed"
    originals.mkdir(parents=True, exist_ok=True)
    preprocessed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORIGINALS_DIR", str(originals))
    monkeypatch.setenv("PREPROCESSED_DIR", str(preprocessed))
    _fs_backend.cache_clear()
    yield
    _fs_backend.cache_clear()


def _make_pdf(path: Path, page_texts: list[str]) -> None:
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for i, body in enumerate(page_texts):
        c.drawString(72, 800, body)
        c.showPage()
    c.save()


def _call(tool_obj, **kwargs):
    """Invoke a LangChain @tool object and return its string output."""
    return tool_obj.invoke(kwargs)


# --------------------------------------------------------------------------
# file_metadata
# --------------------------------------------------------------------------
def test_file_metadata_text(tmp_path):
    f = tmp_path / "originals" / "doc.md"
    f.write_text("hello world\nsecond line\n\nthird para")
    out = json.loads(_call(file_metadata, path="originals/doc.md"))
    assert out["file_type"] == "text"
    assert out["extension"] == ".md"
    assert out["size_bytes"] == f.stat().st_size
    assert out["page_count"] is None
    assert out["char_count"] == len("hello world\nsecond line\n\nthird para")
    # words: hello, world, second, line, third, para = 6
    assert out["word_count"] == 6
    # 3 newlines -> 4 lines
    assert out["line_count"] == 4
    assert out["encoding"] == "utf-8"


def test_file_metadata_pdf_page_count(tmp_path):
    f = tmp_path / "originals" / "doc.pdf"
    _make_pdf(f, ["page one", "page two", "page three"])
    out = json.loads(_call(file_metadata, path="originals/doc.pdf"))
    assert out["file_type"] == "pdf"
    assert out["extension"] == ".pdf"
    assert out["page_count"] == 3
    # text counts are null for PDF
    assert out["char_count"] is None
    assert out["word_count"] is None


def test_file_metadata_missing(tmp_path):
    out = json.loads(_call(file_metadata, path="nope.md"))
    assert "error" in out
    assert "File not found" in out["error"]


def test_file_metadata_binary(tmp_path):
    f = tmp_path / "originals" / "blob.bin"
    f.write_bytes(b"\x00\x01\x02\xff")
    out = json.loads(_call(file_metadata, path="originals/blob.bin"))
    assert out["file_type"] == "binary"
    assert out["size_bytes"] == 4
    assert out["page_count"] is None
    assert out["char_count"] is None


# --------------------------------------------------------------------------
# read_excerpt — lines
# --------------------------------------------------------------------------
def test_read_excerpt_lines_default(tmp_path):
    f = tmp_path / "originals" / "doc.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 6)))
    out = _call(read_excerpt, path="originals/doc.txt")
    assert "[lines 1-5 of 5]" in out
    assert "1: line 1" in out
    assert "5: line 5" in out


def test_read_excerpt_lines_offset_limit(tmp_path):
    f = tmp_path / "originals" / "doc.txt"
    f.write_text("\n".join(f"line {i}" for i in range(1, 11)))
    out = _call(read_excerpt, path="originals/doc.txt", mode="lines", offset=3, limit=2)
    assert "[lines 4-5 of 10]" in out
    assert "4: line 4" in out
    assert "5: line 5" in out
    assert "line 3" not in out
    assert "line 6" not in out


def test_read_excerpt_lines_out_of_range(tmp_path):
    f = tmp_path / "originals" / "doc.txt"
    f.write_text("only one line\n")
    out = _call(read_excerpt, path="originals/doc.txt", offset=99, limit=10)
    assert "exceeds file length" in out


# --------------------------------------------------------------------------
# read_excerpt — pages (PDF)
# --------------------------------------------------------------------------
def test_read_excerpt_pdf_pages(tmp_path):
    f = tmp_path / "originals" / "doc.pdf"
    _make_pdf(f, ["alpha", "beta", "gamma"])
    out = _call(read_excerpt, path="originals/doc.pdf", mode="pages", offset=2, limit=1)
    assert "[pages 2-2 of 3]" in out
    assert "--- page 2 ---" in out
    assert "beta" in out
    assert "alpha" not in out
    assert "gamma" not in out


def test_read_excerpt_pdf_auto_mode(tmp_path):
    f = tmp_path / "originals" / "doc.pdf"
    _make_pdf(f, ["one", "two"])
    out = _call(read_excerpt, path="originals/doc.pdf")  # auto -> pages
    assert "--- page 1 ---" in out
    assert "one" in out


def test_read_excerpt_pdf_out_of_range(tmp_path):
    f = tmp_path / "originals" / "doc.pdf"
    _make_pdf(f, ["only"])
    out = _call(read_excerpt, path="originals/doc.pdf", mode="pages", offset=9, limit=1)
    assert "exceeds page count" in out


# --------------------------------------------------------------------------
# read_excerpt — bytes
# --------------------------------------------------------------------------
def test_read_excerpt_bytes(tmp_path):
    f = tmp_path / "originals" / "blob.bin"
    f.write_bytes(b"ABCDEFGH")
    out = _call(read_excerpt, path="originals/blob.bin", mode="bytes", offset=0, limit=8)
    assert "[bytes 0-7 of 8]" in out
    assert "ascii: ABCDEFGH" in out
    assert "41 42 43" in out  # hex for ABC


def test_read_excerpt_bytes_capped(tmp_path):
    f = tmp_path / "originals" / "big.bin"
    f.write_bytes(b"\x00" * 2048)
    out = _call(read_excerpt, path="originals/big.bin", mode="bytes", limit=2048)
    # Should be capped at 512 bytes regardless of the large limit requested.
    assert "of 2048" in out
    # hex string length for 512 bytes = 512 * 2 + 511 spaces = 1535 chars
    assert len(out) < 4000


def test_read_excerpt_missing(tmp_path):
    out = json.loads(_call(read_excerpt, path="ghost.txt"))
    assert "error" in out


def test_read_excerpt_unknown_mode(tmp_path):
    f = tmp_path / "originals" / "doc.txt"
    f.write_text("hi")
    out = json.loads(_call(read_excerpt, path="originals/doc.txt", mode="bogus"))
    assert "error" in out
    assert "Unknown mode" in out["error"]


# --------------------------------------------------------------------------
# Truncation
# --------------------------------------------------------------------------
def test_read_excerpt_truncation(tmp_path):
    f = tmp_path / "originals" / "long.txt"
    f.write_text("x" * 20000)
    out = _call(read_excerpt, path="originals/long.txt", mode="lines", offset=0, limit=1)
    assert "[truncated" in out
    assert len(out) <= 8000 + 80  # cap + slack for header/truncation marker