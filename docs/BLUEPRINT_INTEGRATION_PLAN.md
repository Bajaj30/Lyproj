# Reader integration plan: bringing the blueprint into PrescriptAI

**Status:** Proposal, 19 Nov 2026. Nothing in this document is built yet.
**Source:** [`MedScript — Production Architecture Blueprint.md`](../MedScript%20—%20Production%20Architecture%20Blueprint.md)
(the "blueprint", project root, untracked).
**Read with:** [`API_IMPLEMENTATION.md`](API_IMPLEMENTATION.md) (how the reader calls the API today),
[`ARCHITECTURE_V2.md`](ARCHITECTURE_V2.md) (§ references below), [`claude.md`](../claude.md) (journal).

---

## 1. Where we actually are

**Reading handwriting through an API is already built.** It has not been run live since Session 10.

| Fact | Evidence |
|---|---|
| A real API reader exists | `ClaudePageReader` in `ml_pipeline/vlm/page_reader.py`; every call goes through `ml_pipeline/vlm/client.py` |
| It is wired into the product | `READER=auto` (the default) picks Claude whenever `ANTHROPIC_API_KEY` is set |
| The key on this machine works | Checked 19 Nov 2026 with the free `models.retrieve` call: `claude-opus-5` and `claude-opus-5-5` both available |
| It has read real prescriptions | Session 4: 2 live calls, `VLM_CHECK/2.jpg` read correctly line by line (claude.md) |
| What has **not** happened | No live call since the Session 10 rewrite, and none on prompt v2. Checklist: API_IMPLEMENTATION §1.9 |

What one read already produces, per medicine line: `raw_text` (verbatim), `drug_token`,
`alternatives`, `dosage`, `frequency`, `duration`, `instructions`, `confidence`, `legible`
and `bbox`. S2–S4 then add the matched brand, ingredients, NLEM verification and the
interaction check.

**What is genuinely missing for "handwritten text → normal text in a table":**

1. **Running it.** Phase 0: no code, under $0.50.
2. **A table.** The review screen renders one card per medicine (`MedicineCard`). The
   only `<table>` in the app is the pharmacy's dispensing records. Phase 1.
3. **Normalised columns.** The reading is verbatim by design ("1-0-1", "BD"). A "per day"
   column needs the deterministic sig parser from blueprint §4.2. Phase 4.

The blueprint is not needed to make conversion work. It contributes a second provider
(Gemini), extra evidence (OCR and a second read) and deterministic clinical checks. This
plan takes those in without disturbing what works.

---

## 2. Rules for this work

These hold for every phase. Each one exists to protect something that works today.

1. **Defaults do not change.** `READER=auto` keeps resolving to Claude when the Anthropic
   key is set. Every new capability is off until a setting turns it on.
2. **`PageRead` is the boundary.** Every new reader returns a `PageRead`. S2–S5, the
   database, the review UI and the PDF do not learn that a new provider exists.
3. **Safety additions only downgrade.** Look-alike guard, high-alert list, OCR
   disagreement and second-read disagreement can lower an outcome (CONFIRMED → PROBABLE
   or AMBIGUOUS). Nothing new can raise one.
4. **No auto-approval.** The blueprint's "Green: auto-approved" status does not apply in
   Phase 1 (Workflow.md stage 3): a doctor reviews every page. Its gates become review
   reasons, never a bypass.
5. **Existing cache entries keep their keys.** New readers supply their own cache
   identity. Claude's key is computed exactly as now.
6. **One spend ledger, one cap, every provider.** Every call reserves before it starts.
   An unknown model raises; it is never priced at $0 (`vlm/spend.py`).
7. **Thresholds are per reading model.** Today's numbers stay the Claude profile.
   A model with no calibrated profile can never reach CONFIRMED.
8. **Database changes are additive.** New columns are nullable and come with a new
   Alembic migration. Nothing is altered or dropped.
