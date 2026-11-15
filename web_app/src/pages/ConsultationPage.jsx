import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  AlertTriangle, CheckCircle2, Loader2, ShieldAlert, Plus, FilePlus2, Trash2, History, Stethoscope,
  UserRound, Lock, BadgeCheck, Save, Copy,
} from 'lucide-react'
import MedicineCard from '../components/MedicineCard'
import DemoReaderBanner from '../components/DemoReaderBanner'
import {
  getConsultation, updateConsultation, uploadPage, deletePage, addMedicine, removeMedicine,
  restoreMedicine, finaliseConsultation, submitCorrection, imageUrl, apiError,
} from '../services/api'

/**
 * Doctor review and finalisation (Workflow stages 2–3).
 *
 * Every page of one prescription on one screen, with ONE interaction check
 * across all of them. The page image sits beside the extraction: reviewing
 * against the pixels is a glance, reviewing against memory is a rubber-stamp.
 *
 * Finalising writes an immutable version. The patient and any pharmacy only
 * ever see finalised versions, so editing afterwards is safe: nothing changes
 * for them until the doctor finalises again, as a labelled revision.
 */

const CORRECTION_TYPES = [
  { value: 'misread',      label: 'Misread — wrong characters' },
  { value: 'hallucinated', label: 'Hallucinated — not on the page' },
  { value: 'missed',       label: 'Missed — present but not reported' },
  { value: 'wrong_field',  label: 'Wrong field — right drug, wrong dose/frequency' },
  { value: 'other',        label: 'Other' },
]

const VISIBILITY = [
  { value: 'patient', label: 'Patient only' },
  { value: 'patient_and_pharmacy', label: 'Patient and pharmacy' },
  { value: 'hidden', label: 'Nobody (keep it on my record only)' },
]

const fmt = (iso) => new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })

