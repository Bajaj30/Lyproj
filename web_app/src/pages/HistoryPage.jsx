import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Search, ChevronRight, ScanLine, FileX2, Loader2, Lock, PencilLine, ShieldAlert } from 'lucide-react'
import { getConsultations, apiError } from '../services/api'

/**
 * The record shelf: this doctor's consultations, newest first.
 *
 * There is deliberately no demo dataset behind this screen. An empty shelf is
 * an honest answer; a shelf of invented patients that looks identical to real
 * records is not.
 */

const RISK_TONE = {
  none:            'severity-none',
  minor:           'severity-minor',
  moderate:        'severity-moderate',
  severe:          'severity-severe',
  contraindicated: 'severity-contraindicated',
}

const STATUS = {
  draft:     { icon: PencilLine, cls: 'text-amber-700', label: () => 'draft, not sent' },
  revising:  { icon: PencilLine, cls: 'text-amber-700', label: (c) => `editing, patient sees v${c.current_version}` },
  finalised: { icon: Lock,       cls: 'text-care-700',  label: (c) => `finalised v${c.current_version}` },
}

export default function HistoryPage() {
  const [rows, setRows] = useState([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    getConsultations()
      .then(setRows)
      .catch((e) => setError(apiError(e)))
      .finally(() => setLoading(false))
  }, [])

  const q = search.trim().toLowerCase()
  const filtered = !q ? rows : rows.filter((c) =>
    [c.patient_name, c.patient_phone, c.created_at?.slice(0, 10)].filter(Boolean)
      .some((s) => String(s).toLowerCase().includes(q)))

  return (
    <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
      <header className="mb-8 flex animate-fade-in flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <span className="rule-label text-care-700">Records</span>
          <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Consultations</h1>
          <p className="mt-1 text-sm text-ink-600">
            {loading ? 'Loading…' : `${filtered.length} prescription${filtered.length === 1 ? '' : 's'} on record`}
          </p>
        </div>
        <Link to="/upload" className="btn-primary py-2.5 text-sm"><ScanLine size={16} /> New consultation</Link>
      </header>

      <div className="relative mb-6 animate-slide-up">
        <Search size={18} className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-ink-400" />
        <input type="search" value={search} onChange={(e) => setSearch(e.target.value)}
               placeholder="Search by patient name, phone or date…" className="input-field py-3 pl-11" />
      </div>

      {loading && <div className="flex items-center gap-2 p-8 text-ink-600"><Loader2 className="h-5 w-5 animate-spin" /> Loading records…</div>}
      {error && <div className="card border-vital-200 bg-vital-50 text-sm text-vital-800">{error}</div>}

      <div className="space-y-3">
        {filtered.map((c, i) => {
          const s = STATUS[c.status] ?? STATUS.draft
          const day = new Date(c.created_at)
          return (
            <Link key={c.id} to={`/consultations/${c.id}`} className="card-link flex animate-slide-up items-center gap-4"
                  style={{ animationDelay: `${Math.min(i, 8) * 0.04}s` }}>
              <div className="hidden w-16 shrink-0 flex-col items-center border-r border-dashed border-ink-200 pr-4 sm:flex">
                <span className="data text-2xl font-bold text-care-700">{day.getDate()}</span>
                <span className="rule-label mt-0.5">{day.toLocaleString('en', { month: 'short' })}</span>
              </div>
              <div className="min-w-0 flex-1">
                <div className="mb-1.5 flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-ink-900">{c.patient_name || 'Patient not named'}</span>
                  {c.patient_phone && <span className="data text-sm text-ink-500">{c.patient_phone}</span>}
                  <span className={`ml-auto rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${RISK_TONE[c.overall_risk] ?? RISK_TONE.none}`}>
                    {c.overall_risk === 'none' ? 'no interactions' : `${c.overall_risk} risk`}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs text-ink-500">
                  <span className={`inline-flex items-center gap-1 ${s.cls}`}><s.icon size={12} /> {s.label(c)}</span>
                  <span>{c.page_count} page{c.page_count === 1 ? '' : 's'} · {c.medicine_count} medicine{c.medicine_count === 1 ? '' : 's'}</span>
                  {c.needs_attention > 0 && <span className="inline-flex items-center gap-1 text-vital-700"><ShieldAlert size={12} /> {c.needs_attention} to confirm</span>}
                  {c.legacy && <span className="chip py-0">from before consultations</span>}
                </div>
              </div>
              <ChevronRight size={18} className="shrink-0 text-ink-300" />
            </Link>
          )
        })}
      </div>

      {!loading && !error && filtered.length === 0 && (
        <div className="card py-16 text-center">
          <FileX2 size={40} className="mx-auto text-ink-300" />
          <p className="mt-4 font-display text-xl text-ink-800">{rows.length === 0 ? 'No consultations yet' : 'Nothing matches that search'}</p>
          <p className="mt-1 text-sm text-ink-500">{rows.length === 0 ? 'Start one with the patient’s name, phone and the prescription pages.' : 'Try a patient name, phone or date.'}</p>
          {rows.length === 0 && <Link to="/upload" className="btn-primary mt-6"><ScanLine size={18} /> New consultation</Link>}
        </div>
      )}
    </div>
  )
}
