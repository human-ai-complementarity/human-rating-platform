import { useState } from 'react';
import { api } from '../../api';
import ConfirmDialog from '../ConfirmDialog';
import type { Dataset, Experiment, ExperimentGroup } from '../../types';
import { secondaryButton } from './ui';

// Moves between groups on the experiment page. Both controls report through
// the same two callbacks; the page refreshes and toasts on `onMoved`.
interface MoveCallbacks {
  onMoved: (message: string) => Promise<void>;
  onError: (message: string) => void;
}

const smallButton: React.CSSProperties = {
  ...secondaryButton,
  padding: '4px 10px',
  font: '600 12.5px var(--font-body)',
};

const errorMessage = (err: unknown) => (err instanceof Error ? err.message : 'Move failed');

// Only `group_id` changes; the PATCH schema requires assistance_method, and
// re-sending the stored one is a no-op.
async function moveToGroup(experiment: Experiment, groupId: number | null): Promise<void> {
  await api.updateExperiment(experiment.id, {
    assistance_method: experiment.assistance_method,
    group_id: groupId,
  });
}

/** Groups bucketed by dataset, the experiment's own dataset first. */
function datasetSections(groups: ExperimentGroup[], datasetId: number | null) {
  const sections = new Map<number, { name: string; groups: ExperimentGroup[] }>();
  for (const group of groups) {
    const section = sections.get(group.dataset_id) ?? { name: group.dataset_name, groups: [] };
    section.groups.push(group);
    sections.set(group.dataset_id, section);
  }
  return [...sections.entries()]
    .sort(([aId, a], [bId, b]) =>
      aId === datasetId ? -1 : bId === datasetId ? 1 : a.name.localeCompare(b.name),
    )
    .map(([id, section]) => ({ id, ...section }));
}

/**
 * The header's "Group:" line. A DRAFT can move to any existing group or out
 * of its group; a move to another dataset's group asks first, since it
 * re-attributes the experiment. Once launched the group is spend attribution
 * and stays put.
 */
