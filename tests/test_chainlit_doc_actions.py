"""Tests for the per-document action callbacks (Open / Preprocess / Delete).

The ``DocumentManager`` sidebar renders three per-row buttons that dispatch
``@cl.action_callback`` handlers in :mod:`falkordb_harness.chainlit_app`:

- ``on_open_document``       — render the file inline as a Chainlit element.
- ``on_preprocess_document`` — run docprep on a single uploaded original.
- ``on_delete_document``     — remove the row + on-disk file via the registry.

These tests call the unwrapped decorated functions directly with a fake
:class:`chainlit.action.Action` (``payload={"id": ...}``) and a throwaway
SQLite registry, then assert the registry side effects and the chat messages
the handlers post. ``cl.user_session`` / ``cl.context`` are stubbed; docprep
is mocked so no VLM call is made.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway SQLite DB + DATA_DIR."""
    db_file = tmp_path / "docreg_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from falkordb_harness import document_registry

    document_registry.reset_engine_cache()
    return document_registry


class _FakeMessage:
    """Captures cl.Message(...).send() calls for assertions."""

    def __init__(self):
        self.sent = []

    def __call__(self, *, content="", elements=None, actions=None):
        rec = {"content": content, "elements": elements or [], "actions": actions or []}
        self.sent.append(rec)
        return SimpleNamespace(send=self._noop, stream_token=self._noop_token)

    async def _noop(self):
        return None

    async def _noop_token(self, _token):
        return None


class _FakeSessionStore:
    """A dict-backed ``cl.user_session``."""

    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


def _install_cl_stubs(monkeypatch, *, user_identifier="u1", thread_id="t1"):
    """Stub ``cl.user_session`` / ``cl.context`` and a message recorder.

    Note: ``cl.Text`` / ``cl.Pdf`` / ``cl.Image`` constructors read
    ``context_var`` (a contextvar) directly via a Pydantic ``default_factory``,
    bypassing ``cl.context``. Those element constructors therefore need the
    contextvar set inside the running loop — handled by :func:`_run_with_ctx`.
    """
    import chainlit as cl

    session = _FakeSessionStore()
    session.set("user_identifier", user_identifier)
    session.set("lang", "en")
    session.set("uploaded_files", [])
    session.set("ingestion_settings", {"overwrite_preprocessed": False})
    monkeypatch.setattr(cl, "user_session", session)

    fake_ctx = SimpleNamespace(session=SimpleNamespace(thread_id=thread_id))
    monkeypatch.setattr(cl, "context", fake_ctx)

    recorder = _FakeMessage()
    monkeypatch.setattr(cl, "Message", recorder)
    return session, recorder


def _run_with_ctx(coro, thread_id="t1"):
    """Run a coroutine with a real ChainlitContext on the contextvar.

    Element constructors (``cl.Text`` etc.) read ``context_var`` at construction
    time, so the context must be set inside the running loop (asyncio.run
    creates a fresh one per call).
    """

    async def _wrapper():
        from types import SimpleNamespace

        from chainlit.context import ChainlitContext, context_var

        ctx = ChainlitContext(
            session=SimpleNamespace(thread_id=thread_id), emitter=None
        )
        context_var.set(ctx)
        return await coro

    return asyncio.run(_wrapper())


def _action(callback_name: str, row_id: str):
    """Build a minimal Action with a payload id."""
    from chainlit.action import Action

    return Action(name=callback_name, payload={"id": row_id})


def _resolve_doc_id(registry, ingestion_id: str, *, name: str) -> str:
    """Translate a register_ingested return value (ingestion-row id) to the
    documents-row id the action callbacks expect.

    Under the v1 schema ``register_ingested`` returns the
    ``document_ingestions.id``, not the documents-row id. The action
    callbacks fetch the row via ``document_registry.get(id)`` (which
    reads the documents table), so tests must pass the documents id.
    """
    from sqlalchemy import text

    async def _fetch():
        async with registry._engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT "documentId" FROM document_ingestions '
                        'WHERE "id" = :id LIMIT 1'
                    ),
                    {"id": ingestion_id},
                )
            ).fetchone()
            if row is None:
                # fallback: look the document up by name
                row = (
                    await conn.execute(
                        text(
                            'SELECT "id" FROM documents WHERE "name" = :n '
                            "LIMIT 1"
                        ),
                        {"n": name},
                    )
                ).fetchone()
                return row[0] if row else None
            return row[0]

    return _run(_fetch())


