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
# _maybe_send_open_docs_button (startup-screen preservation + gating)
# ---------------------------------------------------------------------------
# The floating Documents button must NOT be sent during on_chat_start (it
# would swap the starter screen for an empty active chat). Instead it's
# injected lazily by _maybe_send_open_docs_button, gated on the once-per-
# session ``open_docs_button_sent`` flag AND on the document manager having
# content (uploaded/preprocessed for the thread OR ingested for the graph).
# These tests cover the gating logic directly; on_chat_start's contract
# (not calling the button sender) is asserted by confirming the helper is
# the only send path and the flag starts False.


def _stub_open_docs_sender(monkeypatch):
    """Replace _send_open_docs_button with a recorder; return (calls, send)."""
    import falkordb_harness.chainlit_app as app

    calls = []

    async def _fake_send():
        calls.append("sent")

    monkeypatch.setattr(app, "_send_open_docs_button", _fake_send)
    return calls


def _stub_doc_props(monkeypatch, props_value):
    """Stub _build_document_manager_props to return a fixed value."""
    import falkordb_harness.chainlit_app as app

    async def _fake_props():
        return props_value

    monkeypatch.setattr(app, "_build_document_manager_props", _fake_props)


def test_maybe_send_no_send_when_no_documents(tmp_registry, monkeypatch):
    """No documents → props is None → button is NOT sent, flag stays unset/False."""
    session, _ = _install_cl_stubs(monkeypatch)
    session.set("open_docs_button_sent", False)  # mirror on_chat_start
    calls = _stub_open_docs_sender(monkeypatch)
    _stub_doc_props(monkeypatch, None)  # empty registry

    import falkordb_harness.chainlit_app as app

    _run(app._maybe_send_open_docs_button())

    assert calls == []  # nothing sent
    # Flag must NOT have been flipped to True by this call.
    assert app.cl.user_session.get("open_docs_button_sent") is False


def test_maybe_send_sends_when_documents_exist(tmp_registry, monkeypatch):
    """Documents exist → props non-None → button sent once, flag flipped True."""
    session, _ = _install_cl_stubs(monkeypatch)
    session.set("open_docs_button_sent", False)
    calls = _stub_open_docs_sender(monkeypatch)
    _stub_doc_props(monkeypatch, {"documents": [{"id": "x"}], "lang": "en"})

    import falkordb_harness.chainlit_app as app

    _run(app._maybe_send_open_docs_button())

    assert calls == ["sent"]
    assert app.cl.user_session.get("open_docs_button_sent") is True


def test_maybe_send_skips_when_already_sent(tmp_registry, monkeypatch):
    """Flag already True → button is NOT re-sent (persists for the session)."""
    session, _ = _install_cl_stubs(monkeypatch)
    session.set("open_docs_button_sent", True)
    calls = _stub_open_docs_sender(monkeypatch)
    # Even if documents now exist, the once-per-session guard wins.
    _stub_doc_props(monkeypatch, {"documents": [{"id": "x"}], "lang": "en"})

    import falkordb_harness.chainlit_app as app

    _run(app._maybe_send_open_docs_button())

    assert calls == []  # already sent this session → skip


def test_maybe_send_sends_after_graph_switch_brought_ingested_rows(
    tmp_registry, monkeypatch
):
    """C1: a graph switch can make ingested rows appear; button sends then.

    Models the scenario: first on_message found no docs (flag False, no send);
    user switches to a populated graph; on_settings_update re-checks and the
    button now injects. Verified by calling the helper a second time with
    non-None props while the flag is still False.
    """
    session, _ = _install_cl_stubs(monkeypatch)
    session.set("open_docs_button_sent", False)
    calls = _stub_open_docs_sender(monkeypatch)
    _stub_doc_props(monkeypatch, {"documents": [{"id": "ing"}], "lang": "en"})

    import falkordb_harness.chainlit_app as app

    # First call (e.g. on_message against an empty default graph): no docs.
    _stub_doc_props(monkeypatch, None)
    _run(app._maybe_send_open_docs_button())
    assert calls == []

    # Second call (e.g. on_settings_update after switching to a populated
    # graph): docs now present → button sends.
    _stub_doc_props(monkeypatch, {"documents": [{"id": "ing"}], "lang": "en"})
    _run(app._maybe_send_open_docs_button())
    assert calls == ["sent"]
    assert app.cl.user_session.get("open_docs_button_sent") is True


def test_on_chat_start_does_not_send_open_docs_button(monkeypatch):
    """on_chat_start must not send the floating button (startup preservation).

    Asserted at the source level: on_chat_start's executable code must not
    call _send_open_docs_button (the eager call was removed). The button is
    injected only via _maybe_send_open_docs_button from on_message /
    on_settings_update. We verify by stripping comments/docstrings from the
    source and confirming no eager invocation remains.
    """
    import inspect
    import re

    import falkordb_harness.chainlit_app as app

    src = inspect.getsource(app.on_chat_start)
    # Drop comments (lines/segments starting with #) and docstrings so only
    # executable statements are inspected.
    cleaned = re.sub(r"#.*", "", src)
    cleaned = re.sub(r'""".*?"""', "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.replace("_maybe_send_open_docs_button()", "")  # never eager here
    assert "_send_open_docs_button()" not in cleaned, (
        "on_chat_start must not eagerly call _send_open_docs_button"
    )