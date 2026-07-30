"""Password authentication + gated self-service registration for the Chainlit app.

Implements a public-deployment account lifecycle:

1. **Self-service registration** — a user signs up at ``/register`` with
   username, email, display name and password. The account is created with
   ``accountStatus = 'pending'`` and a one-time ``emailVerifyToken`` is
   emailed.
2. **Email verification** — clicking the link in the email flips the
   account to ``accountStatus = 'email_verified'``.
3. **Admin approval** — an administrator reviews pending users on the
   ``/admin/users`` page and approves them (→ ``accountStatus = 'active'``)
   or rejects/disables them. Only ``active`` accounts may log in.
4. **Password reset** — a forgot-password flow emails a time-limited
   ``passwordResetToken`` that lets the user set a new password.

Passwords are hashed with bcrypt. Login is delegated to Chainlit's
built-in ``password_auth_callback`` (which returns ``User | None``);
non-active accounts are rejected there. JWT cookie signing uses
``CHAINLIT_AUTH_SECRET`` (Chainlit-managed).

Security measures:
- bcrypt password hashing (12 rounds)
- strong password policy (min 10 chars, letter + digit, common-password
  blocklist)
- per-IP + per-username rate limiting on auth endpoints (slowapi)
- signed CSRF tokens on every state-changing form (itsdangerous, keyed
  off ``CHAINLIT_AUTH_SECRET``)
- role-based access control (``user`` / ``admin``); only admins reach
  ``/admin/*`` and only admins get the destructive ``reset_graph`` tool
- email enumeration resistance on forgot-password (always returns success)

Env vars:
- ``CHAINLIT_AUTH_SECRET``: JWT signing secret (required by Chainlit when
  a password_auth_callback is registered). Also keys the CSRF signer.
- ``REGISTER_ENABLED``: ``0``/``false``/``no`` disables self-registration.
- ``FIRST_ADMIN_USERNAME`` / ``FIRST_ADMIN_EMAIL`` / ``FIRST_ADMIN_PASSWORD``:
  bootstrap the first admin at startup (idempotent). See
  :func:`bootstrap_admin_from_env`.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from chainlit import User
from email_validator import EmailNotValidError, validate_email
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from falkordb_harness.data_layer import build_data_layer
from falkordb_harness.email_service import (
    send_approval_notification,
    send_password_reset_email,
    send_verification_email,
)

logger = logging.getLogger("falkordb_harness.auth")

# Password / username policy.
MIN_PASSWORD_LEN = 10
MAX_USERNAME_LEN = 64
MAX_EMAIL_LEN = 254

# Token lifetimes.
EMAIL_VERIFY_TTL = timedelta(hours=24)
PASSWORD_RESET_TTL = timedelta(hours=1)

# Account statuses.
STATUS_PENDING = "pending"
STATUS_EMAIL_VERIFIED = "email_verified"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# Roles.
ROLE_USER = "user"
ROLE_ADMIN = "admin"

# A small blocklist of extremely common passwords. Not exhaustive — the
# min-length + character-class rule already filters most trivial choices —
# but these show up in every leaked-password top-100.
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    p.lower()
    for p in (
        "password", "password1", "password123", "123456789", "1234567890",
        "qwerty123", "abc123456", "iloveyou", "letmein1", "welcome1",
        "changeme1", "football1", "baseball1", "dragon123", "monkey123",
        "passw0rd", "p@ssw0rd", "admin123", "administrator", "root12345",
    )
)


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``.

    Matches the format Chainlit's SQLAlchemy layer writes to ``createdAt``.
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


def validate_password_strength(password: str) -> str | None:
    """Return an error message if ``password`` is too weak, else ``None``.

    Enforces: minimum length, at least one letter and one digit, and a
    small common-password blocklist. Deliberately pragmatic — strong
    enough for a gated app without forcing arbitrary symbol rules that
    users defeat with trivial substitutions.
    """
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if not any(c.isalpha() for c in password):
        return "Password must contain at least one letter."
    if not any(c.isdigit() for c in password):
        return "Password must contain at least one digit."
    if password.lower() in _COMMON_PASSWORDS:
        return "That password is too common — choose a less predictable one."
    return None


def _validate_email(email: str) -> str | None:
    """Return a normalized email or an error message (tuple via None sentinel).

    Actually returns ``(normalized_email, None)`` on success or
    ``(None, error_message)`` on failure.
    """
    try:
        result = validate_email(email, check_deliverability=False)
        normalized = result.normalized
    except EmailNotValidError as exc:
        return None, str(exc)
    if len(normalized) > MAX_EMAIL_LEN:
        return None, "Email is too long."
    return normalized, None


# --- CSRF token signer -----------------------------------------------------

def _csrf_signer() -> URLSafeTimedSerializer:
    """Return a timed serializer keyed off the Chainlit JWT secret.

    CSRF tokens are signed with the same secret that signs login JWTs so a
    single rotation covers both. Tokens expire after 1 hour.
    """
    secret = os.getenv("CHAINLIT_AUTH_SECRET") or "dev-insecure-csrf-key"
    return URLSafeTimedSerializer(secret, salt="csrf")


def issue_csrf_token() -> str:
    """Return a fresh signed CSRF token for embedding in a form."""
    return _csrf_signer().dumps({"t": "csrf"})


def verify_csrf_token(token: str | None, max_age: int = 3600) -> bool:
    """Return True if ``token`` is a valid, unexpired CSRF token."""
    if not token:
        return False
    try:
        _csrf_signer().loads(token, max_age=max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def _get_engine():
    """Return the data layer's async engine (creating the layer if needed)."""
    layer = build_data_layer()
    return layer.engine


