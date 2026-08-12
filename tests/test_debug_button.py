"""Tests for the debug "Run Showcase" button endpoint and prompt.

Covers:
- ``/api/debug-allowed`` returns ``{"allowed": false, "prompt": ""}`` for
  non-admin users.
- ``/api/debug-allowed`` returns ``{"allowed": true, "prompt": SHOWCASE_PROMPT}``
  for admin users.
- ``SHOWCASE_PROMPT`` mentions every tool name the agent should call (so a
  refactor that drops a tool from the prompt fails the test).
- The i18n keys for the debug button exist in both languages.
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


# ---------------------------------------------------------------------------
# /api/debug-allowed endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def _auth_secret(monkeypatch):
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-for-debug-allowed")


def _make_request(cookie_value=None):
    """Build a minimal Starlette Request, optionally with a Cookie header."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/debug-allowed",
        "headers": [],
    }
    if cookie_value:
        scope["headers"] = [
            (b"cookie", f"access_token={cookie_value}".encode("ascii"))
        ]
    return Request(scope)


def _get_debug_route():
    """Return the debug_allowed route from the router, or None."""
    from chainlit.server import router

    for r in router.routes:
        if getattr(r, "name", None) == "debug_allowed":
            return r
    return None


def _register_debug_route():
    """Clear any existing debug_allowed route and re-register."""
    from chainlit.server import router

    router.routes[:] = [
        r for r in router.routes
        if getattr(r, "name", None) != "debug_allowed"
    ]
    from falkordb_harness.auth import register_routes

    register_routes()


def test_debug_allowed_no_secret_returns_503(_auth_secret, monkeypatch):
    """When CHAINLIT_AUTH_SECRET is unset, the endpoint returns 503."""
    monkeypatch.delenv("CHAINLIT_AUTH_SECRET")

    with patch(
        "falkordb_harness.auth._csrf_secret", return_value="bypassed"
    ):
        _register_debug_route()

    route = _get_debug_route()
    assert route is not None, "debug_allowed route not registered"

    with patch(
        "chainlit.auth.jwt.get_jwt_secret", return_value=None
    ):
        req = _make_request()
        resp = _run(route.endpoint(req))

    assert resp.status_code == 503
    body = resp.body if hasattr(resp, "body") else resp.render(req)
    data = _json.loads(body) if isinstance(body, bytes) else body
    assert data["allowed"] is False
    assert data["prompt"] == ""


def test_debug_allowed_no_cookie_returns_401(_auth_secret):
    """When no JWT cookie is present, the endpoint returns 401."""
    _register_debug_route()
    route = _get_debug_route()
    assert route is not None

    req = _make_request()
    resp = _run(route.endpoint(req))
    assert resp.status_code == 401


