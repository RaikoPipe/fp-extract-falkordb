"""Tests for password auth + gated self-service registration.

Covers:
- ``register_user`` happy path, duplicate rejection, weak-password rejection,
  empty-username rejection, email validation, and that the stored row carries
  a bcrypt hash + ``pending`` status + an email-verify token.
- ``verify_credentials`` success, wrong-password failure, unknown-user
  failure, empty inputs, account-without-hash rejection, and that non-active
  accounts (pending / disabled) cannot log in.
- email verification token lifecycle (valid / invalid / expired / reused).
- password reset token lifecycle (valid / invalid / expired / reuse).
- admin operations (approve / disable / enable / set role / list).
- first-admin bootstrap from env vars (create + idempotent + promote).
- legacy account migration (passwordless rows → disabled).
- password-strength validator.
- bcrypt round-trip via ``_hash_password`` / ``_verify_password``.

All tests run against a throwaway SQLite database under tmp so no real data
is touched. ``DATABASE_URL`` is set per-test via monkeypatch.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def _mock_smtp_send():
    """Block every test in this module from making a real SMTP connection.

    ``falkordb_harness.email_service._send`` calls ``aiosmtplib.send`` when
    ``SMTP_HOST``/``SMTP_FROM`` are set, and the on-disk ``.env`` carries
    live SMTP credentials that chainlit loads at import time. Any test that
    ends up calling ``approve_user`` / ``request_password_reset`` /
    ``resend_verification_email`` would therefore try to contact a real
    mail server. Replacing ``aiosmtplib.send`` with a no-op ``AsyncMock``
    keeps the auth-flow coverage without ever sending mail.
    """
    import falkordb_harness.email_service as email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock()):
        yield


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the data layer + auth at a fresh SQLite file per test."""
    db_file = tmp_path / "cl_auth_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force the elements dir under tmp so we don't pollute ./data.
    monkeypatch.setenv("ELEMENTS_DIR", str(tmp_path / "elements"))
    # Stable CSRF/JWT secret so token round-trips are deterministic.
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-key-for-csrf-jwt")
    return db_file


async def _init(tmp_db):
    """Create the schema in the temp DB once per test."""
    from falkordb_harness.data_layer import build_data_layer, init_db

    layer = build_data_layer()
    await init_db(layer)
    await layer.engine.dispose()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _register(tmp_db, username="alice", password="Supersecret1", email=None, display="Alice"):
    """Helper: init + register, returning (user, error, token)."""
    _run(_init(tmp_db))
    from falkordb_harness.auth import register_user

    return _run(
        register_user(
            username, password, email or f"{username}@example.com", display
        )
    )


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------
def test_register_user_creates_account(tmp_db):
    user, err, token = _register(tmp_db)
    assert err is None
    assert user is not None
    assert user.identifier == "alice"
    assert user.display_name == "Alice"
    assert token  # a verification token is returned


def test_register_user_rejects_duplicate_username(tmp_db):
    _run(_init(tmp_db))
    from falkordb_harness.auth import register_user

    _run(register_user("alice", "Supersecret1", "alice@example.com"))
    user, err, _token = _run(register_user("alice", "Anotherpw1", "alice2@example.com"))
    assert user is None
    assert "already taken" in err


def test_register_user_rejects_duplicate_email(tmp_db):
    _run(_init(tmp_db))
    from falkordb_harness.auth import register_user

    _run(register_user("alice", "Supersecret1", "shared@example.com"))
    user, err, _token = _run(register_user("bob", "Anotherpw1", "shared@example.com"))
    assert user is None
    assert "email" in err


def test_register_user_rejects_short_password(tmp_db):
    from falkordb_harness.auth import MIN_PASSWORD_LEN

    user, err, _token = _register(tmp_db, password="short1")
    assert user is None
    assert str(MIN_PASSWORD_LEN) in err


def test_register_user_rejects_no_letter(tmp_db):

    user, err, _token = _register(tmp_db, password="1234567890")
    assert user is None
    assert "letter" in err


def test_register_user_rejects_no_digit(tmp_db):

    user, err, _token = _register(tmp_db, password="supersecret")
    assert user is None
    assert "digit" in err


