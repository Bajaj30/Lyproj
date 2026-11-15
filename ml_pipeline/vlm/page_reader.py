"""
S1 — VLM page read.  (docs/ARCHITECTURE_V2.md §5/S1, §6.4)

The reader takes a normalised page and returns a schema-guaranteed `PageRead`.
It is the only stage that talks to a model.

Three implementations of one Protocol:

  MockPageReader    — fixed realistic output. No API, no key, no cost.
                      Ships FIRST so Tracks B and C are never blocked (DAY1 §3/A1).
  ClaudePageReader  — the real one, Opus 5 @ effort:medium.
  CachedPageReader  — wraps any reader with an on-disk response cache.

The cache is not an optimisation, it is what makes the testing budget work:
at a 70% hit rate the phase costs $7.38 instead of $24.58 (§14.2). Most
development re-runs exercise S2-S4 and the UI against an UNCHANGED VLM output;
those must cost nothing.

Every reader returns a `ReadResult` from `read_detailed()`: the page PLUS the
raw reply, usage, cost and which model answered. Earlier the reader kept "the
last call's" metadata on itself, which is wrong the moment two pages are read
at once (page A stored with page B's raw reply), and was already wrong for a
cache hit (stored with the PREVIOUS miss's raw reply and cost). `read()` stays
as a thin wrapper for the CLI. `read_many()` reads several pages in parallel.
See docs/API_IMPLEMENTATION.md.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Union, runtime_checkable

from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env import load_env  # noqa: E402
from ingest.normalise import NormalisedImage, normalise  # noqa: E402
from schemas import MedicineLine, PageRead  # noqa: E402
from vlm.prompts import (  # noqa: E402
    PAGE_READER_SYSTEM_PROMPT,
    PROMPT_VERSION,
    USER_TURN_TEXT,
    prompt_fingerprint,
)
from vlm.spend import get_guard  # noqa: E402

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"  # §6.5 — effort is the dominant cost lever
MAX_TOKENS = 16000  # never lower this to save money: a truncated PageRead is a failed read

_ML_DIR = Path(__file__).resolve().parent.parent
# PRESCRIPTAI_CACHE_DIR moves the read cache and the spend ledger together
# (a mounted volume in production; a temp directory under test).
CACHE_DIR = Path(os.environ.get("PRESCRIPTAI_CACHE_DIR") or _ML_DIR / ".cache") / "vlm"

PageSource = Union[str, Path, bytes, Image.Image, NormalisedImage]


def _as_normalised(source: PageSource) -> NormalisedImage:
    return source if isinstance(source, NormalisedImage) else normalise(source)


@dataclass
class ReadResult:
    """One page read, with everything the correction log must capture at write
    time (models.py §16.3). Self-contained: safe to produce concurrently."""

    page: PageRead
    image_sha256: str
    model: str                         # the model that ANSWERED (after any fallback)
    effort: Optional[str] = None
    raw_response: Optional[dict] = None
    usage: Optional[dict] = None
    cost_usd: float = 0.0              # 0 for a cache hit: nothing was spent
    from_cache: bool = False


@runtime_checkable
class PageReader(Protocol):
    """Swappable so hosted and self-hosted paths are interchangeable (§10)."""

    model: str

    def read(self, source: PageSource) -> PageRead: ...

    def read_detailed(self, source: PageSource) -> ReadResult: ...


# ─────────────────────────────────────────────────────────────────────────────
# Mock — built first, on purpose
# ─────────────────────────────────────────────────────────────────────────────


class MockPageReader:
    """Fixed, realistic output. Modelled on VLM_CHECK/2.jpg (a real eval image).

    The third medicine is deliberately ILLEGIBLE. Track C must build the
    abstention UI from the start rather than bolting it on once the real reader
    lands — a mock where everything succeeds produces a UI that cannot express
    failure.
    """

    def __init__(self, document_type: str = "prescription"):
        self.document_type = document_type
        # `model` is the reader's cache identity, so two mock variants must not
        # share it — otherwise the negative fixture serves the positive one's
        # cached entry. Anything that changes the output belongs in the key.
        self.model = f"mock-{document_type.replace('_', '-')}"

    def read(self, source: PageSource) -> PageRead:
        return self.read_detailed(source).page

    def read_detailed(self, source: PageSource) -> ReadResult:
        img = _as_normalised(source)  # exercise the real S0 path so bugs surface here too
        return ReadResult(page=self._page(), image_sha256=img.sha256, model=self.model)

    def _page(self) -> PageRead:
        if self.document_type == "not_a_prescription":
            # The hard-negative case: a clinical note with no medications.
            return PageRead(
                document_type="not_a_prescription",
                patient_name=None,
                prescriber_name=None,
                date=None,
                medicines=[],
                unreadable_regions=[
                    "Gynaecology progress note. No medications are prescribed on this page."
                ],
                overall_legibility=0.62,
            )

        return PageRead(
            document_type="prescription",
            patient_name="Ms. Prathna",
            prescriber_name="Dr. R. Keshwani",
            date="15-03-17",
            diagnosis="AGE",
            medicines=[
                MedicineLine(
                    line_index=0,
                    raw_text="1. TAB OFLAZEST OZ - (6)  1 ---- 1",
                    drug_token="OFLAZEST OZ",
                    alternatives=[],
                    dosage=None,
                    frequency="1-0-1",
                    duration="3 days",
                    instructions=None,
                    confidence=0.93,
                    legible=True,
                    bbox=[0.08, 0.30, 0.55, 0.36],
                ),
                MedicineLine(
                    line_index=1,
                    raw_text="2. TAB AZENAC-MR - (6)  1 ---- 1",
                    drug_token="AZENAC-MR",
                    alternatives=["ANEVAC-MR"],
                    dosage=None,
                    frequency="1-0-1",
                    duration="3 days",
                    instructions=None,
                    confidence=0.71,
                    legible=True,
                    bbox=[0.08, 0.38, 0.55, 0.44],
                ),
                MedicineLine(
                    line_index=2,
                    raw_text="4. TAB ZOFER u-r - (6)  1 ---- 1",
                    drug_token=None,
                    alternatives=["ZOFER 4mg", "ZOFER 4-V"],
                    dosage=None,
                    frequency="1-0-1",
                    duration="3 days",
                    instructions=None,
                    confidence=0.34,
                    legible=False,
                    bbox=[0.08, 0.54, 0.55, 0.60],
                ),
            ],
            unreadable_regions=[
                "Strength suffix on line 4 ('u-r') is unclear.",
                "A brace groups all four drugs with the note '3 days'.",
            ],
            overall_legibility=0.74,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Response cache
# ─────────────────────────────────────────────────────────────────────────────


def _owner_only(path: Path, directory: bool = False) -> None:
    """chmod 700 / 600. Best effort: a filesystem without POSIX modes (Windows)
    is left as it is rather than failing the read."""
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        pass


class CachedPageReader:
    """On-disk response cache around any PageReader.

    Key: (image_sha256, prompt_version, prompt_fingerprint, model, effort).

    `prompt_fingerprint` is in the key on purpose — editing the prompt without
    bumping PROMPT_VERSION would otherwise serve stale reads forever, which is
    the single most confusing bug this design can produce.

    The raw model response is stored alongside the parsed one because §16.3
    needs it for the correction log, and it cannot be reconstructed later.
    """

    def __init__(self, inner: PageReader, cache_dir: Path = CACHE_DIR, enabled: bool = True):
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # The cache holds patient names and prescriptions (PHI): owner-only.
        _owner_only(self.cache_dir, directory=True)
        self.hits = 0
        self.misses = 0
        self._flights: Dict[str, threading.Lock] = {}
        self._flight_lock = threading.Lock()

    @property
    def model(self) -> str:
        return self.inner.model

    @staticmethod
    def _safe(part: str) -> str:
        """Cache keys become filenames, so keep them to a filesystem-safe set."""
        return "".join(c if (c.isalnum() or c in "-.") else "-" for c in str(part))

    def _key(self, img: NormalisedImage) -> str:
        effort = getattr(self.inner, "effort", None) or "none"
        return "_".join(
            self._safe(p)
            for p in (
                img.sha256[:16],
                PROMPT_VERSION,
                prompt_fingerprint(),
                self.inner.model,
                effort,
            )
        )

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def read(self, source: PageSource) -> PageRead:
        return self.read_detailed(source).page

    def cached(self, source: PageSource) -> bool:
        """Whether this page would be served without calling the inner reader."""
        return self.enabled and self._path(self._key(_as_normalised(source))).exists()

    def _load(self, path: Path, img: NormalisedImage) -> ReadResult:
        payload = json.loads(path.read_text())
        return ReadResult(
            page=PageRead.model_validate(payload["page_read"]), image_sha256=img.sha256,
            model=payload.get("served_by") or payload.get("model") or self.inner.model,
            effort=payload.get("effort"),
            # The cached entry carries ITS OWN raw reply and usage, so a page
            # served from cache is stored with the reply that produced it.
            raw_response=payload.get("raw_response"), usage=payload.get("usage"),
            cost_usd=0.0, from_cache=True,
        )

    def read_detailed(self, source: PageSource) -> ReadResult:
        img = _as_normalised(source)

        if not self.enabled:
            return self.inner.read_detailed(img)

        key = self._key(img)
        path = self._path(key)
        # Single-flight: two requests for the same page at the same moment (a
        # double click, a retry, the same photo twice in one upload) make ONE
        # call. The second waits here and then reads what the first wrote.
        with self._flight_lock:
            lock = self._flights.setdefault(key, threading.Lock())
        with lock:
            if path.exists():
                with self._flight_lock:
                    self.hits += 1
                return self._load(path, img)

            with self._flight_lock:
                self.misses += 1
            result = self.inner.read_detailed(img)

            payload = json.dumps(
                {
                    "page_read": result.page.model_dump(),
                    "raw_response": result.raw_response,
                    "usage": result.usage,
                    "model": self.inner.model,
                    "served_by": result.model,
                    "effort": result.effort,
                    "prompt_version": PROMPT_VERSION,
                    "prompt_fingerprint": prompt_fingerprint(),
                    "image_sha256": img.sha256,
                    "image_size": [img.width, img.height],
                },
                indent=2,
                default=str,
            )
            # Written whole or not at all: a crash mid-write must not leave a
            # truncated entry that every later read of this page trips over.
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(payload)
            _owner_only(tmp)
            os.replace(tmp, path)
            return result

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = 100 * self.hits / total if total else 0.0
        return f"cache: {self.hits} hits / {self.misses} misses ({rate:.0f}% hit rate)"


# ─────────────────────────────────────────────────────────────────────────────
# The real reader
# ─────────────────────────────────────────────────────────────────────────────


class ClaudePageReader:
    """Opus 5 @ effort:medium, structured output guaranteed by the schema."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        enforce_spend_cap: bool = True,
        caller=None,
    ):
        from vlm.client import ClaudeCaller

        # Reads the project-root .env if present; a real env var still wins.
        load_env()
        self.caller = caller or ClaudeCaller()
        self.model = model
        self.effort = effort
        self.enforce_spend_cap = enforce_spend_cap

    @property
    def _warm_key(self) -> str:
        return f"{self.model}:{self.effort}:{prompt_fingerprint()}"

    def prefix_is_warm(self) -> bool:
        return self.caller.prefix_is_warm(self._warm_key)

    def read(self, source: PageSource) -> PageRead:
        return self.read_detailed(source).page

    def read_detailed(self, source: PageSource) -> ReadResult:
        img = _as_normalised(source)
        img_b64 = base64.standard_b64encode(img.jpeg_bytes).decode("utf-8")

        done = self.caller.parse(
            warm_key=self._warm_key,
            spend_tag=img.sha256[:12],
            enforce_spend_cap=self.enforce_spend_cap,
            model=self.model,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            # The system prompt is the stable prefix and carries the cache
            # breakpoint. The image comes after it, so a different page never
            # invalidates the cached prompt.
            system=[
                {
                    "type": "text",
                    "text": PAGE_READER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_b64,
                            },
                        },
                        {"type": "text", "text": USER_TURN_TEXT},
                    ],
                }
            ],
            output_format=PageRead,
        )
        # Keep the raw response: §16.3 needs it and it cannot be rebuilt later.
        return ReadResult(
            page=done.parsed, image_sha256=img.sha256, model=done.served_by, effort=self.effort,
            raw_response=done.raw_response, usage=done.usage, cost_usd=done.cost.usd,
        )


