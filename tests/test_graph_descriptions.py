"""Tests for graph_descriptions persistence + per-user last-used graph.

Covers:
- graph_descriptions table auto-creation
- get/set/append/list description round-trip
- get_description_sync (sync sqlite reader used by build_agent)
- get/set_last_graph (users.metadata JSON column)
- set_last_graph_sync (sync sqlite writer used by use_graph)
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_graph_store(tmp_path, monkeypatch):
    """Point the description store at a throwaway SQLite DB + seed a user row."""
    db_file = tmp_path / "graphdesc_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    from falkordb_harness import graph_descriptions as gd

    gd.reset_engine_cache()
    # Create the users table + a test user row so last_graph helpers work.
    _run(gd._ensure_graph_descriptions_table())

    async def seed_user():
        async with gd._engine().connect() as conn:
            from sqlalchemy import text

            await conn.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS users ('
                    '"id" TEXT PRIMARY KEY, "identifier" TEXT UNIQUE NOT NULL, '
                    '"createdAt" TEXT NOT NULL, "metadata" TEXT DEFAULT \'{}\')'
                )
            )
            await conn.execute(
                text(
                    'INSERT INTO users ("id","identifier","createdAt","metadata") '
                    "VALUES (:id,:ident,:ts,'{}')"
                ),
                {"id": "u1", "ident": "alice", "ts": "2026-01-01T00:00:00+00:00"},
            )
            await conn.commit()

    _run(seed_user())
    return gd


# ---------------------------------------------------------------------------
# graph_descriptions CRUD
# ---------------------------------------------------------------------------
def test_get_description_empty_for_missing_row(tmp_graph_store):
    assert _run(tmp_graph_store.get_description("nope")) == ""


def test_set_then_get_description_round_trip(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "Order docs"))
    assert _run(tmp_graph_store.get_description("g1")) == "Order docs"


def test_set_description_upserts(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "first"))
    _run(tmp_graph_store.set_description("g1", "second"))
    assert _run(tmp_graph_store.get_description("g1")) == "second"


def test_append_description_merges(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "Base."))
    _run(tmp_graph_store.append_description("g1", "Ingested 3 files."))
    desc = _run(tmp_graph_store.get_description("g1"))
    assert "Base." in desc
    assert "Ingested 3 files." in desc


def test_append_description_creates_row_when_absent(tmp_graph_store):
    _run(tmp_graph_store.append_description("g2", "Ingested 1 file."))
    assert _run(tmp_graph_store.get_description("g2")) == "Ingested 1 file."


def test_list_descriptions_returns_all(tmp_graph_store):
    _run(tmp_graph_store.set_description("a", "A desc"))
    _run(tmp_graph_store.set_description("b", "B desc"))
    rows = _run(tmp_graph_store.list_descriptions())
    names = [r["name"] for r in rows]
    assert names == ["a", "b"]
    assert rows[0]["description"] == "A desc"


def test_get_description_row(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "desc"))
    row = _run(tmp_graph_store.get_description_row("g1"))
    assert row is not None
    assert row["name"] == "g1"
    assert row["description"] == "desc"
    assert row["updatedAt"]


def test_get_description_row_none_when_missing(tmp_graph_store):
    assert _run(tmp_graph_store.get_description_row("nope")) is None


def test_get_description_map(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "d1"))
    _run(tmp_graph_store.set_description("g2", "d2"))
    m = _run(tmp_graph_store.get_description_map(["g1", "g2", "g3"]))
    assert m["g1"] == "d1"
    assert m["g2"] == "d2"
    assert "g3" not in m


# ---------------------------------------------------------------------------
# Sync reader (used by build_agent)
# ---------------------------------------------------------------------------
def test_get_description_sync_round_trip(tmp_graph_store):
    _run(tmp_graph_store.set_description("g1", "sync desc"))
    # The sync reader uses sqlite3 directly against the same DB file.
    assert tmp_graph_store.get_description_sync("g1") == "sync desc"


def test_get_description_sync_empty_for_missing(tmp_graph_store):
    assert tmp_graph_store.get_description_sync("nope") == ""


def test_get_description_sync_empty_for_empty_name(tmp_graph_store):
    assert tmp_graph_store.get_description_sync("") == ""


# ---------------------------------------------------------------------------
# Per-user last-used graph (users.metadata JSON)
# ---------------------------------------------------------------------------
def test_get_last_graph_none_when_unset(tmp_graph_store):
    assert _run(tmp_graph_store.get_last_graph("alice")) is None


def test_set_then_get_last_graph_round_trip(tmp_graph_store):
    _run(tmp_graph_store.set_last_graph("alice", "orders"))
    assert _run(tmp_graph_store.get_last_graph("alice")) == "orders"


def test_set_last_graph_preserves_other_metadata(tmp_graph_store):
    """set_last_graph merges with existing metadata keys, not overwriting."""
    import sqlite3

    # Pre-populate the user's metadata with another key.
    db_path = tmp_graph_store._sqlite_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            'UPDATE users SET "metadata" = ? WHERE "identifier" = ?',
            (json.dumps({"role": "user"}), "alice"),
        )
        conn.commit()
    _run(tmp_graph_store.set_last_graph("alice", "orders"))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            'SELECT "metadata" FROM users WHERE "identifier" = ?', ("alice",)
        ).fetchone()
    meta = json.loads(row[0])
    assert meta["last_graph"] == "orders"
    assert meta["role"] == "user"


def test_set_last_graph_sync_round_trip(tmp_graph_store):
    tmp_graph_store.set_last_graph_sync("alice", "factory_planning")
    assert _run(tmp_graph_store.get_last_graph("alice")) == "factory_planning"


def test_set_last_graph_sync_empty_identifier_noop(tmp_graph_store):
    tmp_graph_store.set_last_graph_sync("", "orders")
    # No row was created / no error.
    assert _run(tmp_graph_store.get_last_graph("alice")) is None


def test_get_last_graph_empty_identifier_returns_none(tmp_graph_store):
    assert _run(tmp_graph_store.get_last_graph("")) is None