export default function ConsultationPage() {
  const { id } = useParams()
  const [c, setC] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)        // which action is running
  const [editing, setEditing] = useState(null)
  const [activePage, setActivePage] = useState(1)
  const [notice, setNotice] = useState(null)

  const run = async (label, fn) => {
    setBusy(label)
    setError(null)
    try {
      const next = await fn()
      if (next?.id && next?.pages) setC(next)
      return next
    } catch (e) {
      setError(apiError(e))
      return null
    } finally { setBusy(null) }
  }

  useEffect(() => { getConsultation(id).then(setC).catch((e) => setError(apiError(e))) }, [id])

  const live = useMemo(() => (c?.medicines ?? []).filter((m) => !m.removed), [c])
  const needsAttention = live.filter((m) => m.needs_hard_confirmation).length

  if (!c && error) return <div className="mx-auto max-w-6xl p-4 sm:p-6"><Banner tone="error">{error}</Banner></div>
  if (!c) return <div className="flex items-center gap-2 p-8 text-ink-600"><Loader2 className="h-5 w-5 animate-spin" />Loading…</div>

  const page = c.pages.find((p) => p.page_no === activePage) ?? c.pages[0]
  const reload = () => getConsultation(id).then(setC)

  const correct = (m, field, value, type, note) => run('correct', async () => {
    await submitCorrection(m.prescription_id, { medicine_index: m.index, field, corrected_value: value, correction_type: type, note })
    setEditing(null)
    return reload()
  })
  const pick = (m) => (_, value) => correct(m, 'brand', value, 'misread', 'picked from candidates')

  return (
    <div className="mx-auto max-w-6xl px-4 sm:px-6">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <span className="rule-label text-care-700">Consultation</span>
          <h1 className="mt-1 font-display text-3xl font-normal text-ink-900">{c.patient_name || 'Patient not named yet'}</h1>
          <p className="mt-1 text-sm text-ink-600">
            <span className="data">{c.patient_phone || 'no phone'}</span>
            {c.clinic_address && <> · {c.clinic_address}</>} · {c.pages.length} page{c.pages.length === 1 ? '' : 's'}
          </p>
        </div>
        <StatusBadge c={c} />
      </header>

      <DemoReaderBanner />
      <Banner tone="warn">AI-extracted. Check every line against the original before you finalise. Only NLEM-verified ingredients are checked for interactions; anything else is listed under “Not checked”.</Banner>

      {c.status === 'revising' && (
        <Banner tone="info">
          You have edited this after finalising version {c.current_version}. The patient and any pharmacy still see version {c.current_version} until you finalise again.
        </Banner>
      )}
      {needsAttention > 0 && (
        <Banner tone="error">
          <ShieldAlert className="mr-1 inline h-4 w-4" />
          {needsAttention} line{needsAttention > 1 ? 's need' : ' needs'} your confirmation.
        </Banner>
      )}
      {c.duplicates.filter((d) => d.kind === 'same_reading').map((d) => (
        <Banner key={`dup-${d.page_no}`} tone="error">
          <Copy className="mr-1 inline h-4 w-4" />{d.message}
          {c.current_version === 0 && (
            <button type="button" disabled={!!busy} className="ml-2 font-semibold underline"
                    onClick={() => { const p = c.pages.find((x) => x.page_no === d.page_no); if (p) run('page', () => deletePage(c.id, p.id)) }}>
              Delete page {d.page_no}
            </button>
          )}
        </Banner>
      ))}
      {c.duplicates.filter((d) => d.kind === 'same_image_elsewhere').map((d) => (
        <Banner key={`same-${d.page_no}-${d.other_consultation_id}`} tone="info">
          {d.message}{' '}
          <Link to={`/consultations/${d.other_consultation_id}`} className="font-semibold underline">Open it</Link>
        </Banner>
      ))}
      {error && <Banner tone="error">{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}

      <div className="mt-4 grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <section className="min-w-0 space-y-6">
          {c.pages.length === 0 && (
            <p className="card text-ink-600">No pages yet. Add the first page of the prescription.</p>
          )}
          {c.pages.map((p) => (
            <div key={p.id} className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" onClick={() => setActivePage(p.page_no)}
                        className={`font-display text-xl font-semibold ${activePage === p.page_no ? 'text-care-800' : 'text-ink-900'} hover:underline`}>
                  Page {p.page_no}
                </button>
                <span className="text-xs text-ink-500">
                  legibility {Math.round((p.overall_legibility ?? 0) * 100)}%
                  {p.date && <> · dated {p.date}</>}
                  {p.prescriber_name && <> · written by “{p.prescriber_name}”</>}
                </span>
                {c.current_version === 0 && (
                  <button type="button" disabled={!!busy}
                          onClick={() => window.confirm(`Delete page ${p.page_no} and everything read from it?`) && run('page', () => deletePage(c.id, p.id))}
                          className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-ink-500 hover:text-vital-700">
                    <Trash2 size={13} /> Delete page
                  </button>
                )}
              </div>
              {p.document_type === 'not_a_prescription' && (
                <Banner tone="info">This page does not look like a prescription. No medicines were reported, which is a valid result.</Banner>
              )}
              {c.medicines.filter((m) => m.prescription_id === p.id).map((m) => (
                <MedicineCard
                  key={`${p.id}-${m.index}`} medicine={m} imageSrc={imageUrl(p.image_url)}
                  onPickCandidate={pick(m)} onCorrect={setEditing}
                  onRemove={(x) => run('remove', () => removeMedicine(x.prescription_id, x.index))}
                  onRestore={(x) => run('restore', () => restoreMedicine(x.prescription_id, x.index))}
                />
              ))}
              {p.unreadable_regions?.length > 0 && (
                <div className="rounded-xl border border-ink-200 bg-white p-4 shadow-clinical">
                  <h3 className="text-sm font-semibold text-ink-800">Reader notes, page {p.page_no}</h3>
                  <ul className="mt-1 list-disc pl-5 text-sm text-ink-600">
                    {p.unreadable_regions.map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                </div>
              )}
            </div>
          ))}

          {c.pages.length > 0 && <AddMedicine pages={c.pages} busy={!!busy}
            onAdd={(body) => run('add', () => addMedicine(c.id, body))} />}

          <SafetySection interactions={c.interactions} pages={c.pages.length} />
        </section>

        <aside className="space-y-5 lg:sticky lg:top-20 lg:max-h-[calc(100vh-6rem)] lg:self-start lg:overflow-y-auto lg:pr-1">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <h2 className="mr-auto text-sm font-semibold text-ink-800">Original</h2>
              {c.pages.length > 1 && c.pages.map((p) => (
                <button key={p.id} type="button" onClick={() => setActivePage(p.page_no)}
                        className={`rounded-md px-2 py-0.5 text-xs font-semibold ${activePage === p.page_no ? 'bg-care-700 text-white' : 'bg-ink-100 text-ink-700'}`}>
                  {p.page_no}
                </button>
              ))}
            </div>
            {page && <img src={imageUrl(page.image_url)} alt={`Page ${page.page_no} of the prescription`}
                          className="w-full rounded-xl border border-ink-200 bg-white p-1 shadow-clinical" />}
            <AddPage busy={busy === 'upload'} onFile={(f) => run('upload', async () => {
              const next = await uploadPage(c.id, f)
              setActivePage(next.pages.length)
              return next
            })} />
          </div>

          <PatientPanel key={`${c.patient_name}|${c.patient_phone}|${c.clinic_address}`} c={c} busy={!!busy} onSave={(body) => run('patient', () => updateConsultation(c.id, body))} />

          <DiagnosisPanel key={c.diagnosis ?? ''} c={c} busy={!!busy} onSave={(body) => run('diagnosis', () => updateConsultation(c.id, body))} />

          <FinalisePanel c={c} busy={busy === 'finalise'} onFinalise={async (opts) => {
            const next = await run('finalise', () => finaliseConsultation(c.id, opts))
            if (next) setNotice(`Version ${next.current_version} finalised. ${next.current_version > 1 ? 'The patient now sees it, labelled as revised.' : 'It is now in the patient’s app for their verified phone.'}`)
          }} />

          {c.versions.length > 0 && (
            <section className="card">
              <h2 className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
                <History size={17} className="text-care-700" /> Versions
              </h2>
              <ol className="mt-2 space-y-2 text-sm">
                {[...c.versions].reverse().map((v) => (
                  <li key={v.version} className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-semibold text-ink-900">v{v.version}</span>
                    <span className="text-ink-600">{fmt(v.finalised_at)}</span>
                    <Link to={`/check/${v.verification_id}`} className="data text-xs text-care-800 hover:underline">{v.verification_id}</Link>
                    {v.change_note && <span className="w-full text-xs text-ink-500">{v.change_note}</span>}
                  </li>
                ))}
              </ol>
            </section>
          )}
        </aside>
      </div>

      {editing && (
        <CorrectionDialog medicine={editing} busy={busy === 'correct'} onCancel={() => setEditing(null)}
          onSave={(field, value, type, note) => correct(editing, field, value, type, note)} />
      )}
    </div>
  )
}

function StatusBadge({ c }) {
  if (c.status === 'draft') return <span className="chip border-amber-300 bg-amber-50 text-amber-900">Draft · not sent</span>
  if (c.status === 'revising') return <span className="chip border-amber-300 bg-amber-50 text-amber-900">Editing · patient sees v{c.current_version}</span>
  return <span className="chip border-care-300 bg-care-50 text-care-800"><Lock size={12} /> Finalised · v{c.current_version}</span>
}

function AddPage({ busy, onFile }) {
  return (
    <label className={`btn-secondary mt-3 w-full cursor-pointer py-2.5 text-sm ${busy ? 'pointer-events-none opacity-60' : ''}`}>
      {busy ? <Loader2 size={16} className="animate-spin" /> : <FilePlus2 size={16} />}
      {busy ? 'Reading the new page…' : 'Add a page'}
      <input type="file" accept="image/*" className="hidden" disabled={busy}
             onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) onFile(f) }} />
    </label>
  )
}

