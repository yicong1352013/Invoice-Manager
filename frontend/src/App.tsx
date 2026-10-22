import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import UploadPage from './pages/UploadPage';
import InvoiceListPage from './pages/InvoiceListPage';
import InvoiceDetailPage from './pages/InvoiceDetailPage';
import LoginPage from './pages/LoginPage';
import SettingsPage from './pages/SettingsPage';
import { MainLayout } from './components/layout';
import { getLLMStatus, getToken, clearToken, getCurrentUser } from './services/api';
import type { User } from './types/user';

function AppContent() {
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null);
  const [showLlmPromo, setShowLlmPromo] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  // Validate any stored token once on load
  useEffect(() => {
    const bootstrap = async () => {
      if (!getToken()) {
        setAuthChecked(true);
        return;
      }
      try {
        setUser(await getCurrentUser());
      } catch {
        clearToken();
      } finally {
        setAuthChecked(true);
      }
    };
    bootstrap();
  }, []);

  // Route guard: unauthenticated users always land on /login
  useEffect(() => {
    if (authChecked && !user && location.pathname !== '/login') {
      navigate('/login', { replace: true });
    }
  }, [authChecked, user, location, navigate]);

  // Check LLM status after login (drives the admin promo banner)
  useEffect(() => {
    if (!user) return;
    const checkLLMStatus = async () => {
      try {
        const status = await getLLMStatus();
        setLlmConfigured(status.is_configured);
        // Only admins can fix it, so only they see the promo
        if (!status.is_configured && user.role === 'admin') {
          setShowLlmPromo(true);
        }
      } catch (error) {
        console.error('Failed to check LLM status:', error);
        setLlmConfigured(false);
      }
    };
    checkLLMStatus();
  }, [user]);

  const handleLogout = () => {
    clearToken();
    setUser(null);
    navigate('/login', { replace: true });
  };

  if (!authChecked) {
    return null;
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage onLogin={setUser} />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <MainLayout
      user={user}
      onLogout={handleLogout}
      llmConfigured={llmConfigured}
      showLlmPromo={showLlmPromo}
      onCloseLLMPromo={() => setShowLlmPromo(false)}
    >
      <Routes>
        <Route path="/" element={<InvoiceListPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/invoices/:id" element={<InvoiceDetailPage />} />
        <Route
          path="/settings"
          element={user.role === 'admin' ? <SettingsPage currentUser={user} /> : <Navigate to="/" replace />}
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </MainLayout>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AppContent />
    </BrowserRouter>
  );
}

export default App;
