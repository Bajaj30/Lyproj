import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { FileText, ChevronRight, Loader2, Smartphone, BadgeCheck, Inbox } from 'lucide-react'
import { useAuthStore } from '../../store/useAuthStore'
import { getMyPrescriptions, apiError } from '../../services/api'
import { sendVerification } from '../../services/auth'

/**
 * Patient inbox (Workflow stage 4). Lists prescriptions a doctor has confirmed
 * and addressed to this patient's VERIFIED phone. Until the phone is verified
 * the list is empty by design, and the page says why rather than looking broken.
 */
const RISK = {
  none: null,
  minor: { cls: 'severity-minor', label: 'Minor interaction' },
  moderate: { cls: 'severity-moderate', label: 'Interaction to discuss' },
  severe: { cls: 'severity-severe', label: 'Serious interaction' },
}

const formatDay = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })

export default function PatientHomePage() {
  const { user } = useAuthStore()
  const [inbox, setInbox] = useState(null)
  const [error, setError] = useState(null)
  const [sending, setSending] = useState(false)
  const navigate = useNavigate()

  // Send the code straight away and go to the code screen (one click, not
  // three). A 429 means a code was just sent: go and enter that one.
  const verifyPhone = async () => {
    setSending(true)
    try {
      const res = await sendVerification('phone')
      navigate('/verify', { state: { channel: 'phone', sentTo: res.sent_to, returnTo: '/patient' } })
    } catch (e) {
      if (e?.response?.status === 429) navigate('/verify', { state: { channel: 'phone', returnTo: '/patient' } })
      else { setError(apiError(e)); setSending(false) }
    }
  }

  useEffect(() => {
    getMyPrescriptions().then(setInbox).catch((e) => setError(apiError(e)))
  }, [])

  return (
    <div className="mx-auto max-w-3xl px-4 sm:px-6">
      <header className="mb-7 animate-fade-in">
        <span className="rule-label text-care-700">Patient</span>
        <h1 className="mt-2 font-display text-4xl font-normal text-ink-900">Your prescriptions</h1>
        <p className="mt-2 text-ink-600">
          Signed in as <span className="font-medium text-ink-900">{user.full_name}</span>.
          Prescriptions appear here once your doctor confirms them for your phone number.
        </p>
      </header>

      {error && <p role="alert" className="card border-vital-200 bg-vital-50 text-sm text-vital-800">{error}</p>}

      {!inbox && !error && (
        <p className="flex items-center gap-2 text-ink-600"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</p>
      )}

      {inbox && !inbox.phone_verified && (
        <section className="card animate-slide-up">
          <div className="flex items-center gap-2">
            <Smartphone size={18} className="text-care-700" />
            <h2 className="font-display text-xl font-semibold text-ink-900">
              {inbox.phone ? 'Verify your phone number' : 'Add your phone number'}
            </h2>
          </div>
          <p className="mt-2 text-sm leading-relaxed text-ink-600">
            Doctors send prescriptions to a phone number. To keep them private, they only appear
            here after you prove the number is yours with a one-time code.
            {inbox.phone && <> Your number is <span className="data font-medium text-ink-900">{inbox.phone}</span>.</>}
          </p>
          {inbox.phone
            ? <button type="button" onClick={verifyPhone} disabled={sending} className="btn-primary mt-4">
                {sending ? <Loader2 size={16} className="animate-spin" /> : <BadgeCheck size={16} />} Verify it now
              </button>
            : <Link to="/account" className="btn-primary mt-4">
                <BadgeCheck size={16} /> Add it in your account
              </Link>}
        </section>
      )}

      {inbox?.phone_verified && inbox.prescriptions.length === 0 && (
        <section className="card animate-slide-up text-center">
          <Inbox className="mx-auto text-ink-400" size={28} />
          <p className="mt-2 font-display text-lg text-ink-900">No prescriptions yet</p>
          <p className="mt-1 text-sm text-ink-600">
            Ask your doctor to send your prescription to <span className="data">{inbox.phone}</span>.
          </p>
        </section>
      )}

      {inbox?.phone_verified && inbox.prescriptions.length > 0 && (
        <ul className="space-y-3">
          {inbox.prescriptions.map((p, i) => {
            const risk = RISK[p.overall_risk]
            return (
              <li key={p.id} className="animate-slide-up" style={{ animationDelay: `${i * 0.04}s` }}>
                <Link to={`/patient/prescriptions/${p.id}`} className="card-link flex items-center gap-4">
                  <span className="grid h-11 w-11 shrink-0 place-items-center rounded-lg border border-care-200 bg-care-50 text-care-700">
                    <FileText size={19} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-display text-lg font-semibold text-ink-900">
                      {p.doctor_name ?? 'Your doctor'}
                    </p>
                    <p className="text-sm text-ink-600">
                      {p.date ? `Dated ${p.date}` : `Confirmed ${formatDay(p.reviewed_at)}`}
                      {' · '}{p.medicine_count} medicine{p.medicine_count === 1 ? '' : 's'}
                    </p>
                    {risk && <span className={`chip mt-2 ${risk.cls}`}>{risk.label}</span>}
                  </div>
                  <ChevronRight size={18} className="shrink-0 text-ink-400" />
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
