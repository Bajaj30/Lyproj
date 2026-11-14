"""
Patient app API — Workflow.md stage 4 (read, download) and stage 5 (share).

    GET  /api/patient/prescriptions                     inbox
    GET  /api/patient/prescriptions/{id}                latest finalised version
    GET  /api/patient/prescriptions/{id}/pages/{n}/image
    GET  /api/patient/prescriptions/{id}/pdf
    POST /api/patient/prescriptions/{id}/share          -> a 6-digit code for ONE pharmacy
    POST /api/patient/grants/{grant_id}/revoke          end a pharmacy's access now

Access rule, in one place (`_mine`): a patient sees a prescription only when

  1. it has a FINALISED version — a draft, or an edit not yet finalised, never
     reaches a patient (they keep seeing the last finalised version);
  2. that version is addressed to a phone; and
  3. that number is the patient's own, VERIFIED, account phone.

Without 3, anyone could register with someone else's number and read their
prescriptions. Anything that fails the rule is a 404, never a 403, so the API
does not confirm that a prescription exists.

Wording follows Workflow §4 stage 4, because patients read this without a
clinician beside them: interactions say "discuss with your doctor or
pharmacist" and never name a "safer alternative" to switch to; "no interactions
found" always comes with what was NOT checked; and the page says the
transcription is AI-generated and doctor-reviewed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from access import (
    ACTIVE, ISSUED, check_out, checks_for, dispensed_indexes, dispensing_state, grant_status,
    hash_otp, latest_version, new_otp, now, pharmacy_name, snapshot,
)
from auth import Role, User, require_roles
from auth.models import aware
from auth.routes import _event, _throttle
from config.database import get_db
from config.settings import settings
from models import AccessGrant, Consultation, ConsultationVersion, Prescription
from views import UPLOAD_DIR, image_response

router = APIRouter(prefix="/api/patient", tags=["patient"])
patient_only = require_roles(Role.PATIENT)

PATIENT_NOTICE = (
    "This prescription was transcribed from the handwritten original by AI and "
    "reviewed by your doctor. If anything here differs from what your doctor told "
    "you, ask your doctor or pharmacist. Do not stop or change a medicine on the "
    "basis of this document alone."
)
INTERACTION_ADVICE = "Discuss this with your doctor or pharmacist. Do not stop taking a medicine on your own."
REVISED_NOTICE = "Your doctor revised this prescription. This is the latest version; earlier versions no longer apply."


# ─── the patient view (shared by the JSON API and the PDF) ──────────────────


class DoctorOut(BaseModel):
    name: str
    speciality: Optional[str] = None
    clinic_address: Optional[str] = None
    registration_no: Optional[str] = None


class PatientMedicine(BaseModel):
    index: int
    name: Optional[str] = None      # None: the line could not be read reliably
    as_written: str
    contains: List[str] = []
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    instructions: Optional[str] = None
    uncertain: bool = False         # still needs confirming with the doctor
    page_no: Optional[int] = None
    dispensed: bool = False


class PatientInteraction(BaseModel):
    drug_a: str
    drug_b: str
    severity: str
    effect: str = ""
    advice: str = INTERACTION_ADVICE


class ShareOut(BaseModel):
    id: str
    status: str
    pharmacy_name: Optional[str] = None
    issued_at: str
    redeem_by: str
    redeemed_at: Optional[str] = None
    expires_at: Optional[str] = None
    ended_at: Optional[str] = None


class PatientPrescription(BaseModel):
    id: str
    verification_id: str
    version: int
    revised: bool = False
    revised_notice: Optional[str] = None
    change_note: Optional[str] = None
    finalised_at: str
    patient_name: Optional[str] = None
    prescriber_as_written: Optional[str] = None
    date: Optional[str] = None
    doctor: Optional[DoctorOut] = None
    diagnosis: Optional[str] = None
    medicines: List[PatientMedicine] = []
    interactions: List[PatientInteraction] = []
    overall_risk: str = "none"
    not_checked: List[str] = []
    page_numbers: List[int] = []
    allow_substitution: bool = True
    dispensing_state: str = "none"
    dispensing: List[dict] = []
    shares: List[ShareOut] = []
    notice: str = PATIENT_NOTICE


class PatientSummary(BaseModel):
    id: str
    date: Optional[str] = None
    finalised_at: str
    version: int
    doctor_name: Optional[str] = None
    medicine_count: int
    overall_risk: str
    dispensing_state: str = "none"
    shared_now: bool = False


class PatientInbox(BaseModel):
    phone: Optional[str] = None
    phone_verified: bool
    prescriptions: List[PatientSummary] = []


class ShareCreated(BaseModel):
    grant_id: str
    otp: str
    redeem_by: str
    window_minutes: int


def patient_view(db: Session, c: Consultation, v: ConsultationVersion,
                 with_activity: bool = True) -> PatientPrescription:
    snap = snapshot(v)
    checks = checks_for(db, c.id)
    done = dispensed_indexes(checks, snap["medicines"])
    meds = []
    for m in snap["medicines"]:
        readable = m.get("outcome") in ("confirmed", "probable") and (m.get("brand") or m.get("generic"))
        meds.append(PatientMedicine(
            index=m["index"], name=(m.get("brand") or m.get("generic")) if readable else None,
            as_written=m.get("raw_reading") or "", contains=m.get("ingredients") or [],
            dosage=m.get("dosage"), frequency=m.get("frequency"), duration=m.get("duration"),
            instructions=m.get("instructions"), uncertain=not readable,
            page_no=m.get("page_no"), dispensed=m["index"] in done,
        ))
    result = snap.get("interactions") or {}
    pages = snap.get("pages") or []
    first = pages[0] if pages else {}
    shares = []
    if with_activity:
        grants = (db.query(AccessGrant).filter(AccessGrant.consultation_id == c.id)
                  .order_by(AccessGrant.issued_at.desc()).limit(20).all())
        for g in grants:
            st = grant_status(g)
            ph = db.get(User, g.pharmacy_id) if g.pharmacy_id else None
            ended = g.revoked_at or g.cancelled_at or (g.expires_at if st == "expired" else None) \
                or (g.redeem_by if st == "lapsed" else None)
            shares.append(ShareOut(
                id=g.id, status=st, pharmacy_name=pharmacy_name(ph) if ph else None,
                issued_at=aware(g.issued_at).isoformat(), redeem_by=aware(g.redeem_by).isoformat(),
                redeemed_at=aware(g.redeemed_at).isoformat() if g.redeemed_at else None,
                expires_at=aware(g.expires_at).isoformat() if g.expires_at else None,
                ended_at=aware(ended).isoformat() if ended else None,
            ))
    doc = snap.get("doctor")
    return PatientPrescription(
        id=c.id, verification_id=v.verification_id, version=v.version,
        revised=v.version > 1, revised_notice=REVISED_NOTICE if v.version > 1 else None,
        change_note=v.change_note, finalised_at=aware(v.finalised_at).isoformat(),
        patient_name=(snap.get("patient") or {}).get("name") or first.get("patient_on_page"),
        prescriber_as_written=first.get("prescriber_on_page"), date=first.get("date_on_page"),
        doctor=DoctorOut(**{k: doc.get(k) for k in ("name", "speciality", "clinic_address",
                                                     "registration_no")}) if doc else None,
        diagnosis=snap.get("diagnosis") if snap.get("diagnosis_visibility") != "hidden" else None,
        medicines=meds,
        # Deliberately no `safer_alternative` or `mechanism`: an alternative is
        # a prescribing decision, and a patient reading it alone may switch.
        interactions=[PatientInteraction(drug_a=i["drug_a"], drug_b=i["drug_b"],
                                         severity=i["severity"], effect=i.get("effect", ""))
                      for i in result.get("interactions", [])],
        overall_risk=result.get("overall_risk", "none"),
        not_checked=result.get("skipped", []),
        page_numbers=[p["page_no"] for p in pages],
        allow_substitution=snap.get("allow_substitution", True),
        dispensing_state=dispensing_state(snap["medicines"], checks),
        dispensing=[check_out(ch, pharmacy_name(db.get(User, ch.pharmacy_id)) if ch.pharmacy_id else None)
                    for ch in checks],
        shares=shares,
    )


# ─── access ──────────────────────────────────────────────────────────────────


def _addressed_to(user: User, v: Optional[ConsultationVersion]) -> bool:
    if not v or not user.phone or not user.phone_verified_at:
        return False
    return v.patient_phone == user.phone


def _mine(db: Session, user: User, cid: str):
    c = db.get(Consultation, cid)
    if not c:
        # A link from before consultations existed carries the page id.
        rx = db.get(Prescription, cid)
        c = rx.consultation if rx else None
    v = latest_version(c) if c else None
    if not c or not _addressed_to(user, v):
        raise HTTPException(404, "Prescription not found")
    return c, v


# ─── routes ──────────────────────────────────────────────────────────────────


@router.get("/prescriptions", response_model=PatientInbox)
def my_prescriptions(db: Session = Depends(get_db), user: User = Depends(patient_only)):
    if not user.phone or not user.phone_verified_at:
        return PatientInbox(phone=user.phone, phone_verified=False)
    # Every consultation with ANY version addressed here; kept only if the
    # LATEST version still is (a re-addressed and re-finalised one moves away).
    ids = {cid for (cid,) in db.query(ConsultationVersion.consultation_id)
           .filter(ConsultationVersion.patient_phone == user.phone).distinct()}
    rows = db.query(Consultation).filter(Consultation.id.in_(ids)).all() if ids else []
    out = []
    for c in rows:
        v = latest_version(c)
        if not _addressed_to(user, v):
            continue
        snap = snapshot(v)
        checks = checks_for(db, c.id)
        grants = db.query(AccessGrant).filter(AccessGrant.consultation_id == c.id).all()
        out.append(PatientSummary(
            id=c.id, date=(snap.get("pages") or [{}])[0].get("date_on_page"),
            finalised_at=aware(v.finalised_at).isoformat(), version=v.version,
            doctor_name=(snap.get("doctor") or {}).get("name"),
            medicine_count=len(snap["medicines"]),
            overall_risk=(snap.get("interactions") or {}).get("overall_risk", "none"),
            dispensing_state=dispensing_state(snap["medicines"], checks),
            shared_now=any(grant_status(g) == ACTIVE for g in grants),
        ))
    out.sort(key=lambda s: s.finalised_at, reverse=True)
    return PatientInbox(phone=user.phone, phone_verified=True, prescriptions=out)


@router.get("/prescriptions/{cid}", response_model=PatientPrescription)
def my_prescription(cid: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(patient_only)):
    c, v = _mine(db, user, cid)
    _event(db, request, "rx_viewed", user.id, f"{c.id} v{v.version}")
    db.commit()
    return patient_view(db, c, v)


def _page_image(v: ConsultationVersion, n: int):
    page = next((p for p in snapshot(v).get("pages", []) if p["page_no"] == n), None)
    path = UPLOAD_DIR / page["image"] if page else None
    if not path or not path.is_file():
        raise HTTPException(404, "Not found")
    return path


@router.get("/prescriptions/{cid}/pages/{n}/image")
def my_page_image(cid: str, n: int, db: Session = Depends(get_db), user: User = Depends(patient_only)):
    _, v = _mine(db, user, cid)
    return image_response(_page_image(v, n))


@router.get("/prescriptions/{cid}/image")
def my_prescription_image(cid: str, db: Session = Depends(get_db), user: User = Depends(patient_only)):
    """First page. Kept for links from before multi-page prescriptions."""
    return my_page_image(cid, 1, db, user)


@router.get("/prescriptions/{cid}/pdf")
def my_prescription_pdf(cid: str, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(patient_only)):
    from rx_pdf import DISPLAY_TZ, render  # fpdf2 is only needed here

    c, v = _mine(db, user, cid)
    view = patient_view(db, c, v, with_activity=False)
    images = []
    for n in view.page_numbers:
        try:
            images.append(_page_image(v, n))
        except HTTPException:
            pass
    pdf = render(view, images, generated_at=now(),
                 verify_url=f"{settings.PUBLIC_BASE_URL}/check/{v.verification_id}")
    _event(db, request, "rx_pdf_downloaded", user.id, f"{c.id} v{v.version}")
    db.commit()
    day = aware(v.finalised_at).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")
    return Response(
        pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="prescription-{day}-{view.verification_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/prescriptions/{cid}/share", response_model=ShareCreated)
def share_with_pharmacy(cid: str, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(patient_only)):
    """Workflow stage 5. The code opens THIS prescription only, for one pharmacy,
    once. Issuing a new one cancels any earlier code not yet used."""
    _throttle("share_issue", user.id, 10, 60 * 60)
    c, v = _mine(db, user, cid)
    t = now()
    for g in db.query(AccessGrant).filter(AccessGrant.consultation_id == c.id,
                                          AccessGrant.redeemed_at.is_(None),
                                          AccessGrant.cancelled_at.is_(None)).all():
        if grant_status(g, t) == ISSUED:
            g.cancelled_at = t
    otp = new_otp(db)
    g = AccessGrant(consultation_id=c.id, patient_id=user.id, otp_hash=hash_otp(otp), issued_at=t,
                    redeem_by=t + timedelta(minutes=settings.SHARE_REDEEM_MINUTES))
    db.add(g)
    db.flush()
    _event(db, request, "rx_share_issued", user.id, f"{c.id} grant {g.id}")
    db.commit()
    return ShareCreated(grant_id=g.id, otp=otp, redeem_by=aware(g.redeem_by).isoformat(),
                        window_minutes=settings.SHARE_WINDOW_MINUTES)


@router.post("/grants/{gid}/revoke")
def revoke_grant(gid: str, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(patient_only)):
    g = db.get(AccessGrant, gid)
    if not g or g.patient_id != user.id:
        raise HTTPException(404, "Not found")
    st = grant_status(g)
    if st in (ISSUED, ACTIVE):
        g.revoked_at = now()
        _event(db, request, "rx_share_revoked", user.id, f"{g.consultation_id} grant {g.id} ({st})")
        db.commit()
    return {"ok": True, "status": grant_status(g)}

