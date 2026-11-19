"""
S1 through Koncile (https://www.koncile.ai), a hosted document-extraction
service. It is the page reader when READER=koncile, or when READER=auto and
KONCILE_API_KEY is set with no Anthropic key.

Flow, from Koncile's public OpenAPI spec (v1.1.0, https://api.koncile.ai/documentation):

    POST /v1/upload_file/?template_id=..   multipart "files"   -> a task id
    GET  /v1/fetch_tasks_results/?task_id=..                  -> status
         IN PROGRESS | DONE | FAILED | DUPLICATE; when DONE it carries
         General_fields {...} and Line_fields {...}
    POST /v1/check_api_key/                                   -> the free key check
    GET  /v1/fetch_all_folders/                               -> finds the template

WHICH fields come back is set by the template in the Koncile app, not by this
code. config/koncile_fields.yaml maps their names onto PageRead. The spec does
not describe the inside of General_fields / Line_fields, so the parser accepts a
bare value or a {value, confidence} object per field, and line fields that are
column-oriented, row-oriented or a named table.

What a reviewer should know about a page read here (the screens are unchanged):

  * Koncile may normalise what it reads. `raw_reading` is Koncile's value: the
    S1 verbatim rule (vlm/prompts.py) cannot be enforced on a third-party model.
  * There are no `alternatives` (the uncertainty channel) and no line boxes, so
    S2 has one reading to match and the review screen has no per-line crop.
  * Cost is Koncile credits, not dollars. Each call is written to the spend
    ledger at $0 with the tag "koncile:<image>", so the ledger still counts calls.
  * The uploaded file is named after the image hash, never the original name,
    which can carry a patient's name.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env import load_env  # noqa: E402
from schemas import MedicineLine, PageRead  # noqa: E402
from vlm.client import (  # noqa: E402
    ReaderAuthError,
    ReaderBusy,
    ReaderFailed,
    ReaderQuotaExceeded,
    ReaderTimedOut,
    ReaderUnavailable,
)
from vlm.page_reader import PageSource, ReadResult, _as_normalised  # noqa: E402
from vlm.spend import CallCost, get_guard  # noqa: E402

MODEL = "koncile"
# Bump when the same Koncile reply would now become a different PageRead.
MAPPING_VERSION = "k2"   # k2: Koncile's "Dosage" is the schedule; durations get units
FIELDS_PATH = Path(__file__).resolve().parent.parent / "config" / "koncile_fields.yaml"
DEFAULT_CONFIDENCE = 0.5      # a field Koncile returned without a score

_VALUE_KEYS = ("value", "text", "content", "answer", "raw_value", "normalized_value")
_CONF_KEYS = ("confidence_score", "confidence", "score", "probability", "conf")
_EMPTY = {"", "not found", "n/a", "na", "none", "null", "-", "—"}
_FALSE = {"false", "no", "non", "0", "faux"}
_UNITS = (("day", "days"), ("week", "weeks"), ("month", "months"))


def _with_unit(column: Optional[str], value: Optional[str]) -> Optional[str]:
    """A bare number takes its unit from the column name: "3" under
    "Duration of treatment (in days)" reads as "3 days"."""
    if value and column and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        low = column.lower()
        for word, unit in _UNITS:
            if word in low:
                return f"{value.strip()} {unit}"
    return value


def _int_or_none(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_fields(path: Path = FIELDS_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def fields_fingerprint(path: Path = FIELDS_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


# ── matching Koncile's field names ───────────────────────────────────────────


def _tokens(name: str) -> List[str]:
    return "".join(ch if ch.isalnum() else " " for ch in str(name).lower()).split()


def _matches(name: str, words: List[str], exclude: List[str]) -> bool:
    toks = _tokens(name)
    if any(t.startswith(str(x).lower()) for x in exclude for t in toks):
        return False
    return all(any(t.startswith(str(w).lower()) for t in toks) for w in words)


def _pick(names: List[str], rule: Optional[dict], taken: Tuple[str, ...] = ()) -> Optional[str]:
    """The first field name matched by the rule's alternatives, in rule order."""
    rule = rule or {}
    for words in rule.get("match", []):
        for n in names:
            if n not in taken and _matches(n, words, rule.get("exclude", [])):
                return n
    return None


