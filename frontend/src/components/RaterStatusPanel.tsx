import { useState, type ReactNode } from 'react';

import { QUESTION_TIME_LIMIT_MINUTES } from './QuestionTimer';

interface RaterStatusPanelProps {
  /** Minutes past the session deadline in which the open question can still be sent. */
  graceMinutes: number;
  /** The readout rows (TimerChip). */
  children: ReactNode;
  /** Adds an Instructions button floating just below the clocks. Lives here
   *  rather than in the page header because this panel floats over the
   *  header's right end. */
  onOpenInstructions?: () => void;
}

/** The floating readout in the corner of the rater view. Hovering or focusing
 *  the clocks explains what each one means, so the rows themselves can stay terse. */
function RaterStatusPanel({ graceMinutes, children, onOpenInstructions }: RaterStatusPanelProps) {
  const [helpOpen, setHelpOpen] = useState(false);

  const styles = {
    // Holds the clocks and, separately below them, the Instructions button.
    dock: {
      position: 'fixed' as const,
      top: 20,
      right: 20,
      zIndex: 30,
      display: 'flex',
      flexDirection: 'column' as const,
      alignItems: 'flex-end',
      gap: 8,
    },
    panel: {
      // Above the Instructions button, so the clock help can cover it.
      position: 'relative' as const,
      zIndex: 1,
      // Width, background and shadow come from .rater-status-panel (index.css),
      // which owns the small see-through rest state and the solid expanded one.
      color: '#fff',
      padding: 5,
      borderRadius: 'var(--radius-sm)',
      fontFamily: 'var(--font-mono)',
      display: 'flex',
      flexDirection: 'column' as const,
      gap: 2,
    },
    // Not positioned, so the help still anchors to the whole panel.
    readout: {
      display: 'flex',
      flexDirection: 'column' as const,
      gap: 2,
      cursor: 'help',
      borderRadius: 'calc(var(--radius-sm) - 2px)',
    },
    instructions: {
      // Background and hover come from .rater-instructions-button (index.css).
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      minHeight: 32,
      padding: '5px 12px',
      border: 'none',
      borderRadius: 'var(--radius-sm)',
      color: '#fff',
      fontFamily: 'var(--font-mono)',
      fontSize: 'var(--text-label)',
      fontWeight: 600,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.08em',
      cursor: 'pointer',
    },
    help: {
      position: 'absolute' as const,
      top: 'calc(100% + 8px)',
      right: 0,
      width: 320,
      maxWidth: 'calc(100vw - 40px)',
      background: 'var(--surface)',
      color: 'var(--ink)',
      border: '1px solid var(--faint)',
      borderRadius: 'var(--radius-sm)',
      boxShadow: '0 10px 28px rgba(40, 36, 32, 0.22)',
      padding: '14px 16px',
      fontFamily: 'var(--font-body)',
      fontSize: 'var(--text-secondary)',
      lineHeight: 1.5,
      // Explanatory only, and it must not hold the hover open over the page.
      pointerEvents: 'none' as const,
    },
    term: {
      fontFamily: 'var(--font-mono)',
      fontSize: 'var(--text-label)',
      fontWeight: 700,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.08em',
      color: 'var(--muted)',
      marginBottom: 2,
    },
    detail: { margin: '0 0 12px' },
    lastDetail: { margin: 0 },
  };

  return (
    <div style={styles.dock}>
      <div className="rater-status-panel" style={styles.panel} data-testid="rater-status-panel">
        <div
          className="rsp-readout"
          style={styles.readout}
          tabIndex={0}
          aria-describedby={helpOpen ? 'rater-status-help' : undefined}
          onMouseEnter={() => setHelpOpen(true)}
          onMouseLeave={() => setHelpOpen(false)}
          onFocus={() => setHelpOpen(true)}
          onBlur={() => setHelpOpen(false)}
          data-testid="rater-status-readout"
        >
          {children}
        </div>
        {helpOpen && (
          <div id="rater-status-help" role="tooltip" style={styles.help} data-testid="rater-status-help">
            <div style={styles.term}>Session</div>
            <p style={styles.detail}>
              The time left in your session. When it reaches zero no new questions are served
              {graceMinutes > 0 ? (
                <>
                  , and you get {graceMinutes} more {graceMinutes === 1 ? 'minute' : 'minutes'} to
                  send the question you are on.
                </>
              ) : (
                <>.</>
              )}
            </p>
            <div style={styles.term}>Current question</div>
            <p style={styles.detail}>
              We strongly recommend spending <strong>no more than {QUESTION_TIME_LIMIT_MINUTES} minutes</strong>{' '}
              on any one question. This clock counts down from {QUESTION_TIME_LIMIT_MINUTES}:00 and
              restarts with each new question.
            </p>
            <p style={styles.lastDetail}>
              When it runs out it turns red and counts the extra time. Nothing is cut off and your
              answer is still accepted, but please give your best answer and move on.
            </p>
          </div>
        )}
      </div>
      {/* Outside the panel, so pointing at it leaves the clocks collapsed and
          the button where the pointer found it. */}
      {onOpenInstructions && (
        <button
          type="button"
          className="rater-instructions-button"
          style={styles.instructions}
          aria-haspopup="dialog"
          onClick={(e) => {
            // Safari and Firefox on macOS don't focus a clicked button; focus
            // it so the dialog can hand focus back here on close.
            e.currentTarget.focus();
            onOpenInstructions();
          }}
          data-testid="instructions-button"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 11v5M12 8h.01" />
          </svg>
          Instructions
        </button>
      )}
    </div>
  );
}

export default RaterStatusPanel;
