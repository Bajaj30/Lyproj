import axios from 'axios'

// `??`, not `||`: the production build sets VITE_API_URL="" so the API is the
// page's own origin (FastAPI serves both), and an empty string must survive.
const API_BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

// A real VLM read takes 10-30s on a dense page. The old 30s default was long
// enough to time out on exactly the prescriptions that matter most.
//
// Auth: the session is an HttpOnly cookie the page can never read, so
// `withCredentials` sends it. The X-Requested-With header is the backend's CSRF
// check — a cross-site request cannot set it without a preflight the API refuses.
const api = axios.create({
  baseURL: API_BASE,
  timeout: 120000,
  withCredentials: true,
  headers: { 'X-Requested-With': 'PrescriptAI' },
})

// One place hears about an expired session. The auth store registers a
// listener so a 401 anywhere signs the UI out instead of leaving stale state.
let onUnauthorized = null
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn }
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const url = err?.config?.url || ''
    if (err?.response?.status === 401 && !url.startsWith('/api/auth/') && onUnauthorized) {
      onUnauthorized()
    }
    return Promise.reject(err)
  },
)

/** The API's own error message, or a fallback. */
export const apiError = (err, fallback = 'Something went wrong. Please try again.') => {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg.replace(/^Value error, /, '')
  if (!err?.response) return 'Could not reach the PrescriptAI API. Check that the backend is running on port 8000.'
  return fallback
}

export const uploadPrescription = async (file) => {
  const formData = new FormData()
  formData.append('image', file)
  const { data } = await api.post('/api/prescriptions/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return data
}

export const getPrescriptions = async (limit = 20, offset = 0) => {
  const { data } = await api.get('/api/prescriptions', { params: { limit, offset } })
  return data
}

export const getPrescription = async (id) => {
  const { data } = await api.get(`/api/prescriptions/${id}`)
  return data
}

/**
 * Record one doctor edit.
 * This is the clinical audit trail AND the training corpus (§8.3) — the
 * backend stores the model's original value alongside the correction, so the
 * before/after pair survives even after the UI shows the corrected value.
 */
export const submitCorrection = async (prescriptionId, correction) => {
  const { data } = await api.post(
    `/api/prescriptions/${prescriptionId}/corrections`,
    correction,
  )
  return data
}

// ── consultations (Workflow stages 1–3) ──────────────────────────────────────
// A consultation is one doctor, one patient, one prescription of N pages. Every
// mutating call returns the whole consultation, so the screen re-renders from
// what the server now holds rather than from a local guess.

const data = async (p) => (await p).data

export const createConsultation = (body) => data(api.post('/api/consultations', body))
export const getConsultations = () => data(api.get('/api/consultations'))
export const getConsultation = (id) => data(api.get(`/api/consultations/${id}`))
export const updateConsultation = (id, body) => data(api.patch(`/api/consultations/${id}`, body))

export const uploadPage = (consultationId, file) => {
  const form = new FormData()
  form.append('image', file)
  return data(api.post(`/api/consultations/${consultationId}/pages`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }))
}
export const deletePage = (consultationId, pageId) =>
  data(api.delete(`/api/consultations/${consultationId}/pages/${pageId}`))
export const addMedicine = (consultationId, body) =>
  data(api.post(`/api/consultations/${consultationId}/medicines`, body))
export const removeMedicine = (pageId, index, note) =>
  data(api.post(`/api/prescriptions/${pageId}/medicines/${index}/remove`, { note: note || null }))
export const restoreMedicine = (pageId, index) =>
  data(api.post(`/api/prescriptions/${pageId}/medicines/${index}/restore`))
export const finaliseConsultation = (id, { acknowledgeUnresolved = false, changeNote = '' } = {}) =>
  data(api.post(`/api/consultations/${id}/finalise`, {
    acknowledge_unresolved: acknowledgeUnresolved, change_note: changeNote || null,
  }))

/** Public: what the QR on a prescription PDF opens. */
export const verifyDocument = (vid) => data(api.get(`/api/verify/${encodeURIComponent(vid)}`))

// ── patient app (Workflow stages 4–5) ────────────────────────────────────────

export const getMyPrescriptions = async () => {
  const { data } = await api.get('/api/patient/prescriptions')
  return data
}

export const getMyPrescription = async (id) => {
  const { data } = await api.get(`/api/patient/prescriptions/${id}`)
  return data
}

export const myPrescriptionImageUrl = (id, page = 1) =>
  `${API_BASE}/api/patient/prescriptions/${id}/pages/${page}/image`

/** A 6-digit code for ONE pharmacy, for this prescription only. Shown once. */
export const shareWithPharmacy = (id) => data(api.post(`/api/patient/prescriptions/${id}/share`))
export const revokeShare = (grantId) => data(api.post(`/api/patient/grants/${grantId}/revoke`))

// ── pharmacy app (Workflow stages 5–7) ───────────────────────────────────────

export const redeemCode = (otp) => data(api.post('/api/pharmacy/redeem', { otp }))
export const getGrants = () => data(api.get('/api/pharmacy/grants'))
export const getGrant = (id) => data(api.get(`/api/pharmacy/grants/${id}`))
export const grantImageUrl = (id, page) => `${API_BASE}/api/pharmacy/grants/${id}/pages/${page}/image`

/** method: 'qr' | 'photo' | 'manual'. `fields` are the form fields; `photo` a File. */
export const checkPack = (grantId, method, fields = {}, photo = null) => {
  const form = new FormData()
  form.append('method', method)
  Object.entries(fields).forEach(([k, v]) => { if (v != null && String(v).trim()) form.append(k, v) })
  if (photo) form.append('image', photo)
  return data(api.post(`/api/pharmacy/grants/${grantId}/checks`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }))
}
export const confirmCheck = (grantId, checkId) =>
  data(api.post(`/api/pharmacy/grants/${grantId}/checks/${checkId}/confirm`))
export const getDispensingRecords = () => data(api.get('/api/pharmacy/records'))

/**
 * Fetch the PDF and hand it to the browser as a download. Fetched rather than
 * linked so a failure shows as an error on the page, not as a JSON tab.
 */
export const downloadMyPrescriptionPdf = async (id) => {
  let res
  try {
    res = await api.get(`/api/patient/prescriptions/${id}/pdf`, { responseType: 'blob' })
  } catch (err) {
    // With responseType 'blob' an error body is a Blob too; decode it so
    // apiError() can read the API's message.
    const body = err?.response?.data
    if (body instanceof Blob) {
      try { err.response.data = JSON.parse(await body.text()) } catch { /* not JSON */ }
    }
    throw err
  }
  const name = /filename="([^"]+)"/.exec(res.headers['content-disposition'] ?? '')?.[1]
    ?? `prescription-${id}.pdf`
  const url = URL.createObjectURL(res.data)
  const a = Object.assign(document.createElement('a'), { href: url, download: name })
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const getCorrections = async (limit = 100) => {
  const { data } = await api.get('/api/corrections', { params: { limit } })
  return data
}

/**
 * Ad-hoc interaction lookup by GENERIC name.
 * Bypasses S2/S3 — the caller asserts the names are right. The prescription
 * pipeline does not use this; it backs the manual lookup screen.
 */
export const checkInteractions = async (medicines) => {
  const { data } = await api.post('/api/interactions/check', { medicines })
  return data
}

export const getStats = async () => {
  const { data } = await api.get('/api/stats')
  return data
}

export const getHealth = async () => {
  const { data } = await api.get('/api/health')
  return data
}

export const imageUrl = (path) => (path ? `${API_BASE}${path}` : null)

export default api