export function GroupControl({
  experiment,
  currentWave,
  onMoved,
  onError,
}: MoveCallbacks & { experiment: Experiment; currentWave: string | null }) {
  // Null while the picker is closed; loaded fresh each time it opens.
  const [groups, setGroups] = useState<ExperimentGroup[] | null>(null);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  // A move to another dataset's group, waiting on the confirm.
  const [crossDataset, setCrossDataset] = useState<ExperimentGroup | null>(null);
  const locked = experiment.status !== 'DRAFT';
  const currentValue = experiment.group_id == null ? '' : String(experiment.group_id);

  const open = async () => {
    setBusy(true);
    try {
      setGroups(await api.listExperimentGroups());
      setSelected(currentValue);
    } catch (err) {
      onError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const move = async (target: ExperimentGroup | null) => {
    setBusy(true);
    try {
      await moveToGroup(experiment, target?.id ?? null);
      setGroups(null);
      setCrossDataset(null);
      await onMoved(target ? `Moved to ${target.name}.` : 'Moved out of its group.');
    } catch (err) {
      setCrossDataset(null);
      onError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const target = groups?.find((group) => String(group.id) === selected) ?? null;
    const fromDataset = experiment.group_dataset_id;
    if (target && fromDataset != null && target.dataset_id !== fromDataset) {
      setCrossDataset(target);
      return;
    }
    void move(target);
  };

  return (
    <div
      data-testid="group-control"
      style={{
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 8,
        marginTop: 6,
        fontSize: 14,
        color: 'var(--muted)',
      }}
    >
      <span>Group:</span>
      {groups === null ? (
        <>
          <span data-testid="group-current">
            {experiment.group_name
              ? `${experiment.group_name} · ${experiment.group_dataset_name} × ${experiment.wave}`
              : 'Ungrouped'}
          </span>
          {locked ? (
            <span data-testid="group-locked" style={{ fontSize: 12.5 }}>
              (locked after launch)
            </span>
          ) : (
            <button
              type="button"
              data-testid="group-move-button"
              onClick={open}
              disabled={busy}
              style={smallButton}
            >
              {busy ? 'Loading…' : 'Move'}
            </button>
          )}
        </>
      ) : (
        <>
          <select
            aria-label="Group"
            data-testid="group-select"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={busy}
            style={{
              width: 'auto',
              maxWidth: 360,
              padding: '4px 8px',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              font: '400 13.5px var(--font-body)',
              color: 'var(--ink)',
            }}
          >
            <option value="">Ungrouped</option>
            {datasetSections(groups, experiment.group_dataset_id).map((section) => (
              <optgroup key={section.id} label={section.name}>
                {section.groups.map((group) => (
                  <option key={group.id} value={group.id}>
                    {group.name} · {group.wave}
                    {group.wave === currentWave ? ' (current wave)' : ''}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <button
            type="button"
            data-testid="group-save"
            onClick={save}
            disabled={busy || selected === currentValue}
            style={{ ...smallButton, opacity: busy || selected === currentValue ? 0.55 : 1 }}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button
            type="button"
            onClick={() => setGroups(null)}
            disabled={busy}
            style={smallButton}
          >
            Cancel
          </button>
        </>
      )}
      {crossDataset && (
        <ConfirmDialog
          title={`Move from ${experiment.group_dataset_name} to ${crossDataset.dataset_name}?`}
          message={
            `${crossDataset.name} is a ${crossDataset.dataset_name} group. Moving there ` +
            `re-attributes this experiment from ${experiment.group_dataset_name} to ` +
            `${crossDataset.dataset_name}.`
          }
          confirmLabel="Move"
          busy={busy}
          onConfirm={() => move(crossDataset)}
          onCancel={() => setCrossDataset(null)}
        />
      )}
    </div>
  );
}

/** First of "<base>", "<base> (2)", … not taken on the dataset (names are case-insensitive). */
function freeGroupName(base: string, siblings: ExperimentGroup[]): string {
  const taken = new Set(siblings.map((group) => group.name.toLowerCase()));
  let name = base;
  for (let n = 2; taken.has(name.toLowerCase()); n++) name = `${base} (${n})`;
  return name;
}

/**
 * One-click fix for a group in an older wave: move to the same dataset's
 * group for `wave`. If there is none, confirm, then add `wave` to the
 * dataset's waves (when missing), create the group, and move.
 */
export function MoveToWaveButton({
  experiment,
  wave,
  onMoved,
  onError,
}: MoveCallbacks & { experiment: Experiment; wave: string }) {
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<{ dataset: Dataset; groupName: string } | null>(null);
  const datasetId = experiment.group_dataset_id;
  if (datasetId == null) return null;

  const start = async () => {
    setBusy(true);
    try {
      const groups = await api.listExperimentGroups({ dataset_id: datasetId });
      const siblings = groups.filter((group) => group.dataset_id === datasetId);
      const target = siblings.find((group) => group.wave === wave);
      if (target) {
        await moveToGroup(experiment, target.id);
        await onMoved(`Moved to ${target.name}.`);
        return;
      }
      const dataset = await api.getDataset(datasetId);
      setPlan({ dataset, groupName: freeGroupName(`${dataset.name} ${wave}`, siblings) });
    } catch (err) {
      onError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const createAndMove = async () => {
    if (!plan) return;
    const { dataset, groupName } = plan;
    setBusy(true);
    try {
      // `waves` replaces the set, so build on a fresh read: a wave added
      // while the dialog was open must not be dropped.
      const { waves } = await api.getDataset(dataset.id);
      if (!waves.includes(wave)) {
        await api.updateDatasetCard(dataset.id, { waves: [...waves, wave] });
      }
      const group = await api.createExperimentGroup({
        name: groupName,
        dataset_id: dataset.id,
        wave,
      });
      await moveToGroup(experiment, group.id);
      setPlan(null);
      await onMoved(`Created ${group.name} and moved this experiment there.`);
    } catch (err) {
      setPlan(null);
      onError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const confirmMessage =
    plan === null
      ? ''
      : plan.dataset.waves.includes(wave)
        ? `${plan.dataset.name} has no ${wave} group yet. This creates the group ` +
          `“${plan.groupName}” and moves this experiment into it.`
        : `${plan.dataset.name} has no ${wave} group yet. This adds ${wave} to its waves, ` +
          `creates the group “${plan.groupName}”, and moves this experiment into it.`;
  return (
    <>
      <button
        type="button"
        data-testid="move-to-current-wave"
        onClick={start}
        disabled={busy}
        style={{ ...smallButton, opacity: busy ? 0.6 : 1 }}
      >
        Move to {experiment.group_dataset_name} × {wave}
      </button>
      {plan && (
        <ConfirmDialog
          title={`Create a ${wave} group for ${plan.dataset.name}?`}
          message={confirmMessage}
          confirmLabel="Create and move"
          busy={busy}
          onConfirm={createAndMove}
          onCancel={() => setPlan(null)}
        />
      )}
    </>
  );
}
