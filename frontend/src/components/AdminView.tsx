import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import type {
  Dataset,
  Experiment,
  ExperimentCreate,
  ExperimentGroup,
  ExperimentStatus,
} from '../types';
import StatusLabel from './StatusLabel';
import RowActionMenu from './RowActionMenu';
import ConfirmDialog from './ConfirmDialog';
import { rewardDecimals } from './experiment-detail/reward';

// Notification amber (from the design mock). Used for the row "needs attention"
// dot and the needs-attention filter toggle.
const AMBER = 'oklch(0.64 0.12 68)';
const AMBER_HALO = 'oklch(0.64 0.12 68 / 0.14)';
const AMBER_SOFT_BG = 'oklch(0.64 0.12 68 / 0.10)';

// Status segment is one control; "ARCHIVED" is a pseudo-status that flips the
// list to archived rows. Everything else filters non-archived rows by status.
type StatusTab = ExperimentStatus | 'ALL' | 'ARCHIVED';
const STATUS_TABS: { value: StatusTab; label: string }[] = [
  { value: 'ALL', label: 'All' },
  { value: 'DRAFT', label: 'Draft' },
  { value: 'LAUNCH', label: 'Launched' },
  { value: 'FINISHED', label: 'Finished' },
  { value: 'ARCHIVED', label: 'Archived' },
];

// Search + filter selections persist across refreshes.
const FILTER_STORAGE_KEY = 'hrp.experiments.filters.v2';
type Filters = {
  query: string;
  statusFilter: StatusTab;
  needsOnly: boolean;
  grouped: boolean;
  waveFilter: string;
};
const DEFAULT_FILTERS: Filters = {
  query: '',
  statusFilter: 'ALL',
  needsOnly: false,
  grouped: true,
  waveFilter: '',
};

function parseWaveList(raw: string): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const part of raw.split(',')) {
    const token = part.trim().toLowerCase();
    if (token && !seen.has(token)) {
      seen.add(token);
      result.push(token);
    }
  }
  return result;
}

// `value` is the API contract and must not change. `label` is display only —
// "Unassisted" rather than "None" so a control row reads as a condition, not a
// missing setting.
const ASSISTANCE_METHODS: { value: string; label: string; description: string }[] = [
  { value: 'none', label: 'Unassisted', description: 'Control condition — raters work alone.' },
  { value: 'top_n', label: 'Top-N', description: 'Model surfaces N candidate answers.' },
  {
    value: 'human_as_a_tool',
    label: 'Human as a tool',
    description: 'Model delegates to the rater.',
  },
];

// Selectable pill shared by the dataset and wave rows in the group builder.
function chipStyle(active: boolean): React.CSSProperties {
  return {
    border: `1px solid ${active ? 'var(--accent)' : 'var(--faint)'}`,
    borderRadius: 999,
    padding: '5px 11px',
    font: `${active ? 600 : 500} 12px var(--font-mono)`,
    color: active ? 'var(--accent-soft-ink)' : 'var(--muted)',
    background: active ? 'var(--accent-soft)' : 'var(--surface)',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  };
}

function methodLabel(method: string): string {
  return ASSISTANCE_METHODS.find((m) => m.value === method)?.label ?? method;
}

function loadFilters(): Filters {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return DEFAULT_FILTERS;
    const parsed = JSON.parse(raw) as Partial<Filters>;
    const validStatus = STATUS_TABS.some((t) => t.value === parsed.statusFilter);
    return {
      query: typeof parsed.query === 'string' ? parsed.query : '',
      statusFilter: validStatus ? (parsed.statusFilter as StatusTab) : 'ALL',
      needsOnly: Boolean(parsed.needsOnly),
      grouped: parsed.grouped !== false,
      waveFilter: typeof parsed.waveFilter === 'string' ? parsed.waveFilter : '',
    };
  } catch {
    return DEFAULT_FILTERS;
  }
}

type GroupBucket = {
  key: string;
  groupId: number | null;
  name: string;
  datasetName: string | null;
  wave: string | null;
  experiments: Experiment[];
};

function bucketExperiments(experiments: Experiment[]): GroupBucket[] {
  const buckets = new Map<string, GroupBucket>();
  for (const exp of experiments) {
    const key = exp.group_id != null ? `group:${exp.group_id}` : 'ungrouped';
    const existing = buckets.get(key);
    if (existing) {
      existing.experiments.push(exp);
      continue;
    }
    buckets.set(key, {
      key,
      groupId: exp.group_id,
      name: exp.group_name ?? 'Ungrouped',
      datasetName: exp.group_dataset_name,
      wave: exp.wave,
      experiments: [exp],
    });
  }
  return [...buckets.values()].sort((a, b) => {
    if (a.groupId == null) return 1;
    if (b.groupId == null) return -1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
  });
}

// Zero-decimal currencies (JPY, KRW, …) have no minor unit, so the divisor and
// decimal places come from rewardDecimals rather than a hardcoded /100.
const formatSpend = (minorUnits: number, symbol: string, currencyCode: string | null) => {
  const decimals = rewardDecimals(currencyCode);
  return `${symbol}${(minorUnits / 10 ** decimals).toFixed(decimals)}`;
};

/**
 * Admin dashboard: create-new panel on the left, existing experiments on the
 * right. Clicking a row navigates into ExperimentDetail. The `internal_name`
 * (researcher-facing) is preferred as the row label — the public name still
 * shows underneath so the two aren't confused. Search / status / needs-only
 * filtering all happen client-side over the full set so the total-spend figure
 * and the archived count stay in sync with what's shown.
 */