# ---------------------------------------------------------------------------
# on_delete_document
# ---------------------------------------------------------------------------
def test_on_delete_document_removes_uploaded_row(tmp_registry, tmp_path, monkeypatch):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"data")
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path=str(f), checksum="1",
        )
    )
    session, recorder = _install_cl_stubs(monkeypatch)
    session.set("uploaded_files", [f])  # simulate the Ingest button target list
    refresh_calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_delete_document(_action("delete_document", rid)))

    assert _run(tmp_registry.get(rid)) is None  # row gone
    assert not f.exists()  # on-disk file unlinked
    assert session.get("uploaded_files") == []
    assert any("Deleted" in m["content"] for m in recorder.sent)
    assert refresh_calls == []


def test_on_delete_document_ingested_row_not_deletable(tmp_registry, monkeypatch):
    # Under the v1 schema, register_ingested returns the ingestion-row id;
    # resolve the documents-row id so the action callback can fetch it.
    rid = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="a.md", source="a.md",
        )
    )
    doc_id = _resolve_doc_id(tmp_registry, rid, name="a.md")
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_delete_document(_action("delete_document", doc_id)))

    assert _run(tmp_registry.get(doc_id)) is not None
    assert any("permanent" in m["content"] for m in recorder.sent)


def test_on_delete_document_missing_row_posts_not_found(tmp_registry, monkeypatch):
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_delete_document(_action("delete_document", "no-such-id")))
    assert any("not found" in m["content"].lower() for m in recorder.sent)


# ---------------------------------------------------------------------------
# on_preprocess_document
# ---------------------------------------------------------------------------
def test_on_preprocess_document_runs_docprep_and_registers(tmp_registry, tmp_path, monkeypatch):
    src = tmp_path / "originals" / "scan.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-1.4 fake")
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="scan.pdf",
            original_path=str(src), checksum="c1",
        )
    )
    _session, recorder = _install_cl_stubs(monkeypatch)
    refresh_calls = _stub_refresh_sidebar(monkeypatch)

    out_md = tmp_path / "preprocessed" / "scan.md"
    out_md.parent.mkdir(parents=True)
    out_md.write_text("# converted\n", encoding="utf-8")
    fake_result = json.dumps(
        {
            "already_exists": False,
            "output_path": "preprocessed/scan.md",
            "source": "originals/scan.pdf",
            "markdown_char_count": 12,
        }
    )

    import falkordb_harness.chainlit_app as app
    import falkordb_harness.tools.preprocess_tools as pt

    monkeypatch.setattr(
        pt, "_preprocess_document_impl",
        lambda path, yaml_path, overwrite: fake_result,
    )
    # Patch the module attribute the callback lazy-imports at call time.

    _run(app.on_preprocess_document(_action("preprocess_document_action", rid)))

    docs = _run(tmp_registry.list_for_thread("t1"))
    assert any(d.get("preprocessedPath") for d in docs)
    assert any("Preprocessed" in m["content"] for m in recorder.sent)
    assert refresh_calls == []


def test_on_preprocess_document_wrong_stage_rejected(tmp_registry, monkeypatch):
    rid = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/o/a.pdf", preprocessed_path="/p/a.md",
        )
    )
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_preprocess_document(_action("preprocess_document_action", rid)))
    assert any("Only uploaded" in m["content"] for m in recorder.sent)


def test_on_preprocess_document_missing_row_posts_not_found(tmp_registry, monkeypatch):
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_preprocess_document(_action("preprocess_document_action", "nope")))
    assert any("not found" in m["content"].lower() for m in recorder.sent)


