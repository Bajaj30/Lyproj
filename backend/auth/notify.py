"""
Delivery of one-time codes.

  email — sent over SMTP when SMTP_HOST is set; otherwise printed to the
          backend console.
  phone — chosen by SMS_DELIVERY:

      twilio    a real text message through Twilio's REST API. Needs
                TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and either TWILIO_FROM
                (a number) or TWILIO_MESSAGING_SERVICE_SID.
      webhook   POSTs {"to", "message", "purpose"} as JSON to SMS_WEBHOOK_URL
                with `Authorization: Bearer SMS_WEBHOOK_TOKEN`. This is the slot
                for any other gateway (MSG91, Fast2SMS, an in-house sender): a
                few lines on their side, no change here. HTTPS only, except to
                localhost.
      email     PROTOTYPE default. The code is sent BY EMAIL instead: to the
                account's email if it has one, otherwise to
                `<digits>@SMS_EMAIL_DOMAIN`, an address only a local catch-all
                inbox such as Mailpit receives. A "verified phone" then means
                "verified by email", which is not proof the person holds the
                phone. Acceptable for a demo, not for patients.
      console   printed to the server log. Development only.

With twilio or webhook, a verified phone finally means what it says. Sending
transactional SMS to Indian numbers also needs the sender and the message
template registered under DLT (TRAI); that is done with the provider, not here.

Nothing in this module logs a code, a token or a provider credential, except the
console channel, whose whole purpose is to show the code to a developer.

Console delivery is a development convenience, not a fallback to rely on: it
puts live codes in the server log. Configure a real channel before anyone but
a developer uses the system.
"""

from __future__ import annotations

import base64
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import Optional

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@prescriptai.local")
# Off for a local catch-all server (Mailpit) that has no TLS.
SMTP_STARTTLS = os.getenv("SMTP_STARTTLS", "true").lower() == "true"

SMS_DELIVERY = os.getenv("SMS_DELIVERY", "email").lower()       # twilio | webhook | email | console
SMS_EMAIL_DOMAIN = os.getenv("SMS_EMAIL_DOMAIN", "sms.prescriptai.local")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
TWILIO_API_BASE = os.getenv("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/")

SMS_WEBHOOK_URL = os.getenv("SMS_WEBHOOK_URL", "")
SMS_WEBHOOK_TOKEN = os.getenv("SMS_WEBHOOK_TOKEN", "")

SMS_TIMEOUT_SECONDS = 10

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


def _sms_text(purpose: str, code: str, minutes: int) -> str:
    what = {"verify_phone": "verification", "reset_password": "password reset"}.get(purpose, "security")
    return (f"PrescriptAI: your {what} code is {code}. It expires in {minutes} minutes. "
            "Never share it with anyone.")


def _post(req: urllib.request.Request, provider: str) -> None:
    """Send, and turn any failure into one short error that names the provider
    and the status, and nothing else: not the URL, the body or a credential."""
    try:
        with urllib.request.urlopen(req, timeout=SMS_TIMEOUT_SECONDS) as resp:
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"{provider} answered HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{provider} answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"{provider} could not be reached ({type(exc).__name__})") from None


def _twilio_send(to: str, body: str) -> None:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and (TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID)):
        raise RuntimeError("Twilio is not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                           "and TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID)")
    form = {"To": to, "Body": body}
    if TWILIO_MESSAGING_SERVICE_SID:
        form["MessagingServiceSid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        form["From"] = TWILIO_FROM
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    req = urllib.request.Request(
        f"{TWILIO_API_BASE}/2010-04-01/Accounts/{urllib.parse.quote(TWILIO_ACCOUNT_SID)}/Messages.json",
        data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    _post(req, "Twilio")


def _webhook_send(to: str, body: str, purpose: str) -> None:
    url = urllib.parse.urlparse(SMS_WEBHOOK_URL)
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    if url.scheme != "https" and not (url.scheme == "http" and local):
        # A login code crossing the network in clear text is a login anyone on
        # the path can use.
        raise RuntimeError("SMS_WEBHOOK_URL must be https:// (http:// is allowed for localhost only)")
    headers = {"Content-Type": "application/json"}
    if SMS_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {SMS_WEBHOOK_TOKEN}"
    req = urllib.request.Request(
        SMS_WEBHOOK_URL, method="POST", headers=headers,
        data=json.dumps({"to": to, "message": body, "purpose": purpose}).encode(),
    )
    _post(req, "The SMS webhook")


def sms_channel() -> str:
    """How a phone code will actually travel, for the startup log and
    /api/health: sms (a real text) | email | console."""
    if SMS_DELIVERY in ("twilio", "webhook"):
        return "sms"
    return "email" if SMS_DELIVERY == "email" and SMTP_HOST else "console"


def send_sms(to: str, purpose: str, code: str, minutes: int, email: Optional[str] = None) -> str:
    """Returns the address the code actually went to (for "code sent to …")."""
    if SMS_DELIVERY == "twilio":
        _twilio_send(to, _sms_text(purpose, code, minutes))
        return to
    if SMS_DELIVERY == "webhook":
        _webhook_send(to, _sms_text(purpose, code, minutes), purpose)
        return to
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
