import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { BadgeCheck, Loader2, ShieldX, History, Search } from 'lucide-react'
import { verifyDocument, apiError } from '../services/api'

/**
 * Public page the QR on a prescription PDF opens (/check/:id).
 *
 * It answers only: is this a genuine prescription, who issued it, and is it
 * the current version. It never shows the patient, the medicines or the
 * diagnosis: anyone holding the paper can scan it, so the answer must not be
 * a copy of the paper.
 */
export default function VerifyDocumentPage() {
  const { vid } = useParams()
  const navigate = useNavigate()
  // Results are kept with the ID they answer, so a new ID never shows the
  // previous ID's verdict while it loads.
  const [answer, setAnswer] = useState({ vid: null, r: null, error: null })
  const [input, setInput] = useState(vid ?? '')

  useEffect(() => {
    if (!vid) return
    verifyDocument(vid)
      .then((r) => setAnswer({ vid, r, error: null }))
      .catch((e) => setAnswer({ vid, r: null, error: apiError(e) }))
  }, [vid])
  const r = answer.vid === vid ? answer.r : null
  const error = answer.vid === vid ? answer.error : null

  return (
    <div className="mx-auto max-w-xl px-4 sm:px-6">
      <header className="mb-6">
        <span className="rule-label text-care-700">Verify</span>
        <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Check a prescription</h1>
        <p className="mt-2 text-sm text-ink-600">Enter the verification ID printed on a PrescriptAI prescription, or scan its QR code.</p>
      </header>

      <form className="mb-6 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (input.trim()) navigate(`/check/${input.trim().toUpperCase()}`) }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="PRX-XXXXX-XXXXX" className="input-field data flex-1 uppercase" aria-label="Verification ID" />
        <button className="btn-primary px-4" type="submit"><Search size={18} /><span className="sr-only">Check</span></button>
      </form>

      {vid && !r && !error && <p className="flex items-center gap-2 text-ink-600"><Loader2 className="h-4 w-4 animate-spin" /> Checking…</p>}
      {error && <p role="alert" className="card text-sm text-vital-800">{error}</p>}

      {r && !r.valid && (
        <section className="card border-vital-300 bg-vital-50 text-vital-900">
          <p className="flex items-center gap-2 font-display text-xl font-semibold"><ShieldX /> Not recognised</p>
          <p className="mt-2 text-sm">No prescription has the ID <span className="data">{r.verification_id}</span>. Check it was typed correctly. If it was, do not rely on this document.</p>
        </section>
      )}

      {r?.valid && (
        <section className={`card ${r.superseded ? 'border-amber-300 bg-amber-50' : 'border-care-300 bg-care-50'}`}>
          <p className={`flex items-center gap-2 font-display text-xl font-semibold ${r.superseded ? 'text-amber-900' : 'text-care-900'}`}>
            {r.superseded ? <History /> : <BadgeCheck />}
            {r.superseded ? 'Genuine, but replaced' : 'Genuine and current'}
          </p>
          {r.superseded && (
            <p className="mt-2 text-sm text-amber-900">
              This is version {r.version}. The doctor has since issued version {r.latest_version}. Ask the patient for the latest one.
            </p>
          )}
          <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm text-ink-800">
            <dt className="text-ink-500">ID</dt><dd className="data">{r.verification_id}</dd>
            <dt className="text-ink-500">Doctor</dt><dd>{r.doctor_name}</dd>
            {r.speciality && <><dt className="text-ink-500">Speciality</dt><dd>{r.speciality}</dd></>}
            <dt className="text-ink-500">Registration</dt><dd className="data">{r.registration_no || 'not given'}</dd>
            {r.clinic_address && <><dt className="text-ink-500">Clinic</dt><dd>{r.clinic_address}</dd></>}
            <dt className="text-ink-500">Finalised</dt><dd>{new Date(r.finalised_at).toLocaleString()}</dd>
            <dt className="text-ink-500">Medicines</dt><dd>{r.medicine_count}</dd>
          </dl>
          <p className="mt-4 text-xs text-ink-600">The patient and the medicines are not shown here, on purpose. A registration number is self-declared by the doctor and has not been checked against a medical council register.</p>
        </section>
      )}
    </div>
  )
}
