"""Tests for the document-management registry.

Covers the async CRUD API in :mod:`falkordb_harness.document_registry`:
- table auto-creation
- register_upload / register_preprocessed / register_ingested (insert + dedup upsert)
- list_for_thread / list_for_graph / get
- delete (uploaded/preprocessed only) + IngestedDocumentNotDeletable for ingested
- clear_ingested_for_graph (used by reset_graph)
- orphan_thread (used on thread deletion)
- checksum_file helper
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway SQLite DB and (re)cache the engine."""
    db_file = tmp_path / "docreg_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els"))
    from falkordb_harness import document_registry

    document_registry.reset_engine_cache()
    return document_registry


# ---------------------------------------------------------------------------
# table creation
# ---------------------------------------------------------------------------
def test_ensure_documents_table_creates_table(tmp_registry):
    from sqlalchemy import text

    _run(tmp_registry._ensure_documents_table())
    async def fetch():
        async with tmp_registry._engine().connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='documents'")
                )
            ).fetchall()
        await tmp_registry._engine().dispose()
        return rows
    rows = _run(fetch())
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# register_upload
# ---------------------------------------------------------------------------
def test_register_upload_inserts_row(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1",
            user_identifier="u1",
            name="foo.pdf",
            original_path="/data/originals/foo.pdf",
            mime="application/pdf",
            bytes_size=123,
            checksum="abc",
        )
    )
    assert rid is not None
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["stage"] == "uploaded"
    assert docs[0]["name"] == "foo.pdf"
    assert docs[0]["threadId"] == "t1"
    assert docs[0]["graphName"] is None


def test_register_upload_dedups_by_checksum(tmp_registry):
    r1 = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    r2 = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    assert r1 == r2  # same row id, no duplicate
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1


def test_register_upload_none_thread_id(tmp_registry):
    """thread_id=None is allowed (uploads before thread id known)."""
    rid = _run(
        tmp_registry.register_upload(
            thread_id=None, user_identifier="u1", name="x.txt",
            original_path="/o/x.txt", checksum="zzz",
        )
    )
    assert rid is not None
    # list_for_thread filters by threadId, so a None-thread row won't show
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert docs == []


# ---------------------------------------------------------------------------
# register_preprocessed
# ---------------------------------------------------------------------------
def test_register_preprocessed_inserts_and_dedups(tmp_registry):
    r1 = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.md",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    r2 = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.md",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    assert r1 == r2
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["stage"] == "preprocessed"
    assert docs[0]["preprocessedPath"] == "/p/foo.md"


# ---------------------------------------------------------------------------
# register_ingested
# ---------------------------------------------------------------------------
def test_register_ingested_inserts_and_dedups(tmp_registry):
    r1 = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.md",
            source="foo.md", preprocessed_path="/p/foo.md",
        )
    )
    r2 = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.md",
            source="foo.md", preprocessed_path="/p/foo.md",
        )
    )
    assert r1 == r2
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert len(docs) == 1
    assert docs[0]["stage"] == "ingested"
    assert docs[0]["graphName"] == "g1"
    assert docs[0]["threadId"] is None
    assert docs[0]["ingestedAt"] is not None


def test_register_ingested_separate_graphs(tmp_registry):
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    _run(tmp_registry.register_ingested(graph_name="g2", user_identifier="u", name="a.md", source="a.md"))
    assert len(_run(tmp_registry.list_for_graph("g1"))) == 1
    assert len(_run(tmp_registry.list_for_graph("g2"))) == 1


# ---------------------------------------------------------------------------
# list_for_thread excludes ingested; list_for_graph excludes uploaded/preprocessed
# ---------------------------------------------------------------------------
def test_list_for_thread_excludes_ingested(tmp_registry):
    _run(tmp_registry.register_upload(thread_id="t1", user_identifier="u", name="a.pdf", original_path="/o/a.pdf", checksum="1"))
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["stage"] == "uploaded"