def test_register_user_rejects_common_password(tmp_db):

    user, err, _token = _register(tmp_db, password="password123")
    assert user is None
    assert "common" in err


def test_register_user_rejects_empty_username(tmp_db):
    user, err, _token = _register(tmp_db, username="   ")
    assert user is None
    assert "Username" in err


def test_register_user_rejects_bad_email(tmp_db):
    user, err, _token = _register(tmp_db, email="not-an-email")
    assert user is None
    assert err


def test_register_user_stores_bcrypt_hash_and_pending_status(tmp_db):
    from sqlalchemy import text

    from falkordb_harness.data_layer import build_data_layer

    _register(tmp_db, username="carol")
    layer = build_data_layer()

    async def fetch():
        async with layer.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT "passwordHash", "accountStatus", "role", '
                        '"emailVerifyToken" FROM users WHERE "identifier" = :u'
                    ),
                    {"u": "carol"},
                )
            ).fetchone()
        await layer.engine.dispose()
        return row

    row = _run(fetch())
    assert row is not None
    assert row[0].startswith("$2")  # bcrypt
    assert row[1] == "pending"
    assert row[2] == "user"
    assert row[3]  # verify token present


# ---------------------------------------------------------------------------
# verify_credentials
# ---------------------------------------------------------------------------
def test_verify_credentials_rejects_pending(tmp_db):
    """A freshly registered (pending) account cannot log in."""
    from falkordb_harness.auth import verify_credentials

    _register(tmp_db, username="dave")
    user = _run(verify_credentials("dave", "Supersecret1"))
    assert user is None


def test_verify_credentials_success_after_activation(tmp_db):
    from falkordb_harness.auth import (
        approve_user,
        verify_credentials,
    )

    _register(tmp_db, username="dave")
    _run(approve_user("dave"))
    user = _run(verify_credentials("dave", "Supersecret1"))
    assert user is not None
    assert user.identifier == "dave"
    assert user.metadata["role"] == "user"
    assert user.metadata["email"] == "dave@example.com"


def test_verify_credentials_wrong_password(tmp_db):
    from falkordb_harness.auth import approve_user, verify_credentials

    _register(tmp_db, username="eve")
    _run(approve_user("eve"))
    assert _run(verify_credentials("eve", "wrongpassword")) is None


def test_verify_credentials_unknown_user(tmp_db):
    from falkordb_harness.auth import verify_credentials

    _run(_init(tmp_db))
    assert _run(verify_credentials("nobody", "Supersecret1")) is None


def test_verify_credentials_empty_inputs(tmp_db):
    from falkordb_harness.auth import verify_credentials

    _run(_init(tmp_db))
    assert _run(verify_credentials("", "Supersecret1")) is None
    assert _run(verify_credentials("alice", "")) is None


def test_verify_credentials_rejects_disabled(tmp_db):
    from falkordb_harness.auth import (
        approve_user,
        disable_user,
        verify_credentials,
    )

    _register(tmp_db, username="frank")
    _run(approve_user("frank"))
    _run(disable_user("frank"))
    assert _run(verify_credentials("frank", "Supersecret1")) is None


def test_verify_credentials_rejects_account_without_hash(tmp_db):
    """An account with no stored password hash can never log in."""
    from sqlalchemy import text

    from falkordb_harness.auth import verify_credentials
    from falkordb_harness.data_layer import build_data_layer

    _run(_init(tmp_db))
    layer = build_data_layer()

    async def insert_no_hash():
        async with layer.engine.connect() as conn:
            await conn.execute(
                text(
                    'INSERT INTO users ("id", "identifier", "createdAt", "metadata", '
                    '"accountStatus") VALUES (:id, :i, :c, :m, :s)'
                ),
                {
                    "id": "abc",
                    "i": "ghost",
                    "c": "2026-01-01T00:00:00Z",
                    "m": "{}",
                    "s": "active",
                },
            )
            await conn.commit()
        await layer.engine.dispose()

    _run(insert_no_hash())
    assert _run(verify_credentials("ghost", "anything")) is None


