import { useState } from 'react';
import { primaryButton, secondaryButton, textareaStyle } from './experiment-detail/ui';

// Mirrors RATER_FEEDBACK_MAX_LENGTH in backend/schemas.py.
export const FEEDBACK_MAX_LENGTH = 5000;

interface RaterFeedbackProps {
  submitting: boolean;
  // The study has a Prolific completion link waiting behind this screen.
  returnsToProlific: boolean;
  onSubmit: (feedback: string) => void;
  onSkip: () => void;
}

/**
 * Shown once the session is over, before the completion card (or the
 * deception debrief). Entirely optional: skipping goes straight on, and the
 * parent moves on even when saving fails, so this can never stand between a
 * rater and their completion code.
 */
function RaterFeedback({ submitting, returnsToProlific, onSubmit, onSkip }: RaterFeedbackProps) {
  const [feedback, setFeedback] = useState('');
  const empty = feedback.trim() === '';

  return (
    <div
      data-testid="feedback-screen"
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
        Optional
      </div>

      <h1
        style={{
          fontFamily: 'var(--font-head)',
          fontWeight: 600,
          fontSize: 'var(--text-title)',
          lineHeight: 'var(--leading-heading)',
          letterSpacing: '-0.012em',
          color: 'var(--ink)',
          margin: '14px 0 8px',
        }}
      >
        Any feedback for us?
      </h1>
      <label
        htmlFor="rater-feedback"
        style={{
          display: 'block',
          color: 'var(--muted)',
          fontSize: 'var(--text-secondary)',
          lineHeight: 'var(--leading-body)',
          margin: '0 0 18px',
        }}
      >
        Your answers are saved. If anything was unclear, broken or frustrating, tell us here.
        Please do not include any personal information. You can skip this step.
      </label>

      <textarea
        id="rater-feedback"
        data-testid="feedback-input"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        maxLength={FEEDBACK_MAX_LENGTH}
        rows={9}
        disabled={submitting}
        style={{ ...textareaStyle, width: '100%', boxSizing: 'border-box', minHeight: 200 }}
      />

      <div style={{ display: 'flex', gap: 12, marginTop: 20, flexWrap: 'wrap' }}>
        <button
          type="button"
          data-testid="feedback-submit"
          onClick={() => onSubmit(feedback)}
          disabled={empty || submitting}
          style={{
            ...primaryButton,
            flex: '1 1 200px',
            padding: '13px 22px',
            fontSize: 'var(--text-body)',
            opacity: empty || submitting ? 0.55 : 1,
            cursor: empty || submitting ? 'not-allowed' : 'pointer',
          }}
        >
          {submitting ? 'Sending…' : 'Send feedback'}
        </button>
        <button
          type="button"
          data-testid="feedback-skip"
          onClick={onSkip}
          disabled={submitting}
          style={{
            ...secondaryButton,
            flex: '0 1 140px',
            padding: '13px 22px',
            fontSize: 'var(--text-body)',
          }}
        >
          Skip
        </button>
      </div>

      {returnsToProlific && (
        // The completion code is behind this screen, so a rater who reads
        // "optional" and closes the tab would never register on Prolific.
        <p
          data-testid="feedback-prolific-note"
          style={{ color: 'var(--muted)', fontSize: 'var(--text-fine)', margin: '16px 0 0', textAlign: 'center' }}
        >
          Please do not close this tab yet. Either button takes you on to Prolific to register
          your completion.
        </p>
      )}
    </div>
  );
}

export default RaterFeedback;
