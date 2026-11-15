import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft, Loader2, Timer, QrCode, Camera, Keyboard, CheckCircle2, Circle, AlertTriangle,
  Ban, Stethoscope, ShieldCheck, FileText, Lock,
} from 'lucide-react'
import {
  getGrant, checkPack, confirmCheck, grantImageUrl, apiError,
} from '../../services/api'
import DemoReaderBanner from '../../components/DemoReaderBanner'
import { DISPENSE_RESULT } from './dispenseResults'

/**
 * One prescription opened by a patient's code (Workflow stages 5–7).
 *
 * Read-only, for a fixed window. The countdown is a courtesy; the SERVER refuses
 * every request once access ends (410), and when it does this screen drops
 * everything it was holding — the prescription is not kept in the browser.
 *
 * Each pack is checked by QR, photo or typed details, and compared by
 * composition and strength. Grey "could not verify" is never drawn as a match;
 * orange needs the chemist to confirm; red cannot be dispensed.
 */
const mmss = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.max(0, s) % 60).padStart(2, '0')}`

export default function PharmacyGrantPage() {
  const { id } = useParams()
  const [g, setG] = useState(null)
  const [ended, setEnded] = useState(null)   // message once access has ended
  const [error, setError] = useState(null)
  const [left, setLeft] = useState(null)

  const handle = useCallback((e) => {
    if (e?.response?.status === 410 || e?.response?.status === 404) {
      setG(null)   // drop the prescription from memory, not just from view
      setEnded(apiError(e))
    } else setError(apiError(e))
  }, [])

  const load = useCallback(() => getGrant(id).then((x) => { setG(x); setError(null) }).catch(handle), [id, handle])
  useEffect(() => { load() }, [load])

  // Countdown, plus a periodic re-check so a revoke by the patient shows promptly.
  useEffect(() => {
    if (!g) return undefined
    const end = Date.now() + g.seconds_left * 1000
    const tick = setInterval(() => {
      const s = Math.round((end - Date.now()) / 1000)
      setLeft(s)
      if (s <= 0) load()
    }, 1000)
    const poll = setInterval(load, 20000)
    return () => { clearInterval(tick); clearInterval(poll) }
  }, [g?.grant_id, g?.seconds_left, load]) // eslint-disable-line react-hooks/exhaustive-deps

  const back = <Link to="/pharmacy" className="btn-ghost -ml-2 mb-4 inline-flex"><ArrowLeft size={16} /> Dispensing desk</Link>

  if (ended) {
    return (
      <div className="mx-auto max-w-xl px-4 sm:px-6">
        {back}
        <section className="card text-center">
          <Lock className="mx-auto text-ink-400" size={28} />
          <h1 className="mt-2 font-display text-2xl text-ink-900">Access ended</h1>
          <p className="mt-2 text-sm text-ink-600">{ended}</p>
          <Link to="/pharmacy/records" className="btn-secondary mt-4">See what you dispensed</Link>
        </section>
      </div>
    )
  }
  if (!g) return <div className="mx-auto max-w-5xl px-4 sm:px-6">{back}{error ? <p role="alert" className="card text-sm text-vital-800">{error}</p> : <p className="flex items-center gap-2 text-ink-600"><Loader2 className="h-4 w-4 animate-spin" /> Opening…</p>}</div>

  const secs = left ?? g.seconds_left
  const outstanding = g.medicines.filter((m) => !m.dispensed).length

  return (
    <div className="mx-auto max-w-5xl px-4 sm:px-6">
      {back}
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <span className="rule-label text-care-700">Prescription · read-only</span>
          <h1 className="mt-1 font-display text-3xl font-normal text-ink-900">{g.patient_name ?? 'Patient'}</h1>
          <p className="mt-1 text-sm text-ink-600">
            <span className="data">{g.verification_id}</span> · version {g.version}{g.revised && ' (revised)'} · finalised {new Date(g.finalised_at).toLocaleDateString()}
          </p>
        </div>
        <div className={`chip px-3 py-1.5 text-sm ${secs < 120 ? 'border-vital-300 bg-vital-50 text-vital-800' : 'border-care-300 bg-care-50 text-care-800'}`} role="timer" aria-live="off">
          <Timer size={15} /> Access ends in <span className="data font-semibold">{mmss(secs)}</span>
        </div>
      </header>

      {g.revised && g.change_note && <p className="card mb-4 border-amber-300 bg-amber-50 text-sm text-amber-900"><strong>Revised by the doctor:</strong> {g.change_note}</p>}
      {!g.allow_substitution && (
        <p className="card mb-4 flex items-center gap-2 border-vital-300 bg-vital-50 text-sm font-semibold text-vital-900">
          <Ban size={16} /> Do not substitute: dispense the prescribed brands only.
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
        <section className="min-w-0 space-y-6">
          <div className="card">
            <h2 className="font-display text-xl font-semibold text-ink-900">
              Medicines <span className="text-sm font-normal text-ink-500">· {outstanding ? `${outstanding} still to dispense` : 'all dispensed'}</span>
            </h2>
            <ol className="mt-3 divide-y divide-ink-100">
              {g.medicines.map((m) => (
                <li key={m.index} className="flex gap-3 py-3">
                  {m.dispensed ? <CheckCircle2 className="mt-0.5 shrink-0 text-care-600" size={20} aria-label="Dispensed" />
                               : <Circle className="mt-0.5 shrink-0 text-ink-300" size={20} aria-label="Not yet dispensed" />}
                  <div className="min-w-0">
                    {m.name
                      ? <p className="font-semibold text-ink-900">{m.name}</p>
                      : <p className="flex items-center gap-1.5 font-semibold text-vital-800"><AlertTriangle size={15} /> Not identified: check the original</p>}
                    <p className="text-sm text-ink-600">{m.contains.join(' + ') || <>written as “{m.as_written}”</>}</p>
                    <p className="text-sm text-ink-700">{[m.dosage, m.frequency, m.duration, m.instructions].filter(Boolean).join(' · ')}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          <CheckPanel grantId={g.grant_id} onResult={setG} onError={handle} />

          {g.checks.length > 0 && (
            <div className="card">
              <h2 className="font-display text-xl font-semibold text-ink-900">Packs checked</h2>
              <ul className="mt-3 space-y-2">
                {[...g.checks].reverse().map((c) => (
                  <CheckRow key={c.id} c={c} onConfirm={async () => {
                    try { setG(await confirmCheck(g.grant_id, c.id)) } catch (e) { handle(e) }
                  }} />
                ))}
              </ul>
            </div>
          )}

          <div className="card">
            <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
              <ShieldCheck size={18} className="text-care-700" /> Interactions
            </h2>
            {g.interactions.length === 0
              ? <p className="mt-2 text-sm text-ink-700">No known interactions among the medicines that could be checked.</p>
              : g.interactions.map((i, k) => (
                  <div key={k} className="mt-2 rounded-lg border p-3" style={{ borderColor: i.severity_color }}>
                    <p className="font-semibold" style={{ color: i.severity_color }}>{i.severity.toUpperCase()}: {i.drug_a} + {i.drug_b}</p>
                    {i.effect && <p className="mt-1 text-sm text-ink-700">{i.effect}</p>}
                    {i.safer_alternative && <p className="mt-1 text-sm text-care-800">Safer: {i.safer_alternative}</p>}
                    {i.reference && <p className="mt-1 text-xs italic text-ink-500">{i.reference}</p>}
                  </div>
                ))}
            {g.not_checked.length > 0 && (
              <p className="mt-3 text-xs text-ink-600"><strong>Not checked:</strong> {g.not_checked.join('; ')}</p>
            )}
          </div>
        </section>

        <aside className="space-y-5">
          {g.doctor && (
            <section className="card">
              <h2 className="flex items-center gap-2 font-display text-lg font-semibold text-ink-900"><Stethoscope size={17} className="text-care-700" /> Prescriber</h2>
              <p className="mt-2 font-semibold text-ink-900">{g.doctor.name}</p>
              <p className="text-sm text-ink-600">{g.doctor.speciality}</p>
              <p className="text-sm text-ink-600">{g.doctor.clinic_address}</p>
              <p className="mt-1 text-sm">Reg. no. <span className="data">{g.doctor.registration_no || <em className="text-amber-700">not given</em>}</span></p>
              <p className="mt-1 text-xs text-ink-500">
                {g.doctor.vetted ? 'Registration details reviewed by a PrescriptAI administrator.' : 'Registration number as declared by the doctor; not reviewed.'}
              </p>
              {g.diagnosis && <p className="mt-2 text-sm"><span className="text-ink-500">Diagnosis:</span> {g.diagnosis}</p>}
            </section>
          )}
          <section>
            <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-ink-800"><FileText size={15} /> Original</h2>
            {g.page_numbers.map((n) => (
              <img key={n} src={grantImageUrl(g.grant_id, n)} alt={`Original prescription page ${n}`}
                   className="mb-3 w-full rounded-xl border border-ink-200 bg-white p-1 shadow-clinical" />
            ))}
          </section>
        </aside>
      </div>
    </div>
  )
}

function CheckRow({ c, onConfirm }) {
  const r = DISPENSE_RESULT[c.result] ?? DISPENSE_RESULT.could_not_verify
  const canConfirm = c.needs_confirmation && !c.blocked && !c.chemist_confirmed
  return (
    <li className={`rounded-lg border p-3 text-sm ${r.cls}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{r.label}</span>
        <span>{c.pack_brand || c.pack_composition || 'unidentified pack'}{c.pack_strength && ` · ${c.pack_strength}`}</span>
        {c.medicine_label && <span className="opacity-80">for {c.medicine_label}</span>}
        <span className="ml-auto text-xs opacity-80">{c.method}{c.batch && ` · batch ${c.batch}`}{c.quantity && ` · qty ${c.quantity}`}</span>
      </div>
      {c.reasons.length > 0 && <ul className="mt-1 list-disc pl-5">{c.reasons.map((x, i) => <li key={i}>{x}</li>)}</ul>}
      {c.blocked && <p className="mt-1 flex items-center gap-1 font-semibold"><Ban size={14} /> Do not dispense this pack against this prescription.</p>}
      {c.dispensed && <p className="mt-1 flex items-center gap-1 font-semibold"><CheckCircle2 size={14} /> Recorded as dispensed.</p>}
      {canConfirm && (
        <button type="button" onClick={onConfirm} className="btn-secondary mt-2 py-1.5 text-sm">
          I have checked this. Dispense it.
        </button>
      )}
    </li>
  )
}

