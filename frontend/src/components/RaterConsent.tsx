import { useState } from 'react';
import { primaryButton } from './experiment-detail/ui';

interface RaterConsentProps {
  experimentName: string;
  // Pre-rendered HTML from the server (markdown converted via to_prolific_html).
  // Rendered with dangerouslySetInnerHTML — safe because the converter only
  // emits a whitelisted set of Prolific-allowed tags with all input escaped.
  statementHtml: string;
  submitting: boolean;
  error: string | null;
  onAgree: () => void;
}

/**
 * The very first screen a rater sees: the study's informed-consent statement
 * with an explicit tick-box and an "I agree" button. Nothing about the study
 * (description, questions) is shown before this, and the backend refuses
 * questions until consent is recorded, so the screen is not skippable.
 */
function RaterConsent({
  experimentName,
  statementHtml,
  submitting,
  error,
  onAgree,
}: RaterConsentProps) {
  const [ticked, setTicked] = useState(false);

  return (
    <div
      data-testid="consent-screen"
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
        padding: '44px 48px 40px',
      }}
    >
      <div
        style={{
          font: '600 11px/1 var(--font-mono)',
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          color: 'var(--accent-soft-ink)',
        }}
      >
        Participant information
      </div>

      <h1
        style={{
          fontFamily: 'var(--font-head)',
          fontWeight: 600,
          fontSize: 28,
          lineHeight: 1.18,
          letterSpacing: '-0.012em',
          color: 'var(--ink)',
          margin: '14px 0 8px',
        }}
      >
        {experimentName}
      </h1>
      <p style={{ color: 'var(--muted)', fontSize: 14, margin: '0 0 24px' }}>
        Please read the following before you begin.
      </p>

      <div
        className="rater-consent-statement"
        dangerouslySetInnerHTML={{ __html: statementHtml }}
        style={{ fontSize: 15, lineHeight: 1.65, color: 'var(--ink)' }}
      />

      <label
        style={{
          display: 'flex',
          gap: 12,
          alignItems: 'flex-start',
          margin: '28px 0 18px',
          padding: '14px 16px',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius-sm)',
          fontSize: 14,
          lineHeight: 1.5,
          color: 'var(--ink)',
          cursor: 'pointer',
        }}
      >
        <input
          type="checkbox"
          checked={ticked}
          onChange={(e) => setTicked(e.target.checked)}
          disabled={submitting}
          style={{ marginTop: 3, width: 16, height: 16, flexShrink: 0 }}
        />
        <span>
          I have read and understood the information above, I am 18 or older, and I
          voluntarily agree to take part in this study.
        </span>
      </label>

      {error && (
        <p style={{ color: 'var(--danger, #b42318)', fontSize: 14, margin: '0 0 14px' }}>
          {error}
        </p>
      )}

      <button
        type="button"
        onClick={onAgree}
        disabled={!ticked || submitting}
        style={{
          ...primaryButton,
          width: '100%',
          padding: '13px 22px',
          fontSize: 15,
          opacity: !ticked || submitting ? 0.55 : 1,
          cursor: !ticked || submitting ? 'not-allowed' : 'pointer',
        }}
      >
        {submitting ? 'Saving…' : 'I agree'}
      </button>

      <p style={{ color: 'var(--muted)', fontSize: 13, margin: '16px 0 0', textAlign: 'center' }}>
        If you do not wish to take part, close this tab and return the study on Prolific.
      </p>
    </div>
  );
}

export default RaterConsent;