function PatientPanel({ c, busy, onSave }) {
  const [name, setName] = useState(c.patient_name ?? '')
  const [phone, setPhone] = useState(c.patient_phone ?? '')
  const [clinic, setClinic] = useState(c.clinic_address ?? '')
  const dirty = name !== (c.patient_name ?? '') || phone !== (c.patient_phone ?? '') || clinic !== (c.clinic_address ?? '')
  return (
    <section className="card">
      <h2 className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
        <UserRound size={17} className="text-care-700" /> Patient
      </h2>
      <label className="mt-3 block text-xs font-medium text-ink-600">Name
        <input value={name} onChange={(e) => setName(e.target.value)} className="input-field mt-1 py-2" />
      </label>
      <label className="mt-2 block text-xs font-medium text-ink-600">Phone
        <input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} className="input-field mt-1 py-2" />
      </label>
      <label className="mt-2 block text-xs font-medium text-ink-600">Clinic
        <input value={clinic} onChange={(e) => setClinic(e.target.value)} className="input-field mt-1 py-2" />
      </label>
      {c.current_version > 0 && <p className="mt-2 text-xs text-ink-500">A new phone number takes effect when you finalise the next version.</p>}
      {dirty && (
        <button type="button" disabled={busy} onClick={() => onSave({ patient_name: name, patient_phone: phone, clinic_address: clinic })}
                className="btn-secondary mt-3 w-full py-2 text-sm"><Save size={15} /> Save patient details</button>
      )}
    </section>
  )
}