const TABS = [
  { key: 'qr', label: 'Scan QR', icon: QrCode },
  { key: 'photo', label: 'Photo', icon: Camera },
  { key: 'manual', label: 'Type it', icon: Keyboard },
]

function CheckPanel({ grantId, onResult, onError }) {
  const [tab, setTab] = useState('qr')
  const [f, setF] = useState({ qr_text: '', brand: '', generic_name: '', strength: '', form: '', batch: '', expiry: '', quantity: '' })
  const [photo, setPhoto] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setMsg(null)
    try {
      const fields = tab === 'qr' ? { qr_text: f.qr_text, batch: f.batch, expiry: f.expiry, quantity: f.quantity }
        : tab === 'photo' ? { batch: f.batch, expiry: f.expiry, quantity: f.quantity }
        : { brand: f.brand, generic_name: f.generic_name, strength: f.strength, form: f.form, batch: f.batch, expiry: f.expiry, quantity: f.quantity }
      onResult(await checkPack(grantId, tab, fields, tab === 'photo' ? photo : null))
      setF({ qr_text: '', brand: '', generic_name: '', strength: '', form: '', batch: '', expiry: '', quantity: '' })
      setPhoto(null)
    } catch (err) {
      if ([404, 410].includes(err?.response?.status)) onError(err)
      else setMsg(apiError(err))
    } finally { setBusy(false) }
  }

  return (
    <form onSubmit={submit} className="card border-care-200">
      <h2 className="font-display text-xl font-semibold text-ink-900">Check a pack</h2>
      <p className="mt-1 text-sm text-ink-600">Compared with the prescription by ingredients and strength, not by brand name.</p>
      {tab === 'photo' && <div className="mt-3"><DemoReaderBanner what="pack" /></div>}
      <div role="tablist" className="mt-3 flex gap-1 rounded-lg bg-ink-100 p-1">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} type="button" role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
                  className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-2 text-sm font-medium ${tab === key ? 'bg-white text-care-800 shadow-sm' : 'text-ink-600'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'qr' && (
        <div className="mt-3">
          <QrScanner onCode={(text) => setF((cur) => ({ ...cur, qr_text: text }))} />
          <label className="mt-3 block text-xs font-medium text-ink-600">QR contents
            <textarea rows={3} value={f.qr_text} onChange={set('qr_text')} required className="input-field data mt-1 py-2 text-xs"
                      placeholder="Scan with the camera or a handheld scanner, or paste the text" />
          </label>
        </div>
      )}
      {tab === 'photo' && (
        <label className="mt-3 block text-xs font-medium text-ink-600">Photo of the label (brand and strength visible)
          <input type="file" accept="image/*" capture="environment" required onChange={(e) => setPhoto(e.target.files?.[0] ?? null)}
                 className="mt-1 block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-care-50 file:px-3 file:py-2 file:text-care-800" />
          <span className="mt-1 block text-ink-500">The photo is read and then discarded; only the reading is kept.</span>
        </label>
      )}
      {tab === 'manual' && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <label className="block text-xs font-medium text-ink-600">Brand<input value={f.brand} onChange={set('brand')} className="input-field mt-1 py-2" placeholder="Dolo 650" /></label>
          <label className="block text-xs font-medium text-ink-600">Composition (if no brand)<input value={f.generic_name} onChange={set('generic_name')} className="input-field mt-1 py-2" placeholder="Paracetamol IP 650 mg" /></label>
          <label className="block text-xs font-medium text-ink-600">Strength<input value={f.strength} onChange={set('strength')} className="input-field mt-1 py-2" placeholder="650 mg" /></label>
          <label className="block text-xs font-medium text-ink-600">Form<input value={f.form} onChange={set('form')} className="input-field mt-1 py-2" placeholder="tablet" /></label>
        </div>
      )}
      <div className="mt-3 grid grid-cols-3 gap-3">
        <label className="block text-xs font-medium text-ink-600">Batch<input value={f.batch} onChange={set('batch')} className="input-field mt-1 py-2" /></label>
        <label className="block text-xs font-medium text-ink-600">Expiry<input value={f.expiry} onChange={set('expiry')} className="input-field mt-1 py-2" placeholder="08/2027" /></label>
        <label className="block text-xs font-medium text-ink-600">Quantity<input value={f.quantity} onChange={set('quantity')} className="input-field mt-1 py-2" placeholder="10 tabs" /></label>
      </div>
      {msg && <p role="alert" className="mt-2 text-sm text-vital-700">{msg}</p>}
      <button type="submit" disabled={busy} className="btn-primary mt-4 w-full">
        {busy ? <Loader2 size={18} className="animate-spin" /> : <CheckCircle2 size={18} />} Check this pack
      </button>
    </form>
  )
}

