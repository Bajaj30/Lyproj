"""
Response shapes shared by the doctor routes (main.py, consultations.py), and the
helpers that build them. Patient and pharmacy views live in their own modules:
they are deliberately narrower than the doctor's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from auth.models import aware
from config.settings import settings

# PRESCRIPTAI_UPLOAD_DIR moves the stored page images, e.g. onto a mounted
# volume, or into a temp directory so a test run never touches real ones.
UPLOAD_DIR = Path(os.environ.get("PRESCRIPTAI_UPLOAD_DIR")
                  or Path(__file__).resolve().parent / "uploads").resolve()

# Stored extension -> served type. The extension is chosen from the image's real
# format at upload (consultations.ingest_page), never from the client's filename,
# and anything not in this map is served as a download, never rendered. This
# closes the stored-XSS hole in docs/UPDATES_SINCE_LOGIN.md §6, where a JPEG named
# rx.html came back as text/html from the site's own origin.
IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
               "tif": "image/tiff", "tiff": "image/tiff"}
PIL_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp",
               "TIFF": "tif", "MPO": "jpg"}


def image_response(path: Path):
    """Serve a stored prescription image safely: a fixed image type, no sniffing,
    and a sandboxing CSP in case anything ever renders it as a document."""
    from fastapi.responses import FileResponse

    ext = path.suffix.lower().lstrip(".")
    return FileResponse(
        path, media_type=IMAGE_TYPES.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox",
                 **({} if ext in IMAGE_TYPES else {"Content-Disposition": "attachment"})},
    )

DISCLAIMER = (
    "AI-extracted. Verify against the original prescription before dispensing — "
    "this can be wrong. Only ingredients on the NLEM 2022 essential-medicines "
    "list are checked for interactions; anything else is listed under 'skipped'."
)


class MedicineOut(BaseModel):
    index: int
    brand: Optional[str] = None
    generic: Optional[str] = None
    ingredients: List[str] = []
    verified_ingredients: List[str] = []
    unverified_ingredients: List[str] = []
    verification: str
    outcome: str
    confidence: float
    match_score: Optional[float] = None
    raw_reading: str
    matched_via: Optional[str] = None
    candidates: List[str] = []
    dosage: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    instructions: Optional[str] = None
    bbox: Optional[List[float]] = None
    needs_hard_confirmation: bool
    # Workflow stage 3: the doctor can strike a line the model invented
    # (`removed`, kept so the correction log can point at it) and add one it missed.
    removed: bool = False
    doctor_entered: bool = False
    page_no: Optional[int] = None
    prescription_id: Optional[str] = None


class InteractionOut(BaseModel):
    drug_a: str
    drug_b: str
    severity: str
    severity_color: str
    effect: str = ""
    mechanism: str = ""
    safer_alternative: str = ""
    reference: str = ""


class InteractionOutcome(BaseModel):
    interactions: List[InteractionOut] = []
    total_count: int = 0
    overall_risk: str = "none"
    medicines_checked: List[str] = []
    skipped: List[str] = Field(
        default=[], description="Excluded from the safety check, with reasons. Never hidden."
    )


class PrescriptionOut(BaseModel):
    """One PAGE. Kept under its historical name: the table is `prescriptions`."""

    id: str
    created_at: str
    image_url: str
    document_type: str
    overall_legibility: float
    patient_name: Optional[str] = None
    prescriber_name: Optional[str] = None
    date: Optional[str] = None
    diagnosis: Optional[str] = None
    unreadable_regions: List[str] = []
    medicines: List[MedicineOut] = []
    interactions: InteractionOutcome
    reviewed: bool = False
    patient_phone: Optional[str] = None
    consultation_id: Optional[str] = None
    page_no: Optional[int] = None
    disclaimer: str = DISCLAIMER


def interaction_out(result: Optional[dict]) -> InteractionOutcome:
    return InteractionOutcome(**result) if result else InteractionOutcome()


def page_out(rx) -> PrescriptionOut:
    page = json.loads(rx.page_read_json)
    meds = json.loads(rx.resolved_json)
    run = rx.interaction_runs[-1] if rx.interaction_runs else None
    return PrescriptionOut(
        id=rx.id,
        created_at=rx.created_at.isoformat() if rx.created_at else "",
        image_url=f"/uploads/{Path(rx.image_path).name}",
        document_type=rx.document_type or "prescription",
        overall_legibility=rx.overall_legibility or 0.0,
        patient_name=rx.patient_name, prescriber_name=rx.prescriber_name,
        date=rx.prescribed_date, diagnosis=page.get("diagnosis"),
        unreadable_regions=page.get("unreadable_regions", []),
        medicines=[MedicineOut(**{**m, "page_no": rx.page_no, "prescription_id": rx.id})
                   for m in meds],
        interactions=interaction_out(json.loads(run.result_json) if run else None),
        reviewed=rx.reviewed_at is not None,
        patient_phone=rx.patient_phone,
        consultation_id=rx.consultation_id, page_no=rx.page_no,
    )


def make_verification_id(consultation_id: str, version: int, finalised_at) -> str:
    """Short code tying a document to ONE version of one prescription. Keyed by
    SECRET_KEY, so it cannot be computed without the server; printed on the PDF
    and encoded in its QR, and looked up by GET /api/verify/{id}."""
    msg = f"consult:{consultation_id}:v{version}:{aware(finalised_at).isoformat(timespec='seconds')}"
    return _format_vid(msg)


def legacy_verification_id(prescription_id: str, reviewed_at) -> str:
    """The Session 8 formula (one page, keyed on its review time). Backfilled
    versions keep it, so a PDF downloaded before consultations existed still
    verifies."""
    return _format_vid(f"rx:{prescription_id}:{aware(reviewed_at).isoformat(timespec='seconds')}")


def _format_vid(msg: str) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    code = base64.b32encode(digest).decode()[:10]
    return f"PRX-{code[:5]}-{code[5:]}"