# ---------------------------------------------------------------------------
# on_open_document
# ---------------------------------------------------------------------------
def test_on_open_document_renders_inline_text(tmp_registry, tmp_path, monkeypatch):
    # Preprocessed markdown row → builds a cl.Text element.
    out_md = tmp_path / "preprocessed" / "a.md"
    out_md.parent.mkdir(parents=True)
    out_md.write_text("# hello markdown", encoding="utf-8")
    rid = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="a.md",
            original_path=str(tmp_path / "originals" / "a.pdf"),
            preprocessed_path=str(out_md),
        )
    )
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run_with_ctx(app.on_open_document(_action("open_document", rid)))

    # A message with at least one element was sent.
    sent_with_elements = [m for m in recorder.sent if m["elements"]]
    assert sent_with_elements, "expected an inline element to be attached"
    assert any("Showing" in m["content"] for m in recorder.sent)


def test_on_open_document_ingested_hint(tmp_registry, monkeypatch):
    rid = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="a.md", source="a.md",
        )
    )
    doc_id = _resolve_doc_id(tmp_registry, rid, name="a.md")
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run_with_ctx(app.on_open_document(_action("open_document", doc_id)))
    assert any("knowledge graph" in m["content"].lower() for m in recorder.sent)


def test_on_open_document_missing_row_posts_not_found(tmp_registry, monkeypatch):
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_open_document(_action("open_document", "nope")))
    assert any("not found" in m["content"].lower() for m in recorder.sent)


def test_on_open_document_missing_file_posts_failure(tmp_registry, tmp_path, monkeypatch):
    # Row exists but the on-disk file is gone → failure message.
    rid = _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="gone.pdf",
            original_path=str(tmp_path / "originals" / "gone.pdf"),  # never written
            checksum="x",
        )
    )
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run_with_ctx(app.on_open_document(_action("open_document", rid)))
    assert any("Could not open" in m["content"] for m in recorder.sent)


# ---------------------------------------------------------------------------
# on_window_message (document-sidebar toggle — open path)
# ---------------------------------------------------------------------------
# The floating toggle button (public/docs_toggle.js) is a custom_js script,
# NOT a Chainlit CustomElement. To OPEN the sidebar it does
# window.postMessage({type: "chainlit-toggle-docs-sidebar", open: true}),
# which Chainlit's AppWrapper forwards to the backend ``window_message``
# socket event, dispatched to the @cl.on_window_message handler in
# chainlit_app.py. That handler filters on the ``type`` string and re-runs
# _refresh_sidebar (whose set_elements call re-opens the ElementSidebar).
# Closing is handled client-side (the button clicks the sidebar's own
# close button), so no server round-trip is needed for close.
#
# These tests cover the handler's payload filtering directly: it must call
# _refresh_sidebar for the recognized open payload, and ignore everything
# else (non-dict, wrong type, open:false) so other window.postMessage
# consumers are unaffected.


def _stub_refresh_sidebar(monkeypatch):
    """Replace _refresh_sidebar with a recorder; return the calls list."""
    import falkordb_harness.chainlit_app as app

    calls = []

    async def _fake_refresh():
        calls.append("refreshed")

    monkeypatch.setattr(app, "_refresh_sidebar", _fake_refresh)
    return calls


def test_on_window_message_opens_sidebar_for_recognized_payload(monkeypatch):
    """{type: 'chainlit-toggle-docs-sidebar', open: true} -> _refresh_sidebar."""
    _install_cl_stubs(monkeypatch)
    calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_window_message({"type": "chainlit-toggle-docs-sidebar", "open": True}))

    assert calls == ["refreshed"]


def test_on_window_message_ignores_wrong_type(monkeypatch):
    """A different ``type`` string must NOT trigger _refresh_sidebar."""
    _install_cl_stubs(monkeypatch)
    calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_window_message({"type": "some-other-message", "open": True}))
    assert calls == []


def test_on_window_message_ignores_open_false(monkeypatch):
    """open:false (close) is handled client-side; server must not refresh."""
    _install_cl_stubs(monkeypatch)
    calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_window_message({"type": "chainlit-toggle-docs-sidebar", "open": False}))
    assert calls == []


