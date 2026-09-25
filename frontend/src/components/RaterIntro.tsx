import { Banner, primaryButton } from './experiment-detail/ui';

interface RaterIntroProps {
  experimentName: string;
  // Pre-rendered HTML from the server (markdown converted via to_prolific_html).
  // Rendered with dangerouslySetInnerHTML — safe because the converter only
  // emits a whitelisted set of Prolific-allowed tags with all input escaped.
  descriptionHtml: string | null;
  assistanceInstructions: string | null;
  /** Minutes the rater gets, derived from the session the server issued. */
  sessionMinutes: number;
  /** Minutes past the deadline in which the open question can still be sent. */
  graceMinutes: number;
  onContinue: () => void;
}

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? '' : 's'}`;

/** "45 minutes" / "1 hour" / "2 hours 30 minutes" / "1 hour 1 minute".
 *  Reads predicatively — "You have ..." — so both halves are pluralised. */
function formatDuration(minutes: number): string {
  if (minutes < 60) return plural(minutes, 'minute');
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  const hourPart = plural(hours, 'hour');
  return rest === 0 ? hourPart : `${hourPart} ${plural(rest, 'minute')}`;
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
  sessionMinutes,
  graceMinutes,
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

      {/* Set expectations before the rater commits. A return costs us a place
          and delays the round, so someone who would rather not spend this long
          is better off knowing now than discovering it twenty minutes in. */}
      <div
        data-testid="session-expectations"
        style={{
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius-sm)',
          padding: '14px 16px',
          marginBottom: 24,
          fontSize: 14,
          lineHeight: 1.6,
          color: 'var(--ink)',
        }}
      >
        <strong>You have {formatDuration(sessionMinutes)}.</strong> Rate as many questions as
        you can in that time, and finish early if you want to.{' '}
        {graceMinutes > 0 ? (
          <>
            When the time is up you get {graceMinutes} more{' '}
            {graceMinutes === 1 ? 'minute' : 'minutes'} to send the question you are on, and
            everything you have already submitted is kept.
          </>
        ) : (
          <>Everything you submit is kept.</>
        )}
      </div>

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
