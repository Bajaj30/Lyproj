"""
The pipeline, held once per process, for every router that needs it.

    read_page()     S0 -> S1 -> S2 -> S3 on one uploaded page
    read_pages()    the same for several pages, read in parallel (one result or
                    one error per page)
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
import threading
import traceback
from typing import List, Optional, Sequence, Union

from fastapi import HTTPException

from ingest.normalise import NormalisedImage, normalise
from schemas import Outcome, ResolvedMedicine, VerificationTier
from vlm.client import describe_error
from vlm.page_reader import ReadResult, build_reader, read_many, resolve_reader_kind
from vlm.prompts import PROMPT_VERSION
from vlm.vocab import _describe, _describe_ingredient, load_config, resolve_page

READER_SETTING = os.environ.get("READER", "auto")

reader = None
checker = None
pack_reader = None
reader_kind = "unset"
# Result of the free key check run at startup: not_run | ok | auth_error |
# model_unavailable | unreachable. Reported by /api/health.
reader_check = "not_run"


def init() -> None:
    global reader, checker, pack_reader, reader_kind, reader_check
    from drug_interaction.interaction_inference import InteractionChecker
    from vlm.pack_reader import build_pack_reader

    reader_kind = resolve_reader_kind(READER_SETTING)
    caller = None
    if reader_kind == "claude":
        from vlm.client import ClaudeCaller

        caller = ClaudeCaller()   # one client: shared retries, concurrency and spend rules
    reader = build_reader(reader_kind, caller=caller)
    pack_reader = build_pack_reader(reader_kind if reader_kind in ("claude", "mock") else "mock",
                                    caller=caller)
    checker = InteractionChecker()
    if medicine_data() == "missing":
        from normalise.india_medicines import DATA_DIR

        print(f"[API] WARNING: the medicine CSVs are not in {DATA_DIR}. Brand matching cannot run, "
              f"so every upload will fail until they are added (README §9).", flush=True)
    if reader_kind.startswith("mock"):
        reader_check = "not_run"
        print("[API] READER is the MOCK: every page reads as the same demo prescription. "
              "Add ANTHROPIC_API_KEY to .env and restart to read real pages.", flush=True)
    elif caller is not None:
        # Tell the operator NOW whether the key works, not at the first upload.
        # One free metadata call, off the startup path so a slow network never
        # delays the server.
        def probe() -> None:
            global reader_check
            reader_check = caller.check_access(reader.model)
            if reader_check == "ok":
                print(f"[API] API key accepted; {reader.model} is available.", flush=True)
            else:
                print(f"[API] WARNING: reader check failed ({reader_check}). Page reads will "
                      f"fail until ANTHROPIC_API_KEY and the model are right.", flush=True)

        threading.Thread(target=probe, daemon=True).start()


def medicine_data() -> str:
    """ok | missing. The two CSVs S2/S3 need are gitignored, so a fresh clone
    does not have them; say so at startup rather than at the first upload."""
    from normalise.india_medicines import MASTER_CSV, NLEM_FORMULATIONS_CSV

    return "ok" if MASTER_CSV.exists() and NLEM_FORMULATIONS_CSV.exists() else "missing"


def reader_http_error(exc: BaseException) -> HTTPException:
    """A failed read as an HTTP error whose message is safe to show the user.
    The full exception goes to the server log, never to the browser."""
    status, message = describe_error(exc)
    print(f"[READER] {type(exc).__name__}: {exc}", flush=True)
    if status == 502 and not isinstance(exc, (RuntimeError, OSError)):
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    headers = {"Retry-After": "60"} if status == 503 else None
    return HTTPException(status, message, headers=headers)


def is_mock() -> bool:
    return reader_kind.startswith("mock")


def _provenance(r: ReadResult) -> dict:
    """Fields 1 and 2 of models.py §16.3, taken from THIS read's own result."""
    return dict(
        model=r.model, prompt_version=PROMPT_VERSION, effort=r.effort,
        raw_response=json.dumps(r.raw_response),
        cost_usd=r.cost_usd, from_cache=r.from_cache,
    )


def read_page(source: Union[bytes, NormalisedImage]):
    """S0-S3. Returns (page, resolved medicine dicts, normalised image, provenance)."""
    norm = source if isinstance(source, NormalisedImage) else normalise(source)   # S0
    result = reader.read_detailed(norm)                                           # S1
    return result.page, resolve_page(result.page), norm, _provenance(result)      # S2 + S3


def read_pages(sources: Sequence[Union[bytes, NormalisedImage]]) -> list:
    """S0-S3 for several pages, S1 in parallel. One entry per input, in order:
    either the read_page() tuple or the exception that page raised."""
    norms = [s if isinstance(s, NormalisedImage) else normalise(s) for s in sources]
    out = []
    for norm, result in zip(norms, read_many(reader, norms)):
        if isinstance(result, Exception):
            out.append(result)
            continue
        try:
            out.append((result.page, resolve_page(result.page), norm, _provenance(result)))
        except Exception as exc:        # e.g. the medicine dataset is missing
            out.append(exc)
    return out


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
