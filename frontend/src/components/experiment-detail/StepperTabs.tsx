import React from 'react';

/**
 * The numbered stepper tab bar on the experiment detail page.
 *
 * Semantics (matches the Fieldbook redesign):
 * - Overview is an entry surface — not a step, just a home tab with the setup
 *   checklist and topline stats.
 * - Steps 1–4 are the guided setup sequence: Questions → Instructions &
 *   prompts → Rater assistance → Launch on Prolific. Each has a completion
 *   state derived from the model (see `deriveStepStates` in the parent).
 * - Responsive: the tab row scrolls sideways (edge fade ≤1024px); on phones
 *   (≤640px) it's swapped for a single native <select> of the same sections.
 *   Both are always rendered; CSS in index.css picks which one shows.
 *
 * A step's marker shows:
 *   - a checkmark on a filled accent circle if `done`
 *   - the step number on a filled accent circle if `current` (highlighted next)
 *   - the step number on a muted circle if `todo`
 */

export type TabKey =
  | 'overview'
  | 'questions'
  | 'instructions'
  | 'assistance'
  | 'launch';

export type StepStatus = 'done' | 'current' | 'todo';

export interface StepDef {
  key: TabKey;
  index: number; // 1..4 for the stepper items
  label: string;
  status: StepStatus;
}

interface StepperTabsProps {
  active: TabKey;
  onChange: (key: TabKey) => void;
  steps: StepDef[];
}

export function StepperTabs({ active, onChange, steps }: StepperTabsProps) {
  const doneCount = steps.filter((s) => s.status === 'done').length;
  return (
    <>
      <select
        className="xd-section-select"
        aria-label="Section"
        data-testid="section-select"
        value={active}
        onChange={(e) => onChange(e.target.value as TabKey)}
      >
        <option value="overview">Overview</option>
        <optgroup label={`Setup · ${doneCount} of ${steps.length} done`}>
          {steps.map((step) => (
            <option key={step.key} value={step.key}>
              {step.status === 'done' ? '✓' : `${step.index}.`} {step.label}
            </option>
          ))}
        </optgroup>
      </select>
      <div className="xd-tabs">
        <TabButton
          active={active === 'overview'}
          onClick={() => onChange('overview')}
          testId="tab-overview"
        >
          <span
            aria-hidden
            style={{
              width: 22,
              height: 22,
              borderRadius: '50%',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              flex: '0 0 auto',
              background: 'var(--surface-2)',
              border: '1px solid var(--faint)',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: 'var(--muted)',
              }}
            />
          </span>
          <span>Overview</span>
        </TabButton>

        <Divider />

        {steps.map((step) => (
          <TabButton
            key={step.key}
            active={active === step.key}
            onClick={() => onChange(step.key)}
            testId={`tab-${step.key}`}
          >
            <StepMarker index={step.index} status={step.status} />
            <span>{step.label}</span>
          </TabButton>
        ))}
      </div>
    </>
  );
}


function TabButton({
  active,
  onClick,
  children,
  style,
  tone = 'default',
  testId,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  style?: React.CSSProperties;
  tone?: 'default' | 'danger';
  testId?: string;
}) {
  const activeColor = tone === 'danger' ? 'var(--danger)' : 'var(--ink)';
  const inactiveColor = tone === 'danger' ? 'var(--muted)' : 'var(--muted)';
  const underline = tone === 'danger' ? 'var(--danger)' : 'var(--accent)';
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      style={{
        position: 'relative',
        background: 'none',
        border: 'none',
        padding: '14px 2px',
        cursor: 'pointer',
        font: '600 14px var(--font-body)',
        display: 'flex',
        alignItems: 'center',
        gap: 9,
        color: active ? activeColor : inactiveColor,
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {children}
      {active && (
        <span
          aria-hidden
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            // 0, not -1: the tab row scrolls (overflow-x), which clips
            // anything hanging below its padding box.
            bottom: 0,
            height: 2,
            background: underline,
          }}
        />
      )}
    </button>
  );
}

function StepMarker({ index, status }: { index: number; status: StepStatus }) {
  if (status === 'done') {
    return (
      <span
        aria-hidden
        style={{
          width: 22,
          height: 22,
          borderRadius: '50%',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flex: '0 0 auto',
          background: 'var(--accent)',
          color: '#fff',
          fontSize: 12,
        }}
      >
        ✓
      </span>
    );
  }
  if (status === 'current') {
    return (
      <span
        aria-hidden
        style={{
          width: 22,
          height: 22,
          borderRadius: '50%',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flex: '0 0 auto',
          background: 'var(--accent)',
          color: '#fff',
          fontSize: 12,
          fontWeight: 600,
        }}
      >
        {index}
      </span>
    );
  }
  return (
    <span
      aria-hidden
      style={{
        width: 22,
        height: 22,
        borderRadius: '50%',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        flex: '0 0 auto',
        background: 'var(--surface-2)',
        border: '1px solid var(--faint)',
        color: 'var(--muted)',
        fontSize: 12,
      }}
    >
      {index}
    </span>
  );
}

function Divider() {
  return (
    <span
      aria-hidden
      style={{
        width: 1,
        background: 'var(--faint)',
        margin: '10px 4px',
        alignSelf: 'stretch',
      }}
    />
  );
}
