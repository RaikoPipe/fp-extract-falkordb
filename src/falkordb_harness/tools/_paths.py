"""Shared filesystem path resolution for all tools.

A single :class:`FilesystemBackend` rooted at ``DATA_DIR`` (default
``./data``) exposes both the ``originals/`` raw-source tree and the
``preprocessed/`` Markdown tree under one virtual root, so the agent's
built-in ``ls`` / ``read_file`` / ``glob`` / ``grep`` and the custom
``file_metadata`` / ``read_excerpt`` / ``preprocess_document`` /
``chunk_documents`` / ``extract_and_write`` tools all resolve identical
paths. Previously the backend was rooted only at ``ORIGINALS_DIR``, which
made ``PREPROCESSED_DIR`` invisible to the agent's filesystem tools — the
agent could not read the Markdown it had just produced, so it looped
re-calling ``file_metadata`` / ``glob`` until it hit the LangGraph
recursion limit.

Env vars:
- ``DATA_DIR``: the single filesystem root (default ``./data``).
- ``ORIGINALS_DIR``: raw uploaded/source files. Defaults to
  ``DATA_DIR/originals``. Chainlit uploads land here, under a per-session
  subdirectory named after the Chainlit thread id (``originals/<thread_id>/``);
  uploads with no thread context (CLI / pre-session) land in
  ``originals/_unscoped/``.
- ``PREPROCESSED_DIR``: docprep Markdown output. Defaults to
  ``DATA_DIR/preprocessed``. ``chunk_documents`` / ``extract_and_write``
  read from here by default. Output is written under a per-session
  subdirectory (``preprocessed/<thread_id>/``), mirroring the originals tree.

The per-session subdirectory layout mirrors the document registry's
``threadId`` discrimination (:mod:`falkordb_harness.document_registry`), so
the agent's ``ls`` / ``glob`` tools expose session ownership directly and
the LLM can avoid touching files from sessions other than the current one.

All incoming paths are Unicode-normalized to NFC before resolution so
non-ASCII filenames (e.g. ``Technologieübersicht.pptx``) resolve
consistently regardless of how the client normalized them.
"""

from __future__ import annotations

import os
import unicodedata
from functools import lru_cache
from pathlib import Path


def data_dir() -> Path:
    """Return the resolved ``DATA_DIR`` (the single filesystem root)."""
    return Path(os.getenv("DATA_DIR", "./data")).resolve()


def originals_dir() -> Path:
    """Return the resolved ``ORIGINALS_DIR`` (raw sources), created if missing."""
    p = Path(os.getenv("ORIGINALS_DIR", str(data_dir() / "originals"))).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def preprocessed_dir() -> Path:
    """Return the resolved ``PREPROCESSED_DIR`` (Markdown output), created if missing."""
    p = Path(os.getenv("PREPROCESSED_DIR", str(data_dir() / "preprocessed"))).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


# Sentinel used as the on-disk subdirectory for files that belong to no
# session (e.g. CLI uploads, or uploads arriving before the Chainlit thread
# id is known). Reserved: a real Chainlit thread id is a UUID and will never
# equal this value, but we reject it defensively in the helpers below.
_UNSCOPED = "_unscoped"


def thread_originals_dir(thread_id: str | None) -> Path:
    """Return the per-session originals directory, created if missing.

    ``thread_id is None`` (CLI / pre-session uploads) resolves to
    ``originals/_unscoped``. Otherwise resolves to ``originals/<thread_id>``.
    The on-disk layout mirrors the registry's ``threadId`` discrimination so
    the agent's ``ls`` / ``glob`` tools see session ownership directly.
    """
    if thread_id == _UNSCOPED:
        raise ValueError(
            f"Invalid thread id {thread_id!r}: '_unscoped' is a reserved sentinel."
        )
    p = originals_dir() / (thread_id if thread_id is not None else _UNSCOPED)
    p.mkdir(parents=True, exist_ok=True)
    return p


def thread_preprocessed_dir(thread_id: str | None) -> Path:
    """Return the per-session preprocessed directory, created if missing.

    ``thread_id is None`` (CLI / pre-session) resolves to
    ``preprocessed/_unscoped``. Otherwise resolves to
    ``preprocessed/<thread_id>``.
    """
    if thread_id == _UNSCOPED:
        raise ValueError(
            f"Invalid thread id {thread_id!r}: '_unscoped' is a reserved sentinel."
        )
    p = preprocessed_dir() / (thread_id if thread_id is not None else _UNSCOPED)
    p.mkdir(parents=True, exist_ok=True)
    return p


@lru_cache(maxsize=1)
def fs_backend():
    """Return the shared ``FilesystemBackend`` rooted at ``DATA_DIR``.

    Both ``originals/`` and ``preprocessed/`` are visible as subdirectories
    under this root, so the agent can inspect raw sources and preprocessed
    Markdown with the same tool set. ``virtual_mode=True`` keeps path
    traversal contained within ``DATA_DIR``.
    """
    from deepagents.backends.filesystem import FilesystemBackend

    return FilesystemBackend(root_dir=str(data_dir()), virtual_mode=True)


def _normalize(path: str) -> str:
    """Normalize a path string to NFC for consistent non-ASCII handling."""
    return unicodedata.normalize("NFC", path)


def resolve(path: str) -> Path | str:
    """Resolve ``path`` under ``DATA_DIR`` via the backend's containment.

    Returns a ``Path`` on success or an error string on failure (never
    raises). The input is NFC-normalized first so decomposed Unicode
    filenames resolve the same as composed ones.
    """
    try:
        return fs_backend()._resolve_path(_normalize(path))
    except (OSError, ValueError, RuntimeError) as exc:
        return f"Error resolving path '{path}': {exc}"


def virtual_path(absolute: Path) -> str:
    """Return the root-relative virtual path for an absolute path under DATA_DIR.

    Used to produce agent-passable paths (e.g. ``preprocessed/foo.md``)
    from absolute on-disk locations, so a tool's returned ``output_path``
    can be fed straight back into ``read_excerpt`` / ``file_metadata``.
    Falls back to the absolute string if the path is outside the root.
    """
    try:
        rel = absolute.resolve().relative_to(data_dir())
    except ValueError:
        return str(absolute)
    return str(rel).replace("\\", "/")