# ---------------------------------------------------------------------------
# email verification
# ---------------------------------------------------------------------------
def test_verify_email_token_success(tmp_db):
    from falkordb_harness.auth import verify_email_token

    _, _, token = _register(tmp_db, username="greg")
    ok, msg = _run(verify_email_token(token))
    assert ok
    assert "verified" in msg or "review" in msg


def test_verify_email_token_invalid(tmp_db):
    from falkordb_harness.auth import verify_email_token

    _run(_init(tmp_db))
    ok, _msg = _run(verify_email_token("bogus-token"))
    assert not ok


def test_verify_email_token_expired(tmp_db):
    from sqlalchemy import text

    from falkordb_harness.auth import verify_email_token
    from falkordb_harness.data_layer import build_data_layer

    _, _, token = _register(tmp_db, username="hank")
    # Force the expiry into the past.
    layer = build_data_layer()

    async def expire():
        async with layer.engine.begin() as conn:
            await conn.execute(
                text(
                    'UPDATE users SET "emailVerifyExpires" = :e '
                    'WHERE "emailVerifyToken" = :t'
                ),
                {"e": "2000-01-01T00:00:00+00:00", "t": token},
            )
        await layer.engine.dispose()

    _run(expire())
    ok, msg = _run(verify_email_token(token))
    assert not ok
    assert "expired" in msg


def test_verify_email_token_reuse_rejected(tmp_db):
    from falkordb_harness.auth import verify_email_token

    _, _, token = _register(tmp_db, username="iris")
    _run(verify_email_token(token))
    # Second use: token was cleared, so it's now invalid.
    ok, _msg = _run(verify_email_token(token))
    assert not ok


# ---------------------------------------------------------------------------
# password reset
# ---------------------------------------------------------------------------
def test_password_reset_roundtrip(tmp_db):
    from falkordb_harness.auth import (
        approve_user,
        request_password_reset,
        reset_password,
        verify_credentials,
    )

    _register(tmp_db, username="jane")
    _run(approve_user("jane"))
    # Request reset — always returns True (no email actually sent in tests).
    assert _run(request_password_reset("jane@example.com")) is True
    # We need the token from the DB since the email isn't sent.
    from sqlalchemy import text

    from falkordb_harness.data_layer import build_data_layer

    layer = build_data_layer()

    async def get_token():
        async with layer.engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT "passwordResetToken" FROM users WHERE "identifier" = :u'),
                    {"u": "jane"},
                )
            ).fetchone()
        await layer.engine.dispose()
        return row[0]

    token = _run(get_token())
    ok, _msg = _run(reset_password(token, "Newpassword1"))
    assert ok
    # Old password no longer works; new one does.
    assert _run(verify_credentials("jane", "Supersecret1")) is None
    assert _run(verify_credentials("jane", "Newpassword1")) is not None


def test_password_reset_expired(tmp_db):
    from sqlalchemy import text

    from falkordb_harness.auth import (
        approve_user,
        request_password_reset,
        reset_password,
    )
    from falkordb_harness.data_layer import build_data_layer

    _register(tmp_db, username="kyle")
    _run(approve_user("kyle"))
    _run(request_password_reset("kyle@example.com"))
    layer = build_data_layer()

    async def expire():
        async with layer.engine.begin() as conn:
            await conn.execute(
                text(
                    'UPDATE users SET "passwordResetExpires" = :e '
                    'WHERE "identifier" = :u'
                ),
                {"e": "2000-01-01T00:00:00+00:00", "u": "kyle"},
            )
        await layer.engine.dispose()

    _run(expire())

    async def get_token():
        async with layer.engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT "passwordResetToken" FROM users WHERE "identifier" = :u'),
                    {"u": "kyle"},
                )
            ).fetchone()
        return row[0]

    token = _run(get_token())
    ok, msg = _run(reset_password(token, "Newpassword1"))
    assert not ok
    assert "expired" in msg


