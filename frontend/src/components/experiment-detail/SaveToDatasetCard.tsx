import { useEffect, useState } from 'react';
import { api } from '../../api';
import type { Dataset, ExperimentRound } from '../../types';
import { formatMinorUnits } from './reward';
import { SectionCard, secondaryButton } from './ui';

function fieldLabel(field: string): string {
  return field.replace(/_/g, ' ');
}

/**
 * Offers to save a pilot's estimated completion time and reward to the
 * experiment's dataset card, so the dataset's next study can launch in one
 * click (#96).
 *
 * The values are the pilot round's own, which every later round on this
 * experiment reuses; the recommendation itself sizes places only. Both units
 * already match the card's: minutes, and the per-participant reward in the
 * workspace currency's minor units (what PilotStudyCreate.reward and one-click
 * launch take), so nothing is converted.
 */
export function SaveToDatasetCard({
  datasetId,
  datasetName,
  pilot,
  currencyCode,
  currencySymbol,
}: {
  datasetId: number;
  datasetName: string;
  pilot: ExperimentRound;
  currencyCode: string | null;
  currencySymbol: string | null;
}) {
  const [card, setCard] = useState<Dataset | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getDataset(datasetId)
      .then((dataset) => {
        if (!cancelled) setCard(dataset);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load the dataset card');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [datasetId]);

  const time = pilot.estimated_completion_time;
  const reward = pilot.reward;
  const money = (minor: number) => formatMinorUnits(minor, currencyCode, currencySymbol);

  const handleSave = async () => {
    if (!card) return;
    // Name only the card values the save would actually change.
    const replaced: string[] = [];
    if (card.estimated_completion_time !== null && card.estimated_completion_time !== time) {
      replaced.push(`${card.estimated_completion_time} min`);
    }
    if (card.reward !== null && card.reward !== reward) {
      replaced.push(money(card.reward));
    }
    if (
      replaced.length > 0 &&
      !window.confirm(
        `The ${datasetName} card already has ${replaced.join(' and ')}. ` +
          `Replace with the pilot's ${time} min and ${money(reward)}?`,
      )
    ) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // The PATCH answers with the card as stored, readiness included.
      setCard(await api.updateDatasetCard(datasetId, { estimated_completion_time: time, reward }));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save to the dataset card');
    } finally {
      setSaving(false);
    }
  };

  const saved = card !== null && card.estimated_completion_time === time && card.reward === reward;

  return (
    <div data-testid="save-to-card-panel">
      <SectionCard header="Dataset card">
        <div style={{ fontSize: 13.5, color: 'var(--ink)', lineHeight: 1.6, marginBottom: 10 }}>
          The pilot was set to <strong>{time} min</strong> and <strong>{money(reward)}</strong> per
          participant, which later rounds reuse.{' '}
          {saved
            ? `The ${datasetName} card carries the same.`
            : `Save them to the ${datasetName} card so its next study can skip the pilot form.`}
        </div>
        {card && (
          <div
            data-testid="card-readiness"
            style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.55, marginBottom: 10 }}
          >
            {card.complete
              ? 'Card complete: the next study on this dataset can launch in one click.'
              : `Card incomplete: missing ${card.missing_for_complete.map(fieldLabel).join(', ')}.`}
          </div>
        )}
        {error && (
          <div style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 10 }}>{error}</div>
        )}
        {card && !saved && (
          <button
            data-testid="save-to-card-button"
            onClick={handleSave}
            disabled={saving}
            style={secondaryButton}
          >
            {saving ? 'Saving…' : 'Save time and reward to the dataset card'}
          </button>
        )}
      </SectionCard>
    </div>
  );
}
