"""
The downloadable prescription PDF (Workflow.md stage 4).

Renders a `patient.PatientPrescription` — the same view the patient app shows —
so the file and the screen cannot say different things. Contents, in order:
doctor details, patient and date, diagnosis (unless the doctor hid it), medicines,
interactions (with what was not checked), the AI/doctor-review notice, a QR code
that opens the public verification page, and every original handwritten page.

The QR encodes PUBLIC_BASE_URL/check/<verification ID>. That page answers only
"genuine, by whom, current version or superseded" — never the contents.

Fonts: the PDF core fonts are Latin-1 only, so a dash, a curly quote or a
Devanagari name would break them. A Unicode TTF is used when one is installed
(DejaVu in the Docker image, Arial on Windows); otherwise text is reduced to
Latin-1 rather than failing the download.
"""

from __future__ import annotations

import io
import os
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from PIL import Image

CARE = (31, 131, 117)      # tailwind care-500: the one accent
VITAL = (196, 53, 58)      # vital-500: actual risk only
INK = (34, 43, 40)
MUTED = (98, 110, 105)
RULE = (214, 219, 214)

# Dates and times print in the patient's zone, labelled. The server runs in UTC,
# and an unlabelled UTC time is 5h30m wrong for the target region.
DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Kolkata"))

SEVERITY_LABEL = {"severe": "Serious", "moderate": "Moderate", "minor": "Minor"}

