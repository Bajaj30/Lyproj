import { useEffect, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { getPrescription, apiError } from '../services/api'

/**
 * /review/:pageId — kept so links from before consultations existed still work.
 * Every page now belongs to a consultation (stage 1), and review happens there,
 * across all of its pages at once.
 */
export default function ReviewPage() {
  const { id } = useParams()
  const [target, setTarget] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    getPrescription(id)
      .then((p) => setTarget(`/consultations/${p.consultation_id}`))
      .catch((e) => setError(apiError(e)))
  }, [id])

  if (target) return <Navigate to={target} replace />
  if (error) return <p role="alert" className="card mx-auto max-w-xl text-sm text-vital-800">{error}</p>
  return <p className="flex items-center gap-2 p-8 text-ink-600"><Loader2 className="h-5 w-5 animate-spin" />Opening…</p>
}
