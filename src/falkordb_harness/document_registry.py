"""Document-management registry backed by the Chainlit SQLite data layer.

Two tables (created by :mod:`falkordb_harness.data_layer`):

``documents`` — one row per uploaded file, scoped to a chat thread:
    ``threadId`` set, ``graphName`` removed (no longer on this table).
    Columns track the original upload (``originalPath``, ``bytes``,
    ``checksum``) and an optional preprocessed Markdown output
    (``preprocessedPath``, ``preprocessedAt``). The sidebar derives the
    "Preprocessed ✓/✗" column from ``preprocessedPath IS NOT NULL``.

``document_ingestions`` — many-to-many link between ``documents`` rows and
    knowledge graphs. A file is "ingested" into a graph when a row exists
    here for the ``(documentId, graphName)`` pair. The sidebar derives the
    "Ingested ✓/✗" column for the active graph from the presence of a
    matching row. Resetting a graph deletes its ingestion rows (see
    :func:`clear_ingested_for_graph`) but leaves the ``documents`` row and
    its on-disk file intact, so the file can be re-ingested.

Dedup:

- Uploaded/preprocessed rows are deduplicated by ``(threadId, name)``:
  re-uploading or re-preprocessing the same file to the same thread
  updates the existing row in place rather than creating a duplicate.
  ``checksum`` additionally dedups uploads within a thread when the
  checksum is known.
- Ingestion rows are deduplicated by ``(documentId, graphName)``: re-
  ingesting the same file into the same graph bumps ``ingestedAt`` on the
  existing row rather than creating a duplicate.

On thread deletion, :func:`orphan_thread` deletes the thread's document
rows (and their ingestion rows via ``ON DELETE CASCADE``) along with their
per-session on-disk directories. Documents that have been ingested into a
graph are preserved as provenance: their ``threadId`` is set to NULL and
their on-disk files are kept, so the sidebar continues to show them as
ingested for the active graph.

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

_ENGINE: AsyncEngine | None = None


class IngestedDocumentNotDeletable(ValueError):
    """Raised by :func:`delete` when the row is currently ingested.

    A row is "ingested" when it has at least one ``document_ingestions``
    link. The delete path raises this so the UI can show the "permanent
    / reset the graph" message. Callers that want to forcibly delete
    (e.g. :func:`orphan_thread`) bypass this check by deleting the
    documents row directly, which cascades to its ingestion links.
    """


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


async def _ensure_tables() -> None:
    """Create the documents + document_ingestions tables if missing.

    Idempotent guard so the registry works in tests / early-startup paths
    before ``init_db`` has run. The canonical DDL lives in
    :data:`data_layer._DDL_STATEMENTS`; the copies here are kept in sync.
    """
    async with _engine().connect() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    "id"                TEXT PRIMARY KEY,
                    "userIdentifier"    TEXT,
                    "threadId"          TEXT,
                    "name"              TEXT NOT NULL,
                    "originalPath"      TEXT,
                    "preprocessedPath"  TEXT,
                    "preprocessedAt"    TEXT,
                    "mime"              TEXT,
                    "bytes"             INTEGER,
                    "checksum"          TEXT,
                    "createdAt"         TEXT NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_documents_thread
                    ON documents ("threadId")
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS document_ingestions (
                    "id"           TEXT PRIMARY KEY,
                    "documentId"   TEXT NOT NULL
                        REFERENCES documents ("id") ON DELETE CASCADE,
                    "graphName"    TEXT NOT NULL,
                    "source"       TEXT,
                    "ingestedAt"   TEXT NOT NULL,
                    "createdAt"    TEXT NOT NULL,
                    UNIQUE ("documentId", "graphName")
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ingestions_document
                    ON document_ingestions ("documentId")
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_ingestions_graph
                    ON document_ingestions ("graphName")
                """
            )
        )
        await conn.commit()


# Back-compat alias — older code/tests import ``_ensure_documents_table``.
async def _ensure_documents_table() -> None:
    await _ensure_tables()