# ── reading one field ────────────────────────────────────────────────────────


def _conf(x: Any) -> Optional[float]:
    try:
        c = float(x)
    except (TypeError, ValueError):
        return None
    if c > 1.0:               # a percentage (the Koncile app shows "89 %")
        c /= 100.0
    return max(0.0, min(1.0, c))


def _is_entry(x: Any) -> bool:
    """One field value: a scalar, or an object carrying a value key."""
    return not isinstance(x, (dict, list)) or (isinstance(x, dict) and any(k in x for k in _VALUE_KEYS))


def _value_conf(entry: Any) -> Tuple[Optional[str], Optional[float]]:
    """(text, confidence 0-1) of one field, whatever shape Koncile used."""
    if entry is None:
        return None, None
    if isinstance(entry, list):
        parts = [_value_conf(e) for e in entry]
        vals = [v for v, _ in parts if v]
        confs = [c for _, c in parts if c is not None]
        return (", ".join(vals) or None), (min(confs) if confs else None)
    c = None
    if isinstance(entry, dict):
        v = next((entry[k] for k in _VALUE_KEYS if k in entry), None)
        c = next((_conf(entry[k]) for k in _CONF_KEYS if entry.get(k) is not None), None)
        if isinstance(v, (dict, list)):
            v, inner = _value_conf(v)
            c = c if c is not None else inner
    else:
        v = entry
    if v is None:
        return None, c
    s = str(v).strip()
    return (None if s.lower() in _EMPTY else s), c


def _rows(line_fields: Any, cfg: dict) -> List[Dict[str, Any]]:
    """Medicine rows as {column name: entry}, for any of the three layouts."""
    if not line_fields:
        return []
    if isinstance(line_fields, list):                       # [{col: entry}, ...]
        return [r for r in line_fields if isinstance(r, dict)]
    if not isinstance(line_fields, dict):
        return []
    lists = {k: v for k, v in line_fields.items() if isinstance(v, list)}
    tables = {k: v for k, v in lists.items()
              if v and all(isinstance(r, dict) and not _is_entry(r) for r in v)}
    if tables:                                              # {"Medications": [{col: entry}, ...]}
        name = _pick(list(tables), cfg.get("table")) or next(iter(tables))
        return tables[name]
    n = max((len(v) for v in lists.values()), default=0)    # {col: [row0, row1, ...]}
    return [{k: (v[i] if i < len(v) else None) for k, v in lists.items()} for i in range(n)]


# ── the whole reply -> PageRead ──────────────────────────────────────────────