# --- Registration ----------------------------------------------------------

async def register_user(
    username: str,
    password: str,
    email: str,
    display_name: str | None = None,
) -> tuple[User | None, str | None, str | None]:
    """Create a new pending user account and queue a verification email.

    Returns ``(User, None, verify_token)`` on success (the caller is
    responsible for emailing the token, or it is emailed here when
    ``send_email=True``), or ``(None, error_message, None)`` on failure
    (duplicate username/email, weak password, invalid email).

    The account is created with ``accountStatus = 'pending'``. The caller
    sends the verification email; until the user verifies, login is
    rejected.
    """
    username = (username or "").strip()
    if not username:
        return None, "Username is required.", None
    if len(username) > MAX_USERNAME_LEN:
        return None, f"Username must be at most {MAX_USERNAME_LEN} characters.", None
    email = (email or "").strip()
    if not email:
        return None, "Email is required.", None
    normalized_email, email_err = _validate_email(email)
    if email_err is not None:
        return None, email_err, None
    pw_err = validate_password_strength(password)
    if pw_err is not None:
        return None, pw_err, None

    verify_token = secrets.token_urlsafe(32)
    now = _now_iso()
    verify_expires = (datetime.now(timezone.utc) + EMAIL_VERIFY_TTL).isoformat()

    engine = await _get_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO users ('
                    '"id", "identifier", "createdAt", "metadata", "passwordHash", '
                    '"email", "role", "accountStatus", "displayName", '
                    '"emailVerifyToken", "emailVerifyExpires") '
                    "VALUES (:id, :identifier, :createdAt, :metadata, :passwordHash, "
                    ":email, :role, :accountStatus, :displayName, "
                    ":emailVerifyToken, :emailVerifyExpires)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "identifier": username,
                    "createdAt": now,
                    "metadata": "{}",
                    "passwordHash": _hash_password(password),
                    "email": normalized_email,
                    "role": ROLE_USER,
                    "accountStatus": STATUS_PENDING,
                    "displayName": display_name or username,
                    "emailVerifyToken": verify_token,
                    "emailVerifyExpires": verify_expires,
                },
            )
    except IntegrityError:
        # UNIQUE constraint on identifier or email — figure out which for
        # a friendlier message.
        async with engine.connect() as conn:
            dup_user = (
                await conn.execute(
                    text('SELECT 1 FROM users WHERE "identifier" = :u'), {"u": username}
                )
            ).fetchone()
        if dup_user:
            return None, "That username is already taken.", None
        return None, "That email is already registered.", None

    logger.info("Registered new user: %s (%s)", username, normalized_email)
    return (
        User(
            identifier=username,
            display_name=display_name or username,
            metadata={"email": normalized_email, "status": STATUS_PENDING},
        ),
        None,
        verify_token,
    )


# --- Email verification ----------------------------------------------------

async def verify_email_token(token: str) -> tuple[bool, str]:
    """Consume a verification token, flipping the account to ``email_verified``.

    Returns ``(True, message)`` on success or ``(False, error_message)``
    on an invalid/expired/already-used token.
    """
    if not token:
        return False, "Invalid verification link."
    engine = await _get_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "identifier", "accountStatus", "emailVerifyExpires" '
                    'FROM users WHERE "emailVerifyToken" = :t'
                ),
                {"t": token},
            )
        ).fetchone()
        if row is None:
            return False, "Invalid or already-used verification link."
        identifier, status, expires_iso = row
        if status == STATUS_ACTIVE:
            return True, "Your account is already active — you can log in."
        if status == STATUS_DISABLED:
            return False, "This account has been disabled."
        # Check expiry.
        try:
            expired = datetime.fromisoformat(expires_iso) < datetime.now(timezone.utc)
        except (TypeError, ValueError):
            expired = True
        if expired:
            await conn.execute(
                text(
                    'UPDATE users SET "emailVerifyToken" = NULL, '
                    '"emailVerifyExpires" = NULL WHERE "identifier" = :u'
                ),
                {"u": identifier},
            )
            return False, "This verification link has expired. Please request a new one."
        await conn.execute(
            text(
                'UPDATE users SET "accountStatus" = :verified, '
                '"emailVerifiedAt" = :now, "emailVerifyToken" = NULL, '
                '"emailVerifyExpires" = NULL WHERE "identifier" = :u'
            ),
            {"verified": STATUS_EMAIL_VERIFIED, "now": _now_iso(), "u": identifier},
        )
    logger.info("Email verified for user: %s", identifier)
    return True, "Email verified — an administrator will review your account."