/**
 * Camera QR scanning with the browser's BarcodeDetector (Chrome, Edge, Android).
 * Elsewhere the text box takes a handheld scanner, which types the code like a
 * keyboard — the usual setup at a counter anyway.
 */
function QrScanner({ onCode }) {
  const supported = typeof window !== 'undefined' && 'BarcodeDetector' in window
  const video = useRef(null)
  const [on, setOn] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    if (!on) return undefined
    let stream, timer, stopped = false
    ;(async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
        if (stopped) return
        video.current.srcObject = stream
        await video.current.play()
        const detector = new window.BarcodeDetector({ formats: ['qr_code', 'data_matrix'] })
        timer = setInterval(async () => {
          try {
            const [hit] = await detector.detect(video.current)
            if (hit?.rawValue) { onCode(hit.rawValue); setOn(false) }
          } catch { /* frame not ready */ }
        }, 300)
      } catch (e) {
        setErr(e?.name === 'NotAllowedError' ? 'Camera permission was refused.' : 'The camera could not be opened.')
        setOn(false)
      }
    })()
    return () => { stopped = true; clearInterval(timer); stream?.getTracks().forEach((t) => t.stop()) }
  }, [on, onCode])

  if (!supported) return <p className="text-xs text-ink-500">This browser cannot read QR codes from the camera. Use a handheld scanner into the box below, or paste the text.</p>
  return (
    <div>
      {on
        ? <video ref={video} muted playsInline className="aspect-video w-full rounded-lg bg-ink-900 object-cover" />
        : <button type="button" onClick={() => { setErr(null); setOn(true) }} className="btn-secondary w-full py-2.5 text-sm"><Camera size={16} /> Scan with the camera</button>}
      {on && <button type="button" onClick={() => setOn(false)} className="btn-ghost mt-1">Stop camera</button>}
      {err && <p className="mt-1 text-xs text-vital-700">{err}</p>}
    </div>
  )
}
