import { useEffect, useState } from 'react'
import { FlaskConical } from 'lucide-react'
import { getHealth } from '../services/api'

/**
 * Shown whenever the backend is on the MOCK reader (no ANTHROPIC_API_KEY yet).
 * The mock returns the same sample prescription for every page, and a pack photo
 * always reads as the same pack. Without this banner a demo reading looks
 * exactly like a real one, which is the one confusion this product exists to
 * prevent. It disappears on its own once a key is added and the API restarted.
 */
let cached = null

export default function DemoReaderBanner({ what = 'page' }) {
  const [health, setHealth] = useState(cached)
  useEffect(() => {
    if (cached) return
    getHealth().then((h) => { cached = h; setHealth(h) }).catch(() => {})
  }, [])
  // Pack photos can be on the demo reader while pages are real (Koncile reads
  // pages; packs need the Claude reader), so the pack banner has its own flag.
  const mock = what === 'pack'
    ? (health?.pack_reader_is_mock ?? health?.reader_is_mock)
    : health?.reader_is_mock
  if (!mock) return null
  return (
    <div role="note" className="mb-5 flex items-start gap-2.5 rounded-xl border border-violet-200 bg-violet-50 px-4 py-3 text-sm text-violet-900">
      <FlaskConical size={17} className="mt-0.5 shrink-0" aria-hidden />
      <p>
        <strong>Demo reader.</strong>{' '}
        {what === 'pack'
          ? 'A photographed pack always reads as the same sample pack, not your photo. QR and typed checks are real.'
          : 'Every page reads as the same sample prescription, not your image.'}{' '}
        Add <code className="data rounded bg-white/70 px-1">ANTHROPIC_API_KEY</code> to <code className="data rounded bg-white/70 px-1">.env</code> and
        restart the API to read real {what === 'pack' ? 'labels' : 'handwriting'}.
      </p>
    </div>
  )
}
