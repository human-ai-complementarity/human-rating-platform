import { useNavigate } from 'react-router-dom';

export default function LandingPage() {
  const navigate = useNavigate();

  return (
    <div className="login-page">
      <div
        className="login-card"
        style={{ maxWidth: 640, padding: '48px 40px' }}
      >
        <h1 style={{ fontSize: 26, lineHeight: 1.3, marginBottom: 10 }}>
          Run Human-AI rating studies without the platform getting in your way.
        </h1>
        <p className="login-subtitle" style={{ marginBottom: 28 }}>
          A free, open-source alternative to tools like Gorilla — built for AI safety
          researchers who need Prolific integration and flexible study design.
        </p>

        <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
          <button
            type="button"
            onClick={() => navigate('/admin')}
            className="login-btn login-btn-primary"
            style={{ width: 'auto', minWidth: 140 }}
          >
            Sign In
          </button>
          <a
            href="https://github.com/human-ai-complementarity/human-rating-platform/"
            target="_blank"
            rel="noopener noreferrer"
            className="login-btn"
            style={{
              width: 'auto',
              minWidth: 140,
              background: 'transparent',
              color: '#4a90d9',
              border: '1px solid #4a90d9',
              textDecoration: 'none',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            GitHub →
          </a>
        </div>

        <div
          style={{
            marginTop: 40,
            textAlign: 'left',
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          <p style={{ fontSize: 14, color: '#333', lineHeight: 1.5 }}>
            <strong>Experiment management</strong> — CSV upload, live monitoring, result
            exports.
          </p>
          <p style={{ fontSize: 14, color: '#333', lineHeight: 1.5 }}>
            <strong>Smart routing</strong> — questions served until each hits its target
            count.
          </p>
          <p style={{ fontSize: 14, color: '#333', lineHeight: 1.5 }}>
            <strong>Prolific-native</strong> — one-click studies with automatic completion
            codes.
          </p>
          <p style={{ fontSize: 14, color: '#333', lineHeight: 1.5 }}>
            <strong>Automated recruitment</strong> — use a small pilot study to estimate and recruit the correct number of participants
          </p>
        </div>

        <p
          style={{
            marginTop: 32,
            fontSize: 13,
            color: '#666',
            textAlign: 'center',
          }}
        >
          Prolific participant? Use the link the researcher sent you.
        </p>
      </div>
    </div>
  );
}
