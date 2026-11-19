"""
The Koncile page reader (vlm/koncile_reader.py), against a fake Koncile server.

    cd ml_pipeline && python -m tests.test_koncile_reader

No key, no network, no credits: httpx.MockTransport stands in for api.koncile.ai,
following the routes and statuses in Koncile's OpenAPI spec (v1.1.0). The real
.env is never read. What it cannot prove is the live reply's exact field layout,
which the spec leaves undocumented; the parser is checked here against the three
layouts it accepts.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

TMP = Path(tempfile.mkdtemp(prefix="koncile-test-"))
os.environ["PRESCRIPTAI_CACHE_DIR"] = str(TMP)       # ledger + cache in a temp dir
for k in ("ANTHROPIC_API_KEY", "KONCILE_API_KEY", "KONCILE_TEMPLATE_ID", "KONCILE_FOLDER_ID"):
    os.environ.pop(k, None)

import env  # noqa: E402

env._loaded = True                                   # never read the real .env (it holds keys)

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from ingest.normalise import normalise  # noqa: E402
from vlm.client import (  # noqa: E402
    ReaderAuthError, ReaderFailed, ReaderQuotaExceeded, ReaderTimedOut, ReaderUnavailable, describe_error,
)
from vlm.koncile_reader import KoncilePageReader, load_fields, to_page_read  # noqa: E402
from vlm.page_reader import CachedPageReader, read_many, resolve_reader_kind  # noqa: E402
from vlm.spend import get_guard  # noqa: E402
from vlm.vocab import load_config, thresholds_for  # noqa: E402

KEY = "test-koncile-key-0123456789"
checks = []


def check(name, cond, extra=""):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))


def page(n: int):
    buf = io.BytesIO()
    Image.new("RGB", (300 + n, 400), (20 * n % 255, 60, 120)).save(buf, "JPEG")
    return normalise(buf.getvalue())


# A reply shaped like the Koncile app's screen for a prescription (its field
# names and percentages). The people and numbers are made up. The
# value/confidence layout is an assumption: the spec does not document it.
GENERAL = {
    "Doctor's name": {"value": "Dr. A. Example", "confidence_score": 89},
    "Doctor's phone number": {"value": "9000000000", "confidence_score": 96},
    "Patient's name": {"value": "Ms. TEST PATIENT", "confidence_score": 88},
    "Is this document indeed a prescription?": {"value": True, "confidence_score": 96},
    "Is the prescription still valid?": {"value": False, "confidence_score": 90},
    "Doctor's identification number": {"value": "Not found", "confidence_score": 96},
    "Prescription issue date": {"value": "01/02/2026", "confidence_score": 85},
    "Patient's date of birth": {"value": None, "confidence_score": 97},
    "Additional information about the patient": {"value": "Fever x 3 days", "confidence_score": 94},
}
MEDS = [("OFLAZEST OZ", "1-0-1", "3 days", 92), ("AZENAC MR", "1-0-1", "3 days", 90),
        ("ANDIAL", "2-1", "3 days", 81), ("ZOFER", "1-0-1", "3 days", 77)]
COLUMNS = {
    "Medication name": [{"value": d, "confidence_score": c} for d, _, _, c in MEDS],
    "Frequency": [{"value": f, "confidence_score": 95} for _, f, _, _ in MEDS],
    "Duration": [{"value": u, "confidence_score": 95} for _, _, u, _ in MEDS],
}
ROWS = [{"Medication name": {"value": d, "confidence_score": c}, "Frequency": {"value": f},
         "Duration": {"value": u}} for d, f, u, c in MEDS]


class FakeKoncile:
    """Just enough of api.koncile.ai for the reader's four routes."""

    def __init__(self, line_fields=None, general=None, in_progress=1, upload_errors=(), final="DONE",
                 folders=None, per_image=False):
        self.line_fields = COLUMNS if line_fields is None else line_fields
        self.general = GENERAL if general is None else general
        self.in_progress, self.final = in_progress, final
        self.upload_errors = list(upload_errors)
        self.folders = folders if folders is not None else [
            {"id": 7, "name": "Default", "templates": [{"id": 11, "name": "Invoice"}, {"id": 12, "name": "Prescription"}]}]
        self.per_image = per_image
        self.calls, self.uploads, self.polls, self.task_image = [], [], {}, {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.url.params)))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "Invalid API key"})
        if path == "/v1/check_api_key/":
            return httpx.Response(200, json={"detail": "API key is valid"})
        if path == "/v1/fetch_all_folders/":
            return httpx.Response(200, json={"folders": self.folders})
        if path == "/v1/upload_file/":
            request.read()
            self.uploads.append(request)
            if self.upload_errors:
                code, text = self.upload_errors.pop(0)
                return httpx.Response(code, text=text)
            tid = f"task-{len(self.uploads)}"
            body = request.content.decode("latin-1")
            self.task_image[tid] = body.split('filename="page-')[1][:16] if 'filename="page-' in body else "?"
            return httpx.Response(200, json={"task_ids": [tid]})
        if path == "/v1/fetch_tasks_results/":
            tid = request.url.params["task_id"]
            n = self.polls[tid] = self.polls.get(tid, 0) + 1
            if n <= self.in_progress:
                return httpx.Response(200, json={"status": "IN PROGRESS", "status_message": "processing"})
            if self.final == "DUPLICATE" and tid != "task-original":
                return httpx.Response(200, json={"status": "DUPLICATE", "status_message": "dup",
                                                 "duplicate_of_task_id": "task-original"})
            if self.final == "FAILED":
                return httpx.Response(200, json={"status": "FAILED", "status_message": "unreadable file"})
            general = dict(self.general)
            if self.per_image:   # each task answers with ITS image, to catch swapped results
                general["Patient's name"] = {"value": self.task_image.get(tid, "?"), "confidence_score": 90}
            return httpx.Response(200, json={"status": "DONE", "status_message": "ok", "task_id": tid,
                                             "document_id": 99, "General_fields": general,
                                             "Line_fields": self.line_fields})
        return httpx.Response(404)


