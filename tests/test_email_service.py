"""Tests for the SMTP email service.

Mocks ``aiosmtplib.send`` so no real mail server is required. Verifies the
right transactional email is composed and addressed for each account-
lifecycle notification, and that unconfigured SMTP is a safe no-op.
"""

import asyncio
import quopri
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _run(coro):
    return asyncio.run(coro)


def _html_body(msg) -> str:
    """Extract and decode the HTML alternative payload of an EmailMessage."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            raw = part.get_payload(decode=True)
            if raw is not None:
                return raw.decode("utf-8")
    # Fallback: decode the raw string payload as quoted-printable.
    payload = msg.get_payload()[1].get_payload()
    return quopri.decodestring(payload.encode()).decode()


def test_send_verification_email_calls_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("APP_BASE_URL", "https://kg.example.com")
    from falkordb_harness import email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock()) as mock_send:
        _run(email_service.send_verification_email("user@example.com", "tok123"))

    assert mock_send.await_count == 1
    msg = mock_send.await_args.args[0]
    assert msg["To"] == "user@example.com"
    assert msg["From"] == "noreply@example.com"
    assert "Verify" in msg["Subject"]
    html = _html_body(msg)
    assert "https://kg.example.com/verify-email?token=tok123" in html


def test_send_approval_notification(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("APP_BASE_URL", "https://kg.example.com")
    from falkordb_harness import email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock()) as mock_send:
        _run(email_service.send_approval_notification("user@example.com"))

    msg = mock_send.await_args.args[0]
    assert "approved" in msg["Subject"].lower()
    assert "https://kg.example.com/login" in _html_body(msg)


def test_send_password_reset_email(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("APP_BASE_URL", "https://kg.example.com")
    from falkordb_harness import email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock()) as mock_send:
        _run(email_service.send_password_reset_email("user@example.com", "resettok"))

    msg = mock_send.await_args.args[0]
    assert "reset" in msg["Subject"].lower()
    html = _html_body(msg)
    assert "https://kg.example.com/reset-password?token=resettok" in html
    assert "expires" in html.lower()


def test_unconfigured_smtp_is_noop(monkeypatch, caplog):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    from falkordb_harness import email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock()) as mock_send:
        _run(email_service.send_verification_email("user@example.com", "tok"))

    assert mock_send.await_count == 0  # nothing sent


def test_smtp_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    from falkordb_harness import email_service

    with patch.object(email_service.aiosmtplib, "send", new=AsyncMock(
            side_effect=ConnectionRefusedError("no mail server"))):
        # Must not raise — mail failure must never crash the auth flow.
        _run(email_service.send_verification_email("user@example.com", "tok"))