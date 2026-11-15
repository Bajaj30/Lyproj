# PrescriptAI web app

React 19 + Vite + Tailwind. One app for all three roles: doctor, patient and
pharmacy. The project overview is the [root README](../README.md).

```bash
npm install
npm run dev        # http://localhost:5173, talks to the API on :8000
npm run build      # dist/; in Docker FastAPI serves it with VITE_API_URL=""
npm run lint
```

| Role | Routes |
|---|---|
| Doctor | `/upload` (new consultation), `/consultations/:id` (review, finalise, versions), `/history`, `/dashboard` |
| Patient | `/patient`, `/patient/prescriptions/:id` (PDF, share with a pharmacy, revoke) |
| Pharmacy | `/pharmacy` (enter code), `/pharmacy/grants/:id` (check packs), `/pharmacy/records` |
| Administrator | `/admin` (approve or reject doctor and pharmacy accounts) |
| Public | `/`, `/interactions`, `/check/:id` (verify a prescription PDF) |

Rules the screens keep:

- **The server enforces access; the UI only reflects it.** Route guards
  (`components/auth/RequireAuth.jsx`) avoid rendering screens that would 403. A
  doctor or pharmacy not yet approved sees `AwaitingVerification` instead.
- **No fabricated results.** A failed request shows a failure. The mock reader is
  labelled "Demo reader" (`components/DemoReaderBanner.jsx`).
- **Every unsafe request sends `X-Requested-With: PrescriptAI`** (`services/api.js`).
  The session is an HttpOnly cookie the page cannot read.
