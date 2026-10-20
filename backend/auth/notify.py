"""
Delivery of one-time codes.

  email — sent over SMTP when SMTP_HOST is set; otherwise printed to the
          backend console.
  phone — there is no SMS provider yet (`send_sms` is the single function to
          replace: MSG91, Twilio, ...). For the prototype, SMS_DELIVERY=email
          (the default) sends the code BY EMAIL instead: to the account's email
          if it has one, otherwise to `<digits>@SMS_EMAIL_DOMAIN`, an address
          only a local catch-all inbox such as Mailpit receives. Without
          SMTP_HOST it is printed to the console. SMS_DELIVERY=console forces
          the console.

A verified phone therefore means "verified by email" in this prototype. Swap in
a real SMS provider before that distinction matters.

Console delivery is a development convenience, not a fallback to rely on: it
puts live codes in the server log. Configure a real channel before anyone but
a developer uses the system.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Optional

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@prescriptai.local")
# Off for a local catch-all server (Mailpit) that has no TLS.
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

SMS_DELIVERY = os.getenv("SMS_DELIVERY", "email").lower()       # email | console
SMS_EMAIL_DOMAIN = os.getenv("SMS_EMAIL_DOMAIN", "sms.prescriptai.local")

_SUBJECTS = {
    "verify_email": "Verify your email",
    "verify_phone": "Verify your phone",
    "reset_password": "Reset your password",
}


def _console(channel: str, destination: str, purpose: str, code: str) -> None:
    print(
        f"[AUTH] {channel} code for {destination} ({purpose}): {code}"
        "   <- dev delivery; configure SMTP / an SMS provider for real use",
        flush=True,
    )


def _smtp_send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        if SMTP_STARTTLS:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def send_email(to: str, purpose: str, code: str, minutes: int) -> str:
    if not SMTP_HOST:
        _console("email", to, purpose, code)
        return to
    _smtp_send(
        to, f"PrescriptAI — {_SUBJECTS.get(purpose, 'Your code')}",
        f"Your PrescriptAI code is {code}\n\n"
        f"It expires in {minutes} minutes and can be used once.\n"
        "If you did not ask for this, ignore this email — nothing changes "
        "until the code is entered.",
    )
    return to


def send_sms(to: str, purpose: str, code: str, minutes: int, email: Optional[str] = None) -> str:
    """Returns the address the code actually went to (for "code sent to …")."""
    if SMS_DELIVERY != "email" or not SMTP_HOST:
        _console("sms", to, purpose, code)
        return to
    inbox = email or f"{to.lstrip('+')}@{SMS_EMAIL_DOMAIN}"
    _smtp_send(
        inbox, f"PrescriptAI — {_SUBJECTS.get(purpose, 'Your code')} ({to})",
        f"PrescriptAI code for phone {to}: {code}\n\n"
        f"It expires in {minutes} minutes and can be used once.\n\n"
        "This is a prototype: text messages are delivered by email until an SMS "
        "provider is connected. If you did not ask for this, ignore it.",
    )
    return inbox


def deliver(channel: str, destination: str, purpose: str, code: str, minutes: int,
            email: Optional[str] = None) -> str:
    """Send a code. Returns where it actually went, which for a phone code may
    be an email address."""
    if channel == "email":
        return send_email(destination, purpose, code, minutes)
    return send_sms(destination, purpose, code, minutes, email=email)
