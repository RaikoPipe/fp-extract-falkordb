"""Document-management registry backed by the Chainlit SQLite data layer.

A single ``documents`` table (created by :mod:`falkordb_harness.data_layer`)
tracks every file through three lifecycle stages:

    uploaded  → raw original copied into ``originals/`` (scoped to a chat
                thread; ``threadId`` set, ``graphName`` NULL).
    preprocessed → docprep Markdown output in ``preprocessed/`` (scoped to a
                thread; paired to its original by name).
    ingested  → file whose extractions were written to a knowledge graph
                (scoped to the graph; ``graphName`` set, ``threadId`` NULL).
                Ingested rows are permanent: they are not deletable from the
                sidebar and are only removed when the graph itself is reset
                (``clear_ingested_for_graph``).

The registry is the single source of truth for the document-management
sidebar. Uploaded/preprocessed rows are deduplicated by ``(threadId,
checksum, stage)``; ingested rows are deduplicated by ``(graphName, name)``.
On thread deletion, ``orphan_thread`` nulls ``threadId`` so the on-disk
files remain re-usable by other chats and graphs.

All methods are async and run against the data layer's SQLAlchemy engine
(see :func:`falkordb_harness.data_layer.build_data_layer`). They are safe
to call from Chainlit handlers (which are already async) and from tests.
Errors are logged and swallowed for the non-critical register/list calls
(never breaking ingestion on a registry write failure), but propagated for
``delete`` (which the UI treats as authoritative).

A small module-level cache (:data:`_ENGINE`) reuses the engine across calls
within a process. ``reset_engine_cache`` (used by tests) drops it so a
fresh ``DATABASE_URL`` is picked up.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("falkordb_harness.document_registry")

# Stage constants — kept as plain strings so SQL queries stay readable.
STAGE_UPLOADED = "uploaded"
STAGE_PREPROCESSED = "preprocessed"
STAGE_INGESTED = "ingested"

_ENGINE: AsyncEngine | None = None


def _utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp (seconds resolution)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_id() -> str:
    """Return a fresh random row id (UUID4 hex)."""
    return uuid.uuid4().hex


def _engine() -> AsyncEngine:
    """Return the cached data-layer async engine, building it once.

    Reuses :func:`build_data_layer` so the registry shares the same SQLite
    file / connection URL as the rest of the Chainlit persistence layer
    (users, threads, steps, elements). The engine itself is async and
    backed by ``aiosqlite``.
    """
    global _ENGINE
    if _ENGINE is None:
        from falkordb_harness.data_layer import build_data_layer

        _ENGINE = build_data_layer().engine  # type: ignore[assignment]
    return _ENGINE


def reset_engine_cache() -> None:
    """Drop the cached engine so the next call rebuilds it.

    Used by tests that monkeypatch ``DATABASE_URL`` per-case so a stale
    engine pointing at the previous DB file is not reused.
    """
    global _ENGINE
    _ENGINE = None


def checksum_file(path: Path) -> str:
    """Return the sha256 hex digest of ``path`` (streamed, memory-safe)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLAlchemy row (with column names) to a plain dict."""
    return {k: getattr(row, k) for k in row._mapping}  # type: ignore[attr-defined]


async def _ensure_documents_table() -> None:
    """Create the documents table if missing (idempotent).

    Normally :func:`data_layer.init_db` runs at app startup and creates the
    table. This guard lets the registry be used in tests / early-startup
    paths before ``init_db`` has run, without duplicating the DDL (the
    canonical DDL lives in :data:`data_layer._DDL_STATEMENTS`).
    """
    async with _engine().connect() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    "id"               TEXT PRIMARY KEY,
                    "userIdentifier"   TEXT,
                    "threadId"         TEXT,
                    "graphName"        TEXT,
                    "name"             TEXT NOT NULL,
                    "stage"            TEXT NOT NULL,
                    "originalPath"     TEXT,
                    "preprocessedPath" TEXT,
                    "source"           TEXT,
                    "mime"             TEXT,
                    "bytes"            INTEGER,
                    "checksum"         TEXT,
                    "ingestedAt"       TEXT,
                    "createdAt"        TEXT NOT NULL
                )
                """
            )
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Register (upsert) helpers
# ---------------------------------------------------------------------------

