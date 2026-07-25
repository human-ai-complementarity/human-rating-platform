import { useEffect } from 'react';
import { dangerButton, primaryButton, secondaryButton } from './experiment-detail/ui';

/**
 * Styled confirmation modal. Replaces native window.confirm for destructive or
 * state-changing actions (delete/archive) triggered from the experiment list.
 * Closes on overlay click or Escape; the confirm button reflects `tone`.
 */
interface ConfirmDialogProps {
  title: string;
  message: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: 'danger' | 'default';
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmDialog({
  title,
  message,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone = 'default',
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onCancel]);

  const confirmStyle = tone === 'danger' ? dangerButton : primaryButton;

  return (
    <div
      role="presentation"
      onClick={() => {
        if (!busy) onCancel();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 100,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 20,
        background: 'rgba(40, 36, 32, 0.45)',
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: 440,
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: '0 24px 60px -20px rgba(40, 36, 32, 0.5)',
          padding: 24,
        }}
      >
        <h2
          style={{
            margin: '0 0 10px',
            fontFamily: 'var(--font-head)',
            fontWeight: 600,
            fontSize: 19,
            letterSpacing: '-0.01em',
            color: 'var(--ink)',
          }}
        >
          {title}
        </h2>
        <div style={{ fontSize: 14, lineHeight: 1.55, color: 'var(--muted)' }}>{message}</div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 10,
            marginTop: 24,
          }}
        >
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            style={{ ...secondaryButton, opacity: busy ? 0.6 : 1 }}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            style={{ ...confirmStyle, opacity: busy ? 0.7 : 1 }}
          >
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ConfirmDialog;
