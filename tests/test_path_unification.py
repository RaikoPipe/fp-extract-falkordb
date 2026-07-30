"""Tests for the shared path-resolution layer (falkordb_harness.tools._paths).

Verifies that the FilesystemBackend rooted at DATA_DIR exposes both the
``originals/`` and ``preprocessed/`` subtrees, that virtual paths round-trip,
and that non-ASCII (NFC/NFD) filenames resolve consistently.
"""

import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from falkordb_harness.tools import _paths


@pytest.fixture(autouse=True)
def _scoped_data_dir(tmp_path, monkeypatch):
    originals = tmp_path / "originals"
    preprocessed = tmp_path / "preprocessed"
    originals.mkdir(parents=True, exist_ok=True)
    preprocessed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORIGINALS_DIR", str(originals))
    monkeypatch.setenv("PREPROCESSED_DIR", str(preprocessed))
    _paths.fs_backend.cache_clear()
    yield
    _paths.fs_backend.cache_clear()


def test_resolve_under_originals_subtree(tmp_path):
    f = tmp_path / "originals" / "foo.md"
    f.write_text("hi")
    resolved = _paths.resolve("originals/foo.md")
    assert isinstance(resolved, Path)
    assert resolved == f.resolve()


def test_resolve_under_preprocessed_subtree(tmp_path):
    """The preprocessed tree is visible to the same resolver (the bug fix)."""
    f = tmp_path / "preprocessed" / "bar.md"
    f.write_text("hi")
    resolved = _paths.resolve("preprocessed/bar.md")
    assert isinstance(resolved, Path)
    assert resolved == f.resolve()


def test_virtual_path_roundtrips():
    """virtual_path(absolute) yields a root-relative path resolve() accepts."""
    abs_path = _paths.preprocessed_dir() / "out.md"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("x")
    v = _paths.virtual_path(abs_path)
    assert v.replace("\\", "/") == "preprocessed/out.md"
    assert _paths.resolve(v) == abs_path.resolve()


def test_path_traversal_rejected():
    out = _paths.resolve("../escape.txt")
    assert isinstance(out, str)
    assert "resolving path" in out


def test_non_ascii_nfc_nfd_equivalence(tmp_path):
    """A decomposed (NFD) filename resolves the same as composed (NFC)."""
    name_nfc = "Technologie\u00fcbersicht.pptx"  # ü as single codepoint
    name_nfd = unicodedata.normalize("NFD", name_nfc)  # u + combining diaeresis
    f = tmp_path / "originals" / name_nfc
    f.write_bytes(b"PK")

    resolved_nfc = _paths.resolve(f"originals/{name_nfc}")
    resolved_nfd = _paths.resolve(f"originals/{name_nfd}")
    assert isinstance(resolved_nfc, Path)
    assert isinstance(resolved_nfd, Path)
    # Both must point at the same on-disk file.
    assert resolved_nfc == resolved_nfd
    assert resolved_nfd.exists()


def test_data_dir_originals_preprocessed_defaults(monkeypatch, tmp_path):
    """When ORIGINALS_DIR / PREPROCESSED_DIR are unset, they default under DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ORIGINALS_DIR", raising=False)
    monkeypatch.delenv("PREPROCESSED_DIR", raising=False)
    assert _paths.data_dir() == tmp_path.resolve()
    assert _paths.originals_dir() == (tmp_path / "originals").resolve()
    assert _paths.preprocessed_dir() == (tmp_path / "preprocessed").resolve()


# ---------------------------------------------------------------------------
# Per-session subdirectory helpers
# ---------------------------------------------------------------------------
def test_thread_originals_dir_thread_id(tmp_path):
    d = _paths.thread_originals_dir("thread-123")
    assert d == (tmp_path / "originals" / "thread-123").resolve()
    assert d.is_dir()  # auto-created


def test_thread_preprocessed_dir_thread_id(tmp_path):
    d = _paths.thread_preprocessed_dir("thread-123")
    assert d == (tmp_path / "preprocessed" / "thread-123").resolve()
    assert d.is_dir()


def test_thread_dirs_none_id_uses_unscoped(tmp_path):
    """thread_id=None (CLI / pre-session) lands in the _unscoped subdir."""
    assert _paths.thread_originals_dir(None) == (tmp_path / "originals" / "_unscoped").resolve()
    assert _paths.thread_preprocessed_dir(None) == (tmp_path / "preprocessed" / "_unscoped").resolve()
    assert _paths.thread_originals_dir(None).is_dir()
    assert _paths.thread_preprocessed_dir(None).is_dir()


def test_thread_dirs_reject_unscoped_sentinel():
    """A real thread id equal to the reserved '_unscoped' sentinel is rejected."""
    import pytest

    with pytest.raises(ValueError):
        _paths.thread_originals_dir("_unscoped")
    with pytest.raises(ValueError):
        _paths.thread_preprocessed_dir("_unscoped")


def test_resolve_under_thread_subtree(tmp_path):
    """Files in a per-session subdir resolve via the same root-relative path."""
    f = tmp_path / "originals" / "t1" / "foo.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    resolved = _paths.resolve("originals/t1/foo.pdf")
    assert isinstance(resolved, Path)
    assert resolved == f.resolve()


def test_virtual_path_includes_session_subdir():
    """virtual_path produces originals/<tid>/<name> for nested session files."""
    abs_path = _paths.thread_originals_dir("t1") / "foo.pdf"
    abs_path.write_bytes(b"x")
    v = _paths.virtual_path(abs_path)
    assert v.replace("\\", "/") == "originals/t1/foo.pdf"