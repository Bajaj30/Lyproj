"""
The pipeline, held once per process, for every router that needs it.

    read_page()     S0 -> S1 -> S2 -> S3 on one uploaded page
    check()         S4 over a list of stored medicine dicts (any number of pages)
    reresolve()     S3 again after a doctor corrects a medicine name
    pack_reader     stage 6, reading a photographed pack

READER selects the reader. The default, "auto", uses Claude when
ANTHROPIC_API_KEY is set in .env and the mock reader otherwise, so the product
runs end to end with no key and starts making real reads the moment one is added.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from ingest.normalise import normalise
from schemas import Outcome, ResolvedMedicine, VerificationTier
from vlm.page_reader import CachedPageReader, build_reader, resolve_reader_kind
from vlm.prompts import PROMPT_VERSION
from vlm.vocab import _describe, _describe_ingredient, load_config, resolve_page

READER_SETTING = os.environ.get("READER", "auto")

reader = None
checker = None
pack_reader = None
reader_kind = "unset"


def init() -> None:
    global reader, checker, pack_reader, reader_kind
    from drug_interaction.interaction_inference import InteractionChecker
    from vlm.pack_reader import build_pack_reader

    reader_kind = resolve_reader_kind(READER_SETTING)
    reader = build_reader(reader_kind)
    pack_reader = build_pack_reader(reader_kind if reader_kind in ("claude", "mock") else "mock")
    checker = InteractionChecker()
    if reader_kind.startswith("mock"):
        print("[API] READER is the MOCK: every page reads as the same demo prescription. "
              "Add ANTHROPIC_API_KEY to .env and restart to read real pages.", flush=True)


def is_mock() -> bool:
    return reader_kind.startswith("mock")


def read_page(contents: bytes):
    """S0-S3. Returns (page, resolved medicine dicts, normalised image, provenance)."""
    norm = normalise(contents)                 # S0
    page = reader.read(norm)                   # S1
    meds = resolve_page(page)                  # S2 + S3
    inner = getattr(reader, "inner", reader)
    cost = getattr(inner, "last_cost", None)
    provenance = dict(
        model=reader.model, prompt_version=PROMPT_VERSION,
        effort=getattr(inner, "effort", None),
        raw_response=json.dumps(getattr(inner, "last_raw_response", None)),
        cost_usd=cost.usd if cost else 0.0,
        from_cache=isinstance(reader, CachedPageReader) and cost is None,
    )
    return page, meds, norm, provenance


def med_dict(i: int, m: ResolvedMedicine) -> dict:
    return dict(
        index=i, brand=m.brand, generic=m.generic, ingredients=m.ingredients,
        verified_ingredients=m.verified_ingredients,
        unverified_ingredients=m.unverified_ingredients,
        verification=m.verification.value, outcome=m.outcome.value,
        confidence=m.confidence, match_score=m.match_score,
        raw_reading=m.raw_reading, matched_via=m.matched_via,
        candidates=m.candidates, dosage=m.dosage, frequency=m.frequency,
        duration=m.duration, instructions=m.instructions, bbox=m.bbox,
        needs_hard_confirmation=m.needs_hard_confirmation,
        removed=False, doctor_entered=False,
    )


def _as_resolved(d: dict) -> ResolvedMedicine:
    return ResolvedMedicine(**{k: v for k, v in d.items() if k in ResolvedMedicine.model_fields})


def check(meds: List[dict]) -> dict:
    """S4 across every live medicine given — one check for the whole consultation."""
    live = [_as_resolved(m) for m in meds if not m.get("removed")]
    return checker.check(live)


def _needs_hard(d: dict) -> bool:
    return (d.get("verification") in (VerificationTier.UNVERIFIED.value, VerificationTier.UNKNOWN.value)
            or d.get("outcome") in (Outcome.AMBIGUOUS.value, Outcome.UNMATCHED.value))


def reresolve(d: dict, name: Optional[str]) -> dict:
    """A doctor typed a medicine name (correction, ambiguity pick, or a new line).

    The old route stored the new brand and left the old composition beside it, so
    a corrected name still carried the WRONG ingredients into the interaction
    check. Now the name goes back through S3: brand -> composition -> NLEM. The
    doctor is the authority on what was written, so the outcome is CONFIRMED when
    the name is in the database and UNMATCHED when it is not; the verification
    tier is recomputed honestly either way. `raw_reading` is never touched.
    """
    from normalise.india_medicines import get_medicines

    t = load_config()
    med = get_medicines()
    name = (name or "").strip()
    d = dict(d)
    d.update(candidates=[], match_score=None, matched_via=None, doctor_entered=True)
    if not name:
        d.update(brand=None, generic=None, ingredients=[], verified_ingredients=[],
                 unverified_ingredients=[], verification=VerificationTier.UNKNOWN.value,
                 outcome=Outcome.ILLEGIBLE.value)
    elif med.lookup(name):
        info = med.lookup(name)
        desc = _describe(info.brand, med, t)
        d.update(brand=info.brand, outcome=Outcome.CONFIRMED.value, **_plain(desc))
    elif med.ingredient(name):
        desc = _describe_ingredient(name, med, t)
        d.update(brand=None, outcome=Outcome.CONFIRMED.value, **_plain(desc))
    else:
        comps = med.compositions_for(name)
        d.update(brand=name, generic=None, ingredients=[], verified_ingredients=[],
                 unverified_ingredients=[], verification=VerificationTier.UNKNOWN.value,
                 outcome=Outcome.AMBIGUOUS.value if len(comps) > 1 else Outcome.UNMATCHED.value)
        if len(comps) > 1:
            # The base name spans several compositions: ask which, never pick.
            d["candidates"] = med.variants(name)[:8]
    d["needs_hard_confirmation"] = _needs_hard(d)
    return d


def _plain(desc: dict) -> dict:
    out = dict(desc)
    v = out.get("verification")
    out["verification"] = v.value if hasattr(v, "value") else v
    return out
