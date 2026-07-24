import { useState } from 'react';
import type { Experiment } from '../../types';

/**
 * Prior-participant exclusion picker used by both the pilot form and the
 * round-edit form. Only FINISHED experiments are valid new picks (their rater
 * set is fixed); any already-selected non-FINISHED experiment is grandfathered
 * — kept visible with a tag so admins can preserve prior selections but not
 * add new non-FINISHED targets. Matches backend validation in
 * services/admin/status.py:validate_new_exclusion_targets.
 */
function experimentSearchHaystack(exp: Experiment): string {
  return [exp.name, exp.internal_name ?? '', ...(exp.dataset_filenames ?? [])]
    .join(' ')
    .toLowerCase();
}

interface ExclusionPickerProps {
  experiments: Experiment[];
  selectedIds: number[];
  onChange: (ids: number[]) => void;
  testIdPrefix: string;
}

export function ExperimentExclusionPicker({
  experiments,
  selectedIds,
  onChange,
  testIdPrefix,
}: ExclusionPickerProps) {
  const [query, setQuery] = useState('');
  const q = query.trim().toLowerCase();
  const selectable = experiments.filter(
    (e) => e.status === 'FINISHED' || selectedIds.includes(e.id),
  );
  const visible = q
    ? selectable.filter((e) => experimentSearchHaystack(e).includes(q))
    : selectable;
  // Selected IDs with no matching experiment reference a since-deleted target.
  // The list above can't render a checkbox for them, so surface them as their
  // own removable rows — otherwise they count toward the total but can't be
  // unselected. Blank when searching so the filter stays clean.
  const deletedSelectedIds = q
    ? []
    : selectedIds.filter((id) => !experiments.some((e) => e.id === id));
  const toggle = (id: number, checked: boolean) => {
    if (checked) {
      onChange(Array.from(new Set([...selectedIds, id])));
    } else {
      onChange(selectedIds.filter((v) => v !== id));
    }
  };
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        padding: '10px 12px',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--surface-2)',
      }}
    >
      <input
        type="text"
        data-testid={`${testIdPrefix}-search`}
        placeholder="Search by experiment name or dataset filename"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{
          padding: '6px 8px',
          border: '1px solid var(--faint)',
          borderRadius: 4,
          fontSize: 13,
          margin: 0,
          background: 'var(--surface)',
        }}
      />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 180, overflowY: 'auto' }}>
        {visible.length === 0 && deletedSelectedIds.length === 0 && (
          <div
            style={{
              color: 'var(--muted)',
              fontSize: 12,
              fontStyle: 'italic',
              padding: '4px 0',
            }}
          >
            {selectable.length === 0 ? 'No finished experiments yet.' : 'No matches.'}
          </div>
        )}
        {visible.map((exp) => {
          const checked = selectedIds.includes(exp.id);
          const grandfathered = checked && exp.status !== 'FINISHED';
          const subtitle = [
            exp.internal_name && exp.internal_name !== exp.name ? exp.internal_name : null,
            exp.dataset_filenames.length > 0 ? exp.dataset_filenames.join(', ') : null,
          ]
            .filter(Boolean)
            .join(' · ');
          return (
            <label
              key={exp.id}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                fontSize: 13,
                cursor: 'pointer',
                margin: 0,
              }}
            >
              <input
                type="checkbox"
                data-testid={`${testIdPrefix}-option-${exp.id}`}
                checked={checked}
                onChange={(e) => toggle(exp.id, e.target.checked)}
                style={{
                  width: 16,
                  height: 16,
                  flex: '0 0 auto',
                  margin: '3px 0 0 0',
                  padding: 0,
                  cursor: 'pointer',
                }}
              />
              <span style={{ lineHeight: 1.4 }}>
                <strong>{exp.name}</strong>
                {grandfathered && (
                  <span
                    style={{
                      marginLeft: 6,
                      padding: '1px 6px',
                      borderRadius: 999,
                      background: 'var(--warn-soft)',
                      color: 'var(--warn)',
                      fontSize: 10,
                      fontWeight: 600,
                      textTransform: 'uppercase',
                      letterSpacing: '0.3px',
                    }}
                  >
                    Kept from prior round
                  </span>
                )}
                {subtitle && <span style={{ color: 'var(--muted)' }}> — {subtitle}</span>}
              </span>
            </label>
          );
        })}
        {deletedSelectedIds.map((id) => (
          <label
            key={`deleted-${id}`}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: 10,
              fontSize: 13,
              cursor: 'pointer',
              margin: 0,
            }}
          >
            <input
              type="checkbox"
              data-testid={`${testIdPrefix}-option-${id}`}
              checked
              onChange={() => toggle(id, false)}
              style={{
                width: 16,
                height: 16,
                flex: '0 0 auto',
                margin: '3px 0 0 0',
                padding: 0,
                cursor: 'pointer',
              }}
            />
            <span style={{ lineHeight: 1.4, color: 'var(--muted)' }}>
              <strong>Experiment #{id}</strong>
              <span
                style={{
                  marginLeft: 6,
                  padding: '1px 6px',
                  borderRadius: 999,
                  background: 'var(--warn-soft)',
                  color: 'var(--warn)',
                  fontSize: 10,
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  letterSpacing: '0.3px',
                }}
              >
                Deleted
              </span>
              <span> — uncheck to remove</span>
            </span>
          </label>
        ))}
      </div>
      {selectedIds.length > 0 && (
        <div style={{ color: 'var(--muted)', fontSize: 12 }}>
          {selectedIds.length} experiment{selectedIds.length === 1 ? '' : 's'} selected
        </div>
      )}
    </div>
  );
}
