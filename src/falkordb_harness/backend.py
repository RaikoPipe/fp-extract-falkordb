"""Shared backend instances for all tools.

The getters cache a *successful* construction but never a *failed* one, so a
transient misconfiguration or env-parsing error at first call does not poison
the cache for the process lifetime: the next call retries construction. The
underlying :class:`FalkorDBBackend` opens its FalkorDB connection lazily on
first use (and reconnects on transient errors), so ``get_backend()`` itself
never performs network IO and is safe to call repeatedly.

Per-session graph selection (Chainlit UI) is supported via a
:mod:`contextvars` slot: :func:`set_session_backend` installs a session-scoped
backend bound to the user's chosen graph, and :func:`get_backend` returns it in
preference to the module-level default. Because Chainlit runs each user
session in its own asyncio task (and ``cl.user_session`` is itself
contextvar-backed), different sessions selecting different graphs get
isolated backends without coupling this module to Chainlit. The CLI path,
which never calls ``set_session_backend``, keeps using the module-level cache
unchanged.
"""

from __future__ import annotations

import contextvars
import os
import threading

from knowledge.falkordb_backend import FalkorDBBackend
from knowledge.search import GraphSearcher

_BACKEND: FalkorDBBackend | None = None
_SEARCHER: GraphSearcher | None = None
_LOCK = threading.Lock()

# Per-session backend override. Set by the Chainlit UI / agent builder when
# the user picks a knowledge graph; None falls back to the module-level cache.
# A contextvar (rather than a plain global) isolates concurrent user sessions
# in the same process.
_SESSION_BACKEND: contextvars.ContextVar[FalkorDBBackend | None] = contextvars.ContextVar(
    "falkordb_session_backend", default=None
)
_SESSION_SEARCHER: contextvars.ContextVar[GraphSearcher | None] = contextvars.ContextVar(
    "falkordb_session_searcher", default=None
)


def set_session_backend(backend: FalkorDBBackend | None) -> None:
    """Install (or clear) a session-scoped backend.

    When set, :func:`get_backend` returns this instance in preference to the
    module-level default. Pass ``None`` to revert to the default. The
    associated session searcher is also reset so :func:`get_searcher` rebuilds
    against the new backend.
    """
    _SESSION_BACKEND.set(backend)
    _SESSION_SEARCHER.set(None)


def clear_session_backend() -> None:
    """Convenience wrapper to clear the session-scoped backend."""
    _SESSION_BACKEND.set(None)
    _SESSION_SEARCHER.set(None)


def get_backend() -> FalkorDBBackend:
    """Return the effective :class:`FalkorDBBackend` for the current context.

    A session-scoped backend (installed via :func:`set_session_backend`, used
    by the Chainlit UI for per-user graph selection) wins over the
    module-level cache. The module-level cache (used by the CLI) is
    constructed lazily from ``FALKORDB_*`` env vars.

    A construction failure of the module-level cache is **not** cached: the
    exception propagates and the next call retries, so a transient env/load
    error doesn't permanently break every tool for the session.
    """
    session_backend = _SESSION_BACKEND.get()
    if session_backend is not None:
        return session_backend

    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
        _BACKEND = FalkorDBBackend(
            host=os.getenv("FALKORDB_HOST"),
            port=int(os.getenv("FALKORDB_PORT", "6379")) if os.getenv("FALKORDB_PORT") else None,
            graph_name=os.getenv("FALKORDB_GRAPH"),
            merge_mode=os.getenv("MERGE_MODE"),
            conflicts_log_path=os.getenv("CONFLICTS_LOG"),
        )
        return _BACKEND


def get_searcher() -> GraphSearcher:
    """Return the effective :class:`GraphSearcher` for the current context.

    Mirrors :func:`get_backend`: a session-scoped searcher is built once and
    cached in the contextvar (reset when the session backend changes). The
    module-level searcher is built from the module-level backend.

    Construction failures of the module-level searcher are not cached.
    """
    session_searcher = _SESSION_SEARCHER.get()
    if session_searcher is not None:
        return session_searcher
    session_backend = _SESSION_BACKEND.get()
    if session_backend is not None:
        searcher = GraphSearcher(
            session_backend,
            llm_model=os.getenv("LLM_MODEL"),
            api_base=os.getenv("OLLAMA_API_BASE"),
        )
        _SESSION_SEARCHER.set(searcher)
        return searcher

    global _SEARCHER
    if _SEARCHER is not None:
        return _SEARCHER
    with _LOCK:
        if _SEARCHER is not None:
            return _SEARCHER
        _SEARCHER = GraphSearcher(
            get_backend(),
            llm_model=os.getenv("LLM_MODEL"),
            api_base=os.getenv("OLLAMA_API_BASE"),
        )
        return _SEARCHER


def reset_backend_cache() -> None:
    """Drop the cached backend/searcher so the next call rebuilds them.

    Used by tests to force a fresh backend between cases, and by the reconnect
    path when an unrecoverable handle loss is detected. Does **not** clear the
    session-scoped contextvar backends (those are owned by their sessions).
    """
    global _BACKEND, _SEARCHER
    with _LOCK:
        _BACKEND = None
        _SEARCHER = None