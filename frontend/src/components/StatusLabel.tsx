import { useState } from 'react';
import type { ExperimentStatus } from '../types';

// Single source of truth for the status pill and its hover legend. Reused
// by AdminView (list) and ExperimentDetail (header) so the same wording and
// colours appear everywhere.

interface StatusLabelProps {
  status: ExperimentStatus;
  size?: 'sm' | 'md';
}

const STATUS_META: Record<
  ExperimentStatus,
  { label: string; background: string; color: string; description: string }
> = {
  DRAFT: {
    label: 'Draft',
    background: '#f1f5f9',
    color: '#334155',
    description: 'Config editable. Pilot can run. First main round moves to Launch.',
  },
  LAUNCH: {
    label: 'Launch',
    background: '#dbeafe',
    color: '#1d4ed8',
    description:
      'Experiment config is locked (name, description, prompts, assistance method, dataset). More rounds can be launched. Round-level fields stay editable while unpublished.',
  },
  FINISHED: {
    label: 'Finished',
    background: '#dcfce7',
    color: '#166534',
    description:
      'Terminal. No more rounds. Selectable by other experiments as an exclusion source (its rater set is now fixed).',
  },
};

function StatusLabel({ status, size = 'md' }: StatusLabelProps) {
  const [hovered, setHovered] = useState(false);
  const meta = STATUS_META[status];

  const pillStyle: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    borderRadius: '999px',
    padding: size === 'sm' ? '2px 8px' : '4px 10px',
    fontSize: size === 'sm' ? '11px' : '12px',
    fontWeight: 600,
    letterSpacing: '0.3px',
    textTransform: 'uppercase',
    background: meta.background,
    color: meta.color,
    cursor: 'help',
    userSelect: 'none',
  };

  return (
    <span
      style={{ position: 'relative', display: 'inline-block' }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)}
      onBlur={() => setHovered(false)}
    >
      <span
        data-testid={`experiment-status-${status.toLowerCase()}`}
        tabIndex={0}
        style={pillStyle}
      >
        {meta.label}
      </span>
      {hovered && (
        <div
          role="tooltip"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            zIndex: 20,
            width: '320px',
            padding: '12px 14px',
            background: '#1e293b',
            color: '#f1f5f9',
            borderRadius: '6px',
            boxShadow: '0 6px 16px rgba(15, 23, 42, 0.25)',
            fontSize: '12px',
            lineHeight: 1.5,
            fontWeight: 400,
            textTransform: 'none',
            letterSpacing: 0,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: '6px', color: '#fff' }}>
            Experiment states
          </div>
          {(Object.entries(STATUS_META) as [ExperimentStatus, typeof meta][]).map(
            ([key, entry]) => (
              <div key={key} style={{ marginBottom: '6px' }}>
                <span
                  style={{
                    display: 'inline-block',
                    marginRight: '6px',
                    padding: '1px 6px',
                    borderRadius: '999px',
                    background: entry.background,
                    color: entry.color,
                    fontSize: '10px',
                    fontWeight: 600,
                    letterSpacing: '0.3px',
                    textTransform: 'uppercase',
                    verticalAlign: 'middle',
                  }}
                >
                  {entry.label}
                </span>
                <span style={{ color: key === status ? '#fff' : '#cbd5e1' }}>
                  {entry.description}
                </span>
              </div>
            ),
          )}
        </div>
      )}
    </span>
  );
}

export default StatusLabel;