9. **Optional dependencies are imported lazily.** A missing Google package only matters
   when the Google reader is selected, and then it fails at startup with a clear message.
10. **The existing suite passes at every step**, unchanged: `python tests/run.py` (89 unit,
    137 + 6 API, 50 browser). New features add their own tests.
11. **No real patient page goes to a free tier.** The blueprint's rule (Gemini's unpaid
    tier may use content to improve Google's products) becomes a startup check.
12. **Vocabulary stays out of the S1 prompt** (ARCHITECTURE_V2 §5, S1). This rules out
    one blueprint item, Tier C as written (see §3).

---

## 3. The blueprint, item by item

**Adopt** = fits as written. **Adapt** = fits with changes that respect §2.
**Defer** = needs data, money or a decision first. **Not adopting** = conflicts with a
deliberate, recorded decision.

| Blueprint item | Verdict | How it lands here |
|---|---|---|
| Model-agnostic adapter, pinned versions (Decision 4) | **Adopt** | Already the `PageReader` Protocol. Add `GeminiPageReader` behind it (Phase 3) |
| Gemini Flash / Flash-Lite / Pro tiers (Stage 2) | **Adapt** | Gemini added as a second reader, selectable by `READER`. Tier routing deferred (Phase 5e) |
| Extraction schema with `Field` objects (Stage 3) | **Adapt** | Gemini fills a private wire schema converted to `PageRead`. Useful extra fields join `PageRead` later as optional fields (Phase 6) |
| Prompt rules (Stage 3) | **Adapt** | Gemini uses **our** S1 rules, so the two readers are comparable. Blueprint additions (sig shorthand copied verbatim, Hindi → `instructions_english`, struck-out lines) land in prompt v3 (Phase 6) |
| `box_2d` [ymin,xmin,ymax,xmax] 0–1000 | **Adapt** | Converted to our `bbox` [x0,y0,x1,y1] 0–1 inside the adapter |
| Two independent Tier B reads (Decision 1) | **Adapt, evidence-gated** | `ConsensusReader` merges two reads into one `PageRead` through the existing `alternatives` channel (Phase 5d). This reverses the recorded "dual-VLM cross-check deferred", so it needs team sign-off and an eval result |
| Tier C: Pro adjudicates with top-5 formulary candidates | **Not adopting now** | It is the S2 VLM tiebreak the project chose not to build, and it shows the vocabulary to the model (§2 rule 12). AMBIGUOUS already goes to the doctor as pick-one buttons. Revisit at the Phase 1 → 2 boundary |
| Cloud Vision OCR (Stage 2a) | **Defer, then adapt** | Optional evidence channel: stored per page, used for a downgrade-only agreement check and tighter review boxes (Phase 5c) |
| Router on OCR confidence (Stage 2b) | **Defer** | Only pays off with a large share of printed pages, and needs OCR first (Phase 5e) |
| Quality gate: blur, dark, glare, small (Stage 1, step 3) | **Adopt, warn-only** | Pillow + numpy, no OpenCV. Shows a retake tip; blocks only after calibration on real photos, as the blueprint itself says (Phase 5a) |
| Flatten, deskew, CLAHE, denoise (Stage 1, steps 4–7) | **Defer** | Changes what the model sees. A/B on the eval set first (Phase 5f). Never binarise: both documents agree |
| Resize long edge to 2,000 px | **Not adopting** | S0 resizes to 1,568 px, measured at 65% fewer image tokens. Revisit only if the eval says it costs accuracy |
| PDF intake (Stage 1, step 0) | **Adopt** | Render pages, feed the existing batch upload (Phase 5b). Check the licence: PyMuPDF is AGPL; `pypdfium2` is Apache/BSD |
| Perceptual-hash dedupe | **Not adopting** | Rejected in Session 10 with reasons: pages from one pad share a letterhead. Exact-image and read-identical duplicates are already handled |
| Drug-name grounding (Stage 4.1) | **Already built** | S2/S3: RapidFuzz over 239,541 brands, thresholds in `thresholds.yaml` (88 / 6 / 82, measured). The blueprint's 92 / 8 / 75 are not adopted |
| Look-alike (LASA) guard | **Adopt** | Downgrade-only check on S2's top two candidates (Phase 4) |
| Sig parser (Stage 4.2) | **Adopt** | Pure function; adds a "per day" and duration in days to each line (Phase 4) |
| Dose sanity table (Stage 4.3) | **Adopt code, defer data** | Code ships with an empty table. Rows come from the National Formulary of India with a citation per row, never from memory (Phase 4) |
| Therapeutic duplication (Stage 4.4) | **Adopt** | Same verified ingredient on two live lines, across all pages (Phase 4) |
| DDInter, RxNorm, openFDA label quotes (Stage 4.4) | **Defer** | DDInter needs a licence check. The 180 cited rules stay the S4 source |
| Decision gates, Green / Amber / Red (Stage 5) | **Adapt** | Gates become `review_reasons` shown on the review screen. No auto-approval (§2 rule 4) |
| Always-human list (gate 7) | **Adopt** | High-alert drugs get `needs_hard_confirmation` with a reason (Phase 4) |
| Schedule H1 / X (gate 8) | **Defer** | Needs a sourced schedule list |
| Paid tier only for real data; spend cap in code | **Adopt** | Startup check (§2 rule 11). The cap already exists and will cover Gemini too |
| PII redaction before the call | **Defer** | A wrong box could black out a medicine line. Needs OCR (Phase 5c) and an eval |
| Gold set, rule-of-three, three-variant comparison (Evaluation) | **Adopt first** | Phase 2. It is also ARCHITECTURE_V2 §11 / P0, which was never built |
| Keep eval outputs in Git | **Adapt** | Only for SPECIMEN pages. Outputs on real pages contain patient data and stay gitignored |
| Postgres + pg_trgm, Celery/RQ workers, Django/Streamlit review app | **Not adopting** | SQLite, in-memory RapidFuzz, worker threads and the existing React review screen cover the current scale |
| The name "MedScript" | **Not adopting** | The project is PrescriptAI |