async def resend_verification_email(email: str) -> tuple[bool, str]:
    """Regenerate a verification token for an unverified account and email it.

    Always returns a success-shaped message to avoid email enumeration,
    but only actually sends when the email matches a pending account.
    """
    email = (email or "").strip()
    normalized, email_err = _validate_email(email) if email else (None, "invalid")
    if email_err is not None or normalized is None:
        return True, "If that email is registered and unverified, a new link has been sent."

    engine = await _get_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "identifier", "accountStatus" FROM users WHERE "email" = :e'
                ),
                {"e": normalized},
            )
        ).fetchone()
        if row is None or row[1] not in (STATUS_PENDING, STATUS_EMAIL_VERIFIED):
            # No pending account — say nothing specific.
            return True, "If that email is registered and unverified, a new link has been sent."
        identifier = row[0]
        new_token = secrets.token_urlsafe(32)
        new_expires = (datetime.now(timezone.utc) + EMAIL_VERIFY_TTL).isoformat()
        await conn.execute(
            text(
                'UPDATE users SET "emailVerifyToken" = :t, "emailVerifyExpires" = :e, '
                '"accountStatus" = :pending WHERE "identifier" = :u'
            ),
            {
                "t": new_token,
                "e": new_expires,
                "pending": STATUS_PENDING,
                "u": identifier,
            },
        )

    await send_verification_email(normalized, new_token)
    logger.info("Re-sent verification email to %s", normalized)
    return True, "If that email is registered and unverified, a new link has been sent."


# --- Login -----------------------------------------------------------------

async def verify_credentials(username: str, password: str) -> User | None:
    """Return a :class:`User` if credentials are valid AND the account is
    active, else ``None``.

    Non-active accounts (pending / email_verified / disabled) are rejected
    here. Because Chainlit's ``password_auth_callback`` can only return
    ``User | None``, a rejected user sees the generic "invalid
    credentials" message on Chainlit's login page — by design, to avoid
    leaking account-status details to unauthenticated visitors.
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "passwordHash", "accountStatus", "role", '
                    '"displayName", "email" FROM users WHERE "identifier" = :u'
                ),
                {"u": username},
            )
        ).fetchone()

    if row is None or not row[0]:
        return None
    password_hash, status, role, display_name, email = row
    if status != STATUS_ACTIVE:
        return None
    if not _verify_password(password, password_hash):
        return None

    return User(
        identifier=username,
        display_name=display_name or username,
        metadata={"role": role, "email": email},
    )


async def get_user_role(identifier: str) -> str | None:
    """Return the role for ``identifier`` (``user``/``admin``) or ``None``."""
    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text('SELECT "role" FROM users WHERE "identifier" = :u'),
                {"u": identifier},
            )
        ).fetchone()
    return row[0] if row else None


# --- Password reset --------------------------------------------------------

async def request_password_reset(email: str) -> bool:
    """Generate a reset token for the account matching ``email`` and email it.

    Always returns ``True`` (and logs a no-op when no account matches) to
    avoid leaking which emails are registered.
    """
    email = (email or "").strip()
    if not email:
        return True
    normalized, email_err = _validate_email(email) if email else (None, "invalid")
    if email_err is not None or normalized is None:
        return True

    engine = await _get_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "identifier", "accountStatus" FROM users WHERE "email" = :e'
                ),
                {"e": normalized},
            )
        ).fetchone()
        if row is None:
            logger.info("Password reset requested for unknown email — no-op")
            return True
        identifier, status = row
        if status == STATUS_DISABLED:
            return True
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + PASSWORD_RESET_TTL).isoformat()
        await conn.execute(
            text(
                'UPDATE users SET "passwordResetToken" = :t, '
                '"passwordResetExpires" = :e WHERE "identifier" = :u'
            ),
            {"t": token, "e": expires, "u": identifier},
        )

    await send_password_reset_email(normalized, token)
    logger.info("Sent password reset email to %s", normalized)
    return True


async def reset_password(token: str, new_password: str) -> tuple[bool, str]:
    """Set a new password for the account owning ``token``.

    Returns ``(True, message)`` on success or ``(False, error_message)``
    on an invalid/expired token or weak password.
    """
    if not token:
        return False, "Invalid reset link."
    pw_err = validate_password_strength(new_password)
    if pw_err is not None:
        return False, pw_err

    engine = await _get_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    'SELECT "identifier", "passwordResetExpires", "accountStatus" '
                    'FROM users WHERE "passwordResetToken" = :t'
                ),
                {"t": token},
            )
        ).fetchone()
        if row is None:
            return False, "Invalid or already-used reset link."
        identifier, expires_iso, status = row
        if status == STATUS_DISABLED:
            return False, "This account has been disabled."
        try:
            expired = datetime.fromisoformat(expires_iso) < datetime.now(timezone.utc)
        except (TypeError, ValueError):
            expired = True
        if expired:
            await conn.execute(
                text(
                    'UPDATE users SET "passwordResetToken" = NULL, '
                    '"passwordResetExpires" = NULL WHERE "identifier" = :u'
                ),
                {"u": identifier},
            )
            return False, "This reset link has expired. Please request a new one."
        await conn.execute(
            text(
                'UPDATE users SET "passwordHash" = :h, '
                '"passwordResetToken" = NULL, "passwordResetExpires" = NULL '
                'WHERE "identifier" = :u'
            ),
            {"h": _hash_password(new_password), "u": identifier},
        )
    logger.info("Password reset for user: %s", identifier)
    return True, "Your password has been reset. You can now log in."


# --- Admin operations ------------------------------------------------------

async def list_users() -> list[dict]:
    """Return all users (admin view). Each dict has identifier, email,
    role, accountStatus, displayName, createdAt, emailVerifiedAt."""
    engine = await _get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    'SELECT "identifier", "email", "role", "accountStatus", '
                    '"displayName", "createdAt", "emailVerifiedAt" '
                    "FROM users ORDER BY \"createdAt\" DESC"
                )
            )
        ).fetchall()
    return [
        {
            "identifier": r[0],
            "email": r[1],
            "role": r[2],
            "accountStatus": r[3],
            "displayName": r[4],
            "createdAt": r[5],
            "emailVerifiedAt": r[6],
        }
        for r in rows
    ]


async def _set_account_status(identifier: str, status: str) -> bool:
    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text('UPDATE users SET "accountStatus" = :s WHERE "identifier" = :u'),
            {"s": status, "u": identifier},
        )
        return result.rowcount > 0


async def approve_user(identifier: str) -> tuple[bool, str]:
    """Flip an account to ``active`` and notify the user by email."""
    ok = await _set_account_status(identifier, STATUS_ACTIVE)
    if not ok:
        return False, "User not found."
    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text('SELECT "email" FROM users WHERE "identifier" = :u'),
                {"u": identifier},
            )
        ).fetchone()
    if row and row[0]:
        await send_approval_notification(row[0])
    logger.info("Approved user: %s", identifier)
    return True, "User approved."


async def reject_user(identifier: str) -> tuple[bool, str]:
    """Disable a pending/rejected account (keeps the row, blocks login)."""
    ok = await _set_account_status(identifier, STATUS_DISABLED)
    if not ok:
        return False, "User not found."
    logger.info("Rejected/disabled user: %s", identifier)
    return True, "User disabled."


async def disable_user(identifier: str) -> tuple[bool, str]:
    ok = await _set_account_status(identifier, STATUS_DISABLED)
    if not ok:
        return False, "User not found."
    logger.info("Disabled user: %s", identifier)
    return True, "User disabled."


async def enable_user(identifier: str) -> tuple[bool, str]:
    """Re-enable a previously disabled account back to ``active``."""
    ok = await _set_account_status(identifier, STATUS_ACTIVE)
    if not ok:
        return False, "User not found."
    logger.info("Enabled user: %s", identifier)
    return True, "User enabled."


async def set_user_role(identifier: str, role: str) -> tuple[bool, str]:
    """Set a user's role (``user`` or ``admin``)."""
    if role not in (ROLE_USER, ROLE_ADMIN):
        return False, "Invalid role."
    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text('UPDATE users SET "role" = :r WHERE "identifier" = :u'),
            {"r": role, "u": identifier},
        )
        if result.rowcount == 0:
            return False, "User not found."
    logger.info("Set role %s for user: %s", role, identifier)
    return True, f"Role set to {role}."


