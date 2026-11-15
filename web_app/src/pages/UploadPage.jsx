import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  UploadCloud, X, Loader2, AlertCircle, ScanLine, Sun, Crop, Focus, UserRound, Plus,
} from 'lucide-react'
import { useAuthStore } from '../store/useAuthStore'
import { createConsultation, uploadPages, apiError } from '../services/api'
import DemoReaderBanner from '../components/DemoReaderBanner'

/**
 * New consultation (Workflow stage 1 + 2): who the prescription is for, then
 * its page(s). One consultation is one prescription; a second page attaches to
 * the same one, and the interaction check runs across every page together.
 *
 * Doctor details are not asked for: they come from the doctor's profile.
 */
const STAGES = [
  'Normalising the page…',
  'Reading the handwriting…',
  'Matching against the Indian brand register…',
  'Verifying ingredients and screening interactions…',
]

const TIPS = [
  { icon: Sun,   title: 'Even light',   desc: 'No hard shadow across the page and no flash glare on the ink.' },
  { icon: Crop,  title: 'Whole page',   desc: 'Include the header and every line — a cropped edge is a lost medicine.' },
  { icon: Focus, title: 'Flat and sharp', desc: 'Lay it flat on a contrasting surface and let the camera focus before shooting.' },
]

const MAX_PAGES = 10

