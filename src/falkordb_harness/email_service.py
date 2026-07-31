"""Async SMTP email service for account lifecycle notifications.

Sends transactional emails for the public-deployment auth flow:
- email-verification links after self-service registration
- approval notifications once an admin unlocks an account
- password-reset links for the forgot-password flow

Configuration is entirely env-driven so no secrets live in the repo:

- ``SMTP_HOST``            SMTP server hostname (required to send mail).
- ``SMTP_PORT``            SMTP server port (default 587).
- ``SMTP_USER``            SMTP username (optional).
- ``SMTP_PASSWORD``        SMTP password (optional).
- ``SMTP_USE_TLS``         ``1``/true → STARTTLS (default, port 587),
                          ``0``/false → plaintext or implicit TLS
                          depending on port. For port 465, implicit TLS
                          is used automatically.
- ``SMTP_FROM``            ``From`` address (required to send mail).
- ``APP_BASE_URL``         Public base URL of the deployed app, used to
                          build absolute links in email bodies. Defaults
                          to ``http://localhost:8000`` for local dev.

When ``SMTP_HOST``/``SMTP_FROM`` are unset, sending is skipped and a
warning is logged — registration still succeeds, the user just won't
receive the email (an operator can resend later). This keeps the app
runnable in dev without a mail server, while failing loudly in prod if
mail is misconfigured.
"""

from __future__ import annotations

import logging
import os
from email.message import EmailMessage

import aiosmtplib

logger = logging.getLogger("falkordb_harness.email_service")


def _bool_env(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _smtp_config() -> dict:
    """Return SMTP connection params derived from env vars."""
    port = int(os.getenv("SMTP_PORT", "587"))
    use_tls = _bool_env("SMTP_USE_TLS", True)
    # Port 465 uses implicit TLS (SMTPS); other ports use STARTTLS when enabled.
    use_starttls = use_tls and port != 465
    use_ssl = port == 465
    return {
        "hostname": os.getenv("SMTP_HOST", ""),
        "port": port,
        "username": os.getenv("SMTP_USER") or None,
        "password": os.getenv("SMTP_PASSWORD") or None,
        "start_tls": use_starttls,
        "use_tls": use_ssl,
    }


def _from_addr() -> str:
    return os.getenv("SMTP_FROM", "")


def _app_base_url() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def _is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST")) and bool(os.getenv("SMTP_FROM"))


async def _send(message: EmailMessage) -> None:
    """Send a single message via SMTP, or log+skip when unconfigured."""
    if not _is_configured():
        logger.warning(
            "SMTP_HOST/SMTP_FROM not set — skipping email send to %s. "
            "Set these env vars to deliver account notifications.",
            message["To"],
        )
        return
    config = _smtp_config()
    try:
        await aiosmtplib.send(message, **config)
        logger.info("Sent email to %s (subject=%r)", message["To"], message["Subject"])
    except Exception as exc:  # noqa: BLE001 — never crash auth on mail failure
        logger.error("Failed to send email to %s: %s", message["To"], exc)


def _build_message(to: str, subject: str, body_html: str, body_text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = _from_addr()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")
    return msg


async def send_verification_email(email: str, token: str) -> None:
    """Send the email-verification link for a newly registered account."""
    link = f"{_app_base_url()}/verify-email?token={token}"
    html = f"""<!doctype html>
<html><body style="font-family:system-ui,sans-serif;color:#222;max-width:560px;margin:auto">
<h2>Verify your email</h2>
<p>Confirm your email address to complete registration for the FalkorDB KG Agent.</p>
<p><a href="{link}" style="display:inline-block;padding:.6rem 1.1rem;background:#4a9eff;
color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Verify email</a></p>
<p style="color:#888;font-size:.85rem">If the button doesn't work, copy this link into your browser:<br>
{link}</p>
<p style="color:#888;font-size:.85rem">If you did not create an account, you can ignore this email.</p>
</body></html>"""
    text = (
        "Verify your email\n\n"
        f"Confirm your email to complete registration:\n{link}\n\n"
        "If you did not create an account, ignore this email."
    )
    await _send(_build_message(email, "Verify your email — FalkorDB KG Agent", html, text))


async def send_approval_notification(email: str) -> None:
    """Notify a user that an admin approved their account."""
    login_url = f"{_app_base_url()}/login"
    html = f"""<!doctype html>
<html><body style="font-family:system-ui,sans-serif;color:#222;max-width:560px;margin:auto">
<h2>Your account has been approved</h2>
<p>An administrator approved your account for the FalkorDB KG Agent. You can now log in.</p>
<p><a href="{login_url}" style="display:inline-block;padding:.6rem 1.1rem;background:#4a9eff;
color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Log in</a></p>
</body></html>"""
    text = (
        "Your account has been approved\n\n"
        f"An administrator approved your account. Log in at: {login_url}"
    )
    await _send(_build_message(email, "Account approved — FalkorDB KG Agent", html, text))


async def send_password_reset_email(email: str, token: str) -> None:
    """Send a password-reset link (valid for a limited time)."""
    link = f"{_app_base_url()}/reset-password?token={token}"
    html = f"""<!doctype html>
<html><body style="font-family:system-ui,sans-serif;color:#222;max-width:560px;margin:auto">
<h2>Reset your password</h2>
<p>Use the link below to set a new password for your FalkorDB KG Agent account. The link expires in 1 hour.</p>
<p><a href="{link}" style="display:inline-block;padding:.6rem 1.1rem;background:#4a9eff;
color:#fff;border-radius:8px;text-decoration:none;font-weight:600">Reset password</a></p>
<p style="color:#888;font-size:.85rem">If the button doesn't work, copy this link into your browser:<br>
{link}</p>
<p style="color:#888;font-size:.85rem">If you did not request a password reset, you can ignore this email.</p>
</body></html>"""
    text = (
        "Reset your password\n\n"
        f"Set a new password (link expires in 1 hour):\n{link}\n\n"
        "If you did not request this, ignore this email."
    )
    await _send(_build_message(email, "Reset your password — FalkorDB KG Agent", html, text))