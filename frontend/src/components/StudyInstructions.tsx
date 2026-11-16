import { Banner } from './experiment-detail/ui';
import { QUESTION_TIME_LIMIT_MINUTES } from './QuestionTimer';

export interface StudyInstructionsProps {
  // Pre-rendered HTML from the server (markdown converted via to_prolific_html).
  // Rendered with dangerouslySetInnerHTML — safe because the converter only
  // emits a whitelisted set of Prolific-allowed tags with all input escaped.
  descriptionHtml: string | null;
  assistanceInstructions: string | null;
  /** Minutes the rater gets, derived from the session the server issued. */
  sessionMinutes: number;
  /** Minutes past the deadline in which the open question can still be sent. */
  graceMinutes: number;
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
 * What the rater is told about the study: the session's time budget, the
 * researcher's description, and (for AI-assisted methods) how the assistance
 * works. Shown once on the intro screen, and again on demand from the rating
 * screen's Instructions dialog, so both always say the same thing.
 */
function StudyInstructions({
  descriptionHtml,
  assistanceInstructions,
  sessionMinutes,
  graceMinutes,
}: StudyInstructionsProps) {
  return (
    <>
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
          fontSize: 'var(--text-body)',
          lineHeight: 'var(--leading-body)',
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
        )}{' '}
        We strongly recommend spending no more than {QUESTION_TIME_LIMIT_MINUTES} minutes on any
        one question: a second timer counts down for the current one, and when it runs out
        please give your best answer and move on.
      </div>

      {descriptionHtml && (
        <div
          className="rater-intro-description rater-prose"
          dangerouslySetInnerHTML={{ __html: descriptionHtml }}
          style={{
            color: 'var(--ink)',
            marginBottom: 28,
          }}
        />
      )}

      {assistanceInstructions && (
        <div style={{ marginBottom: 28 }}>
          <div
            style={{
              font: '600 var(--text-label)/1 var(--font-mono)',
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
              marginBottom: 8,
            }}
          >
            How this study works
          </div>
          <Banner tone="info" icon={false}>
            <div style={{ whiteSpace: 'pre-wrap', color: 'var(--ink)', fontSize: 'var(--text-body)', lineHeight: 'var(--leading-body)' }}>
              {assistanceInstructions}
            </div>
          </Banner>
        </div>
      )}
    </>
  );
}

export default StudyInstructions;
