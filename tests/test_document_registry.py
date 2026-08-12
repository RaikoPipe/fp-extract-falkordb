"""Tests for the document-management registry.

Covers the async CRUD API in :mod:`falkordb_harness.document_registry`
under the v1 single-row schema:
- table auto-creation (documents + document_ingestions)
- register_upload / register_preprocessed / register_ingested (insert + dedup upsert)
- list_for_thread / list_for_graph / get / get_ingestion
- delete (non-ingested only) + IngestedDocumentNotDeletable for ingested rows
- clear_ingested_for_graph (used by reset_graph)
- orphan_thread (used on thread deletion — preserves ingested rows)
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
def test_ensure_tables_creates_documents(tmp_registry):
    from sqlalchemy import text

    _run(tmp_registry._ensure_tables())

    async def fetch():
        async with tmp_registry._engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name IN ('documents','document_ingestions')"
                    )
                )
            ).fetchall()
        await tmp_registry._engine().dispose()
        return rows

    rows = _run(fetch())
    names = {r[0] for r in rows}
    assert "documents" in names
    assert "document_ingestions" in names


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
    assert docs[0]["name"] == "foo.pdf"
    assert docs[0]["threadId"] == "t1"
    assert docs[0]["preprocessed"] in (0, 1, False, True)
    assert docs[0]["preprocessedPath"] is None
    assert docs[0]["preprocessed"] in (0, False)


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


def test_register_upload_dedups_by_name_when_checksum_missing(tmp_registry):
    """Without a checksum, dedup falls back to (threadId, name)."""
    r1 = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum=None,
        )
    )
    r2 = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum=None,
        )
    )
    assert r1 == r2
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
def test_register_preprocessed_updates_existing_row(tmp_registry):
    """Preprocessing an uploaded original updates the same row (single-row schema)."""
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    r2 = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    assert r2 == rid  # same row, now with preprocessedPath
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["preprocessedPath"] == "/p/foo.md"
    assert docs[0]["preprocessedAt"] is not None


def test_register_preprocessed_inserts_row_when_no_upload(tmp_registry):
    """If no uploaded row exists yet, register_preprocessed creates one."""
    rid = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    assert rid is not None
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["preprocessedPath"] == "/p/foo.md"


def test_register_preprocessed_dedups_by_name(tmp_registry):
    r1 = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    r2 = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", preprocessed_path="/p/foo.md",
        )
    )
    assert r1 == r2
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1


# ---------------------------------------------------------------------------
# register_ingested
# ---------------------------------------------------------------------------
def test_register_ingested_links_to_existing_document(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    ing_id = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.pdf",
            source="foo.pdf", original_path="/o/foo.pdf",
        )
    )
    assert ing_id is not None
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert len(docs) == 1
    assert docs[0]["id"] == rid
    assert docs[0]["ingestedAt"] is not None
    thread_docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(thread_docs) == 1
    assert thread_docs[0]["id"] == rid


def test_register_ingested_dedups_by_document_and_graph(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    ing1 = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.pdf",
            source="foo.pdf", original_path="/o/foo.pdf",
        )
    )
    ing2 = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.pdf",
            source="foo.pdf", original_path="/o/foo.pdf",
        )
    )
    assert ing1 == ing2  # same ingestion row, no duplicate
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert len(docs) == 1


def test_register_ingested_separate_graphs(tmp_registry):
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.md",
            source="a.md", preprocessed_path="/p/a.md",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g2", user_identifier="u", name="a.md",
            source="a.md", preprocessed_path="/p/a.md",
        )
    )
    assert len(_run(tmp_registry.list_for_graph("g1"))) == 1
    assert len(_run(tmp_registry.list_for_graph("g2"))) == 1


def test_register_ingested_with_explicit_document_id(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="foo.pdf",
            original_path="/o/foo.pdf", checksum="abc",
        )
    )
    ing_id = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="foo.pdf",
            source="foo.pdf", document_id=rid,
        )
    )
    assert ing_id is not None
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert docs[0]["id"] == rid


# ---------------------------------------------------------------------------
# list_for_thread vs list_for_graph
# ---------------------------------------------------------------------------
def test_list_for_thread_returns_thread_rows(tmp_registry):
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="b.md",
            source="b.md", preprocessed_path="/p/b.md",
        )
    )
    docs = _run(tmp_registry.list_for_thread("t1"))
    assert len(docs) == 1
    assert docs[0]["name"] == "a.pdf"


def test_list_for_graph_returns_ingested_documents(tmp_registry):
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf",
        )
    )
    docs = _run(tmp_registry.list_for_graph("g1"))
    assert len(docs) == 1
    assert docs[0]["name"] == "a.pdf"
    assert docs[0]["ingestedAt"] is not None


# ---------------------------------------------------------------------------
# get / get_ingestion
# ---------------------------------------------------------------------------
def test_get_returns_row(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    doc = _run(tmp_registry.get(rid))
    assert doc is not None
    assert doc["id"] == rid
    assert _run(tmp_registry.get("nonexistent")) is None


def test_get_ingestion_returns_link(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=rid,
        )
    )
    ing = _run(tmp_registry.get_ingestion(rid, "g1"))
    assert ing is not None
    assert ing["documentId"] == rid
    assert ing["graphName"] == "g1"
    assert _run(tmp_registry.get_ingestion(rid, "other")) is None


def test_list_ingested_graphs_for_returns_names(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=rid,
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g2", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=rid,
        )
    )
    graphs = _run(tmp_registry.list_ingested_graphs_for(rid))
    assert set(graphs) == {"g1", "g2"}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_non_ingested_removes_row(tmp_path, tmp_registry):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path=str(f), checksum="1",
        )
    )
    deleted = _run(tmp_registry.delete(rid))
    assert deleted is not None
    assert deleted["name"] == "a.pdf"
    assert not f.exists()
    assert _run(tmp_registry.get(rid)) is None


def test_delete_missing_row_returns_none(tmp_registry):
    assert _run(tmp_registry.delete("nope")) is None


def test_delete_ingested_raises(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=rid,
        )
    )
    with pytest.raises(tmp_registry.IngestedDocumentNotDeletable):
        _run(tmp_registry.delete(rid))
    assert _run(tmp_registry.get(rid)) is not None


def test_delete_force_removes_ingested_row(tmp_path, tmp_registry):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path=str(f), checksum="1",
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=rid,
        )
    )
    deleted = _run(tmp_registry.delete(rid, force=True))
    assert deleted is not None
    assert _run(tmp_registry.get(rid)) is None
    assert _run(tmp_registry.get_ingestion(rid, "g1")) is None


def test_delete_remove_file_false_keeps_file(tmp_path, tmp_registry):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path=str(f), checksum="1",
        )
    )
    _run(tmp_registry.delete(rid, remove_file=False))
    assert f.exists()


# ---------------------------------------------------------------------------
# clear_ingested_for_graph
# ---------------------------------------------------------------------------
def test_clear_ingested_for_graph(tmp_registry):
    rid_a = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.md",
            original_path="/o/a.md", checksum="1",
        )
    )
    rid_b = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="b.md",
            original_path="/o/b.md", checksum="2",
        )
    )
    rid_c = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.md",
            original_path="/o/a.md", checksum="3",
        )
    )
    # note: rid_c shares name "a.md" with rid_a within the same thread,
    # so dedup means rid_c == rid_a. Use a different name for the g2 row.
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.md",
            source="a.md", document_id=rid_a,
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="b.md",
            source="b.md", document_id=rid_b,
        )
    )
    _run(
        tmp_registry.register_ingested(
            graph_name="g2", user_identifier="u", name="a.md",
            source="a.md", document_id=rid_a,
        )
    )
    n = _run(tmp_registry.clear_ingested_for_graph("g1"))
    assert n == 2
    assert _run(tmp_registry.list_for_graph("g1")) == []
    assert len(_run(tmp_registry.list_for_graph("g2"))) == 1
    assert _run(tmp_registry.get(rid_a)) is not None
    assert _run(tmp_registry.get(rid_b)) is not None


# ---------------------------------------------------------------------------
# orphan_thread (on thread deletion)
# ---------------------------------------------------------------------------
def test_orphan_thread_deletes_unlinked_rows_and_preserves_ingested(
    tmp_registry, tmp_path, monkeypatch
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORIGINALS_DIR", str(tmp_path / "originals"))
    monkeypatch.setenv("PREPROCESSED_DIR", str(tmp_path / "preprocessed"))
    (tmp_path / "originals" / "t1").mkdir(parents=True)
    (tmp_path / "preprocessed" / "t1").mkdir(parents=True)
    (tmp_path / "originals" / "t1" / "a.pdf").write_bytes(b"data")
    (tmp_path / "preprocessed" / "t1" / "a.md").write_text("md")

    # Two uploads: one will be ingested (preserved), one will not (deleted).
    keep_rid = _run(
        tmp_registry.register_upload(
            thread_id="t1",
            user_identifier="u",
            name="a.pdf",
            original_path=str(tmp_path / "originals" / "t1" / "a.pdf"),
            checksum="1",
        )
    )
    drop_rid = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1",
            user_identifier="u",
            name="b.pdf",  # different name, distinct row
            original_path="/o/b.pdf",
            preprocessed_path=str(tmp_path / "preprocessed" / "t1" / "a.md"),
        )
    )
    assert keep_rid != drop_rid
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.pdf",
            source="a.pdf", document_id=keep_rid,
        )
    )

    n = _run(tmp_registry.orphan_thread("t1"))
    assert n == 1  # only the un-ingested row was deleted
    # The preserved row's threadId is now NULL but the row survives.
    preserved = _run(tmp_registry.get(keep_rid))
    assert preserved is not None
    assert preserved["threadId"] is None
    # The un-ingested row is gone.
    assert _run(tmp_registry.get(drop_rid)) is None
    # Ingestion link on the preserved row is intact.
    assert _run(tmp_registry.get_ingestion(keep_rid, "g1")) is not None
    assert len(_run(tmp_registry.list_for_graph("g1"))) == 1


def test_orphan_thread_deletes_all_rows_when_none_ingested(tmp_registry):
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u", name="a.pdf",
            original_path="/o/a.pdf", checksum="1",
        )
    )
    n = _run(tmp_registry.orphan_thread("t1"))
    assert n == 1
    assert _run(tmp_registry.get(rid)) is None


def test_orphan_thread_leaves_ingested_rows_from_other_threads(tmp_registry):
    ing_id = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u", name="a.md",
            source="a.md", preprocessed_path="/p/a.md",
        )
    )
    # register_ingested returns the ingestion-row id; look up the
    # document row it points at so we can assert its survival.
    ing = _run(tmp_registry.get_ingestion_by_id(ing_id)) if hasattr(
        tmp_registry, "get_ingestion_by_id"
    ) else None
    # Fallback: query the documents table for the row with this name.
    from sqlalchemy import text

    async def _doc_id():
        async with tmp_registry._engine().connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT "id" FROM documents WHERE "name" = :n LIMIT 1'),
                    {"n": "a.md"},
                )
            ).fetchone()
        return row[0] if row else None

    rid = _run(_doc_id())
    n = _run(tmp_registry.orphan_thread("t1"))
    assert n == 0  # nothing matched the thread
    assert len(_run(tmp_registry.list_for_graph("g1"))) == 1
    # The ingested-only row was created with threadId NULL already.
    doc = _run(tmp_registry.get(rid))
    assert doc is not None
    assert doc["threadId"] is None


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


# ---------------------------------------------------------------------------
# count_for_user
# ---------------------------------------------------------------------------
def test_count_for_user_zero_when_no_rows(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    assert _run(count_for_user("u1")) == 0


def test_count_for_user_counts_uploaded_rows(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="b.pdf",
            original_path="/tmp/b.pdf", checksum="c2",
        )
    )
    assert _run(count_for_user("u1")) == 2


def test_count_for_user_counts_preprocessed_rows(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="a.md",
            original_path="/tmp/a.pdf", preprocessed_path="/tmp/a.md",
        )
    )
    assert _run(count_for_user("u1")) == 1


def test_count_for_user_filters_by_user(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )
    _run(
        tmp_registry.register_upload(
            thread_id="t2", user_identifier="u2", name="b.pdf",
            original_path="/tmp/b.pdf", checksum="c2",
        )
    )
    assert _run(count_for_user("u1")) == 1
    assert _run(count_for_user("u2")) == 1
    assert _run(count_for_user("u3")) == 0


def test_count_for_user_empty_identifier_returns_zero(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )
    assert _run(count_for_user("")) == 0
    assert _run(count_for_user(None)) == 0


def test_count_for_user_dedup_does_not_double_count(tmp_registry):
    from falkordb_harness.document_registry import count_for_user

    # Same checksum → dedup upserts, so only one row.
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )
    assert _run(count_for_user("u1")) == 1