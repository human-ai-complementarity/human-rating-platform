import { useEffect, useRef } from 'react';
import { primaryButton } from './experiment-detail/ui';
import StudyInstructions, { type StudyInstructionsProps } from './StudyInstructions';

interface StudyInstructionsDialogProps extends StudyInstructionsProps {
  experimentName: string;
  onClose: () => void;
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * The intro screen's instructions, reopened from the rating screen. A modal:
 * the header and "Back to rating" stay put while the instructions between
 * them scroll, the page behind is locked, and Escape, the close button or a
 * click on the backdrop all return the rater to the question they were on.
 */
function StudyInstructionsDialog({ experimentName, onClose, ...instructions }: StudyInstructionsDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Hand focus to the scrolling instructions so Tab starts inside the dialog
    // and the arrow keys scroll it straight away (Safari won't otherwise),
    // and give it back to the Instructions button on close.
    const opener = document.activeElement as HTMLElement | null;
    bodyRef.current?.focus();

    // Only the instructions scroll, never the question underneath. Hiding the
    // page scrollbar widens the page, so pad by its width to keep it still.
    const { overflow, paddingRight } = document.body.style;
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = 'hidden';
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${scrollbarWidth}px`;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== 'Tab' || !dialogRef.current) return;
      // Keep Tab inside the dialog while it is open.
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === dialogRef.current)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);

    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = overflow;
      document.body.style.paddingRight = paddingRight;
      opener?.focus();
    };
  }, [onClose]);

  return (
    <div
      role="presentation"
      // mousedown, not click: a text selection dragged out of the dialog must
      // not close it. Primary button only, so a right-click leaves it open.
      onMouseDown={(e) => {
        if (e.button === 0 && e.target === e.currentTarget) onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 100,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 'var(--rater-gutter)',
        background: 'rgba(40, 36, 32, 0.45)',
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="study-instructions-title"
        tabIndex={-1}
        data-testid="study-instructions-dialog"
        style={{
          width: '100%',
          maxWidth: 640,
          // 100% of the backdrop, which fills the screen inside its padding:
          // no viewport unit, so no dvh fallback to worry about.
          maxHeight: 'min(760px, 100%)',
          display: 'flex',
          flexDirection: 'column',
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: '0 24px 60px -20px rgba(40, 36, 32, 0.45)',
          outline: 'none',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            padding: '20px 12px 16px clamp(20px, 5vw, 32px)',
            borderBottom: '1px solid var(--line)',
          }}
        >
          <div style={{ paddingTop: 6, minWidth: 0 }}>
            <div
              style={{
                font: '600 var(--text-label)/1 var(--font-mono)',
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                color: 'var(--accent-soft-ink)',
              }}
            >
              Study instructions
            </div>
            <h2
              id="study-instructions-title"
              style={{
                margin: '10px 0 0',
                fontFamily: 'var(--font-head)',
                fontWeight: 600,
                fontSize: 'var(--text-heading)',
                lineHeight: 'var(--leading-heading)',
                color: 'var(--ink)',
              }}
            >
              {experimentName}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close instructions"
            style={{
              flex: '0 0 auto',
              width: 44,
              height: 44,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              border: 'none',
              borderRadius: 'var(--radius-sm)',
              background: 'transparent',
              color: 'var(--muted)',
              cursor: 'pointer',
            }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>

        <div
          ref={bodyRef}
          className="study-instructions-body"
          role="region"
          aria-labelledby="study-instructions-title"
          tabIndex={0}
          data-testid="study-instructions-body"
          style={{
            flex: '1 1 auto',
            minHeight: 0,
            overflowY: 'auto',
            overscrollBehavior: 'contain',
            // The last block carries its own bottom margin.
            padding: '24px clamp(20px, 5vw, 32px) 0',
          }}
        >
          <StudyInstructions {...instructions} />
        </div>

        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            padding: '14px clamp(20px, 5vw, 32px) 18px',
            borderTop: '1px solid var(--line)',
          }}
        >
          <button type="button" onClick={onClose} style={{ ...primaryButton, minHeight: 44 }}>
            Back to rating
          </button>
        </div>
      </div>
    </div>
  );
}

export default StudyInstructionsDialog;
