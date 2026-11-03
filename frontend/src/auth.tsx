import React from 'react';
import {
  ClerkProvider,
  SignInButton as ClerkSignInButton,
  SignUpButton as ClerkSignUpButton,
  SignedIn as ClerkSignedIn,
  SignedOut as ClerkSignedOut,
  UserButton as ClerkUserButton,
  useAuth as useClerkAuth,
  useClerk,
  useUser as useClerkUser,
} from '@clerk/clerk-react';

const E2E_AUTH_BYPASS_ENABLED =
  import.meta.env.VITE_E2E_BYPASS_AUTH === 'true' && !import.meta.env.PROD;
const E2E_AUTH_EMAIL = 'e2e-admin@example.com';

type AuthProviderProps = {
  children: React.ReactNode;
};

type FakeEmailAddress = {
  emailAddress: string;
};

type FakeUser = {
  primaryEmailAddress: FakeEmailAddress;
  emailAddresses: FakeEmailAddress[];
};

const fakeUser: FakeUser = {
  primaryEmailAddress: { emailAddress: E2E_AUTH_EMAIL },
  emailAddresses: [{ emailAddress: E2E_AUTH_EMAIL }],
};

type FakeAuthState = {
  isLoaded: true;
  isSignedIn: true;
  user: FakeUser;
};

type FakeUseAuth = {
  getToken: (_options?: unknown) => Promise<string>;
};

export function isE2eAuthBypassed(): boolean {
  return E2E_AUTH_BYPASS_ENABLED;
}

export function AppAuthProvider({ children }: AuthProviderProps) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return <>{children}</>;
  }

  const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;
  if (!publishableKey) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 24,
          backgroundColor: '#f5f5f5',
        }}
      >
        <div
          style={{
            maxWidth: 640,
            background: '#fff',
            borderRadius: 8,
            padding: 24,
            boxShadow: '0 2px 4px rgba(0, 0, 0, 0.1)',
          }}
        >
          <h1 style={{ marginTop: 0, marginBottom: 12 }}>Missing Clerk configuration</h1>
          <p style={{ margin: 0, color: '#444' }}>
            Set <code>VITE_CLERK_PUBLISHABLE_KEY</code> in <code>frontend/.env.local</code> and
            restart the Vite dev server.
          </p>
        </div>
      </div>
    );
  }

  return (
    <ClerkProvider publishableKey={publishableKey} afterSignOutUrl="/">
      {children}
    </ClerkProvider>
  );
}

export function SignedIn({ children }: { children?: React.ReactNode }) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return <>{children}</>;
  }
  return <ClerkSignedIn>{children}</ClerkSignedIn>;
}

export function SignedOut({ children }: { children?: React.ReactNode }) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return null;
  }
  return <ClerkSignedOut>{children}</ClerkSignedOut>;
}

export function SignInButton({ children }: { children?: React.ReactNode }) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return <button type="button">{children ?? 'Sign In'}</button>;
  }
  return <ClerkSignInButton>{children}</ClerkSignInButton>;
}

export function SignUpButton({ children }: { children?: React.ReactNode }) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return <button type="button">{children ?? 'Sign Up'}</button>;
  }
  return <ClerkSignUpButton>{children}</ClerkSignUpButton>;
}

export function UserButton({ afterSignOutUrl }: { afterSignOutUrl?: string }) {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return (
      <span
        aria-label="Test admin session"
        title={afterSignOutUrl ? `After sign out: ${afterSignOutUrl}` : undefined}
      >
        Test Admin
      </span>
    );
  }
  return <ClerkUserButton afterSignOutUrl={afterSignOutUrl} />;
}

export function useUser(): FakeAuthState | ReturnType<typeof useClerkUser> {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return {
      isLoaded: true,
      isSignedIn: true,
      user: fakeUser,
    };
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useClerkUser();
}

export function useAuth(): FakeUseAuth | ReturnType<typeof useClerkAuth> {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return {
      getToken: async () => 'e2e-bypass-token',
    };
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useClerkAuth();
}

/**
 * Returns a function that ends the Clerk session and sends the browser to
 * `redirectUrl`. Under the e2e auth bypass there is no Clerk session to end,
 * so it only navigates.
 */
export function useSignOut(): (redirectUrl?: string) => Promise<void> {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return async (redirectUrl = '/') => {
      window.location.assign(redirectUrl);
    };
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const { signOut } = useClerk();
  return (redirectUrl = '/') => signOut({ redirectUrl });
}
