"""
Security building blocks, without a server:

    encryption at rest   backend/auth/crypto.py
    shared rate limiter  backend/auth/ratelimit.py
    real SMS delivery    backend/auth/notify.py  (against a local stand-in provider)

    python tests/test_security.py

The SMS checks talk to an HTTP server started inside this script. They prove the
request each provider would receive, and that a failure never leaks a code or a
credential. They do not prove Twilio accepts it: that needs an account.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="security-test-"))

# ── a stand-in SMS provider ──────────────────────────────────────────────────
received = []
fail_with = {"status": None}


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        received.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "type": self.headers.get("Content-Type"), "body": body})
        self.send_response(fail_with["status"] or 201)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), Provider)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# Environment BEFORE the backend modules are imported: they read it at import.
os.environ.update({
    "DATABASE_URL": f"sqlite:///{TMP / 'sec.db'}",
    "SECRET_KEY": "unit-test-secret-key-not-for-real-use",
    "RATE_LIMIT_BACKEND": "database",
    "SMS_DELIVERY": "twilio",
    "TWILIO_ACCOUNT_SID": "ACtest123", "TWILIO_AUTH_TOKEN": "tok-SECRET-456", "TWILIO_FROM": "+15005550006",
    "TWILIO_API_BASE": f"http://127.0.0.1:{PORT}",
    "SMS_WEBHOOK_URL": f"http://127.0.0.1:{PORT}/hook", "SMS_WEBHOOK_TOKEN": "hook-SECRET-789",
})
os.environ.pop("DATA_ENCRYPTION_KEY", None)
sys.path.insert(0, str(ROOT / "ml_pipeline"))
sys.path.insert(0, str(ROOT / "backend"))

from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import text  # noqa: E402

from auth import crypto, notify, ratelimit  # noqa: E402
from auth.models import Role, User  # noqa: E402
from config import migrate  # noqa: E402
from config.database import SessionLocal, engine  # noqa: E402

checks = []


def check(name, cond, extra=""):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))


migrate.upgrade()
check("a new database migrates to the latest revision", migrate.drift() == [])

# ── encryption at rest ───────────────────────────────────────────────────────
SECRET = "JBSWY3DPEHPK3PXP"
sealed = crypto.seal(SECRET)
check("sealed value is marked and is not the secret", sealed.startswith("enc:v1:") and SECRET not in sealed)
check("unseal returns the secret", crypto.unseal(sealed) == SECRET)
check("each seal differs (random IV), so equal secrets are not visible as equal", crypto.seal(SECRET) != sealed)
check("a value written before encryption is still readable", crypto.unseal(SECRET) == SECRET)
check("sealing twice does not double-encrypt", crypto.seal(sealed) == sealed)

with SessionLocal() as db:
    u = User(role=Role.PATIENT, full_name="Enc Test", username="enctest", password_hash="x", mfa_secret=SECRET)
    db.add(u)
    db.commit()
    uid = u.id
with engine.connect() as conn:
    on_disk = conn.execute(text("SELECT mfa_secret FROM users WHERE id = :i"), {"i": uid}).scalar()
check("the database column holds ciphertext", on_disk.startswith("enc:v1:") and SECRET not in on_disk, on_disk[:20])
with SessionLocal() as db:
    check("the model reads it back as plain text", db.get(User, uid).mfa_secret == SECRET)

# a plaintext row from before encryption is encrypted in place, once
with engine.begin() as conn:
    conn.execute(text("UPDATE users SET mfa_secret = :s WHERE id = :i"), {"s": SECRET, "i": uid})
check("startup encrypts a plaintext secret", crypto.encrypt_legacy_secrets(engine) == 1)
check("...and is idempotent", crypto.encrypt_legacy_secrets(engine) == 0)
with SessionLocal() as db:
    check("...and it still reads back", db.get(User, uid).mfa_secret == SECRET)

# key rotation: new key first, old key kept for reading
old_sealed = crypto.seal("ROTATE-ME")
k_new, k_old = Fernet.generate_key().decode(), None
os.environ["DATA_ENCRYPTION_KEY"] = k_new
crypto._fernet.cache_clear()
try:
    crypto.unseal(old_sealed)
    check("a different key cannot read the old value", False)
except RuntimeError as exc:
    check("a different key cannot read the old value (clear error)", "encryption key has changed" in str(exc))
with_new = crypto.seal("under new key")
k_newer = Fernet.generate_key().decode()
os.environ["DATA_ENCRYPTION_KEY"] = f"{k_newer},{k_new}"
crypto._fernet.cache_clear()
check("rotation: the previous key, listed second, still decrypts", crypto.unseal(with_new) == "under new key")
check("rotation: new values use the first key",
      Fernet(k_newer).decrypt(crypto.seal("x")[len("enc:v1:"):].encode()) == b"x")
os.environ.pop("DATA_ENCRYPTION_KEY")
crypto._fernet.cache_clear()

# ── rate limiter ─────────────────────────────────────────────────────────────
waits = [ratelimit.hit("t_login", "alice@example.com", 3, 60) for _ in range(5)]
check("3 allowed, then blocked with a wait", waits[:3] == [0, 0, 0] and waits[3] > 0 and waits[4] > 0, waits)
check("blocked() reports without recording", ratelimit.blocked("t_login", "alice@example.com", 3, 60) > 0
      and ratelimit.blocked("t_login", "bob@example.com", 3, 60) == 0)
check("keys are independent", ratelimit.hit("t_login", "bob@example.com", 3, 60) == 0)
with engine.connect() as conn:
    stored = [r[0] for r in conn.execute(text("SELECT key_hash FROM rate_limit_hits"))]
check("the table stores a keyed hash, never the email or IP",
      stored and all("alice" not in k and "@" not in k and len(k) == 40 for k in stored))

# another PROCESS sees the same counts (what the in-memory limiter could not do)
probe = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
         "from auth import ratelimit; print(ratelimit.blocked('t_login', 'alice@example.com', 3, 60) > 0)"
         % (str(ROOT / "ml_pipeline"), str(ROOT / "backend")))
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=os.environ.copy())
check("a second process is blocked by the first one's failures", out.stdout.strip() == "True", out.stdout + out.stderr[-300:])

ratelimit.hit("t_short", "k", 1, 1)
check("over the limit inside the window", ratelimit.hit("t_short", "k", 1, 1) > 0)
time.sleep(1.2)
check("the window slides: allowed again after it passes", ratelimit.hit("t_short", "k", 1, 1) == 0)
ratelimit.reset("t_login", "alice@example.com")
check("reset clears a key", ratelimit.blocked("t_login", "alice@example.com", 3, 60) == 0)
with engine.begin() as conn:
    conn.execute(text("INSERT INTO rate_limit_hits (bucket, key_hash, at) VALUES ('old', 'h', 1.0)"))
check("purge drops rows no window can need", ratelimit.purge() >= 1)

# ── SMS: Twilio ──────────────────────────────────────────────────────────────
received.clear()
sent_to = notify.send_sms("+919876543210", "verify_phone", "482913", 10)
req = received[-1]
form = dict(urllib.parse.parse_qsl(req["body"]))
check("twilio: POST to the account's Messages endpoint", req["path"] == "/2010-04-01/Accounts/ACtest123/Messages.json")
check("twilio: basic auth with SID and token",
      req["auth"] == "Basic " + base64.b64encode(b"ACtest123:tok-SECRET-456").decode())
check("twilio: To / From / Body with the code", form.get("To") == "+919876543210" and form.get("From") == "+15005550006"
      and "482913" in form.get("Body", "") and "10 minutes" in form["Body"])
check("twilio: reports the phone itself as the destination", sent_to == "+919876543210")
check("phone codes now travel by sms", notify.sms_channel() == "sms")

fail_with["status"] = 401
try:
    notify.send_sms("+919876543210", "verify_phone", "111222", 10)
    check("twilio: a provider error raises", False)
except RuntimeError as exc:
    msg = str(exc)
    check("twilio: a provider error raises, naming only the provider and status",
          "401" in msg and "111222" not in msg and "SECRET" not in msg and "ACtest" not in msg, msg)
fail_with["status"] = None

notify.TWILIO_API_BASE = "http://127.0.0.1:9"       # nothing listens here
try:
    notify.send_sms("+919876543210", "verify_phone", "333444", 10)
    check("twilio: unreachable raises", False)
except RuntimeError as exc:
    check("twilio: unreachable raises without the code or token", "333444" not in str(exc) and "SECRET" not in str(exc))
notify.TWILIO_API_BASE = f"http://127.0.0.1:{PORT}"

notify.TWILIO_AUTH_TOKEN = ""
try:
    notify.send_sms("+919876543210", "verify_phone", "555666", 10)
    check("twilio: missing credentials raise", False)
except RuntimeError as exc:
    check("twilio: missing credentials raise instead of silently not sending", "not configured" in str(exc))
notify.TWILIO_AUTH_TOKEN = "tok-SECRET-456"

# ── SMS: generic webhook ─────────────────────────────────────────────────────
notify.SMS_DELIVERY = "webhook"
received.clear()
notify.send_sms("+919876543210", "reset_password", "909090", 10)
req = received[-1]
payload = json.loads(req["body"])
check("webhook: JSON {to, message, purpose} with a bearer token",
      req["path"] == "/hook" and req["auth"] == "Bearer hook-SECRET-789" and payload["to"] == "+919876543210"
      and "909090" in payload["message"] and payload["purpose"] == "reset_password")
notify.SMS_WEBHOOK_URL = "http://sms.example.com/send"
try:
    notify.send_sms("+919876543210", "verify_phone", "121212", 10)
    check("webhook: plain http to a remote host is refused", False)
except RuntimeError as exc:
    check("webhook: plain http to a remote host is refused (codes must not travel in clear)", "https" in str(exc))

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