def reader(fake: FakeKoncile, **kw) -> KoncilePageReader:
    client = httpx.Client(base_url="https://api.koncile.ai", transport=httpx.MockTransport(fake.handler))
    return KoncilePageReader(api_key=kw.pop("api_key", KEY), client=client, poll_seconds=1, timeout_seconds=5,
                             sleep=lambda s: None, **kw)


cfg = load_fields()

# ── the reply becomes a PageRead ─────────────────────────────────────────────
fake = FakeKoncile()
r = reader(fake)
res = r.read_detailed(page(1))
p = res.page
check("a DONE task becomes a prescription PageRead", p.document_type == "prescription")
check("patient and prescriber are mapped by field name",
      p.patient_name == "Ms. TEST PATIENT" and p.prescriber_name == "Dr. A. Example", (p.patient_name, p.prescriber_name))
check("the issue date is used, never the date of birth", p.date == "01/02/2026", p.date)
check("'Additional information about...' fills the diagnosis", p.diagnosis == "Fever x 3 days")
check("every medicine row becomes a line, in order",
      [m.drug_token for m in p.medicines] == [d for d, *_ in MEDS], [m.drug_token for m in p.medicines])
check("frequency and duration land in their own fields",
      p.medicines[0].frequency == "1-0-1" and p.medicines[0].duration == "3 days")
check("percent confidences become 0-1", abs(p.medicines[3].confidence - 0.77) < 1e-9, p.medicines[3].confidence)
check("raw_text keeps every cell of the row", p.medicines[0].raw_text == "OFLAZEST OZ 1-0-1 3 days", p.medicines[0].raw_text)
check("no alternatives are invented", all(m.alternatives == [] for m in p.medicines))
check("provenance: model 'koncile', its own mapping version, $0",
      res.model == "koncile" and res.prompt_version == "koncile-k2" and res.cost_usd == 0.0)
check("the raw reply is kept whole for the correction log",
      res.raw_response["result"]["General_fields"]["Doctor's name"]["value"] == "Dr. A. Example")

# ── the three line layouts give the same medicines ──────────────────────────
base = [(m.drug_token, m.frequency, m.duration) for m in p.medicines]
for name, lf in (("row-oriented list", ROWS), ("named table", {"Medications": ROWS, "Tests": []}),
                 ("named table among others", {"Lab tests": [{"Test": {"value": "CBC"}}], "Medicines": ROWS})):
    q = to_page_read({"General_fields": GENERAL, "Line_fields": lf}, cfg)
    check(f"line layout: {name}", [(m.drug_token, m.frequency, m.duration) for m in q.medicines] == base,
          [(m.drug_token, m.frequency) for m in q.medicines])
