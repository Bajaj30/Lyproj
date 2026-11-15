import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BadgeCheck, Loader2, ShieldX, Stethoscope, Store, ShieldAlert, Inbox } from 'lucide-react'
import { getVettingAccounts, getVettingSummary, decideVetting, apiError } from '../../services/api'
import { useAuthStore } from '../../store/useAuthStore'

/**
 * Administrator: review doctor and pharmacy accounts (Workflow §10 Q5).
 *
 * Nothing here checks a medical-council or drug-licence register automatically;
 * there is no public API for either. The administrator looks the number up
 * themselves and records the decision. That is what "approved" means, and the
 * public verification page says so in those words.
 */
const TABS = [['pending', 'Waiting'], ['approved', 'Approved'], ['rejected', 'Rejected']]
const fmt = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

export default function AdminPage() {
  const { user } = useAuthStore()
  const [tab, setTab] = useState('pending')
  const [rows, setRows] = useState(null)
  const [counts, setCounts] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    getVettingAccounts(tab).then((r) => { setRows(r); setError(null) }).catch((e) => setError(apiError(e)))
    getVettingSummary().then(setCounts).catch(() => {})
  }, [tab])
  useEffect(() => { load() }, [load])

  return (
    <div className="mx-auto max-w-4xl px-4 sm:px-6">
      <header className="mb-6">
        <span className="rule-label text-care-700">Administrator</span>
        <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Verify accounts</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-600">
          A doctor cannot send a prescription, and a pharmacy cannot open one, until you approve the account.
          Check the registration or licence number against the council or licensing authority yourself: this page does not.
        </p>
      </header>

      {!user.mfa_enabled && (
        <p className="card mb-5 flex items-start gap-2.5 border-amber-300 bg-amber-50 text-sm text-amber-900">
          <ShieldAlert size={17} className="mt-0.5 shrink-0" />
          <span>This account decides who is a doctor, and it has no two-step verification. <Link to="/account" className="font-semibold underline">Turn it on</Link>.</span>
        </p>
      )}

      <div role="tablist" className="mb-4 flex gap-1 rounded-lg bg-ink-100 p-1">
        {TABS.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} onClick={() => { setRows(null); setTab(key) }}
                  className={`flex-1 rounded-md px-3 py-2 text-sm font-medium ${tab === key ? 'bg-white text-care-800 shadow-sm' : 'text-ink-600'}`}>
            {label}{counts && <span className="data ml-1.5 text-ink-500">{counts[key]}</span>}
          </button>
        ))}
      </div>

      {error && <p role="alert" className="card text-sm text-vital-800">{error}</p>}
      {!rows && !error && <p className="flex items-center gap-2 text-ink-600"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</p>}
      {rows?.length === 0 && (
        <div className="card py-10 text-center">
          <Inbox className="mx-auto text-ink-300" size={30} />
          <p className="mt-2 text-sm text-ink-600">{tab === 'pending' ? 'No accounts are waiting for review.' : 'None.'}</p>
        </div>
      )}
      <ul className="space-y-3">
        {rows?.map((a) => <AccountCard key={a.id} a={a} onDone={load} />)}
      </ul>
    </div>
  )
}

function AccountCard({ a, onDone }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const doctor = a.role === 'doctor'
  const number = doctor ? a.profile.registration_no : a.profile.drug_licence_no
  const Icon = doctor ? Stethoscope : Store

  const decide = async (decision) => {
    setBusy(decision)
    setError(null)
    try { await decideVetting(a.id, decision, note); onDone() }
    catch (e) { setError(apiError(e)) }
    finally { setBusy(null) }
  }

  return (
    <li className="card">
      <div className="flex flex-wrap items-start gap-3">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-care-200 bg-care-50 text-care-700"><Icon size={18} /></span>
        <div className="min-w-0 flex-1">
          <p className="font-display text-xl font-semibold text-ink-900">{a.full_name}</p>
          <p className="text-sm text-ink-600">
            {doctor ? 'Doctor' : 'Pharmacy'} · registered {fmt(a.created_at)} · {a.email || a.phone || a.username}
            {!(a.email_verified || a.phone_verified) && <span className="text-amber-700"> · contact not verified</span>}
          </p>
        </div>
        <span className={`chip ${a.vetting_status === 'approved' ? 'border-care-300 bg-care-50 text-care-800' : a.vetting_status === 'rejected' ? 'border-vital-300 bg-vital-50 text-vital-800' : 'border-amber-300 bg-amber-50 text-amber-900'}`}>
          {a.vetting_status}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-sm">
        <dt className="text-ink-500">{doctor ? 'Registration no.' : 'Drug licence no.'}</dt>
        <dd className={`data ${number ? 'font-semibold text-ink-900' : 'text-amber-700'}`}>{number || 'not given'}</dd>
        {doctor
          ? <><dt className="text-ink-500">Speciality</dt><dd>{a.profile.speciality}</dd>
              <dt className="text-ink-500">Clinic</dt><dd>{a.profile.clinic_address}</dd></>
          : <><dt className="text-ink-500">Pharmacy</dt><dd>{a.profile.pharmacy_name}</dd>
              <dt className="text-ink-500">Address</dt><dd>{a.profile.pharmacy_address}</dd></>}
        {a.vetting_note && <><dt className="text-ink-500">Note</dt><dd>{a.vetting_note}</dd></>}
        {a.vetted_at && <><dt className="text-ink-500">Decided</dt><dd>{fmt(a.vetted_at)}</dd></>}
      </dl>

      <label className="mt-3 block text-xs font-medium text-ink-600">
        Note to the applicant {a.vetting_status !== 'rejected' && <span className="text-ink-400">(required to reject)</span>}
        <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} className="input-field mt-1 py-2"
               placeholder={number ? 'e.g. Registration confirmed on the state council register' : 'e.g. Please add your registration number'} />
      </label>
      {error && <p role="alert" className="mt-2 text-sm text-vital-700">{error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {a.vetting_status !== 'approved' && (
          <button type="button" disabled={!!busy} onClick={() => decide('approve')} className="btn-primary py-2 text-sm">
            {busy === 'approve' ? <Loader2 size={15} className="animate-spin" /> : <BadgeCheck size={15} />} Approve
          </button>
        )}
        {a.vetting_status !== 'rejected' && (
          <button type="button" disabled={!!busy} onClick={() => decide('reject')} className="btn-secondary py-2 text-sm hover:border-vital-400 hover:text-vital-800">
            {busy === 'reject' ? <Loader2 size={15} className="animate-spin" /> : <ShieldX size={15} />}
            {a.vetting_status === 'approved' ? 'Withdraw approval' : 'Reject'}
          </button>
        )}
      </div>
    </li>
  )
}
