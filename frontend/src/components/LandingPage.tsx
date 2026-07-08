import { useNavigate } from 'react-router-dom';

/**
 * Unauthenticated landing page. Serves two audiences at once — researchers
 * arrive here first, participants land here only if they follow a stale link.
 * The footer line ("Prolific participant?") intentionally sits below the
 * feature grid so researchers get the pitch first.
 */
export default function LandingPage() {
  const navigate = useNavigate();

  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'var(--bg)',
        display: 'flex',
        flexDirection: 'column',
        color: 'var(--ink)',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 11,
          padding: '20px 32px',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <span
          aria-hidden
          style={{
            width: 16,
            height: 16,
            background: 'var(--accent)',
            transform: 'rotate(45deg)',
            borderRadius: 3,
          }}
        />
        <span style={{ font: '600 17px/1 var(--font-head)' }}>Complementarities Platform</span>
      </header>

      <main
        style={{
          flex: 1,
          padding: '72px 32px 64px',
          display: 'flex',
          justifyContent: 'center',
        }}
      >
        <div style={{ maxWidth: 640, width: '100%', textAlign: 'center' }}>
          <div
            style={{
              font: '600 11px/1 var(--font-mono)',
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              color: 'var(--accent-soft-ink)',
            }}
          >
            For AI-safety researchers
          </div>
          <h1
            style={{
              fontFamily: 'var(--font-head)',
              fontWeight: 600,
              fontSize: 41,
              lineHeight: 1.12,
              letterSpacing: '-0.015em',
              margin: '16px 0 0',
            }}
          >
            Run Human–AI rating studies without the platform getting in your way.
          </h1>
          <p
            style={{
              fontSize: 17,
              lineHeight: 1.6,
              color: 'var(--muted)',
              maxWidth: 520,
              margin: '20px auto 0',
            }}
          >
            A free, open-source alternative to tools like Gorilla — built for AI-safety
            researchers who need Prolific integration and flexible study design.
          </p>

          <div style={{ display: 'flex', gap: 12, justifyContent: 'center', marginTop: 32 }}>
            <button
              type="button"
              onClick={() => navigate('/admin')}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                padding: '12px 26px',
                background: 'var(--accent)',
                color: 'var(--accent-ink)',
                border: 'none',
                borderRadius: 'var(--radius-sm)',
                fontWeight: 600,
                fontSize: 15,
              }}
            >
              Sign in
            </button>
            <a
              href="https://github.com/human-ai-complementarity/human-rating-platform/"
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                padding: '12px 24px',
                background: 'var(--surface)',
                color: 'var(--ink)',
                border: '1px solid var(--faint)',
                borderRadius: 'var(--radius-sm)',
                fontWeight: 600,
                fontSize: 15,
                textDecoration: 'none',
              }}
            >
              GitHub <span style={{ color: 'var(--muted)' }}>↗</span>
            </a>
          </div>

          <div
            style={{
              marginTop: 48,
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 1,
              background: 'var(--faint)',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius)',
              overflow: 'hidden',
              textAlign: 'left',
            }}
          >
            <FeatureCell
              title="Experiment management"
              body="CSV & Parquet upload, live monitoring, result exports."
            />
            <FeatureCell
              title="Smart routing"
              body="Questions served until each hits its target count."
            />
            <FeatureCell
              title="Prolific-native"
              body="One-click studies with automatic completion codes."
            />
            <FeatureCell
              title="Automated recruitment"
              body="Pilot-based estimation to recruit the right number of participants."
            />
          </div>

          <p style={{ margin: '32px 0 0', fontSize: 13, color: 'var(--muted)' }}>
            Prolific participant? Use the link the researcher sent you.
          </p>
        </div>
      </main>
    </div>
  );
}

function FeatureCell({ title, body }: { title: string; body: string }) {
  return (
    <div style={{ background: 'var(--surface)', padding: '22px 24px' }}>
      <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 5 }}>{title}</div>
      <div style={{ fontSize: 14, lineHeight: 1.55, color: 'var(--muted)' }}>{body}</div>
    </div>
  );
}
