"""
Persistence.  (docs/ARCHITECTURE_V2.md §8.3, §16.3)

Three tables. The second one is the important one.

`prescriptions` and `interactions` are ordinary application state. `corrections`
is the training corpus — every doctor edit is a labelled example on a real page,
in our exact target domain, produced as a byproduct of normal use. At ~100
prescriptions/week that is ~5,000 labelled pages a year, free. It is also the
clinical audit trail, which is why it is justified on today's merits alone and
not scaffolding for a future feature.

FIVE FIELDS THAT ARE UNRECOVERABLE IF NOT CAPTURED AT WRITE TIME (§16.3):

  1. `raw_response`      — the model's full reply, not just the parsed PageRead.
                           Without it, T2 preference pairs cannot be built.
  2. `model` / `prompt_version` / `effort` — without these, no correction is
                           attributable to a configuration and every later
                           comparison is uninterpretable.
  3. `page_read_json`    — carries per-line `confidence` and `alternatives`,
                           which are the input features for T0 threshold
                           calibration.
  4. before AND after    — `predicted_value` and `corrected_value`, never a diff.
                           The model's wrong answer is as informative as the
                           right one.
  5. `correction_type`   — misread / hallucinated / missed / wrong_field. This is
                           the reward signal, and it costs one dropdown.

A page a doctor passes UNCHANGED is also a label — the majority class — so
confirmations are recorded too, via `Prescription.reviewed_at` (set on every page
when its consultation is finalised).

Workflow.md §7 adds the consultation layer on top:

  Consultation         one doctor, one patient, one prescription of N pages
  ConsultationVersion  an IMMUTABLE snapshot, written at each finalise. Patients
                       and pharmacies only ever read these, so an edit after
                       finalising cannot reach them until it is finalised as
                       version 2 ("revised").
  AccessGrant          one patient-issued OTP -> one pharmacy, one prescription,
                       a short window. Expiry is checked on every request.
  DispenseCheck        one pack checked against the prescription (stage 6).
  DispensingRecord     the minimal record a pharmacy keeps after access ends
                       (stage 7): no phone, no image, no diagnosis.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from config.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CorrectionType(str, enum.Enum):
    """Why the doctor changed something. The reward signal for T0/T2."""

    MISREAD = "misread"            # model read the wrong characters
    HALLUCINATED = "hallucinated"  # model reported something not on the page
    MISSED = "missed"              # model failed to report something present
    WRONG_FIELD = "wrong_field"    # right drug, wrong dosage/frequency/duration
    OTHER = "other"


class Prescription(Base):
    __tablename__ = "prescriptions"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    image_path = Column(String, nullable=False)
    image_sha256 = Column(String, index=True)

    # Configuration provenance — field 2. Without these the row is unusable
    # for any later comparison.
    model = Column(String, nullable=False)
    prompt_version = Column(String, nullable=False)
    effort = Column(String)

    # Field 1 and 3: the full raw reply and the parsed read, both kept.
    raw_response = Column(Text)
    page_read_json = Column(Text, nullable=False)
    resolved_json = Column(Text, nullable=False)

    document_type = Column(String)
    overall_legibility = Column(Float)
    patient_name = Column(String)
    prescriber_name = Column(String)
    prescribed_date = Column(String)

    cost_usd = Column(Float, default=0.0)
    from_cache = Column(Boolean, default=False)

    # A page confirmed unchanged is a fully labelled page — the majority class,
    # and worthless to a later training run if we only ever store edits.
    reviewed_at = Column(DateTime(timezone=True))
    reviewed_by = Column(String)

    # Who the doctor addressed this to (E.164). A patient sees the prescription
    # once it is reviewed AND this matches their verified phone (patient.py).
    # Stands in for Workflow stage 1's session until sessions exist.
    patient_phone = Column(String, index=True)

    # Workflow stage 1: every page belongs to a consultation. Nullable only for
    # rows written before consultations existed; startup backfills them.
    consultation_id = Column(String, ForeignKey("consultations.id", ondelete="CASCADE"), index=True)
    page_no = Column(Integer)

    consultation = relationship("Consultation", back_populates="pages")
    corrections = relationship(
        "Correction", back_populates="prescription", cascade="all, delete-orphan"
    )
    interaction_runs = relationship(
        "InteractionRun", back_populates="prescription", cascade="all, delete-orphan"
    )


class Correction(Base):
    """One doctor edit. The unit of the training corpus."""

    __tablename__ = "corrections"

    id = Column(String, primary_key=True, default=_uuid)
    prescription_id = Column(
        String, ForeignKey("prescriptions.id", ondelete="CASCADE"), index=True
    )
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    medicine_index = Column(Integer)          # -1 for page-level fields
    field = Column(String, nullable=False)    # brand | dosage | frequency | ...

    # Field 4: full before AND after, never a diff.
    predicted_value = Column(Text)
    corrected_value = Column(Text)

    # Field 5: the reward signal.
    correction_type = Column(
        Enum(CorrectionType), default=CorrectionType.OTHER, nullable=False
    )

    # Snapshot of what the model believed at the time, so a later calibration
    # run does not have to re-derive it from the page JSON.
    model_confidence = Column(Float)
    match_score = Column(Float)
    outcome = Column(String)
    verification = Column(String)

    corrected_by = Column(String, default="doctor")
    note = Column(Text)

    prescription = relationship("Prescription", back_populates="corrections")


class InteractionRun(Base):
    """Result of one safety check. Kept for audit, not for training.

    Per page (prescription_id) for the page's own view, and per consultation
    (consultation_id) across EVERY page — a page-1 drug and a page-2 drug can
    interact, so the consultation run is the one that counts."""

    __tablename__ = "interaction_runs"

    id = Column(String, primary_key=True, default=_uuid)
    prescription_id = Column(
        String, ForeignKey("prescriptions.id", ondelete="CASCADE"), index=True
    )
    consultation_id = Column(
        String, ForeignKey("consultations.id", ondelete="CASCADE"), index=True
    )
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    overall_risk = Column(String)
    total_count = Column(Integer, default=0)
    result_json = Column(Text, nullable=False)

    prescription = relationship("Prescription", back_populates="interaction_runs")


# ─── Workflow stage 1 / 3: consultations and immutable versions ─────────────


class ConsultationStatus(str, enum.Enum):
    DRAFT = "draft"          # never finalised: nothing has reached anyone
    FINALISED = "finalised"  # working copy == latest version
    REVISING = "revising"    # edited since the last finalise; patients still see that version


class DiagnosisVisibility(str, enum.Enum):
    """Workflow §10 Q6. Default: the patient sees it, the pharmacy does not."""

    PATIENT = "patient"
    PATIENT_AND_PHARMACY = "patient_and_pharmacy"
    HIDDEN = "hidden"


class Consultation(Base):
    __tablename__ = "consultations"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    # The owning doctor. Doctors see only their own consultations (open item 10).
    # NULL only on rows backfilled from before accounts recorded an owner.
    doctor_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True)

    patient_name = Column(String)
    patient_phone = Column(String, index=True)   # E.164; the patient's identity
    clinic_address = Column(String)              # which of the doctor's clinics

    diagnosis = Column(Text)
    diagnosis_visibility = Column(
        Enum(DiagnosisVisibility), default=DiagnosisVisibility.PATIENT, nullable=False
    )
    # Workflow §10 Q4: a "do not substitute" prescription blocks a different
    # brand at the pharmacy even when the composition matches.
    allow_substitution = Column(Boolean, default=True, nullable=False)

    status = Column(Enum(ConsultationStatus), default=ConsultationStatus.DRAFT, nullable=False)
    current_version = Column(Integer, default=0, nullable=False)
    finalised_at = Column(DateTime(timezone=True))
    legacy = Column(Boolean, default=False)      # backfilled from a single page

    pages = relationship("Prescription", back_populates="consultation",
                         order_by="Prescription.page_no")
    versions = relationship("ConsultationVersion", back_populates="consultation",
                            order_by="ConsultationVersion.version",
                            cascade="all, delete-orphan")
    interaction_runs = relationship("InteractionRun", order_by="InteractionRun.created_at",
                                    primaryjoin="Consultation.id == InteractionRun.consultation_id",
                                    viewonly=True)


class ConsultationVersion(Base):
    """What a patient or pharmacy sees. Written once at finalise, never updated."""

    __tablename__ = "consultation_versions"

    id = Column(String, primary_key=True, default=_uuid)
    consultation_id = Column(String, ForeignKey("consultations.id", ondelete="CASCADE"),
                             index=True, nullable=False)
    version = Column(Integer, nullable=False)
    finalised_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    finalised_by = Column(String)
    change_note = Column(Text)
    # Who this version is addressed to — the patient access check reads this,
    # not the working copy, so re-addressing takes effect only when finalised.
    patient_phone = Column(String, index=True)
    # Printed on the PDF and encoded in its QR; looked up by /api/verify.
    verification_id = Column(String, unique=True, index=True, nullable=False)
    snapshot_json = Column(Text, nullable=False)

    consultation = relationship("Consultation", back_populates="versions")


# ─── Workflow stages 5–7: pharmacy access, dispensing ───────────────────────


class AccessGrant(Base):
    """One OTP the patient issued for one prescription.

    Lifecycle (Workflow §6): issued -> active (redeemed) -> expired | revoked;
    or issued -> lapsed (not redeemed in time) | cancelled (a newer OTP replaced it).
    The status is computed from the timestamps on every request (pharmacy.py),
    so expiry needs no background job and cannot be missed.
    """

    __tablename__ = "access_grants"

    id = Column(String, primary_key=True, default=_uuid)
    consultation_id = Column(String, ForeignKey("consultations.id", ondelete="CASCADE"),
                             index=True, nullable=False)
    patient_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pharmacy_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True)

    otp_hash = Column(String, index=True, nullable=False)   # HMAC, never the code
    issued_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    redeem_by = Column(DateTime(timezone=True), nullable=False)
    redeemed_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))

    consultation = relationship("Consultation")
    checks = relationship("DispenseCheck", back_populates="grant",
                          order_by="DispenseCheck.created_at", cascade="all, delete-orphan")


class DispenseCheck(Base):
    """One pack checked at the counter (stage 6). The pack photo is NOT kept."""

    __tablename__ = "dispense_checks"

    id = Column(String, primary_key=True, default=_uuid)
    grant_id = Column(String, ForeignKey("access_grants.id", ondelete="CASCADE"), index=True)
    consultation_id = Column(String, ForeignKey("consultations.id", ondelete="CASCADE"), index=True)
    version = Column(Integer, nullable=False)
    pharmacy_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    method = Column(String, nullable=False)        # qr | photo | manual
    pack_json = Column(Text, nullable=False)       # what was scanned / read / typed
    result = Column(String, nullable=False)        # dispense.compare.RESULTS
    result_json = Column(Text, nullable=False)
    medicine_index = Column(Integer)               # into the version's medicines
    blocked = Column(Boolean, default=False, nullable=False)
    needs_confirmation = Column(Boolean, default=False, nullable=False)
    chemist_confirmed_at = Column(DateTime(timezone=True))
    quantity = Column(String)
    dispensed = Column(Boolean, default=False, nullable=False)

    grant = relationship("AccessGrant", back_populates="checks")


class DispensingRecord(Base):
    """Stage 7: what the pharmacy keeps after access ends — and nothing more.

    Date, prescription ID, prescriber + registration no., what was dispensed
    (brand, batch, quantity) and the verification result. No patient phone, no
    full prescription, no diagnosis, no image (Workflow §4 stage 7, §8.5)."""

    __tablename__ = "dispensing_records"

    id = Column(String, primary_key=True, default=_uuid)
    pharmacy_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    check_id = Column(String, ForeignKey("dispense_checks.id", ondelete="SET NULL"))
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    verification_id = Column(String, nullable=False)
    prescriber_name = Column(String)
    prescriber_registration_no = Column(String)
    brand = Column(String)
    composition = Column(String)
    strength = Column(String)
    batch = Column(String)
    expiry = Column(String)
    quantity = Column(String)
    result = Column(String, nullable=False)