q = to_page_read({"General_fields": {"Patient's name": "Ravi", "Medicines": ["Tab Zofer 4mg", "Tab Andial"]}}, cfg)
check("bare values and a medicines list in the general fields still read",
      q.patient_name == "Ravi" and [m.drug_token for m in q.medicines] == ["Tab Zofer 4mg", "Tab Andial"])
q = to_page_read({"General_fields": {**GENERAL, "Is this document indeed a prescription?": {"value": "False"}},
                  "Line_fields": COLUMNS}, cfg)
check("'not a prescription' lists no medicines (PageRead contract)",
      q.document_type == "not_a_prescription" and q.medicines == [])
q = to_page_read({"General_fields": {}, "Line_fields": {}}, cfg)
check("an empty reply says so instead of looking like a blank prescription",
      q.medicines == [] and q.unreadable_regions)

# The columns of Koncile's own prescription template, as seen on a real reply
# (values made up). Its "Dosage" holds the schedule, durations are bare days.
def col(*vals):
    return [{"value": v, "confidence_score": 0.95} for v in vals]


REAL = {"Medication name": col("ZOFER", "ANDIAL"), "Quantity": col("6", "5"),
        "Dosage": col("1-0-1", "2, then 1 every 6 hours"),
        "Duration of treatment (in days)": col("3", "3"),
        "Additional information": col("after food", ""), "Refill": col("", "")}
q = to_page_read({"General_fields": GENERAL, "Line_fields": REAL}, cfg)
m0, m1 = q.medicines
check("real template: 'Dosage' (the schedule) fills How often, not Dose",
      m0.frequency == "1-0-1" and m1.frequency == "2, then 1 every 6 hours" and m0.dosage is None,
      (m0.frequency, m0.dosage))
check("real template: a bare duration takes 'days' from its column name",
      m0.duration == "3 days", m0.duration)
check("real template: 'Additional information' fills Instructions",
      m0.instructions == "after food" and m1.instructions is None, (m0.instructions, m1.instructions))
check("real template: Quantity stays visible in the row as written", "6" in m0.raw_text.split(), m0.raw_text)
check("a duration that already has words is left alone",
      to_page_read({"Line_fields": {"Medication name": col("X"), "Duration": col("5 days")}}, cfg)
      .medicines[0].duration == "5 days")

# ── the calls it makes ───────────────────────────────────────────────────────
up = fake.uploads[0]
check("the template is found by name and sent with the upload",
      up.url.params.get("template_id") == "12" and up.url.params.get("folder_id") == "7", dict(up.url.params))
check("the uploaded file is named by hash, never by the original name", b'filename="page-' in up.content)
check("one upload, then polling until DONE", len(fake.uploads) == 1 and fake.polls.get("task-1") == 2)
r.read_detailed(page(2))
check("the folder lookup happens once per process",
      sum(1 for _, path, _ in fake.calls if path == "/v1/fetch_all_folders/") == 1)
fake = FakeKoncile()
reader(fake, template_id=55).read_detailed(page(1))
check("KONCILE_TEMPLATE_ID pins the template (no lookup)",
      fake.uploads[0].url.params.get("template_id") == "55"
      and not any(path == "/v1/fetch_all_folders/" for _, path, _ in fake.calls))

# ── cache, ledger, parallel ──────────────────────────────────────────────────
fake = FakeKoncile()
cached = CachedPageReader(reader(fake), cache_dir=TMP / "cache")
first = cached.read_detailed(page(3))
again = cached.read_detailed(page(3))
check("the same image twice costs one Koncile call", len(fake.uploads) == 1 and again.from_cache)
check("a cache hit keeps Koncile's provenance", again.model == "koncile" and again.prompt_version == "koncile-k2")
key = cached._key(page(3))
check("the cache key names Koncile, not the Claude prompt", "koncile" in key and "_v2_" not in key, key)
ledger = [e for e in get_guard()._read() if str(e.get("tag", "")).startswith("koncile:")]
check("each Koncile call is in the spend ledger at $0", ledger and all(e["usd"] == 0.0 for e in ledger))

