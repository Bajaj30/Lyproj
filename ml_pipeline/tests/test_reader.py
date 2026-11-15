"""
The API-calling layer (vlm/client.py, vlm/page_reader.py), with a fake client.

    cd ml_pipeline && python -m tests.test_reader

No key and no network: a stand-in for the Anthropic client records every call,
so the tests can prove how MANY calls a situation makes, that parallel reads do
not swap their results, and what happens when the API refuses, truncates or
rejects a parameter. What it cannot prove is the live API's behaviour; that
checklist is in docs/API_IMPLEMENTATION.md.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import anthropic  # noqa: E402
import httpx2  # noqa: E402
from PIL import Image  # noqa: E402

import vlm.spend as spend  # noqa: E402
from schemas import PageRead  # noqa: E402
from vlm.client import ClaudeCaller, ReaderRefused, ReaderTruncated, describe_error  # noqa: E402
from vlm.page_reader import CachedPageReader, ClaudePageReader, read_many  # noqa: E402

checks = []


def check(name, cond, extra=""):
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra and not cond else ""))


def page_bytes(n: int) -> bytes:
    """A distinct image per n (a different grey, a different size)."""
    buf = io.BytesIO()
    Image.new("RGB", (200 + n, 260), (10 * n % 255, 40, 90)).save(buf, "PNG")
    return buf.getvalue()


class Usage(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


class FakeResponse:
    def __init__(self, marker, model="claude-opus-5", stop_reason="end_turn", parsed=True):
        self.model = model
        self.stop_reason = stop_reason
        self.stop_details = SimpleNamespace(category="bio") if stop_reason == "refusal" else None
        self.usage = Usage(input_tokens=1200, output_tokens=1000,
                           cache_read_input_tokens=3000, cache_creation_input_tokens=0)
        self.parsed_output = PageRead(document_type="prescription", patient_name=marker,
                                      medicines=[], overall_legibility=0.9) if parsed else None
        self._marker = marker

    def model_dump(self, mode="json", warnings=True):
        return {"id": f"msg_{self._marker}", "model": self.model}


class FakeMessages:
    """Records calls. `behaviour(marker)` may return a FakeResponse or raise."""

    def __init__(self, owner, beta=False):
        self.owner, self.beta = owner, beta

    def parse(self, **kw):
        o = self.owner
        if self.beta and o.reject_fallbacks:
            raise bad_request("Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header.")
        data = kw["messages"][0]["content"][0]["source"]["data"]
        marker = hashlib.sha256(data.encode()).hexdigest()[:8]
        with o.lock:
            o.in_flight += 1
            o.peak = max(o.peak, o.in_flight)
            o.calls.append({"marker": marker, "beta": self.beta, "start": time.monotonic(),
                            "fallbacks": kw.get("fallbacks")})
            call = o.calls[-1]
        try:
            time.sleep(o.delay)
            return o.behaviour(marker)
        finally:
            with o.lock:
                o.in_flight -= 1
                call["end"] = time.monotonic()


class FakeClient:
    def __init__(self, delay=0.08):
        self.delay, self.lock = delay, threading.Lock()
        self.calls, self.in_flight, self.peak = [], 0, 0
        self.reject_fallbacks = False
        self.behaviour = lambda marker: FakeResponse(marker)
        self.messages = FakeMessages(self)
        self.beta = SimpleNamespace(messages=FakeMessages(self, beta=True))


def bad_request(msg):
    resp = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(msg, response=resp, body=None)


def fresh(cap=100.0, delay=0.08, concurrency=4, fallbacks=True):
    """A reader over a fake client, with its own cache dir and spend ledger."""
    tmp = Path(tempfile.mkdtemp(prefix="reader-test-"))
    spend._default_guard = spend.SpendGuard(tmp / "ledger.json", cap_usd=cap)
    client = FakeClient(delay)
    caller = ClaudeCaller(client=client, max_concurrency=concurrency, fallbacks=fallbacks)
    reader = CachedPageReader(ClaudePageReader(caller=caller), cache_dir=tmp / "cache")
    return reader, client, tmp


# ── parallel reads keep their own results ───────────────────────────────────
reader, client, tmp = fresh()
pages = [page_bytes(i) for i in range(1, 5)]
results = read_many(reader, pages)
check("4 pages -> 4 results, in order, no failures", len(results) == 4 and not any(isinstance(r, Exception) for r in results))
own = all(r.raw_response["id"] == f"msg_{r.page.patient_name}" for r in results)
check("each page is stored with ITS OWN raw reply (no cross-talk between threads)", own)
check("4 distinct pages -> 4 API calls", len(client.calls) == 4, len(client.calls))
check("real cost recorded per page", all(r.cost_usd > 0 and not r.from_cache for r in results))

# cold prefix: one lead request, then the fan-out
lead, rest = client.calls[0], client.calls[1:]
check("cold prompt cache: the first page finishes before the others start",
      all(c["start"] >= lead["end"] for c in rest))
check("...and the others overlap each other", client.peak >= 2, client.peak)
check("refusal fallback requested by default", all(c["beta"] and c["fallbacks"] == "default" for c in client.calls))

# ── the response cache ──────────────────────────────────────────────────────
again = read_many(reader, pages)
check("same 4 pages again -> 0 new API calls", len(client.calls) == 4, len(client.calls))
check("cache hit: cost 0, from_cache, and its own raw reply",
      all(r.from_cache and r.cost_usd == 0 and r.raw_response["id"] == f"msg_{r.page.patient_name}" for r in again))
mode = stat.S_IMODE(os.stat(next((tmp / "cache").glob("*.json"))).st_mode)
check("cache files are owner-only (they hold patient data)", mode == 0o600 or os.name == "nt", oct(mode))

# warm prefix: no lead needed
more = [page_bytes(i) for i in range(10, 14)]
n0 = len(client.calls)
client.peak = 0
read_many(reader, more)
new = client.calls[n0:]
check("warm prompt cache: all pages start together (no lead)",
      sum(1 for c in new if c["start"] < min(x["end"] for x in new)) >= 2 and client.peak >= 2)

# ── duplicates in flight ────────────────────────────────────────────────────
reader, client, _ = fresh(delay=0.15)
same = page_bytes(7)
results = read_many(reader, [same] * 5)
check("the same image 5 times at once -> ONE API call (single-flight)", len(client.calls) == 1, len(client.calls))
check("...and all 5 get the reading", all(not isinstance(r, Exception) and r.page.patient_name for r in results))

# ── concurrency cap ─────────────────────────────────────────────────────────
reader, client, _ = fresh(concurrency=2, delay=0.1)
reader.inner.caller._warm[reader.inner._warm_key] = time.monotonic()   # warm: pure fan-out
read_many(reader, [page_bytes(i) for i in range(20, 28)], max_workers=8)
check("never more than READER_MAX_CONCURRENCY calls in flight", client.peak <= 2, client.peak)

# ── one bad page does not lose the others ───────────────────────────────────
reader, client, _ = fresh()
bad = hashlib.sha256(__import__("base64").standard_b64encode(
    __import__("ingest.normalise", fromlist=["normalise"]).normalise(page_bytes(31)).jpeg_bytes).decode().encode()).hexdigest()[:8]


def flaky(marker):
    if marker == bad:
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    return FakeResponse(marker)


client.behaviour = flaky
results = read_many(reader, [page_bytes(30), page_bytes(31), page_bytes(32)])
check("a failed page comes back as its exception; the other two are read",
      isinstance(results[1], anthropic.APIConnectionError) and not isinstance(results[0], Exception)
      and not isinstance(results[2], Exception))
check("a failed read is not cached", not reader.cached(page_bytes(31)) and reader.cached(page_bytes(30)))

# ── refusal / truncation are failures, never readings ───────────────────────
reader, client, _ = fresh()
client.behaviour = lambda m: FakeResponse(m, stop_reason="refusal", parsed=False)
try:
    reader.read_detailed(page_bytes(40))
    check("refusal raises ReaderRefused", False)
except ReaderRefused as exc:
    check("refusal raises ReaderRefused with its category", exc.category == "bio")
check("a refusal is not cached as an empty prescription", not reader.cached(page_bytes(40)))
client.behaviour = lambda m: FakeResponse(m, stop_reason="max_tokens", parsed=False)
try:
    reader.read_detailed(page_bytes(41))
    check("max_tokens raises ReaderTruncated", False)
except ReaderTruncated:
    check("max_tokens raises ReaderTruncated", True)
check("refused / truncated calls are still costed from the usage the API reports",
      len(spend.get_guard()._read()) == 2)

# ── a fallback model answering is recorded as such ──────────────────────────
reader, client, _ = fresh()
client.behaviour = lambda m: FakeResponse(m, model="claude-opus-4-8")
r = reader.read_detailed(page_bytes(45))
check("provenance names the model that ANSWERED", r.model == "claude-opus-4-8")
check("...and the cache hit keeps it", reader.read_detailed(page_bytes(45)).model == "claude-opus-4-8")

# ── the beta parameter being rejected does not break reading ────────────────
reader, client, _ = fresh()
client.reject_fallbacks = True
r = reader.read_detailed(page_bytes(50))
check("fallback parameter rejected (400) -> plain call succeeds", r.page.patient_name and not r.from_cache)
check("...and it is not sent again for the rest of the process", reader.inner.caller._fallbacks is False)
reader.read_detailed(page_bytes(51))
check("next read goes straight to the plain endpoint", client.calls[-1]["beta"] is False)

reader, client, _ = fresh()


def always_400(marker):
    raise bad_request("image is not valid")


client.behaviour = always_400
try:
    reader.read_detailed(page_bytes(52))
    check("a genuinely bad request still raises", False)
except anthropic.BadRequestError:
    check("a genuinely bad request still raises", True)
check("...and does NOT switch the fallback off", reader.inner.caller._fallbacks is True)

# ── the spend cap holds under concurrency ───────────────────────────────────
reader, client, _ = fresh(cap=0.12, delay=0.15)          # room for two 0.05 reservations
reader.inner.caller._warm[reader.inner._warm_key] = time.monotonic()
results = read_many(reader, [page_bytes(i) for i in range(60, 66)], max_workers=6)
refused = [r for r in results if isinstance(r, spend.SpendCapExceeded)]
check("6 parallel reads against a cap with room for 2 -> only 2 reach the API",
      len(client.calls) == 2 and len(refused) == 4, (len(client.calls), len(refused)))

# ── error messages shown to users carry no internals ────────────────────────
status, msg = describe_error(spend.SpendCapExceeded("cap reached; clear /app/ml_pipeline/.cache/spend_ledger.json"))
check("spend cap -> 503 without the ledger path", status == 503 and "ledger" not in msg and "/" not in msg)
resp401 = httpx2.Response(401, request=httpx2.Request("POST", "https://api.anthropic.com"))
status, msg = describe_error(anthropic.AuthenticationError("invalid x-api-key sk-ant-abc", response=resp401, body=None))
check("bad key -> 502 that never echoes the provider's text", status == 502 and "sk-ant" not in msg)
check("refusal -> 422", describe_error(ReaderRefused("bio"))[0] == 422)
check("timeout -> 504", describe_error(anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")))[0] == 504)
check("missing medicine data -> 503", describe_error(FileNotFoundError("x"))[0] == 503)

failed = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
