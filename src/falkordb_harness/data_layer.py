"""Chainlit data layer + local binary-element storage for chat persistence.

Registers a :class:`SQLAlchemyDataLayer` (Chainlit's built-in async SQL
backend) pointed at a SQLite database under ``DATA_DIR`` so that users,
threads, steps (messages) and elements are persisted across restarts.
Uploaded files attached to a persisted thread are written to a local
``ELEMENTS_DIR`` tree via :class:`LocalStorageClient` (Chainlit's
``BaseStorageClient`` interface) — no S3/Azure/GCS dependency required.

The DDL for the tables Chainlit's SQLAlchemy layer expects is defined in
:func:`init_db` and run once at startup (Chainlit does NOT auto-create
tables). The column names and quoting match the queries in
``chainlit/data/sql_alchemy.py`` verbatim — changing them will break
persistence.

Env vars:
- ``DATABASE_URL``: SQLAlchemy async URL. Defaults to
  ``sqlite+aiosqlite:///./data/chainlit.db``.
- ``ELEMENTS_DIR``: where to store uploaded-file blobs. Defaults to
  ``<DATA_DIR>/elements``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiofiles
from chainlit.data import BaseDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import queue_until_user_message

logger = logging.getLogger("falkordb_harness.data_layer")


def _database_url() -> str:
    """Return the SQLAlchemy async connection URL, with a sane default."""
    return os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/chainlit.db")


def _elements_dir() -> Path:
    """Return the resolved elements directory, created if missing."""
    default = Path(os.getenv("DATA_DIR", "./data")) / "elements"
    p = Path(os.getenv("ELEMENTS_DIR", str(default))).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


class LocalStorageClient(BaseStorageClient):
    """Filesystem-backed ``BaseStorageClient`` for binary element blobs.

    Each element is written to ``<ELEMENTS_DIR>/<object_key>`` (the
    object key already encodes ``user_id/element_id/name`` per
    ``SQLAlchemyDataLayer.create_element``). Reads are served back as a
    relative URL the Chainlit frontend can fetch via the app's static
    element route. When the file cannot be served (no static mount), the
    absolute file path is returned so the element is at least traceable.

    This client is intentionally minimal: no expiry, no signing, no
    remote calls — it exists so persisted threads keep their uploaded
    file attachments without requiring an object-store subscription.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _full(self, object_key: str) -> Path:
        return (self.root / object_key).resolve()

    async def upload_file(
        self,
        object_key: str,
        data: bytes | str,
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> dict[str, Any]:
        path = self._full(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
        async with aiofiles.open(path, "wb") as f:
            await f.write(payload)
        return {
            "object_key": object_key,
            "url": self._url_for(object_key),
        }

    async def delete_file(self, object_key: str) -> bool:
        path = self._full(object_key)
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            logger.warning("LocalStorageClient delete failed for %s: %s", object_key, exc)
            return False

    async def get_read_url(self, object_key: str) -> str:
        return self._url_for(object_key)

    async def close(self) -> None:
        return

    def _url_for(self, object_key: str) -> str:
        """Return a fetchable URL for an element blob.

        The Chainlit frontend fetches elements by the ``url`` stored on
        the element row. We expose elements under the app's
        ``/public/files/<object_key>`` path; a static-file route is
        mounted on this prefix by the auth module at startup. If the
        object key is not under our root (defensive), fall back to the
        absolute file path so the blob is at least discoverable.
        """
        return f"/public/files/{object_key}"


_DDL_STATEMENTS: tuple[str, ...] = (
    # users — Chainlit's user table extended with password auth, roles,
    # account lifecycle, and reset/verify tokens. Added columns are nullable
    # so Chainlit's own queries keep working unchanged.
    """
    CREATE TABLE IF NOT EXISTS users (
        "id" TEXT PRIMARY KEY,
        "identifier" TEXT UNIQUE NOT NULL,
        "createdAt" TEXT NOT NULL,
        "metadata" TEXT DEFAULT '{}',
        "passwordHash" TEXT,
        "email" TEXT,
        "role" TEXT NOT NULL DEFAULT 'user',
        "accountStatus" TEXT NOT NULL DEFAULT 'pending',
        "displayName" TEXT,
        "emailVerifiedAt" TEXT,
        "emailVerifyToken" TEXT,
        "emailVerifyExpires" TEXT,
        "passwordResetToken" TEXT,
        "passwordResetExpires" TEXT
    )
    """,
    # Unique email index (partial — only enforced when email is set, so
    # legacy/seed rows with NULL email don't collide).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
        ON users ("email") WHERE "email" IS NOT NULL
    """,
    # threads — one row per chat thread, owned by a user.
    """
    CREATE TABLE IF NOT EXISTS threads (
        "id" TEXT PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" TEXT,
        "userIdentifier" TEXT,
        "tags" TEXT,
        "metadata" TEXT DEFAULT '{}'
    )
    """,
    # steps — column set must cover EVERY StepDict key: the SQLAlchemy
    # layer builds INSERT columns dynamically from StepDict, so a missing
    # column raises OperationalError. Keep in sync with chainlit/step.py.
    """
    CREATE TABLE IF NOT EXISTS steps (
        "id" TEXT PRIMARY KEY,
        "name" TEXT,
        "type" TEXT,
        "threadId" TEXT,
        "parentId" TEXT,
        "command" TEXT,
        "modes" TEXT,
        "streaming" INTEGER,
        "waitForAnswer" INTEGER,
        "isError" INTEGER,
        "metadata" TEXT DEFAULT '{}',
        "tags" TEXT,
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" TEXT DEFAULT '{}',
        "showInput" TEXT,
        "defaultOpen" INTEGER,
        "autoCollapse" INTEGER,
        "language" TEXT,
        "icon" TEXT
    )
    """,
    # elements — files / images / data attached to steps.
    """
    CREATE TABLE IF NOT EXISTS elements (
        "id" TEXT PRIMARY KEY,
        "threadId" TEXT,
        "type" TEXT,
        "chainlitKey" TEXT,
        "url" TEXT,
        "objectKey" TEXT,
        "name" TEXT,
        "display" TEXT,
        "size" INTEGER,
        "language" TEXT,
        "page" INTEGER,
        "autoPlay" INTEGER,
        "playerConfig" TEXT,
        "forId" TEXT,
        "mime" TEXT,
        "props" TEXT DEFAULT '{}'
    )
    """,
    # feedbacks — thumbs-up/down on steps.
    """
    CREATE TABLE IF NOT EXISTS feedbacks (
        "id" TEXT PRIMARY KEY,
        "forId" TEXT,
        "value" INTEGER,
        "comment" TEXT
    )
    """,
    # documents — document-management registry. Scoped to a thread via
    # threadId (NULLed on thread deletion when the file remains ingested);
    # checksum (sha256) dedups uploads within a thread.
    # See falkordb_harness.document_registry for the CRUD API.
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
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_documents_thread
        ON documents ("threadId")
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_documents_checksum
        ON documents ("checksum")
    """,
    # document_ingestions — many-to-many link between documents and graphs.
    # The document row is NOT deleted on graph reset; only the link is
    # (clear_ingested_for_graph), so the file remains re-ingestable.
    # ON DELETE CASCADE from the document row keeps records from outliving it.
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
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ingestions_document
        ON document_ingestions ("documentId")
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ingestions_graph
        ON document_ingestions ("graphName")
    """,
    # graph_descriptions — LLM-maintained description per graph. Seeded at
    # create_graph, revised after each ingestion. Keyed by graph name;
    # rows are created on demand and never auto-deleted (graph reset does
    # NOT clear the description).
    """
    CREATE TABLE IF NOT EXISTS graph_descriptions (
        "name"        TEXT PRIMARY KEY,
        "description" TEXT NOT NULL DEFAULT '',
        "updatedAt"   TEXT NOT NULL
    )
    """,
)


# Columns that may be missing from ``steps``/``elements``/``users`` tables in
# older DBs. SQLite lacks ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``;
# ``_migrate_columns`` checks ``PRAGMA table_info`` and adds only what's
# absent. Forward-only. New StepDict columns must be added here + _DDL_STATEMENTS.
_MIGRATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("steps", "command", "TEXT"),
    ("steps", "modes", "TEXT"),
    ("steps", "defaultOpen", "INTEGER"),
    ("steps", "autoCollapse", "INTEGER"),
    ("steps", "icon", "TEXT"),
    # elements — to_dict doesn't emit 'path', but defensive in case a
    # future version does.
    ("elements", "path", "TEXT"),
    # users — auth columns added for public-deployment account lifecycle
    # (email verification + admin approval), role-based access control,
    # and password-reset / email-verification tokens. Nullable/defaulted
    # so pre-existing rows and Chainlit's own queries are unaffected.
    ("users", "email", "TEXT"),
    ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
    ("users", "accountStatus", "TEXT NOT NULL DEFAULT 'pending'"),
    ("users", "displayName", "TEXT"),
    ("users", "emailVerifiedAt", "TEXT"),
    ("users", "emailVerifyToken", "TEXT"),
    ("users", "emailVerifyExpires", "TEXT"),
    ("users", "passwordResetToken", "TEXT"),
    ("users", "passwordResetExpires", "TEXT"),
)


async def _migrate_columns(conn) -> None:
    """Add any columns from ``_MIGRATION_COLUMNS`` missing from existing tables.

    Idempotent: queries ``PRAGMA table_info(<table>)`` for each table and
    only runs ``ALTER TABLE ... ADD COLUMN`` for columns not yet present.
    SQLite has no ``ADD COLUMN IF NOT EXISTS`` so this check is mandatory
    for re-runnable migrations. Wrapped per-statement so one failure
    (e.g. table not yet created) doesn't abort the rest.
    """
    from sqlalchemy import text

    existing_columns: dict[str, set[str]] = {}
    for table, _col, _typ in _MIGRATION_COLUMNS:
        if table not in existing_columns:
            try:
                rows = await conn.execute(text(f'PRAGMA table_info("{table}")'))
                existing_columns[table] = {row[1] for row in rows}
            except Exception:  # noqa: BLE001 — table may not exist yet
                existing_columns[table] = set()
    for table, col, typ in _MIGRATION_COLUMNS:
        if col not in existing_columns.get(table, set()):
            try:
                await conn.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ}')
                )
                logger.info("Migrated: added column %s.%s", table, col)
            except Exception as exc:  # noqa: BLE001 — already exists, etc.
                logger.debug("Migrate skip %s.%s: %s", table, col, exc)


async def init_db(data_layer: SQLAlchemyDataLayer) -> None:
    """Create Chainlit's expected tables if missing, then add any columns
    absent from older schemas.

    Uses the data layer's own async engine so the SQLite file (and its
    parent directory) is created on first run. Safe to call on every
    startup: ``CREATE TABLE IF NOT EXISTS`` is a no-op once tables exist,
    and ``_migrate_columns`` only adds columns that are missing. The
    column names match ``chainlit/data/sql_alchemy.py``'s queries and
    the full ``StepDict``/``ElementDict`` key sets.
    """
    from sqlalchemy import text

    async with data_layer.engine.connect() as conn:
        for stmt in _DDL_STATEMENTS:
            await conn.execute(text(stmt))
        await _migrate_columns(conn)
        await conn.commit()
    logger.info("Chainlit data layer tables ensured at %s", _database_url())


def _coerce_tags_to_json(value):
    """Serialize a step's ``tags`` list to a JSON string for SQLite binding.

    Chainlit's :meth:`SQLAlchemyDataLayer.create_step` builds its INSERT
    parameter dict straight from the ``StepDict`` and only JSON-serializes
    ``metadata`` / ``generation`` — not ``tags``. With the SQLite driver
    (``aiosqlite``) the ``tags`` column is ``TEXT``, and binding a Python
    ``list`` raises ``sqlite3.ProgrammingError: type 'list' is not
    supported``. This helper converts a list (or None) to a JSON string so
    it round-trips through the TEXT column; the matching
    :func:`_coerce_tags_from_json` deserializes it back on read.
    """
    import json

    if value is None:
        return None
    if isinstance(value, str):
        # Already a string — assume already JSON-encoded; leave as-is so we
        # don't double-encode. (Defensive: a previous version may have
        # written a raw string.)
        return value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value))
    return json.dumps(value)


def _coerce_tags_from_json(value):
    """Deserialize a step's ``tags`` JSON string back to a list on read.

    Mirrors :func:`_coerce_tags_to_json`. Returns the value unchanged when
    it is already a list (e.g. from a non-SQLite backend) or None, so the
    override is a no-op for deployments that store tags natively as JSON
    arrays (Postgres ``JSON`` columns return Python lists already).
    """
    import json

    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            # A raw (non-JSON) string — wrap as a single-element list so
            # the frontend still gets a list as it expects.
            return [value] if value else []
        return parsed if isinstance(parsed, list) else [parsed]
    return value


class TagsJsonSQLAlchemyDataLayer(SQLAlchemyDataLayer):
    """SQLAlchemy data layer that JSON-encodes the ``steps.tags`` list.

    Works around Chainlit's ``SQLAlchemyDataLayer.create_step`` not
    serializing ``tags`` before binding, which breaks SQLite (TEXT column
    + Python list -> ProgrammingError). Writes store ``tags`` as a JSON
    string; reads deserialize it back to a list so the frontend renders
    tag badges exactly as before. On Postgres (where ``tags`` is typically
    a ``JSON``/``JSONB`` column that already round-trips lists) the
    encode/decode pair is a harmless no-op (list in -> JSON string out on
    write is still valid JSON for a JSON column; list out on read skips
    re-parsing). The override is intentionally narrow — only ``tags`` is
    touched; everything else delegates to the parent.
    """

    @queue_until_user_message()
    async def create_step(self, step_dict):  # type: ignore[override]
        if step_dict.get("tags") is not None:
            step_dict = {**step_dict, "tags": _coerce_tags_to_json(step_dict["tags"])}
        # Call the parent's unwrapped create_step: this override already has
        # @queue_until_user_message, so the parent's decorator would double-wrap.
        parent_create_step = SQLAlchemyDataLayer.create_step.__wrapped__  # type: ignore[attr-defined]
        return await parent_create_step(self, step_dict)

    async def get_step(self, step_id):  # type: ignore[override]
        result = await super().get_step(step_id)
        if result is not None and result.get("tags") is not None:
            result["tags"] = _coerce_tags_from_json(result["tags"])  # type: ignore[index]
        return result

    async def get_all_user_threads(self, *args, **kwargs):  # type: ignore[override]
        threads = await super().get_all_user_threads(*args, **kwargs)
        for thread in threads or []:
            for step in thread.get("steps", []) or []:
                if step.get("tags") is not None:
                    step["tags"] = _coerce_tags_from_json(step["tags"])  # type: ignore[index]
        return threads

    async def get_favorite_steps(self, user_id):  # type: ignore[override]
        steps = await super().get_favorite_steps(user_id)
        for step in steps or []:
            if step.get("tags") is not None:
                step["tags"] = _coerce_tags_from_json(step["tags"])  # type: ignore[index]
        return steps


def build_data_layer() -> BaseDataLayer:
    """Construct and return the SQLAlchemy data layer with local storage.

    Used by ``@cl.data_layer`` so Chainlit's persistence hooks (thread
    save/resume, element upload, feedback) target our SQLite database.
    Returns a :class:`TagsJsonSQLAlchemyDataLayer` so the ``steps.tags``
    list is JSON-encoded for SQLite binding (see that class's docstring).
    """
    storage = LocalStorageClient(_elements_dir())
    layer = TagsJsonSQLAlchemyDataLayer(
        conninfo=_database_url(),
        storage_provider=storage,
        show_logger=bool(os.getenv("DATA_LAYER_DEBUG", "").lower() in ("1", "true", "yes")),
    )
    return layer