function DiagnosisPanel({ c, busy, onSave }) {
  const [diagnosis, setDiagnosis] = useState(c.diagnosis ?? '')
  const asRead = c.diagnosis_as_read?.length ? c.diagnosis_as_read.join('; ') : null
  return (
    <section className="card">
      <h2 className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
        <Stethoscope size={17} className="text-care-700" /> Diagnosis
      </h2>
      <textarea rows={2} value={diagnosis} onChange={(e) => setDiagnosis(e.target.value)}
                placeholder="Optional" className="input-field mt-3 py-2" />
      <p className="mt-1 text-xs text-ink-500">{asRead ? <>Read from the page as “{asRead}”.</> : 'Nothing was written on the page.'}</p>
      {diagnosis !== (c.diagnosis ?? '') && (
        <button type="button" disabled={busy} onClick={() => onSave({ diagnosis })} className="btn-secondary mt-2 w-full py-2 text-sm">
          <Save size={15} /> Save diagnosis
        </button>
      )}
      <label className="mt-3 block text-xs font-medium text-ink-600">Who can see it
        <select value={c.diagnosis_visibility} disabled={busy}
                onChange={(e) => onSave({ diagnosis_visibility: e.target.value })} className="input-field mt-1 py-2">
          {VISIBILITY.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
        </select>
      </label>
      <label className="mt-3 flex items-start gap-2 text-sm text-ink-700">
        <input type="checkbox" className="mt-1 rounded border-ink-300 text-care-700" checked={!c.allow_substitution} disabled={busy}
               onChange={(e) => onSave({ allow_substitution: !e.target.checked })} />
        <span><strong>Do not substitute.</strong> The pharmacy may not give a different brand, even with the same composition.</span>
      </label>
    </section>
  )
}

function FinalisePanel({ c, busy, onFinalise }) {
  const [ack, setAck] = useState(false)
  const [ackDup, setAckDup] = useState(false)
  const [note, setNote] = useState('')
  const twice = c.duplicates.filter((d) => d.kind === 'same_reading')
  const unchanged = c.status === 'finalised'
  const ambiguous = c.medicines.filter((m) => !m.removed && m.outcome === 'ambiguous').length
  const missing = !c.patient_name || !c.patient_phone
  const others = c.unresolved.length - ambiguous
  const blocked = unchanged || !c.pages.length || ambiguous > 0 || missing || (others > 0 && !ack)
    || (twice.length > 0 && !ackDup)
  const revision = c.current_version > 0
  return (
    <section className="card border-care-200">
      <h2 className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900">
        <BadgeCheck size={17} className="text-care-700" /> {revision ? `Finalise version ${c.current_version + 1}` : 'Finalise'}
      </h2>
      <p className="mt-1 text-sm text-ink-600">
        {revision
          ? 'Replaces the version the patient sees, labelled as revised. Earlier versions stay on record.'
          : 'Signs the prescription off. It then appears in the patient’s app and can be shared with a pharmacy.'}
      </p>
      <ul className="mt-3 space-y-1 text-sm">
        {missing && <Req>Add the patient’s name and phone.</Req>}
        {ambiguous > 0 && <Req>Choose the medicine for {ambiguous} ambiguous line{ambiguous > 1 ? 's' : ''}.</Req>}
        {others > 0 && (
          <li>
            <p className="font-medium text-vital-800">{others} line{others > 1 ? 's are' : ' is'} still unresolved:</p>
            <ul className="ml-4 list-disc text-xs text-ink-600">{c.unresolved.filter((u) => !u.endsWith('ambiguous')).map((u) => <li key={u}>{u}</li>)}</ul>
            <label className="mt-2 flex items-start gap-2 text-xs text-ink-700">
              <input type="checkbox" className="mt-0.5 rounded border-ink-300 text-care-700" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              The patient will see these as “could not be read reliably”. Finalise anyway.
            </label>
          </li>
        )}
        {twice.length > 0 && (
          <li>
            <p className="font-medium text-vital-800">
              {twice.map((d) => `Page ${d.page_no} reads the same as page ${d.other_page_no}.`).join(' ')}
            </p>
            <label className="mt-1 flex items-start gap-2 text-xs text-ink-700">
              <input type="checkbox" className="mt-0.5 rounded border-ink-300 text-care-700" checked={ackDup} onChange={(e) => setAckDup(e.target.checked)} />
              These are different pages and every medicine on both is meant. Finalise with both.
            </label>
          </li>
        )}
      </ul>
      {revision && !unchanged && (
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed? (shown to the patient)"
               className="input-field mt-3 py-2 text-sm" maxLength={300} />
      )}
      <button type="button" disabled={blocked || busy} onClick={() => onFinalise({ acknowledgeUnresolved: ack, acknowledgeDuplicates: ackDup, changeNote: note })}
              className="btn-confirm mt-3">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
        {unchanged ? `Version ${c.current_version} is current` : revision ? 'Finalise revision' : 'Finalise and send to patient'}
      </button>
    </section>
  )
}

const Req = ({ children }) => <li className="flex items-start gap-1.5 text-vital-800"><AlertTriangle size={14} className="mt-0.5 shrink-0" />{children}</li>

function AddMedicine({ pages, busy, onAdd }) {
  const [open, setOpen] = useState(false)
  const [f, setF] = useState({ name: '', dosage: '', frequency: '', duration: '', instructions: '', prescription_id: '' })
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="btn-secondary w-full py-2.5 text-sm">
        <Plus size={16} /> Add a medicine the reader missed
      </button>
    )
  }
  return (
    <form className="card space-y-3" onSubmit={async (e) => {
      e.preventDefault()
      await onAdd({ ...f, prescription_id: f.prescription_id || null })
      setF({ name: '', dosage: '', frequency: '', duration: '', instructions: '', prescription_id: '' })
      setOpen(false)
    }}>
      <h3 className="font-display text-lg font-semibold text-ink-900">Add a medicine</h3>
      <label className="block text-xs font-medium text-ink-600">Brand or generic name
        <input required autoFocus value={f.name} onChange={set('name')} className="input-field mt-1 py-2" placeholder="e.g. Dolo 650, or Paracetamol" />
      </label>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-xs font-medium text-ink-600">Dose<input value={f.dosage} onChange={set('dosage')} className="input-field mt-1 py-2" /></label>
        <label className="block text-xs font-medium text-ink-600">How often<input value={f.frequency} onChange={set('frequency')} className="input-field mt-1 py-2" placeholder="1-0-1" /></label>
        <label className="block text-xs font-medium text-ink-600">For how long<input value={f.duration} onChange={set('duration')} className="input-field mt-1 py-2" placeholder="5 days" /></label>
      </div>
      <label className="block text-xs font-medium text-ink-600">Instructions<input value={f.instructions} onChange={set('instructions')} className="input-field mt-1 py-2" placeholder="after food" /></label>
      {pages.length > 1 && (
        <label className="block text-xs font-medium text-ink-600">On page
          <select value={f.prescription_id} onChange={set('prescription_id')} className="input-field mt-1 py-2">
            <option value="">Last page</option>
            {pages.map((p) => <option key={p.id} value={p.id}>Page {p.page_no}</option>)}
          </select>
        </label>
      )}
      <p className="text-xs text-ink-500">The name is looked up in the medicine register, so its ingredients enter the interaction check.</p>
      <div className="flex justify-end gap-2">
        <button type="button" onClick={() => setOpen(false)} className="btn-secondary px-4 py-2 text-sm">Cancel</button>
        <button type="submit" disabled={busy} className="btn-primary px-4 py-2 text-sm">{busy ? 'Adding…' : 'Add medicine'}</button>
      </div>
    </form>
  )
}