_FONT_CANDIDATES = [
    (os.environ.get("PDF_FONT_REGULAR"), os.environ.get("PDF_FONT_BOLD")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]


def _unicode_font() -> Optional[tuple]:
    for regular, bold in _FONT_CANDIDATES:
        if regular and bold and Path(regular).is_file() and Path(bold).is_file():
            return regular, bold
    return None


_LATIN1 = str.maketrans({"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
                         "…": "...", "•": "-", "₹": "Rs.", "→": "->", "·": "-"})


class _Doc(FPDF):
    def __init__(self, verification_id: str, generated_at: datetime):
        super().__init__(format="A4")
        self.vid, self.generated_at = verification_id, generated_at.astimezone(DISPLAY_TZ)
        fonts = _unicode_font()
        if fonts:
            self.add_font("Body", "", fonts[0])
            self.add_font("Body", "B", fonts[1])
            self.family_name, self.unicode = "Body", True
        else:
            self.family_name, self.unicode = "Helvetica", False
        self.set_margins(18, 18, 18)
        self.set_auto_page_break(True, margin=20)

    def t(self, s) -> str:
        s = "" if s is None else str(s)
        if self.unicode:
            return s
        s = unicodedata.normalize("NFKD", s.translate(_LATIN1))
        return s.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=INK):
        self.set_font(self.family_name, "B" if bold else "", size)
        self.set_text_color(*color)

    def header(self):
        self.font(15, bold=True, color=CARE)
        self.cell(0, 8, self.t("℞ PrescriptAI") if self.unicode else "Rx PrescriptAI",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.font(8.5, color=MUTED)
        self.cell(0, 4.5, self.t(f"Digital copy of a doctor-reviewed prescription  ·  Verification ID {self.vid}"),
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*RULE)
        self.line(self.l_margin, self.get_y() + 2, self.w - self.r_margin, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        self.set_y(-14)
        self.font(7.5, color=MUTED)
        self.cell(0, 4, self.t(
            f"{self.vid}  ·  generated {self.generated_at:%d %b %Y %H:%M %Z}  ·  page {self.page_no()}/{{nb}}"),
            align="C")

    # building blocks

    def heading(self, text: str):
        self.ln(2)
        self.font(11.5, bold=True, color=CARE)
        self.cell(0, 7, self.t(text.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def para(self, text: str, size=10, bold=False, color=INK, h=5):
        self.font(size, bold, color)
        self.multi_cell(0, h, self.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def field(self, label: str, value: Optional[str]):
        if not value:
            return
        self.font(9, color=MUTED)
        self.cell(34, 5, self.t(label))
        self.font(10)
        self.multi_cell(0, 5, self.t(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _qr_png(url: str) -> Optional[io.BytesIO]:
    try:
        import segno
    except ImportError:   # the ID is still printed; only the QR is missing
        return None
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)
    buf.seek(0)
    return buf


def render(view, images: Sequence[Path], generated_at: datetime,
           verify_url: Optional[str] = None) -> bytes:
    pdf = _Doc(view.verification_id, generated_at)
    pdf.alias_nb_pages()
    pdf.add_page()

    if verify_url:
        qr = _qr_png(verify_url)
        if qr:
            size, top = 30, pdf.get_y()
            pdf.image(qr, x=pdf.w - pdf.r_margin - size, y=top - 2, w=size, h=size)
            pdf.font(7, color=MUTED)
            pdf.set_xy(pdf.w - pdf.r_margin - size - 6, top + size - 1)
            pdf.cell(size + 6, 3.5, pdf.t("Scan to verify"), align="C")
            pdf.set_xy(pdf.l_margin, top)
            pdf.set_right_margin(pdf.r_margin + size + 4)

    if getattr(view, "revised", False):
        pdf.para(f"Version {view.version}: revised by the doctor. Earlier versions no longer apply.",
                 size=10, bold=True, color=VITAL)
        if getattr(view, "change_note", None):
            pdf.para(f"What changed: {view.change_note}", size=9.5, color=MUTED)

    # doctor + patient
    pdf.heading("Prescribed by")
    if view.doctor:
        pdf.para(view.doctor.name, size=12, bold=True)
        pdf.field("Speciality", view.doctor.speciality)
        pdf.field("Clinic", view.doctor.clinic_address)
        pdf.field("Registration no.", view.doctor.registration_no)
    if view.prescriber_as_written and (not view.doctor or view.prescriber_as_written != view.doctor.name):
        pdf.field("Written on page", view.prescriber_as_written)
    reviewed = datetime.fromisoformat(view.finalised_at).astimezone(DISPLAY_TZ)
    pdf.field("Finalised", f"{reviewed:%d %b %Y}, by the doctor above (version {view.version})")

    pdf.heading("Patient")
    pdf.field("Name", view.patient_name or "Not written on the page")
    pdf.field("Date on page", view.date)
    pdf.set_right_margin(18)   # back to full width below the QR
    if getattr(view, "diagnosis", None):
        pdf.field("Diagnosis", view.diagnosis)

    # medicines
    pdf.heading(f"Medicines ({len(view.medicines)})")
    if not view.medicines:
        pdf.para("No medicines were recorded on this prescription.", color=MUTED)
    for i, m in enumerate(view.medicines, 1):
        pdf.ln(1)
        if m.name:
            pdf.para(f"{i}. {m.name}", size=11, bold=True)
        else:
            pdf.para(f"{i}. Could not be read reliably. Ask your doctor or pharmacist.",
                     size=11, bold=True, color=VITAL)
        if m.as_written:
            pdf.field("As written", f"“{m.as_written}”" if pdf.unicode else f'"{m.as_written}"')
        pdf.field("Contains", " + ".join(m.contains))
        pdf.field("Dose", m.dosage)
        pdf.field("How often", m.frequency)
        pdf.field("For how long", m.duration)
        pdf.field("Instructions", m.instructions)

    # interactions
    pdf.heading("Medicines taken together")
    if view.interactions:
        for it in view.interactions:
            colour = VITAL if it.severity == "severe" else INK
            pdf.para(f"{SEVERITY_LABEL.get(it.severity, it.severity.title())}: "
                     f"{it.drug_a} with {it.drug_b}", bold=True, color=colour)
            if it.effect:
                pdf.para(it.effect)
            pdf.para(it.advice, size=9, color=MUTED)
            pdf.ln(1)
    else:
        pdf.para("No known interactions were found among the medicines that could be checked.")
    if view.not_checked:
        pdf.ln(1)
        pdf.para("Not checked for interactions:", size=9.5, bold=True, color=MUTED)
        for s in view.not_checked:
            pdf.para(f"·  {s}" if pdf.unicode else f"-  {s}", size=9.5, color=MUTED)

    # notice
    pdf.ln(3)
    pdf.set_fill_color(240, 247, 245)
    pdf.font(9)
    pdf.multi_cell(0, 4.8, pdf.t(view.notice), fill=True, padding=3,
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # every original page, each on its own page and scaled to fit
    for n, image_path in enumerate(images, 1):
        pdf.add_page()
        pdf.heading("Original handwritten prescription"
                    + (f" (page {n} of {len(images)})" if len(images) > 1 else ""))
        img = Image.open(image_path)
        img.load()
        img = img.convert("RGB")
        img.thumbnail((1800, 1800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        max_w, max_h = pdf.epw, pdf.h - pdf.get_y() - 24
        scale = min(max_w / img.width, max_h / img.height)
        w, h = img.width * scale, img.height * scale
        x, y = pdf.l_margin + (max_w - w) / 2, pdf.get_y()
        pdf.image(buf, x=x, y=y, w=w, h=h)
        pdf.set_draw_color(*RULE)   # a pale page photo needs a visible edge
        pdf.rect(x, y, w, h)

    return bytes(pdf.output())