# ---------------------------------------------------------------------------
# Register helpers
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

    Dedup: if a row with the same ``(threadId, checksum)`` (when checksum
    is known) or ``(threadId, name)`` already exists, it is updated in
    place (path/mime/bytes refreshed) and its id returned — re-uploading
    the same file to the same thread does not create a duplicate. When
    the preprocessed Markdown for an existing uploaded row is missing,
    this update preserves it (only ``originalPath`` / metadata are
    refreshed).

    ``thread_id`` may be ``None`` (e.g. uploads arriving before the
    thread id is known); in that case dedup falls back to ``checksum``
    alone among rows with ``threadId IS NULL``.

    Returns the row id, or ``None`` on a non-fatal failure (the error is
    logged). Upload tracking must never block the chat.
    """
    await _ensure_tables()
    row_id = _new_id()
    created = _utc_now_iso()
    try:
        async with _engine().begin() as conn:
            # Match by checksum first (more reliable), then by name.
            existing = None
            if checksum:
                existing = (
                    await conn.execute(
                        text(
                            """
                            SELECT "id" FROM documents
                             WHERE "checksum" IS NOT NULL
                               AND "checksum" = :checksum
                               AND ("threadId" IS :thread_null
                                    OR "threadId" = :thread_id)
                             LIMIT 1
                            """
                        ),
                        {
                            "checksum": checksum,
                            "thread_null": thread_id is None,
                            "thread_id": thread_id,
                        },
                    )
                ).fetchone()
            if existing is None:
                existing = (
                    await conn.execute(
                        text(
                            """
                            SELECT "id" FROM documents
                             WHERE "name" = :name
                               AND ("threadId" IS :thread_null
                                    OR "threadId" = :thread_id)
                             LIMIT 1
                            """
                        ),
                        {
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
                               "mime" = :mime,
                               "bytes" = :bytes,
                               "checksum" = COALESCE(:checksum, "checksum"),
                               "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier")
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": row_id,
                        "originalPath": original_path,
                        "mime": mime,
                        "bytes": bytes_size,
                        "checksum": checksum,
                        "userIdentifier": user_identifier,
                    },
                )
                return row_id
            await conn.execute(
                text(
                    """
                    INSERT INTO documents
                        ("id","userIdentifier","threadId","name",
                         "originalPath","preprocessedPath","preprocessedAt",
                         "mime","bytes","checksum","createdAt")
                    VALUES
                        (:id,:userIdentifier,:threadId,:name,
                         :originalPath,NULL,NULL,
                         :mime,:bytes,:checksum,:createdAt)
                    """
                ),
                {
                    "id": row_id,
                    "userIdentifier": user_identifier,
                    "threadId": thread_id,
                    "name": name,
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
    """Record (upsert) a docprep Markdown output for an uploaded original.

    Looks up the existing ``documents`` row for ``(threadId, name)`` — the
    preprocessed file shares the uploaded original's row, and the original
    name is what pairs them. When the row exists, ``preprocessedPath`` and
    ``preprocessedAt`` are set (or refreshed) and ``originalPath`` is
    updated to the latest source path. When no row exists yet (e.g.
    docprep ran before the upload was registered), a new row is inserted
    with both paths populated.

    Returns the row id, or ``None`` on a non-fatal failure.
    """
    await _ensure_tables()
    row_id = _new_id()
    created = _utc_now_iso()
    preprocessed_at = created
    try:
        async with _engine().begin() as conn:
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT "id" FROM documents
                         WHERE "name" = :name
                           AND ("threadId" IS :thread_null
                                OR "threadId" = :thread_id)
                         LIMIT 1
                        """
                    ),
                    {
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
                           SET "originalPath" = COALESCE(:originalPath, "originalPath"),
                               "preprocessedPath" = :preprocessedPath,
                               "preprocessedAt" = :preprocessedAt,
                               "checksum" = COALESCE(:checksum, "checksum"),
                               "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier")
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": row_id,
                        "originalPath": original_path,
                        "preprocessedPath": preprocessed_path,
                        "preprocessedAt": preprocessed_at,
                        "checksum": checksum,
                        "userIdentifier": user_identifier,
                    },
                )
                return row_id
            await conn.execute(
                text(
                    """
                    INSERT INTO documents
                        ("id","userIdentifier","threadId","name",
                         "originalPath","preprocessedPath","preprocessedAt",
                         "mime","bytes","checksum","createdAt")
                    VALUES
                        (:id,:userIdentifier,:threadId,:name,
                         :originalPath,:preprocessedPath,:preprocessedAt,
                         NULL,NULL,:checksum,:createdAt)
                    """
                ),
                {
                    "id": row_id,
                    "userIdentifier": user_identifier,
                    "threadId": thread_id,
                    "name": name,
                    "originalPath": original_path,
                    "preprocessedPath": preprocessed_path,
                    "preprocessedAt": preprocessed_at,
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
    document_id: str | None = None,
) -> str | None:
    """Record that a document has been ingested into a knowledge graph.

    Links the existing ``documents`` row (looked up by ``document_id``, or
    by ``(threadId, name)`` using the current Chainlit session, or by
    ``original_path``/``preprocessed_path`` as fallbacks) to ``graph_name``
    via a new (or upserted) ``document_ingestions`` row. When no
    ``documents`` row exists yet, a fresh one is inserted (best-effort:
    the registry's job is tracking, not blocking ingestion).

    ``document_id`` is the preferred lookup key when the caller already
    has the row id (e.g. the Ingest button path resolves the uploaded
    rows first). When ``document_id`` is None, the lookup uses the
    session's ``threadId`` from ``cl.context`` (best-effort: imported
    lazily so the function also works in non-Chainlit contexts).

    Dedup: if an ingestion row with the same ``(documentId, graphName)``
    exists, it is updated in place (``source`` refreshed, ``ingestedAt``
    bumped) and its id returned — re-ingesting the same file into the
    same graph does not create a duplicate.

    Returns the ingestion row id, or ``None`` on a non-fatal failure.
    """
    await _ensure_tables()
    try:
        async with _engine().begin() as conn:
            doc_id = document_id
            if doc_id is None:
                # Try to resolve via the current Chainlit session thread id.
                thread_id = None
                try:
                    import chainlit as cl  # noqa: PLC0415

                    thread_id = cl.context.session.thread_id  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 — no Chainlit context
                    thread_id = None
                # Match by name within the thread, then fall back to path.
                row = None
                if thread_id is not None:
                    row = (
                        await conn.execute(
                            text(
                                """
                                SELECT "id" FROM documents
                                 WHERE "name" = :name
                                   AND "threadId" = :threadId
                                 LIMIT 1
                                """
                            ),
                            {"name": name, "threadId": thread_id},
                        )
                    ).fetchone()
                if row is None and (original_path or preprocessed_path):
                    if preprocessed_path:
                        row = (
                            await conn.execute(
                                text(
                                    """
                                    SELECT "id" FROM documents
                                     WHERE "preprocessedPath" = :path
                                     LIMIT 1
                                    """
                                ),
                                {"path": preprocessed_path},
                            )
                        ).fetchone()
                    if row is None and original_path:
                        row = (
                            await conn.execute(
                                text(
                                    """
                                    SELECT "id" FROM documents
                                     WHERE "originalPath" = :path
                                     LIMIT 1
                                    """
                                ),
                                {"path": original_path},
                            )
                        ).fetchone()
                if row is None:
                    # No existing documents row — insert a minimal one so
                    # the ingestion link has something to point at. This
                    # is the registry's "track, don't block" fallback.
                    doc_id = _new_id()
                    created = _utc_now_iso()
                    await conn.execute(
                        text(
                            """
                            INSERT INTO documents
                                ("id","userIdentifier","threadId","name",
                                 "originalPath","preprocessedPath",
                                 "preprocessedAt","mime","bytes","checksum",
                                 "createdAt")
                            VALUES
                                (:id,:userIdentifier,NULL,:name,
                                 :originalPath,:preprocessedPath,
                                 NULL,NULL,NULL,:checksum,:createdAt)
                            """
                        ),
                        {
                            "id": doc_id,
                            "userIdentifier": user_identifier,
                            "name": name,
                            "originalPath": original_path,
                            "preprocessedPath": preprocessed_path,
                            "checksum": checksum,
                            "createdAt": created,
                        },
                    )
                else:
                    doc_id = row[0]
                    # Refresh userIdentifier if newly provided.
                    if user_identifier:
                        await conn.execute(
                            text(
                                """
                                UPDATE documents
                                   SET "userIdentifier" = COALESCE(:userIdentifier, "userIdentifier"),
                                       "originalPath" = COALESCE(:originalPath, "originalPath"),
                                       "preprocessedPath" = COALESCE(:preprocessedPath, "preprocessedPath")
                                 WHERE "id" = :id
                                """
                            ),
                            {
                                "id": doc_id,
                                "userIdentifier": user_identifier,
                                "originalPath": original_path,
                                "preprocessedPath": preprocessed_path,
                            },
                        )
            # Upsert the ingestion link.
            ing_id = _new_id()
            ingested_at = _utc_now_iso()
            existing = (
                await conn.execute(
                    text(
                        """
                        SELECT "id" FROM document_ingestions
                         WHERE "documentId" = :documentId
                           AND "graphName" = :graphName
                         LIMIT 1
                        """
                    ),
                    {"documentId": doc_id, "graphName": graph_name},
                )
            ).fetchone()
            if existing is not None:
                ing_id = existing[0]
                await conn.execute(
                    text(
                        """
                        UPDATE document_ingestions
                           SET "source" = COALESCE(:source, "source"),
                               "ingestedAt" = :ingestedAt
                         WHERE "id" = :id
                        """
                    ),
                    {
                        "id": ing_id,
                        "source": source,
                        "ingestedAt": ingested_at,
                    },
                )
                return ing_id
            await conn.execute(
                text(
                    """
                    INSERT INTO document_ingestions
                        ("id","documentId","graphName","source",
                         "ingestedAt","createdAt")
                    VALUES
                        (:id,:documentId,:graphName,:source,
                         :ingestedAt,:createdAt)
                    """
                ),
                {
                    "id": ing_id,
                    "documentId": doc_id,
                    "graphName": graph_name,
                    "source": source,
                    "ingestedAt": ingested_at,
                    "createdAt": ingested_at,
                },
            )
        return ing_id
    except Exception as exc:  # noqa: BLE001
        logger.error("register_ingested failed for %r: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# List helpers (sidebar source of truth)
# ---------------------------------------------------------------------------

async def list_for_thread(thread_id: str) -> list[dict[str, Any]]:
    """Return all document rows for a chat thread.

    Ordered by ``createdAt`` ascending (oldest first) so the sidebar
    lists files in upload order. Each row carries the documents-table
    columns plus a boolean ``preprocessed`` (``preprocessedPath IS NOT
    NULL``) and a NULL ``ingestedAt`` placeholder (ingestion status is
    graph-scoped and joined separately in
    :func:`_build_document_manager_props`).

    Returns ``[]`` on error (sidebar renders empty).
    """
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT *,
                               ("preprocessedPath" IS NOT NULL) AS preprocessed
                          FROM documents
                         WHERE "threadId" = :threadId
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
    """Return all document rows ingested into ``graph_name``.

    Joins ``documents`` with ``document_ingestions`` on ``documentId``,
    so each returned dict carries both the document columns and the
    ingestion's ``ingestedAt`` / ``source``. Ordered by
    ``document_ingestions.createdAt`` ascending (ingestion order).
    Returns ``[]`` on error.
    """
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT d.*,
                               di."id" AS ingestionId,
                               di."source" AS source,
                               di."ingestedAt" AS ingestedAt,
                               (d."preprocessedPath" IS NOT NULL) AS preprocessed
                          FROM documents d
                          JOIN document_ingestions di
                            ON di."documentId" = d."id"
                         WHERE di."graphName" = :graphName
                         ORDER BY di."createdAt" ASC
                        """
                    ),
                    {"graphName": graph_name},
                )
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_for_graph failed: %s", exc)
        return []


async def list_for_user(user_identifier: str) -> list[dict[str, Any]]:
    """Return all document rows owned by ``user_identifier``.

    User-scoped counterpart to :func:`count_for_user` — returns the actual
    rows (count_for_user only returns the count). Covers uploaded +
    preprocessed rows across ALL the user's chat threads, so the document
    sidebar can show files the user uploaded in a previous thread even
    when the current thread is empty. Without this the floating toggle
    button (visibility driven by ``/api/docs-info``, which calls
    ``count_for_user``) would be visible while the sidebar opened empty
    — the button click did nothing because :func:`_build_document_manager_props`
    only looked at the current thread and returned ``None``.

    Ordered by ``createdAt`` ascending (oldest first). Each row carries
    the documents-table columns plus a boolean ``preprocessed``
    (``preprocessedPath IS NOT NULL``). Returns ``[]`` on error.
    """
    if not user_identifier:
        return []
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT *,
                               ("preprocessedPath" IS NOT NULL) AS preprocessed
                          FROM documents
                         WHERE "userIdentifier" = :u
                         ORDER BY "createdAt" ASC
                        """
                    ),
                    {"u": user_identifier},
                )
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_for_user failed: %s", exc)
        return []