async def register_upload(
    *,
    thread_id: str | None,
    user_identifier: str | None,
    name: str,
    original_path: str,
    mime: str | None = None,
    bytes_size: int | None = None,
    checksum: str | None = None,
) -> str | None:
    """Record (upsert) an uploaded original scoped to a chat thread.

    Dedup: if a row with the same ``(threadId, checksum, 'uploaded')``
    already exists, it is updated in place (path/mime/bytes refreshed) and
    its id returned — re-uploading the same file to the same thread does
    not create a duplicate. ``thread_id`` may be ``None`` (e.g. uploads
    arriving before the thread id is known); in that case dedup is by
    ``checksum`` alone among rows with ``threadId IS NULL``.

    Returns the row id, or ``None`` on a non-fatal failure (the error is
    logged). Upload tracking must never block the chat.
    """
    await _ensure_documents_table()
    row_id = _new_id()
    created = _utc_now_iso()
    try:
        async with _engine().begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT "id" FROM documents
                        WHERE "stage" = :stage
                          AND "checksum" IS NOT NULL
                          AND "checksum" = :checksum
                          AND ("threadId" IS :thread_null OR "threadId" = :thread_id)
                        LIMIT 1
                        """
                    ),
                    {
                        "stage": STAGE_UPLOADED,
                        "checksum": checksum,
                        "thread_null": thread_id is None,
                        "thread_id": thread_id,
                    },
                )
            ).fetchone()
            if existing is not None:
                row_id = existing[0]
                await conn.execute(
                    text(
                        """
                        UPDATE documents
                           SET "originalPath" = :originalPath,
                               "mime" = :mime,
                               "bytes" = :bytes,
                               "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier")
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": row_id,
                        "originalPath": original_path,
                        "mime": mime,
                        "bytes": bytes_size,
                        "userIdentifier": user_identifier,
                    },
                )
                return row_id
            await conn.execute(
                text(
                    """
                    INSERT INTO documents
                        ("id","userIdentifier","threadId","graphName","name",
                         "stage","originalPath","preprocessedPath","source",
                         "mime","bytes","checksum","ingestedAt","createdAt")
                    VALUES
                        (:id,:userIdentifier,:threadId,NULL,:name,
                         :stage,:originalPath,NULL,NULL,
                         :mime,:bytes,:checksum,NULL,:createdAt)
                    """
                ),
                {
                    "id": row_id,
                    "userIdentifier": user_identifier,
                    "threadId": thread_id,
                    "name": name,
                    "stage": STAGE_UPLOADED,
                    "originalPath": original_path,
                    "mime": mime,
                    "bytes": bytes_size,
                    "checksum": checksum,
                    "createdAt": created,
                },
            )
        return row_id
    except Exception as exc:  # noqa: BLE001 — never break the chat
        logger.error("register_upload failed for %r: %s", name, exc)
        return None


async def register_preprocessed(
    *,
    thread_id: str | None,
    user_identifier: str | None,
    name: str,
    original_path: str,
    preprocessed_path: str,
    checksum: str | None = None,
) -> str | None:
    """Record (upsert) a docprep Markdown output scoped to a chat thread.

    Dedup: if a preprocessed row with the same ``(threadId, name)`` exists,
    it is updated in place (paths refreshed) and its id returned —
    re-running docprep on the same original does not create a duplicate.

    Returns the row id, or ``None`` on a non-fatal failure.
    """
    await _ensure_documents_table()
    row_id = _new_id()
    created = _utc_now_iso()
    try:
        async with _engine().begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT "id" FROM documents
                        WHERE "stage" = :stage
                          AND "name" = :name
                          AND ("threadId" IS :thread_null OR "threadId" = :thread_id)
                        LIMIT 1
                        """
                    ),
                    {
                        "stage": STAGE_PREPROCESSED,
                        "name": name,
                        "thread_null": thread_id is None,
                        "thread_id": thread_id,
                    },
                )
            ).fetchone()
            if existing is not None:
                row_id = existing[0]
                await conn.execute(
                    text(
                        """
                        UPDATE documents
                           SET "originalPath" = :originalPath,
                               "preprocessedPath" = :preprocessedPath,
                               "checksum" = COALESCE(:checksum, "checksum"),
                               "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier")
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": row_id,
                        "originalPath": original_path,
                        "preprocessedPath": preprocessed_path,
                        "checksum": checksum,
                        "userIdentifier": user_identifier,
                    },
                )
                return row_id
            await conn.execute(
                text(
                    """
                    INSERT INTO documents
                        ("id","userIdentifier","threadId","graphName","name",
                         "stage","originalPath","preprocessedPath","source",
                         "mime","bytes","checksum","ingestedAt","createdAt")
                    VALUES
                        (:id,:userIdentifier,:threadId,NULL,:name,
                         :stage,:originalPath,:preprocessedPath,NULL,
                         NULL,NULL,:checksum,NULL,:createdAt)
                    """
                ),
                {
                    "id": row_id,
                    "userIdentifier": user_identifier,
                    "threadId": thread_id,
                    "name": name,
                    "stage": STAGE_PREPROCESSED,
                    "originalPath": original_path,
                    "preprocessedPath": preprocessed_path,
                    "checksum": checksum,
                    "createdAt": created,
                },
            )
        return row_id
    except Exception as exc:  # noqa: BLE001
        logger.error("register_preprocessed failed for %r: %s", name, exc)
        return None


