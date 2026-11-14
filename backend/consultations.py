"""
Doctor API for Workflow.md stages 1–3: consultations, pages, review, finalise.

    POST   /api/consultations                          start one (patient name + phone)
    GET    /api/consultations                          this doctor's consultations
    GET    /api/consultations/{id}                     working copy, every page
    PATCH  /api/consultations/{id}                     patient, clinic, diagnosis, flags
    POST   /api/consultations/{id}/pages               upload a page: S0–S4 run
    DELETE /api/consultations/{id}/pages/{page_id}     only before the first finalise
    POST   /api/consultations/{id}/medicines           add a medicine the reader missed
    POST   /api/prescriptions/{page_id}/medicines/{i}/remove    strike an invented line
    POST   /api/prescriptions/{page_id}/medicines/{i}/restore
    POST   /api/consultations/{id}/finalise            -> immutable version N
    GET    /api/consultations/{id}/versions/{n}        a finalised snapshot

Rules this module keeps:

  * ONE INTERACTION CHECK PER CONSULTATION, across every page. A page-1 drug and
    a page-2 drug can interact; checking page by page would miss exactly that.
  * FINALISED MEANS IMMUTABLE. Finalising writes a `ConsultationVersion`
    snapshot, and patients and pharmacies read only snapshots. A later edit
    changes the working copy and sets status `revising`; nobody else sees it
    until it is finalised as the next version, labelled "revised".
  * EVERY EDIT IS STILL A CORRECTION ROW (models.py §16.3): additions as
    `missed`, removals as `hallucinated`, a diagnosis edit as a page-level field.
  * A doctor sees only their own consultations. Anything else is a 404.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

import pipeline
from auth import Role, User, require_roles
from auth.models import aware
from auth.routes import _event, norm_phone
from config.database import get_db
from models import (
    Consultation,
    ConsultationStatus,
    ConsultationVersion,
    Correction,
    CorrectionType,
    DiagnosisVisibility,
    InteractionRun,
    Prescription,
)
from views import (
    PIL_FORMATS,
    UPLOAD_DIR,
    InteractionOutcome,
    MedicineOut,
    PrescriptionOut,
    interaction_out,
    make_verification_id,
    page_out,
)

router = APIRouter(tags=["consultations"])
doctor_only = require_roles(Role.DOCTOR)

MAX_PAGES = 10
UNRESOLVED = ("ambiguous", "illegible", "unmatched")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── shapes ──────────────────────────────────────────────────────────────────


class ConsultationIn(BaseModel):
    patient_name: str = Field(min_length=1, max_length=120)
    patient_phone: str
    clinic_address: Optional[str] = Field(None, max_length=300)
    diagnosis: Optional[str] = Field(None, max_length=500)


class ConsultationPatch(BaseModel):
    patient_name: Optional[str] = Field(None, min_length=1, max_length=120)
    patient_phone: Optional[str] = None
    clinic_address: Optional[str] = Field(None, max_length=300)
    diagnosis: Optional[str] = Field(None, max_length=500)
    diagnosis_visibility: Optional[DiagnosisVisibility] = None
    allow_substitution: Optional[bool] = None


class AddMedicineIn(BaseModel):
    prescription_id: Optional[str] = Field(None, description="Page to attach it to; default the last page")
    name: str = Field(min_length=1, max_length=120, description="Brand or generic, as the doctor means it")
    dosage: Optional[str] = Field(None, max_length=120)
    frequency: Optional[str] = Field(None, max_length=120)
    duration: Optional[str] = Field(None, max_length=120)
    instructions: Optional[str] = Field(None, max_length=300)


class RemoveIn(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


class FinaliseIn(BaseModel):
    acknowledge_unresolved: bool = Field(
        False, description="Finalise even though some lines are still unreadable or unmatched")
    change_note: Optional[str] = Field(None, max_length=300, description="Shown as 'what changed' on a revision")


class DoctorOut(BaseModel):
    id: Optional[str] = None
    name: str
    speciality: Optional[str] = None
    clinic_address: Optional[str] = None
    registration_no: Optional[str] = None


class VersionSummary(BaseModel):
    version: int
    finalised_at: str
    verification_id: str
    change_note: Optional[str] = None


class ConsultationOut(BaseModel):
    id: str
    created_at: str
    status: str
    current_version: int
    legacy: bool = False
    patient_name: Optional[str] = None
    patient_phone: Optional[str] = None
    clinic_address: Optional[str] = None
    diagnosis: Optional[str] = None
    diagnosis_as_read: List[str] = []
    diagnosis_visibility: str
    allow_substitution: bool
    doctor: Optional[DoctorOut] = None
    pages: List[PrescriptionOut] = []
    medicines: List[MedicineOut] = []
    interactions: InteractionOutcome
    unresolved: List[str] = []
    versions: List[VersionSummary] = []
    reader_is_mock: bool = False


class ConsultationSummary(BaseModel):
    id: str
    created_at: str
    updated_at: Optional[str] = None
    status: str
    current_version: int
    patient_name: Optional[str] = None
    patient_phone: Optional[str] = None
    page_count: int
    medicine_count: int
    needs_attention: int
    overall_risk: str
    legacy: bool = False


# ─── helpers ─────────────────────────────────────────────────────────────────


def doctor_details(user: Optional[User], clinic: Optional[str] = None) -> Optional[DoctorOut]:
    if not user:
        return None
    p = json.loads(user.profile_json or "{}")
    return DoctorOut(id=user.id, name=user.full_name, speciality=p.get("speciality"),
                     clinic_address=clinic or p.get("clinic_address"),
                     registration_no=p.get("registration_no"))


def owned(db: Session, user: User, cid: str) -> Consultation:
    c = db.get(Consultation, cid)
    # Backfilled rows with no recorded owner stay visible to every doctor, as
    # every row was before ownership existed; anything owned is owner-only.
    if not c or (c.doctor_id is not None and c.doctor_id != user.id):
        raise HTTPException(404, "Consultation not found")
    return c


def owned_page(db: Session, user: User, pid: str) -> Prescription:
    rx = db.get(Prescription, pid)
    if not rx or not rx.consultation_id:
        raise HTTPException(404, "Prescription not found")
    owned(db, user, rx.consultation_id)
    return rx


def all_meds(c: Consultation) -> List[dict]:
    out = []
    for rx in c.pages:
        for m in json.loads(rx.resolved_json):
            out.append({**m, "page_no": rx.page_no, "prescription_id": rx.id})
    return out


def _label(m: dict) -> str:
    return m.get("brand") or m.get("generic") or f"“{m.get('raw_reading') or '?'}”"


def unresolved(meds: List[dict]) -> List[str]:
    return [f"{_label(m)} (page {m.get('page_no')}): {m['outcome']}"
            for m in meds if not m.get("removed") and m.get("outcome") in UNRESOLVED]


def recompute(db: Session, c: Consultation) -> dict:
    """S4 across every page, plus each page's own run for the page view."""
    for rx in c.pages:
        meds = json.loads(rx.resolved_json)
        db.add(InteractionRun(prescription_id=rx.id, **_run_fields(pipeline.check(meds))))
    result = pipeline.check(all_meds(c))
    db.add(InteractionRun(consultation_id=c.id, **_run_fields(result)))
    return result


