import { primaryButton } from './experiment-detail/ui';

interface RaterDebriefProps {
  // Pre-rendered HTML from the server (the bundle's debrief statement with
  // placeholders filled). Same trust argument as the consent statement: the
  // converter only emits a whitelisted set of tags with all input escaped.
  statementHtml: string;
  completionUrl: string | null;
  questionsCompleted: number;
}

/**
 * The completion screen for studies with a content warning. Prolific requires
 * a debrief before the completion code is issued, so unlike the ordinary
 * completion card there is no automatic redirect: the rater reads, then
 * presses through to Prolific themselves.
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
        Session complete
      </div>
      <p style={{ color: 'var(--muted)', fontSize: 14, margin: '12px 0 24px' }}>
        You answered {questionsCompleted} {questionsCompleted === 1 ? 'question' : 'questions'}.
        Everything you submitted is saved.
      </p>

      <div
        className="rater-consent-statement"
        dangerouslySetInnerHTML={{ __html: statementHtml }}
        style={{ fontSize: 15, lineHeight: 1.65, color: 'var(--ink)' }}
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
            fontSize: 15,
            marginTop: 28,
            textAlign: 'center',
            textDecoration: 'none',
          }}
        >
          Continue to Prolific
        </a>
      ) : (
        <p style={{ color: 'var(--muted)', fontSize: 14, margin: '28px 0 0', textAlign: 'center' }}>
          Thank you for your participation. You may now close this window.
        </p>
      )}
    </div>
  );
}

export default RaterDebrief;
