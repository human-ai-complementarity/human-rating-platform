import React from 'react';
import { AuthenticateWithRedirectCallback } from '@clerk/clerk-react';
import { Routes, Route, Navigate } from 'react-router-dom';
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
  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', padding: 12 }}>
        <UserButton afterSignOutUrl="/" />
      </div>
      <SignedOut>
        <BackendLogoutOnSignedOut />
      </SignedOut>
      {children}
    </>
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
    <div className="container">
      <div className="card" style={{ textAlign: align }}>
        <p style={{ fontSize: 20, marginBottom: body ? 8 : 0 }}>{title}</p>
        {body && (
          <p style={{ color: '#666', margin: 0 }}>
            {body}
          </p>
        )}
      </div>
    </div>
  );
}
export default App;