def _run_fields(result: dict) -> dict:
    return dict(overall_risk=result["overall_risk"], total_count=result["total_count"],
                result_json=json.dumps(result))


def latest_result(db: Session, c: Consultation) -> Optional[dict]:
    run = (db.query(InteractionRun).filter(InteractionRun.consultation_id == c.id)
           .order_by(InteractionRun.created_at.desc()).first())
    return json.loads(run.result_json) if run else None


def mark_changed(c: Consultation) -> None:
    if c.status == ConsultationStatus.FINALISED:
        c.status = ConsultationStatus.REVISING
    c.updated_at = _now()


def consultation_out(db: Session, c: Consultation) -> ConsultationOut:
    meds = all_meds(c)
    doctor = db.get(User, c.doctor_id) if c.doctor_id else None
    as_read = []
    for rx in c.pages:
        d = json.loads(rx.page_read_json).get("diagnosis")
        if d and d not in as_read:
            as_read.append(d)
    return ConsultationOut(
        id=c.id, created_at=aware(c.created_at).isoformat(), status=c.status.value,
        current_version=c.current_version, legacy=bool(c.legacy),
        patient_name=c.patient_name, patient_phone=c.patient_phone,
        clinic_address=c.clinic_address, diagnosis=c.diagnosis, diagnosis_as_read=as_read,
        diagnosis_visibility=c.diagnosis_visibility.value,
        allow_substitution=bool(c.allow_substitution),
        doctor=doctor_details(doctor, c.clinic_address),
        pages=[page_out(rx) for rx in c.pages],
        medicines=[MedicineOut(**m) for m in meds],
        interactions=interaction_out(latest_result(db, c)),
        unresolved=unresolved(meds),
        versions=[VersionSummary(version=v.version, finalised_at=aware(v.finalised_at).isoformat(),
                                 verification_id=v.verification_id, change_note=v.change_note)
                  for v in c.versions],
        reader_is_mock=pipeline.is_mock(),
    )


