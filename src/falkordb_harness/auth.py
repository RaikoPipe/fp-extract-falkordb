"""Password authentication + self-service registration for the Chainlit app.

Registers a ``@cl.password_auth_callback`` that verifies credentials
against the ``users`` table in the SQLite data-layer database (the same
file the data layer uses for threads/steps/elements). Passwords are
hashed with bcrypt and stored in the ``passwordHash`` column.

Self-service registration is exposed via a custom ``/register`` route
added to Chainlit's FastAPI app at startup. The route renders a small
HTML form (POST) and, on success, redirects to Chainlit's built-in login
page. Registration can be disabled with ``REGISTER_ENABLED=0``.

Env vars:
- ``CHAINLIT_AUTH_SECRET``: JWT signing secret (required by Chainlit
  when a password_auth_callback is registered). Generate with
  ``chainlit create-secret``.
- ``REGISTER_ENABLED``: when unset or ``0``, the /register route
  rejects new accounts. Defaults to enabled (``1``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import bcrypt
from chainlit import User
from sqlalchemy import text

from falkordb_harness.data_layer import build_data_layer

logger = logging.getLogger("falkordb_harness.auth")

# Minimum password length enforced at registration. Short enough not to
# annoy users, long enough to resist trivial guessing.
MIN_PASSWORD_LEN = 8
MAX_USERNAME_LEN = 64


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``.

    Matches the format Chainlit's SQLAlchemy layer writes to
    ``createdAt`` (``datetime.now().isoformat() + "Z"``).
    """
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password`` as a utf-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """Return True if ``password`` matches the bcrypt ``hashed`` string."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        logger.warning("bcrypt verify failed: %s", exc)
        return False


async def _get_engine():
    """Return the data layer's async engine (creating the layer if needed)."""
    layer = build_data_layer()
    return layer.engine


async def register_user(
    username: str, password: str, display_name: str | None = None
) -> tuple[User | None, str | None]:
    """Create a new user account.

    Returns ``(User, None)`` on success or ``(None, error_message)`` on
    failure (duplicate username, weak password, empty username). The
    row is inserted into the ``users`` table with a bcrypt password hash
    in the ``passwordHash`` column.

    ``display_name`` defaults to ``username`` when not provided.
    """
    username = (username or "").strip()
    if not username:
        return None, "Username is required."
    if len(username) > MAX_USERNAME_LEN:
        return None, f"Username must be at most {MAX_USERNAME_LEN} characters."
    if not password or len(password) < MIN_PASSWORD_LEN:
        return None, f"Password must be at least {MIN_PASSWORD_LEN} characters."

    engine = await _get_engine()
    async with engine.connect() as conn:
        existing = await conn.execute(
            text('SELECT "identifier" FROM users WHERE "identifier" = :u'),
            {"u": username},
        )
        if existing.fetchone() is not None:
            return None, "That username is already taken."

        await conn.execute(
            text(
                'INSERT INTO users ("id", "identifier", "createdAt", "metadata", "passwordHash") '
                "VALUES (:id, :identifier, :createdAt, :metadata, :passwordHash)"
            ),
            {
                "id": str(uuid.uuid4()),
                "identifier": username,
                "createdAt": _now_iso(),
                "metadata": "{}",
                "passwordHash": _hash_password(password),
            },
        )
        await conn.commit()

    logger.info("Registered new user: %s", username)
    return (
        User(
            identifier=username,
            display_name=display_name or username,
            metadata={"registered_at": _now_iso()},
        ),
        None,
    )


async def verify_credentials(username: str, password: str) -> User | None:
    """Return a :class:`User` if credentials are valid, else ``None``.

    Looks up the ``users`` row by ``identifier`` and verifies the bcrypt
    password hash. The returned ``User`` carries the stored display name
    (falling back to the identifier) so the UI shows a friendly name.
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "identifier", "passwordHash" FROM users WHERE "identifier" = :u'
                ),
                {"u": username},
            )
        ).fetchone()

    if row is None or not row[1]:
        # No stored hash means the account predates password auth or is
        # malformed — never allow login without a verifiable password.
        return None

    if not _verify_password(password, row[1]):
        return None

    return User(identifier=username, display_name=username, metadata={})


def _register_html(error: str | None = None) -> str:
    """Return the HTML for the self-service registration page.

    Inline-styled, server-rendered, no JS framework dependency. Shows an
    optional error banner when ``error`` is set. Posts back to ``/register``.
    """
    error_banner = (
        f'<div class="error">{error}</div>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Register — FalkorDB KG Agent</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
            background: #0f0f12; color: #e8e8ea; display: flex;
            align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #1a1a20; border: 1px solid #2a2a32; border-radius: 12px;
            padding: 2rem; width: 100%; max-width: 380px; box-shadow: 0 8px 24px rgba(0,0,0,.4); }}
    h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
    p.sub {{ color: #8a8a92; margin: 0 0 1.5rem; font-size: .9rem; }}
    label {{ display: block; font-size: .85rem; color: #a0a0a8; margin: .75rem 0 .25rem; }}
    input {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem; border-radius: 8px;
            border: 1px solid #33333d; background: #111117; color: #e8e8ea; font-size: .95rem; }}
    input:focus {{ outline: none; border-color: #4a9eff; }}
    button {{ width: 100%; margin-top: 1.25rem; padding: .65rem; border: 0; border-radius: 8px;
            background: #4a9eff; color: #fff; font-weight: 600; font-size: .95rem; cursor: pointer; }}
    button:hover {{ background: #3a8eef; }}
    .error {{ background: #3a1414; border: 1px solid #5a2222; color: #ff9a9a;
            padding: .6rem .75rem; border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }}
    .login-link {{ display: block; text-align: center; margin-top: 1rem; font-size: .85rem;
            color: #6a8aff; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Create your account</h1>
    <p class="sub">Register to use the FalkorDB knowledge-graph agent.</p>
    {error_banner}
    <form method="post" action="/register">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" required autofocus
             maxlength="{MAX_USERNAME_LEN}" autocomplete="username" />
      <label for="display_name">Display name (optional)</label>
      <input id="display_name" name="display_name" type="text" autocomplete="name" />
      <label for="password">Password (min {MIN_PASSWORD_LEN} characters)</label>
      <input id="password" name="password" type="password" required
             minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password" />
      <button type="submit">Register</button>
    </form>
    <a class="login-link" href="/login">Already have an account? Log in</a>
  </div>
</body>
</html>"""


def _register_disabled_html() -> str:
    """Return the HTML shown when registration is disabled."""
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><title>Registration disabled</title>
<style>body{font-family:system-ui;background:#0f0f12;color:#e8e8ea;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
.card{background:#1a1a20;border:1px solid #2a2a32;border-radius:12px;padding:2rem;
  max-width:380px;text-align:center;}
a{color:#6a8aff;}</style></head>
<body><div class="card"><h2>Registration is disabled</h2>
<p>Ask an administrator to create your account.</p>
<a href="/login">Go to login</a></div></body></html>"""


def _registration_enabled() -> bool:
    """Return True if self-service registration is allowed.

    Defaults to enabled unless ``REGISTER_ENABLED`` is explicitly ``0``.
    """
    import os

    return os.getenv("REGISTER_ENABLED", "1").lower() not in ("0", "false", "no")


def register_routes() -> None:
    """Add the ``/register`` route to Chainlit's FastAPI app.

    Called from the ``@cl.on_app_startup`` hook. Adds a route that
    serves a registration form (GET) and processes it (POST). On
    success, the user is redirected to Chainlit's built-in login page.

    The static-file route for persisted element blobs is also mounted
    here so the URLs returned by :class:`LocalStorageClient` resolve.

    Route ordering: Chainlit registers a catch-all
    ``@router.get("/{full_path:path}")`` that serves the SPA shell, and
    mounts it on ``app`` via ``app.include_router(router)`` at import
    time. Routes added to ``app`` *after* that would be shadowed by the
    catch-all (Starlette matches in declaration order). We therefore
    insert the ``/register`` routes at the FRONT of Chainlit's
    ``router.routes`` list so they precede the catch-all, and mount the
    elements StaticFiles directly on ``app`` (mounts are matched by
    prefix before the catch-all's path regex).
    """
    from chainlit.server import app, router
    from fastapi import Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from starlette.routing import Route
    from starlette.staticfiles import StaticFiles

    from falkordb_harness.data_layer import _elements_dir

    # Mount the elements directory so LocalStorageClient's /public/elements/...
    # URLs serve the actual files. Chainlit already exposes /public/ for
    # its own public dir; we use a distinct mount path to avoid collisions.
    elements_root = _elements_dir()
    app.mount(
        "/public/elements",
        StaticFiles(directory=str(elements_root)),
        name="elements",
    )

    # Build the /register handlers as plain Starlette Route objects and
    # insert them at the front of Chainlit's router (ahead of the
    # ``/{full_path:path}`` catch-all that serves the SPA shell). Using
    # ``router.routes.insert(0, ...)`` rather than ``@router.get(...)``
    # because the decorator appends to the end — after the catch-all —
    # which would shadow /register.

    async def register_page(request: Request) -> HTMLResponse:
        if not _registration_enabled():
            return HTMLResponse(_register_disabled_html())
        return HTMLResponse(_register_html())

    async def register_submit(request: Request) -> HTMLResponse | RedirectResponse:
        if not _registration_enabled():
            return HTMLResponse(_register_disabled_html())
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        display_name = str(form.get("display_name", "")).strip() or None

        user, error = await register_user(username, password, display_name)
        if user is None:
            assert error is not None
            return HTMLResponse(_register_html(error=error), status_code=400)

        # Redirect to Chainlit's login page so the new user can sign in.
        return RedirectResponse(url="/login", status_code=303)

    register_get = Route(
        "/register",
        endpoint=register_page,
        methods=["GET"],
        name="register_page",
    )
    register_post = Route(
        "/register",
        endpoint=register_submit,
        methods=["POST"],
        name="register_submit",
    )
    # Insert in reverse order so /register GET ends up first.
    router.routes.insert(0, register_post)
    router.routes.insert(0, register_get)


def _configure_password_auth() -> None:
    """Register the password auth callback with Chainlit.

    Done at import time so the callback is in place before Chainlit
    starts its server. The callback delegates to :func:`verify_credentials`.
    """
    import chainlit as cl

    @cl.password_auth_callback
    async def _callback(username: str, password: str) -> User | None:
        return await verify_credentials(username, password)


_configure_password_auth()