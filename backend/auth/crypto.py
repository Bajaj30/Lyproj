"""
Encryption at rest for the few values the server must be able to read back.

Passwords, session tokens and one-time codes are HASHED: the server never needs
them again. A TOTP secret is different. The second-factor check needs the secret
itself, so it has to be stored reversibly, and stored in the clear it means
anyone who reads the database (a leaked backup, a SQL injection) can generate
every user's login codes. It is therefore encrypted with a key that is NOT in
the database.

  * Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from the `cryptography`
    package. The project's other auth code is standard library only; this is the
    one exception, because the standard library has no authenticated cipher and
    writing one here would be the worse risk.
  * The key comes from DATA_ENCRYPTION_KEY: one or more Fernet keys, comma
    separated. The FIRST encrypts; all of them decrypt, so a key is rotated by
    putting the new one in front. Generate one with
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  * If DATA_ENCRYPTION_KEY is unset, a key is derived from SECRET_KEY (HKDF), so
    existing deployments are encrypted without new configuration. The cost:
    changing SECRET_KEY then also makes every stored TOTP secret unreadable, and
    those users must re-enrol. Set DATA_ENCRYPTION_KEY to separate the two.
  * This protects against a database-only leak. It does not protect against an
    attacker who has the server's environment as well; a KMS-held key is the
    next step for that (open item 6).

Stored values are prefixed `enc:v1:`. Anything without the prefix is a value
written before encryption existed: it is still readable, and startup re-saves it
encrypted (`encrypt_legacy_secrets`).
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import String, text
from sqlalchemy.types import TypeDecorator

from config.settings import settings

PREFIX = "enc:v1:"


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    raw = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
    else:
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b"prescriptai:data-at-rest:v1").derive(settings.SECRET_KEY.encode())
        keys = [base64.urlsafe_b64encode(derived).decode()]
    return MultiFernet([Fernet(k) for k in keys])


def using_dedicated_key() -> bool:
    return bool(os.environ.get("DATA_ENCRYPTION_KEY", "").strip())


def seal(value: Optional[str]) -> Optional[str]:
    if value is None or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def unseal(value: Optional[str]) -> Optional[str]:
    if value is None or not value.startswith(PREFIX):
        return value            # written before encryption existed
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "A stored secret could not be decrypted: the encryption key has changed. "
            "Put the previous key back in DATA_ENCRYPTION_KEY (after the new one) to read it."
        ) from exc


class EncryptedString(TypeDecorator):
    """A String column that is encrypted on the way in and decrypted on the way
    out. Code using the model sees plain text; the database never does."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value)

    def process_result_value(self, value, dialect):
        return unseal(value)


def encrypt_legacy_secrets(engine) -> int:
    """Encrypt any TOTP secret still stored in the clear. Idempotent. Uses raw
    SQL so it sees what is actually on disk, not the decrypted view."""
    done = 0
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, mfa_secret, mfa_pending_secret FROM users "
            "WHERE mfa_secret IS NOT NULL OR mfa_pending_secret IS NOT NULL")).fetchall()
        for uid, secret, pending in rows:
            new_secret, new_pending = seal(secret), seal(pending)
            if new_secret != secret or new_pending != pending:
                conn.execute(text("UPDATE users SET mfa_secret = :s, mfa_pending_secret = :p WHERE id = :id"),
                             {"s": new_secret, "p": new_pending, "id": uid})
                done += 1
    return done