async def list_ingested_graphs_for(document_id: str) -> list[str]:
    """Return the graph names a document has been ingested into."""
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT "graphName" FROM document_ingestions
                         WHERE "documentId" = :documentId
                         ORDER BY "createdAt" ASC
                        """
                    ),
                    {"documentId": document_id},
                )
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("list_ingested_graphs_for failed: %s", exc)
        return []


async def count_for_user(user_identifier: str) -> int:
    """Count document rows owned by ``user_identifier``.

    Covers uploaded + preprocessed rows (both stages live in the
    ``documents`` table; ``userIdentifier`` is set on registration and
    refreshed when the user re-appears). Used by the ``/api/docs-info``
    endpoint so the floating toggle button (public/docs_toggle.js) can
    decide whether to show itself even before any ingestion has happened
    (uploaded-but-not-ingested files). Returns ``0`` on error.
    """
    if not user_identifier:
        return 0
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM documents
                         WHERE "userIdentifier" = :u
                        """
                    ),
                    {"u": user_identifier},
                )
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001
        logger.error("count_for_user failed: %s", exc)
        return 0


async def get(row_id: str) -> dict[str, Any] | None:
    """Return one document row by id, or ``None`` if not found."""
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT *,
                               ("preprocessedPath" IS NOT NULL) AS preprocessed
                          FROM documents
                         WHERE "id" = :id
                        """
                    ),
                    {"id": row_id},
                )
            ).fetchone()
        return _row_to_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get failed for %r: %s", row_id, exc)
        return None


async def get_ingestion(document_id: str, graph_name: str) -> dict[str, Any] | None:
    """Return the ingestion row linking ``document_id`` to ``graph_name``."""
    await _ensure_tables()
    try:
        async with _engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT * FROM document_ingestions
                         WHERE "documentId" = :documentId
                           AND "graphName" = :graphName
                         LIMIT 1
                        """
                    ),
                    {"documentId": document_id, "graphName": graph_name},
                )
            ).fetchone()
        return _row_to_dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("get_ingestion failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Delete / clear helpers
# ---------------------------------------------------------------------------

def _maybe_rmdir_if_empty(stored_path: str | None) -> None:
    """Remove the parent directory of ``stored_path`` if it is empty.

    With per-session on-disk isolation, a deleted file's parent is the
    ``originals/<thread_id>/`` or ``preprocessed/<thread_id>/`` directory.
    Removing it when empty keeps the tree tidy after the last file of a
    session is deleted (the top-level ``originals/`` / ``preprocessed/``
    roots are never removed — only session subdirs, which sit one level
    below a file). Missing paths / non-empty dirs are ignored.
    """
    if not stored_path:
        return
    try:
        parent = Path(stored_path).resolve().parent
        parent.rmdir()
    except (OSError, ValueError):
        pass


async def clear_ingested_for_graph(graph_name: str) -> int:
    """Delete all ingestion rows for ``graph_name``.

    Called by the ``reset_graph`` tool after the graph data is wiped, so
    the registry reflects that the graph no longer contains those files.
    The ``documents`` rows and their on-disk files are NOT touched (they
    remain re-usable for re-ingestion).

    Returns the number of ingestion rows deleted. Errors are logged and
    return 0.
    """
    await _ensure_tables()
    try:
        async with _engine().begin() as conn:
            result = await conn.execute(
                text(
                    """
                    DELETE FROM document_ingestions
                     WHERE "graphName" = :graphName
                    """
                ),
                {"graphName": graph_name},
            )
        return result.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.error("clear_ingested_for_graph failed for %r: %s", graph_name, exc)
        return 0


async def orphan_thread(thread_id: str) -> int:
    """Delete a thread's on-disk session dirs and its document rows.

    Called when a thread is deleted. With per-session on-disk isolation
    (``originals/<thread_id>/`` and ``preprocessed/<thread_id>/``), the
    session's files are removed along with their registry rows so no
    stale files remain and the on-disk layout keeps reflecting live
    sessions only.

    Documents that have been ingested into a graph are **preserved** as
    provenance: their ``threadId`` is set to NULL (so they no longer
    belong to the deleted thread) and their on-disk files are kept (the
    session-dir cleanup skips files still referenced by preserved rows).
    Their ingestion rows (in ``document_ingestions``) are untouched, so
    the sidebar continues to show them as ingested for the active graph.
    Documents with no ingestion links are deleted entirely.

    Returns the number of document rows deleted (those without
    ingestion links). Errors are logged and return 0.
    """
    import shutil

    from falkordb_harness.tools._paths import originals_dir, preprocessed_dir

    await _ensure_tables()
    deleted_rows = 0
    try:
        async with _engine().begin() as conn:
            # Preserve rows that have been ingested into any graph —
            # null their threadId so they detach from this thread but
            # keep their files + ingestion links for graph-scoped views.
            await conn.execute(
                text(
                    """
                    UPDATE documents
                       SET "threadId" = NULL
                     WHERE "threadId" = :threadId
                       AND "id" IN (
                           SELECT "documentId" FROM document_ingestions
                       )
                    """
                ),
                {"threadId": thread_id},
            )
            # Delete rows with no ingestion links (pure thread-scoped
            # uploads). Their on-disk files are unlinked by the caller-
            # side shutil.rmtree below.
            result = await conn.execute(
                text(
                    """
                    DELETE FROM documents
                     WHERE "threadId" = :threadId
                       AND "id" NOT IN (
                           SELECT "documentId" FROM document_ingestions
                       )
                    """
                ),
                {"threadId": thread_id},
            )
            deleted_rows = result.rowcount or 0
        # Remove the per-session on-disk directories. best-effort: a
        # missing or non-empty dir (e.g. files preserved by the update
        # above, or files added out-of-band) is silently skipped.
        for root in (originals_dir(), preprocessed_dir()):
            session_dir = root / thread_id
            if session_dir.is_dir():
                shutil.rmtree(session_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("orphan_thread failed for %r: %s", thread_id, exc)
        return 0
    return deleted_rows


__all__ = [
    "IngestedDocumentNotDeletable",
    "checksum_file",
    "clear_ingested_for_graph",
    "delete",
    "get",
    "get_ingestion",
    "list_for_graph",
    "list_for_thread",
    "list_ingested_graphs_for",
    "orphan_thread",
    "register_ingested",
    "register_preprocessed",
    "register_upload",
    "reset_engine_cache",
]


async def delete(
    row_id: str,
    *,
    remove_file: bool = True,
    force: bool = False,
) -> dict[str, Any] | None:
    """Delete a document row (and optionally its on-disk files).

    Deleting a ``documents`` row also removes its ``document_ingestions``
    rows (manual cascade — SQLite doesn't enforce FK CASCADE without
    ``PRAGMA foreign_keys=ON`` and the engine is shared with Chainlit),
    so the file disappears from every graph it was ingested into.

    Unless ``force=True``, raises :class:`IngestedDocumentNotDeletable`
    when the row currently has any ingestion link — the UI treats
    ingested files as permanent (the JSX hides the Delete button for
    them, and this is the defensive fallback). ``force=True`` is used
    by :func:`orphan_thread` to delete rows whose thread is gone.

    When ``remove_file`` is True (default), the on-disk file(s)
    referenced by the row are unlinked (``missing_ok=True`` so a
    manually-deleted file doesn't raise). Set to False for test paths
    that don't write files.

    Returns the deleted row (for the UI to confirm), or ``None`` if the
    row didn't exist. Other errors propagate.
    """
    await _ensure_tables()
    async with _engine().begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT *,
                           ("preprocessedPath" IS NOT NULL) AS preprocessed
                      FROM documents
                     WHERE "id" = :id
                    """
                ),
                {"id": row_id},
            )
        ).fetchone()
        if row is None:
            return None
        if not force:
            ing = (
                await conn.execute(
                    text(
                        """
                        SELECT 1 FROM document_ingestions
                         WHERE "documentId" = :id
                         LIMIT 1
                        """
                    ),
                    {"id": row_id},
                )
            ).fetchone()
            if ing is not None:
                raise IngestedDocumentNotDeletable(
                    f"Document {row.name!r} is ingested into a knowledge "
                    f"graph and cannot be deleted from the sidebar. Use "
                    f"reset_graph to clear the graph first."
                )
        await conn.execute(
            text('DELETE FROM document_ingestions WHERE "documentId" = :id'),
            {"id": row_id},
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
        _maybe_rmdir_if_empty(deleted.get("originalPath"))
        _maybe_rmdir_if_empty(deleted.get("preprocessedPath"))
    return deleted