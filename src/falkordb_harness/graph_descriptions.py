"""Knowledge-graph description + per-user last-used-graph persistence.

Two related pieces of state live in the Chainlit SQLite database:

1. **Graph descriptions** — one human-readable description per knowledge
   graph (the ``graph_descriptions`` table, created by
   :mod:`falkordb_harness.data_layer`). The description is seeded by the
   LLM at graph creation (``create_graph`` tool) and revised after every
   ingestion (``update_graph_description`` tool, or an auto-derived
   template for the button ingestion path). The LLM reads descriptions as
   the first-contact point for understanding existing graphs
   (``describe_graph`` tool).

2. **Per-user last-used graph** — stored as a ``last_graph`` key inside
   the ``users.metadata`` JSON column. Updated on every graph
   selection/switch (UI dropdown, LLM ``use_graph``, ``create_graph``, and
   on thread resume). Read by ``on_chat_start`` to preselect the graph on
   a new chat without firing ``on_message``.

Both use the same async SQLAlchemy engine as the document registry
(:func:`falkordb_harness.data_layer.build_data_layer`), so they share the
single SQLite file. A module-level cache (:data:`_ENGINE`) reuses the
engine across calls; ``reset_engine_cache`` drops it for tests.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("falkordb_harness.graph_description")

_ENGINE: AsyncEngine | None = None


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp (seconds resolution)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _engine() -> AsyncEngine:
    """Return the cached data-layer async engine, building it once.

    Reuses :func:`build_data_layer` so this module shares the same SQLite
    file / connection URL as the rest of the Chainlit persistence layer.
    """
    global _ENGINE
    if _ENGINE is None:
        from falkordb_harness.data_layer import build_data_layer

        _ENGINE = build_data_layer().engine  # type: ignore[assignment]
    return _ENGINE


def reset_engine_cache() -> None:
    """Drop the cached engine so the next call rebuilds it.

    Used by tests that monkeypatch ``DATABASE_URL`` per-case.
    """
    global _ENGINE
    _ENGINE = None


def _sqlite_path() -> str:
    """Return the on-disk SQLite path parsed from DATABASE_URL (sync fallback)."""
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/chainlit.db")
    # Forms: sqlite+aiosqlite:///./data/chainlit.db  -> ./data/chainlit.db
    #        sqlite:///./data/chainlit.db
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return url[len(prefix):]
    return "./data/chainlit.db"


def get_description_sync(name: str) -> str:
    """Sync read of a graph's description (for use in sync build_agent).

    Uses ``sqlite3`` directly (bypassing the async engine) so it can run
    inside the already-running Chainlit event loop without a nested-loop
    error. Returns an empty string on any failure (missing table, missing
    row, DB not yet initialized) — the preamble degrades gracefully.
    """
    if not name:
        return ""
    import sqlite3

    try:
        with sqlite3.connect(_sqlite_path()) as conn:
            row = conn.execute(
                'SELECT "description" FROM graph_descriptions WHERE "name" = ?',
                (name,),
            ).fetchone()
            return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def set_last_graph_sync(user_identifier: str, graph: str) -> None:
    """Sync write of the user's last-used graph (for sync tool paths).

    Counterpart of :func:`get_last_graph` using ``sqlite3`` directly, so
    the sync ``use_graph`` tool can persist the last-used graph without
    awaiting inside the running event loop. Best-effort: swallows errors.
    """
    if not user_identifier or not graph:
        return
    import sqlite3

    try:
        with sqlite3.connect(_sqlite_path()) as conn:
            row = conn.execute(
                'SELECT "metadata" FROM users WHERE "identifier" = ?',
                (user_identifier,),
            ).fetchone()
            if row is None:
                return
            raw = row[0] or "{}"
            try:
                meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, TypeError):
                meta = {}
            meta["last_graph"] = graph
            conn.execute(
                'UPDATE users SET "metadata" = ? WHERE "identifier" = ?',
                (json.dumps(meta), user_identifier),
            )
            conn.commit()
    except sqlite3.Error:
        pass


async def _ensure_graph_descriptions_table() -> None:
    """Create the graph_descriptions table if missing (idempotent).

    Normally :func:`data_layer.init_db` runs at app startup and creates
    the table. This guard lets the module be used in tests / early-startup
    paths before ``init_db`` has run, without duplicating the canonical
    DDL (which lives in :data:`data_layer._DDL_STATEMENTS`).
    """
    async with _engine().connect() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS graph_descriptions (
                    "name"        TEXT PRIMARY KEY,
                    "description" TEXT NOT NULL DEFAULT '',
                    "updatedAt"   TEXT NOT NULL
                )
                """
            )
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Graph description CRUD
# ---------------------------------------------------------------------------


async def get_description(name: str) -> str:
    """Return the description for graph ``name`` (empty string if none)."""
    await _ensure_graph_descriptions_table()
    async with _engine().connect() as conn:
        row = await conn.execute(
            text('SELECT "description" FROM graph_descriptions WHERE "name" = :n'),
            {"n": name},
        )
        r = row.fetchone()
        return str(r[0]) if r is not None else ""