function SafetySection({ interactions, pages }) {
  if (!interactions) return null
  const { overall_risk: risk, interactions: list = [], skipped = [], medicines_checked = [] } = interactions
  const tone = risk === 'severe' ? 'error' : risk === 'moderate' ? 'warn' : 'info'
  return (
    <div className="rounded-xl border border-ink-200 bg-white p-4 shadow-clinical">
      <h2 className="text-sm font-semibold text-ink-800">Interaction check{pages > 1 ? `, across all ${pages} pages` : ''}</h2>
      <Banner tone={tone}>
        Overall risk: <strong>{risk}</strong> · {list.length} interaction{list.length === 1 ? '' : 's'} found
      </Banner>
      {list.map((i, k) => (
        <div key={k} className="mt-2 rounded-lg border p-3" style={{ borderColor: i.severity_color }}>
          <p className="font-semibold" style={{ color: i.severity_color }}>
            {i.severity.toUpperCase()}: {i.drug_a} + {i.drug_b}
          </p>
          {i.effect && <p className="mt-1 text-sm text-ink-700">{i.effect}</p>}
          {i.mechanism && <p className="mt-1 text-xs text-ink-500">Mechanism: {i.mechanism}</p>}
          {i.safer_alternative && <p className="mt-1 text-sm text-care-800">Safer: {i.safer_alternative}</p>}
          {i.reference && <p className="mt-1 text-xs italic text-ink-500">{i.reference}</p>}
        </div>
      ))}
      <p className="mt-3 text-xs text-ink-600">Checked: {medicines_checked.length ? medicines_checked.join(', ') : 'nothing'}</p>
      {skipped.length > 0 && (
        <div className="mt-1 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <strong>Not checked ({skipped.length}):</strong>
          <ul className="mt-1 list-disc pl-4">{skipped.map((s, i) => <li key={i}>{s}</li>)}</ul>
        </div>
      )}
    </div>
  )
}

