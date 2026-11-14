"""
Shared by the patient (patient.py) and pharmacy (pharmacy.py) APIs: who may see
which finalised version, OTP grants, and what has been dispensed.

Both read ONLY `ConsultationVersion` snapshots — never the doctor's working
copy — so nothing a doctor is still editing can reach them (Workflow §4 stage 3:
"a draft is never shared").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from auth.models import aware
from config.settings import settings
from models import AccessGrant, Consultation, ConsultationVersion, DispenseCheck


def now() -> datetime:
    return datetime.now(timezone.utc)


def latest_version(c: Consultation) -> Optional[ConsultationVersion]:
    return c.versions[-1] if c.versions else None


def snapshot(v: ConsultationVersion) -> dict:
    return json.loads(v.snapshot_json)


# ─── grants ──────────────────────────────────────────────────────────────────

ISSUED, ACTIVE, EXPIRED, REVOKED, LAPSED, CANCELLED = (
    "issued", "active", "expired", "revoked", "lapsed", "cancelled")


def grant_status(g: AccessGrant, at: Optional[datetime] = None) -> str:
    """Computed from timestamps on every request: expiry needs no background job
    and cannot be skipped (Workflow §8.3)."""
    at = at or now()
    if g.revoked_at:
        return REVOKED
    if g.redeemed_at:
        return ACTIVE if aware(g.expires_at) > at else EXPIRED
    if g.cancelled_at:
        return CANCELLED
    return ISSUED if aware(g.redeem_by) > at else LAPSED


def hash_otp(otp: str) -> str:
    """Not salted per row: the pharmacy types only the 6 digits, so the row has
    to be findable from the code alone. Keyed by SECRET_KEY, so the table is
    useless without the server."""
    return hmac.new(settings.SECRET_KEY.encode(), f"share-otp:{otp}".encode(),
                    hashlib.sha256).hexdigest()


def new_otp(db: Session) -> str:
    """6 random digits, unique among codes that could still be redeemed."""
    for _ in range(20):
        otp = f"{secrets.randbelow(10**6):06d}"
        live = (db.query(AccessGrant)
                .filter(AccessGrant.otp_hash == hash_otp(otp), AccessGrant.redeemed_at.is_(None),
                        AccessGrant.cancelled_at.is_(None), AccessGrant.revoked_at.is_(None))
                .all())
        if not any(grant_status(g) == ISSUED for g in live):
            return otp
    raise RuntimeError("Could not allocate a sharing code.")


# ─── dispensing ──────────────────────────────────────────────────────────────


def med_key(m: dict) -> str:
    """What a medicine IS, independent of its position in a version's list:
    ingredients + brand. Duration, frequency and dosage are deliberately not in
    it — a revision that changes only "3 days" to "5 days" does not un-dispense
    a pack the patient already holds."""
    ings = "+".join(sorted(i.strip().lower() for i in (m.get("ingredients") or [])))
    name = (m.get("brand") or m.get("generic") or m.get("raw_reading") or "").strip().lower()
    return f"{ings}#{name}"


def checks_for(db: Session, consultation_id: str) -> List[DispenseCheck]:
    """Every check on this prescription, across versions: what was handed over
    against version 1 is still handed over once the doctor issues version 2."""
    return (db.query(DispenseCheck)
            .filter(DispenseCheck.consultation_id == consultation_id)
            .order_by(DispenseCheck.created_at).all())


def dispensed_indexes(checks: List[DispenseCheck], meds: List[dict]) -> Set[int]:
    """Indexes in `meds` (one version) that some dispensed check covers."""
    keys = {json.loads(c.result_json).get("medicine_key") for c in checks if c.dispensed}
    return {m["index"] for m in meds if med_key(m) in keys}


def dispensing_state(meds: List[dict], checks: List[DispenseCheck]) -> str:
    """none | partial | complete, over the medicines that could be identified."""
    done = dispensed_indexes(checks, meds)
    if not done:
        return "none"
    return "complete" if all(m["index"] in done for m in meds) else "partial"


def check_out(c: DispenseCheck, pharmacy_name: Optional[str] = None) -> Dict:
    r = json.loads(c.result_json)
    pack = json.loads(c.pack_json)
    return {
        "id": c.id, "created_at": aware(c.created_at).isoformat(), "method": c.method,
        "version": c.version,
        "result": c.result, "medicine_index": c.medicine_index,
        "medicine_label": r.get("medicine_label"), "reasons": r.get("reasons", []),
        "blocked": c.blocked, "needs_confirmation": c.needs_confirmation,
        "chemist_confirmed": c.chemist_confirmed_at is not None,
        "dispensed": c.dispensed, "quantity": c.quantity,
        "pack_brand": r.get("pack_brand"), "pack_composition": r.get("pack_composition"),
        "pack_strength": r.get("pack_strength"), "prescribed_strength": r.get("prescribed_strength"),
        "batch": pack.get("batch"), "expiry": pack.get("expiry"), "expired": r.get("expired", False),
        "reader": pack.get("reader"), "pharmacy_name": pharmacy_name,
    }


def pharmacy_name(user) -> str:
    if not user:
        return "A pharmacy"
    return json.loads(user.profile_json or "{}").get("pharmacy_name") or user.full_name
