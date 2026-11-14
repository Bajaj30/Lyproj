"""
Dispensing comparison (dispense/compare.py) against the test fixture.

    cd ml_pipeline && PRESCRIPTAI_DATA_DIR=tests/fixtures/india python -m tests.test_dispense

Plain asserts, no pytest: the same convention as the API checks (README §6a).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("PRESCRIPTAI_DATA_DIR", str(HERE / "fixtures" / "india"))
sys.path.insert(0, str(HERE.parent))

from dispense.compare import (  # noqa: E402
    COULD_NOT_VERIFY, MATCH, STRENGTH_MISMATCH, SUBSTITUTE, WRONG_MEDICINE,
    PackIdentity, compare, counts_as_dispensed, identify, parse_qr, parse_strength,
)
from normalise.india_medicines import IndiaMedicines  # noqa: E402

FIX = HERE / "fixtures" / "india"
med = IndiaMedicines(FIX / "indian_medicines_master.csv", FIX / "nlem2022_formulations.csv")
TODAY = date(2026, 11, 14)

RX = [
    {"index": 0, "brand": "Oflazest OZ", "ingredients": ["Ofloxacin", "Ornidazole"],
     "raw_reading": "TAB OFLAZEST OZ", "dosage": None},
    {"index": 1, "brand": "Dolo 650", "ingredients": ["Paracetamol"], "raw_reading": "Dolo 650"},
    {"index": 2, "brand": "Zofer 4mg", "ingredients": ["Ondansetron"], "raw_reading": "ZOFER",
     "dosage": None},
    {"index": 3, "brand": None, "generic": "Loperamide", "ingredients": ["Loperamide"],
     "raw_reading": "Loperamide 2mg", "dosage": "2 mg"},
]

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def run(pack, rx=RX, **kw):
    return compare(identify(pack, med), rx, med, today=TODAY, **kw)


# strength parsing
check("650 mg == 0.65 g", parse_strength("650 mg") == parse_strength("0.65g"))
check("combination order-independent", parse_strength("500mg/200mg") == parse_strength("200 mg + 500 mg"))
check("no strength -> None, not zero", parse_strength("Zofer") is None)

# outcomes
r = run(PackIdentity(source="manual", brand="Dolo 650"))
check("same brand + strength -> match", r.result == MATCH and r.medicine_index == 1 and not r.blocked)

r = run(PackIdentity(source="manual", brand="Crocin 500"))
check("same drug, 500 vs 650 -> strength mismatch", r.result == STRENGTH_MISMATCH and r.needs_confirmation)

r = run(PackIdentity(source="manual", brand="O2 Tablet", strength="200mg/500mg"))
check("other brand, same composition -> substitute", r.result == SUBSTITUTE and r.medicine_index == 0)

r = run(PackIdentity(source="manual", brand="O2 Tablet", strength="200mg/500mg"), allow_substitution=False)
check("substitute blocked when doctor says do-not-substitute", r.result == SUBSTITUTE and r.blocked)

r = run(PackIdentity(source="manual", brand="Brufen 400"))
check("ibuprofen not prescribed -> wrong medicine", r.result == WRONG_MEDICINE and r.blocked)

r = run(PackIdentity(source="manual", brand="Xyzzy 10"))
check("unknown product -> could not verify, never match", r.result == COULD_NOT_VERIFY and r.blocked)

r = run(PackIdentity(source="manual", brand="Zofer"))
check("'Zofer' (strength not on the pack) vs prescribed 4 mg -> could not verify",
      r.result == COULD_NOT_VERIFY and "strength" in " ".join(r.reasons))

r = run(PackIdentity(source="manual", brand="Emeset 4mg"))
check("Zofer 4mg written in full -> strength known; Emeset 4mg is a substitute",
      r.result == SUBSTITUTE and r.prescribed_strength == "4 mg")

r = run(PackIdentity(source="manual", brand="Oflazest OZ"))
check("prescription without a strength -> match but must be confirmed",
      r.result == MATCH and r.needs_confirmation)

r = run(PackIdentity(source="manual", generic_name="Loperamide Tablets IP 2 mg"))
check("generic-written line matched by printed composition", r.result == MATCH and r.medicine_index == 3)

r = run(PackIdentity(source="manual", brand="Dolo 650", expiry="03/2026"))
check("expired pack is blocked", r.expired and r.blocked)

rx_with_unreadable = RX + [{"index": 4, "brand": None, "ingredients": [], "raw_reading": "???"}]
r = run(PackIdentity(source="manual", brand="Brufen 400"), rx=rx_with_unreadable)
check("no match while a line is unreadable -> grey, not red", r.result == COULD_NOT_VERIFY)

r = run(PackIdentity(source="manual", brand="Dolo 650"), dispensed=[1])
check("second pack of an already-dispensed line asks for confirmation", r.needs_confirmation)

check("orange counts only after chemist confirms",
      not counts_as_dispensed(STRENGTH_MISMATCH, False, True, False)
      and counts_as_dispensed(STRENGTH_MISMATCH, False, True, True))
check("red never counts", not counts_as_dispensed(WRONG_MEDICINE, True, False, True))

# QR parsing
p = parse_qr('{"brand_name": "Dolo 650", "batch_no": "DL123", "expiry": "12/2027"}')
check("JSON QR", p.brand == "Dolo 650" and p.expiry == "12/2027")
p = parse_qr("Brand Name: Dolo 650\nGeneric Name: Paracetamol IP 650 mg\nBatch No: X1\nExp: 01/2028")
check("key: value QR", p.brand == "Dolo 650" and p.batch == "X1" and p.generic_name.startswith("Paracetamol"))
p = parse_qr("(01)08901234567890(17)270831(10)AB12")
check("GS1 QR -> batch + expiry, no brand", p.batch == "AB12" and p.expiry == "08/2027" and not p.brand)
r = run(parse_qr("(01)08901234567890(17)270831(10)AB12"))
check("GS1 alone cannot verify", r.result == COULD_NOT_VERIFY)
p = parse_qr("https://verify.example.com/p/123")
check("URL-only QR is reported, not guessed", p.brand is None and "web link" in p.note)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
