"""
PrescriptAI Backend — FastAPI REST API.  (docs/ARCHITECTURE_V2.md §8)

Wires the pipeline:

    S0 normalise -> S1 VLM read -> S2 vocab -> S3 brand/generic + NLEM
                 -> S4 interaction check -> S5 doctor verification

Two behaviours this API is deliberately built around:

  * THE READING IS ALWAYS RETURNED. Verification changes the strength of the
    warning, never whether the result is visible. A blocked screen helps nobody
    and the doctor is the safety net.

  * NOTHING IS SILENTLY DROPPED. `skipped` names every medicine and every
    ingredient excluded from the safety check, with the reason. "We checked 4 of
    6" and "we checked 6" are very different claims to put in front of a
    clinician.

READER selects the page reader: "auto" (the default) uses Claude when
ANTHROPIC_API_KEY is set in .env and the mock reader otherwise; "mock" and
"claude" force one. The mock runs the whole product with no key and no cost.

Routers: auth (auth/), consultations + pages + finalise (consultations.py),
patient (patient.py), pharmacy (pharmacy.py). This file keeps the page-level
routes, stats, the corrections corpus, image serving and /api/verify.

Access (backend/auth/): prescription routes and uploaded images are for signed-in
DOCTORS only — the images are patient data. The manual interaction lookup and
/api/health stay public; neither touches a patient record.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "ml_pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env import load_env  # noqa: E402

load_env()

import pipeline  # noqa: E402
from auth import Role, User, require_vetted  # noqa: E402
from auth import router as auth_router  # noqa: E402
from auth.models import aware  # noqa: E402
from auth.routes import _event, _ip, _throttle  # noqa: E402
from config.database import SessionLocal, engine, get_db  # noqa: E402
from config.settings import settings  # noqa: E402
from consultations import (  # noqa: E402
    build_snapshot, doctor_details, ingest_page, mark_changed, owned_page, recompute,
)
from consultations import router as consultations_router  # noqa: E402
from env import api_key_configured  # noqa: E402
from models import (  # noqa: E402
    Consultation, ConsultationStatus, ConsultationVersion, Correction, CorrectionType,
    Prescription,
)
from admin import router as admin_router  # noqa: E402
from patient import router as patient_router  # noqa: E402
from pharmacy import router as pharmacy_router  # noqa: E402
from views import (  # noqa: E402
    UPLOAD_DIR, InteractionOutcome, image_response, PrescriptionOut, legacy_verification_id, page_out,
)
from vlm.prompts import PROMPT_VERSION  # noqa: E402
from vlm.spend import get_guard  # noqa: E402
from vlm.vocab import load_config  # noqa: E402

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="PrescriptAI API", version="2.0.0")

CSRF_HEADER, CSRF_VALUE = "x-requested-with", "PrescriptAI"


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    """The session is a cookie, so a state-changing request must also carry a
    custom header. A browser only lets another site send one after a CORS
    preflight, which the origin list below refuses — so a forged form post or
    cross-site fetch cannot ride the cookie."""
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
        return JSONResponse({"detail": "Missing request header."}, status_code=403)
    return await call_next(request)


# Security headers on every response (docs/UPDATES_SINCE_LOGIN.md §6). The CSP
# allows only this origin plus Google Fonts (index.html); inline styles are
# needed by React style props. No inline or third-party script runs.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; media-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if settings.COOKIE_SECURE:
        # Only once the site really is on HTTPS (COOKIE_SECURE=true): tells the
        # browser never to try plain http for this host again.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# Added after the CSRF guard so it wraps it: a refused request still carries
# the CORS headers, and the browser shows the real error instead of a CORS one.
app.add_middleware(
    CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # the PDF download's filename
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(consultations_router)
app.include_router(patient_router)
app.include_router(pharmacy_router)

doctor_only = require_vetted(Role.DOCTOR)


def _sms_channel() -> str:
    from auth import notify

    return notify.sms_channel()


def _backfill_consultations() -> None:
    """Pages written before consultations existed each become a one-page
    consultation. A page that was already confirmed becomes version 1 with the
    verification ID it was issued under, so a PDF downloaded then still verifies."""
    with SessionLocal() as db:
        rows = db.query(Prescription).filter(Prescription.consultation_id.is_(None)).all()
        for rx in rows:
            owner = db.get(User, rx.reviewed_by) if rx.reviewed_by else None
            if owner and owner.role != Role.DOCTOR:
                owner = None
            page = json.loads(rx.page_read_json)
            c = Consultation(doctor_id=owner.id if owner else None, patient_name=rx.patient_name,
                             patient_phone=rx.patient_phone, legacy=True,
                             created_at=rx.created_at, diagnosis=page.get("diagnosis"))
            db.add(c)
            db.flush()
            rx.consultation_id, rx.page_no = c.id, 1
            db.flush()
            db.refresh(c)
            result = recompute(db, c)
            if rx.reviewed_at and rx.patient_phone:
                at = aware(rx.reviewed_at)
                snap = build_snapshot(c, doctor_details(owner, None), result, 1, at)
                db.add(ConsultationVersion(
                    consultation_id=c.id, version=1, finalised_at=at, finalised_by=rx.reviewed_by,
                    patient_phone=rx.patient_phone, change_note=None,
                    verification_id=legacy_verification_id(rx.id, rx.reviewed_at),
                    snapshot_json=json.dumps(snap)))
                c.current_version, c.finalised_at, c.status = 1, at, ConsultationStatus.FINALISED
        if rows:
            db.commit()
            print(f"[DB] wrapped {len(rows)} page(s) from before consultations", flush=True)


@app.on_event("startup")
def _startup() -> None:
    from auth import notify, ratelimit
    from auth.crypto import encrypt_legacy_secrets, using_dedicated_key
    from config import migrate

    migrate.upgrade()
    missing = migrate.drift()
    if missing:
        print("[DB] WARNING: the models have changed without a migration. Missing in the database: "
              + ", ".join(missing) + ". Run:  cd backend && alembic revision --autogenerate -m \"...\"",
              flush=True)
    sealed = encrypt_legacy_secrets(engine)
    if sealed:
        print(f"[DB] encrypted {sealed} two-step secret(s) that were stored in the clear", flush=True)
    ratelimit.purge()

    pipeline.init()
    print(f"[API] reader={pipeline.reader_kind} (READER={pipeline.READER_SETTING}) "
          f"prompt={PROMPT_VERSION}", flush=True)
    _backfill_consultations()
    cfg = load_config()
    print(f"[API] vocabulary region={cfg.region} nlem={cfg.nlem_enabled}", flush=True)
    print(f"[API] phone codes travel by: {notify.sms_channel()}"
          + ("" if notify.sms_channel() == "sms" else
             "  (not a real text message: a 'verified phone' is not proof of the phone)"), flush=True)
    print(f"[API] vetting of doctors and pharmacies: {'required' if settings.REQUIRE_VETTING else 'OFF'}",
          flush=True)
    if "change-in-production" in settings.SECRET_KEY:
        print("[API] WARNING: SECRET_KEY is the built-in default. Set it in .env "
              "before anyone but a developer uses this.", flush=True)
    if not using_dedicated_key():
        print("[API] note: DATA_ENCRYPTION_KEY is not set, so two-step secrets are encrypted with a "
              "key derived from SECRET_KEY. Changing SECRET_KEY will then lock those users out.",
              flush=True)
    if not settings.REQUIRE_VETTING:
        print("[API] WARNING: REQUIRE_VETTING is off. Anyone who registers as a doctor can issue "
              "prescriptions. Development only.", flush=True)


class CorrectionIn(BaseModel):
    medicine_index: int = Field(description="-1 for page-level fields")
    field: str
    corrected_value: Optional[str] = None
    correction_type: CorrectionType = CorrectionType.OTHER
    note: Optional[str] = None
    corrected_by: str = Field("doctor", description="Ignored — taken from the signed-in user")


EDITABLE_FIELDS = {"brand", "dosage", "frequency", "duration", "instructions"}


# ─── routes ──────────────────────────────────────────────────────────────────


@app.get("/api/health")
def health():
    guard = get_guard()
    return {
        "status": "healthy", "version": "2.0.0", "reader": pipeline.reader_kind,
        # The UI shows a "demo reader" banner while this is true. Adding
        # ANTHROPIC_API_KEY to .env and restarting turns it off.
        "reader_is_mock": pipeline.is_mock(),
        "api_key_configured": api_key_configured(),
        # ok | auth_error | model_unavailable | unreachable | not_run (mock).
        # A free check made at startup, so a wrong key shows here at once.
        "reader_check": pipeline.reader_check,
        # ok | missing: the two medicine CSVs brand matching needs (README §9).
        "medicine_data": pipeline.medicine_data(),
        "prompt_version": PROMPT_VERSION,
        "spend_usd": round(guard.total_usd(), 4),
        "spend_cap_usd": guard.cap_usd,
        "spend_remaining_usd": round(guard.remaining_usd(), 4),
        # Where demo codes can be read (Mailpit), so the UI can link to it.
        # Unset outside a demo setup.
        "demo_inbox_url": os.environ.get("DEMO_INBOX_URL") or None,
        # sms | email | console: how a phone code really travels.
        "phone_codes_by": _sms_channel(),
        "vetting_required": settings.REQUIRE_VETTING,
    }


@app.post("/api/prescriptions/upload", response_model=PrescriptionOut)
async def upload_prescription(
    request: Request, image: UploadFile = File(...), db: Session = Depends(get_db),
    user: User = Depends(doctor_only),
):
    """One page with no patient details yet. Kept for scripts and old clients:
    it opens a consultation around the page, which the doctor completes on the
    review screen (POST /api/consultations is the normal path)."""
    profile = json.loads(user.profile_json or "{}")
    # The id is assigned here and nothing is flushed yet: a flush would take the
    # database's write lock, and the read-limit check inside ingest_page (which
    # writes on its own connection) would then wait on this very request.
    c = Consultation(id=str(uuid.uuid4()), doctor_id=user.id, clinic_address=profile.get("clinic_address"))
    db.add(c)
    rx = await ingest_page(db, image, c, user)
    _event(db, request, "page_uploaded", user.id, f"{c.id} page 1")
    db.commit()
    db.refresh(rx)
    return page_out(rx)


def _mine_q(db: Session, user: User):
    return (db.query(Prescription).join(Consultation, Prescription.consultation_id == Consultation.id)
            .filter(or_(Consultation.doctor_id == user.id, Consultation.doctor_id.is_(None))))


@app.get("/api/prescriptions", response_model=List[PrescriptionOut])
def list_prescriptions(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(doctor_only),
):
    rows = _mine_q(db, user).order_by(Prescription.created_at.desc()).offset(offset).limit(limit).all()
    return [page_out(r) for r in rows]


@app.get("/api/prescriptions/{prescription_id}", response_model=PrescriptionOut)
def get_prescription(
    prescription_id: str, db: Session = Depends(get_db), user: User = Depends(doctor_only)
):
    return page_out(owned_page(db, user, prescription_id))


@app.post("/api/prescriptions/{prescription_id}/corrections")
def add_correction(
    prescription_id: str, body: CorrectionIn, db: Session = Depends(get_db),
    user: User = Depends(doctor_only),
):
    """Record one doctor edit.

    This is the training corpus (§8.3) and the clinical audit trail. The
    predicted value is read from the stored result rather than trusted from the
    client, so before/after is always the model's actual output.

    A corrected NAME goes back through S3 (pipeline.reresolve): the old route
    stored the new brand beside the OLD composition, so the interaction check
    kept screening the drug the model misread.
    """
    rx = owned_page(db, user, prescription_id)
    if body.field not in EDITABLE_FIELDS:
        raise HTTPException(422, f"field must be one of: {', '.join(sorted(EDITABLE_FIELDS))}")
    meds = json.loads(rx.resolved_json)
    if not 0 <= body.medicine_index < len(meds):
        raise HTTPException(404, "Medicine not found")
    m = meds[body.medicine_index]

    c = Correction(
        prescription_id=rx.id, medicine_index=body.medicine_index,
        field=body.field, predicted_value=None if m.get(body.field) is None else str(m.get(body.field)),
        corrected_value=body.corrected_value,
        correction_type=body.correction_type, model_confidence=m.get("confidence"),
        match_score=m.get("match_score"), outcome=m.get("outcome"),
        verification=m.get("verification"),
        # Who made the edit comes from the session, not the request body: an
        # audit trail the client can write its own name into is not one.
        corrected_by=user.id, note=body.note,
    )
    db.add(c)

    if body.field == "brand":
        meds[body.medicine_index] = pipeline.reresolve(m, body.corrected_value)
    else:
        m[body.field] = (body.corrected_value or "").strip() or None
    rx.resolved_json = json.dumps(meds)
    consultation = rx.consultation
    recompute(db, consultation)
    mark_changed(consultation)
    db.commit()
    return {"ok": True, "correction_id": c.id, "medicine": meds[body.medicine_index]}


class InteractionCheckIn(BaseModel):
    medicines: List[str] = Field(description="Generic/molecule names")


@app.post("/api/interactions/check", response_model=InteractionOutcome)
def check_interactions(body: InteractionCheckIn):
    """Ad-hoc lookup against the 180-rule table.

    Takes GENERIC names — this path bypasses S2/S3, so the caller is asserting
    the names are correct. The prescription pipeline never uses it; it exists
    for the manual lookup screen.
    """
    if len([m for m in body.medicines if m.strip()]) < 2:
        return InteractionOutcome(medicines_checked=body.medicines)
    return InteractionOutcome(**pipeline.checker.check(body.medicines))


@app.get("/api/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    pages = _mine_q(db, user)
    total = pages.count()
    reviewed = pages.filter(Prescription.reviewed_at.isnot(None)).count()
    ids = [rx.id for rx in pages.all()]
    corrections = db.query(Correction).filter(Correction.prescription_id.in_(ids)).count() if ids else 0
    consultations = db.query(Consultation).filter(
        or_(Consultation.doctor_id == user.id, Consultation.doctor_id.is_(None)))
    meds = confirmed = 0
    for rx in pages.all():
        # Coverage measures the READER, so it counts what the model produced:
        # lines the doctor added or struck are excluded.
        rows = [m for m in json.loads(rx.resolved_json) if not m.get("doctor_entered")]
        meds += len(rows)
        confirmed += sum(1 for m in rows if m["outcome"] in ("confirmed", "probable"))
    guard = get_guard()
    return {
        "total_prescriptions": total,
        "consultations": consultations.count(),
        "finalised": consultations.filter(Consultation.current_version > 0).count(),
        "reviewed": reviewed,
        "total_medicines": meds,
        # Coverage is the honest headline number for a system that may abstain.
        "coverage": round(confirmed / meds, 3) if meds else 0.0,
        "corrections_logged": corrections,
        "spend_usd": round(guard.total_usd(), 4),
        "spend_remaining_usd": round(guard.remaining_usd(), 4),
    }


@app.get("/api/corrections")
def list_corrections(
    limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db),
    user: User = Depends(doctor_only),
):
    """The training corpus so far. Also how you check the log is complete
    before trusting any calibration run (§16.6 step 1)."""
    ids = [rx.id for rx in _mine_q(db, user).all()]
    rows = (db.query(Correction).filter(Correction.prescription_id.in_(ids))
            .order_by(Correction.created_at.desc()).limit(limit).all()) if ids else []
    return [
        {
            "id": c.id, "prescription_id": c.prescription_id,
            "medicine_index": c.medicine_index, "field": c.field,
            "predicted_value": c.predicted_value,
            "corrected_value": c.corrected_value,
            "correction_type": c.correction_type.value,
            "model_confidence": c.model_confidence, "match_score": c.match_score,
            "outcome": c.outcome, "verification": c.verification,
            "corrected_by": c.corrected_by,
            "created_at": c.created_at.isoformat() if c.created_at else "",
        }
        for c in rows
    ]



@app.get("/uploads/{name}")
def uploaded_image(name: str, db: Session = Depends(get_db), user: User = Depends(doctor_only)):
    """Prescription images are patient data: served only to the doctor whose
    consultation the page belongs to, never as an open static directory."""
    path = (UPLOAD_DIR / name).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "Not found")
    rx = db.query(Prescription).filter(Prescription.image_path.like(f"%{path.name}")).first()
    if not rx or not rx.consultation_id:
        raise HTTPException(404, "Not found")
    owned_page(db, user, rx.id)
    return image_response(path)


@app.get("/api/verify/{verification_id}")
def verify_document(verification_id: str, request: Request, db: Session = Depends(get_db)):
    """Public: what the QR on a prescription PDF opens.

    Answers only "is this a genuine prescription, from whom, and is it the
    current version". No patient name, no medicines, no diagnosis: anyone who
    holds the paper can scan it, so the answer must not be a copy of it."""
    _throttle("verify_ip", _ip(request), 30, 60)
    vid = verification_id.strip().upper()
    v = db.query(ConsultationVersion).filter(ConsultationVersion.verification_id == vid).first()
    if not v:
        return {"valid": False, "verification_id": vid}
    c = v.consultation
    snap = json.loads(v.snapshot_json)
    doc = snap.get("doctor") or {}
    latest = c.versions[-1] if c.versions else v
    return {
        "valid": True, "verification_id": vid, "version": v.version,
        "latest_version": latest.version, "superseded": latest.version > v.version,
        "finalised_at": aware(v.finalised_at).isoformat(),
        "doctor_name": doc.get("name"), "registration_no": doc.get("registration_no"),
        # Whether an administrator had reviewed the doctor's registration
        # details when this version was finalised. Not a register lookup.
        "doctor_vetted": bool(doc.get("vetted")),
        "speciality": doc.get("speciality"), "clinic_address": doc.get("clinic_address"),
        "medicine_count": len(snap.get("medicines", [])),
    }


# ─── the built frontend (production image only) ─────────────────────────────
# Serving the React build from this process puts the UI and the API on one
# origin, so the session cookie and the CSRF header need no CORS at all. In
# development WEB_DIST is unset and Vite serves the UI on :5173 as before.
# Registered last so it can never shadow an /api or /uploads route.

WEB_DIST = os.environ.get("WEB_DIST")
if WEB_DIST and (Path(WEB_DIST) / "index.html").is_file():
    from fastapi.staticfiles import StaticFiles

    _dist = Path(WEB_DIST).resolve()
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith(("api/", "uploads/")):
            raise HTTPException(404, "Not found")
        path = (_dist / full_path).resolve()
        if full_path and path.is_file() and _dist in path.parents:
            return FileResponse(path)  # favicon, robots.txt, other public/ files
        return FileResponse(_dist / "index.html")  # client-side routes


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