# --- Bootstrap / migration -------------------------------------------------

async def bootstrap_admin_from_env() -> None:
    """Create or promote the first admin account from env vars.

    Reads ``FIRST_ADMIN_USERNAME``, ``FIRST_ADMIN_EMAIL`` and
    ``FIRST_ADMIN_PASSWORD``. If all three are set: if the user doesn't
    exist, creates it with ``role='admin'`` and ``accountStatus='active'``;
    if it exists, promotes it to admin and activates it. Idempotent —
    safe to run on every startup. Missing env vars → no-op (logged once).
    """
    username = os.getenv("FIRST_ADMIN_USERNAME", "").strip()
    email = os.getenv("FIRST_ADMIN_EMAIL", "").strip()
    password = os.getenv("FIRST_ADMIN_PASSWORD", "")
    if not (username and email and password):
        logger.info("FIRST_ADMIN_* env vars not set — skipping admin bootstrap.")
        return
    normalized, email_err = _validate_email(email)
    if email_err is not None or normalized is None:
        logger.error("FIRST_ADMIN_EMAIL invalid: %s", email_err)
        return

    engine = await _get_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text('SELECT "identifier" FROM users WHERE "identifier" = :u'),
                {"u": username},
            )
        ).fetchone()
        if row is None:
            await conn.execute(
                text(
                    'INSERT INTO users ('
                    '"id", "identifier", "createdAt", "metadata", "passwordHash", '
                    '"email", "role", "accountStatus", "displayName", "emailVerifiedAt") '
                    "VALUES (:id, :identifier, :createdAt, :metadata, :passwordHash, "
                    ":email, :role, :accountStatus, :displayName, :emailVerifiedAt)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "identifier": username,
                    "createdAt": _now_iso(),
                    "metadata": "{}",
                    "passwordHash": _hash_password(password),
                    "email": normalized,
                    "role": ROLE_ADMIN,
                    "accountStatus": STATUS_ACTIVE,
                    "displayName": username,
                    "emailVerifiedAt": _now_iso(),
                },
            )
            logger.info("Bootstrapped first admin account: %s", username)
        else:
            await conn.execute(
                text(
                    'UPDATE users SET "role" = :role, "accountStatus" = :status, '
                    '"email" = :email, "passwordHash" = :passwordHash, '
                    '"emailVerifiedAt" = COALESCE("emailVerifiedAt", :now) '
                    'WHERE "identifier" = :u'
                ),
                {
                    "role": ROLE_ADMIN,
                    "status": STATUS_ACTIVE,
                    "email": normalized,
                    "passwordHash": _hash_password(password),
                    "now": _now_iso(),
                    "u": username,
                },
            )
            logger.info("Promoted existing user to admin: %s", username)


