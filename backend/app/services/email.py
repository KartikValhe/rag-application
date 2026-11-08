"""
app/services/email.py
──────────────────────
Thin email sender.

In development (ENVIRONMENT=development or MAIL_HOST=localhost) emails are
printed to stdout as well as sent to Mailpit (which catches them on port 1025).
In production an SMTP relay (SES, Postmark, Resend, etc.) is configured via
environment variables — no code change required.

Templates are plain f-strings here.  Phase 3 will upgrade to Jinja2 HTML
templates with tenant branding (logo, colors from the token system).
"""

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger(__name__)


async def _send(*, to: str, subject: str, body_text: str, body_html: str) -> None:
    """Low-level SMTP send.  Called by all public helpers below."""
    message = MIMEMultipart("alternative")
    message["From"] = f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>"
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(body_text, "plain"))
    message.attach(MIMEText(body_html, "html"))

    if settings.is_development:
        # Always log the email body in dev so you can grab links from the console.
        logger.info(
            "DEV EMAIL\n  To: %s\n  Subject: %s\n\n%s", to, subject, body_text
        )

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.MAIL_HOST,
            port=settings.MAIL_PORT,
            username=settings.MAIL_USERNAME or None,
            password=settings.MAIL_PASSWORD or None,
            use_tls=settings.MAIL_USE_TLS,
            start_tls=False,
        )
    except Exception as exc:  # noqa: BLE001
        # In dev, non-fatal — the console log is enough.
        # In production this should propagate or be handled by a retry queue.
        if settings.is_development:
            logger.warning("Email send failed (dev mode): %s", exc)
        else:
            raise


async def send_verification_email(
    *,
    to_email: str,
    full_name: str,
    verify_url: str,
    tenant_name: str,
) -> None:
    subject = f"Verify your email — {tenant_name}"
    text = (
        f"Hi {full_name},\n\n"
        f"Please verify your email address by clicking the link below:\n\n"
        f"{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"— {tenant_name}"
    )
    html = f"""
<p>Hi {full_name},</p>
<p>Please verify your email address by clicking the button below:</p>
<p><a href="{verify_url}" style="background:#4f46e5;color:#fff;padding:10px 20px;
   border-radius:6px;text-decoration:none;">Verify Email</a></p>
<p>Or copy this link: {verify_url}</p>
<p>This link expires in 24 hours.</p>
<p>— {tenant_name}</p>
"""
    await _send(to=to_email, subject=subject, body_text=text, body_html=html)


async def send_password_reset_email(
    *,
    to_email: str,
    full_name: str,
    reset_url: str,
    tenant_name: str,
) -> None:
    subject = f"Reset your password — {tenant_name}"
    text = (
        f"Hi {full_name},\n\n"
        f"Click the link below to reset your password:\n\n"
        f"{reset_url}\n\n"
        f"This link expires in 1 hour.  If you did not request this, ignore this email.\n\n"
        f"— {tenant_name}"
    )
    html = f"""
<p>Hi {full_name},</p>
<p>Click the button below to reset your password:</p>
<p><a href="{reset_url}" style="background:#4f46e5;color:#fff;padding:10px 20px;
   border-radius:6px;text-decoration:none;">Reset Password</a></p>
<p>Or copy this link: {reset_url}</p>
<p>This link expires in 1 hour. If you did not request this, ignore this email.</p>
<p>— {tenant_name}</p>
"""
    await _send(to=to_email, subject=subject, body_text=text, body_html=html)


async def send_invitation_email(
    *,
    to_email: str,
    invited_by_name: str,
    accept_url: str,
    tenant_name: str,
    role_name: str,
) -> None:
    subject = f"You're invited to join {tenant_name}"
    text = (
        f"Hi,\n\n"
        f"{invited_by_name} has invited you to join {tenant_name} as a {role_name}.\n\n"
        f"Click the link below to accept the invitation and create your account:\n\n"
        f"{accept_url}\n\n"
        f"This link expires in 7 days.\n\n"
        f"— {tenant_name}"
    )
    html = f"""
<p>Hi,</p>
<p><strong>{invited_by_name}</strong> has invited you to join
   <strong>{tenant_name}</strong> as a <strong>{role_name}</strong>.</p>
<p><a href="{accept_url}" style="background:#4f46e5;color:#fff;padding:10px 20px;
   border-radius:6px;text-decoration:none;">Accept Invitation</a></p>
<p>Or copy this link: {accept_url}</p>
<p>This link expires in 7 days.</p>
<p>— {tenant_name}</p>
"""
    await _send(to=to_email, subject=subject, body_text=text, body_html=html)


async def send_temp_password_email(
    *,
    to_email: str,
    full_name: str,
    temp_password: str,
    login_url: str,
    tenant_name: str,
) -> None:
    subject = f"Your admin account on {tenant_name}"
    text = (
        f"Hi {full_name},\n\n"
        f"A superadmin has created an admin account for you on {tenant_name}.\n\n"
        f"Email:    {to_email}\n"
        f"Password: {temp_password}\n\n"
        f"Log in at: {login_url}\n\n"
        f"Please change your password immediately after logging in.\n\n"
        f"— Platform Team"
    )
    html = f"""
<p>Hi {full_name},</p>
<p>A superadmin has created an admin account for you on <strong>{tenant_name}</strong>.</p>
<ul>
  <li><strong>Email:</strong> {to_email}</li>
  <li><strong>Temporary password:</strong> <code>{temp_password}</code></li>
</ul>
<p><a href="{login_url}">Log in now</a> and change your password immediately.</p>
<p>— Platform Team</p>
"""
    await _send(to=to_email, subject=subject, body_text=text, body_html=html)

