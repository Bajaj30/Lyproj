import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, ClipboardList, Loader2 } from 'lucide-react'
import { getDispensingRecords, apiError } from '../../services/api'
import { DISPENSE_RESULT } from './dispenseResults'

/**
 * Workflow stage 7: what the pharmacy keeps after access ends, and nothing
 * more — date, prescription ID, prescriber and registration number, what was
 * handed over (brand, batch, quantity) and the check result. No patient phone,
 * no prescription contents, no diagnosis, no image.
 */
export default function DispensingRecordsPage() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { getDispensingRecords().then(setRows).catch((e) => setError(apiError(e))) }, [])

  return (
    <div className="mx-auto max-w-6xl px-4 sm:px-6">
      <Link to="/pharmacy" className="btn-ghost -ml-2 mb-4 inline-flex"><ArrowLeft size={16} /> Dispensing desk</Link>
      <header className="mb-6">
        <span className="rule-label text-care-700">Pharmacy</span>
        <h1 className="mt-2 flex items-center gap-2 font-display text-4xl font-normal text-ink-900"><ClipboardList className="text-care-700" /> Dispensing record</h1>
        <p className="mt-2 max-w-2xl text-sm text-ink-600">
          Every pack recorded as dispensed. This stays after a patient’s access ends, and holds no patient contact details.
        </p>
      </header>
      {error && <p role="alert" className="card text-sm text-vital-800">{error}</p>}
      {!rows && !error && <p className="flex items-center gap-2 text-ink-600"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</p>}
      {rows?.length === 0 && <p className="card text-sm text-ink-600">Nothing dispensed yet.</p>}
      {rows?.length > 0 && (
        <div className="card-tight overflow-x-auto">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="border-b border-ink-200 bg-ink-50 text-xs uppercase tracking-wider text-ink-500">
              <tr>{['Date', 'Prescription ID', 'Prescriber', 'Reg. no.', 'Dispensed', 'Batch', 'Qty', 'Result'].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-ink-100">
              {rows.map((r) => {
                const res = DISPENSE_RESULT[r.result] ?? DISPENSE_RESULT.could_not_verify
                return (
                  <tr key={r.id}>
                    <td className="whitespace-nowrap px-3 py-2">{new Date(r.date).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</td>
                    <td className="data whitespace-nowrap px-3 py-2 text-xs">{r.prescription_id}</td>
                    <td className="px-3 py-2">{r.prescriber_name}</td>
                    <td className="data px-3 py-2 text-xs">{r.prescriber_registration_no || '—'}</td>
                    <td className="px-3 py-2"><span className="font-medium">{r.brand || r.composition}</span>{r.strength && <span className="text-ink-500"> · {r.strength}</span>}</td>
                    <td className="data px-3 py-2 text-xs">{r.batch || '—'}</td>
                    <td className="px-3 py-2">{r.quantity || '—'}</td>
                    <td className="px-3 py-2"><span className={`chip ${res.cls}`}>{res.label}</span></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
