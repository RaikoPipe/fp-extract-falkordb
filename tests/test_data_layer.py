"""Tests for the Chainlit data layer + local storage client.

Covers:
- ``init_db`` creates the five tables Chainlit's SQLAlchemy layer expects
  (users, threads, steps, elements, feedbacks) and is idempotent.
- ``build_data_layer`` returns a configured ``SQLAlchemyDataLayer`` with
  a ``LocalStorageClient`` storage provider.
- ``LocalStorageClient`` upload / read-url / delete round-trip on a temp
  directory, including nested object keys and MIME passthrough.
- The default ``DATABASE_URL`` and ``ELEMENTS_DIR`` env-var resolution.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_layer(tmp_path, monkeypatch):
    """Configure a throwaway SQLite DB + elements dir and return the layer."""
    db_file = tmp_path / "cl_dl_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "elements"))
    from falkordb_harness.data_layer import build_data_layer

    return build_data_layer()


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------
def test_init_db_creates_all_tables(tmp_layer):
    from sqlalchemy import text

    from falkordb_harness.data_layer import init_db

    _run(init_db(tmp_layer))

    async def fetch_tables():
        async with tmp_layer.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
            ).fetchall()
        await tmp_layer.engine.dispose()
        return [r[0] for r in rows]

    tables = _run(fetch_tables())
    assert set(tables) == {"documents", "elements", "feedbacks", "steps", "threads", "users"}


def test_init_db_is_idempotent(tmp_layer):
    """Running init_db twice does not error or duplicate tables."""
    from sqlalchemy import text

    from falkordb_harness.data_layer import init_db

    _run(init_db(tmp_layer))
    _run(init_db(tmp_layer))  # should be a no-op

    async def count_tables():
        async with tmp_layer.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
            ).fetchall()
        await tmp_layer.engine.dispose()
        return rows

    tables = _run(count_tables())
    assert len(tables) == 6


def test_init_db_steps_table_has_all_stepdict_columns(tmp_layer):
    """The steps table must cover every StepDict key, because the
    SQLAlchemy layer builds its INSERT column list dynamically from the
    StepDict keys present — a missing column raises OperationalError.
    """
    from sqlalchemy import text

    from falkordb_harness.data_layer import init_db

    _run(init_db(tmp_layer))

    async def cols():
        async with tmp_layer.engine.connect() as conn:
            rows = (
                await conn.execute(text("PRAGMA table_info(steps)"))
            ).fetchall()
        await tmp_layer.engine.dispose()
        return {r[1] for r in rows}

    columns = _run(cols())
    required = {
        "id", "name", "type", "threadId", "parentId", "command", "modes",
        "streaming", "waitForAnswer", "isError", "metadata", "tags",
        "input", "output", "createdAt", "start", "end", "generation",
        "showInput", "defaultOpen", "autoCollapse", "language", "icon",
    }
    assert required.issubset(columns), f"missing: {required - columns}"


def test_migrate_adds_missing_columns_to_old_schema(tmp_path, monkeypatch):
    """A DB created with an older DDL (missing defaultOpen/autoCollapse/
    command/modes/icon) is migrated to the full column set on the next
    init_db run. This is the bug that caused 'table steps has no column
    named defaultOpen' OperationalError on every step INSERT.
    """
    import sqlite3

    from sqlalchemy import text

    from falkordb_harness.data_layer import build_data_layer, init_db

    db_file = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els"))

    # Create an OLD-schema steps table missing the columns added later.
    conn = sqlite3.connect(db_file)
    conn.execute(
        'CREATE TABLE steps ('
        '"id" TEXT PRIMARY KEY, "name" TEXT, "type" TEXT, "threadId" TEXT, '
        '"streaming" INTEGER, "input" TEXT, "isError" INTEGER, "output" TEXT, '
        '"createdAt" TEXT, "start" TEXT, "showInput" TEXT, "metadata" TEXT, '
        '"generation" TEXT)'
    )
    conn.commit()
    conn.close()

    layer = build_data_layer()
    _run(init_db(layer))

    async def cols():
        async with layer.engine.connect() as conn:
            rows = (await conn.execute(text("PRAGMA table_info(steps)"))).fetchall()
        await layer.engine.dispose()
        return {r[1] for r in rows}

    columns = _run(cols())
    for missing in ("command", "modes", "defaultOpen", "autoCollapse", "icon"):
        assert missing in columns, f"migration failed to add {missing}"


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    """Running init_db twice on a migrated DB doesn't re-add columns or error."""
    import sqlite3

    from falkordb_harness.data_layer import build_data_layer, init_db

    db_file = tmp_path / "old2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els2"))

    conn = sqlite3.connect(db_file)
    conn.execute('CREATE TABLE steps ("id" TEXT PRIMARY KEY, "name" TEXT)')
    conn.commit()
    conn.close()

    layer = build_data_layer()
    _run(init_db(layer))
    _run(init_db(layer))  # should be a no-op for migration
    await_none = _run(layer.engine.dispose())
    assert await_none is None


