import { primaryButton } from './experiment-detail/ui';

interface RaterDebriefProps {
  // Pre-rendered HTML from the server (the debrief statement with
  // placeholders filled). Same trust argument as the consent statement: the
  // converter only emits a whitelisted set of tags with all input escaped.
  statementHtml: string;
  completionUrl: string | null;
  questionsCompleted: number;
}

/**
 * The completion screen for a deception study. Prolific requires a debrief
 * explaining what was withheld before the completion code is issued, so
 * unlike the ordinary completion card there is no automatic redirect: the
 * rater reads, then presses through to Prolific themselves. Which flags
 * get a debrief is tabled in backend/services/terms/flags.py.
 */
function RaterDebrief({ statementHtml, completionUrl, questionsCompleted }: RaterDebriefProps) {
  return (
    <div
      data-testid="debrief-screen"
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
        padding: 'clamp(24px, 6vw, 44px) var(--rater-card-pad) clamp(24px, 6vw, 40px)',
      }}
    >
      <div
        style={{
          font: '600 var(--text-label)/1 var(--font-mono)',
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          color: 'var(--accent-soft-ink)',
        }}
      >
        Session complete
      </div>
      <p style={{ color: 'var(--muted)', fontSize: 'var(--text-secondary)', margin: '12px 0 24px' }}>
        You answered {questionsCompleted} {questionsCompleted === 1 ? 'question' : 'questions'}.
        Everything you submitted is saved.
      </p>

      <div
        className="rater-consent-statement rater-prose"
        dangerouslySetInnerHTML={{ __html: statementHtml }}
        style={{ color: 'var(--ink)' }}
      />

      {completionUrl ? (
        <a
          href={completionUrl}
          data-testid="debrief-continue"
          style={{
            ...primaryButton,
            display: 'block',
            width: '100%',
            boxSizing: 'border-box',
            padding: '13px 22px',
            fontSize: 'var(--text-body)',
            marginTop: 28,
            textAlign: 'center',
            textDecoration: 'none',
          }}
        >
          Continue to Prolific
        </a>
      ) : (
        <p style={{ color: 'var(--muted)', fontSize: 'var(--text-secondary)', margin: '28px 0 0', textAlign: 'center' }}>
          Thank you for your participation. You may now close this window.
        </p>
      )}
    </div>
  );
}

export default RaterDebrief;
