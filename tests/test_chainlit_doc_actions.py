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

    import falkordb_harness.chainlit_app as app

    _run(app.on_delete_document(_action("delete_document", rid)))

    assert _run(tmp_registry.get(rid)) is None  # row gone
    assert not f.exists()  # on-disk file unlinked
    # uploaded_files trimmed
    assert session.get("uploaded_files") == []
    # a confirmation message was sent
    assert any("Deleted" in m["content"] for m in recorder.sent)


def test_on_delete_document_ingested_row_not_deletable(tmp_registry, monkeypatch):
    rid = _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="a.md", source="a.md",
        )
    )
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run(app.on_delete_document(_action("delete_document", rid)))

    # row still present
    assert _run(tmp_registry.get(rid)) is not None
    # a "not deletable" message was sent
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

    # Mock _preprocess_document_impl so no docprep/VLM call is made.
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
    # The callback imports the impl lazily via module attribute lookup, so
    # also patch the chainlit_app module's reference path by ensuring the
    # lazy import resolves to the patched module. The callback does:
    #   from falkordb_harness.tools.preprocess_tools import _preprocess_document_impl
    # which re-reads the module attribute at call time → patched value wins.

    _run(app.on_preprocess_document(_action("preprocess_document_action", rid)))

    # A preprocessed row was registered for the thread.
    docs = _run(tmp_registry.list_for_thread("t1"))
    stages = {d["stage"] for d in docs}
    assert "preprocessed" in stages
    # A "done" message was sent.
    assert any("Preprocessed" in m["content"] for m in recorder.sent)


def test_on_preprocess_document_wrong_stage_rejected(tmp_registry, monkeypatch):
    rid = _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="a.md",
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
    _session, recorder = _install_cl_stubs(monkeypatch)

    import falkordb_harness.chainlit_app as app

    _run_with_ctx(app.on_open_document(_action("open_document", rid)))
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