def build_snapshot(c: Consultation, doctor: Optional[DoctorOut], result: dict,
                   version: int, finalised_at: datetime) -> dict:
    """Everything a patient or pharmacy will ever see of this version. Medicines
    are re-indexed across pages; struck lines are left out."""
    meds, i = [], 0
    for m in all_meds(c):
        if m.get("removed"):
            continue
        meds.append({k: m.get(k) for k in (
            "brand", "generic", "ingredients", "verified_ingredients", "unverified_ingredients",
            "verification", "outcome", "raw_reading", "dosage", "frequency", "duration",
            "instructions", "page_no", "doctor_entered")} | {"index": i})
        i += 1
    return {
        "version": version,
        "finalised_at": finalised_at.isoformat(),
        "doctor": doctor.model_dump() if doctor else None,
        "patient": {"name": c.patient_name, "phone": c.patient_phone},
        "diagnosis": c.diagnosis or None,
        "diagnosis_visibility": c.diagnosis_visibility.value,
        "allow_substitution": bool(c.allow_substitution),
        "pages": [{"page_no": rx.page_no, "prescription_id": rx.id,
                   "image": Path(rx.image_path).name, "date_on_page": rx.prescribed_date,
                   "prescriber_on_page": rx.prescriber_name, "patient_on_page": rx.patient_name}
                  for rx in c.pages],
        "medicines": meds,
        "interactions": result,
    }


def _summary(db: Session, c: Consultation) -> ConsultationSummary:
    meds = [m for m in all_meds(c) if not m.get("removed")]
    result = latest_result(db, c) or {}
    return ConsultationSummary(
        id=c.id, created_at=aware(c.created_at).isoformat(),
        updated_at=aware(c.updated_at).isoformat() if c.updated_at else None,
        status=c.status.value, current_version=c.current_version,
        patient_name=c.patient_name, patient_phone=c.patient_phone,
        page_count=len(c.pages), medicine_count=len(meds),
        needs_attention=sum(1 for m in meds if m.get("needs_hard_confirmation")),
        overall_risk=result.get("overall_risk", "none"), legacy=bool(c.legacy),
    )


# ─── routes ──────────────────────────────────────────────────────────────────


@router.post("/api/consultations", response_model=ConsultationOut, status_code=201)
def create_consultation(body: ConsultationIn, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(doctor_only)):
    profile = json.loads(user.profile_json or "{}")
    c = Consultation(
        doctor_id=user.id, patient_name=body.patient_name.strip(),
        patient_phone=norm_phone(body.patient_phone),
        clinic_address=(body.clinic_address or "").strip() or profile.get("clinic_address"),
        diagnosis=(body.diagnosis or "").strip() or None,
    )
    db.add(c)
    db.flush()  # assigns c.id for the audit row
    _event(db, request, "consultation_created", user.id, c.id)
    db.commit()
    db.refresh(c)
    return consultation_out(db, c)


