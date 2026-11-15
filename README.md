# PrescriptAI

**Reads a photo of a handwritten Indian prescription, says what it can and cannot
read, maps each brand to its ingredients, checks those ingredients against a cited
drug-interaction table, and puts every page in front of a doctor before it is used.**

> ⚠️ **Not a medical device.** Every extraction is AI-generated and can be wrong. The
> product is designed around mandatory clinician review (Phase 1): nothing is
> dispensed on the model's word alone.

| | |
|---|---|
| **Status** | Working end to end on real prescriptions (S0 → S5). Testing phase. |
| **Reader** | Claude Opus 5 vision (`claude-opus-5`, `effort: medium`), or a free mock |
| **Target** | English-language prescriptions, primarily Indian |
| **Cost** | ~$0.034 per prescription measured; cached re-runs are free |
| **Stack** | Python 3.12+ · FastAPI · SQLite · React 19 + Vite + Tailwind |
| **Accounts** | Doctor / patient / pharmacy sign-in, MFA, recovery. See [§6a](#6a-authentication). |
| **Patients** | See and download (PDF) the prescriptions a doctor confirmed for their verified phone. See [§5](#5-api-reference). |
| **Run it** | `docker compose up --build -d` → `http://localhost:8000`, demo inbox at `:8025`. See [§3.5](#5-hosting-the-whole-site-docker). |

---

## Contents

1. [How it works](#1-how-it-works)
2. [Repository layout](#2-repository-layout)
3. [Quick start](#3-quick-start)
4. [Running the pipeline directly](#4-running-the-pipeline-directly)
5. [API reference](#5-api-reference)
6. [Core concepts](#6-core-concepts)
   - [6a. Authentication](#6a-authentication)
7. [Design rules: do not break these](#7-design-rules-do-not-break-these)
8. [Configuration](#8-configuration)
9. [Data](#9-data)
10. [Cost control](#10-cost-control)
11. [Current status](#11-current-status)
12. [Known issues and stale files](#12-known-issues-and-stale-files)
13. [Deliberately not built](#13-deliberately-not-built)
14. [Roadmap](#14-roadmap)
15. [Where to read more](#15-where-to-read-more)

---

## 1. How it works

The pipeline is six stages. Each boundary is a typed contract in
[`ml_pipeline/schemas.py`](ml_pipeline/schemas.py).

```
 photo ─► S0 Ingest      EXIF-rotate, downscale to the VLM's token budget, JPEG.
          │              No binarisation. Hash of the final bytes = cache key.
          ▼
          S1 VLM read    Whole page → PageRead JSON (schema-guaranteed).
          │              Verbatim text, per-line confidence, alternatives, bbox.
          │              Can say "not_a_prescription" or "illegible".
          ▼
          S2 Vocabulary  RapidFuzz against 239k Indian brands (full + base-name
          │              indices) and 1,679 generic ingredients.
          │              → CONFIRMED / PROBABLE / AMBIGUOUS / UNMATCHED / ILLEGIBLE
          ▼
          S3 Brand →     Brand → composition ("Azenac MR" → Aceclofenac + Paracetamol).
          │  generic     Each ingredient checked against NLEM 2022.
          │              → VERIFIED / PARTIAL / UNVERIFIED / UNKNOWN
          ▼
          S4 DDI check   Deterministic lookup: 180 cited rules over 212 drugs
          │              (NetworkX graph). Only NLEM-verified ingredients of
          │              CONFIRMED/PROBABLE medicines enter. Everything else is
          │              listed in `skipped` with a reason.
          ▼
          S5 Doctor      Review screen: page image beside the extraction, per-line
             review      crops. Every edit and every confirmation is logged.
                         That log is the audit trail and the future training corpus.
```

The backend runs S0 → S4 on upload, stores everything, and sends the doctor to the
review screen.

**Why a VLM and not OCR:** the earlier version fine-tuned TrOCR on 4.6k word crops
and reached 6% raw / 14% post-fuzzy accuracy. The product needs whole pages read,
and a VLM reads whole pages with no training data. TrOCR is gone from the serving
path. The reasoning is in [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md) §2–3.

---

## 2. Repository layout

```
.
├── README.md                 ← you are here: overview + how to run
├── claude.md                 ← implementation journal: what was built, what broke, why
├── docs/
│   ├── ARCHITECTURE_V2.md    ← design rationale, stage specs, cost model, roadmap (§ refs)
│   └── DAY1_PLAN.md          ← the original 3-track build plan
├── .env.example              ← copy to .env
│
├── ml_pipeline/              ← the pipeline (importable modules + CLIs)
│   ├── schemas.py            shared contracts: PageRead, ResolvedMedicine, Outcome, ...
│   ├── env.py                loads project-root .env (real env vars win)
│   ├── config/thresholds.yaml   S2 matching + confidence thresholds (all guesses, see §8)
│   ├── ingest/normalise.py   S0
│   ├── vlm/
│   │   ├── prompts.py        S1 system prompt; PROMPT_VERSION + fingerprint
│   │   ├── page_reader.py    S1 readers: MockPageReader, ClaudePageReader, CachedPageReader
│   │   ├── pack_reader.py    stage 6: reads a photographed pack label (mock / Claude)
│   │   ├── client.py         the ONE place that calls the API: retries, timeouts,
│   │   │                     concurrency cap, refusals, safe errors (docs/API_IMPLEMENTATION.md)
│   │   ├── spend.py          spend ledger + hard cap
│   │   └── vocab.py          S2 (+ calls S3): resolve_page()
│   ├── normalise/
│   │   ├── india_medicines.py   S2/S3 data: brand indices, compositions, NLEM gate
│   │   └── drug_names.py        salt/ester stripping, INN↔USAN synonyms
│   ├── dispense/compare.py   stage 6: pack (QR / photo / typed) vs prescription
│   ├── tests/                test_dispense.py + fixtures/india (TEST FIXTURE data)
│   ├── drug_interaction/
│   │   ├── build_knowledge_graph.py   loads DDI Database.json → NetworkX graph
│   │   ├── interaction_inference.py   S4: InteractionChecker
│   │   └── severity_labels.py
│   └── data/
│       ├── ddi_dataset/DDI Database.json   committed (180 rules)
│       └── india/*.csv                     NOT committed, see §9
│
├── backend/                  ← FastAPI app (port 8000)
│   ├── main.py               app, CSRF + CORS, page routes, stats, /api/verify, backfill
│   ├── pipeline.py           reader / checker / pack reader singletons; READER=auto
│   ├── consultations.py      stages 1–3: consultations, pages, review, finalise
│   ├── patient.py            stages 4–5: patient view, PDF, share by OTP, revoke
│   ├── pharmacy.py           stages 5–7: redeem, pack checks, dispensing record
│   ├── access.py             grant status, OTP hashing, dispensing state
│   ├── views.py              shared response shapes, verification IDs
│   ├── rx_pdf.py             the prescription PDF (fpdf2 + segno QR)
│   ├── admin.py              administrators approve doctor and pharmacy accounts
│   ├── manage.py             operator CLI: create-admin, pending, migrate, new-key
│   ├── alembic.ini, migrations/   database migrations (config/migrate.py runs them)
│   ├── models.py             SQLAlchemy: consultations, versions, pages, corrections,
│   │                         interaction runs, grants, dispense checks, records
│   ├── auth/                 accounts for every stakeholder (§6a)
│   │   ├── routes.py         /api/auth/*; current_user / require_roles dependencies
│   │   ├── models.py         users, auth_sessions, one_time_codes, mfa_challenges,
│   │   │                     recovery_codes, auth_events
│   │   ├── security.py       scrypt, token/code hashing, TOTP (stdlib only)
│   │   ├── notify.py         code delivery: SMTP email, else console
│   │   └── ratelimit.py      in-memory throttling
│   ├── config/               settings.py, database.py (SQLite by default), migrate.py
│   └── uploads/              uploaded images (gitignored)
│
├── web_app/                  ← main React app (port 5173). Clinical light theme.
│   └── src/
│       ├── pages/            Home, Upload (new consultation), Consultation (review +
│       │                     finalise), Results, Interactions, History, Dashboard,
│       │                     VerifyDocument (public /check/:id)
│       ├── pages/patient/    inbox, prescription (share with pharmacy, revoke)
│       ├── pages/pharmacy/   desk (redeem code), grant (check packs), records
│       ├── components/       Layout, Navbar, MedicineCard, MedicalBackdrop, Logo
│       ├── pages/auth/       Login, Register, ForgotPassword, Verify, Account
│       ├── components/auth/  AuthUI (shared form pieces), RequireAuth (route guard)
│       ├── services/api.js   axios client: cookie + CSRF header, 120s timeout
│       ├── services/auth.js  /api/auth/* calls, ROLE_HOME
│       ├── store/            zustand: useAppStore, useAuthStore (who is signed in)
│       └── data/ddiDrugs.js  210 generics generated from DDI Database.json (autocomplete)
│
├── tests/                    run.py (one command), e2e_api.py, e2e_browser.py, test_security.py
├── deploy/                   Caddyfile (HTTPS), docker-entrypoint.sh (drops root)
├── Dockerfile                one production image: API + built web app, see §3.5
├── docker-compose.yml        local demo on http://localhost:8000 (Mailpit, plain http)
└── docker-compose.prod.yml   the same image behind HTTPS (Caddy)
```

---

## 3. Quick start

### Prerequisites

- **Python 3.12+**. `backend/config/settings.py` nests double quotes inside an
  f-string, which is a syntax error before 3.12.
- **Node.js 18+** and npm
- **The Indian medicine CSVs** in `ml_pipeline/data/india/` (§9). Without them,
  upload fails at S2.
- An **Anthropic API key**. Optional if you use the mock reader.

### 1. Environment

```bash
cp .env.example .env
# optional: paste ANTHROPIC_API_KEY into .env. Left empty, everything runs on the
# mock reader at zero cost and the UI labels it "Demo reader".
```

### 2. Python dependencies

```bash
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
```

`ml_pipeline/requirements.txt` is the subset the pipeline needs on its own.

### 3. Backend

```bash
cd backend
python manage.py create-admin     # once: the account that approves doctors and pharmacies
python main.py                    # → http://localhost:8000  (docs at /docs)
```

Startup applies database migrations. A doctor or pharmacy that registers is
**pending** until the administrator approves it at `/admin` (§6a). For local
development only, `REQUIRE_VETTING=false` skips that.

`READER` defaults to `auto`: the Claude reader when `ANTHROPIC_API_KEY` is set in
`.env`, the mock reader when it is empty. Adding the key and restarting is the only
step needed to start reading real handwriting and pack labels. `/api/health`
reports `reader` and `reader_is_mock`, and the UI shows a "Demo reader" banner on
the mock. `READER` also accepts `claude`, `mock` or `mock_negative` (returns "not a
prescription") to force one.

No medicine CSVs on this machine? Point the loader at the test fixture (15 brands,
every row marked `TEST FIXTURE`, never for real use):
`PRESCRIPTAI_DATA_DIR=ml_pipeline/tests/fixtures/india`.

### 4. Web app

```bash
cd web_app
npm install
npm run dev                       # → http://localhost:5173
```

`VITE_API_URL` sets the backend URL (default `http://localhost:8000`).

**First run:** open the site and **Create account → Doctor**. Prescription screens
need a signed-in doctor. The email/phone verification code is **printed in the backend
terminal** unless SMTP is configured (§8); under Docker every code, phone codes included,
arrives in the demo inbox at `http://localhost:8025` instead.

**Patients:** a patient verifies their phone, and the prescriptions a doctor confirmed
for that number appear at `/patient`, each with a **Download PDF** button. The doctor
enters the patient's phone on the review screen before confirming. Pharmacy screens are
still placeholders until Workflow stages 5–7 are built.

**The flow:** Upload → **Review** (image beside extraction; correct or confirm) →
Results. History and Dashboard are computed from real stored records.

Use `http://localhost:5173`, not `127.0.0.1:5173`. The session cookie belongs to
`localhost`, so mixing the two hosts looks like being signed out.

**Demo note:** most real outpatient prescriptions (analgesics, antibiotics, PPIs)
correctly return **no interactions**. To show S4 firing, use a known high-risk pair,
e.g. `Cofarin 1mg` (Warfarin) + `ALFAM 400MG` (Ibuprofen) → SEVERE, GI bleeding. In
the Interactions page, try `Warfarin` + `Ibuprofen` or `Sildenafil` + `Nitroglycerin`.

### 5. Hosting the whole site (Docker)

One container runs the API **and** serves the built web app, so both are on
`http://localhost:8000` and the session cookie needs no CORS.

```bash
# once: copy the template and fill in SECRET_KEY and DATA_ENCRYPTION_KEY (gitignored)
cp .env.docker.example .env.docker

docker compose up --build -d      # → http://localhost:8000
docker compose exec -u app prescriptai python manage.py create-admin   # once
# verification codes (email AND phone) → the demo inbox at http://localhost:8025
docker compose down               # stop; data survives in the named volumes
```

- **Reader.** `READER: auto`: with no `ANTHROPIC_API_KEY` in `.env.docker` the
  container runs the mock reader and spends nothing. Add the key to `.env.docker`
  and `docker compose up -d` to read real images; the spend cap still applies.
- **Codes go to Mailpit**, an open-source local inbox (`axllent/mailpit`). It accepts
  mail for any address and forwards none, so codes never leave the machine. Phone
  codes are emailed too: to the account's email, or to `<digits>@sms.prescriptai.local`.
  To send real email instead, set `SMTP_*` to a real server.
- **Localhost only, plain http.** The port is bound to `127.0.0.1`. To serve it on
  a network use the HTTPS file instead, which puts Caddy in front, sets `Secure`
  cookies and HSTS, and does not publish the app's own port:
  `SITE_ADDRESS=rx.example.in PUBLIC_URL=https://rx.example.in docker compose -f docker-compose.prod.yml up --build -d`
  (it reads `.env.production`).
- **Medicine data** is mounted from `ml_pipeline/data/india/` on the host, read-only.
  Put the two CSVs there (§9) and restart; no rebuild. Without them uploads answer
  "the medicine database is not installed" and `/api/health` says `"medicine_data": "missing"`.
- **Not root.** The server runs as an unprivileged user; the entrypoint hands the
  volumes to it first, so volumes from an older image keep working.
- **State** lives in three volumes: `prescriptai_data` (SQLite),
  `prescriptai_uploads` (prescription images) and `prescriptai_cache` (read cache
  and spend ledger). `docker compose down -v` deletes all of it.

---

## 4. Running the pipeline directly

All modules are runnable from `ml_pipeline/`:

```bash
cd ml_pipeline

python -m ingest.normalise path/to/*.jpg                  # S0 + image token accounting
python -m vlm.page_reader --reader mock path/to/rx.jpg    # S1, free
python -m vlm.page_reader --reader mock_negative rx.jpg   # S1, "not a prescription" path
python -m vlm.page_reader --reader claude rx.jpg          # S1 for real (needs key)
python -m vlm.vocab --reader mock rx.jpg                  # S1 + S2 + S3 together
python -m vlm.prompts                                     # prompt version + fingerprint
python -m vlm.spend                                       # spend so far vs cap
python env.py                                             # is .env loaded / key set?
```

`page_reader` also takes `--model`, `--effort`, `--no-cache`.

Response cache and spend ledger: `ml_pipeline/.cache/` (gitignored; the cache can
contain patient data). Delete it to force a cold run.

---

## 5. API reference

Base URL `http://localhost:8000`. Interactive docs at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status, `reader`, `reader_is_mock`, `reader_check` (is the key accepted), `medicine_data`, `phone_codes_by`, `vetting_required`, prompt version, spend. **Public.** |
| GET | `/api/verify/{verification_id}` | What the PDF's QR opens: genuine?, doctor, registration no., version, `superseded`. **No patient data, no medicines.** **Public**, throttled. |
| POST | `/api/interactions/check` | Ad-hoc lookup by **generic** names. Bypasses S2/S3. **Public.** |
| | **Doctor** (`backend/consultations.py`, `main.py`) | |
| POST | `/api/consultations` | Start one: `patient_name`, `patient_phone`, optional `clinic_address` (defaults to the profile's) |
| GET | `/api/consultations` | This doctor's consultations |
| GET | `/api/consultations/{id}` | Working copy: every page, every medicine, ONE interaction check across all pages, `unresolved`, `versions` |
| PATCH | `/api/consultations/{id}` | Patient, clinic, `diagnosis`, `diagnosis_visibility` (`patient` / `patient_and_pharmacy` / `hidden`), `allow_substitution` |
| POST | `/api/consultations/{id}/pages` | Multipart `image`. Runs S0→S4 on it and rechecks the whole consultation. `409` if that image is already a page of it (nothing is read). A reader failure is a `502`/`503`/`504` with a message safe to show. |
| POST | `/api/consultations/{id}/pages/batch` | Multipart `images` (several). Exact duplicates are dropped first, the rest are read **in parallel**, the interaction check runs once. One result per file: `added` / `duplicate` / `failed`. |
| DELETE | `/api/consultations/{id}/pages/{page_id}` | Only before the first finalise |
| POST | `/api/consultations/{id}/medicines` | Add a line the reader missed (`missed` correction). The name goes through S3. |
| POST | `/api/prescriptions/{page_id}/medicines/{i}/remove` · `/restore` | Strike a line not on the page (`hallucinated` correction) |
| POST | `/api/prescriptions/{page_id}/corrections` | One field edit. A corrected **name** is re-resolved through S3, so ingredients and the interaction check follow it. |
| POST | `/api/consultations/{id}/finalise` | Writes immutable version N. `422` while a line is AMBIGUOUS, unresolved without `acknowledge_unresolved`, or two pages read identically without `acknowledge_duplicates`. `409` if nothing changed. |
| GET | `/api/consultations/{id}/versions/{n}` | A finalised snapshot |
| POST | `/api/prescriptions/upload` | Legacy single page: opens a consultation around it |
| GET | `/api/prescriptions`, `/api/prescriptions/{id}` | Pages (this doctor's) |
| GET | `/api/stats`, `/api/corrections` | Coverage, totals; the correction corpus (this doctor's) |
| | **Administrator** (`backend/admin.py`) | |
| GET | `/api/admin/accounts?status=pending\|approved\|rejected\|all` | Doctor and pharmacy accounts, with the registration / licence number each declared |
| POST | `/api/admin/accounts/{id}/vetting` | `{"decision": "approve"\|"reject", "note"}`. A rejection needs a note; the applicant sees it. Takes effect on the account's next request. |
| GET | `/api/admin/summary` | Counts by status |
| | **Patient** (`backend/patient.py`) | |
| GET | `/api/patient/prescriptions` | Inbox: finalised prescriptions whose latest version is addressed to their **verified** phone |
| GET | `/api/patient/prescriptions/{id}` | Latest version: `revised`, diagnosis (unless hidden), medicines with `dispensed`, interactions, `not_checked`, `shares`, `dispensing` |
| GET | `/api/patient/prescriptions/{id}/pages/{n}/image`, `/pdf` | Original pages; the PDF (with QR) |
| POST | `/api/patient/prescriptions/{id}/share` | A 6-digit code for one pharmacy (redeem within 5 min). Cancels any unused earlier code. |
| POST | `/api/patient/grants/{id}/revoke` | End a pharmacy's access now |
| | **Pharmacy** (`backend/pharmacy.py`) | |
| POST | `/api/pharmacy/redeem` | `{"otp"}` → read-only access for 15 min. Single use. Wrong codes all get one answer; 5 failures lock the account for 15 min. |
| GET | `/api/pharmacy/grants`, `/api/pharmacy/grants/{id}` | Open and ended access. A grant that has ended is **`410`** on every route. |
| GET | `/api/pharmacy/grants/{id}/pages/{n}/image` | Original page, `no-store`, during access only |
| POST | `/api/pharmacy/grants/{id}/checks` | One pack: `method` = `qr` (`qr_text`) / `photo` (`image`) / `manual` (`brand`, `generic_name`, `strength`, `form`), plus `batch`, `expiry`, `quantity` |
| POST | `/api/pharmacy/grants/{id}/checks/{check_id}/confirm` | Chemist confirms an orange result. Red / grey can never be confirmed. |
| GET | `/api/pharmacy/records` | The minimal dispensing record; survives the end of access |

**Access.** Doctor routes need a signed-in **doctor whose account an administrator
has approved** (`403` until then; the same for pharmacy routes) and see only their own
consultations (anything else is `404`); pages backfilled from before ownership was
recorded stay visible to every doctor. Images are served at `/uploads/{name}` to the
owning doctor only. `corrected_by` / `finalised_by` come from the session, never the
request body.

**Versions.** Finalising writes a `ConsultationVersion` snapshot. Patients and
pharmacies read **only** snapshots, so a doctor's later edit reaches nobody until it
is finalised as the next version, labelled "revised". Re-addressing to another phone
also takes effect only on finalise.

**Patient access:** the latest version is addressed to the patient's own **verified**
phone, or `404`. The patient view leaves out `safer_alternative` and says "discuss
with your doctor or pharmacist" (Workflow stage 4). The PDF (`backend/rx_pdf.py`) is
rendered from that same view, includes every page and a QR to
`PUBLIC_BASE_URL/check/<id>`, and prints times in `DISPLAY_TZ`.

**Dispensing** (`ml_pipeline/dispense/compare.py`): a pack is compared on
**composition and strength**, never the brand string: `match`, `substitute`,
`strength_mismatch` (chemist must confirm), `wrong_medicine` (blocked),
`could_not_verify` (blocked, never shown as a match). Dispensing carries across
versions by what the medicine is, so a revision does not un-dispense a pack.

**Every POST/PUT/PATCH/DELETE to `/api/*` must send `X-Requested-With: PrescriptAI`**
(CSRF guard). `web_app` sets it on every request; curl and scripts must add it.

**Database** (`backend/models.py`, SQLite at `backend/prescriptai.db` by default):

- `consultations`: owner, patient name + phone, clinic, diagnosis + visibility,
  `allow_substitution`, `status` (draft / finalised / revising), `current_version`
- `consultation_versions`: immutable snapshots, `verification_id`, `patient_phone`
- `prescriptions` (one row per **page**): image path + sha256, `model` /
  `prompt_version` / `effort`, `raw_response`, `page_read_json`, `resolved_json`,
  header fields, cost, `from_cache`, `reviewed_at` / `reviewed_by`,
  `consultation_id`, `page_no`
- `access_grants`, `dispense_checks`, `dispensing_records`: Workflow stages 5–7
- `corrections`: before **and** after, `correction_type`
  (`misread` / `hallucinated` / `missed` / `wrong_field` / `other`), model
  confidence, match score, outcome, verification, who, when
- `interaction_runs`: S4 result per page and per consultation (across all pages)
- Auth tables: see §6a

---

## 6. Core concepts

Each medicine carries **two independent facts**. The UI shows both.

**`outcome`: how sure we are about what was *written*** (S2)

| Outcome | Meaning |
|---|---|
| `confirmed` | Strong read, clean vocabulary match, clear winner |
| `probable` | Accepted but flagged: weak VLM confidence, or a near-miss match |
| `ambiguous` | Narrowed to N candidates; the doctor picks one (buttons, never free text) |
| `unmatched` | Read clearly, but not in the medicine database |
| `illegible` | Could not read it |

**`verification`: how sure we are about *which drug* it is** (S3, per ingredient, NLEM 2022)

| Tier | Meaning |
|---|---|
| `verified` | Every ingredient is on NLEM |
| `partial` | Some are, some are not (common for combinations) |
| `unverified` | None are. This means "not on the essential list", **not** "not a real drug". |
| `unknown` | Brand not found in the dataset |

**Other fields worth knowing:**

- `raw_reading`: the model's primary reading, verbatim. Always preserved.
- `matched_via`: set when an *alternative* reading, not the primary, produced the match.
- `candidates`: for `ambiguous`
- `needs_hard_confirmation`: the UI must make the doctor confirm this one explicitly
- `skipped` (on the interaction result): every medicine or ingredient excluded from
  S4, with the reason

**Confidence works as a downgrade, not a veto:** VLM confidence ≥ 0.60 can reach
CONFIRMED; below 0.60 it is capped at PROBABLE; below 0.45 it is ILLEGIBLE. Real
Opus 5 confidences on handwritten brands cluster at 0.50–0.62, so a hard veto at
0.60 threw away correct reads.

---

## 6a. Authentication

One account system for the three stakeholders in [`Workflow.md`](Workflow.md):
**doctor**, **patient** and **chemist** (shown as "Pharmacy"), plus an
**administrator** who approves the first and last. Code lives in `backend/auth/`,
`backend/admin.py` and `web_app/src/pages/auth/`. Passwords, tokens and TOTP use only
the Python standard library; encrypting TOTP secrets at rest uses `cryptography`.

| Capability | How it works |
|---|---|
| **Register** with email, phone **or** username | At least one. Stored normalised: email lowercased, phone to E.164 (a bare 10-digit number is taken as +91), username lowercased (3–32 chars). Each is unique. Role-specific profile: doctor → speciality, clinic address (+ optional registration no.); chemist → pharmacy name, address (+ optional drug licence no.); patient → name only. |
| **Log in** | Any of the three identifiers + password. Wrong password and unknown user give the same message, the same timing (a dummy hash is computed) and the same throttling, so login can't be used to discover accounts. |
| **Log out** | Ends *this* session server-side and clears the cookie. Account page: end any single session, or every other one. |
| **Password reset / recovery** | 6-digit code to the account's email or phone (10 min, 5 attempts, single use, 60 s resend cooldown), **or** a single-use recovery code. `forgot` answers identically whether or not the account exists. A reset signs in nowhere, ends every session, and leaves MFA on. Username-only accounts get 10 recovery codes at sign-up, because nothing else can recover them. |
| **Sessions** | Opaque 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie; only its SHA-256 is stored. Ends after `SESSION_IDLE_MINUTES` idle (60) or `SESSION_MAX_HOURS` (12). Changing the password ends other sessions. CSRF: required `X-Requested-With` header + explicit CORS origin list. |
| **MFA** | TOTP authenticator app (RFC 6238), QR + manual key. Enabling issues 10 recovery codes. After the password, login returns an `mfa_required` challenge (5 min, 5 tries), and no session exists until the code is right. A TOTP code can't be replayed. Disabling needs password + code. |
| **Email / phone verification** | Code sent at sign-up and whenever a contact is added or changed (which needs the password). A successful reset by code also verifies that channel. |
| **Passwords** | scrypt (n=2¹⁵, r=8), per-password salt, parameters stored in the hash. 10–128 chars, a small common-password blocklist, must not contain the email/phone/username. |
| **Audit** | `auth_events`: register, login, login_failed, mfa_*, logout, password_*, *_verified, *_changed, session(s)_revoked, with ip and user agent. |
| **Throttling** | Per-IP and per-identifier sliding windows: 5 failed logins / 15 min per identifier → `429`. Counted in the database (`rate_limit_hits`), so limits are shared by every worker and survive a restart; keys are stored as a keyed hash. |
| **Vetting** | A doctor or pharmacy starts `pending` and gets `403` on every clinical route until an administrator approves it at `/admin`. Rejecting or withdrawing approval takes effect on the next request. Changing the name, registration number, pharmacy name or licence number afterwards sends the account back for review. Approval means a person reviewed the declared number; nothing looks it up in a register. `REQUIRE_VETTING=false` turns this off for local development. |
| **Administrators** | Created only on the server (`python manage.py create-admin`); `/api/auth/register` refuses the role. |
| **Secrets at rest** | TOTP secrets are encrypted (Fernet, `enc:v1:…`) with `DATA_ENCRYPTION_KEY`, or a key derived from `SECRET_KEY` if that is unset. Rotate by putting the new key first. |

**Routes** (`/api/auth/…`): `register`, `login`, `login/mfa`, `logout`, `me` (GET/PATCH),
`me/contact` (PUT), `verify/send`, `verify/confirm`, `password/forgot`,
`password/reset`, `password/change`, `mfa/setup`, `mfa/enable`, `mfa/disable`,
`recovery-codes`, `sessions` (GET), `sessions/{id}` (DELETE), `sessions/revoke-others`.

**Protecting a new route:** add `user: User = Depends(require_roles(Role.DOCTOR))`
(or `current_user` for any signed-in user) from `auth`. Never take the actor's
identity from the request body.

**Frontend:** `useAuthStore` holds the profile only (the token is unreadable by
design). `RequireAuth roles={[...]}` guards routes; a `401` from any API call signs
the UI out. After login: doctor → `/upload`, patient → `/patient`,
chemist → `/pharmacy`, admin → `/admin`. Navbar links depend on the role. A doctor
or pharmacy that is not approved yet sees a waiting screen in place of its pages.

**Phone codes.** `SMS_DELIVERY=twilio` sends a real text; `webhook` posts to any other
gateway; the default `email` is the prototype path (Mailpit in Docker), where a
"verified phone" is not proof of the phone. The real paths are built and tested
against a stand-in provider, and have never sent a real text (§12).

The full list of security features, and what is still open, is in
[`docs/API_IMPLEMENTATION.md`](docs/API_IMPLEMENTATION.md) §2.

---

## 7. Design rules: do not break these

These come from real bugs and measured failures. The details are in `claude.md`.

1. **Never silently correct in S1.** The prompt transcribes verbatim ("Dijoxin" stays
   "Dijoxin"). Correction happens in S2, where it is scored and auditable.
2. **`raw_reading` is always preserved,** even when a match replaces the name.
3. **Nothing is silently dropped from the safety check.** Exclusions go in `skipped`
   with a reason. "Checked 4 of 6" and "checked 6" are different claims.
4. **Only NLEM-verified ingredients of CONFIRMED/PROBABLE medicines enter S4.** A
   wrong mapping becomes a wrong safety verdict.
5. **The reading is always shown.** Verification changes how strong the warning is,
   never whether the result is visible.
6. **Never fabricate results in the frontend.** No mock fallbacks on API failure, no
   client-side interaction tables. A failure says it failed. (Both existed once and
   were removed.)
7. **Abstention is a valid answer.** There is no "Unrecognized" placeholder medicine.
8. **Anything that changes the output belongs in the cache key:** image hash, prompt
   version, prompt fingerprint, model, effort. Editing the prompt without bumping
   `PROMPT_VERSION` is safe because the fingerprint changes too.
9. **Fail loudly on missing data.** The DDI loader raises `DDILoadError` instead of
   falling back to a handful of hardcoded rules. The medicines loader raises if the
   CSVs are missing.
10. **On duplicate DDI rules with conflicting severity, the most severe wins** (e.g.
    Nitroglycerin + Sildenafil was listed as both Major and Minor).
11. **Matching is case- and punctuation-insensitive** (`match_key()`). Prescriptions
    are often in block capitals, and Indian brands mix `-`, `.` and spaces.
12. **Thresholds live in `config/thresholds.yaml`, not in code.** Do not tune them
    against the test split.
13. **Log confirmations, not only corrections.** A corpus of only edits is biased.
14. **The headline metric is coverage, not accuracy.** The system is allowed to abstain.
15. **Patient data is behind a role check on the server.** The UI guard is a
    convenience; the API dependency is the control. The actor in an audit field
    always comes from the session.
16. **Auth endpoints must not reveal whether an account exists** (login, forgot,
    reset). Keep messages, timing and throttling identical.

---

## 8. Configuration

### Environment (`.env` at the project root; real env vars override it)

| Variable | Default | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Claude page + pack readers. Empty = mock reader. |
| `READER` | `auto` | `auto` (Claude if a key is set, else mock) / `claude` / `mock` / `mock_negative` |
| `PRESCRIPTAI_DATA_DIR` | `ml_pipeline/data/india` | where the two medicine CSVs are |
| `SHARE_REDEEM_MINUTES` / `SHARE_WINDOW_MINUTES` | `5` / `15` | pharmacy code lifetime / access window |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | what the PDF's QR opens |
| `PRESCRIPTAI_SPEND_CAP` | `30` (USD) | spend guard; refuses calls past this |
| `DATABASE_URL` | `sqlite:///backend/prescriptai.db` | backend |
| `SQL_ECHO` | off | set `1` to log SQL |
| `VITE_API_URL` | `http://localhost:8000` | web_app |
| `SECRET_KEY` | dev default (warned at startup) | HMAC key for one-time and recovery codes. **Set it.** |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | `60` / `12` | session lifetime |
| `COOKIE_SECURE` | `false` | must be `true` behind HTTPS |
| `SESSION_COOKIE_NAME` | `prescriptai_session` | |
| `CORS_ORIGINS` | localhost:5173, 127.0.0.1:5173, :3001, :3000 | origins allowed to send the session cookie |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | unset | email delivery for codes; unset → printed to the console |
| `SMTP_STARTTLS` | `true` | `false` for a local server without TLS (Mailpit) |
| `SMS_DELIVERY` | `email` | `twilio` / `webhook` send a real text. `email` (prototype) sends the code **by email**, to the account's email or else `<digits>@SMS_EMAIL_DOMAIN`; `console` prints it. |
| `SMS_EMAIL_DOMAIN` | `sms.prescriptai.local` | Stand-in address for phone-only accounts; only a catch-all inbox like Mailpit receives it |
| `DEMO_INBOX_URL` | unset | Shown on the verify page as "Open the demo inbox" |
| `REQUIRE_VETTING` | `true` | Doctors and pharmacies need an administrator's approval. `false` for local development only |
| `DATA_ENCRYPTION_KEY` | derived from `SECRET_KEY` | Fernet key(s) for TOTP secrets, comma separated, newest first. `python manage.py new-key` |
| `RATE_LIMIT_BACKEND` | `database` | `memory` for throwaway runs |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM` (or `TWILIO_MESSAGING_SERVICE_SID`) | unset | For `SMS_DELIVERY=twilio` |
| `SMS_WEBHOOK_URL` / `SMS_WEBHOOK_TOKEN` | unset | For `SMS_DELIVERY=webhook`: POSTs `{to, message, purpose}`. HTTPS only, except localhost |
| `READER_MAX_CONCURRENCY` / `READER_MAX_RETRIES` / `READER_TIMEOUT_SECONDS` | `4` / `2` / `90` | API calls in flight; SDK retries; seconds per attempt |
| `READER_FALLBACKS` | `default` | `off` never sends the refusal-fallback parameter |
| `PAGE_READS_PER_DOCTOR_PER_DAY` | `200` | Reader calls per doctor per 24 h (0 = no limit) |
| `PRESCRIPTAI_UPLOAD_DIR` / `PRESCRIPTAI_CACHE_DIR` | `backend/uploads` / `ml_pipeline/.cache` | Page images; read cache and spend ledger |
| `FORWARDED_ALLOW_IPS` | localhost | Which proxy may set the client address. `*` only when the app's port is not published |
| `REDIS_URL`, `CELERY_*`, `ML_SERVICE_URL` | | in `settings.py` but **unused**: scaffolding |

### Thresholds ([`ml_pipeline/config/thresholds.yaml`](ml_pipeline/config/thresholds.yaml))

| Key | Value | Meaning |
|---|---|---|
| `matching.confirm_score` | 88 | Fuzzy score needed for CONFIRMED |
| `matching.confirm_margin` | 6 | Required lead over the runner-up |
| `matching.abstain_score` | 82 | Below this, abstain |
| `matching.candidate_limit` | 5 | Candidates shown for AMBIGUOUS |
| `matching.collapse_same_composition` | true | Two brands of the same composition don't count as ambiguous (reviewer load 19% → 4%) |
| `reading.confident_read` | 0.60 | VLM confidence needed to reach CONFIRMED |
| `reading.abstain_below` | 0.45 | Below this, ILLEGIBLE |
| `nlem.enabled` | true | Per-ingredient NLEM verification |

**Every number here is a guess.** Calibrating them against logged doctor corrections
is roadmap tier T0 (§14).

---

## 9. Data

| Data | In git? | Notes |
|---|---|---|
| `ml_pipeline/data/ddi_dataset/DDI Database.json` | ✅ | 180 interactions, 212 drugs, 175 edges, with severity, mechanism, safer alternative, citation (e.g. DrugBank 6.0) |
| `ml_pipeline/data/india/indian_medicines_master.csv` | ❌ gitignored | 239,640 brands. Columns: `Brand Name, Composition, Strength, Form, Manufacturer`. **Required for S2/S3.** |
| `ml_pipeline/data/india/nlem2022_formulations.csv` | ❌ gitignored | NLEM 2022: 241 molecules + 63 aliases. Columns include `molecule`, `aliases`. **Required.** |
| `ml_pipeline/data/india/nlem_verified_medicines.csv` | ❌ gitignored | 40,451 brands pre-joined to NLEM codes. **Not read by any code; optional.** |

**Required formats** (read with `pandas.read_csv`, UTF-8, header row required; extra columns are ignored):

- `indian_medicines_master.csv`: all five columns must exist (blanks are allowed). `Brand Name`
  may include strength (`Crocin 650`), because the loader strips it for a second index.
  `Composition` is ingredient names joined by ` + ` (`Aceclofenac + Paracetamol`).
  Strengths in parentheses (`Paracetamol (500mg)`) are tolerated, but bare strengths
  (`Paracetamol 500mg`) break NLEM and DDI matching.
- `nlem2022_formulations.csv`: `molecule` is one generic name per row; `aliases` holds
  alternative names separated by `;` or `,` (may be blank).
| `VLM_CHECK/` eval images | ❌ gitignored | Real prescriptions with patient names and prescriber details (PHI) |

**Get the CSVs from a teammate**, since they aren't in the repo. Rejected sources,
so nobody re-evaluates them: `India Medicines and Drug Info Dataset.csv` (64.6% of
compositions truncated) and `CDSCO.pdf` (one page, about 4 entries).

**Privacy:** uploaded images, the VLM response cache and the database all contain
patient data and are gitignored. Keep it that way.

---

## 10. Cost control

- **Measured:** ~$0.034 per prescription on Opus 5 at `effort: medium` (input ~1.2k,
  cached prefix ~3.2k, output ~1.1k tokens). Thinking tokens were 0 in practice.
- **Prompt caching** is on. The ~3k-token system prompt is read at 0.1×.
- **Response cache** (`CachedPageReader`): the same image + prompt + model + effort
  never costs twice. Most development re-runs exercise S2–S4 and the UI, so they cost
  nothing.
- **Spend guard** (`vlm/spend.py`): prices from real `response.usage`, keeps a ledger,
  and raises `SpendCapExceeded` before any call that would cross
  `PRESCRIPTAI_SPEND_CAP`. An unknown model raises rather than being priced at $0.
- Check with `GET /api/health` or `python -m vlm.spend`.
- Image downscaling in S0 cut image tokens by 65% on the eval set.

---

## 11. Current status

*As of the last journal entry (Session 10). `claude.md` is the source of truth.*

| Stage | Status |
|---|---|
| Shared contracts (`schemas.py`) | ✅ built, tested |
| S0 ingest | ✅ tested on real images |
| S1 prompt, mock reader, response cache, spend guard | ✅ |
| S1 Claude reader | ✅ verified against the live API (`messages.parse()` with an image works) |
| S2 vocabulary constraint | ✅ all paths tested except AMBIGUOUS on real data |
| S3 brand → generic + NLEM | ✅ rebuilt on Indian data |
| S4 DDI graph + checker | ✅ 180 rules, 8 severity conflicts resolved |
| S5 review screen + correction log | ✅ |
| Backend API | ✅ |
| web_app | ✅ retheme done, `vite build` passes, renamed MedScript → PrescriptAI |
| Authentication (all three roles) | ✅ built (Session 7): 77 API checks + 21 browser checks passed |
| Patient view + PDF download (Workflow stage 4) | ✅ built (Session 8): 34 API checks + 11 browser checks passed |
| Phone codes by email (prototype) | ✅ built (Session 8): Mailpit demo inbox; 13 API + 5 browser checks. No real SMS yet. |
| Docker hosting | ✅ one image, `localhost:8000`, mock reader (Session 8) |
| Consultations, multi-page, finalise + versions, diagnosis (stages 1–3) | ✅ built (Session 9) |
| Pharmacy sharing by OTP, pack checks, expiry, dispensing record (stages 5–7) | ✅ built (Session 9), in `web_app` under `/pharmacy` |
| QR verification (`/check/<id>`) | ✅ built (Session 9) |
| Session 9 checks | 23 dispensing unit checks, 98 API checks (incl. the upload-XSS regression), 38 browser checks under the new CSP, on the mock reader + test fixture |
| Upload stored-XSS (UPDATES_SINCE_LOGIN §6) | ✅ fixed (Session 9): extension from the real image format, images served with a fixed type + `nosniff`, security headers + CSP on every response |
| API calling: parallel multi-page reads, duplicate handling, refusals, safe errors | ✅ built (Session 10); not yet run against the live API. [`docs/API_IMPLEMENTATION.md`](docs/API_IMPLEMENTATION.md) |
| Vetting of doctors and pharmacies, administrators | ✅ built (Session 10) |
| TOTP secrets encrypted; rate limits in the database; Alembic migrations | ✅ built (Session 10) |
| Real SMS (Twilio / webhook) | 🟡 built and tested against a stand-in; never sent a real text |
| HTTPS deployment, non-root container | ✅ built (Session 10); checked locally with Caddy's own certificate |
| Tests in the repo | ✅ `python tests/run.py`: 89 unit, 137 + 6 API, 50 browser checks |

**Open items**

1. A strength suffix in `drug_token` (`Monas 10` vs `Monas`) can fall below the
   abstain floor. Watch real output before changing anything.
2. AMBIGUOUS has not yet fired on a real page.

---

## 12. Known issues and stale files

These come from the pre-v2 prototype. They are wrong, so don't trust them.

| File | Problem |
|---|---|
| `backend/config/settings.py` | `ML_SERVICE_URL`, JWT, Redis and Celery settings are unused scaffolding. |
| Real SMS | Built (`twilio`, `webhook`) and tested against a local stand-in, but it has never sent a real text: that needs a provider account, and for Indian numbers a DLT-registered sender and template. Until `SMS_DELIVERY` is changed, phone codes go by email and a "verified phone" is not proof of the phone. |
| Spend ledger | Per process (a JSON file plus an in-memory reservation). Run one API worker per ledger; move it into the database before running several. |
| Encryption key | TOTP secrets are encrypted, but the key lives in the server's environment. That covers a leaked database or backup, not a compromised server; a KMS-held key is the next step. |
| Vetting is manual | An administrator reviews the registration / licence number by hand. Nothing checks a medical-council or drug-licence register: there is no public API for either (Workflow.md §10 Q5). |
| Patient medicine information | "What it is used for" needs a content source; only the composition is shown. |
| Pack QR codes | Only JSON, `Key: value`, GS1 (batch/expiry only) and plain-text payloads are parsed. A link-only QR is reported as "could not verify" (Workflow §10 Q9). |
| Live API | The reader was rewritten in Session 10 and has not been run against the live API since. The checklist is `docs/API_IMPLEMENTATION.md` §1.9. `fastapi.testclient` is still broken here (`httpx` 0.28 vs Starlette 0.35), so the suites in `tests/` drive a real server. |

---

## 13. Deliberately not built

Recorded so these aren't mistaken for oversights or re-proposed:

- **TrOCR in any form.** Measured at 6% / 14%.
- **BioBERT or any learned DDI classifier.** A cited lookup table is better on
  auditability, accuracy, data cost and failure mode (ARCH §9.3).
- **Dual-VLM cross-check.** While a doctor reviews every page, their corrections are
  ground truth. Revisit at the Phase 1 → 2 boundary.
- **S2 VLM tiebreak.** AMBIGUOUS goes to the human instead.
- **RL / training scaffolding, stub `rl/` packages, `# TODO: policy update`.** The
  prerequisite is captured data, not code.
- **Open-weights migration.** A separate architectural decision, deferred.

---

## 14. Roadmap

Continuous improvement is driven by the correction log (ARCH §16):

| Tier | Unlocks at | What |
|---|---|---|
| Regression suite | ~100 corrections | Replay logged pages through the current prompt, diff against known answers |
| **T0 threshold calibration** | ~200 corrections | Fit `thresholds.yaml` to observed correction rates. No GPU. Biggest early win. |
| T1 few-shot retrieval | ~500 | Inject similar past corrections into the S1 prompt |
| T2 SFT / DPO | ~5,000 + GPU | Train an open VLM. Means leaving the Claude API. |
| T3 RL | n/a | Not planned |

**Before starting any of it:** confirm stored rows have `raw_response`,
`prompt_version`, confidence and alternatives populated, then count corrections by
type. Build the regression suite first.

Other next steps: run the live-API checklist (`docs/API_IMPLEMENTATION.md` §1.9),
find a way to distribute the medicine CSVs, and possibly expand DDI coverage for
acute outpatient drugs.

---

## 15. Where to read more

| Question | Read |
|---|---|
| Why is it built this way? What was rejected? | [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md): §1 TL;DR, §3 rejected approaches, §5 stage specs, §6 VLM choice + cost, §8 human-in-the-loop, §16 roadmap |
| What actually happened, including bugs and measurements? | [`claude.md`](claude.md), the implementation journal, newest session first |
| How was the work split? | [`docs/DAY1_PLAN.md`](docs/DAY1_PLAN.md) |
| How are API calls kept few and cheap? What protects the key and the data? | [`docs/API_IMPLEMENTATION.md`](docs/API_IMPLEMENTATION.md) |
| How do I run the checks? | `python tests/run.py` ([`tests/run.py`](tests/run.py)) |
| Exact data shapes | [`ml_pipeline/schemas.py`](ml_pipeline/schemas.py) |

`§` references in code comments point to `docs/ARCHITECTURE_V2.md`.


