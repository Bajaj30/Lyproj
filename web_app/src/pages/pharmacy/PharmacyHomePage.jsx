import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { KeyRound, Loader2, ChevronRight, ClipboardList, ShieldCheck, Clock, ShieldPlus } from 'lucide-react'
import { useAuthStore } from '../../store/useAuthStore'
import { redeemCode, getGrants, apiError } from '../../services/api'

/**
 * Dispensing desk (Workflow stage 5). The chemist types the 6-digit code the
 * patient gives them and gets read-only access to that ONE prescription for a
 * short window. Every wrong code counts towards a lock on this account.
 */
export default function PharmacyHomePage() {
  const { user } = useAuthStore()
  const navigate = useNavigate()
  const [otp, setOtp] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [grants, setGrants] = useState(null)

  useEffect(() => { getGrants().then(setGrants).catch(() => setGrants([])) }, [])

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const g = await redeemCode(otp)
      navigate(`/pharmacy/grants/${g.id}`)
    } catch (err) {
      setError(apiError(err))
      setOtp('')
    } finally { setBusy(false) }
  }

  const open = (grants ?? []).filter((g) => g.status === 'active')
  const ended = (grants ?? []).filter((g) => g.status !== 'active').slice(0, 8)
  const secured = (user.email_verified || user.phone_verified) && user.mfa_enabled

  return (
    <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
      <header className="mb-8 animate-fade-in">
        <span className="rule-label text-care-700">Pharmacy</span>
        <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Dispensing desk</h1>
        <p className="mt-2 max-w-xl text-ink-600">
          {user.profile.pharmacy_name ?? user.full_name}. Enter the code the patient gives you to open their prescription.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[1.3fr_1fr]">
        <section className="space-y-6">
          <form onSubmit={submit} className="pad animate-slide-up p-6">
            <h2 className="flex items-center gap-2 font-display text-2xl font-semibold text-ink-900">
              <KeyRound size={20} className="text-care-700" /> Patient code
            </h2>
            <label htmlFor="otp" className="mt-4 block text-sm text-ink-600">The 6 digits on the patient’s phone</label>
            <input
              id="otp" value={otp} required autoFocus inputMode="numeric" autoComplete="one-time-code"
              pattern="[0-9 ]{6,7}" maxLength={7} placeholder="123 456"
              onChange={(e) => setOtp(e.target.value.replace(/[^0-9 ]/g, ''))}
              className="input-field data mt-1.5 text-center text-3xl tracking-[0.3em]"
              aria-invalid={!!error} aria-describedby="otp-help"
            />
            <p id="otp-help" className={`mt-2 text-sm ${error ? 'text-vital-700' : 'text-ink-500'}`}>
              {error ?? 'Opens one prescription, read-only, for 15 minutes. Each code works once.'}
            </p>
            <button type="submit" disabled={busy || otp.replace(/\D/g, '').length !== 6} className="btn-primary mt-4 w-full">
              {busy ? <Loader2 size={18} className="animate-spin" /> : <KeyRound size={18} />} Open prescription
            </button>
          </form>

          <section className="card">
            <h2 className="flex items-center gap-2 font-display text-xl font-semibold text-ink-900">
              <Clock size={18} className="text-care-700" /> Open now
            </h2>
            {!grants && <p className="mt-2 flex items-center gap-2 text-sm text-ink-500"><Loader2 size={14} className="animate-spin" /> Loading…</p>}
            {grants && open.length === 0 && <p className="mt-2 text-sm text-ink-600">No prescription is open at this counter.</p>}
            <ul className="mt-2 space-y-2">
              {open.map((g) => (
                <li key={g.id}>
                  <Link to={`/pharmacy/grants/${g.id}`} className="card-link flex items-center gap-3 py-3">
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold text-ink-900">{g.patient_name ?? 'Patient'}</p>
                      <p className="text-xs text-ink-500">{g.doctor_name} · until {new Date(g.expires_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</p>
                    </div>
                    <ChevronRight size={16} className="text-ink-400" />
                  </Link>
                </li>
              ))}
            </ul>
            {ended.length > 0 && (
              <details className="mt-3 text-sm text-ink-600">
                <summary className="cursor-pointer">Ended ({ended.length})</summary>
                <p className="mt-1 text-xs text-ink-500">Prescription details are no longer available once access ends. What you dispensed is in the dispensing record.</p>
                <ul className="mt-1 space-y-0.5 text-xs">
                  {ended.map((g) => <li key={g.id}>{g.status} {g.ended_at && `· ${new Date(g.ended_at).toLocaleString()}`}</li>)}
                </ul>
              </details>
            )}
          </section>
        </section>

        <aside className="space-y-6">
          <Link to="/pharmacy/records" className="card-link block">
            <ClipboardList size={18} className="text-care-600" />
            <p className="mt-3 font-display text-lg font-semibold text-ink-900">Dispensing record</p>
            <p className="mt-1 text-sm text-ink-600">Date, prescription ID, prescriber, what you handed over (brand, batch, quantity) and the check result. Kept after access ends; no patient contact details.</p>
          </Link>
          <Link to="/account" className="card-link block">
            <ShieldCheck size={18} className={secured ? 'text-care-600' : 'text-amber-600'} />
            <p className="mt-3 font-display text-lg font-semibold text-ink-900">Account security</p>
            <p className="mt-1 text-sm text-ink-600">{secured ? 'Verified, with two-step verification on.' : 'Verify your contact details and turn on two-step verification: this account opens patient prescriptions.'}</p>
          </Link>
          <Link to="/interactions" className="card-link block">
            <ShieldPlus size={18} className="text-care-600" />
            <p className="mt-3 font-display text-lg font-semibold text-ink-900">Check an interaction</p>
            <p className="mt-1 text-sm text-ink-600">Look up medicines by generic name against 180 cited rules.</p>
          </Link>
        </aside>
      </div>
    </div>
  )
}
