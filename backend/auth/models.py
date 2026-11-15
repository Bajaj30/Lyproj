"""
Identity tables.  (Workflow.md §2 actors, §8 privacy principles)

One `users` table for all three stakeholders — doctor, patient, chemist — with
the role as a column. They share every credential mechanism; what differs is
the profile they fill in and the routes they may call.

Nothing here stores a usable secret in the clear. Passwords, tokens and codes
are hashed. `mfa_secret` must be readable by the server (the TOTP check needs
it), so it is ENCRYPTED at rest with a key held outside the database
(auth/crypto.py).

Vetting (Workflow.md §10 Q5): a doctor or pharmacy account starts `pending` and
cannot touch a prescription until an administrator has reviewed its
registration / licence details and approved it (admin.py).

SQLite note: `DateTime(timezone=True)` round-trips as a NAIVE datetime on
SQLite. Everything is written in UTC, and `aware()` re-attaches the zone on
read — compare only through it.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from config.database import Base
from models import _now, _uuid

from .crypto import EncryptedString


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Role(str, enum.Enum):
    DOCTOR = "doctor"
    PATIENT = "patient"
    CHEMIST = "chemist"
    # Reviews doctor and pharmacy accounts. Never created through /register:
    # only by the operator, with `python manage.py create-admin`.
    ADMIN = "admin"


class Vetting(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Roles that issue or dispense prescriptions, and so must be vetted first.
VETTED_ROLES = (Role.DOCTOR, Role.CHEMIST)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_uuid)
    role = Column(Enum(Role), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    # Sign-in identifiers. At least one is required; each is unique and stored
    # normalised (email lowercased, phone E.164, username lowercased).
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    phone = Column(String, unique=True, index=True)
    email_verified_at = Column(DateTime(timezone=True))
    phone_verified_at = Column(DateTime(timezone=True))

    password_hash = Column(String, nullable=False)
    password_changed_at = Column(DateTime(timezone=True), default=_now)

    full_name = Column(String, nullable=False)
    # Role-specific fields (Workflow.md §4 Stage 1): speciality, clinic address
    # and registration no. for doctors; pharmacy name, address and drug licence
    # no. for chemists. JSON because the three shapes share nothing.
    profile_json = Column(Text, default="{}")

    mfa_enabled = Column(Boolean, default=False, nullable=False)
    mfa_secret = Column(EncryptedString)          # active TOTP secret, encrypted at rest
    mfa_pending_secret = Column(EncryptedString)  # set during enrolment, until confirmed
    mfa_last_step = Column(Integer)      # last accepted TOTP step — replay guard

    is_active = Column(Boolean, default=True, nullable=False)

    # Doctor / pharmacy vetting. A plain string so an account created before
    # vetting existed (NULL) reads as pending rather than failing to load.
    vetting_status = Column(String, index=True)
    vetted_at = Column(DateTime(timezone=True))
    vetted_by = Column(String)
    vetting_note = Column(Text)

    @property
    def needs_vetting(self) -> bool:
        return self.role in VETTED_ROLES

    @property
    def is_vetted(self) -> bool:
        return not self.needs_vetting or self.vetting_status == Vetting.APPROVED.value

    sessions = relationship("AuthSession", back_populates="user", cascade="all, delete-orphan")
    recovery_codes = relationship("RecoveryCode", back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base):
    """A signed-in browser. The cookie holds the token; we hold only its hash."""

    __tablename__ = "auth_sessions"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # absolute cap
    revoked_at = Column(DateTime(timezone=True))
    mfa_passed = Column(Boolean, default=False, nullable=False)
    user_agent = Column(String)
    ip = Column(String)

    user = relationship("User", back_populates="sessions")


class CodePurpose(str, enum.Enum):
    VERIFY_EMAIL = "verify_email"
    VERIFY_PHONE = "verify_phone"
    RESET_PASSWORD = "reset_password"


class OneTimeCode(Base):
    """A 6-digit code sent by email or SMS. Short-lived, attempt-limited, single-use."""

    __tablename__ = "one_time_codes"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    purpose = Column(Enum(CodePurpose), nullable=False)
    channel = Column(String, nullable=False)       # "email" | "phone"
    destination = Column(String, nullable=False)   # where it was sent
    code_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    consumed_at = Column(DateTime(timezone=True))


class MfaChallenge(Base):
    """Password accepted, second factor pending. Not a session — grants nothing."""

    __tablename__ = "mfa_challenges"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    consumed_at = Column(DateTime(timezone=True))


class RecoveryCode(Base):
    """Single-use backup code: stands in for the authenticator, or for a lost
    email/phone during password reset."""

    __tablename__ = "recovery_codes"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    code_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    used_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="recovery_codes")


class AuthEvent(Base):
    """Security audit log (Workflow.md §8, principle 4). Append-only."""

    __tablename__ = "auth_events"

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    event = Column(String, nullable=False)
    ip = Column(String)
    user_agent = Column(String)
    detail = Column(Text)
