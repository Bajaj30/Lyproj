"""
Pack-label reader — Workflow.md stage 6, "photograph the pack".

At the pharmacy counter, a pack without a usable QR code is photographed and the
same VLM reads the printed label. What comes back goes through the existing S2/S3
lookup (brand -> composition) in `dispense/compare.py`, so a pack is compared to
the prescription on COMPOSITION AND STRENGTH, never on the brand string.

Same shape as the page reader:

  MockPackReader    — fixed output, no key, no cost. Every result it produces is
                      labelled `mock` all the way to the screen, because a demo
                      reading presented as a real one is exactly the fabricated
                      result this project refuses to show.
  ClaudePackReader  — the real one. Used automatically once ANTHROPIC_API_KEY is
                      set (READER=auto).

The verbatim rule applies here too: a pack label is printed, so it is usually
clear, but a glare-damaged strength must come back as null, not as a plausible
number. A wrong strength is the one error this check exists to catch.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env import load_env  # noqa: E402
from ingest.normalise import normalise  # noqa: E402
from vlm.page_reader import DEFAULT_MODEL, resolve_reader_kind  # noqa: E402
from vlm.spend import get_guard, price_call  # noqa: E402

PACK_PROMPT_VERSION = "pack-v1"


class PackLabel(BaseModel):
    """What is printed on one medicine pack, verbatim."""

    legible: bool = Field(description="False when the label cannot be read at all")
    brand: Optional[str] = Field(default=None, description="Brand name exactly as printed")
    generic_name: Optional[str] = Field(
        default=None, description="Printed composition / generic name, e.g. 'Paracetamol IP 650 mg'"
    )
    strength: Optional[str] = Field(
        default=None, description="Strength exactly as printed, e.g. '650 mg' or '200 mg/500 mg'"
    )
    form: Optional[str] = Field(default=None, description="Tablet, capsule, syrup, injection, ...")
    manufacturer: Optional[str] = None
    batch: Optional[str] = None
    expiry: Optional[str] = Field(default=None, description="Expiry exactly as printed, e.g. '08/2027'")
    confidence: float = Field(ge=0.0, le=1.0)
    notes: Optional[str] = Field(default=None, description="Anything obscured, torn or glared")


PACK_SYSTEM_PROMPT = """\
You read photographs of medicine packs (strips, cartons, bottles) at a pharmacy \
counter. A pharmacist uses your reading to check the pack against a doctor's \
prescription before handing it to a patient.

Transcribe what is PRINTED, exactly. Do not correct spellings and do not complete a \
partly hidden word. If the strength is not clearly visible, leave `strength` null: a \
guessed strength is the most dangerous error you can make here, because a wrong \
strength is exactly what this check exists to catch. The same applies to the brand.

- `brand` — the product's brand name as printed, without the strength.
- `generic_name` — the printed composition line, verbatim.
- `strength` — as printed. For combinations, every component, e.g. "200 mg/500 mg".
- `form` — tablet, capsule, syrup, suspension, injection, cream, drops, inhaler...
- `batch`, `expiry` — as printed ("B.No.", "Exp.").
- `confidence` — your honest confidence in the brand AND strength together.
- `legible: false` if you cannot read the label at all; say why in `notes`.
"""


class MockPackReader:
    """Fixed reading, matching the first medicine of the mock prescription so the
    whole dispensing flow can be exercised without a key. Labelled `mock`."""

    model = "mock-pack"
    is_mock = True

    def read(self, image: bytes) -> PackLabel:
        normalise(image)  # exercise the real S0 path, as the page mock does
        return PackLabel(
            legible=True, brand="Oflazest OZ", generic_name="Ofloxacin & Ornidazole Tablets IP",
            strength=None, form="Tablet", manufacturer=None, batch="MOCK-0001",
            expiry=None, confidence=0.9,
            notes="Demo reader: this is a fixed reading, not a reading of your photo.",
        )


class ClaudePackReader:
    is_mock = False

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "low"):
        import anthropic

        load_env()
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.last_raw_response: Optional[dict] = None

    def read(self, image: bytes) -> PackLabel:
        img = normalise(image)
        guard = get_guard()
        guard.check_before_call()
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=[{"type": "text", "text": PACK_SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(img.jpeg_bytes).decode(),
                    }},
                    {"type": "text", "text": "Read this pack."},
                ],
            }],
            output_format=PackLabel,
        )
        self.last_raw_response = response.model_dump(mode="json", warnings=False)
        guard.record(price_call(self.model, response.usage), tag=f"pack-{img.sha256[:8]}")
        return response.parsed_output


def build_pack_reader(kind: str = "auto"):
    kind = resolve_reader_kind(kind)
    if kind == "claude":
        # Printed labels need far less reasoning than handwriting: effort low.
        return ClaudePackReader(model=DEFAULT_MODEL, effort="low")
    return MockPackReader()
