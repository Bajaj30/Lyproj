import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft, Download, Loader2, Info, AlertTriangle, ShieldCheck, Stethoscope, Store, KeyRound,
  XCircle, CheckCircle2, PackageCheck, History, FileText,
} from 'lucide-react'
import {
  getMyPrescription, downloadMyPrescriptionPdf, myPrescriptionImageUrl, shareWithPharmacy,
  revokeShare, apiError,
} from '../../services/api'
import { DISPENSE_RESULT } from '../pharmacy/dispenseResults'

/**
 * One prescription, as the patient sees it (Workflow stages 4–5): the latest
 * FINALISED version, the PDF, and sharing with a pharmacy by one-time code.
 *
 * Wording rules (Workflow §4): interactions say "discuss with your doctor or
 * pharmacist" and never suggest a switch; "no interactions" always comes with
 * what was not checked.
 *
 * Sharing: the code opens THIS prescription for ONE pharmacy, once, for a short
 * window. The patient sees who has it and until when, and can end it early.
 * The server enforces all of that; this screen only shows it.
 */
const SEVERITY = {
  severe: { cls: 'severity-severe', label: 'Serious' },
  moderate: { cls: 'severity-moderate', label: 'Moderate' },
  minor: { cls: 'severity-minor', label: 'Minor' },
}

const formatDay = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
const formatTime = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function useNow(active) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return undefined
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return now
}

