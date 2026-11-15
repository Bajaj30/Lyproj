"""
Administrator API — vetting doctors and pharmacies (Workflow.md §10 Q5).

    GET  /api/admin/accounts?status=pending|approved|rejected|all
    POST /api/admin/accounts/{id}/vetting      {"decision": "approve"|"reject", "note"}
    GET  /api/admin/summary

Why this exists: anyone can type "Dr." into a registration form. Until someone
has looked at the registration number, an account that claims to be a doctor
must not be able to send a prescription to a patient, and one that claims to be
a pharmacy must not be able to open one. So both start `pending`, and every
clinical route refuses them (`require_vetted`) until an administrator approves.

What an approval means, precisely: a person reviewed the name and the
registration / licence number the account declared. Nothing here checks a
medical-council or drug-licence register automatically; India has no public API
for either. The public verification page says exactly that.

  * Administrators are created only by the operator (`python manage.py
    create-admin`). `/api/auth/register` refuses the role.
  * A decision can be reversed: rejecting an approved account cuts it off on its
    very next request, because the check runs on every request.
  * Every decision is written to `auth_events` with who made it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import Role, User, require_roles
from auth.models import VETTED_ROLES, Vetting, aware
from auth.routes import _event
from config.database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_only = require_roles(Role.ADMIN)


class AccountOut(BaseModel):
    id: str
    role: str
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    username: Optional[str] = None
    email_verified: bool
    phone_verified: bool
    mfa_enabled: bool
    profile: Dict[str, str]
    created_at: str
    vetting_status: str
    vetted_at: Optional[str] = None
    vetting_note: Optional[str] = None


class VettingIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(None, max_length=500)


def _out(u: User) -> AccountOut:
    return AccountOut(
        id=u.id, role=u.role.value, full_name=u.full_name, email=u.email, phone=u.phone,
        username=u.username, email_verified=u.email_verified_at is not None,
        phone_verified=u.phone_verified_at is not None, mfa_enabled=bool(u.mfa_enabled),
        profile=json.loads(u.profile_json or "{}"),
        created_at=aware(u.created_at).isoformat() if u.created_at else "",
        vetting_status=u.vetting_status or Vetting.PENDING.value,
        vetted_at=aware(u.vetted_at).isoformat() if u.vetted_at else None,
        vetting_note=u.vetting_note,
    )


def _status(u: User) -> str:
    return u.vetting_status or Vetting.PENDING.value


@router.get("/accounts", response_model=List[AccountOut])
def list_accounts(
    status: Literal["pending", "approved", "rejected", "all"] = Query("pending"),
    db: Session = Depends(get_db), admin: User = Depends(admin_only),
):
    rows = (db.query(User).filter(User.role.in_(VETTED_ROLES))
            .order_by(User.created_at.desc()).limit(500).all())
    # Filtered in Python: an account from before vetting existed has NULL here
    # and must read as pending, which a SQL equality test would miss.
    return [_out(u) for u in rows if status == "all" or _status(u) == status]


@router.get("/summary")
def summary(db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    rows = db.query(User).filter(User.role.in_(VETTED_ROLES)).all()
    counts = {s.value: 0 for s in Vetting}
    for u in rows:
        counts[_status(u)] += 1
    return counts


@router.post("/accounts/{user_id}/vetting", response_model=AccountOut)
def decide(user_id: str, body: VettingIn, request: Request, db: Session = Depends(get_db),
           admin: User = Depends(admin_only)):
    u = db.get(User, user_id)
    if not u or u.role not in VETTED_ROLES:
        raise HTTPException(404, "Account not found")
    if body.decision == "reject" and not (body.note or "").strip():
        raise HTTPException(422, "Say why the account is being rejected; the applicant sees this.")
    u.vetting_status = (Vetting.APPROVED if body.decision == "approve" else Vetting.REJECTED).value
    u.vetted_at, u.vetted_by = datetime.now(timezone.utc), admin.id
    u.vetting_note = (body.note or "").strip() or None
    _event(db, request, "account_vetted", admin.id, f"{u.id} ({u.role.value}) -> {u.vetting_status}")
    db.commit()
    db.refresh(u)
    return _out(u)