fake = FakeKoncile(per_image=True)
imgs = [page(10), page(11), page(12)]
out = read_many(reader(fake), imgs)
check("three pages in parallel: three uploads, none swapped",
      len(fake.uploads) == 3 and all(o.page.patient_name == i.sha256[:16] for o, i in zip(out, imgs)),
      [(o.page.patient_name, i.sha256[:16]) for o, i in zip(out, imgs)])

# ── failures ────────────────────────────────────────────────────────────────
try:
    reader(FakeKoncile(), api_key="wrong-key-000000000000").read_detailed(page(1))
    check("a rejected key raises", False)
except ReaderAuthError as exc:
    status, msg = describe_error(exc)
    check("a rejected key -> 502 'API key', with no key in the message",
          status == 502 and "API key" in msg and "wrong-key" not in msg + str(exc))
check("key check: ok", reader(FakeKoncile()).check_access() == "ok")
check("key check: auth_error", reader(FakeKoncile(), api_key="wrong-key-000000000000").check_access() == "auth_error")


def down(request):
    raise httpx.ConnectError("no route")


offline = KoncilePageReader(api_key=KEY, sleep=lambda s: None, client=httpx.Client(
    base_url="https://api.koncile.ai", transport=httpx.MockTransport(down)))
check("key check: unreachable", offline.check_access() == "unreachable")

fake = FakeKoncile(upload_errors=[(429, "slow down")])
reader(fake, template_id=1).read_detailed(page(1))
check("a 429 on upload is retried (it never registered)", len(fake.uploads) == 2)
fake = FakeKoncile(upload_errors=[(500, "boom")])
try:
    reader(fake, template_id=1).read_detailed(page(1))
    check("a 500 on upload fails", False)
except ReaderUnavailable:
    check("a 500 on upload is NOT retried (it may have registered: no double credits)", len(fake.uploads) == 1)
fake = FakeKoncile(upload_errors=[(403, "Not enough credits on this account")])
try:
    reader(fake, template_id=1).read_detailed(page(1))
    check("credits used up -> 503 that says so", False, "no error raised")
except ReaderQuotaExceeded as exc:
    check("credits used up -> 503 that says so", describe_error(exc)[0] == 503 and "credits" in describe_error(exc)[1])
try:
    reader(FakeKoncile(final="FAILED"), template_id=1).read_detailed(page(1))
    check("a FAILED task -> 422", False, "no error raised")
except ReaderFailed as exc:
    check("a FAILED task -> 422", describe_error(exc)[0] == 422)
fake = FakeKoncile(final="DUPLICATE")
res = reader(fake, template_id=1).read_detailed(page(1))
check("a DUPLICATE task follows the original's reading",
      res.page.medicines and fake.polls.get("task-original"), fake.polls)
fake = FakeKoncile(in_progress=10_000)
try:
    reader(fake, template_id=1).read_detailed(page(1))
    check("a task that never finishes times out", False, "no error raised")
except ReaderTimedOut as exc:
    check("a task that never finishes times out -> 504, after a bounded number of polls",
          describe_error(exc)[0] == 504 and fake.polls["task-1"] <= 7, fake.polls)

# ── selection and thresholds ─────────────────────────────────────────────────
os.environ["KONCILE_API_KEY"] = KEY
check("READER=auto with only a Koncile key -> koncile", resolve_reader_kind("auto") == "koncile")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-" + "a" * 40
check("an Anthropic key keeps priority (existing deployments unchanged)", resolve_reader_kind("auto") == "claude")
os.environ.pop("ANTHROPIC_API_KEY")
os.environ.pop("KONCILE_API_KEY")
check("no key -> mock", resolve_reader_kind("auto") == "mock")
check("Claude pages keep the top-level thresholds, unchanged",
      thresholds_for("claude-opus-5") == load_config() and load_config().confident_read == 0.60)
t = thresholds_for("koncile")
check("Koncile pages get their own reading thresholds",
      t.confident_read == 0.90 and t.abstain_below == 0.50 and t.confirm_score == load_config().confirm_score)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
