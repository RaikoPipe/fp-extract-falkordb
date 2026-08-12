"""Unit tests for the ``estimate_ingestion_time`` agent tool.

The tool is read-only and pure-Python: it discovers files under a
``data_dir`` (resolved through the shared FilesystemBackend), chunks plain-
text files via the real ``chunk_text`` helper, and computes a conservative
time estimate from env-overridable per-stage rates. No LLM, FalkorDB, or
Chainlit is involved, matching the repo's "all mocked" test posture
(AGENTS.md).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness.tools import ingest_tools
from falkordb_harness.tools._paths import fs_backend as _fs_backend
from falkordb_harness.tools.ingest_tools import estimate_ingestion_time


@pytest.fixture(autouse=True)
def _scoped_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at tmp_path and clear the cached backend per test."""
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


def _invoke(data_dir: str = "preprocessed", **kw) -> dict:
    """Call the tool synchronously and parse its JSON result."""
    raw = estimate_ingestion_time.invoke(
        {"data_dir": data_dir, **kw}
    )
    return json.loads(raw)


def test_empty_directory_returns_error(tmp_path):
    # preprocessed/ exists but is empty.
    result = _invoke("preprocessed")
    assert "error" in result
    assert "No documents" in result["error"]


def test_plain_only_directory(tmp_path):
    # Two plain-text files under preprocessed/ (the default data_dir).
    pp = tmp_path / "preprocessed"
    (pp / "a.txt").write_text("Para A content.\n\nPara B content.")
    (pp / "b.md").write_text("# Title\n\nSome markdown body here.")

    result = _invoke("preprocessed", chunk_size=4000, overlap=200, concurrency=4)

    assert result["file_count"] == 2
    assert result["plain_files"] == ["a.txt", "b.md"]
    assert result["binary_files"] == []
    assert result["concurrency"] == 4
    # Both small files fit in one chunk each.
    assert result["chunk_count"] == 2

    # Math: extract = 15 * 2 / 4 = 7.5, preprocess = 0, write = 0.5 * 2 = 1.0
    # base = 8.5, margin = 8.5 * 0.2 = 1.7, total = 10.2
    assert result["breakdown"]["preprocess_s"] == 0.0
    assert result["breakdown"]["extract_s"] == pytest.approx(7.5, abs=0.01)
    assert result["breakdown"]["write_s"] == pytest.approx(1.0, abs=0.01)
    assert result["estimated_seconds"] == pytest.approx(10.2, abs=0.05)
    assert "estimated_human" in result
    assert result["estimated_human"].startswith("≈ ")


def test_binary_file_includes_preprocess_term(tmp_path):
    pp = tmp_path / "preprocessed"
    # A fake PDF: discover_files picks it up by extension; read_document is
    # NOT called for binary files (we estimate chunks from byte size).
    (pp / "scan.pdf").write_bytes(b"\x25PDF-1.4 junk" * 1000)  # ~13 KB
    (pp / "a.txt").write_text("Plain text body.")

    result = _invoke("preprocessed", chunk_size=4000, concurrency=2)

    assert result["plain_files"] == ["a.txt"]
    assert result["binary_files"] == ["scan.pdf"]
    assert result["file_count"] == 2

    # Preprocess term must be present and non-zero (30s / 2 concurrency = 15s).
    assert result["breakdown"]["preprocess_s"] == pytest.approx(15.0, abs=0.01)


def test_concurrency_floor_of_one(tmp_path):
    pp = tmp_path / "preprocessed"
    (pp / "a.txt").write_text("one chunk")

    result = _invoke("preprocessed", concurrency=0)

    # concurrency=0 is clamped to 1.
    assert result["concurrency"] == 1


def test_traversal_failure_returns_error_json():
    # A path-traversal attempt is rejected by the FilesystemBackend.
    result = _invoke("../../../etc/passwd")
    assert "error" in result
    assert "traversal" in result["error"].lower()


def test_env_overrides_change_estimate(tmp_path, monkeypatch):
    pp = tmp_path / "preprocessed"
    (pp / "a.txt").write_text("plain body content")

    # Bump the per-chunk rate to 60s and margin to 1.5.
    monkeypatch.setattr(ingest_tools, "_SECS_PER_CHUNK", 60.0)
    monkeypatch.setattr(ingest_tools, "_SECS_PER_WRITE", 1.0)
    monkeypatch.setattr(ingest_tools, "_SECS_PER_PREPROCESS", 60.0)
    monkeypatch.setattr(ingest_tools, "_ESTIMATE_MARGIN", 1.5)

    result = _invoke("preprocessed", concurrency=1)

    # 1 chunk: extract = 60, write = 1, base = 61, margin = 30.5, total = 91.5
    assert result["breakdown"]["extract_s"] == pytest.approx(60.0, abs=0.01)
    assert result["breakdown"]["write_s"] == pytest.approx(1.0, abs=0.01)
    assert result["estimated_seconds"] == pytest.approx(91.5, abs=0.05)


def test_human_duration_short():
    from falkordb_harness.tools.ingest_tools import _human_duration

    assert _human_duration(0) == "≈ 0s"
    assert _human_duration(45) == "≈ 45s"
    assert _human_duration(60) == "≈ 1m 00s"
    assert _human_duration(125) == "≈ 2m 05s"
    assert _human_duration(3700) == "≈ 1h 01m"


def test_binary_chunk_estimate_uses_byte_size(tmp_path):
    """Binary files (no docprep run) estimate chunks from byte size / chunk_size."""
    pp = tmp_path / "preprocessed"
    # 10000 bytes / 4000 chunk_size -> ceil = 3 chunks
    (pp / "big.pdf").write_bytes(b"x" * 10000)

    result = _invoke("preprocessed", chunk_size=4000)

    assert result["binary_files"] == ["big.pdf"]
    assert result["chunk_count"] == math.ceil(10000 / 4000)