def test_step_insert_with_full_stepdict_keys_works(tmp_path, monkeypatch):
    """The exact INSERT that previously raised OperationalError (with
    defaultOpen/autoCollapse columns) now succeeds after migration.
    """
    from sqlalchemy import text

    from falkordb_harness.data_layer import build_data_layer, init_db

    db_file = tmp_path / "insert.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els3"))

    layer = build_data_layer()
    _run(init_db(layer))

    async def do_insert():
        async with layer.engine.connect() as conn:
            await conn.execute(text('INSERT INTO threads ("id","createdAt","name","userId","userIdentifier","metadata") VALUES (:id,:c,:n,:u,:ui,:m)'),
                {"id":"t1","c":"2026-01-01","n":"x","u":"u1","ui":"rrai","m":"{}"})
            await conn.execute(text('INSERT INTO steps ("name","type","id","threadId","streaming","input","isError","output","createdAt","start","defaultOpen","autoCollapse","showInput","metadata","generation") VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i,:j,:k,:l,:m,:n,:o) ON CONFLICT (id) DO UPDATE SET "defaultOpen"=:k,"autoCollapse"=:l'),
                {"a":"on_message","b":"run","c":"s1","d":"t1","e":False,"f":"hi","g":False,"h":"hello","i":"2026","j":"2026","k":False,"l":False,"m":"json","n":"{}","o":"null"})
            await conn.commit()
            row = (await conn.execute(text('SELECT "output" FROM steps WHERE "id"=:id'), {"id":"s1"})).fetchone()
        await layer.engine.dispose()
        return row

    row = _run(do_insert())
    assert row is not None
    assert row[0] == "hello"


def test_create_step_with_list_tags_round_trips(tmp_path, monkeypatch):
    """A step whose ``tags`` is a Python list must persist + read back.

    Regression for ``sqlite3.ProgrammingError: type 'list' is not
    supported`` — Chainlit's SQLAlchemyDataLayer.create_step binds the
    ``tags`` list directly to the TEXT column, which SQLite rejects. The
    TagsJsonSQLAlchemyDataLayer subclass JSON-encodes it on write and
    decodes it on read so the frontend still gets a list.

    The real ``create_step`` / ``get_all_user_threads`` are wrapped by
    ``@queue_until_user_message`` which needs a Chainlit session context
    (unavailable in a unit test), so we bypass the decorator via
    ``__wrapped__`` and stub ``update_thread`` to a no-op. ``execute_sql``
    itself uses the async session (no context needed), so the actual SQL
    binding is exercised for real.
    """
    from falkordb_harness.data_layer import build_data_layer, init_db

    db_file = tmp_path / "tags.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els_tags"))

    layer = build_data_layer()
    _run(init_db(layer))

    # Stub update_thread (called by create_step) — no context needed and
    # we don't need a real thread row for the step insert to succeed.
    async def _noop_update_thread(*a, **kw):
        return None

    layer.update_thread = _noop_update_thread  # type: ignore[assignment]

    step_dict = {
        "id": "s_tags",
        "type": "tool",
        "name": "tool_calls",
        "threadId": "tt",
        "streaming": False,
        "isError": False,
        "input": "",
        "output": "",
        "createdAt": "2026-07-30T00:00:00Z",
        "metadata": {},
        "generation": {},
        "tags": ["Tool calls", "read_excerpt"],
    }

    async def do_roundtrip():
        # Bypass the @queue_until_user_message decorator (it needs a
        # Chainlit session context) by calling the unwrapped function.
        create_step = type(layer).create_step.__wrapped__  # type: ignore[attr-defined]
        await create_step(layer, step_dict)
        # get_step is NOT decorated, so call it directly.
        got = await layer.get_step("s_tags")
        await layer.engine.dispose()
        return got

    got = _run(do_roundtrip())
    assert got is not None
    # Read-back returns a list, not a JSON string.
    assert got["tags"] == ["Tool calls", "read_excerpt"]


