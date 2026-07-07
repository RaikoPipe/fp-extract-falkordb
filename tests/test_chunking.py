"""Unit tests for document chunking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge.chunking import chunk_text, discover_files


def test_chunk_text_single_chunk():
    text = "Hello world.\n\nThis is a test."
    chunks = chunk_text(text, chunk_size=1000)
    assert len(chunks) == 1
    assert "Hello world." in chunks[0]


def test_chunk_text_multiple_chunks():
    paragraphs = [f"Paragraph {i} with some content." for i in range(50)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, chunk_size=200, overlap=50)
    assert len(chunks) > 1
    # Each chunk should be non-empty
    for c in chunks:
        assert len(c) > 0


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_overlap():
    text = "Para A content.\n\nPara B content.\n\nPara C content.\n\nPara D content."
    chunks = chunk_text(text, chunk_size=40, overlap=20)
    # With overlap, later chunks should contain some text from previous chunks
    assert len(chunks) >= 2


def test_discover_files(tmp_path):
    (tmp_path / "doc.txt").write_text("hello")
    (tmp_path / "data.csv").write_text("a,b")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.md").write_text("# Title")

    files = discover_files(tmp_path)
    names = {f.name for f in files}
    assert "doc.txt" in names
    assert "data.csv" in names
    assert "nested.md" in names
    assert "image.png" not in names