async def register_ingested(
    *,
    graph_name: str,
    user_identifier: str | None,
    name: str,
    source: str | None = None,
    original_path: str | None = None,
    preprocessed_path: str | None = None,
    checksum: str | None = None,
) -> str | None:
    """Record (upsert) a file ingested into a knowledge graph.

    Ingested rows are scoped to the graph (``graphName`` set,
    ``threadId`` NULL) and are **permanent** — they are not deletable from
    the sidebar. Dedup: if an ingested row with the same
    ``(graphName, name)`` exists, it is updated in place (paths/source
    refreshed, ``ingestedAt`` bumped) and its id returned — re-ingesting
    the same file into the same graph does not create a duplicate.

    Returns the row id, or ``None`` on a non-fatal failure.
    """
    await _ensure_documents_table()
    row_id = _new_id()
    ingested_at = _utc_now_iso()
    try:
        async with _engine().begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT "id" FROM documents
                        WHERE "stage" = :stage
                          AND "graphName" = :graphName
                          AND "name" = :name
                        LIMIT 1
                        """
                    ),
                    {
                        "stage": STAGE_INGESTED,
                        "graphName": graph_name,
                        "name": name,
                    },
                )
            ).fetchone()
            if existing is not None:
                row_id = existing[0]
                await conn.execute(
                    text(
                        """
                        UPDATE documents
                           SET "source" = COALESCE(:source, "source"),
                               "originalPath" = COALESCE(:originalPath, "originalPath"),
                               "preprocessedPath" = COALESCE(:preprocessedPath, "preprocessedPath"),
                               "checksum" = COALESCE(:checksum, "checksum"),
                               "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier"),
                               "ingestedAt" = :ingestedAt
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": row_id,
                        "source": source,
                        "originalPath": original_path,
                        "preprocessedPath": preprocessed_path,
                        "checksum": checksum,
                        "userIdentifier": user_identifier,
                        "ingestedAt": ingested_at,
                    },
                )
                return row_id
            await conn.execute(
                text(
                    """
                    INSERT INTO documents
                        ("id","userIdentifier","threadId","graphName","name",
                         "stage","originalPath","preprocessedPath","source",
                         "mime","bytes","checksum","ingestedAt","createdAt")
                    VALUES
                        (:id,:userIdentifier,NULL,:graphName,:name,
                         :stage,:originalPath,:preprocessedPath,:source,
                         NULL,NULL,:checksum,:ingestedAt,:createdAt)
                    """
                ),
                {
                    "id": row_id,
                    "userIdentifier": user_identifier,
                    "graphName": graph_name,
                    "name": name,
                    "stage": STAGE_INGESTED,
                    "originalPath": original_path,
                    "preprocessedPath": preprocessed_path,
                    "source": source,
                    "checksum": checksum,
                    "ingestedAt": ingested_at,
                    "createdAt": ingested_at,
                },
            )
        return row_id
    except Exception as exc:  # noqa: BLE001
        logger.error("register_ingested failed for %r: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# List helpers (sidebar source of truth)
# ---------------------------------------------------------------------------

async def list_for_thread(thread_id: str) -> list[dict[str, Any]]:
    """Return all uploaded + preprocessed rows for a chat thread.

    Ordered by ``createdAt`` ascending (oldest first) so the sidebar lists
    files in upload order. Returns ``[]`` on error (sidebar renders empty).
    """
    await _ensure_documents_table()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT * FROM documents
                         WHERE "threadId" = :threadId
                           AND "stage" IN ('uploaded','preprocessed')
                         ORDER BY "createdAt" ASC
                        """
                    ),
                    {"threadId": thread_id},
                )
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_for_thread failed: %s", exc)
        return []


async def list_for_graph(graph_name: str) -> list[dict[str, Any]]:
    """Return all ingested rows for a knowledge graph.

    Ordered by ``ingestedAt`` ascending. Returns ``[]`` on error.
    """
    await _ensure_documents_table()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT * FROM documents
                         WHERE "graphName" = :graphName
                           AND "stage" = 'ingested'
                         ORDER BY "ingestedAt" ASC
                        """
                    ),
                    {"graphName": graph_name},
                )
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_for_graph failed: %s", exc)
        return []


