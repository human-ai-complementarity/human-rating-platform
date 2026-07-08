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
 * - A "Delete" action button sits at the far right — not a tab, just a direct
 *   trigger for the delete-confirmation flow.
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
  onDeleteClick: () => void;
  deleting?: boolean;
}

export function StepperTabs({ active, onChange, steps, onDeleteClick, deleting }: StepperTabsProps) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'stretch',
        gap: 18,
        borderBottom: '1px solid var(--faint)',
      }}
    >
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
      <SetupLabel />

      {steps.map((step, i) => (
        <React.Fragment key={step.key}>
          <TabButton
            active={active === step.key}
            onClick={() => onChange(step.key)}
            testId={`tab-${step.key}`}
          >
            <StepMarker index={step.index} status={step.status} />
            <span>{step.label}</span>
          </TabButton>
          {i < steps.length - 1 && (
            <span
              aria-hidden
              style={{ alignSelf: 'center', color: 'var(--faint)', fontSize: 12 }}
            >
              →
            </span>
          )}
        </React.Fragment>
      ))}

      <button
        type="button"
        onClick={onDeleteClick}
        disabled={deleting}
        data-testid="delete-experiment-button"
        style={{
          marginLeft: 'auto',
          background: 'none',
          border: 'none',
          padding: '14px 2px',
          cursor: deleting ? 'wait' : 'pointer',
          font: '600 14px var(--font-body)',
          color: 'var(--danger)',
          opacity: deleting ? 0.6 : 1,
          whiteSpace: 'nowrap',
        }}
      >
        {deleting ? 'Deleting…' : 'Delete'}
      </button>
    </div>
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
            bottom: -1,
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

function SetupLabel() {
  return (
    <span
      aria-hidden
      style={{
        alignSelf: 'center',
        font: '600 10px/1 var(--font-mono)',
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: 'var(--muted)',
      }}
    >
      Setup
    </span>
  );
}