def test_coerce_tags_helpers_round_trip():
    from falkordb_harness.data_layer import _coerce_tags_from_json, _coerce_tags_to_json

    assert _coerce_tags_to_json(["a", "b"]) == '["a", "b"]'
    assert _coerce_tags_from_json('["a", "b"]') == ["a", "b"]
    assert _coerce_tags_to_json(None) is None
    assert _coerce_tags_from_json(None) is None
    # Already-a-list passthrough (Postgres path).
    assert _coerce_tags_from_json(["a"]) == ["a"]
    # Raw non-JSON string wrapped as single-element list.
    assert _coerce_tags_from_json("plain") == ["plain"]
    assert _coerce_tags_from_json("") == []


# ---------------------------------------------------------------------------
# build_data_layer
# ---------------------------------------------------------------------------
def test_build_data_layer_returns_sqlalchemy_layer_with_local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "els"))
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

    from falkordb_harness.data_layer import LocalStorageClient, build_data_layer

    layer = build_data_layer()
    assert isinstance(layer, SQLAlchemyDataLayer)
    assert isinstance(layer.storage_provider, LocalStorageClient)


# ---------------------------------------------------------------------------
# LocalStorageClient
# ---------------------------------------------------------------------------
def test_storage_upload_writes_file_and_returns_url(tmp_path):
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)

    res = _run(client.upload_file("u1/e1/file.txt", b"hello", mime="text/plain"))
    assert res["object_key"] == "u1/e1/file.txt"
    assert res["url"] == "/public/elements/u1/e1/file.txt"

    written = (tmp_path / "u1" / "e1" / "file.txt").read_bytes()
    assert written == b"hello"
    _run(client.close())


def test_storage_get_read_url_matches_upload_url(tmp_path):
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)
    res = _run(client.upload_file("k", b"data"))
    url = _run(client.get_read_url("k"))
    assert url == res["url"]
    _run(client.close())


def test_storage_delete_removes_file(tmp_path):
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)
    _run(client.upload_file("to_delete", b"data"))
    assert (tmp_path / "to_delete").exists()

    assert _run(client.delete_file("to_delete")) is True
    assert not (tmp_path / "to_delete").exists()
    _run(client.close())


def test_storage_delete_missing_file_returns_true(tmp_path):
    """Deleting a non-existent object key is a no-op success."""
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)
    assert _run(client.delete_file("never_existed")) is True
    _run(client.close())


def test_storage_accepts_str_payload(tmp_path):
    """Passing a str (not bytes) is encoded and written."""
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)
    _run(client.upload_file("s.txt", "plain text"))
    assert (tmp_path / "s.txt").read_bytes() == b"plain text"
    _run(client.close())


def test_storage_creates_nested_dirs(tmp_path):
    from falkordb_harness.data_layer import LocalStorageClient

    client = LocalStorageClient(tmp_path)
    _run(client.upload_file("a/b/c/deep.bin", b"deep"))
    assert (tmp_path / "a" / "b" / "c" / "deep.bin").exists()
    _run(client.close())


# ---------------------------------------------------------------------------
# env-var resolution
# ---------------------------------------------------------------------------
def test_default_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from falkordb_harness.data_layer import _database_url

    assert _database_url() == "sqlite+aiosqlite:///./data/chainlit.db"


def test_custom_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./custom.db")
    from falkordb_harness.data_layer import _database_url

    assert _database_url() == "sqlite+aiosqlite:///./custom.db"


def test_elements_dir_defaults_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ELEMENTS_DIR", raising=False)
    from falkordb_harness.data_layer import _elements_dir

    p = _elements_dir()
    assert p == (tmp_path / "elements").resolve()
    assert p.exists()


def test_elements_dir_custom_path(tmp_path, monkeypatch):
    custom = tmp_path / "my_elements"
    monkeypatch.setenv("ELEMENTS_DIR", str(custom))
    from falkordb_harness.data_layer import _elements_dir

    p = _elements_dir()
    assert p == custom.resolve()
    assert p.exists()