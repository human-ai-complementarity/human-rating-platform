import React from 'react';
import { AuthenticateWithRedirectCallback } from '@clerk/clerk-react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import RaterView from './components/RaterView';
import AdminView from './components/AdminView';
import AdminDocs from './components/AdminDocs';
import ExperimentDetailPage from './components/ExperimentDetailPage';
import LoginPage from './components/LoginPage';
import LandingPage from './components/LandingPage';
import { api } from './api';
import {
  isE2eAuthBypassed,
  SignedIn,
  SignedOut,
  useAuth,
  useUser,
  UserButton,
} from './auth';

function App() {
  return (
    <Routes>
      <Route path="/sso-callback" element={<AuthenticateWithRedirectCallback />} />
      <Route path="/rate" element={<RaterView />} />
      <Route
        path="/"
        element={
          <>
            <SignedIn>
              <Navigate to="/admin" replace />
            </SignedIn>
            <SignedOut>
              <LandingPage />
            </SignedOut>
          </>
        }
      />
      <Route
        path="/admin"
        element={
          <>
            <SignedIn>
              <AdminPage />
            </SignedIn>
            <SignedOut>
              <LoginPage />
            </SignedOut>
          </>
        }
      />
      <Route
        path="/admin/docs"
        element={
          <>
            <SignedIn>
              <AdminPage>
                <AdminDocs />
              </AdminPage>
            </SignedIn>
            <SignedOut>
              <LoginPage />
            </SignedOut>
          </>
        }
      />
      <Route
        path="/admin/experiments/:experimentId"
        element={
          <>
            <SignedIn>
              <AdminPage>
                <ExperimentDetailPage />
              </AdminPage>
            </SignedIn>
            <SignedOut>
              <LoginPage />
            </SignedOut>
          </>
        }
      />
    </Routes>
  );
}

const ADMIN_JWT_TEMPLATE = (import.meta.env.VITE_CLERK_JWT_TEMPLATE as string | undefined) || 'admin';

function AdminPage({ children }: { children?: React.ReactNode }) {
  const { isLoaded, isSignedIn, user } = useUser();
  const { getToken } = useAuth();
  const [state, setState] = React.useState<'idle' | 'loading' | 'ok' | 'forbidden' | 'error'>('idle');
  const [message, setMessage] = React.useState<string>('');
  const email = user?.primaryEmailAddress?.emailAddress || user?.emailAddresses?.[0]?.emailAddress;

  React.useEffect(() => {
    if (!isLoaded) return;
    if (!isSignedIn) return;
    if (!email) return;
    if (isE2eAuthBypassed()) {
      setState('ok');
      return;
    }

    let cancelled = false;

    (async () => {
      setState('loading');
      try {
        const token = await getToken({ template: ADMIN_JWT_TEMPLATE });
        if (!token) {
          throw new Error('Missing Clerk session token');
        }
        const resp = await api.adminLogin(token);
        if (cancelled) return;
        if ((resp as any).ok === true) {
          setState('ok');
        } else {
          const msg = (resp as any)?.message || 'Access denied';
          if (msg.toLowerCase().includes('forbidden') || msg.toLowerCase().includes('allowlist')) {
            setState('forbidden');
            setMessage(msg);
          } else {
            setState('ok');
          }
        }
      } catch (err: any) {
        if (cancelled) return;
        const msg = err?.message || 'Failed to create admin session';
        if (msg.includes('403')) {
          setState('forbidden');
          setMessage('You are not allowed to access the admin panel.');
        } else {
          setState('error');
          setMessage(msg);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [ADMIN_JWT_TEMPLATE, isLoaded, isSignedIn, email, getToken]);

  if (!isLoaded || state === 'loading' || state === 'idle') {
    return (
      <AdminShell>
        <InfoCard title="Preparing admin session…" />
      </AdminShell>
    );
  }

  if (state === 'forbidden') {
    return (
      <AdminShell>
        <InfoCard
          title="You don't have admin access."
          body="Please contact Juliana, Andrew, or Sander to have your email added to the allowlist."
        />
      </AdminShell>
    );
  }

  if (state === 'error') {
    return (
      <AdminShell>
        <InfoCard title="Error preparing admin session." body={message} />
      </AdminShell>
    );
  }

  return <AdminShell>{children ?? <AdminView />}</AdminShell>;
}

function AdminShell({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // Highlight the top-nav item that owns this route. `/admin/docs` is its own
  // section; everything else under `/admin` (list + experiment detail) belongs
  // to the "Experiments" tab.
  const isDocs = pathname.startsWith('/admin/docs');
  const isExperiments = pathname.startsWith('/admin') && !isDocs;

  return (
    <div style={{ minHeight: '100vh', background: 'var(--page-bg)' }}>
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 11,
          padding: '14px 30px',
          background: 'var(--surface)',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <button
          type="button"
          onClick={() => navigate('/admin')}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 11,
            background: 'none',
            border: 'none',
            padding: 0,
            cursor: 'pointer',
            color: 'var(--ink)',
          }}
        >
          <span
            aria-hidden
            style={{
              width: 15,
              height: 15,
              background: 'var(--accent)',
              transform: 'rotate(45deg)',
              borderRadius: 3,
            }}
          />
          <span style={{ font: '600 16px/1 var(--font-head)' }}>Complementarities Platform</span>
        </button>
        <NavTab active={isExperiments} onClick={() => navigate('/admin')} label="Experiments" />
        <NavTab active={isDocs} onClick={() => navigate('/admin/docs')} label="Documentation" />
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center' }}>
          <UserButton afterSignOutUrl="/" />
        </div>
      </header>
      <SignedOut>
        <BackendLogoutOnSignedOut />
      </SignedOut>
      {children}
    </div>
  );
}

function NavTab({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        position: 'relative',
        background: 'none',
        border: 'none',
        padding: '6px 2px',
        marginLeft: 22,
        fontSize: 14,
        fontFamily: 'var(--font-body)',
        fontWeight: active ? 600 : 400,
        color: active ? 'var(--ink)' : 'var(--muted)',
        cursor: 'pointer',
        lineHeight: 1,
      }}
    >
      {label}
      {active && (
        <span
          aria-hidden
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            // Header has `padding: '14px 30px'` and a 1px bottom border;
            // -15 lands the accent line flush on top of that border so the
            // underline "hangs" from the tab down to the header edge.
            bottom: -15,
            height: 2,
            background: 'var(--accent)',
          }}
        />
      )}
    </button>
  );
}

function BackendLogoutOnSignedOut() {
  React.useEffect(() => {
    if (isE2eAuthBypassed()) {
      return;
    }
    void api.adminLogout().catch(() => {});
  }, []);
  return null;
}

type InfoCardProps = {
  title: string;
  body?: string;
  align?: React.CSSProperties['textAlign'];
};

function InfoCard({ title, body, align = 'center' }: InfoCardProps) {
  return (
    <div style={{ maxWidth: 800, margin: '0 auto', padding: '40px 24px' }}>
      <div
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          padding: '32px 28px',
          boxShadow: 'var(--shadow)',
          textAlign: align,
        }}
      >
        <p
          style={{
            fontFamily: 'var(--font-head)',
            fontSize: 20,
            fontWeight: 600,
            marginBottom: body ? 8 : 0,
          }}
        >
          {title}
        </p>
        {body && <p style={{ color: 'var(--muted)', margin: 0, fontSize: 14 }}>{body}</p>}
      </div>
    </div>
  );
}
export default App;