def test_password_reset_reuse_rejected(tmp_db):
    from sqlalchemy import text

    from falkordb_harness.auth import (
        approve_user,
        request_password_reset,
        reset_password,
    )
    from falkordb_harness.data_layer import build_data_layer

    _register(tmp_db, username="liam")
    _run(approve_user("liam"))
    _run(request_password_reset("liam@example.com"))
    layer = build_data_layer()

    async def get_token():
        async with layer.engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT "passwordResetToken" FROM users WHERE "identifier" = :u'),
                    {"u": "liam"},
                )
            ).fetchone()
        return row[0]

    token = _run(get_token())
    _run(reset_password(token, "Newpassword1"))
    # Reuse: token was cleared.
    ok, _msg = _run(reset_password(token, "Another1"))
    assert not ok


def test_password_reset_unknown_email_is_noop(tmp_db):
    from falkordb_harness.auth import request_password_reset

    _run(_init(tmp_db))
    # Should not raise and should return True (enumeration resistance).
    assert _run(request_password_reset("nobody@example.com")) is True


# ---------------------------------------------------------------------------
# admin operations
# ---------------------------------------------------------------------------
def test_approve_user_flips_status(tmp_db):
    from falkordb_harness.auth import approve_user, verify_credentials

    _register(tmp_db, username="mike")
    ok, _msg = _run(approve_user("mike"))
    assert ok
    assert _run(verify_credentials("mike", "Supersecret1")) is not None


def test_approve_unknown_user(tmp_db):
    from falkordb_harness.auth import approve_user

    _run(_init(tmp_db))
    ok, _msg = _run(approve_user("ghost"))
    assert not ok


def test_disable_and_enable_user(tmp_db):
    from falkordb_harness.auth import (
        approve_user,
        disable_user,
        enable_user,
        verify_credentials,
    )

    _register(tmp_db, username="nina")
    _run(approve_user("nina"))
    _run(disable_user("nina"))
    assert _run(verify_credentials("nina", "Supersecret1")) is None
    _run(enable_user("nina"))
    assert _run(verify_credentials("nina", "Supersecret1")) is not None


def test_set_user_role(tmp_db):
    from falkordb_harness.auth import (
        approve_user,
        get_user_role,
        set_user_role,
    )

    _register(tmp_db, username="oscar")
    _run(approve_user("oscar"))
    assert _run(get_user_role("oscar")) == "user"
    ok, _msg = _run(set_user_role("oscar", "admin"))
    assert ok
    assert _run(get_user_role("oscar")) == "admin"
    ok, _msg = _run(set_user_role("oscar", "user"))
    assert _run(get_user_role("oscar")) == "user"


def test_set_invalid_role_rejected(tmp_db):
    from falkordb_harness.auth import set_user_role

    _run(_init(tmp_db))
    ok, _msg = _run(set_user_role("anyone", "superuser"))
    assert not ok


def test_list_users(tmp_db):
    from falkordb_harness.auth import list_users, register_user

    _run(_init(tmp_db))
    _run(register_user("paul", "Supersecret1", "paul@example.com"))
    users = _run(list_users())
    assert len(users) == 1
    assert users[0]["identifier"] == "paul"
    assert users[0]["accountStatus"] == "pending"


# ---------------------------------------------------------------------------
# bootstrap admin from env
# ---------------------------------------------------------------------------
def test_bootstrap_admin_creates(tmp_db, monkeypatch):
    _run(_init(tmp_db))
    monkeypatch.setenv("FIRST_ADMIN_USERNAME", "rootadmin")
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "root@example.com")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "Rootadmin1")
    from falkordb_harness.auth import (
        bootstrap_admin_from_env,
        get_user_role,
        verify_credentials,
    )

    _run(bootstrap_admin_from_env())
    assert _run(get_user_role("rootadmin")) == "admin"
    assert _run(verify_credentials("rootadmin", "Rootadmin1")) is not None


def test_bootstrap_admin_idempotent(tmp_db, monkeypatch):
    _run(_init(tmp_db))
    monkeypatch.setenv("FIRST_ADMIN_USERNAME", "rootadmin")
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "root@example.com")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "Rootadmin1")
    from falkordb_harness.auth import bootstrap_admin_from_env, list_users

    _run(bootstrap_admin_from_env())
    _run(bootstrap_admin_from_env())
    users = _run(list_users())
    assert len(users) == 1


