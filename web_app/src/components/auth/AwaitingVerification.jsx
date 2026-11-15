import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Hourglass, ShieldX, RefreshCw, BadgeCheck, Loader2 } from 'lucide-react'
import { useAuthStore } from '../../store/useAuthStore'

/**
 * Shown to a doctor or pharmacy account that an administrator has not approved
 * yet (or has rejected). Until then the API refuses every route that issues,
 * reads or dispenses a prescription, so there is nothing else to show: this
 * says what is being checked, and what the account can still do.
 */
const CHECKED = {
  doctor: [['Name', (u) => u.full_name], ['Registration no.', (u) => u.profile.registration_no],
           ['Speciality', (u) => u.profile.speciality], ['Clinic', (u) => u.profile.clinic_address]],
  chemist: [['Name', (u) => u.full_name], ['Pharmacy', (u) => u.profile.pharmacy_name],
            ['Drug licence no.', (u) => u.profile.drug_licence_no], ['Address', (u) => u.profile.pharmacy_address]],
}

export default function AwaitingVerification() {
  const { user, refresh } = useAuthStore()
  const [checking, setChecking] = useState(false)
  const rejected = user.vetting_status === 'rejected'
  const rows = CHECKED[user.role] ?? []
  const missingNumber = rows.some(([label, get]) => /no\.$/.test(label) && !get(user))

  const recheck = async () => { setChecking(true); await refresh(); setChecking(false) }

  return (
    <div className="mx-auto max-w-2xl px-4 sm:px-6">
      <section className={`card ${rejected ? 'border-vital-300' : 'border-amber-300'}`}>
        <p className={`flex items-center gap-2 font-display text-2xl font-semibold ${rejected ? 'text-vital-900' : 'text-ink-900'}`}>
          {rejected ? <ShieldX className="text-vital-700" /> : <Hourglass className="text-amber-600" />}
          {rejected ? 'Your account was not approved' : 'Your account is being verified'}
        </p>
        <p className="mt-2 text-sm leading-relaxed text-ink-700">
          {rejected
            ? 'An administrator reviewed your details and did not approve this account.'
            : user.role === 'doctor'
              ? 'Before you can send a prescription to a patient, a PrescriptAI administrator checks the details below. You will be able to scan prescriptions as soon as they approve.'
              : 'Before you can open a patient’s prescription, a PrescriptAI administrator checks the details below. You will be able to enter patient codes as soon as they approve.'}
        </p>
        {user.vetting_note && (
          <p className="mt-3 rounded-lg bg-ink-50 px-3 py-2 text-sm text-ink-800">
            <span className="font-semibold">Note from the administrator:</span> {user.vetting_note}
          </p>
        )}

        <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm">
          {rows.map(([label, get]) => (
            <div key={label} className="contents">
              <dt className="text-ink-500">{label}</dt>
              <dd className={get(user) ? 'text-ink-900' : 'text-amber-700'}>{get(user) || 'not given'}</dd>
            </div>
          ))}
        </dl>
        {missingNumber && !rejected && (
          <p className="mt-3 text-sm text-amber-800">
            You have not given a {user.role === 'doctor' ? 'registration' : 'drug licence'} number. Add it in your account: it is the main thing the administrator checks.
          </p>
        )}

        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" onClick={recheck} disabled={checking} className="btn-primary py-2.5 text-sm">
            {checking ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />} Check again
          </button>
          <Link to="/account" className="btn-secondary py-2.5 text-sm"><BadgeCheck size={16} /> Edit my details</Link>
        </div>
        <p className="mt-4 text-xs text-ink-500">
          Changing your name or {user.role === 'doctor' ? 'registration' : 'licence'} number later sends the account back for review.
        </p>
      </section>
    </div>
  )
}