export default function UploadPage() {
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const [patientName, setPatientName] = useState('')
  const [phone, setPhone] = useState('')
  const [clinic, setClinic] = useState(user?.profile?.clinic_address ?? '')
  const [files, setFiles] = useState([])        // [{ file, preview }]
  const [dragActive, setDragActive] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(null) // { of, stage }
  const [results, setResults] = useState(null)   // per-file outcome when something did not go in
  const [created, setCreated] = useState(null)   // consultation id once it exists
  const timer = useRef(null)

  useEffect(() => () => clearInterval(timer.current), [])

  const addFiles = useCallback((list) => {
    const next = []
    for (const f of Array.from(list ?? [])) {
      if (!f.type.startsWith('image/')) { setError(`${f.name} is not an image. Upload JPG or PNG photos of the pages.`); continue }
      if (f.size > 10 * 1024 * 1024) { setError(`${f.name} is over 10 MB. Please use a smaller image.`); continue }
      next.push({ file: f, preview: URL.createObjectURL(f) })
    }
    if (next.length) setError(null)
    setFiles((cur) => [...cur, ...next].slice(0, MAX_PAGES))
  }, [])

  const removeFile = (i) => setFiles((cur) => cur.filter((_, k) => k !== i))

  const submit = async (e) => {
    e.preventDefault()
    if (!files.length) return setError('Add at least one page.')
    setBusy(true)
    setError(null)
    setResults(null)
    let id = created
    try {
      if (!id) {
        const c = await createConsultation({ patient_name: patientName, patient_phone: phone, clinic_address: clinic })
        id = c.id
        setCreated(id)
      }
      // One request for every page: the server skips exact duplicates before
      // any reading, reads the rest in parallel, and reports per file.
      setProgress({ of: files.length, stage: 0 })
      timer.current = setInterval(() => setProgress((p) => p && { ...p, stage: Math.min(p.stage + 1, STAGES.length - 1) }), 4000)
      const out = await uploadPages(id, files.map((f) => f.file))
      const failed = out.results.filter((r) => r.status === 'failed')
      if (failed.length === 0) return navigate(`/consultations/${id}`)
      // Some pages are in, some are not. Say exactly which, and keep only the
      // ones still to retry in the list.
      setResults(out.results)
      setFiles((cur) => cur.filter((f) => failed.some((r) => r.filename === f.file.name)))
      setError(`${failed.length} of ${out.results.length} page${out.results.length === 1 ? '' : 's'} could not be read.`)
    } catch (err) {
      // No fabricated result: a failure is reported as a failure. Pages read
      // before it are kept in the consultation and the doctor can open it.
      setError(err?.code === 'ECONNABORTED'
        ? 'The read timed out. Dense pages can take a while. Try again, or use a smaller image.'
        : apiError(err))
    } finally {
      clearInterval(timer.current)
      setProgress(null)
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6 lg:px-8">
      <header className="mb-8 animate-fade-in">
        <span className="rule-label text-care-700">New consultation</span>
        <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Scan a prescription</h1>
        <p className="mt-2 max-w-xl text-ink-600">
          Who it is for, then every page of it. You review each line against the original before
          anything reaches the patient.
        </p>
      </header>

      <DemoReaderBanner />

      <form onSubmit={submit} className="space-y-6">
        <section className="card animate-slide-up">
          <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
            <UserRound size={18} className="text-care-700" /> Patient
          </h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="block">
              <span className="rule-label">Patient name</span>
              <input required value={patientName} onChange={(e) => setPatientName(e.target.value)}
                     disabled={!!created} className="input-field mt-1.5" autoComplete="off" />
            </label>
            <label className="block">
              <span className="rule-label">Patient phone</span>
              <input required type="tel" inputMode="tel" value={phone} placeholder="+91 98765 43210"
                     onChange={(e) => setPhone(e.target.value)} disabled={!!created}
                     className="input-field mt-1.5" autoComplete="off" />
              <span className="mt-1 block text-xs text-ink-500">The prescription reaches the patient's app on this number once you finalise it.</span>
            </label>
            <label className="block sm:col-span-2">
              <span className="rule-label">Clinic</span>
              <input value={clinic} onChange={(e) => setClinic(e.target.value)} disabled={!!created}
                     className="input-field mt-1.5" placeholder="Clinic address" />
              <span className="mt-1 block text-xs text-ink-500">From your profile. Change it if you are seeing this patient elsewhere.</span>
            </label>
          </div>
        </section>

        <section className="card animate-slide-up" style={{ animationDelay: '.05s' }}>
          <h2 className="font-display text-xl font-semibold text-ink-900">Pages</h2>
          <p className="mt-1 text-sm text-ink-600">One or more photos of the same prescription, in order. You can add more later.</p>

          {files.length > 0 && (
            <ol className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              {files.map(({ file, preview }, i) => (
                <li key={preview} className="relative overflow-hidden rounded-lg border border-ink-200 bg-ink-50">
                  <img src={preview} alt={`Page ${i + 1}`} className="h-36 w-full object-cover" />
                  <span className="absolute left-2 top-2 rounded bg-white/90 px-1.5 text-xs font-semibold text-ink-800">Page {i + 1}</span>
                  {!busy && (
                    <button type="button" onClick={() => removeFile(i)} aria-label={`Remove page ${i + 1}`}
                            className="absolute right-1.5 top-1.5 rounded-md bg-white/90 p-1 text-ink-600 hover:text-vital-700">
                      <X size={14} />
                    </button>
                  )}
                  <p className="truncate px-2 py-1 text-[11px] text-ink-500">{file.name}</p>
                </li>
              ))}
            </ol>
          )}

          {files.length < MAX_PAGES && !busy && (
            <div
              onDragOver={(e) => { e.preventDefault(); setDragActive(true) }}
              onDragLeave={() => setDragActive(false)}
              onDrop={(e) => { e.preventDefault(); setDragActive(false); addFiles(e.dataTransfer.files) }}
              onClick={() => document.getElementById('page-input').click()}
              className={`mt-4 cursor-pointer rounded-xl border-2 border-dashed p-8 text-center transition-all ${
                dragActive ? 'border-care-500 bg-care-50' : 'border-ink-300 bg-white/70 hover:border-care-400 hover:bg-white'}`}
            >
              <input id="page-input" type="file" accept="image/*" multiple className="hidden"
                     onChange={(e) => { addFiles(e.target.files); e.target.value = '' }} />
              {files.length ? <Plus className="mx-auto text-care-600" /> : <UploadCloud size={30} className="mx-auto text-care-500" />}
              <p className="mt-2 font-medium text-ink-800">{files.length ? 'Add another page' : 'Drop the page photos here'}</p>
              <p className="mt-1 text-xs text-ink-500">or click to browse · JPG or PNG · up to 10 MB each</p>
            </div>
          )}
        </section>

        {progress && (
          <div className="card" aria-live="polite">
            <div className="flex items-center gap-3">
              <Loader2 size={18} className="animate-spin text-care-700" />
              <span className="text-sm font-medium text-ink-800">
                {progress.of > 1 ? `${progress.of} pages together: ` : ''}{STAGES[progress.stage]}
              </span>
            </div>
            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-ink-100">
              <div className="h-full w-1/3 rounded-full bg-care-600 animate-sweep" />
            </div>
          </div>
        )}

        {error && (
          <div role="alert" className="flex items-start gap-3 rounded-xl border border-vital-200 bg-vital-50 px-4 py-3.5 text-sm text-vital-800">
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div>
              <p className="font-semibold">{created ? 'Not every page was processed.' : 'Nothing was saved.'}</p>
              <p className="mt-0.5">{error}</p>
              {results && (
                <ul className="mt-2 space-y-0.5">
                  {results.map((r, i) => (
                    <li key={i}>
                      <span className="font-medium">{r.filename}</span>:{' '}
                      {r.status === 'added' ? `read, page ${r.page_no}` : r.status === 'duplicate' ? `skipped. ${r.detail}` : r.detail}
                    </li>
                  ))}
                </ul>
              )}
              {created && (
                <p className="mt-1.5">
                  Pages read before this are kept. <Link to={`/consultations/${created}`} className="font-semibold underline">Open the consultation</Link> or try again.
                </p>
              )}
            </div>
          </div>
        )}

        <button type="submit" disabled={busy} className="btn-primary w-full sm:w-auto">
          {busy ? <><Loader2 size={18} className="animate-spin" /> Reading…</> : <><ScanLine size={18} /> Read {files.length > 1 ? `${files.length} pages` : 'this prescription'}</>}
        </button>
      </form>

      <div className="mt-10 grid gap-4 sm:grid-cols-3">
        {TIPS.map(({ icon: Icon, title, desc }) => (
          <div key={title} className="card">
            <Icon size={18} className="text-care-600" />
            <h3 className="mt-3 font-display text-base font-semibold text-ink-900">{title}</h3>
            <p className="mt-1 text-xs leading-relaxed text-ink-600">{desc}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
