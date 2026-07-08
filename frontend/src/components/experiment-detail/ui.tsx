import React from 'react';

/**
 * Small styled primitives shared across the experiment-detail panels. Kept
 * here (not in a global design-system directory) because they encode
 * panel-specific spacing conventions — field density, hint tone, banner
 * corners — that the rest of the app doesn't share.
 */

export function Field({
  id,
  label,
  hint,
  disabled,
  children,
}: {
  id?: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: 16, opacity: disabled ? 0.65 : 1 }}>
      <label
        htmlFor={id}
        style={{
          display: 'block',
          fontSize: 13,
          fontWeight: 600,
          marginBottom: 7,
          color: 'var(--ink)',
        }}
      >
        {label}
      </label>
      {children}
      {hint && (
        <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7, lineHeight: 1.55 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

/** Standard input styling used inside Field. */
export const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '11px 13px',
  border: '1px solid var(--faint)',
  borderRadius: 'var(--radius-sm)',
  background: 'var(--surface)',
  font: '400 15px var(--font-body)',
  color: 'var(--ink)',
};

export const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  lineHeight: 1.55,
  resize: 'vertical',
  fontFamily: 'var(--font-body)',
};

/** Colored banner used for info/success/warning/error messages inside panels. */
export function Banner({
  tone,
  children,
  icon = true,
}: {
  tone: 'info' | 'ok' | 'warn' | 'danger';
  children: React.ReactNode;
  icon?: boolean;
}) {
  const palette = {
    info: { bg: 'var(--accent-soft)', border: 'var(--accent-soft)', ink: 'var(--accent-soft-ink)', dot: 'var(--accent)' },
    ok: { bg: 'var(--ok-soft)', border: 'var(--accent)', ink: 'var(--accent-soft-ink)', dot: 'var(--accent)' },
    warn: { bg: 'var(--warn-soft)', border: 'var(--warn)', ink: 'var(--warn)', dot: 'var(--warn)' },
    danger: { bg: 'var(--danger-soft)', border: 'var(--danger)', ink: 'var(--danger)', dot: 'var(--danger)' },
  }[tone];
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 10,
        background: palette.bg,
        border: `1px solid ${palette.border}`,
        borderRadius: 'var(--radius-sm)',
        padding: '11px 14px',
        fontSize: 13,
        lineHeight: 1.55,
        color: palette.ink,
      }}
    >
      {icon && (
        <span
          aria-hidden
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            background: palette.dot,
            marginTop: 6,
            flex: '0 0 auto',
          }}
        />
      )}
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  );
}

/**
 * Floating variant of Banner used for transient page-level toasts. Same
 * palette as Banner but adds a shadow (so it lifts off the page), a dismiss
 * button, and stronger visual weight since it can appear anywhere over the
 * layout without a natural surrounding surface.
 */
export function Toast({
  tone,
  children,
  onDismiss,
}: {
  tone: 'info' | 'ok' | 'warn' | 'danger';
  children: React.ReactNode;
  onDismiss?: () => void;
}) {
  const palette = {
    info: { bg: 'var(--accent-soft)', border: 'var(--accent-soft)', ink: 'var(--accent-soft-ink)', dot: 'var(--accent)' },
    ok: { bg: 'var(--ok-soft)', border: 'var(--accent)', ink: 'var(--accent-soft-ink)', dot: 'var(--accent)' },
    warn: { bg: 'var(--warn-soft)', border: 'var(--warn)', ink: 'var(--warn)', dot: 'var(--warn)' },
    danger: { bg: 'var(--danger-soft)', border: 'var(--danger)', ink: 'var(--danger)', dot: 'var(--danger)' },
  }[tone];
  return (
    <div
      role="status"
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 10,
        background: palette.bg,
        border: `1px solid ${palette.border}`,
        borderRadius: 'var(--radius-sm)',
        padding: '12px 14px',
        fontSize: 13,
        lineHeight: 1.55,
        color: palette.ink,
        boxShadow: '0 12px 32px -12px rgba(40, 36, 32, 0.35)',
      }}
    >
      <span
        aria-hidden
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: palette.dot,
          marginTop: 6,
          flex: '0 0 auto',
        }}
      />
      <div style={{ flex: 1 }}>{children}</div>
      {onDismiss && (
        <button
          type="button"
          aria-label="Dismiss"
          onClick={onDismiss}
          style={{
            background: 'none',
            border: 'none',
            color: palette.ink,
            opacity: 0.6,
            fontSize: 16,
            lineHeight: 1,
            cursor: 'pointer',
            padding: 2,
            margin: '-2px -2px 0 0',
          }}
        >
          ×
        </button>
      )}
    </div>
  );
}

/** Section card — the standard surface for a tab panel's contents. */
export function SectionCard({
  header,
  children,
  padded = true,
}: {
  header?: React.ReactNode;
  children: React.ReactNode;
  padded?: boolean;
}) {
  return (
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
        overflow: 'hidden',
      }}
    >
      {header && (
        <div
          style={{
            padding: '16px 22px',
            borderBottom: '1px solid var(--line)',
            font: '600 11px/1 var(--font-mono)',
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: 'var(--muted)',
          }}
        >
          {header}
        </div>
      )}
      <div style={{ padding: padded ? 22 : 0 }}>{children}</div>
    </section>
  );
}

/** Small toggle switch. Uses the accent color when on. */
export function ToggleSwitch({
  checked,
  onChange,
  disabled,
  testId,
}: {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <div
      style={{
        position: 'relative',
        width: 40,
        height: 23,
        flexShrink: 0,
      }}
    >
      <div
        data-testid={testId}
        role="switch"
        aria-checked={checked}
        tabIndex={0}
        onClick={() => {
          if (!disabled) onChange();
        }}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault();
            onChange();
          }
        }}
        style={{
          width: 40,
          height: 23,
          borderRadius: 99,
          border: '1px solid var(--faint)',
          background: checked ? 'var(--accent)' : 'var(--surface-2)',
          cursor: disabled ? 'not-allowed' : 'pointer',
          transition: 'background 0.2s',
          outline: 'none',
        }}
      />
      <div
        style={{
          position: 'absolute',
          top: 2,
          left: checked ? 20 : 2,
          width: 17,
          height: 17,
          borderRadius: '50%',
          background: 'var(--surface)',
          boxShadow: '0 1px 2px rgba(0,0,0,0.25)',
          transition: 'left 0.2s',
          pointerEvents: 'none',
        }}
      />
    </div>
  );
}

/** Primary and secondary button styles. */
export const primaryButton: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '11px 22px',
  background: 'var(--accent)',
  color: 'var(--accent-ink)',
  border: 'none',
  borderRadius: 'var(--radius-sm)',
  font: '600 14px var(--font-body)',
  cursor: 'pointer',
};

export const secondaryButton: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '10px 18px',
  background: 'var(--surface)',
  color: 'var(--ink)',
  border: '1px solid var(--faint)',
  borderRadius: 'var(--radius-sm)',
  font: '600 14px var(--font-body)',
  cursor: 'pointer',
};

export const dangerButton: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '11px 22px',
  background: 'var(--danger)',
  color: '#fff',
  border: 'none',
  borderRadius: 'var(--radius-sm)',
  font: '600 14px var(--font-body)',
  cursor: 'pointer',
};
