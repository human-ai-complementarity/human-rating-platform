import { Banner, primaryButton } from './experiment-detail/ui';

interface RaterIntroProps {
  experimentName: string;
  // Pre-rendered HTML from the server (markdown converted via to_prolific_html).
  // Rendered with dangerouslySetInnerHTML — safe because the converter only
  // emits a whitelisted set of Prolific-allowed tags with all input escaped.
  descriptionHtml: string | null;
  assistanceInstructions: string | null;
  onContinue: () => void;
}

/**
 * The rater's first screen after landing from Prolific — an editorial
 * splash with the study name, description, and (when applicable) an
 * "How this study works" callout for AI-assisted rating methods.
 *
 * Serif headline, generous whitespace, single primary CTA. Matches the
 * Fieldbook aesthetic used across the researcher-facing surfaces so the
 * study feels part of a coherent product rather than a bolt-on form.
 */
function RaterIntro({
  experimentName,
  descriptionHtml,
  assistanceInstructions,
  onContinue,
}: RaterIntroProps) {
  return (
    <div
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
        Rating study
      </div>

      <h1
        style={{
          fontFamily: 'var(--font-head)',
          fontWeight: 600,
          fontSize: 32,
          lineHeight: 1.18,
          letterSpacing: '-0.012em',
          color: 'var(--ink)',
          margin: '14px 0 24px',
        }}
      >
        {experimentName}
      </h1>

      {descriptionHtml && (
        <div
          className="rater-intro-description"
          dangerouslySetInnerHTML={{ __html: descriptionHtml }}
          style={{
            fontSize: 16,
            lineHeight: 1.65,
            color: 'var(--ink)',
            marginBottom: 28,
          }}
        />
      )}

      {assistanceInstructions && (
        <div style={{ marginBottom: 28 }}>
          <div
            style={{
              font: '600 10px/1 var(--font-mono)',
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
              marginBottom: 8,
            }}
          >
            How this study works
          </div>
          <Banner tone="info" icon={false}>
            <div style={{ whiteSpace: 'pre-wrap', color: 'var(--ink)' }}>
              {assistanceInstructions}
            </div>
          </Banner>
        </div>
      )}

      <button
        type="button"
        onClick={onContinue}
        style={{ ...primaryButton, width: '100%', padding: '13px 22px', fontSize: 15 }}
      >
        Continue
      </button>
    </div>
  );
}

export default RaterIntro;
