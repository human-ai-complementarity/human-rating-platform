import { useEffect, useState } from 'react';
import { api } from '../../api';
import type { Dataset, LaunchPreview } from '../../types';
import { formatMinorUnits } from './reward';
import { SectionCard, primaryButton } from './ui';

const fieldLabel = (field: string) => field.replace(/_/g, ' ');

/**
 * One-click pilot (#96): builds the pilot draft from a complete dataset card
 * instead of the pilot form, excluding the participants of the dataset's other
 * experiments (the backend's list, shown on load and re-read for the confirm).
 * Publishing stays a separate step. An incomplete card gets one line naming
 * what's missing, since a dataset's first study usually goes through the pilot
 * form anyway.
 */
export function OneClickLaunch({
  experimentId,
  datasetId,
  datasetName,
  launchBlockers,
  currencyCode,
  currencySymbol,
  onLaunched,
}: {
  experimentId: number;
  datasetId: number;
  datasetName: string;
  launchBlockers: string[];
  currencyCode: string | null;
  currencySymbol: string | null;
  onLaunched: () => Promise<void>;
}) {
  const [card, setCard] = useState<Dataset | null>(null);
  const [excluded, setExcluded] = useState<LaunchPreview['excluded_experiments']>([]);
  const [checking, setChecking] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.getDataset(datasetId), api.getLaunchPreview(experimentId)]).then(
      ([dataset, preview]) => {
        if (cancelled) return;
        setExcluded(preview.excluded_experiments);
        setCard(dataset);
      },
      (err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load the dataset card');
      },
    );
    return () => {
      cancelled = true;
    };
  }, [datasetId, experimentId]);

  const errorLine = error && (
    <div role="alert" style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 10 }}>
      {error}
    </div>
  );

  if (!card) return errorLine || null;

  if (!card.complete) {
    return (
      <div data-testid="one-click-incomplete" style={{ fontSize: 13, color: 'var(--muted)' }}>
        One-click launch needs a complete {datasetName} card; it's missing{' '}
        {card.missing_for_complete.map(fieldLabel).join(', ')}. Use the pilot form below.
      </div>
    );
  }

  const time = card.estimated_completion_time ?? 0;
  const money = formatMinorUnits(card.reward ?? 0, currencyCode, currencySymbol);
  const blocked = launchBlockers.length > 0;
  const namesOf = (refs: LaunchPreview['excluded_experiments']) =>
    refs.map((exp) => exp.name).join(', ');
  const excludedNames = namesOf(excluded);

  const handleLaunch = async () => {
    setError(null);
    setChecking(true);
    let names: string;
    try {
      // Re-read the list, so the confirm names what the launch excludes now.
      const preview = await api.getLaunchPreview(experimentId);
      setExcluded(preview.excluded_experiments);
      names = namesOf(preview.excluded_experiments);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the exclusions');
      return;
    } finally {
      setChecking(false);
    }
    if (
      !window.confirm(
        `Create a pilot draft from the ${datasetName} card: ${time} min, ${money} per participant?\n` +
          (names
            ? `It excludes participants of: ${names}.\n`
            : 'The dataset has no other grouped experiments, so it excludes nobody.\n') +
          'You can still edit the draft before publishing.',
      )
    ) {
      return;
    }
    setLaunching(true);
    try {
      await api.launchFromCard(experimentId);
      await onLaunched();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to launch from the dataset card');
      setLaunching(false);
    }
  };

  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: 'flex', gap: 12, fontSize: 13.5, lineHeight: 1.55 }}>
      <span style={{ width: 150, flexShrink: 0, color: 'var(--muted)' }}>{label}</span>
      <span style={{ minWidth: 0, color: 'var(--ink)', whiteSpace: 'pre-wrap' }}>{value}</span>
    </div>
  );

  return (
    <div data-testid="one-click-panel">
      <SectionCard header="One-click pilot">
        <p style={{ fontSize: 13.5, color: 'var(--muted)', margin: '0 0 12px' }}>
          The {datasetName} card is complete, so the pilot draft can come straight from it. It
          excludes the participants of the dataset's other experiments automatically, and you can
          still edit the draft before publishing.
        </p>
        <div data-testid="one-click-card" style={{ display: 'grid', gap: 6, marginBottom: 14 }}>
          {row('Completion time', `${time} min`)}
          {row('Reward', `${money} per participant`)}
          {row('Study description', card.study_blurb)}
          {row(
            'Excludes',
            excludedNames
              ? `Participants of ${excludedNames}`
              : 'Nobody: the dataset has no other grouped experiments',
          )}
        </div>
        {blocked && (
          <div
            data-testid="one-click-blockers"
            style={{ fontSize: 13, color: 'var(--warn)', marginBottom: 10 }}
          >
            Can't launch yet — missing {launchBlockers.join(', ')}. The pilot form below says how to
            fix each.
          </div>
        )}
        {errorLine}
        <button
          type="button"
          data-testid="one-click-launch-button"
          onClick={handleLaunch}
          disabled={blocked || checking || launching}
          style={{
            ...primaryButton,
            ...(blocked || checking || launching ? { opacity: 0.5, cursor: 'not-allowed' } : {}),
          }}
        >
          {launching ? 'Creating…' : 'Create pilot draft from the card'}
        </button>
      </SectionCard>
    </div>
  );
}