def test_on_window_message_ignores_non_dict_payload(monkeypatch):
    """Non-dict payloads (strings, numbers, None) must be ignored."""
    _install_cl_stubs(monkeypatch)
    calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    for payload in ("a string", 42, None, [1, 2, 3]):
        _run(app.on_window_message(payload))
    assert calls == []


def test_on_window_message_ignores_missing_open_key(monkeypatch):
    """Payload with the right type but no ``open`` key must be ignored."""
    _install_cl_stubs(monkeypatch)
    calls = _stub_refresh_sidebar(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_window_message({"type": "chainlit-toggle-docs-sidebar"}))
    assert calls == []


def test_on_chat_start_does_not_send_open_docs_button(monkeypatch):
    """on_chat_start must not send any chat message that would dismiss the
    starter screen.

    The floating toggle button is now a custom_js script
    (public/docs_toggle.js) injected by the browser, never sent as a
    Chainlit message. Asserted at the source level: on_chat_start's
    executable code must not reference the removed CustomElement sender.
    """
    import inspect
    import re

    import falkordb_harness.chainlit_app as app

    src = inspect.getsource(app.on_chat_start)
    # Drop comments so only executable statements are inspected.
    cleaned = re.sub(r"#.*", "", src)
    cleaned = re.sub(r'""".*?"""', "", cleaned, flags=re.DOTALL)
    assert "_send_open_docs_button" not in cleaned, (
        "on_chat_start must not reference the removed _send_open_docs_button"
    )
    assert "OpenDocsButton" not in cleaned, (
        "on_chat_start must not reference the removed OpenDocsButton CustomElement"
    )


# ---------------------------------------------------------------------------
# Sidebar must NOT auto-open from any behavioral flow
# ---------------------------------------------------------------------------
# The sidebar opens only on an explicit user click of the floating toggle
# button (public/docs_toggle.js → on_window_message → _refresh_sidebar).
# All other flows (chat start / resume / settings update / upload /
# ingestion / preprocessing / deletion / tool-end) must NOT call
# _refresh_sidebar — they mutate the registry in place, and the toggle's
# open path re-reads current data.
#
# Asserted at the source level so the tests run without invoking the full
# Chainlit handler machinery (which would need a live Chainlit session).

def _clean_source(src: str) -> str:
    """Strip comments + docstrings so only executable statements remain."""
    import re

    cleaned = re.sub(r"#.*", "", src)
    cleaned = re.sub(r'""".*?"""', "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", "", cleaned, flags=re.DOTALL)
    return cleaned


def test_on_chat_start_does_not_refresh_sidebar():
    """on_chat_start must not call _refresh_sidebar (no auto-open on new chat)."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_chat_start))
    assert "_refresh_sidebar" not in cleaned, (
        "on_chat_start must not call _refresh_sidebar — the sidebar opens "
        "only on explicit user click of the floating toggle button"
    )


def test_on_chat_resume_does_not_refresh_sidebar():
    """on_chat_resume must not call _refresh_sidebar (no auto-open on resume)."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_chat_resume))
    assert "_refresh_sidebar" not in cleaned, (
        "on_chat_resume must not call _refresh_sidebar — the sidebar opens "
        "only on explicit user click of the floating toggle button"
    )


def test_on_settings_update_does_not_refresh_sidebar():
    """on_settings_update must not call _refresh_sidebar (no auto-open on graph switch)."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_settings_update))
    assert "_refresh_sidebar" not in cleaned, (
        "on_settings_update must not call _refresh_sidebar — the sidebar "
        "opens only on explicit user click of the floating toggle button"
    )


def test_on_ingest_documents_does_not_refresh_sidebar():
    """on_ingest_documents must not call _refresh_sidebar (no auto-open after ingest)."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_ingest_documents))
    assert "_refresh_sidebar" not in cleaned, (
        "on_ingest_documents must not call _refresh_sidebar — the sidebar "
        "opens only on explicit user click of the floating toggle button"
    )


