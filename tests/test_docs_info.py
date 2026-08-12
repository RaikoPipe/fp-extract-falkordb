"""Tests for the /api/docs-info endpoint.

The endpoint is consumed by the custom_js document-sidebar toggle
(public/docs_toggle.js) to decide whether to show the floating toggle
button. It reports ``{"has_documents": bool}`` — true when there is
anything to show in the sidebar.

Coverage:
- 503 when CHAINLIT_AUTH_SECRET is unset.
- 401 when no JWT cookie / invalid token / missing identifier.
- ``has_documents=true`` when the user's last graph has ingested rows.
- ``has_documents=true`` when the user owns uploaded/preprocessed rows
  even with no last graph (the upload-but-not-ingested case — the
  sidebar is no longer auto-opened on upload, so this endpoint is the
  toggle's only signal that there is something to show).
- ``has_documents=false`` when the user has no rows and no last graph.
"""

import asyncio
import json as _json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def _auth_secret(monkeypatch):
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-for-docs-info")


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    """Point the document registry at a throwaway SQLite DB."""
    db_file = tmp_path / "docreg_docs_info.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from falkordb_harness import document_registry

    document_registry.reset_engine_cache()
    return document_registry


def _make_request(cookie_value=None):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/docs-info",
        "headers": [],
    }
    if cookie_value:
        scope["headers"] = [
            (b"cookie", f"access_token={cookie_value}".encode("ascii"))
        ]
    return Request(scope)


def _get_docs_info_route():
    """Return the docs_info route from the router, or None."""
    from chainlit.server import router

    for r in router.routes:
        if getattr(r, "name", None) == "docs_info":
            return r
    return None


def _register_docs_info_route():
    """Clear any existing docs_info route and re-register."""
    from chainlit.server import router

    router.routes[:] = [
        r for r in router.routes
        if getattr(r, "name", None) != "docs_info"
    ]
    from falkordb_harness.auth import register_routes

    register_routes()


def _decode_body(resp, req):
    body = resp.body if hasattr(resp, "body") else resp.render(req)
    return _json.loads(body) if isinstance(body, bytes) else body


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_docs_info_no_secret_returns_503(_auth_secret, monkeypatch):
    monkeypatch.delenv("CHAINLIT_AUTH_SECRET")
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    with patch("chainlit.auth.jwt.get_jwt_secret", return_value=None):
        req = _make_request()
        resp = _run(route.endpoint(req))

    assert resp.status_code == 503


def test_docs_info_no_cookie_returns_401(_auth_secret):
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    req = _make_request()
    resp = _run(route.endpoint(req))
    assert resp.status_code == 401


def test_docs_info_invalid_token_returns_401(_auth_secret):
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    with patch(
        "chainlit.auth.cookie.get_token_from_cookies", return_value="fake-token"
    ), patch(
        "chainlit.auth.jwt.decode_jwt", side_effect=Exception("invalid token")
    ), patch(
        "chainlit.auth.jwt.get_jwt_secret", return_value="test-secret"
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 401


def test_docs_info_missing_identifier_returns_401(_auth_secret):
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    fake_user = MagicMock()
    fake_user.identifier = None

    with patch(
        "chainlit.auth.cookie.get_token_from_cookies", return_value="fake-token"
    ), patch(
        "chainlit.auth.jwt.decode_jwt", return_value=fake_user
    ), patch(
        "chainlit.auth.jwt.get_jwt_secret", return_value="test-secret"
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def _patch_auth(identifier="u1"):
    fake_user = MagicMock()
    fake_user.identifier = identifier
    return (
        patch(
            "chainlit.auth.cookie.get_token_from_cookies",
            return_value="fake-token",
        ),
        patch("chainlit.auth.jwt.decode_jwt", return_value=fake_user),
        patch("chainlit.auth.jwt.get_jwt_secret", return_value="test-secret"),
    )


def test_docs_info_true_when_last_graph_has_ingested_rows(_auth_secret, tmp_registry):
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    # Register an ingested row for graph "g1" owned by u1.
    _run(
        tmp_registry.register_ingested(
            graph_name="g1", user_identifier="u1", name="a.md", source="a.md",
        )
    )

    p1, p2, p3 = _patch_auth("u1")
    with p1, p2, p3, patch(
        "falkordb_harness.graph_descriptions.get_last_graph",
        new=AsyncMock(return_value="g1"),
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    data = _decode_body(resp, req)
    assert data["has_documents"] is True


def test_docs_info_true_when_user_has_uploads_but_no_last_graph(
    _auth_secret, tmp_registry
):
    """Upload-but-not-ingested case: the toggle must still show.

    The sidebar is no longer auto-opened on upload, so this endpoint is
    the toggle's only signal that there is something to show. The user's
    uploaded rows count toward has_documents even when no graph has been
    selected / ingested into.
    """
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    # Upload a row owned by u1 (no ingestion).
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u1", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )

    p1, p2, p3 = _patch_auth("u1")
    with p1, p2, p3, patch(
        "falkordb_harness.graph_descriptions.get_last_graph",
        new=AsyncMock(return_value=None),
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    data = _decode_body(resp, req)
    assert data["has_documents"] is True


def test_docs_info_true_when_user_has_preprocessed_rows_only(
    _auth_secret, tmp_registry
):
    """Preprocessed-only rows also count toward has_documents."""
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    _run(
        tmp_registry.register_preprocessed(
            thread_id="t1", user_identifier="u1", name="a.md",
            original_path="/tmp/a.pdf", preprocessed_path="/tmp/a.md",
        )
    )

    p1, p2, p3 = _patch_auth("u1")
    with p1, p2, p3, patch(
        "falkordb_harness.graph_descriptions.get_last_graph",
        new=AsyncMock(return_value=None),
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    data = _decode_body(resp, req)
    assert data["has_documents"] is True


def test_docs_info_false_when_user_has_no_rows_and_no_last_graph(
    _auth_secret, tmp_registry
):
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    p1, p2, p3 = _patch_auth("u1")
    with p1, p2, p3, patch(
        "falkordb_harness.graph_descriptions.get_last_graph",
        new=AsyncMock(return_value=None),
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    data = _decode_body(resp, req)
    assert data["has_documents"] is False


def test_docs_info_false_when_last_graph_has_no_rows_but_other_user_has_uploads(
    _auth_secret, tmp_registry
):
    """The user-scoped count must NOT leak across users.

    u1 has a last graph but no rows in it; u2 has uploads. u1's
    has_documents must be false (its own count is 0).
    """
    _register_docs_info_route()
    route = _get_docs_info_route()
    assert route is not None

    # u2 owns an upload; u1 owns nothing.
    _run(
        tmp_registry.register_upload(
            thread_id="t1", user_identifier="u2", name="a.pdf",
            original_path="/tmp/a.pdf", checksum="c1",
        )
    )

    p1, p2, p3 = _patch_auth("u1")
    with p1, p2, p3, patch(
        "falkordb_harness.graph_descriptions.get_last_graph",
        new=AsyncMock(return_value="g1"),
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    data = _decode_body(resp, req)
    assert data["has_documents"] is False