---

## 4. Target shape

```
upload ─► S0 normalise (unchanged) ──► [P5a quality report, warn only]
              │
              ▼
      CachedPageReader      key = image hash + reader.cache_identity()
              │
   ┌──────────┼───────────────────┬──────────────────────────┐
ClaudePageReader     GeminiPageReader       ConsensusReader(a, b)       ← each returns PageRead
(default, today)     READER=gemini (P3)     READER=consensus (P5d)
   │                       │
ClaudeCaller          GeminiCaller          ← same contract: concurrency cap, spend
   └───────────┬───────────┘                  reservation, refusal / truncation, safe errors
          SpendGuard          one ledger, one cap, every provider
              │
              ▼
S2 resolve_page(page, thresholds_for(model))   + LASA guard, high-alert reasons (P4)
S3 brand → generic + NLEM                      + sig parser (P4)
S4 DDI across all pages                        + therapeutic duplication (P4)
S5 review                                      + table view, CSV export (P1)
```

---

## 5. Phases

Phases 0 and 1 deliver the conversion into a table, with no new model code. Phase 2 comes
before any provider change, which is the blueprint's own "gold set first" rule. Phase 4
makes no model calls and can run alongside Phases 2 and 3. Phase 6 comes last because it
changes the working Claude path.

### Phase 0: Switch on what is built

**Goal:** a real handwritten page read end to end on this machine. **Size:** hours.
**Cost:** about $0.35–0.50.

1. Python 3.12+ environment (`/opt/anaconda3/bin/python3` is 3.13). Install
   `backend/requirements.txt`; `segno` is currently missing.
2. Start the backend with the defaults. `/api/health` must show `reader: claude`,
   `reader_check: ok`, `medicine_data: ok`.