def read_many(reader: PageReader, sources: Sequence[PageSource],
              max_workers: Optional[int] = None) -> List[Union[ReadResult, Exception]]:
    """Read several pages at once. Results come back in input order; a page that
    fails is returned as its exception, so one bad page does not lose the rest.

    Three things keep this cheap:

      * Pages already in the response cache never reach the API.
      * The same image twice in one call is read once (single-flight).
      * PROMPT-CACHE LEAD. A cache entry only becomes readable once the first
        response has started, so N cold requests fired together all pay the
        full price for the system prompt and none reads what the others are
        writing. When the prefix is cold, ONE page goes first and the rest fan
        out behind it and read the cache it wrote.
    """
    imgs = [_as_normalised(s) for s in sources]
    results: List[Union[ReadResult, Exception, None]] = [None] * len(imgs)
    if not imgs:
        return []

    def one(i: int) -> None:
        try:
            results[i] = reader.read_detailed(imgs[i])
        except Exception as exc:  # returned to the caller, per page
            results[i] = exc

    inner = getattr(reader, "inner", reader)
    is_cached = getattr(reader, "cached", lambda _img: False)
    order = list(range(len(imgs)))
    needs_api = [i for i in order if not is_cached(imgs[i])]
    warm = getattr(inner, "prefix_is_warm", lambda: True)

    if len(needs_api) > 1 and not warm():
        lead = needs_api[0]
        one(lead)
        order.remove(lead)

    workers = max_workers or int(os.environ.get("READER_MAX_CONCURRENCY", "4"))
    if len(order) <= 1 or workers <= 1:
        for i in order:
            one(i)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            list(pool.map(one, order))
    return results  # type: ignore[return-value]