def test_list_for_graph_excludes_uploaded(tmp_registry):
    _run(tmp_registry.register_upload(thread_id="t1", user_identifier="u", name="a.pdf", original_path="/o/a.pdf", checksum="1"))
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert len(docs) == 1
    assert docs[0]["stage"] == "ingested"


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------
def test_get_returns_row(tmp_registry):
    rid = _run(tmp_registry.register_upload(thread_id="t1", user_identifier="u", name="a.pdf", original_path="/o/a.pdf", checksum="1"))
    doc = _run(tmp_registry.get(rid))
    assert doc is not None
    assert doc["id"] == rid
    assert _run(tmp_registry.get("nonexistent")) is None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_uploaded_removes_row(tmp_path, tmp_registry):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(tmp_registry.register_upload(thread_id="t1", user_identifier="u", name="a.pdf", original_path=str(f), checksum="1"))
    deleted = _run(tmp_registry.delete(rid))
    assert deleted is not None
    assert deleted["name"] == "a.pdf"
    assert not f.exists()  # on-disk file removed
    assert _run(tmp_registry.get(rid)) is None


def test_delete_missing_row_returns_none(tmp_registry):
    assert _run(tmp_registry.delete("nope")) is None


def test_delete_ingested_raises(tmp_registry):
    rid = _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    with pytest.raises(tmp_registry.IngestedDocumentNotDeletable):
        _run(tmp_registry.delete(rid))


def test_delete_remove_file_false_keeps_file(tmp_path, tmp_registry):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(tmp_registry.register_upload(thread_id="t1", user_identifier="u", name="a.pdf", original_path=str(f), checksum="1"))
    _run(tmp_registry.delete(rid, remove_file=False))
    assert f.exists()


# ---------------------------------------------------------------------------
# clear_ingested_for_graph
# ---------------------------------------------------------------------------
def test_clear_ingested_for_graph(tmp_registry):
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="b.md", source="b.md"))
    _run(tmp_registry.register_ingested(graph_name="g2", user_identifier="u", name="a.md", source="a.md"))
    n = _run(tmp_registry.clear_ingested_for_graph("g1"))
    assert n == 2
    assert _run(tmp_registry.list_for_graph("g1")) == []
    assert len(_run(tmp_registry.list_for_graph("g2"))) == 1


# ---------------------------------------------------------------------------
# orphan_thread (on thread deletion: delete rows + per-session on-disk dirs)
# ---------------------------------------------------------------------------
def test_orphan_thread_deletes_rows(tmp_registry, tmp_path, monkeypatch):
    # Point the path helpers at the same tmp_path tree the registry rows
    # reference, so orphan_thread can find and remove the session dirs.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORIGINALS_DIR", str(tmp_path / "originals"))
    monkeypatch.setenv("PREPROCESSED_DIR", str(tmp_path / "preprocessed"))
    (tmp_path / "originals" / "t1").mkdir(parents=True)
    (tmp_path / "preprocessed" / "t1").mkdir(parents=True)
    (tmp_path / "originals" / "t1" / "a.pdf").write_bytes(b"data")
    (tmp_path / "preprocessed" / "t1" / "a.md").write_text("md")

    _run(
        tmp_registry.register_upload(
            thread_id="t1",
            user_identifier="u",
            name="a.pdf",
            original_path=str(tmp_path / "originals" / "t1" / "a.pdf"),
            checksum="1",
        )
    )
    _run(
        tmp_registry.register_preprocessed(
            thread_id="t1",
            user_identifier="u",
            name="a.md",
            original_path=str(tmp_path / "originals" / "t1" / "a.pdf"),
            preprocessed_path=str(tmp_path / "preprocessed" / "t1" / "a.md"),
        )
    )
    n = _run(tmp_registry.orphan_thread("t1"))
    assert n == 2  # both uploaded + preprocessed rows deleted
    # No longer listed under the thread.
    assert _run(tmp_registry.list_for_thread("t1")) == []
    # Per-session on-disk dirs are removed.
    assert not (tmp_path / "originals" / "t1").exists()
    assert not (tmp_path / "preprocessed" / "t1").exists()


def test_orphan_thread_leaves_ingested(tmp_registry):
    _run(tmp_registry.register_ingested(graph_name="g1", user_identifier="u", name="a.md", source="a.md"))
    n = _run(tmp_registry.orphan_thread("t1"))
    assert n == 0  # ingested rows have threadId NULL already; nothing deleted
    assert len(_run(tmp_registry.list_for_graph("g1"))) == 1


# ---------------------------------------------------------------------------
# checksum_file
# ---------------------------------------------------------------------------
def test_checksum_file(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    import hashlib

    expected = hashlib.sha256(b"hello").hexdigest()
    from falkordb_harness.document_registry import checksum_file

    assert checksum_file(f) == expected