async def set_description(name: str, description: str) -> None:
    """Upsert the description for graph ``name`` with a fresh timestamp."""
    await _ensure_graph_descriptions_table()
    ts = _utc_now_iso()
    async with _engine().connect() as conn:
        # SQLite UPSERT (ON CONFLICT) requires SQLite >= 3.24 (2018);
        # aiosqlite ships a modern SQLite, so this is safe.
        await conn.execute(
            text(
                """
                INSERT INTO graph_descriptions ("name", "description", "updatedAt")
                VALUES (:n, :d, :t)
                ON CONFLICT("name") DO UPDATE SET
                    "description" = :d,
                    "updatedAt" = :t
                """
            ),
            {"n": name, "d": description, "t": ts},
        )
        await conn.commit()


async def append_description(name: str, addition: str) -> None:
    """Append ``addition`` to the existing description for graph ``name``.

    Used by the button-ingestion path (no LLM turn available) to merge a
    concise stats line into the current description. Creates the row if it
    does not exist.
    """
    existing = await get_description(name)
    merged = f"{existing} {addition}".strip() if existing else addition
    await set_description(name, merged)


async def list_descriptions() -> list[dict[str, str]]:
    """Return all graph descriptions as ``[{name, description, updatedAt}]``.

    Ordered by name ascending. The LLM's ``describe_graph`` tool calls
    this (with no name argument) to read every description in one shot —
    the first-contact point for understanding existing knowledge graphs.
    """
    await _ensure_graph_descriptions_table()
    async with _engine().connect() as conn:
        rows = await conn.execute(
            text(
                'SELECT "name", "description", "updatedAt" '
                "FROM graph_descriptions ORDER BY \"name\" ASC"
            )
        )
        return [
            {"name": r[0], "description": r[1], "updatedAt": r[2]}
            for r in rows.fetchall()
        ]


async def get_description_row(name: str) -> dict[str, str | None] | None:
    """Return the full row ``{name, description, updatedAt}`` or ``None``."""
    await _ensure_graph_descriptions_table()
    async with _engine().connect() as conn:
        row = await conn.execute(
            text(
                'SELECT "name", "description", "updatedAt" '
                'FROM graph_descriptions WHERE "name" = :n'
            ),
            {"n": name},
        )
        r = row.fetchone()
        if r is None:
            return None
        return {"name": r[0], "description": r[1], "updatedAt": r[2]}


async def get_description_map(names: list[str]) -> dict[str, str]:
    """Return ``{name: description}`` for the given names (empty string if missing).

    Convenience for the agent preamble, which needs the active graph's
    description without issuing a tool call each turn.
    """
    if not names:
        return {}
    await _ensure_graph_descriptions_table()
    async with _engine().connect() as conn:
        # Bind an IN (...) list safely.
        params = {f"n{i}": n for i, n in enumerate(names)}
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        rows = await conn.execute(
            text(
                f'SELECT "name", "description" FROM graph_descriptions '
                f'WHERE "name" IN ({placeholders})'
            ),
            params,
        )
        return {r[0]: r[1] for r in rows.fetchall()}


# ---------------------------------------------------------------------------
# Per-user last-used graph (users.metadata JSON)
# ---------------------------------------------------------------------------


async def get_last_graph(user_identifier: str) -> str | None:
    """Return the user's last-used graph name, or ``None`` if unset.

    Reads the ``last_graph`` key from the ``users.metadata`` JSON column.
    """
    if not user_identifier:
        return None
    async with _engine().connect() as conn:
        row = await conn.execute(
            text('SELECT "metadata" FROM users WHERE "identifier" = :i'),
            {"i": user_identifier},
        )
        r = row.fetchone()
        if r is None or not r[0]:
            return None
        try:
            meta = json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {})
        except (json.JSONDecodeError, TypeError):
            return None
        val = meta.get("last_graph")
        return val if isinstance(val, str) and val else None


async def set_last_graph(user_identifier: str, graph: str) -> None:
    """Persist ``last_graph`` into the user's ``metadata`` JSON column.

    Read-modify-write (merges with any existing metadata keys) so other
    metadata entries (role/accountStatus are on dedicated columns, but
    future JSON keys) are preserved.
    """
    if not user_identifier or not graph:
        return
    async with _engine().connect() as conn:
        row = await conn.execute(
            text('SELECT "metadata" FROM users WHERE "identifier" = :i'),
            {"i": user_identifier},
        )
        r = row.fetchone()
        if r is None:
            return
        raw = r[0] or "{}"
        try:
            meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta["last_graph"] = graph
        await conn.execute(
            text('UPDATE users SET "metadata" = :m WHERE "identifier" = :i'),
            {"m": json.dumps(meta), "i": user_identifier},
        )
        await conn.commit()