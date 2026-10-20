import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft, Download, Loader2, Info, AlertTriangle, ShieldCheck, Stethoscope,
} from 'lucide-react'
import {
  getMyPrescription, downloadMyPrescriptionPdf, myPrescriptionImageUrl, apiError,
} from '../../services/api'

/**
 * One prescription, as the patient sees it, with the PDF download
 * (Workflow stage 4). The PDF is rendered server-side from this same view, so
 * the file and the screen cannot disagree.
 *
 * Wording rules (Workflow §4): interactions say "discuss with your doctor or
 * pharmacist" and never suggest a switch; "no interactions" always comes with
 * what was not checked.
 */
const SEVERITY = {
  severe: { cls: 'severity-severe', label: 'Serious' },
  moderate: { cls: 'severity-moderate', label: 'Moderate' },
  minor: { cls: 'severity-minor', label: 'Minor' },
}

const formatDay = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

export default function PatientPrescriptionPage() {
  const { id } = useParams()
  const [rx, setRx] = useState(null)
  const [error, setError] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)

  useEffect(() => {
    getMyPrescription(id).then(setRx).catch((e) => setError(apiError(e)))
  }, [id])

  const download = async () => {
    setDownloading(true)
    setDownloadError(null)
    try { await downloadMyPrescriptionPdf(id) }
    catch (e) { setDownloadError(apiError(e, 'The PDF could not be created. Please try again.')) }
    finally { setDownloading(false) }
  }

  const back = (
    <Link to="/patient" className="btn-ghost -ml-2 mb-4 inline-flex">
      <ArrowLeft size={16} /> Your prescriptions
    </Link>
  )

  if (error) return <div className="mx-auto max-w-3xl px-4 sm:px-6">{back}<p role="alert" className="card text-sm text-vital-800">{error}</p></div>
  if (!rx) return <p className="mx-auto flex max-w-3xl items-center gap-2 px-4 text-ink-600 sm:px-6"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</p>

  return (
    <div className="mx-auto max-w-3xl px-4 sm:px-6">
      {back}

      <header className="mb-6 flex flex-wrap items-end justify-between gap-4 animate-fade-in">
        <div className="min-w-0">
          <span className="rule-label text-care-700">Prescription</span>
          <h1 className="mt-2 font-display text-3xl font-normal text-ink-900 sm:text-4xl">
            {rx.doctor?.name ?? rx.prescriber_as_written ?? 'Your prescription'}
          </h1>
          <p className="mt-1 text-sm text-ink-600">
            {rx.date && <>Dated {rx.date} · </>}confirmed {formatDay(rx.reviewed_at)}
            {' · '}<span className="data whitespace-nowrap">{rx.verification_id}</span>
          </p>
        </div>
        <div className="w-full sm:w-auto">
          <button type="button" onClick={download} disabled={downloading} className="btn-primary w-full sm:w-auto">
            {downloading ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
            Download PDF
          </button>
          {downloadError && <p role="alert" className="mt-2 text-sm text-vital-700">{downloadError}</p>}
        </div>
      </header>

      <p className="card mb-6 flex gap-2.5 border-care-200 bg-care-50 text-sm leading-relaxed text-care-900">
        <Info size={17} className="mt-0.5 shrink-0" /> {rx.notice}
      </p>

      {rx.doctor && (
        <section className="card mb-6">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
            <Stethoscope size={18} className="text-care-700" /> Prescribed by
          </h2>
          <dl className="mt-3 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
            <Field label="Doctor" value={rx.doctor.name} />
            <Field label="Speciality" value={rx.doctor.speciality} />
            <Field label="Clinic" value={rx.doctor.clinic_address} />
            <Field label="Registration no." value={rx.doctor.registration_no} mono />
            <Field label="Patient" value={rx.patient_name} />
          </dl>
        </section>
      )}

      <section className="mb-6">
        <h2 className="mb-3 font-display text-2xl font-normal text-ink-900">Medicines</h2>
        {rx.medicines.length === 0 && <p className="card text-sm text-ink-600">No medicines were recorded on this prescription.</p>}
        <ol className="space-y-3">
          {rx.medicines.map((m, i) => (
            <li key={i} className="card">
              {m.name
                ? <p className="font-display text-xl font-semibold text-ink-900">{m.name}</p>
                : <p className="flex items-center gap-2 font-semibold text-vital-800">
                    <AlertTriangle size={17} /> Could not be read reliably. Ask your doctor or pharmacist.
                  </p>}
              {m.as_written && <p className="mt-0.5 text-sm text-ink-500">Written as “{m.as_written}”</p>}
              <dl className="mt-3 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-[auto_1fr]">
                <Field label="Contains" value={m.contains.join(' + ')} />
                <Field label="Dose" value={m.dosage} />
                <Field label="How often" value={m.frequency} />
                <Field label="For how long" value={m.duration} />
                <Field label="Instructions" value={m.instructions} />
              </dl>
            </li>
          ))}
        </ol>
      </section>

      <section className="card mb-6">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
          <ShieldCheck size={18} className="text-care-700" /> Medicines taken together
        </h2>
        {rx.interactions.length === 0
          ? <p className="mt-2 text-sm text-ink-700">No known interactions were found among the medicines that could be checked.</p>
          : <ul className="mt-3 space-y-3">
              {rx.interactions.map((it, i) => {
                const s = SEVERITY[it.severity] ?? { cls: 'severity-minor', label: it.severity }
                return (
                  <li key={i} className={`rounded-lg border p-3 ${s.cls}`}>
                    <p className="font-semibold">{s.label}: {it.drug_a} with {it.drug_b}</p>
                    {it.effect && <p className="mt-1 text-sm">{it.effect}</p>}
                    <p className="mt-1 text-sm font-medium">{it.advice}</p>
                  </li>
                )
              })}
            </ul>}
        {rx.not_checked.length > 0 && (
          <div className="mt-4 border-t border-ink-100 pt-3">
            <p className="text-sm font-semibold text-ink-700">Not checked for interactions</p>
            <ul className="mt-1 list-disc pl-5 text-sm text-ink-600">
              {rx.not_checked.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          </div>
        )}
      </section>

      <section className="mb-10">
        <h2 className="mb-3 font-display text-2xl font-normal text-ink-900">Original prescription</h2>
        <img src={myPrescriptionImageUrl(rx.id)} alt="Your doctor's handwritten prescription"
             className="w-full rounded-xl border border-ink-200 bg-white p-1 shadow-clinical" />
      </section>
    </div>
  )
}

function Field({ label, value, mono }) {
  if (!value) return null
  return (
    <>
      <dt className="text-ink-500">{label}</dt>
      <dd className={`text-ink-900 ${mono ? 'data' : ''}`}>{value}</dd>
    </>
  )
}