function CorrectionDialog({ medicine, busy, onCancel, onSave }) {
  const [field, setField] = useState('brand')
  const [value, setValue] = useState(medicine.brand ?? medicine.generic ?? '')
  const [type, setType] = useState('misread')
  const [note, setNote] = useState('')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/40 p-4 backdrop-blur-sm" role="dialog" aria-modal="true">
      <div className="w-full max-w-md rounded-xl border border-ink-200 bg-white p-5 shadow-clinical-lg">
        <h3 className="font-display text-xl font-semibold text-ink-900">Correct entry</h3>
        {medicine.raw_reading && <p className="mt-1 font-mono text-xs text-ink-500">read as “{medicine.raw_reading}”</p>}

        <label className="mt-4 block text-sm font-medium text-ink-700">Field
          <select value={field} onChange={(e) => { setField(e.target.value); setValue((e.target.value === 'brand' ? (medicine.brand ?? medicine.generic) : medicine[e.target.value]) ?? '') }}
                  className="input-field mt-1 py-2">
            {[['brand', 'medicine name'], ['dosage', 'dose'], ['frequency', 'how often'], ['duration', 'for how long'], ['instructions', 'instructions']].map(([f, l]) => (
              <option key={f} value={f}>{l}</option>
            ))}
          </select>
        </label>

        <label className="mt-3 block text-sm font-medium text-ink-700">Correct value
          <input value={value} onChange={(e) => setValue(e.target.value)} autoFocus className="input-field mt-1 py-2" />
        </label>
        {field === 'brand' && <p className="mt-1 text-xs text-ink-500">Looked up again in the medicine register, so the ingredients and the interaction check follow the new name.</p>}

        {/* The reward signal for later calibration — one dropdown (§16.3). */}
        <label className="mt-3 block text-sm font-medium text-ink-700">What went wrong?
          <select value={type} onChange={(e) => setType(e.target.value)} className="input-field mt-1 py-2">
            {CORRECTION_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </label>

        <label className="mt-3 block text-sm font-medium text-ink-700">Note (optional)
          <input value={note} onChange={(e) => setNote(e.target.value)} className="input-field mt-1 py-2" />
        </label>

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="btn-secondary px-4 py-2 text-sm">Cancel</button>
          <button type="button" disabled={busy} onClick={() => onSave(field, value, type, note)} className="btn-primary px-4 py-2 text-sm">
            {busy ? 'Saving…' : 'Save correction'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function Banner({ tone = 'info', children }) {
  const cls = {
    info:  'border-care-200 bg-care-50 text-care-900',
    warn:  'border-amber-200 bg-amber-50 text-amber-900',
    error: 'border-vital-200 bg-vital-50 text-vital-900',
  }[tone]
  return (
    <div className={`mt-3 flex items-start gap-2 rounded-xl border px-4 py-3 text-sm ${cls}`}>
      {tone !== 'info' && <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />}
      <div>{children}</div>
    </div>
  )
}