3. `python manage.py create-admin`, register a doctor, approve it at `/admin`.
4. Run the eight checks in API_IMPLEMENTATION §1.9 on `VLM_CHECK/` images. These include
   the "Dijoxin" verbatim regression on prompt v2 and parallel multi-page reads.
5. Record the results as Session 11 in `claude.md`.

**Done when:** all eight checks pass, or each failure is written up with its cause.

### Phase 1: Table view and export

**Goal:** the reading shown as a table. **Size:** small to medium. **Cost:** $0.
No API changes are needed: the consultation response already has every field.

| Column | Source | Notes |
|---|---|---|
| # | `index` | |
| As written | `raw_reading` | Always shown (README §7 rule 2) |
| Medicine | `brand` | Struck lines shown struck-through, never hidden |
| Contains | `ingredients`; unverified ones named | |
| Dose / How often / Duration / Instructions | `dosage`, `frequency`, `duration`, `instructions` | Verbatim until Phase 4 adds "per day" |
| Read | `outcome` | confirmed / probable / ambiguous / unmatched / illegible |
| Drug check | `verification` | verified / partial / unverified / unknown |

Example (`VLM_CHECK/2.jpg`, Session 4 live output, header fields left out):

| # | As written | Medicine | Contains | How often | Duration | Read | Drug check |
|---|---|---|---|---|---|---|---|
| 1 | OFLAZEST-OZ | Oflazest OZ | Ofloxacin + Ornidazole | 1 --- 1 | 3 days | confirmed | unverified |
| 2 | AZENAC-MR. | Azenac MR | Aceclofenac + Paracetamol | 1 --- 1 | 3 days | confirmed | partial |
| 3 | ANDIAL | Andial | Loperamide | 2 (6hr)-1… | 3 days | probable | verified |
| 4 | ZOFER | Zofer | Ondansetron | 1 --- 1 | 3 days | probable | verified |

Changes:

- `web_app/src/components/MedicineTable.jsx` (new) and a Cards / Table toggle on
  `ConsultationPage.jsx`. The cards stay the editing surface; the table is read-only.
- The same table on `patient/PatientPrescriptionPage.jsx`, using the patient view's
  fields only (no `safer_alternative`, as now).
- `GET /api/consultations/{cid}/export.csv`: doctor and owner only, `Cache-Control:
  no-store`, logged as an `auth_events` row. **Escape cells beginning with `= + - @`**:
  text read off a page could otherwise run as a spreadsheet formula when the file is
  opened in Excel.
- Optional: render the PDF's medicines as a table in `backend/rx_pdf.py`.

Tests:

- **Browser:** the table renders, and there is no horizontal page scroll at 390 px. The
  existing `no_hscroll` check measures the whole page, so the table scrolls inside its
  own container.
- **API:** CSV ownership (`404` for another doctor), formula escaping, and `no-store`.

### Phase 2: Evaluation harness and gold set

**Goal:** a measured baseline for the reader that exists, before anything is swapped.
**Size:** medium for the code; labelling is the long pole. **Cost:** about $0.85 for 25
pages on Claude, once. Re-runs come from the cache.

- `ml_pipeline/eval/`:
  - `gold/` holds one JSON per page, with per-field truth. `illegible` and `absent` are
    valid truths (blueprint, Evaluation).
  - `run.py --reader <kind> --split smoke|dev|test` writes a per-field CSV and prints the
    metric table.
