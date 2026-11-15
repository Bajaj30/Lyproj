"""
Sliding-window rate limiter, shared across workers.

Keyed by whatever the caller chooses — IP, user id, or a normalised identifier.
Keying failed logins on the IDENTIFIER (not the account row) is deliberate: an
unknown username is throttled exactly like a real one, so the 429 reveals
nothing about which accounts exist.

Two backends, chosen by RATE_LIMIT_BACKEND:

  database (default)  hits live in the `rate_limit_hits` table of the app's own
                      database. Every worker process sees the same counts, and
                      a restart does not hand an attacker a fresh set of
                      guesses. This is what the earlier in-memory limiter could
                      not do (open item 8), and it needs no extra service.
  memory              per-process, lost on restart. For unit tests and
                      throwaway local runs only.

What is stored: a keyed hash of the bucket and key, and a timestamp. Never the
key itself — the keys are IP addresses, phone numbers and the email addresses
people TRIED to log in with, which is personal data the limiter has no reason
to keep readable.

If the database is unreachable the call falls back to the in-memory window for
that process and says so once in the log: degrading to a weaker limit is better
than either failing every login or having no limit at all.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from sqlalchemy import Column, Float, Index, Integer, String, Table, create_engine, delete, func, insert, select

from config.database import Base
from config.database import engine as app_engine
from config.settings import settings

BACKEND = os.getenv("RATE_LIMIT_BACKEND", "database").lower()

# The limiter writes on its OWN connection, outside the request's transaction:
# a failed login must be counted even though that request rolls back. On SQLite
# that means it can meet a write lock, and the default wait is 5 seconds. A
# limiter must never be the slow part of a request, so its connection waits at
# most a second and then falls back (see hit()). Call the limiter BEFORE a
# request writes anything: after a flush it would be waiting on its own caller.
if settings.DATABASE_URL.startswith("sqlite"):
    engine = create_engine(settings.DATABASE_URL,
                           connect_args={"check_same_thread": False, "timeout": 1.0})
else:
    engine = app_engine

hits_table = Table(
    "rate_limit_hits", Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bucket", String, nullable=False),
    Column("key_hash", String, nullable=False),
    Column("at", Float, nullable=False),          # unix seconds
    Index("ix_rate_limit_lookup", "bucket", "key_hash", "at"),
)

_lock = threading.Lock()
_hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_warned = False


def _key_hash(bucket: str, key: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{bucket}:{key}".encode(),
                    hashlib.sha256).hexdigest()[:40]


# ─── memory backend ──────────────────────────────────────────────────────────


def _prune(q: Deque[float], window: float, now: float) -> None:
    while q and now - q[0] > window:
        q.popleft()


def _mem_hit(bucket: str, key: str, limit: int, window: float) -> int:
    now = time.monotonic()
    with _lock:
        q = _hits[(bucket, key)]
        _prune(q, window, now)
        if len(q) >= limit:
            return int(window - (now - q[0])) + 1
        q.append(now)
        return 0


def _mem_blocked(bucket: str, key: str, limit: int, window: float) -> int:
    now = time.monotonic()
    with _lock:
        q = _hits[(bucket, key)]
        _prune(q, window, now)
        return int(window - (now - q[0])) + 1 if len(q) >= limit else 0


# ─── database backend ────────────────────────────────────────────────────────


def _window(conn, bucket: str, kh: str, window: float, now: float):
    t = hits_table
    # The DELETE comes first on purpose: on SQLite it takes the write lock, so
    # the count and the insert below cannot interleave with another worker's.
    conn.execute(delete(t).where(t.c.bucket == bucket, t.c.key_hash == kh, t.c.at < now - window))
    return conn.execute(select(func.count(), func.min(t.c.at))
                        .where(t.c.bucket == bucket, t.c.key_hash == kh)).one()


def _db_hit(bucket: str, key: str, limit: int, window: float) -> int:
    now, kh = time.time(), _key_hash(bucket, key)
    with engine.begin() as conn:
        count, oldest = _window(conn, bucket, kh, window, now)
        if count >= limit:
            return int(window - (now - oldest)) + 1
        conn.execute(insert(hits_table).values(bucket=bucket, key_hash=kh, at=now))
        return 0


def _db_blocked(bucket: str, key: str, limit: int, window: float) -> int:
    now, kh = time.time(), _key_hash(bucket, key)
    with engine.begin() as conn:
        count, oldest = _window(conn, bucket, kh, window, now)
        return int(window - (now - oldest)) + 1 if count >= limit else 0


def _fallback(exc: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"[RATELIMIT] database backend unavailable ({type(exc).__name__}: {exc}); "
              f"using per-process limits until it returns.", flush=True)


# ─── public API ──────────────────────────────────────────────────────────────


def hit(bucket: str, key: str, limit: int, window: float) -> int:
    """Record one event. Returns seconds to wait if over the limit, else 0."""
    if BACKEND == "database":
        try:
            return _db_hit(bucket, key, limit, window)
        except Exception as exc:
            _fallback(exc)
    return _mem_hit(bucket, key, limit, window)


def blocked(bucket: str, key: str, limit: int, window: float) -> int:
    """Check without recording. Seconds to wait, or 0."""
    if BACKEND == "database":
        try:
            return _db_blocked(bucket, key, limit, window)
        except Exception as exc:
            _fallback(exc)
    return _mem_blocked(bucket, key, limit, window)


def reset(bucket: str, key: str) -> None:
    with _lock:
        _hits.pop((bucket, key), None)
    if BACKEND == "database":
        try:
            with engine.begin() as conn:
                conn.execute(delete(hits_table).where(hits_table.c.bucket == bucket,
                                                      hits_table.c.key_hash == _key_hash(bucket, key)))
        except Exception as exc:
            _fallback(exc)


def purge(older_than_seconds: float = 2 * 24 * 60 * 60) -> int:
    """Drop rows no window can still need. Called at startup."""
    if BACKEND != "database":
        return 0
    try:
        with engine.begin() as conn:
            return conn.execute(delete(hits_table)
                                .where(hits_table.c.at < time.time() - older_than_seconds)).rowcount or 0
    except Exception as exc:
        _fallback(exc)
        return 0