async def migrate_legacy_accounts() -> None:
    """Put legacy pre-auth accounts into a safe disabled state.

    Rows created before password auth (no ``passwordHash``) cannot log in
    anyway; this makes their disabled status explicit so they don't show
    up as pending in the admin queue and can't be accidentally approved
    into an active-but-passwordless state. An operator can reset such a
    user's password via the CLI ``create-admin`` command.
    """
    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                'UPDATE users SET "accountStatus" = :disabled, "role" = :role '
                'WHERE "passwordHash" IS NULL AND "accountStatus" IN (:pending, :active)'
            ),
            {
                "disabled": STATUS_DISABLED,
                "role": ROLE_USER,
                "pending": STATUS_PENDING,
                "active": STATUS_ACTIVE,
            },
        )
        if result.rowcount:
            logger.warning(
                "Disabled %d legacy account(s) with no password hash. "
                "Use `falkordb-agent create-admin` to provision credentials.",
                result.rowcount,
            )


# --- HTML pages ------------------------------------------------------------

def _page_shell(title: str, body: str, error: str | None = None) -> str:
    """Return a styled HTML page wrapping ``body`` (shared CSS)."""
    error_banner = f'<div class="error">{error}</div>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — FalkorDB KG Agent</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
            background: #0f0f12; color: #e8e8ea; display: flex;
            align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
    .card {{ background: #1a1a20; border: 1px solid #2a2a32; border-radius: 12px;
            padding: 2rem; width: 100%; max-width: 420px; box-shadow: 0 8px 24px rgba(0,0,0,.4); }}
    .card.wide {{ max-width: 760px; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; }}
    h2 {{ font-size: 1.1rem; margin: 1.5rem 0 .5rem; }}
    p.sub {{ color: #8a8a92; margin: 0 0 1.5rem; font-size: .9rem; }}
    label {{ display: block; font-size: .85rem; color: #a0a0a8; margin: .75rem 0 .25rem; }}
    input {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem; border-radius: 8px;
            border: 1px solid #33333d; background: #111117; color: #e8e8ea; font-size: .95rem; }}
    input:focus {{ outline: none; border-color: #4a9eff; }}
    button {{ width: 100%; margin-top: 1.25rem; padding: .65rem; border: 0; border-radius: 8px;
            background: #4a9eff; color: #fff; font-weight: 600; font-size: .95rem; cursor: pointer; }}
    button:hover {{ background: #3a8eef; }}
    button.secondary {{ background: #2a2a32; }}
    button.secondary:hover {{ background: #33333d; }}
    .error {{ background: #3a1414; border: 1px solid #5a2222; color: #ff9a9a;
            padding: .6rem .75rem; border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }}
    .success {{ background: #143a14; border: 1px solid #225a22; color: #9aff9a;
            padding: .6rem .75rem; border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }}
    a {{ color: #6a8aff; text-decoration: none; }}
    .link {{ display: block; text-align: center; margin-top: 1rem; font-size: .85rem; }}
    .hint {{ color: #6a6a72; font-size: .8rem; margin-top: .25rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: .85rem; }}
    th, td {{ padding: .5rem .6rem; border-bottom: 1px solid #2a2a32; text-align: left; }}
    th {{ color: #8a8a92; font-weight: 600; }}
    .badge {{ display: inline-block; padding: .15rem .5rem; border-radius: 999px; font-size: .75rem; }}
    .b-pending {{ background: #3a2a14; color: #ffcc7a; }}
    .b-email_verified {{ background: #143a2a; color: #7affcc; }}
    .b-active {{ background: #143a14; color: #9aff9a; }}
    .b-disabled {{ background: #3a1414; color: #ff9a9a; }}
    .b-admin {{ background: #2a143a; color: #cc9aff; }}
    form.inline {{ display: inline; }}
    form.inline button {{ width: auto; margin: 0 .15rem; padding: .3rem .6rem; font-size: .8rem; }}
  </style>
</head>
<body>
  <div class="card">
    {error_banner}
    {body}
  </div>
</body>
</html>"""


def _register_html(error: str | None = None, csrf_token: str | None = None) -> str:
    """Return the HTML for the self-service registration page."""
    body = f"""
    <h1>Create your account</h1>
    <p class="sub">Register to use the FalkorDB knowledge-graph agent. You'll
    receive an email to verify your address, then an administrator will approve
    your account.</p>
    <form method="post" action="/register">
      <input type="hidden" name="csrf_token" value="{csrf_token or ''}" />
      <label for="username">Username</label>
      <input id="username" name="username" type="text" required autofocus
             maxlength="{MAX_USERNAME_LEN}" autocomplete="username" />
      <label for="display_name">Display name (optional)</label>
      <input id="display_name" name="display_name" type="text" autocomplete="name" />
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="email" />
      <label for="password">Password (min {MIN_PASSWORD_LEN} chars, incl. a letter and a digit)</label>
      <input id="password" name="password" type="password" required
             minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password" />
      <button type="submit">Register</button>
    </form>
    <a class="link" href="/login">Already have an account? Log in</a>
    <a class="link" href="/forgot-password">Forgot your password?</a>"""
    return _page_shell("Register", body, error=error)


def _register_disabled_html() -> str:
    """Return the HTML shown when registration is disabled."""
    return _page_shell(
        "Registration disabled",
        """<h2>Registration is disabled</h2>
        <p>Ask an administrator to create your account.</p>
        <a class="link" href="/login">Go to login</a>""",
    )


def _verify_email_html(message: str, success: bool) -> str:
    banner_cls = "success" if success else "error"
    body = f'<h1>Email verification</h1><div class="{banner_cls}">{message}</div>'
    if success:
        body += '<a class="link" href="/login">Go to login</a>'
    body += '<a class="link" href="/resend-verification">Resend verification email</a>'
    return _page_shell("Verify email", body)


def _resend_verification_html(error: str | None = None, csrf_token: str | None = None,
                              info: str | None = None) -> str:
    body = f"""
    <h1>Resend verification email</h1>
    <p class="sub">Enter your email and we'll send a new verification link.</p>
    {f'<div class="success">{info}</div>' if info else ''}
    <form method="post" action="/resend-verification">
      <input type="hidden" name="csrf_token" value="{csrf_token or ''}" />
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="email" />
      <button type="submit">Send</button>
    </form>
    <a class="link" href="/login">Back to login</a>"""
    return _page_shell("Resend verification", body, error=error)


def _forgot_password_html(error: str | None = None, csrf_token: str | None = None,
                          info: str | None = None) -> str:
    body = f"""
    <h1>Reset your password</h1>
    <p class="sub">Enter your account email and we'll send a reset link.</p>
    {f'<div class="success">{info}</div>' if info else ''}
    <form method="post" action="/forgot-password">
      <input type="hidden" name="csrf_token" value="{csrf_token or ''}" />
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="email" />
      <button type="submit">Send reset link</button>
    </form>
    <a class="link" href="/login">Back to login</a>"""
    return _page_shell("Forgot password", body, error=error)


def _reset_password_html(token: str, error: str | None = None, csrf_token: str | None = None) -> str:
    body = f"""
    <h1>Set a new password</h1>
    <p class="sub">Choose a new password (min {MIN_PASSWORD_LEN} chars, incl. a letter and a digit).</p>
    <form method="post" action="/reset-password">
      <input type="hidden" name="token" value="{token}" />
      <input type="hidden" name="csrf_token" value="{csrf_token or ''}" />
      <label for="password">New password</label>
      <input id="password" name="password" type="password" required
             minlength="{MIN_PASSWORD_LEN}" autocomplete="new-password" autofocus />
      <button type="submit">Reset password</button>
    </form>"""
    return _page_shell("Reset password", body, error=error)


def _status_badge(status: str) -> str:
    cls = {
        STATUS_PENDING: "b-pending",
        STATUS_EMAIL_VERIFIED: "b-email_verified",
        STATUS_ACTIVE: "b-active",
        STATUS_DISABLED: "b-disabled",
    }.get(status, "b-disabled")
    return f'<span class="badge {cls}">{status}</span>'


def _admin_users_html(users: list[dict], csrf_token: str, message: str | None = None,
                      error: str | None = None) -> str:
    rows = ""
    for u in users:
        role_badge = (
            '<span class="badge b-admin">admin</span>' if u["role"] == ROLE_ADMIN else ""
        )
        status = _status_badge(u["accountStatus"])
        actions = ""
        if u["accountStatus"] in (STATUS_PENDING, STATUS_EMAIL_VERIFIED):
            actions += (
                f'<form class="inline" method="post" action="/admin/users/{u["identifier"]}/approve">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
                f'<button type="submit">Approve</button></form>'
            )
        if u["accountStatus"] != STATUS_DISABLED:
            actions += (
                f'<form class="inline" method="post" action="/admin/users/{u["identifier"]}/disable">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
                f'<button type="submit" class="secondary">Disable</button></form>'
            )
        if u["accountStatus"] == STATUS_DISABLED:
            actions += (
                f'<form class="inline" method="post" action="/admin/users/{u["identifier"]}/enable">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
                f'<button type="submit">Enable</button></form>'
            )
        if u["role"] != ROLE_ADMIN:
            actions += (
                f'<form class="inline" method="post" action="/admin/users/{u["identifier"]}/promote">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
                f'<button type="submit" class="secondary">Make admin</button></form>'
            )
        else:
            actions += (
                f'<form class="inline" method="post" action="/admin/users/{u["identifier"]}/demote">'
                f'<input type="hidden" name="csrf_token" value="{csrf_token}" />'
                f'<button type="submit" class="secondary">Make user</button></form>'
            )
        rows += (
            f"<tr><td>{u['identifier']}</td><td>{u['displayName'] or ''}</td>"
            f"<td>{u['email'] or ''}</td><td>{role_badge}</td><td>{status}</td>"
            f"<td>{actions}</td></tr>"
        )
    msg = ""
    if message:
        msg = f'<div class="success">{message}</div>'
    if error:
        msg = f'<div class="error">{error}</div>'
    body = f"""
    {msg}
    <h1>User administration</h1>
    <p class="sub">Approve pending users, disable accounts, and manage roles.</p>
    <table>
      <thead><tr><th>Username</th><th>Display name</th><th>Email</th><th>Role</th>
      <th>Status</th><th>Actions</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <a class="link" href="/">Back to app</a>"""
    return _page_shell("Admin — users", body)


def _forbidden_html() -> str:
    return _page_shell(
        "Forbidden",
        """<h2>Admin access required</h2>
        <p>You must be signed in as an administrator to view this page.</p>
        <a class="link" href="/login">Go to login</a>""",
    )


# --- Registration toggle ---------------------------------------------------

def _registration_enabled() -> bool:
    """Return True if self-service registration is allowed.

    Defaults to enabled unless ``REGISTER_ENABLED`` is explicitly ``0``.
    """
    return os.getenv("REGISTER_ENABLED", "1").lower() not in ("0", "false", "no")


# --- Route registration ----------------------------------------------------

def register_routes() -> None:
    """Add the auth routes to Chainlit's FastAPI app.

    Called from the ``@cl.on_app_startup`` hook. Adds:
    - ``/register`` (GET/POST) — self-service registration form
    - ``/verify-email`` (GET) — consume an email-verification token
    - ``/resend-verification`` (GET/POST) — request a new verification email
    - ``/forgot-password`` (GET/POST) — request a password-reset email
    - ``/reset-password`` (GET/POST) — set a new password from a reset token
    - ``/admin/users`` (GET) + ``/admin/users/<id>/<action>`` (POST) — admin UI
    - ``/public/elements`` static mount for persisted uploaded-file blobs

    Route ordering: Chainlit registers a catch-all
    ``/{full_path:path}`` for its SPA shell. We insert our routes at the
    FRONT of ``router.routes`` so they precede the catch-all.
    """
    from chainlit.server import app, router
    from fastapi import Request
    from fastapi.responses import HTMLResponse, RedirectResponse

    # Rate limiter (slowapi) — per-IP limits on auth form submissions.
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from starlette.routing import Route
    from starlette.staticfiles import StaticFiles

    from falkordb_harness.data_layer import _elements_dir

    limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
    app.state.limiter = limiter

    # Mount the elements directory so LocalStorageClient's /public/elements/...
    # URLs serve the actual files.
    elements_root = _elements_dir()
    app.mount(
        "/public/elements",
        StaticFiles(directory=str(elements_root)),
        name="elements",
    )

    def _csrf_from_form(form) -> str | None:
        return str(form.get("csrf_token", "")) or None

    async def _require_admin(request: Request) -> str | None:
        """Return the identifier of the authenticated admin, or ``None``.

        Decodes the Chainlit JWT from the cookie and looks up the user's
        role in the DB. Non-admins / unauthenticated visitors get ``None``.
        """
        from chainlit.auth.cookie import get_token_from_cookies
        from chainlit.auth.jwt import decode_jwt, get_jwt_secret

        secret = get_jwt_secret()
        if not secret:
            return None
        token = get_token_from_cookies(request.cookies)
        if not token:
            return None
        try:
            user = decode_jwt(token)
        except Exception:  # noqa: BLE001 — invalid/expired token
            return None
        identifier = getattr(user, "identifier", None)
        if not identifier:
            return None
        role = await get_user_role(identifier)
        return identifier if role == ROLE_ADMIN else None

    # --- /register ---------------------------------------------------------

    async def register_page(request: Request) -> HTMLResponse:
        if not _registration_enabled():
            return HTMLResponse(_register_disabled_html())
        return HTMLResponse(_register_html(csrf_token=issue_csrf_token()))

    @limiter.limit("5/minute")
    async def register_submit(request: Request) -> HTMLResponse | RedirectResponse:
        if not _registration_enabled():
            return HTMLResponse(_register_disabled_html())
        form = await request.form()
        if not verify_csrf_token(_csrf_from_form(form)):
            return HTMLResponse(_register_html(error="Invalid form submission."), status_code=403)
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        email = str(form.get("email", "")).strip()
        display_name = str(form.get("display_name", "")).strip() or None

        user, error, verify_token = await register_user(username, password, email, display_name)
        if user is None:
            assert error is not None
            return HTMLResponse(_register_html(error=error, csrf_token=issue_csrf_token()),
                                status_code=400)
        # Send the verification email (best-effort; failures are logged).
        if verify_token and email:
            await send_verification_email(email, verify_token)
        return RedirectResponse(url="/login?registered=1", status_code=303)

    # --- /verify-email -----------------------------------------------------

    async def verify_email_page(request: Request) -> HTMLResponse:
        token = request.query_params.get("token", "")
        ok, message = await verify_email_token(token)
        return HTMLResponse(_verify_email_html(message, ok), status_code=200 if ok else 400)

    # --- /resend-verification ---------------------------------------------

    async def resend_verification_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_resend_verification_html(csrf_token=issue_csrf_token()))

    @limiter.limit("3/minute")
    async def resend_verification_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        if not verify_csrf_token(_csrf_from_form(form)):
            return HTMLResponse(_resend_verification_html(error="Invalid form submission."),
                                status_code=403)
        email = str(form.get("email", "")).strip()
        _ok, info = await resend_verification_email(email)
        return HTMLResponse(_resend_verification_html(info=info, csrf_token=issue_csrf_token()))

    # --- /forgot-password --------------------------------------------------

    async def forgot_password_page(request: Request) -> HTMLResponse:
        return HTMLResponse(_forgot_password_html(csrf_token=issue_csrf_token()))

    @limiter.limit("3/minute")
    async def forgot_password_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        if not verify_csrf_token(_csrf_from_form(form)):
            return HTMLResponse(_forgot_password_html(error="Invalid form submission."),
                                status_code=403)
        email = str(form.get("email", "")).strip()
        await request_password_reset(email)
        return HTMLResponse(
            _forgot_password_html(
                info="If that email is registered, a reset link has been sent.",
                csrf_token=issue_csrf_token(),
            )
        )

    # --- /reset-password ---------------------------------------------------

    async def reset_password_page(request: Request) -> HTMLResponse:
        token = request.query_params.get("token", "")
        if not token:
            return HTMLResponse(_page_shell("Reset password",
                                             '<h1>Reset password</h1>'
                                             '<div class="error">Invalid reset link.</div>'),
                                status_code=400)
        return HTMLResponse(_reset_password_html(token, csrf_token=issue_csrf_token()))

    @limiter.limit("3/minute")
    async def reset_password_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        if not verify_csrf_token(_csrf_from_form(form)):
            return HTMLResponse(_reset_password_html(
                str(form.get("token", "")), error="Invalid form submission."), status_code=403)
        token = str(form.get("token", ""))
        password = str(form.get("password", ""))
        ok, message = await reset_password(token, password)
        if ok:
            return HTMLResponse(_page_shell("Reset password",
                                             f'<h1>Reset password</h1>'
                                             f'<div class="success">{message}</div>'
                                             f'<a class="link" href="/login">Go to login</a>'))
        return HTMLResponse(_reset_password_html(token, error=message,
                                                 csrf_token=issue_csrf_token()), status_code=400)

    # --- /admin/users ------------------------------------------------------

    async def admin_users_page(request: Request) -> HTMLResponse:
        admin_id = await _require_admin(request)
        if admin_id is None:
            return HTMLResponse(_forbidden_html(), status_code=403)
        users = await list_users()
        return HTMLResponse(_admin_users_html(users, csrf_token=issue_csrf_token()))

    async def _admin_action(request: Request) -> HTMLResponse | RedirectResponse:
        admin_id = await _require_admin(request)
        if admin_id is None:
            return HTMLResponse(_forbidden_html(), status_code=403)
        form = await request.form()
        if not verify_csrf_token(_csrf_from_form(form)):
            return RedirectResponse(url="/admin/users?error=invalid", status_code=303)
        # Path: /admin/users/{identifier}/{action}
        path = request.url.path
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            return RedirectResponse(url="/admin/users?error=badpath", status_code=303)
        identifier = parts[2]
        action = parts[3]
        dispatch = {
            "approve": approve_user,
            "reject": reject_user,
            "disable": disable_user,
            "enable": enable_user,
        }
        if action == "promote":
            ok, _msg = await set_user_role(identifier, ROLE_ADMIN)
        elif action == "demote":
            ok, _msg = await set_user_role(identifier, ROLE_USER)
        elif action in dispatch:
            ok, _msg = await dispatch[action](identifier)
        else:
            ok, _msg = False, "Unknown action."
        query = f"?{'ok' if ok else 'error'}=1"
        return RedirectResponse(url=f"/admin/users{query}", status_code=303)

    # Build the Starlette Route objects and insert them at the FRONT of
    # Chainlit's router (ahead of the ``/{full_path:path}`` catch-all).
    new_routes = [
        Route("/register", endpoint=register_page, methods=["GET"], name="register_page"),
        Route("/register", endpoint=register_submit, methods=["POST"], name="register_submit"),
        Route("/verify-email", endpoint=verify_email_page, methods=["GET"], name="verify_email"),
        Route("/resend-verification", endpoint=resend_verification_page, methods=["GET"],
              name="resend_verification_page"),
        Route("/resend-verification", endpoint=resend_verification_submit, methods=["POST"],
              name="resend_verification_submit"),
        Route("/forgot-password", endpoint=forgot_password_page, methods=["GET"],
              name="forgot_password_page"),
        Route("/forgot-password", endpoint=forgot_password_submit, methods=["POST"],
              name="forgot_password_submit"),
        Route("/reset-password", endpoint=reset_password_page, methods=["GET"],
              name="reset_password_page"),
        Route("/reset-password", endpoint=reset_password_submit, methods=["POST"],
              name="reset_password_submit"),
        Route("/admin/users", endpoint=admin_users_page, methods=["GET"], name="admin_users"),
        Route("/admin/users/{identifier}/{action}", endpoint=_admin_action, methods=["POST"],
              name="admin_user_action"),
    ]
    # Insert in reverse so the first route ends up first in the list.
    for route in reversed(new_routes):
        router.routes.insert(0, route)


# --- Chainlit callback -----------------------------------------------------

def _configure_password_auth() -> None:
    """Register the password auth callback with Chainlit at import time."""
    import chainlit as cl

    @cl.password_auth_callback
    async def _callback(username: str, password: str) -> User | None:
        return await verify_credentials(username, password)


_configure_password_auth()