def to_page_read(result: dict, cfg: dict) -> PageRead:
    general = result.get("General_fields") if isinstance(result.get("General_fields"), dict) else {}
    g = {name: _value_conf(e) for name, e in general.items()}
    names = list(g)
    G = cfg.get("general", {})

    def gval(target: str) -> Tuple[Optional[str], Optional[float]]:
        n = _pick(names, G.get(target))
        return g[n] if n else (None, None)

    is_rx, _ = gval("is_prescription")
    not_rx = (is_rx or "").strip().lower() in _FALSE
    confs = [c for _, c in g.values() if c is not None]

    lines: List[MedicineLine] = []
    L = cfg.get("lines", {})
    for row in _rows(result.get("Line_fields"), cfg):
        cells = {name: _value_conf(e) for name, e in row.items()}
        cols = list(cells)
        taken: Tuple[str, ...] = ()
        picked: Dict[str, Tuple[Optional[str], Optional[float]]] = {}
        column: Dict[str, Optional[str]] = {}
        for target in ("drug", "dosage", "frequency", "duration", "instructions"):
            n = _pick(cols, L.get(target), taken)
            picked[target] = cells[n] if n else (None, None)
            column[target] = n
            taken += (n,) if n else ()
        if not any(v for v, _ in cells.values()):
            continue
        row_confs = [c for _, c in cells.values() if c is not None]
        confs += row_confs
        drug, dconf = picked["drug"]
        lines.append(MedicineLine(
            line_index=len(lines),
            # Every cell, in Koncile's column order: the closest thing to the line as written.
            raw_text=" ".join(v for v, _ in cells.values() if v),
            drug_token=drug,
            alternatives=[],
            dosage=picked["dosage"][0], frequency=picked["frequency"][0],
            duration=_with_unit(column["duration"], picked["duration"][0]),
            instructions=picked["instructions"][0],
            confidence=dconf if dconf is not None else (min(row_confs) if row_confs else DEFAULT_CONFIDENCE),
            legible=bool(drug),
        ))

    if not lines:
        # No line fields: a general field may hold the medicines, one per item / line.
        n = _pick(names, G.get("medicines"))
        if n:
            raw = general[n]
            items = raw if isinstance(raw, list) else [ln for ln in str(_value_conf(raw)[0] or "").splitlines()]
            for item in items:
                v, c = _value_conf(item)
                if v:
                    lines.append(MedicineLine(line_index=len(lines), raw_text=v, drug_token=v, alternatives=[],
                                              confidence=c if c is not None else (g[n][1] or DEFAULT_CONFIDENCE),
                                              legible=True))

    notes = []
    if not general and not lines:
        notes.append("Koncile returned no fields for this page.")
    return PageRead(
        document_type="not_a_prescription" if not_rx else "prescription",
        patient_name=gval("patient_name")[0],
        prescriber_name=gval("prescriber_name")[0],
        date=gval("date")[0],
        diagnosis=gval("diagnosis")[0],
        # A page that is not a prescription lists no medicines (PageRead contract, §6.6).
        medicines=[] if not_rx else lines,
        unreadable_regions=notes,
        overall_legibility=sum(confs) / len(confs) if confs else DEFAULT_CONFIDENCE,
    )


def _task_ids(body: Any) -> List[str]:
    """The task id(s) in an upload reply. The spec leaves that reply untyped."""
    if isinstance(body, (str, int)) and str(body):
        return [str(body)]
    if isinstance(body, list):
        return [t for b in body for t in _task_ids(b)]
    if isinstance(body, dict):
        for k in ("task_ids", "task_id", "tasks", "taskIds", "id"):
            if body.get(k):
                return _task_ids(body[k])
    return []


# ── the reader ───────────────────────────────────────────────────────────────