function AdminView() {
  const navigate = useNavigate();
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currencySymbol, setCurrencySymbol] = useState('$');
  const [currencyCode, setCurrencyCode] = useState<string | null>(null);

  const [query, setQuery] = useState(() => loadFilters().query);
  const [statusFilter, setStatusFilter] = useState<StatusTab>(() => loadFilters().statusFilter);
  const [needsOnly, setNeedsOnly] = useState(() => loadFilters().needsOnly);
  const [grouped, setGrouped] = useState(() => loadFilters().grouped);
  const [waveFilter, setWaveFilter] = useState(() => loadFilters().waveFilter);
  const [groups, setGroups] = useState<ExperimentGroup[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);

  // Delete is the one destructive/irreversible action, so it still confirms;
  // archive/restore apply immediately with a toast (per the mock).
  const [pendingDelete, setPendingDelete] = useState<Experiment | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [newExperiment, setNewExperiment] = useState<ExperimentCreate>({
    name: '',
    internal_name: '',
    num_ratings_per_question: 3,
    prolific_completion_url: '',
    assistance_method: 'none',
    group_id: null,
  });

  useEffect(() => {
    loadExperiments();
    loadCatalog();
    api
      .getPlatformStatus()
      .then((s) => {
        setCurrencySymbol(s.currency_symbol || '$');
        setCurrencyCode(s.currency_code);
      })
      .catch(() => {});
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  // Persist filters so they survive a refresh.
  useEffect(() => {
    localStorage.setItem(
      FILTER_STORAGE_KEY,
      JSON.stringify({ query, statusFilter, needsOnly, grouped, waveFilter }),
    );
  }, [query, statusFilter, needsOnly, grouped, waveFilter]);

  const loadExperiments = async () => {
    try {
      setLoading(true);
      const data = await api.listExperiments({ includeArchived: true });
      setExperiments(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  const loadCatalog = async () => {
    try {
      const [nextGroups, nextDatasets] = await Promise.all([
        api.listExperimentGroups(),
        api.listDatasets(),
      ]);
      setGroups(nextGroups);
      setDatasets(nextDatasets);
    } catch {
      // Catalog is additive (picker + grouped labels); the list still works.
    }
  };

  const flash = (message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  };

  const label = (exp: Experiment) => exp.internal_name || exp.name;

  // Unlike archive/restore, duplicate is not idempotent — every POST mints
  // another COPY (n) — so ignore re-clicks while one is in flight.
  const handleDuplicateExperiment = async (exp: Experiment) => {
    if (duplicating) return;
    setError(null);
    setDuplicating(true);
    try {
      const copy = await api.duplicateExperiment(exp.id);
      navigate(`/admin/experiments/${copy.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setDuplicating(false);
    }
  };

  const handleArchiveToggle = async (exp: Experiment) => {
    const toArchived = exp.archived_at === null;
    setError(null);
    try {
      if (toArchived) await api.archiveExperiment(exp.id);
      else await api.unarchiveExperiment(exp.id);
      flash(`${toArchived ? 'Archived' : 'Restored'} “${label(exp)}”`);
      await loadExperiments();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const runDelete = async () => {
    if (!pendingDelete) return;
    const exp = pendingDelete;
    setError(null);
    setDeleting(true);
    try {
      await api.deleteExperiment(exp.id);
      setPendingDelete(null);
      flash(`Deleted “${label(exp)}”`);
      await loadExperiments();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setPendingDelete(null);
    } finally {
      setDeleting(false);
    }
  };

  const handleCreateExperiment = async (payload: ExperimentCreate) => {
    setError(null);
    // Backend normalises whitespace/empty → null for internal_name on both
    // create and update, so we just forward the form value as-typed.
    const created = await api.createExperiment(payload);
    setNewExperiment({
      name: '',
      internal_name: '',
      num_ratings_per_question: 3,
      prolific_completion_url: '',
      assistance_method: 'none',
      group_id: null,
    });
    await Promise.all([loadExperiments(), loadCatalog()]);
    navigate(`/admin/experiments/${created.id}`);
  };

  const archivedCount = useMemo(
    () => experiments.filter((e) => e.archived_at !== null).length,
    [experiments],
  );

  // Client-side filtering mirrors the design mock: ARCHIVED flips the source to
  // archived rows; otherwise we filter non-archived rows by status, needs-only,
  // and a name substring against both the internal and public name.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const showingArchived = statusFilter === 'ARCHIVED';
    const source = experiments.filter((e) =>
      showingArchived ? e.archived_at !== null : e.archived_at === null,
    );
    return source.filter((e) => {
      if (statusFilter !== 'ALL' && statusFilter !== 'ARCHIVED' && e.status !== statusFilter)
        return false;
      if (needsOnly && !e.needs_attention) return false;
      if (
        q &&
        !(
          (e.internal_name || '').toLowerCase().includes(q) ||
          e.name.toLowerCase().includes(q) ||
          (e.group_name || '').toLowerCase().includes(q) ||
          (e.group_dataset_name || '').toLowerCase().includes(q)
        )
      )
        return false;
      if (waveFilter && e.wave !== waveFilter) return false;
      return true;
    });
  }, [experiments, query, statusFilter, needsOnly, waveFilter]);

  const totalSpendMinor = useMemo(
    () => filtered.reduce((sum, e) => sum + e.spend_minor_units, 0),
    [filtered],
  );

  const filtersActive = query.trim() !== '' || statusFilter !== 'ALL' || needsOnly || waveFilter !== '';

  const clearFilters = () => {
    setQuery('');
    setStatusFilter('ALL');
    setNeedsOnly(false);
    setWaveFilter('');
  };

  const availableWaves = useMemo(() => {
    const waves = new Set<string>();
    for (const exp of experiments) {
      if (exp.archived_at === null && exp.wave) waves.add(exp.wave);
    }
    return [...waves].sort();
  }, [experiments]);

  return (
    <div className="admin-page">
      <div style={{ marginBottom: 28 }}>
        <h1
          style={{
            fontFamily: 'var(--font-head)',
            fontWeight: 600,
            fontSize: 30,
            letterSpacing: '-0.01em',
            margin: 0,
          }}
        >
          Experiments
        </h1>
        <p style={{ margin: '6px 0 0', fontSize: 15, color: 'var(--muted)' }}>
          Create and manage your rating experiments.
        </p>
      </div>

      {error && <ErrorBanner text={error} />}

      <div style={{ display: 'grid', gridTemplateColumns: '410px 1fr', gap: 28, alignItems: 'start' }}>
        <CreatePanel
          value={newExperiment}
          onChange={setNewExperiment}
          onSubmit={handleCreateExperiment}
          onCatalogRefresh={loadCatalog}
          groups={groups}
          datasets={datasets}
          experiments={experiments}
        />
        <ListPanel
          experiments={filtered}
          loading={loading}
          currencySymbol={currencySymbol}
          currencyCode={currencyCode}
          totalSpendLabel={formatSpend(totalSpendMinor, currencySymbol, currencyCode)}
          query={query}
          onQueryChange={setQuery}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          archivedCount={archivedCount}
          needsOnly={needsOnly}
          onToggleNeeds={() => setNeedsOnly((v) => !v)}
          grouped={grouped}
          onToggleGrouped={() => setGrouped((v) => !v)}
          waveFilter={waveFilter}
          waves={availableWaves}
          onWaveFilterChange={setWaveFilter}
          filtersActive={filtersActive}
          onClearFilters={clearFilters}
          onSelect={(exp) => navigate(`/admin/experiments/${exp.id}`)}
          onDuplicate={handleDuplicateExperiment}
          onArchiveToggle={handleArchiveToggle}
          onDelete={(exp) => setPendingDelete(exp)}
        />
      </div>

      {pendingDelete && (
        <ConfirmDialog
          title="Delete experiment"
          message={
            <>
              Delete <strong>{label(pendingDelete)}</strong>? This permanently removes its
              questions, ratings, and any linked Prolific studies. This cannot be undone.
            </>
          }
          confirmLabel="Delete"
          tone="danger"
          busy={deleting}
          onConfirm={runDelete}
          onCancel={() => {
            setDeleting(false);
            setPendingDelete(null);
          }}
        />
      )}

      {toast && <Toast text={toast} />}
    </div>
  );
}

function ListPanel({
  experiments,
  loading,
  currencySymbol,
  currencyCode,
  totalSpendLabel,
  query,
  onQueryChange,
  statusFilter,
  onStatusFilterChange,
  archivedCount,
  needsOnly,
  onToggleNeeds,
  grouped,
  onToggleGrouped,
  waveFilter,
  waves,
  onWaveFilterChange,
  filtersActive,
  onClearFilters,
  onSelect,
  onDuplicate,
  onArchiveToggle,
  onDelete,
}: {
  experiments: Experiment[];
  loading: boolean;
  currencySymbol: string;
  currencyCode: string | null;
  totalSpendLabel: string;
  query: string;
  onQueryChange: (value: string) => void;
  statusFilter: StatusTab;
  onStatusFilterChange: (value: StatusTab) => void;
  archivedCount: number;
  needsOnly: boolean;
  onToggleNeeds: () => void;
  grouped: boolean;
  onToggleGrouped: () => void;
  waveFilter: string;
  waves: string[];
  onWaveFilterChange: (wave: string) => void;
  filtersActive: boolean;
  onClearFilters: () => void;
  onSelect: (exp: Experiment) => void;
  onDuplicate: (exp: Experiment) => void;
  onArchiveToggle: (exp: Experiment) => void;
  onDelete: (exp: Experiment) => void;
}) {
  return (
    <div>
      {/* Filters live in their own card. The list below is not wrapped in a
          shared card: each group card sits directly on the page ground, which
          is what frees --surface-2 to mean "row hover" and nothing else. */}
      <div
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: 'var(--shadow)',
          padding: '18px 20px 16px',
          marginBottom: 16,
        }}
      >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
          marginBottom: 13,
        }}
      >
      <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
        <span
          aria-hidden
          style={{
            position: 'absolute',
            left: 12,
            top: '50%',
            transform: 'translateY(-50%)',
            color: 'var(--muted)',
            fontSize: 14,
            pointerEvents: 'none',
          }}
        >
          ⌕
        </span>
        <input
          type="search"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="Search experiments, groups, datasets…"
          aria-label="Search"
          style={{
            width: '100%',
            padding: '9px 12px 9px 31px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface)',
            font: '400 13.5px var(--font-body)',
            color: 'var(--ink)',
          }}
        />
      </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', flexShrink: 0 }}>
          Total spent{' '}
          <span style={{ fontWeight: 700, color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}>
            {totalSpendLabel}
          </span>
        </div>
      </div>

      {/* Filter controls row: status segmented control, needs toggle, clear. */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>

        <div style={{ display: 'flex', gap: 4, background: 'var(--surface-2)', padding: 4, borderRadius: 9, flexShrink: 0 }}>
          {STATUS_TABS.map((tab) => {
            const active = statusFilter === tab.value;
            const labelText =
              tab.value === 'ARCHIVED' && archivedCount > 0 ? `Archived (${archivedCount})` : tab.label;
            return (
              <button
                key={tab.value}
                type="button"
                onClick={() => onStatusFilterChange(tab.value)}
                style={{
                  border: 'none',
                  background: active ? 'var(--surface)' : 'transparent',
                  padding: '7px 13px',
                  borderRadius: 6,
                  font: `${active ? 600 : 500} 13px var(--font-body)`,
                  color: active ? 'var(--ink)' : 'var(--muted)',
                  cursor: 'pointer',
                  boxShadow: active ? '0 1px 2px rgba(30,30,20,0.08)' : 'none',
                }}
              >
                {labelText}
              </button>
            );
          })}
        </div>

        <button
          type="button"
          onClick={onToggleGrouped}
          aria-pressed={grouped}
          data-testid="grouped-toggle"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            border: `1px solid ${grouped ? 'var(--accent)' : 'var(--faint)'}`,
            borderRadius: 'var(--radius-sm)',
            padding: '8px 13px',
            font: `${grouped ? 600 : 500} 13px var(--font-body)`,
            color: grouped ? 'var(--accent-soft-ink)' : 'var(--muted)',
            background: grouped ? 'var(--accent-soft)' : 'var(--surface)',
            cursor: 'pointer',
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          Grouped
        </button>

        <button
          type="button"
          onClick={onToggleNeeds}
          aria-pressed={needsOnly}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            border: `1px solid ${needsOnly ? AMBER : 'var(--faint)'}`,
            borderRadius: 'var(--radius-sm)',
            padding: '8px 13px',
            font: `${needsOnly ? 600 : 500} 13px var(--font-body)`,
            color: AMBER,
            background: needsOnly ? AMBER_SOFT_BG : 'var(--surface)',
            cursor: 'pointer',
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <span style={{ fontSize: 9 }}>●</span> Needs attention
        </button>

        {waves.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            {waves.map((wave) => {
              const active = waveFilter === wave;
              return (
                <button
                  key={wave}
                  type="button"
                  data-testid={`wave-filter-${wave}`}
                  aria-pressed={active}
                  onClick={() => onWaveFilterChange(active ? '' : wave)}
                  style={{
                    border: `1px solid ${active ? 'var(--accent)' : 'var(--faint)'}`,
                    borderRadius: 999,
                    padding: '4px 10px',
                    font: `${active ? 600 : 500} 12px var(--font-mono)`,
                    letterSpacing: '0.02em',
                    color: active ? 'var(--accent-soft-ink)' : 'var(--muted)',
                    background: active ? 'var(--accent-soft)' : 'var(--surface)',
                    cursor: 'pointer',
                  }}
                >
                  {wave}
                </button>
              );
            })}
          </div>
        )}

        {filtersActive && (
          <button
            type="button"
            onClick={onClearFilters}
            style={{
              border: 'none',
              background: 'transparent',
              padding: '8px 6px',
              font: '500 13px var(--font-body)',
              color: 'var(--accent)',
              cursor: 'pointer',
              flexShrink: 0,
              whiteSpace: 'nowrap',
            }}
          >
            Clear filters
          </button>
        )}
      </div>
      </div>

      {loading || experiments.length === 0 ? (
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            boxShadow: 'var(--shadow)',
            padding: '48px 24px',
            textAlign: 'center',
            fontSize: 14,
            color: 'var(--muted)',
          }}
        >
          {loading
            ? 'Loading…'
            : filtersActive
              ? 'No experiments match your filters.'
              : 'No experiments yet. Create one to get started.'}
        </div>
      ) : grouped ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {bucketExperiments(experiments).map((bucket) => (
            <GroupCard
              key={bucket.key}
              bucket={bucket}
              currencySymbol={currencySymbol}
              currencyCode={currencyCode}
              onSelect={onSelect}
              onDuplicate={onDuplicate}
              onArchiveToggle={onArchiveToggle}
              onDelete={onDelete}
              onWaveClick={(wave) => onWaveFilterChange(waveFilter === wave ? '' : wave)}
            />
          ))}
        </div>
      ) : (
        <section
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            boxShadow: 'var(--shadow)',
            overflow: 'hidden',
          }}
        >
          {experiments.map((exp, idx) => (
            <ExperimentRow
              key={exp.id}
              exp={exp}
              currencySymbol={currencySymbol}
              currencyCode={currencyCode}
              isLast={idx === experiments.length - 1}
              onSelect={() => onSelect(exp)}
              onDuplicate={() => onDuplicate(exp)}
              onArchiveToggle={() => onArchiveToggle(exp)}
              onDelete={() => onDelete(exp)}
            />
          ))}
        </section>
      )}
    </div>
  );
}

function GroupCard({
  bucket,
  currencySymbol,
  currencyCode,
  onSelect,
  onDuplicate,
  onArchiveToggle,
  onDelete,
  onWaveClick,
}: {
  bucket: GroupBucket;
  currencySymbol: string;
  currencyCode: string | null;
  onSelect: (exp: Experiment) => void;
  onDuplicate: (exp: Experiment) => void;
  onArchiveToggle: (exp: Experiment) => void;
  onDelete: (exp: Experiment) => void;
  onWaveClick: (wave: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const spend = bucket.experiments.reduce((sum, exp) => sum + (exp.spend_minor_units || 0), 0);
  const attention = bucket.experiments.find((exp) => exp.needs_attention);
  const isGroup = bucket.groupId != null;
  const methodsPresent = new Set(
    bucket.experiments.map((exp) => exp.assistance_method || 'none'),
  );

  return (
    <section
      data-testid={isGroup ? `group-card-${bucket.groupId}` : 'group-card-ungrouped'}
      style={{
        background: 'var(--surface)',
        // The ungrouped bucket is recessed — no shadow, plainer border — so
        // scratch work doesn't compete with real groups for attention.
        border: `1px solid ${isGroup ? 'var(--faint)' : 'var(--line)'}`,
        borderRadius: 'var(--radius)',
        boxShadow: isGroup ? 'var(--shadow)' : 'none',
      }}
    >
      <div
        data-testid={isGroup ? `group-card-toggle-${bucket.groupId}` : 'group-card-toggle-ungrouped'}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setOpen((v) => !v);
          }
        }}
        // Header hover is a lift off --surface rather than --surface-2, which
        // now belongs to row hover alone.
        onMouseEnter={(e) => (e.currentTarget.style.background = '#fcfbf7')}
        onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          gap: 16,
          padding: '18px 22px 16px',
          border: 'none',
          background: 'transparent',
          borderRadius: 'var(--radius) var(--radius) 0 0',
          cursor: 'pointer',
          textAlign: 'left',
          transition: 'background 0.15s',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, minWidth: 0 }}>
          <span
            aria-hidden
            style={{ color: 'var(--muted)', fontSize: 12, width: 10, paddingTop: 7 }}
          >
            {open ? '▾' : '▸'}
          </span>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
              {attention && (
                <AttentionDot reason={attention.attention_reason} testId="group-attention-dot" />
              )}
              <span
                style={{
                  fontFamily: 'var(--font-head)',
                  fontSize: 21,
                  fontWeight: 600,
                  letterSpacing: '-0.015em',
                  lineHeight: 1.2,
                }}
              >
                {bucket.name}
              </span>
              {bucket.wave && (
                <span
                  data-testid={`group-wave-${bucket.groupId ?? 'ungrouped'}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onWaveClick(bucket.wave!);
                  }}
                  style={{
                    border: '1px solid var(--faint)',
                    borderRadius: 999,
                    padding: '2px 9px',
                    font: '600 11px var(--font-mono)',
                    color: 'var(--muted)',
                    background: 'var(--surface-2)',
                  }}
                >
                  {bucket.wave}
                </span>
              )}
            </div>
            <div style={{ marginTop: 5, font: '500 12.5px var(--font-mono)', color: 'var(--muted)' }}>
              {isGroup
                ? `${bucket.datasetName ? `${bucket.datasetName} · ` : ''}${bucket.experiments.length} experiment${bucket.experiments.length === 1 ? '' : 's'}`
                : `scratch work and pilots · ${bucket.experiments.length} experiment${bucket.experiments.length === 1 ? '' : 's'}`}
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          <div
            data-testid={isGroup ? `group-spend-${bucket.groupId}` : 'group-spend-ungrouped'}
            style={{
              fontSize: 17,
              fontWeight: 700,
              fontVariantNumeric: 'tabular-nums',
              lineHeight: 1.2,
            }}
          >
            {formatSpend(spend, currencySymbol, currencyCode)}
          </div>
          <div
            style={{
              marginTop: 3,
              font: '500 10.5px var(--font-mono)',
              letterSpacing: '0.1em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
            }}
          >
            group spend
          </div>
        </div>
      </div>

      {/* Arm coverage for the group. Hiding the "Unassisted" row tag removed the
          only place the control arm was visible; this states it once per group
          instead of once per row. Never shown for the ungrouped bucket. */}
      {isGroup && (
        <div
          data-testid={`group-assistance-${bucket.groupId}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
            padding: '0 22px 16px 44px',
          }}
        >
          <span
            style={{
              font: '600 10.5px var(--font-mono)',
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
            }}
          >
            assistance
          </span>
          {ASSISTANCE_METHODS.map((method) => {
            const has = methodsPresent.has(method.value);
            return (
              <span
                key={method.value}
                title={
                  has
                    ? `${method.label} is already in this group`
                    : `No ${method.label} experiment in this group yet`
                }
                style={{
                  borderRadius: 999,
                  padding: '2px 10px',
                  whiteSpace: 'nowrap',
                  border: has ? '1px solid var(--accent-soft)' : '1px dashed var(--faint)',
                  background: has ? 'var(--accent-soft)' : 'transparent',
                  color: has ? 'var(--accent-soft-ink)' : 'var(--muted)',
                  font: `${has ? 600 : 500} 11px var(--font-mono)`,
                  opacity: has ? 1 : 0.85,
                }}
              >
                {method.label}
              </span>
            );
          })}
        </div>
      )}

      {open && (
        <div style={{ borderTop: '1px solid var(--line)' }}>
          {bucket.experiments.map((exp, idx) => (
            <ExperimentRow
              key={exp.id}
              exp={exp}
              currencySymbol={currencySymbol}
              currencyCode={currencyCode}
              isLast={idx === bucket.experiments.length - 1}
              nested
              onSelect={() => onSelect(exp)}
              onDuplicate={() => onDuplicate(exp)}
              onArchiveToggle={() => onArchiveToggle(exp)}
              onDelete={() => onDelete(exp)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function ExperimentRow({
  exp,
  currencySymbol,
  currencyCode,
  isLast,
  nested = false,
  onSelect,
  onDuplicate,
  onArchiveToggle,
  onDelete,
}: {
  exp: Experiment;
  currencySymbol: string;
  currencyCode: string | null;
  isLast: boolean;
  nested?: boolean;
  onSelect: () => void;
  onDuplicate: () => void;
  onArchiveToggle: () => void;
  onDelete: () => void;
}) {
  const isArchived = exp.archived_at !== null;
  const method = exp.assistance_method || 'none';
  // A control row carries no tag at all. "None" read as a missing setting
  // rather than a condition, and absence is the clearer signal.
  const showMethod = method !== 'none';
  const groupLine = exp.group_name
    ? [exp.group_name, exp.group_dataset_name, exp.wave].filter(Boolean).join(' · ')
    : 'Ungrouped · scratch work';

  return (
    <div
      onClick={onSelect}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 20,
        padding: nested ? '14px 22px 14px 44px' : '18px 22px',
        borderBottom: isLast ? 'none' : '1px solid var(--line)',
        // Round the last row's bottom so its full-bleed hover fill follows the
        // card's rounded bottom corners.
        borderBottomLeftRadius: isLast ? 'var(--radius)' : undefined,
        borderBottomRightRadius: isLast ? 'var(--radius)' : undefined,
        cursor: 'pointer',
        transition: 'background 0.15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: nested ? 11 : 12, minWidth: 0 }}>
        {/* Fixed gutter reserves space so titles align whether or not a dot shows. */}
        <div style={{ width: 8, flexShrink: 0, display: 'flex', justifyContent: 'center', paddingTop: 7 }}>
          {exp.needs_attention && <AttentionDot reason={exp.attention_reason} />}
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
            <span
              style={{
                fontFamily: 'var(--font-head)',
                fontSize: nested ? 15.5 : 17,
                fontWeight: 600,
                letterSpacing: nested ? '-0.005em' : '-0.01em',
              }}
            >
              {exp.internal_name || exp.name}
            </span>
            <StatusLabel status={exp.status} size="sm" />
            {showMethod && (
              <span
                data-testid={`experiment-method-${method}`}
                style={{
                  borderRadius: 999,
                  padding: '2px 9px',
                  border: '1px solid var(--accent-soft)',
                  background: 'var(--accent-soft)',
                  color: 'var(--accent-soft-ink)',
                  font: '600 10.5px var(--font-mono)',
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                  whiteSpace: 'nowrap',
                }}
              >
                {methodLabel(method)}
              </span>
            )}
          </div>
          {/* Flat mode has no group card above it, so the row states its own
              group · dataset · wave. Nested rows inherit it from the header. */}
          {!nested && (
            <div style={{ marginTop: 4, font: '500 12px var(--font-mono)', color: 'var(--muted)' }}>
              {groupLine}
            </div>
          )}
          <div style={{ marginTop: nested ? 4 : 3, fontSize: 12.5, color: 'var(--muted)' }}>
            {exp.internal_name ? `Public: ${exp.name} · ` : ''}
            {exp.question_count} questions · {exp.rating_count} ratings
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexShrink: 0 }}>
        <div style={{ width: nested ? 88 : 92, textAlign: 'right' }}>
          <div
            style={{
              fontSize: nested ? 14.5 : 15,
              fontWeight: nested ? 600 : 700,
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {formatSpend(exp.spend_minor_units, currencySymbol, currencyCode)}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>spent</div>
        </div>
        <RowActionMenu
          label={`Actions for ${exp.internal_name || exp.name}`}
          actions={[
            { label: 'View', testId: 'row-action-view', onSelect },
            { label: 'Duplicate', testId: 'row-action-duplicate', onSelect: onDuplicate },
            isArchived
              ? { label: 'Restore', testId: 'row-action-unarchive', onSelect: onArchiveToggle }
              : { label: 'Archive', testId: 'row-action-archive', onSelect: onArchiveToggle },
            { label: 'Delete', tone: 'danger', testId: 'row-action-delete', onSelect: onDelete },
          ]}
        />
      </div>
    </div>
  );
}

/**
 * Amber "action needed" dot shown in the row's left gutter when the backend
 * flags a pending admin action. Hovering reveals the reason, mirroring the
 * StatusLabel tooltip. Only rendered when there's something to flag (the caller
 * guards on `needs_attention`).
 */
function AttentionDot({
  reason,
  testId = 'experiment-attention-dot',
}: {
  reason: string | null;
  testId?: string;
}) {
  const [hovered, setHovered] = useState(false);

  return (
    <span
      style={{ position: 'relative', display: 'inline-flex' }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <span
        role="img"
        aria-label={reason ? `Action needed: ${reason}` : 'Needs attention'}
        tabIndex={0}
        data-testid={testId}
        title={reason ? undefined : 'Needs attention'}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: AMBER,
          boxShadow: `0 0 0 4px ${AMBER_HALO}`,
          cursor: 'help',
        }}
      />
      {hovered && reason && (
        <div
          role="tooltip"
          style={{
            position: 'absolute',
            top: 'calc(100% + 8px)',
            left: -4,
            zIndex: 20,
            width: 240,
            padding: '10px 12px',
            background: 'var(--ink)',
            color: 'var(--bg)',
            borderRadius: 'var(--radius-sm)',
            boxShadow: 'var(--shadow)',
            fontSize: 12,
            lineHeight: 1.5,
            fontWeight: 400,
          }}
        >
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              marginBottom: 5,
              color: AMBER,
            }}
          >
            Action needed
          </div>
          {reason}
        </div>
      )}
    </span>
  );
}

/** Bottom-center transient confirmation for row actions. */
function Toast({ text }: { text: string }) {
  return (
    <div
      role="status"
      style={{
        position: 'fixed',
        bottom: 28,
        left: '50%',
        transform: 'translateX(-50%)',
        background: 'var(--ink)',
        color: 'var(--bg)',
        padding: '12px 20px',
        borderRadius: 'var(--radius-sm)',
        fontSize: 14,
        boxShadow: 'var(--shadow)',
        zIndex: 50,
      }}
    >
      {text}
    </div>
  );
}

function ErrorBanner({ text }: { text: string }) {
  return (
    <div
      role="alert"
      style={{
        background: 'var(--danger-soft)',
        border: '1px solid var(--danger)',
        color: 'var(--danger)',
        borderRadius: 'var(--radius-sm)',
        padding: '11px 14px',
        marginBottom: 20,
        fontSize: 13.5,
      }}
    >
      {text}
    </div>
  );
}

/**
 * Group picker for the create panel. Replaces a `<select>` whose
 * "Create new group…" option disguised a create action as a value: picking a
 * group and starting a new one looked identical. Here the list is for choosing
 * and the footer button is for creating, and whatever was typed into the filter
 * seeds the inline builder instead of being thrown away.
 */
function GroupCombobox({
  groups,
  groupMode,
  selectedGroupId,
  newGroupName,
  onPick,
  onStartNew,
}: {
  groups: ExperimentGroup[];
  groupMode: 'none' | 'existing' | 'new';
  selectedGroupId: number | null;
  newGroupName: string;
  onPick: (groupId: number | null) => void;
  onStartNew: (seedName: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter((g) => `${g.name} ${g.dataset_name} ${g.wave}`.toLowerCase().includes(q));
  }, [groups, query]);

  // Index 0 is always "No group"; the filtered groups follow it.
  const optionCount = matches.length + 1;

  // Index 0 is the synthetic "No group" row, so a highlight parked there turns
  // the natural type-and-Enter flow into a silent "ungroup". Follow intent
  // instead: the first real match while filtering, and whatever is already
  // selected when the list opens with nothing typed.
  useEffect(() => {
    if (!open) return;
    if (query.trim()) {
      setActiveIndex(matches.length > 0 ? 1 : 0);
      return;
    }
    const selectedIndex = groups.findIndex((g) => g.id === selectedGroupId);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex + 1 : 0);
  }, [open, query, matches, groups, selectedGroupId]);

  // Click-outside closes. The listener only exists while the panel is open.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  // Keep the keyboard-active option in view inside the scrolling list.
  useEffect(() => {
    if (!open) return;
    document
      .getElementById(`group-option-${activeIndex}`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [open, activeIndex]);

  const close = (refocus: boolean) => {
    setOpen(false);
    setQuery('');
    if (refocus) triggerRef.current?.focus();
  };

  const commit = (index: number) => {
    onPick(index === 0 ? null : matches[index - 1].id);
    close(true);
  };

  const startNew = () => {
    onStartNew(query.trim());
    close(false);
  };

  const selected = groups.find((g) => g.id === selectedGroupId) ?? null;
  const triggerName = selected
    ? selected.name
    : groupMode === 'new'
      ? newGroupName.trim() || 'New group'
      : 'No group';
  const triggerMeta = selected
    ? `${selected.dataset_name} · ${selected.wave}`
    : groupMode === 'new'
      ? 'being created below'
      : 'scratch work — ungrouped';

  const wavePill = {
    border: '1px solid var(--faint)',
    borderRadius: 999,
    padding: '1px 7px',
    font: '600 10.5px var(--font-mono)',
    background: 'var(--surface-2)',
    color: 'var(--muted)',
  } as const;

  if (!open) {
    return (
      <div ref={wrapRef}>
        <button
          ref={triggerRef}
          type="button"
          data-testid="group-picker"
          aria-haspopup="listbox"
          aria-expanded={false}
          onClick={() => setOpen(true)}
          onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--accent)')}
          onMouseLeave={(e) => (e.currentTarget.style.borderColor = 'var(--faint)')}
          style={{
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            textAlign: 'left',
            padding: '11px 13px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface)',
            cursor: 'pointer',
          }}
        >
          <span style={{ minWidth: 0 }}>
            <span
              style={{
                display: 'block',
                fontSize: 14.5,
                fontWeight: 600,
                color: 'var(--ink)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {triggerName}
            </span>
            <span
              style={{
                display: 'block',
                marginTop: 2,
                font: '500 11.5px var(--font-mono)',
                color: 'var(--muted)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {triggerMeta}
            </span>
          </span>
          <span aria-hidden style={{ color: 'var(--muted)', fontSize: 11, flexShrink: 0 }}>
            ▾
          </span>
        </button>
      </div>
    );
  }

  return (
    <div ref={wrapRef}>
      <div
        style={{
          border: '1px solid var(--accent)',
          borderRadius: 'var(--radius-sm)',
          boxShadow: 'var(--shadow)',
          overflow: 'hidden',
          background: 'var(--surface)',
        }}
      >
        <input
          autoFocus
          type="text"
          role="combobox"
          aria-expanded
          aria-controls="group-picker-listbox"
          aria-activedescendant={`group-option-${activeIndex}`}
          aria-label="Find or name a group"
          data-testid="group-picker-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              setActiveIndex((i) => (i + 1) % optionCount);
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              setActiveIndex((i) => (i - 1 + optionCount) % optionCount);
            } else if (e.key === 'Enter') {
              e.preventDefault();
              // Typing a name that matches nothing is a create, not a reason to
              // fall back to "No group".
              if (query.trim() && matches.length === 0) startNew();
              else commit(activeIndex);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              close(true);
            }
          }}
          placeholder="Find or name a group…"
          style={{
            width: '100%',
            padding: '11px 13px',
            border: 'none',
            borderBottom: '1px solid var(--line)',
            borderRadius: 0,
            outline: 'none',
            background: 'var(--surface)',
            font: '400 14.5px var(--font-body)',
            color: 'var(--ink)',
          }}
        />
        <div
          id="group-picker-listbox"
          role="listbox"
          style={{ maxHeight: 232, overflow: 'auto', padding: 5 }}
        >
          {[null, ...matches].map((group, index) => {
            const isActive = index === activeIndex;
            const isSelected = group == null ? selectedGroupId == null : group.id === selectedGroupId;
            // Server-computed (a real COUNT), so it stays right past the
            // admin list's page size — the client array is capped at 100.
            const count = group?.experiment_count ?? 0;
            return (
              <div
                key={group ? group.id : 'none'}
                id={`group-option-${index}`}
                role="option"
                aria-selected={isSelected}
                data-testid={group ? `group-option-${group.id}` : 'group-option-none'}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => commit(index)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 10,
                  padding: '9px 10px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: isSelected
                    ? 'var(--accent-soft)'
                    : isActive
                      ? 'var(--surface-2)'
                      : 'transparent',
                }}
              >
                <span
                  style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}
                >
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: 'var(--ink)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {group ? group.name : 'No group'}
                  </span>
                  {group && <span style={wavePill}>{group.wave}</span>}
                </span>
                <span
                  style={{
                    font: '500 11.5px var(--font-mono)',
                    color: 'var(--muted)',
                    flexShrink: 0,
                  }}
                >
                  {group ? `${group.dataset_name} · ${count}` : 'scratch'}
                </span>
              </div>
            );
          })}
        </div>
        <div style={{ borderTop: '1px solid var(--line)', padding: 5 }}>
          <button
            type="button"
            data-testid="group-picker-create"
            onClick={startNew}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--accent-soft)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            style={{
              width: '100%',
              textAlign: 'left',
              padding: '9px 10px',
              border: 'none',
              borderRadius: 6,
              background: 'transparent',
              font: '600 13.5px var(--font-body)',
              color: 'var(--accent)',
              cursor: 'pointer',
            }}
          >
            {query.trim() ? `＋ Create “${query.trim()}”` : '＋ Create a new group'}
          </button>
        </div>
      </div>
    </div>
  );
}

function CreatePanel({
  value,
  onChange,
  onSubmit,
  onCatalogRefresh,
  groups,
  datasets,
  experiments,
}: {
  value: ExperimentCreate;
  onChange: (v: ExperimentCreate) => void;
  onSubmit: (data: ExperimentCreate) => Promise<void>;
  onCatalogRefresh: () => Promise<void>;
  groups: ExperimentGroup[];
  datasets: Dataset[];
  experiments: Experiment[];
}) {
  const [groupMode, setGroupMode] = useState<'none' | 'existing' | 'new'>('none');
  const [newGroupName, setNewGroupName] = useState('');
  const [datasetMode, setDatasetMode] = useState<'existing' | 'new'>('existing');
  const [datasetId, setDatasetId] = useState<number | ''>('');
  const [newDatasetName, setNewDatasetName] = useState('');
  // Waves are collected one token at a time rather than as a comma blob, so the
  // chip row and the group's wave pick read from the same list.
  const [newDatasetWaves, setNewDatasetWaves] = useState<string[]>([]);
  const [waveDraft, setWaveDraft] = useState('');
  const [newGroupWave, setNewGroupWave] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const selectedGroup = groups.find((g) => g.id === value.group_id) ?? null;
  const selectedDataset =
    datasetMode === 'existing' ? datasets.find((d) => d.id === datasetId) ?? null : null;
  const datasetWaves = selectedDataset?.waves ?? [];
  const pickerWaves = datasetMode === 'new' ? newDatasetWaves : datasetWaves;

  const resetBuilder = () => {
    setNewGroupName('');
    setDatasetMode('existing');
    setDatasetId('');
    setNewDatasetName('');
    setNewDatasetWaves([]);
    setWaveDraft('');
    setNewGroupWave('');
  };

  const commitWaveDraft = () => {
    const tokens = parseWaveList(waveDraft);
    if (tokens.length === 0) return;
    const next = [...newDatasetWaves];
    for (const token of tokens) if (!next.includes(token)) next.push(token);
    setNewDatasetWaves(next);
    setWaveDraft('');
    if (!newGroupWave) setNewGroupWave(tokens[0]);
  };

  // Typed tokens are removable so a typo doesn't force cancelling the whole
  // builder. Waves that come from an existing dataset are not ours to edit.
  const removeWave = (wave: string) => {
    const next = newDatasetWaves.filter((token) => token !== wave);
    setNewDatasetWaves(next);
    if (newGroupWave === wave) setNewGroupWave(next[0] ?? '');
  };

  const methodsInGroup = useMemo(() => {
    const groupId = selectedGroup?.id;
    if (groupId == null) return new Set<string>();
    return new Set(
      experiments.filter((exp) => exp.group_id === groupId).map((exp) => exp.assistance_method || 'none'),
    );
  }, [experiments, selectedGroup]);

  const chosenMethod = value.assistance_method || 'none';
  const methodAlreadyInGroup = selectedGroup != null && methodsInGroup.has(chosenMethod);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setSubmitting(true);
    try {
      let groupId = value.group_id ?? null;
      if (groupMode === 'new') {
        let nextDatasetId = typeof datasetId === 'number' ? datasetId : null;
        let wavesForGroup = datasetWaves;
        if (datasetMode === 'new') {
          wavesForGroup = newDatasetWaves;
          if (!newDatasetName.trim()) {
            throw new Error('Dataset name is required.');
          }
          if (wavesForGroup.length === 0) {
            throw new Error('Add at least one wave to the new dataset.');
          }
          const created = await api.createDataset({
            name: newDatasetName.trim(),
            waves: wavesForGroup,
          });
          nextDatasetId = created.id;
          setDatasetMode('existing');
          setDatasetId(created.id);
          await onCatalogRefresh();
        }
        if (nextDatasetId == null) {
          throw new Error('Pick a dataset for the new group.');
        }
        if (!newGroupName.trim()) {
          throw new Error('Group name is required.');
        }
        const wave = wavesForGroup.length === 1 ? wavesForGroup[0] : newGroupWave.trim();
        if (!wave) {
          throw new Error('Pick a wave for the new group.');
        }
        const createdGroup = await api.createExperimentGroup({
          name: newGroupName.trim(),
          dataset_id: nextDatasetId,
          wave,
        });
        groupId = createdGroup.id;
        setGroupMode('existing');
        onChange({ ...value, group_id: createdGroup.id });
        await onCatalogRefresh();
      }
      await onSubmit({
        ...value,
        group_id: groupId,
        assistance_method: chosenMethod,
      });
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
      }}
    >
      <SectionHeader label="Create new" />
      <form onSubmit={handleSubmit} style={{ padding: 24 }}>
        <Field
          id="experiment-name"
          testId="experiment-name-input"
          label="Public name"
          hint="Shown to raters on Prolific."
          value={value.name}
          onChange={(v) => onChange({ ...value, name: v })}
          placeholder="e.g., Factuality Evaluation"
          required
        />
        <Field
          id="experiment-internal-name"
          testId="experiment-internal-name-input"
          label={
            <>
              Internal name{' '}
              <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span>
            </>
          }
          hint="Only visible to you and other researchers (in this dashboard and Prolific's researcher view)."
          value={value.internal_name ?? ''}
          onChange={(v) => onChange({ ...value, internal_name: v })}
          placeholder="e.g., Q2 Factuality Eval — Sander"
        />
        <Field
          id="ratings-per-question"
          testId="ratings-per-question-input"
          type="number"
          label="Ratings per question"
          hint="How many different raters should evaluate each question."
          value={String(value.num_ratings_per_question)}
          onChange={(v) => onChange({ ...value, num_ratings_per_question: parseInt(v, 10) || 0 })}
          min={1}
          required
        />

        <div style={{ marginBottom: 16 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              gap: 12,
              marginBottom: 7,
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              Experiment group{' '}
              <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span>
            </span>
            <span style={{ font: '500 11.5px var(--font-mono)', color: 'var(--muted)' }}>
              dataset × wave
            </span>
          </div>
          <GroupCombobox
            groups={groups}
            groupMode={groupMode}
            selectedGroupId={value.group_id ?? null}
            newGroupName={newGroupName}
            onPick={(groupId) => {
              setGroupMode(groupId == null ? 'none' : 'existing');
              resetBuilder();
              onChange({ ...value, group_id: groupId });
            }}
            onStartNew={(seedName) => {
              setGroupMode('new');
              resetBuilder();
              setNewGroupName(seedName);
              onChange({ ...value, group_id: null });
            }}
          />
          <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7 }}>
            Groups are a dataset × wave. Skip this for scratch work.
          </div>
        </div>

        {groupMode === 'new' && (
          <div
            data-testid="new-group-panel"
            style={{
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              padding: '15px 15px 16px',
              marginBottom: 18,
              background: 'var(--surface-2)',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                marginBottom: 12,
              }}
            >
              <span
                style={{
                  font: '600 11px/1 var(--font-mono)',
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  color: 'var(--muted)',
                }}
              >
                New group
              </span>
              <button
                type="button"
                onClick={() => {
                  setGroupMode('none');
                  resetBuilder();
                  onChange({ ...value, group_id: null });
                }}
                onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--ink)')}
                onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--muted)')}
                style={{
                  border: 'none',
                  background: 'transparent',
                  padding: 0,
                  fontSize: 13,
                  color: 'var(--muted)',
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>
            </div>
            <Field
              id="new-group-name"
              testId="new-group-name-input"
              label="Group name"
              value={newGroupName}
              onChange={setNewGroupName}
              placeholder="e.g., MedQA Fall 25"
              required
            />
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>Dataset</div>
              <div
                data-testid="new-group-dataset"
                style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}
              >
                {datasets.map((dataset) => {
                  const active = datasetMode === 'existing' && datasetId === dataset.id;
                  return (
                    <button
                      key={dataset.id}
                      type="button"
                      data-testid={`dataset-chip-${dataset.id}`}
                      aria-pressed={active}
                      onClick={() => {
                        setDatasetMode('existing');
                        setDatasetId(dataset.id);
                        setNewGroupWave(dataset.waves[0] ?? '');
                      }}
                      style={chipStyle(active)}
                    >
                      {dataset.name}
                    </button>
                  );
                })}
                <button
                  type="button"
                  data-testid="dataset-chip-new"
                  aria-pressed={datasetMode === 'new'}
                  onClick={() => {
                    setDatasetMode('new');
                    setDatasetId('');
                    setNewGroupWave('');
                  }}
                  style={{
                    ...chipStyle(datasetMode === 'new'),
                    borderStyle: datasetMode === 'new' ? 'solid' : 'dashed',
                  }}
                >
                  ＋ new dataset
                </button>
              </div>
            </div>
            {datasetMode === 'new' && (
              <Field
                id="new-dataset-name"
                testId="new-dataset-name-input"
                label="Dataset name"
                hint="For pipeline datasets, use the card name verbatim."
                value={newDatasetName}
                onChange={setNewDatasetName}
                placeholder="e.g., medqa"
                required
              />
            )}
            {/* Always rendered. Gating this on "more than one wave" made a
                control appear and vanish as the dataset changed. */}
            <div style={{ marginBottom: 16 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  justifyContent: 'space-between',
                  gap: 12,
                  marginBottom: 7,
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 600 }}>Wave</span>
                <span style={{ font: '500 11.5px var(--font-mono)', color: 'var(--muted)' }}>
                  {datasetMode === 'new'
                    ? 'type a token, press enter'
                    : selectedDataset
                      ? `from ${selectedDataset.name}`
                      : 'pick a dataset first'}
                </span>
              </div>
              <div
                data-testid="new-group-wave"
                style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}
              >
                {pickerWaves.map((wave) => {
                  const active = newGroupWave === wave;
                  if (datasetMode !== 'new') {
                    return (
                      <button
                        key={wave}
                        type="button"
                        data-testid={`wave-chip-${wave}`}
                        aria-pressed={active}
                        onClick={() => setNewGroupWave(wave)}
                        style={chipStyle(active)}
                      >
                        {wave}
                      </button>
                    );
                  }
                  return (
                    <span
                      key={wave}
                      style={{
                        ...chipStyle(active),
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        padding: '5px 5px 5px 11px',
                        cursor: 'default',
                      }}
                    >
                      <button
                        type="button"
                        data-testid={`wave-chip-${wave}`}
                        aria-pressed={active}
                        onClick={() => setNewGroupWave(wave)}
                        style={{
                          border: 'none',
                          background: 'transparent',
                          padding: 0,
                          font: 'inherit',
                          color: 'inherit',
                          cursor: 'pointer',
                        }}
                      >
                        {wave}
                      </button>
                      <button
                        type="button"
                        data-testid={`wave-chip-remove-${wave}`}
                        aria-label={`Remove wave ${wave}`}
                        onClick={() => removeWave(wave)}
                        style={{
                          border: 'none',
                          background: 'transparent',
                          color: 'inherit',
                          cursor: 'pointer',
                          fontSize: 11,
                          lineHeight: 1,
                          padding: '2px 4px',
                        }}
                      >
                        ✕
                      </button>
                    </span>
                  );
                })}
                {datasetMode === 'new' && (
                  <input
                    type="text"
                    data-testid="new-dataset-wave-input"
                    aria-label="Add a wave token"
                    value={waveDraft}
                    onChange={(e) => setWaveDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ',') {
                        e.preventDefault();
                        commitWaveDraft();
                      }
                    }}
                    onBlur={commitWaveDraft}
                    placeholder="add wave ⏎"
                    style={{
                      width: 116,
                      padding: '4px 9px',
                      border: '1px dashed var(--faint)',
                      borderRadius: 999,
                      background: 'var(--surface)',
                      font: '500 12px var(--font-mono)',
                      color: 'var(--ink)',
                    }}
                  />
                )}
              </div>
            </div>
            {datasetMode === 'existing' && selectedDataset && datasetWaves.length === 0 && (
              <div style={{ fontSize: 12.5, color: 'var(--danger)', marginBottom: 16 }}>
                This dataset has no waves yet. Create a new dataset (or add waves via the API)
                before opening a group.
              </div>
            )}
          </div>
        )}

        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>Assistance method</div>
          {/* Option cards rather than a <select>: each arm carries its own
              description, and the "is this arm taken in the chosen group"
              availability now sits on the option it describes instead of in a
              separate sentence below. */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {ASSISTANCE_METHODS.map((method) => {
              const picked = chosenMethod === method.value;
              const inUse = methodsInGroup.has(method.value);
              return (
                <button
                  key={method.value}
                  type="button"
                  data-testid={`assistance-method-${method.value}`}
                  aria-pressed={picked}
                  onClick={() => onChange({ ...value, assistance_method: method.value })}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 12,
                    width: '100%',
                    textAlign: 'left',
                    padding: '10px 12px',
                    borderRadius: 'var(--radius-sm)',
                    border: `1px solid ${picked ? 'var(--accent)' : 'var(--faint)'}`,
                    background: picked ? 'var(--accent-soft)' : 'var(--surface)',
                    boxShadow: picked ? '0 0 0 3px rgba(61,107,92,0.12)' : 'none',
                    cursor: 'pointer',
                  }}
                >
                  <span style={{ minWidth: 0 }}>
                    <span style={{ display: 'block', fontSize: 14, fontWeight: 600, color: 'var(--ink)' }}>
                      {method.label}
                    </span>
                    <span
                      style={{ display: 'block', marginTop: 2, fontSize: 12, color: 'var(--muted)' }}
                    >
                      {method.description}
                    </span>
                  </span>
                  {selectedGroup && (
                    <span
                      style={{
                        borderRadius: 999,
                        padding: '2px 8px',
                        font: '600 10.5px var(--font-mono)',
                        letterSpacing: '0.06em',
                        textTransform: 'uppercase',
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                        border: `1px solid ${inUse ? 'var(--warn-soft)' : 'var(--faint)'}`,
                        background: inUse ? 'var(--warn-soft)' : 'var(--surface-2)',
                        color: inUse ? 'var(--warn)' : 'var(--muted)',
                      }}
                    >
                      {inUse ? 'in use' : 'open'}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          {methodAlreadyInGroup && (
            <div
              data-testid="duplicate-method-warning"
              style={{
                display: 'flex',
                gap: 10,
                marginTop: 10,
                background: 'var(--warn-soft)',
                border: '1px solid var(--warn)',
                borderRadius: 'var(--radius-sm)',
                padding: '10px 12px',
                fontSize: 12.5,
                lineHeight: 1.5,
                color: 'var(--warn)',
              }}
            >
              <span aria-hidden style={{ fontWeight: 700 }}>
                !
              </span>
              <span>
                {selectedGroup?.name} already has an experiment using{' '}
                {methodLabel(chosenMethod)}. One per method is the convention, but you can still
                create this.
              </span>
            </div>
          )}
        </div>

        {formError && (
          <div
            role="alert"
            style={{
              background: 'var(--danger-soft)',
              color: 'var(--danger)',
              borderRadius: 'var(--radius-sm)',
              padding: '10px 12px',
              fontSize: 13,
              marginBottom: 16,
            }}
          >
            {formError}
          </div>
        )}

        <div
          style={{
            background: 'var(--accent-soft)',
            color: 'var(--accent-soft-ink)',
            borderRadius: 'var(--radius-sm)',
            padding: '13px 15px',
            fontSize: 13,
            lineHeight: 1.5,
            marginBottom: 20,
          }}
        >
          After creating the experiment and uploading questions, use the Prolific section
          to run a pilot study and launch rating rounds.
        </div>
        <button
          type="submit"
          disabled={submitting}
          style={{
            width: '100%',
            padding: 13,
            background: 'var(--accent)',
            color: 'var(--accent-ink)',
            border: 'none',
            borderRadius: 'var(--radius-sm)',
            fontWeight: 600,
            fontSize: 15,
            cursor: submitting ? 'wait' : 'pointer',
          }}
        >
          Create experiment
        </button>
      </form>
    </section>
  );
}

function SectionHeader({ label }: { label: string }) {
  return (
    <div
      style={{
        padding: '18px 24px',
        borderBottom: '1px solid var(--line)',
        font: '600 11px/1 var(--font-mono)',
        letterSpacing: '0.16em',
        textTransform: 'uppercase',
        color: 'var(--muted)',
      }}
    >
      {label}
    </div>
  );
}

/** Small labelled input used inside the create panel. */
function Field({
  id,
  testId,
  label,
  hint,
  value,
  onChange,
  placeholder,
  required,
  type = 'text',
  min,
}: {
  id: string;
  testId?: string;
  label: React.ReactNode;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
  type?: 'text' | 'number';
  min?: number;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <label htmlFor={id} style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
        {label}
      </label>
      <input
        id={id}
        data-testid={testId}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        min={min}
        style={{
          width: '100%',
          padding: '11px 13px',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius-sm)',
          background: 'var(--surface)',
          font: '400 15px var(--font-body)',
          color: 'var(--ink)',
        }}
      />
      {hint && <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7 }}>{hint}</div>}
    </div>
  );
}

export default AdminView;
