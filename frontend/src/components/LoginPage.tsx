import React from 'react';
import { useLocation } from 'react-router-dom';
import { useSignIn, useSignUp } from '../auth';

type Stage = 'choose' | 'sign-in' | 'sign-up' | 'verify-email';

export default function LoginPage() {
  const location = useLocation();
  // After auth, redirect back to where the user was trying to go (default /admin)
  const redirectTo = location.pathname === '/' ? '/admin' : location.pathname;
  const { signIn, setActive, isLoaded } = useSignIn();
  const { signUp, setActive: setSignUpActive, isLoaded: signUpLoaded } = useSignUp();
  const [stage, setStage] = React.useState<Stage>('choose');
  const [email, setEmail] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [code, setCode] = React.useState('');
  const [error, setError] = React.useState('');
  const [loading, setLoading] = React.useState(false);

  if (!isLoaded || !signUpLoaded) {
    return (
      <div className="login-page">
        <div className="login-card">
          <p>Loading...</p>
        </div>
      </div>
    );
  }

  const handleGoogleSignIn = async () => {
    setError('');
    try {
      await signIn!.authenticateWithRedirect({
        strategy: 'oauth_google',
        redirectUrl: '/sso-callback',
        redirectUrlComplete: redirectTo,
      });
    } catch (err: any) {
      setError(err?.errors?.[0]?.longMessage || err?.message || 'Failed to start Google sign-in');
    }
  };

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setError('');
    setLoading(true);

    try {
      const result = await signIn!.create({
        identifier: email.trim(),
        password,
      });
      if (result.status === 'complete') {
        await setActive!({ session: result.createdSessionId });
        window.location.href = redirectTo;
      } else {
        setError('Sign-in incomplete. Please try again.');
      }
    } catch (err: any) {
      const code = err?.errors?.[0]?.code;
      if (code === 'form_identifier_not_found') {
        setError('No account with this email yet. Create an account, or continue with Google.');
      } else if (code === 'strategy_for_user_invalid') {
        setError('This account signs in with Google. Use "Continue with Google".');
      } else {
        setError('Invalid email or password.');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleSignUp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setError('');
    setLoading(true);

    try {
      const result = await signUp!.create({
        emailAddress: email.trim(),
        password,
      });
      if (result.status === 'complete') {
        await setSignUpActive!({ session: result.createdSessionId });
        window.location.href = redirectTo;
      } else if (result.status === 'missing_requirements') {
        await signUp!.prepareEmailAddressVerification({ strategy: 'email_code' });
        setStage('verify-email');
      } else {
        setError('Something went wrong. Please try again.');
      }
    } catch (err: any) {
      if (err?.errors?.[0]?.code === 'form_identifier_exists') {
        // A Google sign-in or an earlier sign-up already made this account.
        setPassword('');
        setStage('sign-in');
        setError(
          'An account with this email already exists. Sign in with your password if you set one, or continue with Google.'
        );
        return;
      }
      setError(err?.errors?.[0]?.longMessage || err?.message || 'Failed to create account');
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyEmail = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return;
    setError('');
    setLoading(true);

    try {
      const result = await signUp!.attemptEmailAddressVerification({
        code: code.trim(),
      });
      if (result.status === 'complete') {
        await setSignUpActive!({ session: result.createdSessionId });
        window.location.href = redirectTo;
      } else {
        setError('Verification incomplete. Please try again.');
      }
    } catch (err: any) {
      setError(err?.errors?.[0]?.longMessage || err?.message || 'Invalid code');
    } finally {
      setLoading(false);
    }
  };

  const goBack = () => {
    setStage('choose');
    setError('');
    setPassword('');
    setCode('');
  };

  // ── Verify email (sign-up) ──────────────────────────────────────────────
  if (stage === 'verify-email') {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>Verify your email</h1>
          <p className="login-subtitle">
            We sent a code to <strong>{email}</strong>
          </p>
          <form onSubmit={handleVerifyEmail}>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="Enter verification code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              className="login-input"
            />
            {error && <p className="login-error">{error}</p>}
            <button type="submit" disabled={loading || !code.trim()} className="login-btn login-btn-primary">
              {loading ? 'Verifying...' : 'Verify'}
            </button>
          </form>
          <button className="login-btn login-btn-link" onClick={goBack}>
            Back
          </button>
        </div>
      </div>
    );
  }

  // ── Sign up ─────────────────────────────────────────────────────────────
  if (stage === 'sign-up') {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>Create an account</h1>
          <p className="login-subtitle">Enter your email and choose a password</p>
          <form onSubmit={handleSignUp}>
            <input
              type="email"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              className="login-input"
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="login-input"
            />
            {error && <p className="login-error">{error}</p>}
            <button type="submit" disabled={loading || !email.trim() || !password} className="login-btn login-btn-primary">
              {loading ? 'Creating account...' : 'Sign up'}
            </button>
          </form>
          <button className="login-btn login-btn-link" onClick={goBack}>
            Back
          </button>
        </div>
      </div>
    );
  }

  // ── Sign in ─────────────────────────────────────────────────────────────
  if (stage === 'sign-in') {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>Sign in</h1>
          <p className="login-subtitle">Enter your email and password</p>
          <form onSubmit={handleSignIn}>
            <input
              type="email"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              className="login-input"
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="login-input"
            />
            {error && <p className="login-error">{error}</p>}
            <button type="submit" disabled={loading || !email.trim() || !password} className="login-btn login-btn-primary">
              {loading ? 'Signing in...' : 'Sign in'}
            </button>
          </form>
          <button className="login-btn login-btn-link" onClick={goBack}>
            Back
          </button>
        </div>
      </div>
    );
  }

  // ── Choose method ───────────────────────────────────────────────────────
  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Human Rating Platform</h1>
        <p className="login-subtitle">Sign in to access the admin dashboard</p>
        {error && <p className="login-error">{error}</p>}
        <div className="login-buttons">
          <button onClick={handleGoogleSignIn} className="login-btn login-btn-google">
            <svg width="18" height="18" viewBox="0 0 18 18" style={{ marginRight: 8, verticalAlign: 'middle' }}>
              <path d="M16.51 8H8.98v3h4.3c-.18 1-.74 1.48-1.6 2.04v2.01h2.6a7.8 7.8 0 0 0 2.38-5.88c0-.57-.05-.66-.15-1.18z" fill="#4285F4" />
              <path d="M8.98 17c2.16 0 3.97-.72 5.3-1.94l-2.6-2a4.8 4.8 0 0 1-7.18-2.54H1.83v2.07A8 8 0 0 0 8.98 17z" fill="#34A853" />
              <path d="M4.5 10.52a4.8 4.8 0 0 1 0-3.04V5.41H1.83a8 8 0 0 0 0 7.18l2.67-2.07z" fill="#FBBC05" />
              <path d="M8.98 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.59A8 8 0 0 0 1.83 5.41l2.67 2.07A4.77 4.77 0 0 1 8.98 3.58z" fill="#EA4335" />
            </svg>
            Continue with Google
          </button>
          <button onClick={() => setStage('sign-in')} className="login-btn login-btn-email">
            Sign in with Email
          </button>
          <button onClick={() => setStage('sign-up')} className="login-btn login-btn-email">
            Create an Account
          </button>
        </div>
      </div>
    </div>
  );
}