def test_debug_allowed_non_admin_returns_allowed_false(_auth_secret):
    """A non-admin user gets allowed=false and an empty prompt."""
    _register_debug_route()
    route = _get_debug_route()
    assert route is not None

    fake_user = MagicMock()
    fake_user.identifier = "regular_user"

    with patch(
        "chainlit.auth.cookie.get_token_from_cookies", return_value="fake-token"
    ), patch(
        "chainlit.auth.jwt.decode_jwt", return_value=fake_user
    ), patch(
        "chainlit.auth.jwt.get_jwt_secret", return_value="test-secret"
    ), patch(
        "falkordb_harness.auth.get_user_role", new=AsyncMock(return_value="user")
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    body = resp.body if hasattr(resp, "body") else resp.render(req)
    data = _json.loads(body) if isinstance(body, bytes) else body
    assert data["allowed"] is False
    assert data["prompt"] == ""


def test_debug_allowed_admin_returns_allowed_true_with_prompt(_auth_secret):
    """An admin user gets allowed=true and the full SHOWCASE_PROMPT."""
    _register_debug_route()
    route = _get_debug_route()
    assert route is not None

    fake_user = MagicMock()
    fake_user.identifier = "admin_user"

    with patch(
        "chainlit.auth.cookie.get_token_from_cookies", return_value="fake-token"
    ), patch(
        "chainlit.auth.jwt.decode_jwt", return_value=fake_user
    ), patch(
        "chainlit.auth.jwt.get_jwt_secret", return_value="test-secret"
    ), patch(
        "falkordb_harness.auth.get_user_role", new=AsyncMock(return_value="admin")
    ):
        req = _make_request(cookie_value="fake-token")
        resp = _run(route.endpoint(req))

    assert resp.status_code == 200
    body = resp.body if hasattr(resp, "body") else resp.render(req)
    data = _json.loads(body) if isinstance(body, bytes) else body
    assert data["allowed"] is True
    assert len(data["prompt"]) > 100
    assert "create_graph" in data["prompt"]
    assert "extract_and_write" in data["prompt"]


# ---------------------------------------------------------------------------
# SHOWCASE_PROMPT content coverage
# ---------------------------------------------------------------------------

# Steps the showcase prompt MUST cover (the agent is instructed to perform them).
_EXPECTED_STEPS = {
    "create a new knowledge graph",
    "list the files",
    "inspect its metadata",
    "read a small excerpt",
    "convert it to Markdown",
    "preview the chunks",
    "ingestion pipeline",
    "inspect the graph schema",
    "list available graphs",
    "node count",
    "relationship type counts",
    "natural-language query",
    "keyword search",
    "semantic search",
    "potential duplicate links",
    "resolve it",
    "graph descriptions",
    "update the active graph",
    "pass/fail table",
}

# Actions the showcase prompt MUST NOT instruct the agent to perform
# (destructive or interactive — the prompt explicitly says to skip them).
_SKIPPED_ACTIONS = {
    "reset the graph",
    "switch graphs",
    "request ingestion confirmation",
    "ask the user clarifying questions",
}


def test_showcase_prompt_covers_all_expected_steps():
    """Every step the agent should perform is described in SHOWCASE_PROMPT."""
    from falkordb_harness.showcase import SHOWCASE_PROMPT

    prompt_lower = SHOWCASE_PROMPT.lower()
    for phrase in sorted(_EXPECTED_STEPS):
        assert phrase in prompt_lower, (
            f"Phrase '{phrase}' is missing from SHOWCASE_PROMPT. "
            f"Add it to the prompt or remove it from _EXPECTED_STEPS."
        )


def test_showcase_prompt_does_not_instruct_skipped_actions():
    """Destructive/interactive actions are NOT instructed in the prompt."""
    from falkordb_harness.showcase import SHOWCASE_PROMPT

    prompt_lower = SHOWCASE_PROMPT.lower()
    for phrase in sorted(_SKIPPED_ACTIONS):
        # The exclusion clause at the end says "Do NOT ..." — that's fine.
        # We check that the phrase doesn't appear as a positive instruction.
        lines = SHOWCASE_PROMPT.split("\n")
        for line in lines:
            stripped = line.strip().lower()
            if phrase in stripped and not stripped.startswith("do not"):
                pytest.fail(
                    f"Action '{phrase}' appears in an instruction line of "
                    f"SHOWCASE_PROMPT but should be skipped: {stripped!r}"
                )


def test_showcase_prompt_mentions_skipped_actions_in_exclusion_clause():
    """The prompt explicitly tells the agent NOT to perform the skipped actions."""
    from falkordb_harness.showcase import SHOWCASE_PROMPT

    assert "do not" in SHOWCASE_PROMPT.lower()
    prompt_lower = SHOWCASE_PROMPT.lower()
    for phrase in sorted(_SKIPPED_ACTIONS):
        assert phrase in prompt_lower, (
            f"Action '{phrase}' should be mentioned in the exclusion clause "
            f"of SHOWCASE_PROMPT so the agent knows to skip it."
        )


# ---------------------------------------------------------------------------
# i18n keys
# ---------------------------------------------------------------------------

def test_debug_button_i18n_keys_exist():
    """The debug button label and tooltip have en/de translations."""
    from falkordb_harness.i18n import STRINGS, set_lang, t

    assert "debug.button.label" in STRINGS
    assert "debug.button.tooltip" in STRINGS
    assert "en" in STRINGS["debug.button.label"]
    assert "de" in STRINGS["debug.button.label"]
    assert "en" in STRINGS["debug.button.tooltip"]
    assert "de" in STRINGS["debug.button.tooltip"]

    set_lang("en")
    en_label = t("debug.button.label")
    assert en_label and en_label != "debug.button.label"

    set_lang("de")
    de_label = t("debug.button.label")
    assert de_label and de_label != "debug.button.label"

    assert en_label != de_label


# ---------------------------------------------------------------------------
# Fixture file
# ---------------------------------------------------------------------------

def test_showcase_fixture_exists_and_is_non_empty():
    """The bundled fixture file exists and has content."""
    fixture = Path(__file__).resolve().parent.parent / "data" / "showcase_fixture.md"
    assert fixture.exists(), f"Fixture not found at {fixture}"
    content = fixture.read_text(encoding="utf-8")
    assert len(content) > 50, "Fixture is too short to produce meaningful extractions"
    assert "Machine" in content or "machine" in content.lower()
    assert "Resource" in content or "resource" in content.lower()
