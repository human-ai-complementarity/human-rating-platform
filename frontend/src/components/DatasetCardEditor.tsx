import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Dataset, DatasetCardFields, Screener, StudyLabel } from '../types';
import { ScreenerCheckboxes } from './ExperimentDetail';
import { rewardInputToMinor, rewardMinorToInput } from './experiment-detail/reward';
import {
  Field,
  inputStyle,
  primaryButton,
  secondaryButton,
  textareaStyle,
} from './experiment-detail/ui';

// Placeholders each name template may use; mirrors backend study_names.py. The
// public name gets only {dataset}, so raters can't tell which arm they're in.
const TEMPLATE_PLACEHOLDERS = {
  external_study_name: ['dataset'],
  internal_study_name: ['dataset', 'wave', 'method'],
} as const;

const STUDY_LABELS: [StudyLabel, string][] = [
  ['annotation', 'Annotation'],
  ['survey', 'Survey'],
  ['decision_making_task', 'Decision-making task'],
  ['writing_task', 'Writing task'],
  ['interview', 'Interview'],
  ['other', 'Other'],
];

// What a launch uses when the card leaves screeners unset (PilotStudyCreate's default).
const DEFAULT_SCREENERS: Screener[] = ['ai_taskers', 'fact_checkers', 'approval_rate'];

type Draft = {
  external_study_name: string;
  internal_study_name: string;
  study_blurb: string;
  estimated_completion_time: string;
  reward: string;
  num_ratings_per_question: string;
  study_label: StudyLabel | '';
  screeners: Screener[] | null;
};

const fieldLabel = (field: string) => field.replace(/_/g, ' ');

function toDraft(card: Dataset, currencyCode: string | null): Draft {
  return {
    external_study_name: card.external_study_name ?? '',
    internal_study_name: card.internal_study_name ?? '',
    study_blurb: card.study_blurb ?? '',
    estimated_completion_time: card.estimated_completion_time?.toString() ?? '',
    reward: card.reward != null ? rewardMinorToInput(card.reward, currencyCode) : '',
    num_ratings_per_question: card.num_ratings_per_question?.toString() ?? '',
    study_label: card.study_label ?? '',
    screeners: card.screeners ?? null,
  };
}

function checkTemplate(field: keyof typeof TEMPLATE_PLACEHOLDERS, value: string) {
  const allowed: readonly string[] = TEMPLATE_PLACEHOLDERS[field];
  for (const [, key] of value.matchAll(/\{([^{}]*)\}/g)) {
    if (!allowed.includes(key.trim())) {
      const known = allowed.map((p) => `{${p}}`).join(', ');
      throw new Error(`Unknown placeholder {${key}} in ${fieldLabel(field)}. Available: ${known}.`);
    }
  }
  if (/-\s*(pilot|round)\b/i.test(value)) {
    throw new Error(`Leave "- Pilot" and "- Round" out of ${fieldLabel(field)}; each study adds its own.`);
  }
}

function count(value: string, label: string): number | null {
  if (!value.trim()) return null;
  if (!/^\d+$/.test(value.trim()) || Number(value) < 1) {
    throw new Error(`${label} must be a whole number, at least 1.`);
  }
  return Number(value);
}

/** Card values from the form; a blank field is null, which clears it. */
function parseDraft(draft: Draft, currencyCode: string | null): DatasetCardFields {
  checkTemplate('external_study_name', draft.external_study_name);
  checkTemplate('internal_study_name', draft.internal_study_name);
  const reward = draft.reward.trim() ? rewardInputToMinor(draft.reward, currencyCode) : null;
  if (reward !== null && reward < 1) throw new Error('Reward must be more than zero.');
  return {
    external_study_name: draft.external_study_name.trim() || null,
    internal_study_name: draft.internal_study_name.trim() || null,
    study_blurb: draft.study_blurb.trim() || null,
    estimated_completion_time: count(draft.estimated_completion_time, 'Estimated completion time'),
    reward,
    num_ratings_per_question: count(draft.num_ratings_per_question, 'Ratings per question'),
    study_label: draft.study_label || null,
    screeners: draft.screeners,
  };
}

