import { useEffect, useRef, useState } from 'react';

/**
 * Kebab (⋮) button that opens a small popover of row actions. Lives on each
 * experiment row; menu/trigger clicks stopPropagation so they don't fire the
 * row's navigate-on-click. Closes on outside click or Escape.
 */
export interface RowAction {
  label: string;
  tone?: 'danger' | 'default';
  onSelect: () => void;
  testId?: string;
}

function RowActionMenu({ actions, label = 'Row actions' }: { actions: RowAction[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div
      ref={wrapperRef}
      style={{ position: 'relative', flex: '0 0 auto' }}
      onClick={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 32,
          height: 32,
          borderRadius: 'var(--radius-sm)',
          border: '1px solid transparent',
          background: open ? 'var(--surface-2)' : 'transparent',
          color: 'var(--muted)',
          fontSize: 18,
          lineHeight: 1,
          cursor: 'pointer',
        }}
        onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
        onMouseLeave={(e) => (e.currentTarget.style.background = open ? 'var(--surface-2)' : 'transparent')}
      >
        ⋮
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            zIndex: 30,
            minWidth: 150,
            padding: 4,
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            boxShadow: '0 12px 32px -12px rgba(40, 36, 32, 0.35)',
          }}
        >
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              data-testid={action.testId}
              onClick={() => {
                setOpen(false);
                action.onSelect();
              }}
              style={{
                display: 'block',
                width: '100%',
                textAlign: 'left',
                padding: '8px 12px',
                border: 'none',
                borderRadius: 'calc(var(--radius-sm) - 2px)',
                background: 'transparent',
                color: action.tone === 'danger' ? 'var(--danger)' : 'var(--ink)',
                font: '500 13.5px var(--font-body)',
                cursor: 'pointer',
              }}
              onMouseEnter={(e) =>
                (e.currentTarget.style.background =
                  action.tone === 'danger' ? 'var(--danger-soft)' : 'var(--surface-2)')
              }
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default RowActionMenu;
