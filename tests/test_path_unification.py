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