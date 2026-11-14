"""
Dispensing verification — Workflow.md stage 6.

A pack handed over at the counter is compared to the doctor's finalised
prescription. Three ways in, one comparison:

    QR code on the pack  -> parse_qr()        (preferred: structured, exact)
    photo of the label   -> vlm/pack_reader   (the same VLM, a pack-label prompt)
    typed by the chemist -> manual            (fallback when both fail)

        -> identify()   brand -> composition + strength, through the same S3
                        lookup the prescription went through
        -> compare()    against every prescribed medicine

Outcomes (Workflow §4 stage 6):

    match              same brand (or composition) and same strength     green
    substitute         different brand, same ingredients and strength    amber
    strength_mismatch  right drug, wrong strength or form                orange
    wrong_medicine     composition matches no prescribed medicine        red
    could_not_verify   unreadable, unknown product, or not enough to     grey
                       decide

Rules carried over from the reading pipeline:

  * COMPOSITION, NOT BRAND STRING. Two brands of one drug are equivalent; two
    similar-looking brand names are not (S2's collapse_same_composition).
  * "COULD NOT VERIFY" NEVER BECOMES "MATCH". Every path that lacks the facts to
    decide abstains. In particular: a pack that matches no READABLE prescribed
    medicine is not called wrong while some prescribed line is still unreadable —
    it might be that line. It is grey, and the reason says so.
  * A prescribed strength is only taken from the brand when the written brand is
    an exact full-name hit ("Dolo 650"). A base-name hit ("Zofer") picks one
    strength variant arbitrarily; reading its strength as "the prescribed one"
    would invent a fact.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalise.drug_names import normalise_generic  # noqa: E402
from normalise.india_medicines import (  # noqa: E402
    IndiaMedicines,
    base_brand,
    match_key,
    split_ingredients,
)

MATCH = "match"
SUBSTITUTE = "substitute"
STRENGTH_MISMATCH = "strength_mismatch"
WRONG_MEDICINE = "wrong_medicine"
COULD_NOT_VERIFY = "could_not_verify"

RESULTS = (MATCH, SUBSTITUTE, STRENGTH_MISMATCH, WRONG_MEDICINE, COULD_NOT_VERIFY)


# ─── what was scanned ────────────────────────────────────────────────────────


@dataclass
class PackIdentity:
    """A pack, as scanned/read/typed. Every field verbatim; None = not known."""

    source: str                       # qr | photo | manual
    brand: Optional[str] = None
    generic_name: Optional[str] = None
    strength: Optional[str] = None
    form: Optional[str] = None
    manufacturer: Optional[str] = None
    batch: Optional[str] = None
    expiry: Optional[str] = None
    legible: bool = True
    reader: Optional[str] = None      # which reader produced it ("mock-pack" is flagged)
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


_URL_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
_GS1_RE = re.compile(r"\((\d{2,4})\)([^()]+)")
_KV_KEYS = {
    "brand": ("brand name", "brand", "product name", "product"),
    "generic_name": ("generic name", "proper name", "generic", "composition", "salt"),
    "strength": ("strength",),
    "form": ("dosage form", "form"),
    "manufacturer": ("manufacturer name", "manufacturer", "mfr", "mfd by", "marketed by"),
    "batch": ("batch number", "batch no", "batch", "b no", "lot"),
    "expiry": ("expiry date", "expiry", "exp date", "exp", "use before"),
}


def parse_qr(text: str) -> PackIdentity:
    """Pack QR payload -> PackIdentity.

    Indian packs carry QR codes in several shapes (Drugs Rules Schedule H2 lists
    the CONTENT, not a format): JSON, "Key: value" lines, GS1 element strings,
    or just a web link. A link alone cannot be checked without the
    manufacturer's site (Workflow §10 Q9), so it is reported as such rather than
    guessed at.
    """
    raw = (text or "").strip()
    pack = PackIdentity(source="qr")
    if not raw:
        pack.legible, pack.note = False, "The QR code was empty."
        return pack
    if _URL_RE.match(raw):
        pack.note = ("This QR code holds only a web link. It cannot be checked here; "
                     "photograph the label or enter the pack details instead.")
        return pack

    # JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            lower = {str(k).strip().lower().replace("_", " "): v for k, v in data.items()}
            for fld, keys in _KV_KEYS.items():
                for k in keys:
                    if lower.get(k):
                        setattr(pack, fld, str(lower[k]).strip())
                        break
            return pack
    except (ValueError, TypeError):
        pass

    # GS1 element string with parentheses: (01)GTIN(17)YYMMDD(10)BATCH
    gs1 = dict(_GS1_RE.findall(raw))
    if gs1:
        pack.batch = gs1.get("10", "").strip() or None
        if gs1.get("17"):
            yymmdd = gs1["17"].strip()
            if re.fullmatch(r"\d{6}", yymmdd):
                pack.expiry = f"{yymmdd[2:4]}/20{yymmdd[:2]}"
        pack.note = ("GS1 code: it identifies the product by number only (GTIN). "
                     "The brand and strength are not in it.")
        # Some packs append free text after the element string.
        tail = _GS1_RE.sub("\n", raw)
        _parse_kv(tail, pack)
        return pack

    # "Key: value" lines (or ; | separated)
    if not _parse_kv(raw, pack):
        # A bare line of text: treat it as the product name, the commonest case
        # of a hand-made or reseller sticker. Matching decides whether it is real.
        pack.brand = raw.splitlines()[0][:120]
    return pack


def _parse_kv(text: str, pack: PackIdentity) -> bool:
    found = False
    for part in re.split(r"[\n;|]+", text or ""):
        if ":" not in part and "=" not in part:
            continue
        key, _, val = re.split(r"([:=])", part, maxsplit=1)
        key = re.sub(r"[^a-z ]", " ", key.lower()).strip()
        key = re.sub(r"\s+", " ", key)
        val = val.strip()
        if not val:
            continue
        for fld, keys in _KV_KEYS.items():
            if key in keys and not getattr(pack, fld):
                setattr(pack, fld, val)
                found = True
                break
    return found


# ─── strength and form ───────────────────────────────────────────────────────

_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mcg|µg|ug|mg|gm|g|ml|iu|%)(?![a-z])", re.IGNORECASE
)
_TO_MG = {"mcg": 0.001, "µg": 0.001, "ug": 0.001, "mg": 1.0, "g": 1000.0, "gm": 1000.0}


def parse_strength(text: Optional[str]) -> Optional[Tuple[Tuple[float, str], ...]]:
    """'650 mg' -> ((650.0, 'mg'),); '0.5 g' -> ((500.0, 'mg'),).

    Order-independent (sorted), unit-normalised. None when no strength is
    present — "unknown", never "zero".
    """
    if not text:
        return None
    out = []
    for num, unit in _UNIT_RE.findall(text):
        unit = unit.lower()
        val = float(num)
        if unit in _TO_MG:
            val, unit = round(val * _TO_MG[unit], 4), "mg"
        out.append((val, unit))
    return tuple(sorted(out)) or None


def strength_label(s: Optional[Tuple[Tuple[float, str], ...]]) -> Optional[str]:
    if not s:
        return None
    return " / ".join(f"{v:g} {u}" for v, u in s)


_FORM_FAMILIES = [
    ("tablet", r"\b(tab|tabs|tablet|tablets|tbl)\b"),
    ("capsule", r"\b(cap|caps|capsule|capsules)\b"),
    ("liquid", r"\b(syp|syrup|susp|suspension|solution|liquid|elixir|oral\s+drops)\b"),
    ("injection", r"\b(inj|injection|vial|ampoule|amp|infusion)\b"),
    ("topical", r"\b(cream|ointment|oint|gel|lotion)\b"),
    ("drops", r"\b(drops?|eye\s+drops?|ear\s+drops?)\b"),
    ("inhaler", r"\b(inhaler|respules?|rotacaps?|mdi)\b"),
    ("powder", r"\b(powder|sachet|granules)\b"),
]


def form_family(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.lower()
    for fam, rx in _FORM_FAMILIES:
        if re.search(rx, t):
            return fam
    return None


# ─── identify: pack -> composition ───────────────────────────────────────────


@dataclass
class ResolvedPack:
    pack: PackIdentity
    brand: Optional[str] = None           # canonical brand from the database
    composition: Optional[str] = None
    ingredients: List[str] = field(default_factory=list)
    strength: Optional[Tuple[Tuple[float, str], ...]] = None
    form: Optional[str] = None
    reason: Optional[str] = None          # why it could not be identified


def _ing_keys(ingredients: Sequence[str]) -> frozenset:
    return frozenset(normalise_generic(i) for i in ingredients if i and normalise_generic(i))


def identify(pack: PackIdentity, med: IndiaMedicines) -> ResolvedPack:
    """Brand (or printed generic) -> composition, through the S3 lookup.

    Exact lookup only — no fuzzy matching here. On a printed label the name is
    exact; a fuzzy hit at the counter would be a guess presented as a check.
    """
    r = ResolvedPack(pack=pack)
    if not pack.legible:
        r.reason = pack.note or "The pack could not be read."
        return r

    info = med.lookup(pack.brand) if pack.brand else None
    exact = med.exact(pack.brand) if pack.brand else None
    if info:
        r.brand, r.composition, r.ingredients = info.brand, info.composition, list(info.ingredients)
    elif pack.generic_name:
        # A printed composition line: "Paracetamol IP 650 mg", "Ofloxacin & Ornidazole Tablets IP"
        names = re.split(r"\s*(?:\+|&|\band\b|,)\s*", pack.generic_name, flags=re.IGNORECASE)
        found = []
        for n in names:
            n = re.sub(r"\b(IP|BP|USP|tablets?|capsules?|syrup|injection|oral|suspension)\b", " ",
                       _UNIT_RE.sub(" ", n), flags=re.IGNORECASE)
            n = re.sub(r"\s+", " ", n).strip(" .-")
            canonical = med.ingredient(n)
            if canonical:
                found.append(canonical)
        if found and len(found) == len([n for n in names if n.strip()]):
            r.ingredients = found
            r.composition = " + ".join(found)
    if not r.ingredients:
        if pack.brand and not info and med.compositions_for(pack.brand):
            r.reason = (f"“{pack.brand}” is sold in more than one composition. "
                        "The strength on the pack is needed to tell which.")
        elif pack.brand or pack.generic_name:
            r.reason = f"“{pack.brand or pack.generic_name}” is not in the medicines database."
        else:
            r.reason = pack.note or "No product name could be found on the pack."
        return r

    r.strength = (parse_strength(pack.strength)
                  or parse_strength(pack.generic_name)
                  or (parse_strength(exact.strength) if exact else None)
                  or (parse_strength(pack.brand) if pack.brand else None))
    r.form = form_family(pack.form) or (form_family(exact.form) if exact else None)
    return r


# ─── compare: pack vs prescription ───────────────────────────────────────────


@dataclass
class DispenseResult:
    result: str
    medicine_index: Optional[int] = None   # which prescribed medicine it matched
    medicine_label: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    blocked: bool = False                  # may not be counted as dispensed
    needs_confirmation: bool = False       # chemist must actively confirm
    pack_brand: Optional[str] = None
    pack_composition: Optional[str] = None
    pack_strength: Optional[str] = None
    prescribed_strength: Optional[str] = None
    expired: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def prescribed_strength(m: dict, med: IndiaMedicines) -> Optional[Tuple[Tuple[float, str], ...]]:
    """Strength the DOCTOR specified, or None. See the module docstring."""
    exact = med.exact(m.get("brand")) if m.get("brand") else None
    from_brand = None
    if exact and base_brand(exact.brand) != exact.brand:   # the name carries a strength
        from_brand = parse_strength(exact.strength) or parse_strength(exact.brand)
    return from_brand or parse_strength(m.get("dosage")) or parse_strength(m.get("raw_reading"))


def _expired(expiry: Optional[str], today: date) -> bool:
    """'08/2027', '08-27', 'AUG 2027'. Unparseable -> False (shown, not judged)."""
    if not expiry:
        return False
    m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{2,4})", expiry)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
    else:
        months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
        m = re.search(r"([a-z]{3})[a-z]*\.?\s*[/\-.]?\s*(\d{2,4})", expiry.lower())
        if not m or m.group(1) not in months:
            return False
        month, year = months.index(m.group(1)) + 1, int(m.group(2))
    if year < 100:
        year += 2000
    if not 1 <= month <= 12:
        return False
    # A pack is usable through the end of its expiry month.
    return (year, month) < (today.year, today.month)


def compare(
    resolved: ResolvedPack,
    prescribed: Sequence[dict],
    med: IndiaMedicines,
    dispensed: Sequence[int] = (),
    allow_substitution: bool = True,
    today: Optional[date] = None,
) -> DispenseResult:
    """Compare one identified pack against the finalised medicines.

    `prescribed` are the snapshot's medicine dicts (with `index`, `brand`,
    `ingredients`, `dosage`, `raw_reading`, `removed`). `dispensed` lists the
    indexes already fully dispensed, so a second pack of the same drug is matched
    to a line still outstanding when there is one.
    """
    today = today or date.today()
    pack = resolved.pack
    out = DispenseResult(
        result=COULD_NOT_VERIFY, pack_brand=resolved.brand or pack.brand,
        pack_composition=resolved.composition,
        pack_strength=strength_label(resolved.strength) or pack.strength,
    )
    if pack.reader and pack.reader.startswith("mock"):
        out.reasons.append("Demo reader: this result is not based on your photo.")

    if _expired(pack.expiry, today):
        out.expired, out.blocked = True, True
        out.reasons.append(f"The pack expired ({pack.expiry}). Do not dispense it.")

    if not resolved.ingredients:
        out.reasons.append(resolved.reason or "The pack could not be identified.")
        out.blocked = True
        return out

    live = [m for m in prescribed if not m.get("removed")]
    pack_keys = _ing_keys(resolved.ingredients)
    same = [m for m in live if m.get("ingredients") and _ing_keys(m["ingredients"]) == pack_keys]

    if not same:
        unreadable = [m for m in live if not m.get("ingredients")]
        overlap = [m for m in live if m.get("ingredients") and _ing_keys(m["ingredients"]) & pack_keys]
        if unreadable:
            out.reasons.append(
                f"It matches none of the readable medicines, but {len(unreadable)} prescribed "
                f"line{'s' if len(unreadable) > 1 else ''} could not be identified. "
                "Check it against the original prescription."
            )
            out.blocked = True
            return out
        out.result, out.blocked = WRONG_MEDICINE, True
        out.reasons.append(f"{resolved.composition} is not on this prescription.")
        for m in overlap:
            out.reasons.append(
                f"It shares an ingredient with {_label(m)} ({' + '.join(m['ingredients'])}), "
                "but the composition is different."
            )
        return out

    # Prefer a prescribed line not yet dispensed.
    target = next((m for m in same if m.get("index") not in set(dispensed)), same[0])
    out.medicine_index, out.medicine_label = target.get("index"), _label(target)
    if target.get("index") in set(dispensed):
        out.reasons.append(f"{_label(target)} has already been dispensed.")
        out.needs_confirmation = True

    want = prescribed_strength(target, med)
    out.prescribed_strength = strength_label(want)
    pres_form = form_family(target.get("raw_reading"))

    if want and not resolved.strength:
        out.reasons.append("The strength on the pack could not be read. Rescan, or check it by eye.")
        out.blocked = True
        return out
    if want and resolved.strength and want != resolved.strength:
        out.result, out.needs_confirmation = STRENGTH_MISMATCH, True
        out.reasons.append(
            f"Prescribed {strength_label(want)}, pack is {strength_label(resolved.strength)}."
        )
        return out
    if pres_form and resolved.form and pres_form != resolved.form:
        out.result, out.needs_confirmation = STRENGTH_MISMATCH, True
        out.reasons.append(f"Prescribed as a {pres_form}, the pack is a {resolved.form}.")
        return out

    same_brand = bool(target.get("brand") and resolved.brand
                      and match_key(base_brand(target["brand"])) == match_key(base_brand(resolved.brand)))
    if same_brand or not target.get("brand"):
        # A generic-written line is matched by composition: any brand of it is
        # what was prescribed.
        out.result = MATCH
    else:
        out.result = SUBSTITUTE
        out.reasons.append(
            f"Different brand from the prescribed {target['brand']}, same ingredients"
            + (" and strength." if want else ".")
        )
        if not allow_substitution:
            out.blocked = True
            out.reasons.append("The doctor marked this prescription “do not substitute”.")

    if not want:
        out.needs_confirmation = True
        out.reasons.append("The prescription does not state a strength. Confirm it before dispensing.")
    return out


def _label(m: dict) -> str:
    return m.get("brand") or m.get("generic") or m.get("raw_reading") or f"line {m.get('index', '?')}"


def counts_as_dispensed(result: str, blocked: bool, needs_confirmation: bool,
                        chemist_confirmed: bool) -> bool:
    """Whether a check marks its medicine as handed over."""
    if blocked or result in (WRONG_MEDICINE, COULD_NOT_VERIFY):
        return False
    if needs_confirmation and not chemist_confirmed:
        return False
    return True


def ingredients_of(composition: str) -> List[str]:
    return split_ingredients(composition)


def summarise(results: Sequence[Dict]) -> Dict[str, int]:
    return {r: sum(1 for x in results if x.get("result") == r) for r in RESULTS}