const mmss = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function PatientPrescriptionPage() {
  const { id } = useParams()
  const [rx, setRx] = useState(null)
  const [error, setError] = useState(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(null)
  const [issuedCode, setCode] = useState(null)  // { otp, redeem_by, grant_id, window_minutes }
  const [shareError, setShareError] = useState(null)
  const [sharing, setSharing] = useState(false)

  const load = useCallback(() => getMyPrescription(id).then(setRx).catch((e) => setError(apiError(e))), [id])
  useEffect(() => { load() }, [load])

  const active = rx?.shares?.find((s) => s.status === 'active')
  // The code is only worth showing while the server still has it as unused.
  const issued = issuedCode && rx?.shares?.find((s) => s.id === issuedCode.grant_id)
  const code = issuedCode && (!issued || issued.status === 'issued') ? issuedCode : null
  const waiting = !!code
  const now = useNow(!!(waiting || active))

  // While a code is waiting to be used, or a pharmacy has access, refresh so the
  // patient sees "opened by …" and the end of access without reloading.
  useEffect(() => {
    if (!waiting && !active) return undefined
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [waiting, active, load])

  const download = async () => {
    setDownloading(true)
    setDownloadError(null)
    try { await downloadMyPrescriptionPdf(id) }
    catch (e) { setDownloadError(apiError(e, 'The PDF could not be created. Please try again.')) }
    finally { setDownloading(false) }
  }

  const share = async () => {
    setSharing(true)
    setShareError(null)
    try { setCode(await shareWithPharmacy(id)); await load() }
    catch (e) { setShareError(apiError(e)) }
    finally { setSharing(false) }
  }

  const revoke = async (grantId) => {
    setShareError(null)
    try { await revokeShare(grantId); setCode(null); await load() }
    catch (e) { setShareError(apiError(e)) }
  }

  const back = (
    <Link to="/patient" className="btn-ghost -ml-2 mb-4 inline-flex">
      <ArrowLeft size={16} /> Your prescriptions
    </Link>
  )

  if (error) return <div className="mx-auto max-w-3xl px-4 sm:px-6">{back}<p role="alert" className="card text-sm text-vital-800">{error}</p></div>
  if (!rx) return <p className="mx-auto flex max-w-3xl items-center gap-2 px-4 text-ink-600 sm:px-6"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</p>

  const codeLeft = code ? new Date(code.redeem_by).getTime() - now : 0
  const accessLeft = active ? new Date(active.expires_at).getTime() - now : 0
  const history = rx.shares.filter((s) => !['issued', 'active'].includes(s.status) && s.status !== 'cancelled')

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
            {rx.date && <>Dated {rx.date} · </>}finalised {formatDay(rx.finalised_at)}
            {' · '}<span className="data whitespace-nowrap">{rx.verification_id}</span>
          </p>
        </div>
        <div className="w-full sm:w-auto">
          <button type="button" onClick={download} disabled={downloading} className="btn-secondary w-full sm:w-auto">
            {downloading ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
            Download PDF
          </button>
          {downloadError && <p role="alert" className="mt-2 text-sm text-vital-700">{downloadError}</p>}
        </div>
      </header>

      {rx.revised && (
        <div className="card mb-4 border-amber-300 bg-amber-50 text-sm text-amber-900">
          <p className="flex items-center gap-2 font-semibold"><History size={16} /> Revised · version {rx.version}</p>
          <p className="mt-1">{rx.revised_notice}</p>
          {rx.change_note && <p className="mt-1">What changed: {rx.change_note}</p>}
        </div>
      )}

      <p className="card mb-6 flex gap-2.5 border-care-200 bg-care-50 text-sm leading-relaxed text-care-900">
        <Info size={17} className="mt-0.5 shrink-0" /> {rx.notice}
      </p>

      {/* ── share with a pharmacy (stage 5) ─────────────────────────────── */}
      <section className="card mb-6 border-care-200" aria-live="polite">
        <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
          <Store size={18} className="text-care-700" /> Get it from a pharmacy
        </h2>

        {active ? (
          <div className="mt-3 rounded-lg border border-care-300 bg-care-50 p-4">
            <p className="font-semibold text-care-900">Shared with {active.pharmacy_name} until {formatTime(active.expires_at)}</p>
            <p className="mt-0.5 text-sm text-care-800">They can see this prescription, read-only, for <span className="data">{mmss(accessLeft)}</span> more.</p>
            <button type="button" onClick={() => revoke(active.id)} className="btn-secondary mt-3 py-2 text-sm">
              <XCircle size={16} /> End access now
            </button>
          </div>
        ) : waiting && codeLeft > 0 ? (
          <div className="mt-3 text-center">
            <p className="text-sm text-ink-600">Tell the chemist this code, or show them this screen.</p>
            <p className="data mt-2 select-all text-5xl font-bold tracking-[0.25em] text-ink-900" aria-label={`Code ${code.otp.split('').join(' ')}`}>
              {code.otp.slice(0, 3)}<span className="text-ink-300"> </span>{code.otp.slice(3)}
            </p>
            <p className="mt-2 text-sm text-ink-600">
              Works once, for this prescription only. Use it within <span className="data font-semibold">{mmss(codeLeft)}</span>.
              The pharmacy then has {code.window_minutes} minutes.
            </p>
            <button type="button" onClick={() => revoke(code.grant_id)} className="btn-ghost mx-auto mt-2">Cancel this code</button>
          </div>
        ) : (
          <>
            <p className="mt-2 text-sm text-ink-600">
              Get a one-time code for the chemist. It opens this prescription only, for one pharmacy, for {code?.window_minutes ?? 15} minutes. They never see your phone number or your other prescriptions.
            </p>
            {waiting && <p className="mt-2 text-sm text-amber-800">Your last code expired before it was used.</p>}
            <button type="button" onClick={share} disabled={sharing} className="btn-primary mt-3 w-full sm:w-auto">
              {sharing ? <Loader2 size={16} className="animate-spin" /> : <KeyRound size={16} />} Share with a pharmacy
            </button>
          </>
        )}
        {shareError && <p role="alert" className="mt-2 text-sm text-vital-700">{shareError}</p>}

        {history.length > 0 && (
          <details className="mt-4 border-t border-ink-100 pt-3 text-sm">
            <summary className="cursor-pointer font-medium text-ink-700">Who has had access</summary>
            <ul className="mt-2 space-y-1 text-ink-600">
              {history.map((s) => (
                <li key={s.id}>
                  {s.pharmacy_name
                    ? <>{s.pharmacy_name}: opened {formatDay(s.redeemed_at)} {formatTime(s.redeemed_at)}, {s.status === 'revoked' ? 'ended by you' : 'access ended'} {s.ended_at && formatTime(s.ended_at)}</>
                    : <>Code from {formatTime(s.issued_at)}: {s.status === 'revoked' ? 'cancelled' : 'not used'}</>}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

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
            <Field label="Diagnosis" value={rx.diagnosis} />
          </dl>
        </section>
      )}

      <section className="mb-6">
        <h2 className="mb-3 font-display text-2xl font-normal text-ink-900">Medicines</h2>
        {rx.medicines.length === 0 && <p className="card text-sm text-ink-600">No medicines were recorded on this prescription.</p>}
        <ol className="space-y-3">
          {rx.medicines.map((m) => (
            <li key={m.index} className="card">
              <div className="flex flex-wrap items-center gap-2">
                {m.name
                  ? <p className="font-display text-xl font-semibold text-ink-900">{m.name}</p>
                  : <p className="flex items-center gap-2 font-semibold text-vital-800">
                      <AlertTriangle size={17} /> Could not be read reliably. Ask your doctor or pharmacist.
                    </p>}
                {m.dispensed && <span className="chip ml-auto border-care-300 bg-care-50 text-care-800"><CheckCircle2 size={12} /> Dispensed</span>}
              </div>
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
        {!rx.allow_substitution && <p className="mt-2 text-sm text-ink-600">Your doctor asked for these exact brands: the pharmacy may not substitute another.</p>}
      </section>

      {rx.dispensing.length > 0 && (
        <section className="card mb-6">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
            <PackageCheck size={18} className="text-care-700" /> What the pharmacy checked
          </h2>
          <p className="mt-1 text-sm text-ink-600">Each pack was compared with this prescription by its ingredients and strength.</p>
          <ul className="mt-3 space-y-2">
            {rx.dispensing.map((c) => {
              const r = DISPENSE_RESULT[c.result] ?? DISPENSE_RESULT.could_not_verify
              return (
                <li key={c.id} className={`rounded-lg border p-3 text-sm ${r.cls}`}>
                  <p className="font-semibold">{r.label}: {c.pack_brand || c.pack_composition || 'unidentified pack'}{c.medicine_label && <> for {c.medicine_label}</>}</p>
                  {c.reasons.length > 0 && <p className="mt-0.5">{c.reasons.join(' ')}</p>}
                  <p className="mt-0.5 text-xs opacity-80">{c.pharmacy_name} · {formatDay(c.created_at)} {formatTime(c.created_at)}{c.dispensed ? ' · handed over' : ''}</p>
                </li>
              )
            })}
          </ul>
        </section>
      )}

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
        <h2 className="mb-3 flex items-center gap-2 font-display text-2xl font-normal text-ink-900">
          <FileText size={20} className="text-care-700" /> Original prescription
        </h2>
        <div className="space-y-4">
          {rx.page_numbers.map((n) => (
            <figure key={n}>
              <img src={myPrescriptionImageUrl(rx.id, n)} alt={`Your doctor's handwritten prescription, page ${n}`}
                   className="w-full rounded-xl border border-ink-200 bg-white p-1 shadow-clinical" />
              {rx.page_numbers.length > 1 && <figcaption className="mt-1 text-center text-xs text-ink-500">Page {n} of {rx.page_numbers.length}</figcaption>}
            </figure>
          ))}
        </div>
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
