"""
Patient app API — Workflow.md stage 4: read and download a prescription.

Access rule, in one place (`_mine`): a patient sees a prescription only when

  1. a doctor has confirmed it (`reviewed_at`) — a draft never reaches a patient;
  2. the doctor addressed it to a phone number (`patient_phone`); and
  3. that number is the patient's own, VERIFIED, account phone.

Without 3, anyone could register with someone else's number and read their
prescriptions. Anything that fails the rule is a 404, never a 403, so the API
does not confirm that a prescription exists.

Wording follows Workflow.md §4 stage 4, because patients read this without a
clinician beside them: interactions say "discuss with your doctor or
pharmacist" and never name a "safer alternative" to switch to; "no
interactions found" always comes with what was NOT checked; and the page says
the transcription is AI-generated and doctor-reviewed.

Stage 1 (consultation sessions) is not built, so a prescription is still one
page and the doctor enters the phone on the review screen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import Role, User, require_roles
from auth.models import aware
from auth.routes import _event
from config.database import get_db
from config.settings import settings
from models import Prescription

# Same directory main.py writes uploads to.
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"

router = APIRouter(prefix="/api/patient", tags=["patient"])
patient_only = require_roles(Role.PATIENT)

PATIENT_NOTICE = (
    "This prescription was transcribed from the handwritten original by AI and "
    "reviewed by your doctor. If anything here differs from what your doctor told "
    "you, ask your doctor or pharmacist. Do not stop or change a medicine on the "
    "basis of this document alone."
)
INTERACTION_ADVICE = "Discuss this with your doctor or pharmacist. Do not stop taking a medicine on your own."


# ─── the patient view (shared by the JSON API and the PDF) ──────────────────


class DoctorOut(BaseModel):
    name: str
    speciality: Optional[str] = None
    clinic_address: Optional[str] = None
    registration_no: Optional[str] = None


class PatientMedicine(BaseModel):
    name: Optional[str] = None      # None: the line could not be read reliably
    as_written: str
    contains: List[str] = []
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    instructions: Optional[str] = None
    uncertain: bool = False         # still needs confirming with the doctor


class PatientInteraction(BaseModel):
    drug_a: str
    drug_b: str
    severity: str
    effect: str = ""
    advice: str = INTERACTION_ADVICE


class PatientPrescription(BaseModel):
    id: str
    verification_id: str
    patient_name: Optional[str] = None
    prescriber_as_written: Optional[str] = None
    date: Optional[str] = None
    reviewed_at: str
    doctor: Optional[DoctorOut] = None
    medicines: List[PatientMedicine] = []
    interactions: List[PatientInteraction] = []
    overall_risk: str = "none"
    not_checked: List[str] = []
    notice: str = PATIENT_NOTICE


class PatientSummary(BaseModel):
    id: str
    date: Optional[str] = None
    reviewed_at: str
    doctor_name: Optional[str] = None
    medicine_count: int
    overall_risk: str


class PatientInbox(BaseModel):
    phone: Optional[str] = None
    phone_verified: bool
    prescriptions: List[PatientSummary] = []


def verification_id(rx: Prescription) -> str:
    """Short code tying the document to one confirmed version of one prescription.
    Keyed by SECRET_KEY, so it cannot be computed without the server."""
    msg = f"rx:{rx.id}:{aware(rx.reviewed_at).isoformat(timespec='seconds')}".encode()
    digest = hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).digest()
    code = base64.b32encode(digest).decode()[:10]
    return f"PRX-{code[:5]}-{code[5:]}"


def _doctor(db: Session, rx: Prescription) -> Optional[DoctorOut]:
    doc = db.get(User, rx.reviewed_by) if rx.reviewed_by else None
    if not doc:
        return None
    profile = json.loads(doc.profile_json or "{}")
    return DoctorOut(
        name=doc.full_name, speciality=profile.get("speciality"),
        clinic_address=profile.get("clinic_address"),
        registration_no=profile.get("registration_no"),
    )


def _interaction_result(rx: Prescription) -> dict:
    run = rx.interaction_runs[-1] if rx.interaction_runs else None
    return json.loads(run.result_json) if run else {}


def patient_view(db: Session, rx: Prescription) -> PatientPrescription:
    meds = []
    for m in json.loads(rx.resolved_json):
        readable = m.get("outcome") in ("confirmed", "probable") and m.get("brand")
        meds.append(PatientMedicine(
            name=m.get("brand") if readable else None,
            as_written=m.get("raw_reading") or "",
            contains=m.get("ingredients") or [],
            dosage=m.get("dosage"), frequency=m.get("frequency"),
            duration=m.get("duration"), instructions=m.get("instructions"),
            uncertain=not readable,
        ))
    result = _interaction_result(rx)
    return PatientPrescription(
        id=rx.id, verification_id=verification_id(rx),
        patient_name=rx.patient_name, prescriber_as_written=rx.prescriber_name,
        date=rx.prescribed_date, reviewed_at=aware(rx.reviewed_at).isoformat(),
        doctor=_doctor(db, rx), medicines=meds,
        # Deliberately no `safer_alternative` or `mechanism`: an alternative is
        # a prescribing decision, and a patient reading it alone may switch.
        interactions=[
            PatientInteraction(drug_a=i["drug_a"], drug_b=i["drug_b"],
                               severity=i["severity"], effect=i.get("effect", ""))
            for i in result.get("interactions", [])
        ],
        overall_risk=result.get("overall_risk", "none"),
        not_checked=result.get("skipped", []),
    )


# ─── access ──────────────────────────────────────────────────────────────────


def _mine(db: Session, user: User, prescription_id: str) -> Prescription:
    rx = db.get(Prescription, prescription_id)
    if (not rx or rx.reviewed_at is None or not rx.patient_phone
            or not user.phone_verified_at or rx.patient_phone != user.phone):
        raise HTTPException(404, "Prescription not found")
    return rx


# ─── routes ──────────────────────────────────────────────────────────────────


@router.get("/prescriptions", response_model=PatientInbox)
def my_prescriptions(db: Session = Depends(get_db), user: User = Depends(patient_only)):
    if not user.phone or not user.phone_verified_at:
        return PatientInbox(phone=user.phone, phone_verified=False)
    rows = (
        db.query(Prescription)
        .filter(Prescription.patient_phone == user.phone, Prescription.reviewed_at.isnot(None))
        .order_by(Prescription.reviewed_at.desc()).all()
    )
    out = []
    for rx in rows:
        doc = _doctor(db, rx)
        out.append(PatientSummary(
            id=rx.id, date=rx.prescribed_date, reviewed_at=aware(rx.reviewed_at).isoformat(),
            doctor_name=doc.name if doc else rx.prescriber_name,
            medicine_count=len(json.loads(rx.resolved_json)),
            overall_risk=_interaction_result(rx).get("overall_risk", "none"),
        ))
    return PatientInbox(phone=user.phone, phone_verified=True, prescriptions=out)


@router.get("/prescriptions/{prescription_id}", response_model=PatientPrescription)
def my_prescription(prescription_id: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(patient_only)):
    rx = _mine(db, user, prescription_id)
    _event(db, request, "rx_viewed", user.id, rx.id)
    db.commit()
    return patient_view(db, rx)


@router.get("/prescriptions/{prescription_id}/image")
def my_prescription_image(prescription_id: str, db: Session = Depends(get_db),
                          user: User = Depends(patient_only)):
    rx = _mine(db, user, prescription_id)
    path = UPLOAD_DIR / Path(rx.image_path).name
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


@router.get("/prescriptions/{prescription_id}/pdf")
def my_prescription_pdf(prescription_id: str, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(patient_only)):
    from rx_pdf import DISPLAY_TZ, render  # fpdf2 is only needed here

    rx = _mine(db, user, prescription_id)
    view = patient_view(db, rx)
    image = UPLOAD_DIR / Path(rx.image_path).name
    pdf = render(view, image if image.is_file() else None, generated_at=datetime.now(timezone.utc))
    _event(db, request, "rx_pdf_downloaded", user.id, rx.id)
    db.commit()
    day = aware(rx.reviewed_at).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")
    return Response(
        pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="prescription-{day}-{view.verification_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )
