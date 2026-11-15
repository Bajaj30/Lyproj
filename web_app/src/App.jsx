import { useEffect } from 'react'
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import RequireAuth from './components/auth/RequireAuth'
import HomePage from './pages/HomePage'
import UploadPage from './pages/UploadPage'
import ResultsPage from './pages/ResultsPage'
import ReviewPage from './pages/ReviewPage'
import InteractionsPage from './pages/InteractionsPage'
import HistoryPage from './pages/HistoryPage'
import DashboardPage from './pages/DashboardPage'
import LoginPage from './pages/auth/LoginPage'
import RegisterPage from './pages/auth/RegisterPage'
import ForgotPasswordPage from './pages/auth/ForgotPasswordPage'
import VerifyPage from './pages/auth/VerifyPage'
import AccountPage from './pages/auth/AccountPage'
import PatientHomePage from './pages/patient/PatientHomePage'
import PatientPrescriptionPage from './pages/patient/PatientPrescriptionPage'
import ConsultationPage from './pages/ConsultationPage'
import VerifyDocumentPage from './pages/VerifyDocumentPage'
import PharmacyHomePage from './pages/pharmacy/PharmacyHomePage'
import PharmacyGrantPage from './pages/pharmacy/PharmacyGrantPage'
import DispensingRecordsPage from './pages/pharmacy/DispensingRecordsPage'
import AdminPage from './pages/admin/AdminPage'
import { useAuthStore } from './store/useAuthStore'

// Prescription screens hold patient data: doctors only (the API enforces the
// same rule on every request — this just avoids rendering a screen that 403s).
const doctor = (el) => <RequireAuth roles={['doctor']}>{el}</RequireAuth>
const patient = (el) => <RequireAuth roles={['patient']}>{el}</RequireAuth>
const chemist = (el) => <RequireAuth roles={['chemist']}>{el}</RequireAuth>

export default function App() {
  const refresh = useAuthStore((s) => s.refresh)
  useEffect(() => { refresh() }, [refresh])

  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="interactions" element={<InteractionsPage />} />
        {/* Public: what the QR on a prescription PDF opens. Shows no patient data. */}
        <Route path="check" element={<VerifyDocumentPage />} />
        <Route path="check/:vid" element={<VerifyDocumentPage />} />

        <Route path="login" element={<LoginPage />} />
        <Route path="register" element={<RegisterPage />} />
        <Route path="forgot-password" element={<ForgotPasswordPage />} />
        <Route path="verify" element={<RequireAuth><VerifyPage /></RequireAuth>} />
        <Route path="account" element={<RequireAuth><AccountPage /></RequireAuth>} />

        <Route path="upload" element={doctor(<UploadPage />)} />
        <Route path="consultations/:id" element={doctor(<ConsultationPage />)} />
        <Route path="results/:id" element={doctor(<ResultsPage />)} />
        <Route path="review/:id" element={doctor(<ReviewPage />)} />
        <Route path="results" element={doctor(<ResultsPage />)} />
        <Route path="history" element={doctor(<HistoryPage />)} />
        <Route path="dashboard" element={doctor(<DashboardPage />)} />

        <Route path="patient" element={patient(<PatientHomePage />)} />
        <Route path="patient/prescriptions/:id" element={patient(<PatientPrescriptionPage />)} />
        <Route path="pharmacy" element={chemist(<PharmacyHomePage />)} />
        <Route path="pharmacy/grants/:id" element={chemist(<PharmacyGrantPage />)} />
        <Route path="pharmacy/records" element={chemist(<DispensingRecordsPage />)} />
        <Route path="admin" element={<RequireAuth roles={['admin']}><AdminPage /></RequireAuth>} />
      </Route>
    </Routes>
  )
}