def test_bootstrap_admin_promotes_existing(tmp_db, monkeypatch):
    _run(_init(tmp_db))
    from falkordb_harness.auth import (
        bootstrap_admin_from_env,
        get_user_role,
        register_user,
        verify_credentials,
    )

    _run(register_user("existing", "Supersecret1", "existing@example.com"))
    monkeypatch.setenv("FIRST_ADMIN_USERNAME", "existing")
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "existing@example.com")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "NewAdminPass1")
    _run(bootstrap_admin_from_env())
    assert _run(get_user_role("existing")) == "admin"
    # Bootstrap must set the password on an existing user, not just the role —
    # otherwise the promoted account still can't log in (regression test).
    assert _run(verify_credentials("existing", "NewAdminPass1")) is not None
    assert _run(verify_credentials("existing", "Supersecret1")) is None


def test_bootstrap_admin_promotes_passwordless_active_user(tmp_db, monkeypatch):
    """A legacy passwordless + active row (pre-auth seed) must get a password
    set by bootstrap so it can finally log in."""
    from sqlalchemy import text

    from falkordb_harness.auth import bootstrap_admin_from_env, verify_credentials
    from falkordb_harness.data_layer import build_data_layer

    _run(_init(tmp_db))
    layer = build_data_layer()

    async def insert_legacy():
        async with layer.engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO users ("id", "identifier", "createdAt", "metadata", '
                    '"accountStatus", "role") VALUES (:id, :i, :c, :m, :s, :r)'
                ),
                {
                    "id": "legacy1",
                    "i": "oldseed",
                    "c": "2025-01-01T00:00:00Z",
                    "m": "{}",
                    "s": "active",
                    "r": "user",
                },
            )
        await layer.engine.dispose()

    _run(insert_legacy())
    monkeypatch.setenv("FIRST_ADMIN_USERNAME", "oldseed")
    monkeypatch.setenv("FIRST_ADMIN_EMAIL", "oldseed@example.com")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "FreshPass1")
    _run(bootstrap_admin_from_env())
    assert _run(verify_credentials("oldseed", "FreshPass1")) is not None


def test_bootstrap_admin_no_env_is_noop(tmp_db, monkeypatch):
    _run(_init(tmp_db))
    monkeypatch.delenv("FIRST_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("FIRST_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("FIRST_ADMIN_PASSWORD", raising=False)
    from falkordb_harness.auth import bootstrap_admin_from_env, list_users

    _run(bootstrap_admin_from_env())
    assert _run(list_users()) == []


# ---------------------------------------------------------------------------
# legacy account migration
# ---------------------------------------------------------------------------
def test_migrate_legacy_accounts_disables_passwordless(tmp_db):
    from sqlalchemy import text

    from falkordb_harness.auth import migrate_legacy_accounts
    from falkordb_harness.data_layer import build_data_layer

    _run(_init(tmp_db))
    layer = build_data_layer()

    async def insert_legacy():
        async with layer.engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO users ("id", "identifier", "createdAt", "metadata", '
                    '"accountStatus") VALUES (:id, :i, :c, :m, :s)'
                ),
                {
                    "id": "legacy1",
                    "i": "olduser",
                    "c": "2025-01-01T00:00:00Z",
                    "m": "{}",
                    "s": "pending",
                },
            )
        await layer.engine.dispose()

    _run(insert_legacy())
    _run(migrate_legacy_accounts())

    async def check():
        layer2 = build_data_layer()
        async with layer2.engine.connect() as conn:
            row = (
                await conn.execute(
                    text('SELECT "accountStatus" FROM users WHERE "identifier" = :u'),
                    {"u": "olduser"},
                )
            ).fetchone()
        await layer2.engine.dispose()
        return row[0]

    assert _run(check()) == "disabled"


