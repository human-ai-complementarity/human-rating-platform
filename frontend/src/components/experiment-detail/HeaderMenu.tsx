import React, { useEffect, useRef } from 'react';

/**
 * Small dropdown for the experiment header (Export, More actions).
 *
 * Built on <details> so the items stay in the DOM while closed: export
 * links keep their live `href` (tests and right-click "copy link" rely on
 * it) and nothing re-mounts on open. Closes on outside click, Escape, or
 * after an item is chosen.
 */
export function HeaderMenu({
  summary,
  ariaLabel,
  align = 'left',
  className = '',
  summaryClassName = '',
  summaryStyle,
  testId,
  children,
}: {
  summary: React.ReactNode;
  ariaLabel?: string;
  align?: 'left' | 'right';
  className?: string;
  summaryClassName?: string;
  summaryStyle?: React.CSSProperties;
  testId?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const onPointerDown = (e: MouseEvent) => {
      const el = ref.current;
      if (el?.open && !el.contains(e.target as Node)) el.open = false;
    };
    const onKeyDown = (e: KeyboardEvent) => {
      const el = ref.current;
      if (e.key === 'Escape' && el?.open) {
        el.open = false;
        el.querySelector('summary')?.focus();
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  const close = () => {
    if (ref.current) ref.current.open = false;
  };

  return (
    <details ref={ref} className={`xd-menu ${className}`} data-testid={testId}>
      <summary aria-label={ariaLabel} className={summaryClassName} style={summaryStyle}>
        {summary}
      </summary>
      <div className={`xd-menu-panel xd-menu-panel--${align}`} onClick={close}>
        {children}
      </div>
    </details>
  );
}