class KoncilePageReader:
    """PageReader over the Koncile API. Thread-safe: nothing about one page is
    kept on the instance."""

    model = MODEL
    effort = None
    prompt_version = f"koncile-{MAPPING_VERSION}"

    def __init__(self, api_key: Optional[str] = None, template_id: Optional[int] = None,
                 folder_id: Optional[int] = None, client: Optional[httpx.Client] = None,
                 poll_seconds: Optional[float] = None, timeout_seconds: Optional[float] = None,
                 max_retries: Optional[int] = None, max_concurrency: Optional[int] = None,
                 fields_path: Path = FIELDS_PATH, sleep=time.sleep):
        load_env()
        self.api_key = (api_key if api_key is not None else os.environ.get("KONCILE_API_KEY") or "").strip()
        env_template = _int_or_none(os.environ.get("KONCILE_TEMPLATE_ID"))
        self.template_id = template_id if template_id is not None else env_template
        self.folder_id = folder_id if folder_id is not None else _int_or_none(os.environ.get("KONCILE_FOLDER_ID"))
        self.http = client or httpx.Client(base_url=os.environ.get("KONCILE_BASE_URL", "https://api.koncile.ai"),
                                           timeout=httpx.Timeout(60.0, connect=10.0))
        self.poll = poll_seconds or float(os.environ.get("KONCILE_POLL_SECONDS", "2"))
        self.deadline = timeout_seconds or float(os.environ.get("KONCILE_TIMEOUT_SECONDS", "180"))
        self.retries = max_retries if max_retries is not None else int(os.environ.get("READER_MAX_RETRIES", "2"))
        self._slots = threading.BoundedSemaphore(
            max_concurrency or int(os.environ.get("READER_MAX_CONCURRENCY", "4")))
        self.fields_path = Path(fields_path)
        self.cfg = load_fields(self.fields_path)
        self._template_lock = threading.Lock()
        self._template_resolved = self.template_id is not None
        # What identifies the template in the cache key, fixed at start-up (a
        # discovered id would change the key between the first and later pages).
        self._template_key = f"t{self.template_id}" if self.template_id is not None \
            else f"name-{self.cfg.get('template_name', 'any')}"
        self.sleep = sleep

    # ── PageReader ───────────────────────────────────────────────────────

    def cache_identity(self) -> str:
        """Everything that changes what a page reads as (CachedPageReader key)."""
        return f"{MODEL}-{MAPPING_VERSION}-{fields_fingerprint(self.fields_path)}-{self._template_key}"

    def read(self, source: PageSource) -> PageRead:
        return self.read_detailed(source).page

    def read_detailed(self, source: PageSource) -> ReadResult:
        img = _as_normalised(source)
        if not self.api_key:
            raise ReaderAuthError("KONCILE_API_KEY is not set.")
        with self._slots:
            self._resolve_template()
            task_id = self._upload(img)
            result = self._wait(task_id)
        page = to_page_read(result, self.cfg)
        get_guard().record(CallCost(MODEL, 0, 0, 0, 0, 0.0), tag=f"koncile:{img.sha256[:12]}")
        return ReadResult(
            page=page, image_sha256=img.sha256, model=MODEL, effort=None,
            raw_response={"task_id": task_id, "result": result},
            usage={"provider": "koncile", "pages": 1}, cost_usd=0.0,
            prompt_version=self.prompt_version,
        )

    def check_access(self, model: Optional[str] = None) -> str:
        """ok | auth_error | unreachable. The free key check, run at start-up."""
        if not self.api_key:
            return "auth_error"
        try:
            r = self._send("POST", "/v1/check_api_key/", idempotent=True)
        except ReaderAuthError:
            return "auth_error"
        except Exception:
            return "unreachable"
        try:
            body = r.json()
        except ValueError:
            return "ok"
        if isinstance(body, dict) and any(body.get(k) is False for k in ("valid", "is_valid", "success")):
            return "auth_error"
        return "ok"

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _send(self, method: str, url: str, *, idempotent: bool, **kw) -> httpx.Response:
        """One request with bounded retries. An upload (not idempotent) is retried
        only when it certainly did not register: no connection, 429, or 503.
        A repeated upload would spend credits twice."""
        attempt = 0
        while True:
            try:
                r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.api_key}"}, **kw)
            except httpx.ConnectError as exc:
                if attempt < self.retries:
                    attempt += 1
                    self.sleep(2 ** attempt)
                    continue
                raise ReaderUnavailable("Koncile could not be reached.") from exc
            except httpx.TimeoutException as exc:
                if idempotent and attempt < self.retries:
                    attempt += 1
                    self.sleep(2 ** attempt)
                    continue
                raise ReaderTimedOut("Koncile did not answer in time.") from exc
            retryable = r.status_code in (429, 503) or (idempotent and r.status_code >= 500)
            if retryable and attempt < self.retries:
                attempt += 1
                self.sleep(float(r.headers.get("Retry-After", 2 ** attempt)) if
                           str(r.headers.get("Retry-After", "")).isdigit() else 2 ** attempt)
                continue
            return self._check(r)

    @staticmethod
    def _check(r: httpx.Response) -> httpx.Response:
        if r.status_code < 400:
            return r
        text = (r.text or "")[:300]
        if r.status_code in (400, 402, 403) and ("credit" in text.lower() or "quota" in text.lower()):
            raise ReaderQuotaExceeded(f"Koncile: credits or quota used up (HTTP {r.status_code}): {text}")
        if r.status_code in (401, 403):
            raise ReaderAuthError(f"Koncile rejected the API key (HTTP {r.status_code}).")
        if r.status_code == 402:
            raise ReaderQuotaExceeded(f"Koncile: payment required (HTTP 402): {text}")
        if r.status_code == 429:
            raise ReaderBusy("Koncile rate limit (HTTP 429).")
        if r.status_code >= 500:
            raise ReaderUnavailable(f"Koncile server error (HTTP {r.status_code}).")
        raise ReaderFailed(f"Koncile refused the request (HTTP {r.status_code}): {text}")

    # ── the steps ────────────────────────────────────────────────────────

    def _resolve_template(self) -> None:
        """Find the template named in koncile_fields.yaml, once per process."""
        if self._template_resolved:
            return
        with self._template_lock:
            if self._template_resolved:
                return
            want = str(self.cfg.get("template_name", "")).lower()
            try:
                folders = self._send("GET", "/v1/fetch_all_folders/", idempotent=True).json().get("folders") or []
            except (ReaderAuthError, ReaderQuotaExceeded):
                raise
            except Exception as exc:
                print(f"[KONCILE] could not list folders ({type(exc).__name__}); uploading without a template",
                      flush=True)
                folders = []
            for f in folders:
                for t in f.get("templates") or []:
                    if want and want in str(t.get("name", "")).lower():
                        self.template_id, self.folder_id = t.get("id"), f.get("id")
                        print(f"[KONCILE] using template {t.get('name')!r} (id {self.template_id}) "
                              f"in folder {f.get('name')!r}", flush=True)
                        break
                if self.template_id is not None:
                    break
            if self.template_id is None:
                print(f"[KONCILE] WARNING: no template named like {want!r} in your Koncile folders; "
                      f"Koncile will classify each page itself. Set KONCILE_TEMPLATE_ID to pin one.", flush=True)
            self._template_resolved = True

    def _upload(self, img) -> str:
        params = {k: v for k, v in (("template_id", self.template_id), ("folder_id", self.folder_id)) if v is not None}
        files = [("files", (f"page-{img.sha256[:16]}.jpg", img.jpeg_bytes, "image/jpeg"))]
        body = self._send("POST", "/v1/upload_file/", idempotent=False, params=params, files=files).json()
        ids = _task_ids(body)
        if not ids:
            raise ReaderFailed(f"Koncile accepted the upload but returned no task id: {str(body)[:200]}")
        return ids[0]

    def _wait(self, task_id: str) -> dict:
        waited, seen = 0.0, set()
        while True:
            r = self._send("GET", "/v1/fetch_tasks_results/", idempotent=True, params={"task_id": task_id}).json()
            status = str(r.get("status", "")).upper()
            if status == "DONE":
                children = r.get("child_task_ids") or []
                if children and not (r.get("General_fields") or r.get("Line_fields")):
                    # The page was split into several documents: use the first one with fields.
                    for child in children:
                        out = self._wait(child)
                        if out.get("General_fields") or out.get("Line_fields"):
                            return out
                return r
            if status == "FAILED":
                raise ReaderFailed(f"Koncile could not process the page: {r.get('status_message', '')}")
            if status == "DUPLICATE":
                original = r.get("duplicate_of_task_id")
                if not original or original in seen:
                    raise ReaderFailed("Koncile reported a duplicate without a usable original task.")
                seen.add(task_id)
                task_id = original           # the earlier reading of the same file
                continue
            if waited >= self.deadline:
                raise ReaderTimedOut(f"Koncile task {task_id} still {status or 'pending'} after {self.deadline:.0f}s.")
            self.sleep(self.poll)
            waited += self.poll
