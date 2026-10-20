import { useEffect, useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { MailCheck, Loader2, RotateCw, Terminal, Inbox, ExternalLink } from 'lucide-react'
import { AuthShell, CodeField, Alert } from '../../components/auth/AuthUI'
import { confirmVerification, sendVerification, ROLE_HOME } from '../../services/auth'
import { apiError, getHealth } from '../../services/api'
import { useAuthStore } from '../../store/useAuthStore'

/** Confirm an email or phone with the 6-digit code sent to it. */
export default function VerifyPage() {
  const navigate = useNavigate()
  const { state } = useLocation()
  const { user, setUser } = useAuthStore()

  const channel = state?.channel
    || (user?.email && !user.email_verified ? 'email' : user?.phone && !user.phone_verified ? 'phone' : null)

  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(state?.sentTo ? `Code sent to ${state.sentTo}.` : null)
  const [cooldown, setCooldown] = useState(state?.sentTo ? 60 : 0)
  // Set when codes go to a local demo inbox (Mailpit) instead of real mail/SMS.
  const [demoInbox, setDemoInbox] = useState(null)

  useEffect(() => {
    getHealth().then((h) => setDemoInbox(h.demo_inbox_url)).catch(() => {})
  }, [])

  useEffect(() => {
    if (cooldown <= 0) return undefined
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000)
    return () => clearTimeout(t)
  }, [cooldown])

  const home = ROLE_HOME[user?.role] || '/'
  // Where to go afterwards: the page that sent us here (an in-app path only),
  // else home after sign-up, else the account page.
  const r = state?.returnTo
  const returnTo = typeof r === 'string' && r.startsWith('/') && !r.startsWith('//') ? r : null
  const done = returnTo || (state?.welcome ? home : '/account')
  if (!channel || (channel === 'email' ? user?.email_verified : user?.phone_verified)) {
    return <Navigate to={done} replace />
  }
  const what = channel === 'email' ? 'email address' : 'phone number'
  const destination = channel === 'email' ? user.email : user.phone

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      setUser(await confirmVerification(channel, code))
      navigate(done, { replace: true, state: { verified: channel } })
    } catch (err) {
      setError(apiError(err)); setCode('')
    } finally { setBusy(false) }
  }

  const resend = async () => {
    setError(null)
    try {
      const res = await sendVerification(channel)
      setNotice(`New code sent to ${res.sent_to}.`)
      setCooldown(60)
    } catch (err) {
      setError(apiError(err))
      const wait = Number(err?.response?.headers?.['retry-after'])
      if (wait) setCooldown(wait)
    }
  }

  return (
    <AuthShell
      kicker={state?.welcome ? 'Account created · one more step' : 'Verify'}
      title={`Confirm your ${what}`}
      blurb={channel === 'phone'
        // Prototype: phone codes are delivered by email; the notice says where.
        ? `Enter the 6-digit code for ${destination}. It expires in 10 minutes.`
        : `Enter the 6-digit code we sent to ${destination}. It expires in 10 minutes.`}
      aside={!demoInbox && (
        <Alert kind="info" title="Running locally?">
          <span className="inline-flex items-center gap-1.5"><Terminal size={14} /> Without SMTP or an SMS provider configured,</span>{' '}
          codes are printed in the backend terminal instead of being sent.
        </Alert>
      )}
    >
      <form onSubmit={submit} className="space-y-5" noValidate>
        {notice && !error && <Alert kind="success">{notice}</Alert>}
        {/* In the form, not the aside: AuthShell hides its aside on phones,
            and a phone is exactly where a patient opens this page. */}
        {demoInbox && (
          <Alert kind="info" title="Demo mode">
            Codes, including phone codes, arrive by email in the demo inbox. Nothing is
            sent to a real phone or mailbox.{' '}
            <a href={demoInbox} target="_blank" rel="noreferrer"
               className="inline-flex items-center gap-1 font-semibold underline">
              <Inbox size={14} /> Open the demo inbox <ExternalLink size={12} />
            </a>
          </Alert>
        )}
        {error && <Alert>{error}</Alert>}
        <CodeField label="Verification code" value={code} onChange={setCode} />
        <button type="submit" disabled={busy || code.length !== 6} className="btn-primary w-full">
          {busy ? <Loader2 size={18} className="animate-spin" /> : <MailCheck size={18} />} Verify {what}
        </button>
        <div className="flex items-center justify-between border-t border-ink-200 pt-4 text-sm">
          <button type="button" onClick={resend} disabled={cooldown > 0}
                  className="inline-flex items-center gap-1.5 font-medium text-care-700 hover:text-care-800 disabled:text-ink-400">
            <RotateCw size={15} /> {cooldown > 0 ? `Resend in ${cooldown}s` : 'Resend code'}
          </button>
          <button type="button" onClick={() => navigate(done, { replace: true })}
                  className="btn-ghost -mr-3">
            Do this later
          </button>
        </div>
      </form>
    </AuthShell>
  )
}
