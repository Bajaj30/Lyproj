# Updates since the login pages

> **Update (Session 10, 16 Nov 2026):** open items 2, 7 and 8 are addressed too
> (real SMS built but untested live; vetting; encryption and shared rate limits).
> See [§9](#9-session-10-production-gaps-and-api-calling) and
> [`API_IMPLEMENTATION.md`](API_IMPLEMENTATION.md).
>
> **Update (Session 9, 14 Nov 2026):** the stored-XSS hole in §6 is **fixed**, and
> open items 1 and 3–6 below are done. See [§8](#8-session-9-the-rest-of-the-workflow)
> and [`claude.md`](../claude.md), Session 9.

This covers everything after commit `1bd1a0b` ("Add accounts for doctors, patients and
pharmacies"), up to 20 Oct 2026. The changes are on `main` at
[Sahil-Jawale/Lyproj](https://github.com/Sahil-Jawale/Lyproj), except the security
findings in §6, which came after the push. The detailed engineering log is
[`claude.md`](../claude.md), Session 8.

## Summary

| Area | What changed | Status |
|---|---|---|
| [Hosting](#1-hosting-the-whole-site-in-docker) | The whole site runs as one Docker container at `http://localhost:8000` | ✅ working |
| [Patient features](#2-patients-can-view-and-download-prescriptions) | Patients see the prescriptions a doctor confirmed for them, and download them as a PDF | ✅ working |
| [Phone verification](#3-phone-codes-are-sent-by-email-prototype) | Phone codes are emailed (no SMS provider yet). A local demo inbox shows them. | ✅ working, prototype only |
| [Bug fixes](#4-bugs-fixed-along-the-way) | Two security bugs in code delivery, plus several smaller ones | ✅ fixed |
| [Model research](#5-model-research-no-code-change) | Which vision model to use, via OpenRouter | 📝 advice only |
| [Security review](#6-security-review-ftp-sip-http) | FTP and SIP not applicable; one confirmed stored-XSS hole | ✅ fixed in Session 9 |
| [Session 9](#8-session-9-the-rest-of-the-workflow) | Consultations, versions, pharmacy sharing, pack checks, QR verification, API-key slot | ✅ working (mock reader) |
| [Git](#7-what-was-pushed) | Pushed to the fork's `main` without a force-push | ✅ done |

---

## 1. Hosting the whole site in Docker

**Before:** none of the deployment files worked.
- `backend/Dockerfile` copied from outside its build context and used Python 3.10. `settings.py` needs 3.12+, because it nests quotes inside an f-string.
- `backend/requirements.txt` still listed `torch` and was missing `anthropic`, `sqlalchemy`, `pandas` and `pyyaml`.
- `docker-compose.yml` described Postgres, Redis and an `ml_server` that no longer exist.

**Now:** one container serves both the API and the web app.

```bash
python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))" > .env.docker   # once
docker compose up --build -d     # site → http://localhost:8000, demo inbox → http://localhost:8025
docker compose down              # stop; data survives in volumes
```

- **One origin.** FastAPI serves the built React app (`WEB_DIST`), so the session cookie and the CSRF header need no CORS. In development, Vite still serves the UI on `:5173` as before.
- **Mock reader.** `READER=mock`: no API key goes into the image, and nothing is spent.
- **Localhost only.** Ports are bound to `127.0.0.1`, because doctor accounts aren't vetted yet.
- **Persistent data.** Three volumes hold SQLite, the uploaded images, and the read cache with the spend ledger.
- **Antivirus workaround.** On a machine whose antivirus intercepts HTTPS (Avast does here), put its root certificate in `certs/` and the build trusts it. `certs/README.md` explains how. The `.crt` files are gitignored, because each machine generates its own.

**Files:** `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `backend/requirements.txt`, `backend/main.py` (serves the SPA), `web_app/src/services/api.js` (an empty `VITE_API_URL` now means "same origin"), `certs/README.md`.

---

## 2. Patients can view and download prescriptions

This implements [Workflow.md](../Workflow.md) stage 4.

### How a prescription reaches a patient

Stage 1 (consultation sessions) isn't built, so nothing linked a prescription to a
patient. The interim link:

1. On the **review screen**, the doctor enters the patient's phone number. It is saved when they click Confirm.
2. The prescription appears in the patient's app only when **all three** hold:
   - a doctor has **confirmed** it;
   - it is **addressed** to a phone; and
   - that phone is the patient's own **verified** number.

Without the third condition, anyone could register with someone else's number. In
every other case the API returns `404`, so it never confirms that a prescription exists.

### What the patient sees

- **`/patient`**: a list of their prescriptions. If the phone isn't verified, the page explains why the list is empty, and **Verify it now** sends the code in one click.
- **`/patient/prescriptions/:id`**: the doctor's details, each medicine with how it was written on the page, interactions, what was **not** checked, the original photo, and a **Download PDF** button.

**Wording follows Workflow §4,** because patients read this without a clinician beside them:
- **No "safer alternative".** That is a prescribing decision, and a patient might switch on their own.
- **Warnings say "discuss with your doctor or pharmacist",** never "stop taking".
- **"No interactions found" always comes with the list of what wasn't checked.**
- **Unreadable lines say "could not be read reliably",** with the verbatim reading kept.

### The PDF

It is rendered on the server from the same data as the screen, so the two can't disagree. It contains:
- doctor details;
- patient and date;
- medicines;
- interactions, with what was not checked;
- the AI/doctor-review notice;
- the original page;
- a **verification ID**: an HMAC of the prescription and its confirmation time, keyed by `SECRET_KEY`.

Details:
- **Fonts.** It uses a Unicode font (DejaVu in Docker, Arial on Windows), so dashes and Indian names render correctly.
- **Times** print in India time, labelled (`DISPLAY_TZ`, default `Asia/Kolkata`).
- **No QR code yet.** Nothing exists for a QR code to verify against.

### New API routes

| Method | Path | Who | Purpose |
|---|---|---|---|
| PUT | `/api/prescriptions/{id}/patient` | Doctor | Address a page to a phone (normalised to E.164; empty stops sharing) |
| GET | `/api/patient/prescriptions` | Patient | Inbox |
| GET | `/api/patient/prescriptions/{id}` | Patient | One prescription |
| GET | `/api/patient/prescriptions/{id}/image` | Patient | Original page |
| GET | `/api/patient/prescriptions/{id}/pdf` | Patient | PDF download |

**Audit trail.** Each view, download and change of recipient is logged (`rx_viewed`,
`rx_pdf_downloaded`, `rx_patient_set`).

**Database.** New column `prescriptions.patient_phone`. Existing databases get it
automatically: a startup step adds missing nullable columns, because SQLAlchemy's
`create_all` never alters an existing table.

**Files:** `backend/patient.py`, `backend/rx_pdf.py` (new; uses `fpdf2`), `backend/models.py`, `backend/main.py`, `web_app/src/pages/patient/*` (new), `web_app/src/pages/ReviewPage.jsx`, `web_app/src/App.jsx`, `web_app/src/services/api.js`.

**Tested:**
- **34 API checks.** Covered: access rules, other patients, wrong roles, anonymous access, CSRF, re-addressing, and the PDF itself.
- **11 browser checks**, in Chrome at phone width.

---

## 3. Phone codes are sent by email (prototype)

There is no SMS provider, so phone verification codes used to appear only in the
server log. For the prototype, they are now **emailed**:

- **To the account's email**, if it has one.
- **Otherwise to a stand-in address**, `<digits>@sms.prescriptai.local` (for example `918356045053@sms.prescriptai.local`).
- **Docker includes [Mailpit](https://github.com/axllent/mailpit)**, an open-source local inbox at **http://localhost:8025**. It accepts mail for any address and sends nothing onward, so no code leaves the machine.
- **The verify page links to the demo inbox.**

**Demo flow:**
1. Sign in as a patient and click **Verify it now**.
2. Open the demo inbox and copy the 6-digit code.
3. Enter it. You return to `/patient`.

**Settings:** `SMS_DELIVERY` (`email` | `console`), `SMS_EMAIL_DOMAIN`, `SMTP_STARTTLS`
and `DEMO_INBOX_URL`. To send real email, point `SMTP_*` at a real mail server.

**No fixed demo codes.** Codes stored in `.env` were considered and not added. With
an open-source inbox they aren't needed, and a code that always works would let anyone
verify any phone.

> **Limitation:** in this prototype a "verified phone" only proves the person could
> read that email. Replace `send_sms` in `backend/auth/notify.py` with a real SMS
> provider before real patients use it.

**Tested:** 13 API checks, including with the mail server stopped, and 5 browser checks at phone width.

---

## 4. Bugs fixed along the way

| Bug | Impact | Fix |
|---|---|---|
| **Forgot-password revealed real accounts when sending failed** | A mail error was a `500` for a real account but a `200` for a made-up one, so an attacker could tell which accounts exist | Delivery failures are now a handled `502`, which forgot-password answers identically for every account |
| **A failed send still started the 60 s resend timer** | It said "A code was just sent" when nothing was, and cancelled the user's previous working code | The code is saved only after a successful send; otherwise everything rolls back |
| PDF times were in UTC without a label | "08:14" was really 13:44 in India, and dates could be a day off | Times print in `DISPLAY_TZ`, labelled |
| PDF core fonts are Latin-1 only | A dash or a Devanagari name would break the PDF | Unicode font, with a Latin-1 fallback rather than a failed download |
| Demo-inbox link hidden on phones | The sign-in layout hides its sidebar below the `lg` breakpoint | The link sits inside the form |
| Verification ID wrapped mid-code on phones | Hard to read | Kept on one line |

---

## 5. Model research (no code change)

Recommendations for when the mock reader is switched off. Prices are from OpenRouter's live list as of 20 Oct 2026.

| Model | $ in / out per 1M tokens | Note |
|---|---|---|
| **`anthropic/claude-opus-5.5`** (recommended) | 4 / 20 | Same family as the Opus 5 the pipeline was tuned on, and cheaper. Est. ~$0.03–0.04 per prescription. |
| `anthropic/claude-sonnet-5.5` | 2 / 10 | About half the cost. Prefer it over Sonnet 5, which costs the same but is older. |
| `google/gemini-4-argon` | 2 / 10 (`:batch` 1 / 5) | Untested on handwriting. Needs a new reader. |

- **Batch versions** cost half but don't return straight away. Use them for offline tests, never for the upload screen.
- **Switching models means re-checking the confidence thresholds** in `ml_pipeline/config/thresholds.yaml`, which were tuned on Opus 5.
- **Sending patient images through OpenRouter** adds another company that sees them.
- **The cheapest switch** is changing `DEFAULT_MODEL` to `claude-opus-5-5` on the existing Anthropic key; no new reader is needed.

---

## 6. Security review: FTP, SIP, HTTP

| Protocol | Result |
|---|---|
| **FTP/FTPS** | **Not applicable.** No FTP code, no user-supplied URL fetching, and no FTP port. |
| **SIP** | **Not applicable.** No calling or VoIP code, and no SIP port. |
| **HTTP/HTTPS** | **Mostly protected, with one confirmed hole.** See below. |

**Tested and protected:**
- **Header injection** through the URL is blocked.
- **Request smuggling** is refused: a request with both `Content-Length` and `Transfer-Encoding` gets a `400`.
- **SQL injection** is prevented: every query uses the ORM with bound parameters.
- **CSRF** is prevented by the custom header plus the CORS allowlist.
- **The session cookie** is `HttpOnly` and `SameSite=Lax`.
- **Email header injection** is blocked by normalised inputs plus Python's email library.

**✅ Fixed in Session 9 (was confirmed): stored XSS through uploads.**
- The upload route keeps the extension from the client's filename, and `/uploads/` chooses the content type from that extension.
- So a valid JPEG named `rx.html` is served back as `text/html` from the site's own origin.
- In Chrome its script ran and called the logged-in API as the viewer. `.svg` gets through the same way.
- **Who can do it:** anyone who registers as a doctor (sign-up isn't vetted).
- **Who gets hit:** other doctors, and patients through the patient image route.

**Weaker spots:**
- **A path in the filename** (`x./../../escaped`, *closed in Session 9: the name is never used*): a `500` in Docker, but on a Windows dev server the file could be written outside `uploads/` (not tested).
- **No security headers:** no Content-Security-Policy, `nosniff` or `X-Frame-Options`. *(Added in Session 9.)*
- **Plain HTTP:** fine on localhost only. A real deployment needs TLS, `COOKIE_SECURE=true` and HSTS.

**The fix (Session 9), as planned:**
- Pick the stored extension from the image's real format, never the filename.
- Serve uploads with `nosniff` and a fixed image content type.
- Add a security-headers middleware: CSP, `nosniff`, `frame-ancestors 'none'`, Referrer-Policy.

---

## 7. What was pushed

- **Feature commit:** `e4edfb2`, "Host in Docker; patient prescription view and PDF download; phone codes by email".
- **README edit kept.** The commit was rebased onto the GitHub-only README edit `63024a0` (authors line removed), so that edit is preserved.
- **`main`'s older commit kept in history.** The fork's `main` had an older, superseded Track B commit (`0e90fb6`). It was merged with the "ours" strategy (`5a938c2`). The commit stays in history, the code stays current, and the push was a normal fast-forward, not a force-push.
- **Not pushed:** the fork's `auth-and-workflow` branch is behind `main`, and `Bajaj30/Lyproj` is untouched.
- **Avast on this machine also breaks git over HTTPS.** Use `git -c http.sslBackend=schannel push`, which uses the Windows certificate store.

---

## Open items

| # | Item |
|---|---|
| 1 | ~~Fix the stored-XSS upload hole (§6)~~ **done, Session 9** |
| 2 | ~~Real SMS provider~~ **built, Session 10** (Twilio or a webhook). It has never sent a real text; until `SMS_DELIVERY` is changed, codes still go by email |
| 3 | ~~Silent edits after confirming~~ **done:** immutable versions, labelled revisions |
| 4 | ~~One page per prescription~~ **done:** multi-page consultations |
| 5 | ~~No QR on the PDF~~ **done:** QR → public `/check/<id>` |
| 6 | ~~Pharmacy placeholders~~ **done:** stages 5–7 built |
| 7 | ~~Sign-ups are not vetted~~ **done, Session 10:** an administrator approves every doctor and pharmacy |
| 8 | ~~Encrypt `mfa_secret`; shared rate limiting~~ **done, Session 10:** encrypted at rest (key in the environment, not yet a KMS); limits live in the database |

---

## 8. Session 9: the rest of the workflow

Every Workflow.md stage is now built. It runs on the mock reader until an API key is added.

| Area | What changed |
|---|---|
| **API key slot** | `READER=auto`: the mock reader while `ANTHROPIC_API_KEY` is empty, Claude once it is set. Paste the key into `.env` (or `.env.docker`) and restart. The UI shows "Demo reader" while on the mock. |
| **Consultations** (stage 1) | Patient name + phone, clinic from the doctor's profile, up to 10 pages, one interaction check across all pages. Doctors see only their own. |
| **Review + finalise** (stage 3) | Correct (names re-resolve their ingredients), add missed lines, strike invented ones. Finalising writes an immutable version; later edits become a labelled revision. |
| **Diagnosis** | Read from the page verbatim (prompt v2), editable, with visibility: patient only, patient and pharmacy, or hidden. |
| **Sharing** (stage 5) | Patient gets a 6-digit, single-use code for one pharmacy; 15-minute read-only access; revocable. |
| **Pack checks** (stage 6) | QR, photo or typed; compared by ingredients and strength: match, substitute, strength/form mismatch, wrong medicine, could not verify. |
| **Expiry** (stage 7) | Enforced by the server on every request (`410`). The pharmacy keeps only a minimal dispensing record. |
| **QR verification** | The PDF's QR opens `/check/<id>`: genuine?, doctor, current or superseded. No patient data. |
| **Security** | The §6 upload XSS is fixed; security headers and a CSP on every response. |

Checks: 23 dispensing unit checks, 98 API checks, 38 browser checks, all passing on the
mock reader and a labelled test fixture (the real medicine CSVs are not in this
checkout). Not yet verified: the Claude pack reader live, and the Docker image rebuild.

---

## 9. Session 10: production gaps and API calling

The full account is [`API_IMPLEMENTATION.md`](API_IMPLEMENTATION.md) and
[`claude.md`](../claude.md), Session 10.

| Area | What changed |
|---|---|
| **API calling** | Every call goes through one module. A multi-page upload is one request, read in parallel. A page is never paid for twice: exact repeats cost nothing, and two pages that read the same are flagged before finalising. Refusals and cut-off replies are failures, never empty prescriptions |
| **Vetting** | Doctors and pharmacies start pending; an administrator (created only with `manage.py create-admin`) approves them at `/admin` |
| **Secrets** | Two-step secrets are encrypted in the database |
| **Rate limits** | Stored in the database: shared by every worker, kept across restarts |
| **SMS** | Twilio or a generic webhook. Built, never sent a real text |
| **Migrations** | Alembic; an existing database is adopted without data loss |
| **Docker** | The server runs as an unprivileged user; `docker-compose.prod.yml` adds HTTPS |
| **Tests** | In the repo: `python tests/run.py` |

**After pulling this, on an existing deployment:** every doctor and pharmacy account
becomes pending. Create an administrator and approve them; their data is untouched.

Still not verified: anything against the live API since the reader was rewritten, a
real text message, and a real domain certificate.