- **Metrics, in this order:**
  1. Hallucination rate.
  2. Critical errors among CONFIRMED lines, with the rule-of-three bound (3/*n*).
  3. Coverage.
  4. Medicine accuracy.
  5. Per-field F1.
  6. Cost and p50 / p95 latency.
- **The set:**
  - the 5 `VLM_CHECK` images;
  - a smoke split of 8 from the 129 `data_check` images (ARCHITECTURE_V2 §11, with their
    medicine-free hard negatives);
  - 10 team-written SPECIMEN pages with planted traps: overdose, duplicate ingredient,
    severe pair, look-alike brands.

  Grow it to 50, labelled by two people, with a pharmacist settling disagreements.
- **Never tune on the test split** (README §7 rule 12).

**Done when:** a baseline table for today's Claude reader exists for the smoke split.

### Phase 3: Gemini as a second reader, off by default

**Goal:** `READER=gemini` works anywhere `READER=claude` works. **Size:** medium.
**Cost:** about $0.50 for the same 25 pages. That uses the blueprint's ~$0.02 per read,
which is not verified.

| File | Change |
|---|---|
| `ml_pipeline/vlm/gemini_client.py` (new) | `GeminiCaller`, the same contract as `ClaudeCaller`. Shared concurrency cap and spend reservation. Safety-blocked or empty replies raise the existing `ReaderRefused`; a `MAX_TOKENS` finish raises `ReaderTruncated`. Schema-invalid JSON gets one retry, then fails (blueprint Stage 3). `check_access()` is a free model lookup. One retry policy: the SDK's own if it has one, otherwise a single bounded loop for 429 / 5xx, never both |
| `ml_pipeline/vlm/gemini_prompts.py` (new) | Imports our S1 prompt text and replaces only the box instruction, asking for `box_2d` 0–1000. `prompts.py` is not edited, so Claude's fingerprint is unchanged. Own `GEMINI_PROMPT_VERSION` and fingerprint |
| `ml_pipeline/vlm/gemini_reader.py` (new) | `GeminiPageReader`: image plus prompt, `response_json_schema` with the private wire schema, `media_resolution` high, low thinking. Converts to `PageRead` (`bbox = [xmin, ymin, xmax, ymax] / 1000`). `cache_identity()` returns model, Gemini prompt version and fingerprint |
| `ml_pipeline/vlm/page_reader.py` | `build_reader("gemini")`. `CachedPageReader._key` uses `inner.cache_identity()` when the reader has one; Claude and the mock don't, so their keys are unchanged. `resolve_reader_kind("auto")`: unchanged when the Anthropic key is set; Gemini only when the Gemini key is the only one |
| `ml_pipeline/vlm/spend.py` | Gemini rows in `PRICES`, taken from Google's pricing page on the day. The blueprint's figures are placeholders. `price_call` reads Gemini's usage shape (prompt, candidates, cached and thought tokens; thought tokens billed as output) |
| `ml_pipeline/vlm/client.py` | `describe_error` maps Google SDK errors onto the existing categories, so a doctor sees the same messages |
| `ml_pipeline/config/thresholds.yaml` + `vocab.load_config` | `reading.profiles.<model>`. The top-level numbers stay the default (Opus 5, measured). A profile marked `calibrated: false` caps every line at PROBABLE |
| `backend/pipeline.py` | `resolve_page(result.page, thresholds_for(result.model))`. Startup refuses a Gemini reader unless `GEMINI_PAID_TIER=true` (§2 rule 11). The pack reader gets a Gemini variant, so stage 6 photos work without an Anthropic key |
| `backend/main.py` | `/api/health` also reports `reader_provider`. `reader_is_mock` and the "Demo reader" banner are unchanged |
| `backend/requirements.txt` | `google-genai`, imported lazily |

**Unknown until implemented:** Gemini model IDs, SDK parameter names and prices all come
from the blueprint, dated 18 Nov 2026. Confirm each on Google's own pages before
writing code, and pin the exact model version.

Tests (`ml_pipeline/tests/test_gemini_reader.py`, fake client, mirroring the 33 checks in
`test_reader.py`):

- call counts and no cross-talk between parallel reads;
- refusal and truncation;
- the spend cap under concurrency (six calls against room for two: two reach the API);
- bbox conversion;
- cache identity independent of Claude's prompt;
- error messages;
- the `calibrated: false` cap;
- the paid-tier startup refusal.

**Done when:** the eval table has a Gemini row next to the Claude row, and
`tests/run.py` passes unchanged with `READER` unset.

### Phase 4: Deterministic clinical checks

No model calls, and every rule here can only lower an outcome. **Size:** medium.
**Cost:** $0. New fields go into `resolved_json`, so no migration is needed; old rows simply
lack them.

| Check | File | Behaviour |
|---|---|---|
| Sig parser | `ml_pipeline/normalise/sig.py` (new) | OD, BD/BID, TDS/TID, QID/QDS, HS, SOS/PRN, STAT, AC/PC; `1-0-1`, `½-0-½`; `5/7`, `2/52`, `1/12`; *khane ke baad / pehle*. Adds `sig = {per_day, timing, duration_days, flags}`. A conflict (`BD` beside `1-1-1`) or an unknown pattern is a flag, never a guess. The verbatim `frequency` is kept. One unit test per table row |
| Look-alike guard | `vlm/vocab.py` | When S2's top two candidates are within edit distance 2 **and** have different compositions, the line cannot be CONFIRMED (cap at PROBABLE, with a reason). Computed on the two candidates S2 already has, so no pairwise table over 239k brands is needed |
| High-alert list | `config/high_alert.yaml` (new) | Insulin, anticoagulants, opioids, methotrexate, digoxin, sulfonylureas, injectables. Taken from the ISMP high-alert list, with the source written in the file. Sets `needs_hard_confirmation` plus a reason |
| Therapeutic duplication | `backend/pipeline.py` `check()` | The same verified ingredient on two live lines, on any page, goes into a `duplicates` list next to the interactions. It is not a severity |
| Dose sanity | `normalise/dose.py` + `config/dose_limits.yaml` (new, empty) | `daily = strength × quantity × per_day` against cited maxima. No row means "not checked", and that is shown in `skipped`, never as a pass |
| Review reasons | `schemas.py` | `ResolvedMedicine.review_reasons: List[str] = []` (optional, default empty), shown on `MedicineCard` and in the table |

The table from Phase 1 gains a **Per day** column.

### Phase 5: Optional evidence channels

Each one is behind its own setting and goes on only after the eval shows it helps.

| | Feature | Setting | Needs |
|---|---|---|---|
| 5a | Quality report: Laplacian variance, brightness, glare, short edge; retake tip on the scan screen | `QUALITY_GATE=warn` (later `block`) | Thresholds calibrated on the first ~50 real photos |
| 5b | PDF upload: pages rendered and sent to the existing batch route (10-page limit unchanged) | always on once merged | Licence choice (PyMuPDF AGPL vs pypdfium2) |
| 5c | Cloud Vision OCR stored per page (`prescriptions.ocr_json`, nullable, migration `0002`). Agreement check per line: below 0.90 similarity caps at PROBABLE. Tighter review boxes | `OCR_PROVIDER=google_vision` | Google Cloud project, billing, service-account credentials. First 1,000 pages a month are free |
| 5d | `ConsensusReader(primary, secondary)`. Lines are aligned by index, bbox overlap and token similarity. Where the readings agree, the line is kept. Where they disagree, the second reading goes into `alternatives` and confidence is capped below `confident_read`, so S2 can reach at most PROBABLE, or AMBIGUOUS from the buttons. A line only one read found is kept and flagged, never dropped. Both raw replies are stored | `READER=consensus` + `CONSENSUS_READERS=claude,gemini` | Team sign-off (§7). Roughly doubles cost per page. Two vendors give less correlated errors than one model read twice |
| 5e | Router: Tier A (printed, cheap read) vs Tier B | `READER=routed` | 5c, and a real share of printed pages |
| 5f | Flatten, deskew, CLAHE, denoise before the read | `S0_ENHANCE=true` | An A/B eval. The image hash changes, so the cache invalidates itself |

### Phase 6: `PageRead` v3, the richer schema

Done last, because it changes the reader that works today.

- **New optional fields on `MedicineLine`:**
  - `strength` (separate from dose), `dose_quantity`, `timing`, `route`;
  - `crossed_out`;
  - `total_quantity`;
  - `instructions_english`.
- **New optional fields on `PageRead`:**
  - `prescriber_registration`;
  - `patient_age`, `patient_sex`, `patient_weight`.
- `PROMPT_VERSION = "v3"` for Claude, with a matching Gemini prompt version.
- **Fix a cache gap first:** `prompt_fingerprint()` hashes only the prompt text, so a schema
  change alone would not invalidate cached readings. Add a hash of
  `PageRead.model_json_schema()` to the fingerprint. The cache is empty since the 19 Nov
  cleanup, so this costs nothing now.
- Re-run the "Dijoxin" regression and the eval for both readers before merging.
- Old rows render with blanks in the new columns.

---

## 6. Budget

The spend cap is **$5**. The ledger shows **$0.39** spent across 10 calls, leaving about
**$4.61**.

| Phase | Paid calls | Estimate |
|---|---|---|
| 0 | About 10 Claude reads (§1.9 checklist) | $0.35–0.50 |
| 2 | 25 gold pages, Claude, once | ~$0.85 |
| 3 | The same 25 pages, Gemini Flash | ~$0.50 (unverified) |
| 5d | The same 25 pages, second read | ~$0.50–0.85 |
| **Total** | | **~$2.2–2.7** |

**What the numbers say:** the blueprint's handwritten path (Vision OCR + two Flash reads,
~$0.038) costs about the same as one Claude read today ($0.034, measured). Its cost
advantage is on **printed** pages (~$0.005, Tier A). A switch of provider should be decided
by Phase 2/3 accuracy numbers, not by price. Batch modes (50% off on both providers)
suit eval re-runs only, never the upload screen.

---

## 7. Decisions needed from the team

| # | Decision | Recommendation |
|---|---|---|
| 1 | Keep Claude as the default reader, or switch to Gemini? | Keep Claude (it works and the key is live). Add Gemini behind the Protocol and decide on Phase 3 numbers |
| 2 | Accept two reads per page, reversing "dual-VLM cross-check deferred"? | Only if Phase 2/3 show confident single-read errors that a second read would have caught |
| 3 | `claude-opus-5` or `claude-opus-5-5`? | Decide on the eval. 5.5 is ~20% cheaper, but `thresholds.yaml` is calibrated on Opus 5 (API_IMPLEMENTATION §1.8), so it needs its own profile |
| 4 | Who labels the gold set, and which pharmacist settles disagreements? | Needed before Phase 2 can finish |
| 5 | Google account: paid tier, data terms for health data (DPDP Act), Vision credentials | Needed before Phase 3 touches a real page |
| 6 | PDF library licence | `pypdfium2`, unless AGPL is acceptable |

---

## 8. Risks

| Risk | Handling |
|---|---|
| Gemini's confidences are on a different scale; lines move between CONFIRMED, PROBABLE and ILLEGIBLE | Per-model profiles; uncalibrated profiles cannot CONFIRM (Phase 3) |
| Box format mix-up puts review crops in the wrong place | One conversion function, unit-tested against known boxes |
| A Gemini prompt edit is served stale from cache | `cache_identity()` per reader (Phase 3) |
| A second company processes patient images | Paid tier only, startup check, SPECIMEN pages until terms and consent are settled |
| Double spend from a second read | The same reservation as today; the cap covers every provider |
| A provider is down on demo day | After Phase 3, switching is a setting (the blueprint's own point) |
| The labelling effort stalls Phase 2 | Start with the smoke split (8 + 5 + 10 pages); everything else can proceed in parallel |
| `fastapi.testclient` is broken here (httpx 0.28 vs Starlette) | New API tests drive a real server, like `tests/e2e_api.py` |

Each phase gets its journal entry in `claude.md`, like Sessions 1–10.