def test_migrate_legacy_accounts_disables_passwordless_active(tmp_db):
    """A passwordless row that is already 'active' (a pre-auth seed that was
    never given a password) must also be disabled — leaving it active but
    passwordless is a silent login-blocker and an unsafe state."""
    from sqlalchemy import text

    from falkordb_harness.auth import migrate_legacy_accounts
    from falkordb_harness.data_layer import build_data_layer

    _run(_init(tmp_db))
    layer = build_data_layer()

    async def insert_legacy_active():
        async with layer.engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO users ("id", "identifier", "createdAt", "metadata", '
                    '"accountStatus", "role") VALUES (:id, :i, :c, :m, :s, :r)'
                ),
                {
                    "id": "legacy2",
                    "i": "activeseed",
                    "c": "2025-01-01T00:00:00Z",
                    "m": "{}",
                    "s": "active",
                    "r": "admin",
                },
            )
        await layer.engine.dispose()

    _run(insert_legacy_active())
    _run(migrate_legacy_accounts())

    async def check():
        layer2 = build_data_layer()
        async with layer2.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        'SELECT "accountStatus", "role" FROM users '
                        'WHERE "identifier" = :u'
                    ),
                    {"u": "activeseed"},
                )
            ).fetchone()
        await layer2.engine.dispose()
        return row

    status, role = _run(check())
    assert status == "disabled"
    assert role == "user"  # role reset to user; bootstrap re-promotes if intended


# ---------------------------------------------------------------------------
# password strength validator
# ---------------------------------------------------------------------------
def test_validate_password_strength_accepts_strong():
    from falkordb_harness.auth import validate_password_strength

    assert validate_password_strength("GoodPass12") is None
    assert validate_password_strength("abc1234567") is None


def test_validate_password_strength_rejects_weak():
    from falkordb_harness.auth import validate_password_strength

    assert validate_password_strength("short1") is not None
    assert validate_password_strength("allletters") is not None
    assert validate_password_strength("1234567890") is not None
    assert validate_password_strength("password123") is not None
    assert validate_password_strength("") is not None


# ---------------------------------------------------------------------------
# bcrypt helpers
# ---------------------------------------------------------------------------
def test_hash_and_verify_password_roundtrip():
    from falkordb_harness.auth import _hash_password, _verify_password

    h = _hash_password("mypassword")
    assert h != "mypassword"
    assert _verify_password("mypassword", h) is True
    assert _verify_password("notmypassword", h) is False


def test_verify_password_handles_malformed_hash():
    from falkordb_harness.auth import _verify_password

    assert _verify_password("x", "not-a-hash") is False


# ---------------------------------------------------------------------------
# CSRF token round-trip
# ---------------------------------------------------------------------------
def test_csrf_token_roundtrip(monkeypatch):
    # Import before setenv: chainlit's load_dotenv(override=True) runs at
    # import and would overwrite our monkeypatched value from the on-disk .env.
    from falkordb_harness.auth import issue_csrf_token, verify_csrf_token

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "csrf-test-secret")
    token = issue_csrf_token()
    assert verify_csrf_token(token) is True
    assert verify_csrf_token("garbage") is False
    assert verify_csrf_token(None) is False


def test_csrf_token_rejects_wrong_secret(monkeypatch):
    from falkordb_harness.auth import issue_csrf_token

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "secret-a")
    token = issue_csrf_token()
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "secret-b")
    from falkordb_harness.auth import verify_csrf_token

    assert verify_csrf_token(token) is False


# ---------------------------------------------------------------------------
# registration toggle
# ---------------------------------------------------------------------------
def test_registration_enabled_default(monkeypatch):
    from falkordb_harness.auth import _registration_enabled

    monkeypatch.delenv("REGISTER_ENABLED", raising=False)
    assert _registration_enabled() is True


def test_registration_disabled(monkeypatch):
    from falkordb_harness.auth import _registration_enabled

    monkeypatch.setenv("REGISTER_ENABLED", "0")
    assert _registration_enabled() is False


# ---------------------------------------------------------------------------
# C5: CSRF signer fails fast on missing secret
# ---------------------------------------------------------------------------
def test_csrf_signer_raises_without_secret(monkeypatch):
    # Import before deleting the env var: importing falkordb_harness.auth
    # triggers chainlit's load_dotenv(override=True), which would re-set
    # the secret from the on-disk .env and defeat the monkeypatch.
    from falkordb_harness.auth import _csrf_secret

    monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CHAINLIT_AUTH_SECRET"):
        _csrf_secret()