def test_on_message_does_not_refresh_sidebar():
    """on_message must not call _refresh_sidebar (no auto-open on upload or tool-end)."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_message))
    assert "_refresh_sidebar" not in cleaned, (
        "on_message must not call _refresh_sidebar — the sidebar opens "
        "only on explicit user click of the floating toggle button "
        "(upload, preprocess_document, extract_and_write, reset_graph all "
        "mutate the registry in place; the toggle re-reads it on open)"
    )


def test_on_window_message_still_refreshes_sidebar():
    """on_window_message (the floating toggle's open path) MUST still call
    _refresh_sidebar — this is the sole legitimate caller."""
    import inspect

    import falkordb_harness.chainlit_app as app

    cleaned = _clean_source(inspect.getsource(app.on_window_message))
    assert "_refresh_sidebar" in cleaned, (
        "on_window_message must call _refresh_sidebar — it is the sole "
        "legitimate entry point (the user's explicit click on the toggle)"
    )


# ---------------------------------------------------------------------------
# _build_document_manager_props — cross-thread scoping
# ---------------------------------------------------------------------------
# The floating toggle button's visibility is driven by /api/docs-info,
# which calls count_for_user (user-scoped, across ALL threads). The
# sidebar content builder (_build_document_manager_props) must use the
# same user-scoped query (list_for_user), not list_for_thread (current
# thread only). Otherwise the button is visible while the sidebar opens
# empty and immediately closes — the click does nothing.
#
# This is a regression test for the bug where the sidebar did not open
# on button click when the user's documents lived in a different thread
# than the current one.


def test_build_document_manager_props_uses_user_scope_not_thread():
    """_build_document_manager_props must read list_for_user, not
    list_for_thread, so its scoping matches /api/docs-info's
    count_for_user signal.
    """
    import inspect

    import falkordb_harness.chainlit_app as app

    src = _clean_source(inspect.getsource(app._build_document_manager_props))
    assert "list_for_user" in src, (
        "_build_document_manager_props must use list_for_user (user-scoped) "
        "so the sidebar opens whenever the toggle button is visible "
        "(/api/docs-info uses count_for_user)"
    )
    assert "list_for_thread" not in src, (
        "_build_document_manager_props must not use list_for_thread — it "
        "scopes to the current thread only, causing the sidebar to open "
        "empty when the user's docs are in a previous thread"
    )


def test_build_document_manager_props_returns_docs_from_other_thread(
    tmp_registry, monkeypatch
):
    """End-to-end: a user who uploaded in thread 't_old' starts a fresh
    chat (thread 't_new'). The sidebar must still list their document.
    """
    _install_cl_stubs(monkeypatch, user_identifier="u1", thread_id="t_new")
    # Register a doc in a DIFFERENT thread than the current session's.
    _run(
        tmp_registry.register_upload(
            thread_id="t_old", user_identifier="u1", name="a.pdf",
            original_path="/data/originals/t_old/a.pdf", checksum="c1",
        )
    )
    # No ingested rows for the active graph.
    import falkordb_harness.chainlit_app as app

    props = _run_with_ctx(app._build_document_manager_props())
    assert props is not None, (
        "sidebar must open when the user has documents in another thread"
    )
    assert len(props["documents"]) == 1
    assert props["documents"][0]["name"] == "a.pdf"


def test_build_document_manager_props_empty_when_no_docs(
    tmp_registry, monkeypatch
):
    """A user with zero documents gets a non-None props set with an empty
    documents list, so the sidebar opens and renders DocumentManager.jsx's
    "No documents yet" empty-state card (instead of silently doing nothing).
    """
    _install_cl_stubs(monkeypatch, user_identifier="u1", thread_id="t1")

    import falkordb_harness.chainlit_app as app

    props = _run_with_ctx(app._build_document_manager_props())
    assert props is not None, (
        "_build_document_manager_props must not return None when there are "
        "no docs — that makes _refresh_sidebar bail out before set_elements, "
        "so the toggle button's open path is a silent no-op"
    )
    assert props["documents"] == []
    assert "labels" in props
    assert props["lang"] in ("en", "de")