/**
 * Edits a dataset's card (#96). Saves only the fields that changed, so the
 * PATCH leaves the rest alone; a cleared field is sent as null. Closing with
 * unsaved edits asks first.
 */
export default function DatasetCardEditor({
  datasetId,
  datasetName,
  currencyCode,
  currencySymbol,
  onClose,
  onSaved,
}: {
  datasetId: number;
  datasetName: string;
  currencyCode: string | null;
  currencySymbol: string | null;
  onClose: () => void;
  onSaved: (dataset: Dataset) => void;
}) {
  const [card, setCard] = useState<Dataset | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getDataset(datasetId).then(
      (dataset) => {
        if (cancelled) return;
        setCard(dataset);
        setDraft(toDraft(dataset, currencyCode));
      },
      (err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load the card');
      },
    );
    return () => {
      cancelled = true;
    };
  }, [datasetId, currencyCode]);

  const dirty =
    card !== null &&
    draft !== null &&
    JSON.stringify(draft) !== JSON.stringify(toDraft(card, currencyCode));

  const requestClose = useCallback(() => {
    if (saving) return;
    if (dirty && !window.confirm(`Discard unsaved changes to the ${datasetName} card?`)) return;
    onClose();
  }, [saving, dirty, datasetName, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') requestClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [requestClose]);

  const set = (patch: Partial<Draft>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev));
    setSaved(false);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!card || !draft) return;
    setError(null);
    setSaving(true);
    try {
      const values = parseDraft(draft, currencyCode);
      const keys = Object.keys(values) as (keyof DatasetCardFields)[];
      const changes = Object.fromEntries(
        keys
          .filter((key) => JSON.stringify(values[key]) !== JSON.stringify(card[key] ?? null))
          .map((key) => [key, values[key]]),
      );
      const next = await api.updateDatasetCard(datasetId, changes);
      setCard(next);
      setDraft(toDraft(next, currencyCode));
      setSaved(true);
      onSaved(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save the card');
    } finally {
      setSaving(false);
    }
  };

  const missing = (fields: string[]) => fields.map(fieldLabel).join(', ');
  const text = (field: 'external_study_name' | 'internal_study_name', testId: string) => (
    <input
      id={`card-${field}`}
      data-testid={testId}
      value={draft?.[field] ?? ''}
      maxLength={255}
      onChange={(e) => set({ [field]: e.target.value } as Partial<Draft>)}
      style={inputStyle}
    />
  );

  return (
    <div
      role="presentation"
      onClick={requestClose}
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
        aria-label={`${datasetName} card`}
        data-testid="dataset-card-editor"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: '100%',
          maxWidth: 560,
          maxHeight: '90vh',
          overflow: 'auto',
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: '0 24px 60px -20px rgba(40, 36, 32, 0.5)',
          padding: 24,
        }}
      >
        <h2 style={{ margin: '0 0 6px', fontFamily: 'var(--font-head)', fontSize: 19 }}>
          {datasetName} card
        </h2>
        <p style={{ margin: '0 0 16px', fontSize: 13.5, color: 'var(--muted)', lineHeight: 1.55 }}>
          How studies on this dataset run. An experiment created from the dashboard copies only the
          internal name template; the create form sets its own public name and ratings target, which
          only API creates inherit. One-click pilots use the rest.
        </p>
        {error && (
          <div role="alert" style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 12 }}>
            {error}
          </div>
        )}
        {!card || !draft ? (
          !error && <div style={{ fontSize: 13.5, color: 'var(--muted)' }}>Loading…</div>
        ) : (
          <form onSubmit={handleSave} noValidate>
            <div
              style={{
                background: 'var(--surface-2)',
                borderRadius: 'var(--radius-sm)',
                padding: '10px 12px',
                fontSize: 13,
                lineHeight: 1.6,
                marginBottom: 16,
              }}
            >
              <div data-testid="card-launch-ready">
                {card.launch_ready
                  ? 'Launchable.'
                  : `Not launchable: missing ${missing(card.missing_for_launch)}.`}
              </div>
              <div data-testid="card-complete">
                {card.complete
                  ? 'Complete: one-click pilots available.'
                  : `Not complete: missing ${missing(card.missing_for_complete)}.`}
              </div>
            </div>
            <Field
              id="card-external_study_name"
              label="Public study name template"
              hint="Names an experiment created through the API without one. Placeholder: {dataset}."
            >
              {text('external_study_name', 'card-external-name-input')}
            </Field>
            <Field
              id="card-internal_study_name"
              label="Internal study name template"
              hint="Placeholders: {dataset}, {wave}, {method}."
            >
              {text('internal_study_name', 'card-internal-name-input')}
            </Field>
            <Field id="card-blurb" label="Study description" hint="Sent to Prolific by one-click pilots.">
              <textarea
                id="card-blurb"
                data-testid="card-blurb-input"
                value={draft.study_blurb}
                onChange={(e) => set({ study_blurb: e.target.value })}
                style={{ ...textareaStyle, minHeight: 80 }}
              />
            </Field>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Field id="card-time" label="Estimated completion time (minutes)">
                <input
                  id="card-time"
                  data-testid="card-time-input"
                  inputMode="numeric"
                  value={draft.estimated_completion_time}
                  onChange={(e) => set({ estimated_completion_time: e.target.value })}
                  style={inputStyle}
                />
              </Field>
              <Field
                id="card-reward"
                label={`Reward per participant${currencyCode ? ` (${currencyCode})` : ''}`}
              >
                <div style={{ ...inputStyle, display: 'flex', gap: 4, fontFamily: 'var(--font-mono)' }}>
                  {currencySymbol && <span style={{ color: 'var(--muted)' }}>{currencySymbol}</span>}
                  <input
                    id="card-reward"
                    data-testid="card-reward-input"
                    inputMode="decimal"
                    value={draft.reward}
                    onChange={(e) => {
                      if (/^[0-9]*\.?[0-9]*$/.test(e.target.value)) set({ reward: e.target.value });
                    }}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      border: 'none',
                      padding: 0,
                      outline: 'none',
                      font: 'inherit',
                      background: 'transparent',
                      color: 'var(--ink)',
                    }}
                  />
                </div>
              </Field>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Field id="card-ratings" label="Ratings per question">
                <input
                  id="card-ratings"
                  data-testid="card-ratings-input"
                  inputMode="numeric"
                  value={draft.num_ratings_per_question}
                  onChange={(e) => set({ num_ratings_per_question: e.target.value })}
                  style={inputStyle}
                />
              </Field>
              <Field id="card-study-label" label="Study label">
                <select
                  id="card-study-label"
                  data-testid="card-study-label-select"
                  value={draft.study_label}
                  onChange={(e) => set({ study_label: e.target.value as StudyLabel | '' })}
                  style={{ ...inputStyle, cursor: 'pointer' }}
                >
                  <option value="">Not set (Annotation)</option>
                  {STUDY_LABELS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field
              label="Pre-screeners"
              hint={draft.screeners === null ? 'Not set: launches use these defaults.' : undefined}
            >
              <ScreenerCheckboxes
                value={draft.screeners ?? DEFAULT_SCREENERS}
                onChange={(next) => set({ screeners: next })}
                testIdPrefix="card-screener"
              />
              {draft.screeners !== null && (
                <button
                  type="button"
                  onClick={() => set({ screeners: null })}
                  style={{ ...secondaryButton, marginTop: 8, padding: '4px 10px', fontSize: 12.5 }}
                >
                  Use defaults
                </button>
              )}
            </Field>
            <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 10 }}>
              {saved && (
                <span data-testid="card-saved" style={{ fontSize: 13, color: 'var(--muted)' }}>
                  Saved.
                </span>
              )}
              <button type="button" onClick={requestClose} disabled={saving} style={secondaryButton}>
                Close
              </button>
              <button
                type="submit"
                data-testid="card-save-button"
                disabled={saving}
                style={{ ...primaryButton, opacity: saving ? 0.7 : 1 }}
              >
                {saving ? 'Saving…' : 'Save card'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