@router.get("/api/consultations", response_model=List[ConsultationSummary])
def list_consultations(db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    rows = (db.query(Consultation)
            .filter(or_(Consultation.doctor_id == user.id, Consultation.doctor_id.is_(None)))
            .order_by(Consultation.created_at.desc()).limit(200).all())
    return [_summary(db, c) for c in rows]


@router.get("/api/consultations/{cid}", response_model=ConsultationOut)
def get_consultation(cid: str, db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    return consultation_out(db, owned(db, user, cid))


@router.patch("/api/consultations/{cid}", response_model=ConsultationOut)
def update_consultation(cid: str, body: ConsultationPatch, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    changed = []
    if body.patient_name is not None and body.patient_name.strip() != c.patient_name:
        c.patient_name = body.patient_name.strip()
        changed.append("patient_name")
    if body.patient_phone is not None:
        phone = norm_phone(body.patient_phone)
        if phone != c.patient_phone:
            c.patient_phone = phone
            changed.append("patient_phone")
    if body.clinic_address is not None and body.clinic_address.strip() != (c.clinic_address or ""):
        c.clinic_address = body.clinic_address.strip() or None
        changed.append("clinic_address")
    if body.diagnosis is not None and (body.diagnosis.strip() or None) != c.diagnosis:
        new = body.diagnosis.strip() or None
        # A diagnosis edit is a page-level correction against what was READ, so
        # the corpus learns from it like any other field.
        first = c.pages[0] if c.pages else None
        as_read = json.loads(first.page_read_json).get("diagnosis") if first else None
        if first:
            db.add(Correction(prescription_id=first.id, medicine_index=-1, field="diagnosis",
                              predicted_value=as_read, corrected_value=new,
                              correction_type=CorrectionType.MISREAD if as_read else CorrectionType.MISSED,
                              corrected_by=user.id))
        c.diagnosis = new
        changed.append("diagnosis")
    if body.diagnosis_visibility is not None and body.diagnosis_visibility != c.diagnosis_visibility:
        c.diagnosis_visibility = body.diagnosis_visibility
        changed.append("diagnosis_visibility")
    if body.allow_substitution is not None and body.allow_substitution != c.allow_substitution:
        c.allow_substitution = body.allow_substitution
        changed.append("allow_substitution")
    if changed:
        mark_changed(c)
        _event(db, request, "consultation_updated", user.id, f"{c.id}: {', '.join(changed)}")
        db.commit()
        db.refresh(c)
    return consultation_out(db, c)


@router.post("/api/consultations/{cid}/pages", response_model=ConsultationOut)
async def upload_page(cid: str, request: Request, image: UploadFile = File(...),
                      db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    if len(c.pages) >= MAX_PAGES:
        raise HTTPException(422, f"A prescription can have at most {MAX_PAGES} pages.")
    rx = await ingest_page(db, image, c)
    _event(db, request, "page_uploaded", user.id, f"{c.id} page {rx.page_no}")
    db.commit()
    db.refresh(c)
    return consultation_out(db, c)


async def ingest_page(db: Session, image: UploadFile, c: Consultation) -> Prescription:
    """Store the image, run S0–S4, attach the page, recheck the whole consultation."""
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image (jpg, png, ...)")
    contents = await image.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(413, "That image is over 10 MB.")
    # The stored extension comes from what the bytes ARE, never the filename.
    try:
        import io

        from PIL import Image

        fmt = Image.open(io.BytesIO(contents)).format
    except Exception:
        fmt = None
    ext = PIL_FORMATS.get(fmt or "")
    if not ext:
        raise HTTPException(400, "That file is not a supported image (JPG, PNG, WEBP, GIF, BMP or TIFF).")
    img_id = str(uuid.uuid4())
    img_path = UPLOAD_DIR / f"{img_id}.{ext}"
    img_path.write_bytes(contents)
    try:
        page, meds, norm, prov = pipeline.read_page(contents)
    except Exception as exc:  # spend cap, bad key, API outage, unreadable file
        img_path.unlink(missing_ok=True)
        raise HTTPException(502, f"Reader failed: {exc}") from exc

    rx = Prescription(
        id=img_id, image_path=str(img_path), image_sha256=norm.sha256,
        page_read_json=page.model_dump_json(),
        resolved_json=json.dumps([pipeline.med_dict(i, m) for i, m in enumerate(meds)]),
        document_type=page.document_type, overall_legibility=page.overall_legibility,
        patient_name=page.patient_name, prescriber_name=page.prescriber_name,
        prescribed_date=page.date, patient_phone=c.patient_phone,
        consultation_id=c.id, page_no=len(c.pages) + 1, **prov,
    )
    db.add(rx)
    c.pages.append(rx)
    if not c.diagnosis and page.diagnosis:
        c.diagnosis = page.diagnosis   # as read; the doctor confirms or edits it
    if not c.patient_name and page.patient_name:
        c.patient_name = page.patient_name
    db.flush()
    recompute(db, c)
    mark_changed(c)
    return rx


@router.delete("/api/consultations/{cid}/pages/{pid}", response_model=ConsultationOut)
def delete_page(cid: str, pid: str, request: Request, db: Session = Depends(get_db),
                user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    rx = db.get(Prescription, pid)
    if not rx or rx.consultation_id != c.id:
        raise HTTPException(404, "Page not found")
    if c.current_version > 0:
        # A finalised version points at this page's image. Strike its lines instead.
        raise HTTPException(409, "This page is part of a finalised version and cannot be deleted. "
                                 "Remove its medicines instead.")
    c.pages.remove(rx)
    db.delete(rx)
    for n, p in enumerate(c.pages, 1):
        p.page_no = n
    (UPLOAD_DIR / Path(rx.image_path).name).unlink(missing_ok=True)
    db.flush()
    recompute(db, c)
    mark_changed(c)
    _event(db, request, "page_deleted", user.id, f"{c.id} {pid}")
    db.commit()
    db.refresh(c)
    return consultation_out(db, c)


@router.post("/api/consultations/{cid}/medicines", response_model=ConsultationOut)
def add_medicine(cid: str, body: AddMedicineIn, db: Session = Depends(get_db),
                 user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    if not c.pages:
        raise HTTPException(422, "Upload a page first.")
    rx = c.pages[-1]
    if body.prescription_id:
        rx = next((p for p in c.pages if p.id == body.prescription_id), None)
        if not rx:
            raise HTTPException(404, "Page not found")
    meds = json.loads(rx.resolved_json)
    blank = dict(index=len(meds), brand=None, generic=None, ingredients=[], verified_ingredients=[],
                 unverified_ingredients=[], verification="unknown", outcome="illegible",
                 confidence=1.0, match_score=None, raw_reading="", matched_via=None,
                 candidates=[], bbox=None, needs_hard_confirmation=True, removed=False)
    new = pipeline.reresolve(blank, body.name)
    new.update(dosage=body.dosage, frequency=body.frequency, duration=body.duration,
               instructions=body.instructions)
    meds.append(new)
    rx.resolved_json = json.dumps(meds)
    db.add(Correction(prescription_id=rx.id, medicine_index=new["index"], field="medicine",
                      predicted_value=None, corrected_value=body.name,
                      correction_type=CorrectionType.MISSED, corrected_by=user.id,
                      outcome=new["outcome"], verification=new["verification"]))
    recompute(db, c)
    mark_changed(c)
    db.commit()
    db.refresh(c)
    return consultation_out(db, c)


def _set_removed(db: Session, user: User, pid: str, index: int, removed: bool,
                 note: Optional[str]) -> Consultation:
    rx = owned_page(db, user, pid)
    meds = json.loads(rx.resolved_json)
    if not 0 <= index < len(meds):
        raise HTTPException(404, "Medicine not found")
    m = meds[index]
    if bool(m.get("removed")) == removed:
        return rx.consultation
    m["removed"] = removed
    rx.resolved_json = json.dumps(meds)
    db.add(Correction(
        prescription_id=rx.id, medicine_index=index, field="removed",
        predicted_value=json.dumps({"brand": m.get("brand"), "raw_reading": m.get("raw_reading")}),
        corrected_value="removed" if removed else "restored",
        correction_type=CorrectionType.HALLUCINATED if removed else CorrectionType.OTHER,
        model_confidence=m.get("confidence"), match_score=m.get("match_score"),
        outcome=m.get("outcome"), verification=m.get("verification"),
        corrected_by=user.id, note=note,
    ))
    c = rx.consultation
    recompute(db, c)
    mark_changed(c)
    db.commit()
    db.refresh(c)
    return c


@router.post("/api/prescriptions/{pid}/medicines/{index}/remove", response_model=ConsultationOut)
def remove_medicine(pid: str, index: int, body: RemoveIn, db: Session = Depends(get_db),
                    user: User = Depends(doctor_only)):
    return consultation_out(db, _set_removed(db, user, pid, index, True, body.note))


@router.post("/api/prescriptions/{pid}/medicines/{index}/restore", response_model=ConsultationOut)
def restore_medicine(pid: str, index: int, db: Session = Depends(get_db),
                     user: User = Depends(doctor_only)):
    return consultation_out(db, _set_removed(db, user, pid, index, False, None))


@router.post("/api/consultations/{cid}/finalise", response_model=ConsultationOut)
def finalise(cid: str, body: FinaliseIn, request: Request, db: Session = Depends(get_db),
             user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    if c.status == ConsultationStatus.FINALISED:
        raise HTTPException(409, f"Nothing has changed since version {c.current_version}.")
    if not c.pages:
        raise HTTPException(422, "Upload at least one page before finalising.")
    if not c.patient_name or not c.patient_phone:
        raise HTTPException(422, "Add the patient's name and phone number before finalising.")
    meds = all_meds(c)
    ambiguous = [m for m in meds if not m.get("removed") and m.get("outcome") == "ambiguous"]
    if ambiguous:
        # Picking one of N candidates is a single click; finalising past it would
        # send the patient a line that names no medicine.
        raise HTTPException(422, "Choose the medicine for: " + "; ".join(_label(m) for m in ambiguous))
    left = unresolved(meds)
    if left and not body.acknowledge_unresolved:
        raise HTTPException(422, "Some lines are still unresolved: " + "; ".join(left)
                            + ". Correct or remove them, or confirm you want to finalise anyway.")

    now = _now()
    version = c.current_version + 1
    result = recompute(db, c)
    owner = db.get(User, c.doctor_id) if c.doctor_id else user
    snap = build_snapshot(c, doctor_details(owner, c.clinic_address), result, version, now)
    db.add(ConsultationVersion(
        consultation_id=c.id, version=version, finalised_at=now, finalised_by=user.id,
        patient_phone=c.patient_phone,
        change_note=(body.change_note or "").strip() or None,
        verification_id=make_verification_id(c.id, version, now),
        snapshot_json=json.dumps(snap),
    ))
    # Each page is now a fully labelled example for the corpus (models.py).
    for rx in c.pages:
        rx.reviewed_at, rx.reviewed_by, rx.patient_phone = now, user.id, c.patient_phone
    c.current_version, c.finalised_at, c.status = version, now, ConsultationStatus.FINALISED
    c.updated_at = now
    _event(db, request, "rx_finalised", user.id, f"{c.id} v{version} -> {c.patient_phone}")
    db.commit()
    db.refresh(c)
    return consultation_out(db, c)


@router.get("/api/consultations/{cid}/versions/{n}")
def get_version(cid: str, n: int, db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    c = owned(db, user, cid)
    v = next((v for v in c.versions if v.version == n), None)
    if not v:
        raise HTTPException(404, "Version not found")
    return {"version": v.version, "verification_id": v.verification_id,
            "change_note": v.change_note, **json.loads(v.snapshot_json)}
