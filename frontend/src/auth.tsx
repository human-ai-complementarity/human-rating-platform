import React from 'react';
import {
  ClerkProvider,
  SignInButton as ClerkSignInButton,
  SignUpButton as ClerkSignUpButton,
  SignedIn as ClerkSignedIn,
  SignedOut as ClerkSignedOut,
  UserButton as ClerkUserButton,
  useAuth as useClerkAuth,
  useSignIn as useClerkSignIn,
  useSignUp as useClerkSignUp,
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

// ── Sign-in and sign-up (the login page) ─────────────────────────────────
// The slice of Clerk's sign-in and sign-up hooks the login page uses. In e2e
// mode the fakes below script Clerk's responses by address so the page's
// error handling can be driven without Clerk loaded.

type AuthAttempt = { status: string | null; createdSessionId: string | null };

export type SignInHandle = {
  isLoaded: boolean;
  signIn?: {
    create(params: { identifier: string; password: string }): Promise<AuthAttempt>;
    authenticateWithRedirect(params: {
      strategy: 'oauth_google';
      redirectUrl: string;
      redirectUrlComplete: string;
    }): Promise<void>;
  };
  setActive?: (params: { session: string | null }) => Promise<void>;
};

export type SignUpHandle = {
  isLoaded: boolean;
  signUp?: {
    create(params: { emailAddress: string; password: string }): Promise<AuthAttempt>;
    prepareEmailAddressVerification(params: { strategy: 'email_code' }): Promise<unknown>;
    attemptEmailAddressVerification(params: { code: string }): Promise<AuthAttempt>;
  };
  setActive?: (params: { session: string | null }) => Promise<void>;
};

// Addresses the e2e fakes recognise; frontend/e2e/login.spec.ts uses the same ones.
const E2E_UNKNOWN_EMAIL = 'unknown@example.com';
const E2E_GOOGLE_ONLY_EMAIL = 'google-only@example.com';
const E2E_EXISTING_EMAIL = 'existing@example.com';

function clerkError(code: string) {
  return { errors: [{ code, longMessage: code }] };
}

const fakeSignIn: SignInHandle = {
  isLoaded: true,
  signIn: {
    create: async ({ identifier }) => {
      if (identifier === E2E_UNKNOWN_EMAIL) throw clerkError('form_identifier_not_found');
      if (identifier === E2E_GOOGLE_ONLY_EMAIL) throw clerkError('strategy_for_user_invalid');
      throw clerkError('form_password_incorrect');
    },
    authenticateWithRedirect: async () => {},
  },
  setActive: async () => {},
};

const fakeSignUp: SignUpHandle = {
  isLoaded: true,
  signUp: {
    create: async ({ emailAddress }) => {
      if (emailAddress === E2E_EXISTING_EMAIL) throw clerkError('form_identifier_exists');
      return { status: 'missing_requirements', createdSessionId: null };
    },
    prepareEmailAddressVerification: async () => {},
    attemptEmailAddressVerification: async () => ({
      status: 'complete',
      createdSessionId: 'e2e-session',
    }),
  },
  setActive: async () => {},
};

export function useSignIn(): SignInHandle {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return fakeSignIn;
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useClerkSignIn();
}

export function useSignUp(): SignUpHandle {
  if (E2E_AUTH_BYPASS_ENABLED) {
    return fakeSignUp;
  }
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useClerkSignUp();
}
