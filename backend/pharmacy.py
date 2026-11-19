"""
Pharmacy app API — Workflow.md stages 5 (redeem), 6 (verify packs), 7 (expiry).

    POST /api/pharmacy/redeem                          the patient's 6-digit code
    GET  /api/pharmacy/grants                          this pharmacy's access, open and ended
    GET  /api/pharmacy/grants/{id}                     the prescription — 410 once access ends
    GET  /api/pharmacy/grants/{id}/pages/{n}/image
    POST /api/pharmacy/grants/{id}/checks              check one pack: qr | photo | manual
    POST /api/pharmacy/grants/{id}/checks/{cid}/confirm   chemist confirms an orange result
    GET  /api/pharmacy/records                         the minimal dispensing record (stage 7)

Rules (Workflow §4 stages 5–7, §8):

  * LEAST ACCESS. One prescription, read-only, for SHARE_WINDOW_MINUTES. Never
    the patient's phone, never their other prescriptions.
  * EXPIRY IS ENFORCED HERE, on every request (`_open_grant`). Hiding the screen
    in the browser is not access control.
  * A WRONG CODE REVEALS NOTHING. Unknown, lapsed, used, cancelled and revoked
    codes all get the same answer, and failures are throttled per pharmacy
    account and per IP.
  * "COULD NOT VERIFY" NEVER BECOMES "MATCH" (dispense/compare.py). A red or
    grey result can never be counted as dispensed; an orange one only after the
    chemist confirms it.
  * After access ends the pharmacy keeps only `DispensingRecord` rows: date,
    prescription ID, prescriber + registration no., brand, batch, quantity and
    the result. No phone, no image, no diagnosis.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import pipeline
from access import (
    ACTIVE, ISSUED, check_out, checks_for, dispensed_indexes, dispensing_state, grant_status,
    hash_otp, latest_version, med_key, now, snapshot,
)
from auth import Role, User, require_vetted
from auth import ratelimit as rl
from auth.models import aware
from auth.routes import _event, _ip
from config.database import get_db
from config.settings import settings
from models import AccessGrant, DispenseCheck, DispensingRecord
from views import UPLOAD_DIR, image_response

router = APIRouter(prefix="/api/pharmacy", tags=["pharmacy"])
# Signed in as a pharmacy AND approved by an administrator (admin.py).
chemist_only = require_vetted(Role.CHEMIST)

REDEEM_FAIL_LIMIT, REDEEM_FAIL_WINDOW = 5, 15 * 60
BAD_CODE = "That code is not valid, has expired, or has already been used. Ask the patient for a new one."
ACCESS_ENDED = "Access to this prescription has ended. Ask the patient for a new code if you need it again."


# ─── shapes ──────────────────────────────────────────────────────────────────


class RedeemIn(BaseModel):
    otp: str = Field(min_length=6, max_length=12)


class GrantSummary(BaseModel):
    id: str
    status: str
    redeemed_at: Optional[str] = None
    expires_at: Optional[str] = None
    ended_at: Optional[str] = None
    # Only while access is open; an ended grant shows nothing about the patient.
    patient_name: Optional[str] = None
    doctor_name: Optional[str] = None
    verification_id: Optional[str] = None


class PharmacyMedicine(BaseModel):
    index: int
    name: Optional[str] = None
    as_written: str = ""
    contains: List[str] = []
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    instructions: Optional[str] = None
    uncertain: bool = False
    outcome: Optional[str] = None
    verification: Optional[str] = None
    page_no: Optional[int] = None
    dispensed: bool = False


class PharmacyPrescription(BaseModel):
    grant_id: str
    expires_at: str
    seconds_left: int
    verification_id: str
    version: int
    revised: bool
    change_note: Optional[str] = None
    finalised_at: str
    patient_name: Optional[str] = None
    doctor: Optional[dict] = None
    diagnosis: Optional[str] = None
    allow_substitution: bool
    medicines: List[PharmacyMedicine]
    interactions: List[dict] = []
    overall_risk: str = "none"
    not_checked: List[str] = []
    page_numbers: List[int] = []
    checks: List[dict] = []
    dispensing_state: str = "none"
    reader_is_mock: bool = False


# ─── helpers ─────────────────────────────────────────────────────────────────


def _own_grant(db: Session, user: User, gid: str) -> AccessGrant:
    g = db.get(AccessGrant, gid)
    if not g or g.pharmacy_id != user.id:
        raise HTTPException(404, "Not found")
    return g


def _open_grant(db: Session, user: User, gid: str):
    """The access check for every read and write. Returns (grant, version)."""
    g = _own_grant(db, user, gid)
    if grant_status(g) != ACTIVE:
        raise HTTPException(410, ACCESS_ENDED)
    v = latest_version(g.consultation)
    if not v:
        raise HTTPException(410, ACCESS_ENDED)
    return g, v


def _summary(g: AccessGrant) -> GrantSummary:
    st = grant_status(g)
    out = GrantSummary(
        id=g.id, status=st,
        redeemed_at=aware(g.redeemed_at).isoformat() if g.redeemed_at else None,
        expires_at=aware(g.expires_at).isoformat() if g.expires_at else None,
        ended_at=aware(g.revoked_at or g.expires_at).isoformat() if st != ACTIVE and g.expires_at else None,
    )
    if st == ACTIVE:
        v = latest_version(g.consultation)
        snap = snapshot(v) if v else {}
        out.patient_name = (snap.get("patient") or {}).get("name")
        out.doctor_name = (snap.get("doctor") or {}).get("name")
        out.verification_id = v.verification_id if v else None
    return out


def _view(db: Session, g: AccessGrant, v) -> PharmacyPrescription:
    snap = snapshot(v)
    checks = checks_for(db, g.consultation_id)
    done = dispensed_indexes(checks, snap["medicines"])
    meds = []
    for m in snap["medicines"]:
        readable = m.get("outcome") in ("confirmed", "probable") and (m.get("brand") or m.get("generic"))
        meds.append(PharmacyMedicine(
            index=m["index"], name=(m.get("brand") or m.get("generic")) if readable else None,
            as_written=m.get("raw_reading") or "", contains=m.get("ingredients") or [],
            dosage=m.get("dosage"), frequency=m.get("frequency"), duration=m.get("duration"),
            instructions=m.get("instructions"), uncertain=not readable,
            outcome=m.get("outcome"), verification=m.get("verification"),
            page_no=m.get("page_no"), dispensed=m["index"] in done,
        ))
    result = snap.get("interactions") or {}
    left = int((aware(g.expires_at) - now()).total_seconds())
    return PharmacyPrescription(
        grant_id=g.id, expires_at=aware(g.expires_at).isoformat(), seconds_left=max(left, 0),
        verification_id=v.verification_id, version=v.version, revised=v.version > 1,
        change_note=v.change_note, finalised_at=aware(v.finalised_at).isoformat(),
        patient_name=(snap.get("patient") or {}).get("name"),
        doctor=snap.get("doctor"),
        diagnosis=snap.get("diagnosis") if snap.get("diagnosis_visibility") == "patient_and_pharmacy" else None,
        allow_substitution=snap.get("allow_substitution", True), medicines=meds,
        # A pharmacist is a professional reader: the full citation, mechanism
        # and safer alternative are shown, as on the doctor's screen.
        interactions=result.get("interactions", []),
        overall_risk=result.get("overall_risk", "none"), not_checked=result.get("skipped", []),
        page_numbers=[p["page_no"] for p in snap.get("pages", [])],
        checks=[check_out(c) for c in checks],
        dispensing_state=dispensing_state(snap["medicines"], checks),
        reader_is_mock=pipeline.pack_is_mock(),   # this screen reads PACKS, not pages
    )


def _record(db: Session, user: User, v, check: DispenseCheck, r: dict, pack: dict) -> None:
    snap = snapshot(v)
    doc = snap.get("doctor") or {}
    db.add(DispensingRecord(
        pharmacy_id=user.id, check_id=check.id, verification_id=v.verification_id,
        prescriber_name=doc.get("name"), prescriber_registration_no=doc.get("registration_no"),
        brand=r.get("pack_brand"), composition=r.get("pack_composition"),
        strength=r.get("pack_strength"), batch=pack.get("batch"), expiry=pack.get("expiry"),
        quantity=check.quantity, result=check.result,
    ))


# ─── routes ──────────────────────────────────────────────────────────────────


@router.post("/redeem", response_model=GrantSummary)
def redeem(body: RedeemIn, request: Request, db: Session = Depends(get_db),
           user: User = Depends(chemist_only)):
    for bucket, key in (("redeem_fail_user", user.id), ("redeem_fail_ip", _ip(request))):
        limit = REDEEM_FAIL_LIMIT if bucket.endswith("user") else REDEEM_FAIL_LIMIT * 4
        wait = rl.blocked(bucket, key, limit, REDEEM_FAIL_WINDOW)
        if wait:
            raise HTTPException(429, f"Too many wrong codes. Try again in {wait // 60 + 1} min.",
                                headers={"Retry-After": str(wait)})
    otp = "".join(ch for ch in body.otp if ch.isdigit())
    t = now()
    grant = None
    if len(otp) == 6:
        grant = next((g for g in db.query(AccessGrant).filter(AccessGrant.otp_hash == hash_otp(otp)).all()
                      if grant_status(g, t) == ISSUED), None)
    if not grant:
        rl.hit("redeem_fail_user", user.id, REDEEM_FAIL_LIMIT, REDEEM_FAIL_WINDOW)
        rl.hit("redeem_fail_ip", _ip(request), REDEEM_FAIL_LIMIT * 4, REDEEM_FAIL_WINDOW)
        _event(db, request, "rx_share_redeem_failed", user.id)
        db.commit()
        raise HTTPException(400, BAD_CODE)
    # Single use: from here the code can never be redeemed again, by anyone.
    grant.pharmacy_id, grant.redeemed_at = user.id, t
    grant.expires_at = t + timedelta(minutes=settings.SHARE_WINDOW_MINUTES)
    _event(db, request, "rx_share_redeemed", user.id, f"{grant.consultation_id} grant {grant.id}")
    db.commit()
    return _summary(grant)


@router.get("/grants", response_model=List[GrantSummary])
def my_grants(db: Session = Depends(get_db), user: User = Depends(chemist_only)):
    rows = (db.query(AccessGrant).filter(AccessGrant.pharmacy_id == user.id)
            .order_by(AccessGrant.redeemed_at.desc()).limit(50).all())
    return [_summary(g) for g in rows]


@router.get("/grants/{gid}", response_model=PharmacyPrescription)
def open_grant(gid: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(chemist_only)):
    g, v = _open_grant(db, user, gid)
    _event(db, request, "rx_viewed_by_pharmacy", user.id, f"{g.consultation_id} v{v.version}")
    db.commit()
    return _view(db, g, v)


@router.get("/grants/{gid}/pages/{n}/image")
def grant_page_image(gid: str, n: int, db: Session = Depends(get_db),
                     user: User = Depends(chemist_only)):
    _, v = _open_grant(db, user, gid)
    page = next((p for p in snapshot(v).get("pages", []) if p["page_no"] == n), None)
    path = UPLOAD_DIR / page["image"] if page else None
    if not path or not path.is_file():
        raise HTTPException(404, "Not found")
    # no-store: the pharmacy must not keep the image once access ends.
    return image_response(path)


@router.post("/grants/{gid}/checks", response_model=PharmacyPrescription)
async def check_pack(
    gid: str, request: Request,
    method: str = Form(...),
    qr_text: Optional[str] = Form(None),
    brand: Optional[str] = Form(None),
    generic_name: Optional[str] = Form(None),
    strength: Optional[str] = Form(None),
    form: Optional[str] = Form(None),
    batch: Optional[str] = Form(None),
    expiry: Optional[str] = Form(None),
    quantity: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db), user: User = Depends(chemist_only),
):
    from dispense.compare import PackIdentity, compare, counts_as_dispensed, identify, parse_qr
    from normalise.india_medicines import get_medicines

    g, v = _open_grant(db, user, gid)
    clip = lambda s: (s or "").strip()[:200] or None  # noqa: E731

    if method == "qr":
        if not clip(qr_text):
            raise HTTPException(422, "Scan the QR code first.")
        pack = parse_qr(qr_text[:2000])
    elif method == "photo":
        if not image:
            raise HTTPException(422, "Take a photo of the pack first.")
        data = await image.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "That photo is over 10 MB.")
        try:
            # Off the event loop: a real read takes seconds.
            label = await run_in_threadpool(pipeline.pack_reader.read, data)
        except Exception as exc:
            raise pipeline.reader_http_error(exc) from exc
        # The photo itself is not kept: only what was read from it.
        pack = PackIdentity(source="photo", brand=label.brand, generic_name=label.generic_name,
                            strength=label.strength, form=label.form, manufacturer=label.manufacturer,
                            batch=label.batch, expiry=label.expiry, legible=label.legible,
                            reader=pipeline.pack_reader.model, note=label.notes)
    elif method == "manual":
        if not clip(brand) and not clip(generic_name):
            raise HTTPException(422, "Enter the brand or the composition printed on the pack.")
        pack = PackIdentity(source="manual", brand=clip(brand), generic_name=clip(generic_name),
                            strength=clip(strength), form=clip(form))
    else:
        raise HTTPException(422, "method must be qr, photo or manual.")
    # Anything the chemist typed alongside a scan fills gaps; it never overrides.
    pack.batch = pack.batch or clip(batch)
    pack.expiry = pack.expiry or clip(expiry)
    pack.strength = pack.strength or clip(strength)

    med = get_medicines()
    snap = snapshot(v)
    prior = checks_for(db, g.consultation_id)
    r = compare(identify(pack, med), snap["medicines"], med,
                dispensed=sorted(dispensed_indexes(prior, snap["medicines"])),
                allow_substitution=snap.get("allow_substitution", True))
    rd = r.to_dict()
    target = next((m for m in snap["medicines"] if m["index"] == r.medicine_index), None)
    rd["medicine_key"] = med_key(target) if target else None
    check = DispenseCheck(
        grant_id=g.id, consultation_id=g.consultation_id, version=v.version, pharmacy_id=user.id,
        method=method, pack_json=json.dumps(pack.to_dict()), result=r.result,
        result_json=json.dumps(rd), medicine_index=r.medicine_index, blocked=r.blocked,
        needs_confirmation=r.needs_confirmation, quantity=clip(quantity),
        dispensed=counts_as_dispensed(r.result, r.blocked, r.needs_confirmation, False),
    )
    db.add(check)
    db.flush()
    if check.dispensed:
        _record(db, user, v, check, rd, pack.to_dict())
    _event(db, request, "pack_checked", user.id, f"{g.consultation_id} {method} -> {r.result}")
    db.commit()
    return _view(db, g, v)


@router.post("/grants/{gid}/checks/{chk}/confirm", response_model=PharmacyPrescription)
def confirm_check(gid: str, chk: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(chemist_only)):
    g, v = _open_grant(db, user, gid)
    check = db.get(DispenseCheck, chk)
    if not check or check.grant_id != g.id:
        raise HTTPException(404, "Not found")
    if check.blocked:
        raise HTTPException(409, "This pack cannot be dispensed against this prescription.")
    if not check.needs_confirmation or check.chemist_confirmed_at:
        return _view(db, g, v)
    check.chemist_confirmed_at, check.dispensed = now(), True
    _record(db, user, v, check, json.loads(check.result_json), json.loads(check.pack_json))
    _event(db, request, "pack_confirmed", user.id, f"{g.consultation_id} check {check.id}")
    db.commit()
    return _view(db, g, v)


@router.get("/records")
def dispensing_records(db: Session = Depends(get_db), user: User = Depends(chemist_only)):
    rows = (db.query(DispensingRecord).filter(DispensingRecord.pharmacy_id == user.id)
            .order_by(DispensingRecord.created_at.desc()).limit(500).all())
    return [{
        "id": r.id, "date": aware(r.created_at).isoformat(), "prescription_id": r.verification_id,
        "prescriber_name": r.prescriber_name,
        "prescriber_registration_no": r.prescriber_registration_no,
        "brand": r.brand, "composition": r.composition, "strength": r.strength,
        "batch": r.batch, "expiry": r.expiry, "quantity": r.quantity, "result": r.result,
    } for r in rows]