async def get(row_id: str) -> dict[str, Any] | None:
    """Return one document row by id, or ``None`` if not found."""
    await _ensure_documents_table()
    try:
        async with _engine().connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT * FROM documents WHERE "id" = :id'),
                    {"id": row_id},
                )
            ).fetchone()
        return _row_to_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get failed for %r: %s", row_id, exc)
        return None


# ---------------------------------------------------------------------------
# Delete / clear helpers
# ---------------------------------------------------------------------------

class IngestedDocumentNotDeletable(ValueError):
    """Raised by :func:`delete` when the row is an ingested (permanent) row.

    Ingested files remain tracked once they are in a graph; deletion of the
    already-ingested graph data is a separate concern handled elsewhere.
    """


async def delete(row_id: str, *, remove_file: bool = True) -> dict[str, Any] | None:
    """Delete a document row (and optionally its on-disk file).

    Only ``uploaded`` / ``preprocessed`` rows are deletable. Deleting an
    ``ingested`` row raises :class:`IngestedDocumentNotDeletable` — those
    rows are permanent and only removed by :func:`clear_ingested_for_graph`
    when the graph itself is reset.

    When ``remove_file`` is True (default), the on-disk file(s) referenced
    by the row are unlinked (``missing_ok=True`` so a manually-deleted file
    doesn't raise). Set to False for test paths that don't write files.

    Returns the deleted row (for the UI to confirm), or ``None`` if the row
    didn't exist. Raises :class:`IngestedDocumentNotDeletable` for ingested
    rows; other errors propagate.
    """
    await _ensure_documents_table()
    async with _engine().begin() as conn:
        row = (
            await conn.execute(
                text('SELECT * FROM documents WHERE "id" = :id'),
                {"id": row_id},
            )
        ).fetchone()
        if row is None:
            return None
        stage = row.stage
        if stage == STAGE_INGESTED:
            raise IngestedDocumentNotDeletable(
                f"Ingested document {row.name!r} in graph "
                f"{row.graphName!r} is permanent and cannot be "
                f"deleted from the sidebar. Use reset_graph to clear the graph."
            )
        await conn.execute(
            text('DELETE FROM documents WHERE "id" = :id'),
            {"id": row_id},
        )
    deleted = _row_to_dict(row)
    if remove_file:
        for key in ("originalPath", "preprocessedPath"):
            p = deleted.get(key)
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not unlink %s: %s", p, exc)
    return deleted


async def clear_ingested_for_graph(graph_name: str) -> int:
    """Delete all ingested rows for ``graph_name``.

    Called by the ``reset_graph`` tool after the graph data is wiped, so the
    registry reflects that the graph no longer contains those files. The
    on-disk original/preprocessed files are NOT touched (they remain
    re-usable for re-ingestion).

    Returns the number of rows deleted. Errors are logged and return 0.
    """
    await _ensure_documents_table()
    try:
        async with _engine().begin() as conn:
            result = await conn.execute(
                text(
                    """
                    DELETE FROM documents
                     WHERE "graphName" = :graphName
                       AND "stage" = 'ingested'
                    """
                ),
                {"graphName": graph_name},
            )
        return result.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.error("clear_ingested_for_graph failed for %r: %s", graph_name, exc)
        return 0


async def orphan_thread(thread_id: str) -> int:
    """Null ``threadId`` on all of a thread's rows.

    Called when a thread is deleted: the uploaded/preprocessed rows lose
    their thread link but the on-disk files stay so they remain re-usable
    by other chats and graphs. Ingested rows are unaffected (they're
    graph-scoped, not thread-scoped).

    Returns the number of rows updated. Errors are logged and return 0.
    """
    await _ensure_documents_table()
    try:
        async with _engine().begin() as conn:
            result = await conn.execute(
                text(
                    """
                    UPDATE documents
                       SET "threadId" = NULL
                     WHERE "threadId" = :threadId
                    """
                ),
                {"threadId": thread_id},
            )
        return result.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.error("orphan_thread failed for %r: %s", thread_id, exc)
        return 0


__all__ = [
    "STAGE_INGESTED",
    "STAGE_PREPROCESSED",
    "STAGE_UPLOADED",
    "IngestedDocumentNotDeletable",
    "checksum_file",
    "clear_ingested_for_graph",
    "delete",
    "get",
    "list_for_graph",
    "list_for_thread",
    "orphan_thread",
    "register_ingested",
    "register_preprocessed",
    "register_upload",
    "reset_engine_cache",
]