def resolve_reader_kind(kind: str = "auto") -> str:
    """"auto" means: the real reader when an API key is configured, else the mock.

    This is the slot the API key drops into. With no key the whole product still
    runs end to end on the mock reader, at zero cost, and says so (/api/health
    reports `reader`). Add ANTHROPIC_API_KEY to .env and restart: nothing else
    changes.
    """
    if kind != "auto":
        return kind
    from env import api_key_configured

    return "claude" if api_key_configured() else "mock"


def build_reader(
    kind: str = "claude",
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    cache: bool = True,
    caller=None,
) -> PageReader:
    """One place to construct the reader, so swapping it is a single argument."""
    inner: PageReader
    kind = resolve_reader_kind(kind)
    if kind == "mock":
        inner = MockPageReader()
    elif kind == "mock_negative":
        inner = MockPageReader(document_type="not_a_prescription")
    elif kind == "claude":
        inner = ClaudePageReader(model=model, effort=effort, caller=caller)
    else:
        raise ValueError(f"unknown reader kind {kind!r}")
    return CachedPageReader(inner) if cache else inner


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run S1 on one or more pages.")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--reader", default="auto", choices=["auto", "claude", "mock", "mock_negative"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default=DEFAULT_EFFORT)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    reader = build_reader(args.reader, args.model, args.effort, cache=not args.no_cache)

    for path in args.images:
        page = reader.read(path)
        print(f"\n=== {Path(path).name} ===")
        print(f"document_type    : {page.document_type}")
        print(f"legibility       : {page.overall_legibility}")
        print(f"patient          : {page.patient_name}")
        print(f"prescriber       : {page.prescriber_name}")
        print(f"medicines        : {len(page.medicines)}")
        for m in page.medicines:
            flag = "" if m.legible else "   <- ILLEGIBLE"
            alts = f"  alts={m.alternatives}" if m.alternatives else ""
            print(f"   [{m.confidence:.2f}] {m.raw_text!r}{alts}{flag}")
        for note in page.unreadable_regions:
            print(f"   note: {note}")

    if isinstance(reader, CachedPageReader):
        print(f"\n{reader.stats()}")
    print(get_guard().summary())
