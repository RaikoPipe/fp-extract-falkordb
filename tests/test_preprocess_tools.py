"""Unit tests for the preprocess_document tool.

No docling / docprep pipeline required: ``docprep.entrypoint.convert`` is
mocked per test. The tool resolves input paths through a FilesystemBackend
rooted at DATA_DIR (with ``originals/`` and ``preprocessed/`` subtrees) and
writes Markdown output to PREPROCESSED_DIR, so each test points DATA_DIR at
``tmp_path`` and clears the cached backend.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness.tools._paths import fs_backend as _fs_backend
from falkordb_harness.tools.preprocess_tools import preprocess_document


@pytest.fixture(autouse=True)
def _scoped_dirs(tmp_path, monkeypatch):
    """Point DATA_DIR at tmp_path (with originals/ + preprocessed/) and clear caches."""
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


def _call(tool_obj, **kwargs):
    return tool_obj.invoke(kwargs)


def _fake_result(markdown="# title\n\nbody text", *, pipeline="standard", pages=1):
    """Build a duck-typed stand-in for docprep.result.ConversionResult.

    Avoids importing ``docprep`` at module load so the test file collects
    even when docprep isn't installed yet.
    """
    return SimpleNamespace(
        source_path=Path("/ignored"),
        markdown=markdown,
        format_detected="application/pdf",
        pipeline_used=pipeline,
        page_count=pages,
        escalated=False,
        processing_time_seconds=0.12,
        warnings=[],
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_preprocess_writes_md_to_preprocessed_dir(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    with patch("docprep.entrypoint.convert", return_value=_fake_result()) as fake:
        out = json.loads(_call(preprocess_document, path="originals/scan.pdf"))

    fake.assert_called_once()
    # output_path is a DATA_DIR-relative virtual path (preprocessed/scan.md).
    out_path = Path(os.getenv("DATA_DIR")) / out["output_path"]
    assert out["output_path"].replace("\\", "/") == "preprocessed/scan.md"
    assert out_path.parent == Path(os.getenv("PREPROCESSED_DIR")).resolve()
    assert out_path.name == "scan.md"
    assert out_path.read_text() == "# title\n\nbody text"
    assert out["already_exists"] is False
    assert out["pipeline_used"] == "standard"
    assert out["markdown_char_count"] == len("# title\n\nbody text")
    assert out["page_count"] == 1


def test_preprocess_preserves_relative_path_arg(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "nested" / "doc.docx"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"PK\x03\x04 fake docx")

    with patch("docprep.entrypoint.convert", return_value=_fake_result()):
        out = json.loads(_call(preprocess_document, path="originals/nested/doc.docx"))

    assert out["output_path"].replace("\\", "/").endswith("preprocessed/doc.md")
    assert (Path(os.getenv("PREPROCESSED_DIR")) / "doc.md").exists()


# --------------------------------------------------------------------------
# No-op when target exists
# --------------------------------------------------------------------------
def test_preprocess_noop_when_md_exists(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    out_dir = Path(os.getenv("PREPROCESSED_DIR"))
    (out_dir / "scan.md").write_text("old markdown", encoding="utf-8")

    with patch("docprep.entrypoint.convert") as fake:
        out = json.loads(_call(preprocess_document, path="originals/scan.pdf"))

    fake.assert_not_called()
    assert out["already_exists"] is True
    assert out["markdown_char_count"] == len("old markdown")
    # Existing file is untouched.
    assert (out_dir / "scan.md").read_text() == "old markdown"


def test_preprocess_overwrite_replaces_existing(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    out_dir = Path(os.getenv("PREPROCESSED_DIR"))
    (out_dir / "scan.md").write_text("old markdown", encoding="utf-8")

    with patch("docprep.entrypoint.convert", return_value=_fake_result("new")):
        out = json.loads(
            _call(preprocess_document, path="originals/scan.pdf", overwrite=True)
        )

    assert out["already_exists"] is False
    assert (out_dir / "scan.md").read_text() == "new"


# --------------------------------------------------------------------------
# Errors are structured JSON, never raised
# --------------------------------------------------------------------------
def test_preprocess_missing_file(tmp_path):
    out = json.loads(_call(preprocess_document, path="ghost.pdf"))
    assert "error" in out
    assert "File not found" in out["error"]


def test_preprocess_path_traversal_blocked(tmp_path):
    out = json.loads(_call(preprocess_document, path="../escape.pdf"))
    assert "error" in out
    assert "resolving path" in out["error"]


def test_preprocess_unsupported_format(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "weird.xyz"
    src.write_bytes(b"\x00\x01")

    with patch("docprep.entrypoint.convert", side_effect=ValueError("unsupported")):
        out = json.loads(_call(preprocess_document, path="originals/weird.xyz"))

    assert "error" in out
    assert "unsupported" in out["error"]
    # No .md written on failure.
    assert not (Path(os.getenv("PREPROCESSED_DIR")) / "weird.md").exists()


def test_preprocess_docprep_not_installed(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    if isinstance(__builtins__, dict):
        real_import = __builtins__["__import__"]
    else:
        real_import = __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docprep.entrypoint":
            raise ImportError("docprep missing")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        out = json.loads(_call(preprocess_document, path="originals/scan.pdf"))

    assert "error" in out
    assert "docprep is not installed" in out["error"]


# --------------------------------------------------------------------------
# Config-not-found hard-fail (regression: silent base64 blowup)
# --------------------------------------------------------------------------
def test_preprocess_missing_config_hard_fails(tmp_path, monkeypatch):
    """When no docprep.yaml is resolvable, the tool errors instead of
    silently falling back to PipelineConfig() defaults (embed_images=True),
    which previously produced 33 MB base64-bloated Markdown. See the
    docprep.yaml-not-copied-into-Docker incident.
    """
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    # Ensure no docprep.yaml is visible from CWD (tmp_path has none, and the
    # repo-root docprep.yaml must not be picked up).
    monkeypatch.chdir(tmp_path)

    with patch("docprep.entrypoint.convert") as fake:
        out = json.loads(_call(preprocess_document, path="originals/scan.pdf"))

    fake.assert_not_called()
    assert "error" in out
    assert "docprep config not found" in out["error"]
    assert "base64" in out["error"]  # message explains *why* it refuses


def test_preprocess_missing_explicit_yaml_path_hard_fails(tmp_path):
    """An explicit yaml_path that doesn't exist is rejected, not silently
    dropped in favour of defaults."""
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    with patch("docprep.entrypoint.convert") as fake:
        out = json.loads(
            _call(
                preprocess_document,
                path="originals/scan.pdf",
                yaml_path="/nope/missing.yaml",
            )
        )

    fake.assert_not_called()
    assert "error" in out
    assert "docprep config not found" in out["error"]


# --------------------------------------------------------------------------
# Config plumbing
# --------------------------------------------------------------------------
def test_preprocess_uses_yaml_path_arg(tmp_path):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    cfg = tmp_path / "docprep.yaml"
    cfg.write_text("fallback:\n  enabled: false\n")

    with patch("docprep.entrypoint.convert", return_value=_fake_result()) as fake:
        _call(preprocess_document, path="originals/scan.pdf", yaml_path=str(cfg))

    # convert receives a PipelineConfig built from the yaml file.
    passed_config = fake.call_args[0][1]
    assert passed_config.fallback_enabled is False


def test_preprocess_falls_back_to_default_yaml(tmp_path, monkeypatch):
    src = Path(os.getenv("ORIGINALS_DIR")) / "scan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    # Drop a docprep.yaml in cwd.
    cwd_yaml = tmp_path / "docprep.yaml"
    cwd_yaml.write_text("fallback:\n  provider: ollama\n")
    monkeypatch.chdir(tmp_path)

    with patch("docprep.entrypoint.convert", return_value=_fake_result()) as fake:
        _call(preprocess_document, path="originals/scan.pdf")

    passed_config = fake.call_args[0][1]
    assert passed_config.fallback_provider == "ollama"