def test_csrf_signer_raises_on_empty_secret(monkeypatch):
    from falkordb_harness.auth import _csrf_secret

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "")
    with pytest.raises(RuntimeError, match="CHAINLIT_AUTH_SECRET"):
        _csrf_secret()


# ---------------------------------------------------------------------------
# C7: CSRF tokens bound to session identifier
# ---------------------------------------------------------------------------
def test_csrf_token_bound_to_identifier(monkeypatch):
    # Import before setenv: chainlit's load_dotenv(override=True) runs at
    # import and would overwrite our monkeypatched value from the on-disk .env.
    from falkordb_harness.auth import issue_csrf_token, verify_csrf_token

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "bind-test-secret")
    token = issue_csrf_token("admin-alice")
    # Correct identifier → valid
    assert verify_csrf_token(token, expected_identifier="admin-alice") is True
    # Wrong identifier → rejected
    assert verify_csrf_token(token, expected_identifier="admin-bob") is False
    # No expected_identifier → rejected (bound token requires a match)
    assert verify_csrf_token(token) is False


def test_csrf_anonymous_token_accepted_without_identifier(monkeypatch):
    from falkordb_harness.auth import issue_csrf_token, verify_csrf_token

    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "bind-test-secret")
    # Anonymous token (no identifier) — used by /register, /reset-password
    token = issue_csrf_token()
    assert verify_csrf_token(token) is True
    assert verify_csrf_token(token, expected_identifier="someone") is True


# ---------------------------------------------------------------------------
# C6b: charset validation on username / display_name
# ---------------------------------------------------------------------------
def test_register_rejects_html_in_username(tmp_db):
    user, err, _ = _register(tmp_db, username="<img src=x>")
    assert user is None
    assert "letters" in err or "may only" in err


def test_register_rejects_newline_in_username(tmp_db):
    user, err, _ = _register(tmp_db, username="evil\nuser")
    assert user is None
    assert "letters" in err or "may only" in err


def test_register_rejects_html_in_display_name(tmp_db):
    _run(_init(tmp_db))
    from falkordb_harness.auth import register_user

    user, err, _ = _run(
        register_user("bob", "Supersecret1", "bob@example.com", "<script>alert(1)</script>")
    )
    assert user is None
    assert "display" in err.lower() or "may only" in err.lower()


def test_register_rejects_oversized_display_name(tmp_db):
    _run(_init(tmp_db))
    from falkordb_harness.auth import MAX_DISPLAY_NAME_LEN, register_user

    user, err, _ = _run(
        register_user("bob", "Supersecret1", "bob@example.com", "A" * (MAX_DISPLAY_NAME_LEN + 1))
    )
    assert user is None
    assert "display" in err.lower() and "most" in err.lower()


def test_register_accepts_safe_display_name(tmp_db):
    user, err, _ = _register(tmp_db, username="bob", display="Bob_O.v2")
    assert user is not None
    assert err is None
    assert user.display_name == "Bob_O.v2"


def test_register_allows_empty_display_name(tmp_db):
    user, err, _ = _register(tmp_db, username="bob", display="")
    assert user is not None
    assert err is None


# ---------------------------------------------------------------------------
# C6a: HTML escaping in admin users page
# ---------------------------------------------------------------------------
def test_admin_users_html_escapes_payload(tmp_db):
    _run(_init(tmp_db))
    from falkordb_harness.auth import _admin_users_html, issue_csrf_token

    # Simulate a malicious user row that slipped through (defense-in-depth:
    # charset validation should block this, but escaping must still neutralize
    # any HTML metacharacters if a row contains them).
    malicious = {
        "identifier": "<script>alert(1)</script>",
        "displayName": '<img src=x onerror=alert(1)>',
        "email": "x@y.com<script>",
        "role": "user",
        "accountStatus": "pending",
    }
    html_out = _admin_users_html([malicious], csrf_token=issue_csrf_token("admin"))
    assert "<script>alert(1)</script>" not in html_out
    assert "<img src=x onerror=alert(1)>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "&lt;img" in html_out