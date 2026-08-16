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

const ASSISTANCE_METHODS: { value: string; label: string }[] = [
  { value: 'none', label: 'None' },
  { value: 'top_n', label: 'Top-N' },
  { value: 'human_as_a_tool', label: 'Human as a tool' },
];

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
      datasetName: exp.dataset_name,
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
          (e.dataset_name || '').toLowerCase().includes(q)
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
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
      }}
    >
      {/* Header + filter bar share the panel's horizontal padding; the rows
          below are full-bleed so hover and separators span the card edge. */}
      <div style={{ padding: '20px 24px 16px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 18,
        }}
      >
        <div
          style={{
            font: '600 11px/1 var(--font-mono)',
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: 'var(--muted)',
          }}
        >
          Your experiments
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)' }}>
          Total spent{' '}
          <span style={{ fontWeight: 700, color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}>
            {totalSpendLabel}
          </span>
        </div>
      </div>

      {/* Search on its own row so it stays full-width and stable — the filter
          controls below never squeeze it. */}
      <div style={{ position: 'relative', marginBottom: 12 }}>
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
          placeholder="Search by name…"
          aria-label="Search experiments by name"
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

      {/* Filter controls row: status segmented control, needs toggle, clear. */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>

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

      <div style={{ borderTop: '1px solid var(--line)' }}>
        {loading ? (
          <EmptyState text="Loading…" />
        ) : experiments.length === 0 ? (
          <EmptyState text={filtersActive ? 'No experiments match your filters.' : 'No experiments yet. Create one to get started.'} />
        ) : grouped ? (
          bucketExperiments(experiments).map((bucket, bucketIdx, all) => (
            <GroupCard
              key={bucket.key}
              bucket={bucket}
              currencySymbol={currencySymbol}
              currencyCode={currencyCode}
              isLast={bucketIdx === all.length - 1}
              onSelect={onSelect}
              onDuplicate={onDuplicate}
              onArchiveToggle={onArchiveToggle}
              onDelete={onDelete}
              onWaveClick={(wave) => onWaveFilterChange(waveFilter === wave ? '' : wave)}
            />
          ))
        ) : (
          experiments.map((exp, idx) => (
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
          ))
        )}
      </div>
    </section>
  );
}

function GroupCard({
  bucket,
  currencySymbol,
  currencyCode,
  isLast,
  onSelect,
  onDuplicate,
  onArchiveToggle,
  onDelete,
  onWaveClick,
}: {
  bucket: GroupBucket;
  currencySymbol: string;
  currencyCode: string | null;
  isLast: boolean;
  onSelect: (exp: Experiment) => void;
  onDuplicate: (exp: Experiment) => void;
  onArchiveToggle: (exp: Experiment) => void;
  onDelete: (exp: Experiment) => void;
  onWaveClick: (wave: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const spend = bucket.experiments.reduce((sum, exp) => sum + (exp.spend_minor_units || 0), 0);
  const attention = bucket.experiments.find((exp) => exp.needs_attention);

  return (
    <div
      data-testid={bucket.groupId != null ? `group-card-${bucket.groupId}` : 'group-card-ungrouped'}
      style={{
        borderBottom: isLast ? 'none' : '1px solid var(--line)',
        borderBottomLeftRadius: isLast ? 'var(--radius)' : undefined,
        borderBottomRightRadius: isLast ? 'var(--radius)' : undefined,
      }}
    >
      <div
        data-testid={
          bucket.groupId != null ? `group-card-toggle-${bucket.groupId}` : 'group-card-toggle-ungrouped'
        }
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
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
          padding: '16px 24px',
          border: 'none',
          background: 'var(--surface-2)',
          cursor: 'pointer',
          textAlign: 'left',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
          <span aria-hidden style={{ color: 'var(--muted)', fontSize: 12, width: 10 }}>
            {open ? '▾' : '▸'}
          </span>
          <div style={{ width: 9, flexShrink: 0, display: 'flex', justifyContent: 'center' }}>
            {attention && <AttentionDot reason={attention.attention_reason} />}
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span
                style={{
                  fontFamily: 'var(--font-head)',
                  fontSize: 16,
                  fontWeight: 600,
                  letterSpacing: '-0.01em',
                }}
              >
                {bucket.name}
              </span>
              {bucket.wave && (
                <span
                  data-testid={`group-wave-${bucket.groupId ?? 'ungrouped'}-${bucket.wave}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onWaveClick(bucket.wave!);
                  }}
                  style={{
                    border: '1px solid var(--faint)',
                    borderRadius: 999,
                    padding: '2px 8px',
                    font: '600 11px var(--font-mono)',
                    color: 'var(--muted)',
                    background: 'var(--surface)',
                  }}
                >
                  {bucket.wave}
                </span>
              )}
            </div>
            <div style={{ marginTop: 3, fontSize: 12.5, color: 'var(--muted)' }}>
              {bucket.datasetName ? `${bucket.datasetName} · ` : ''}
              {bucket.experiments.length} experiment{bucket.experiments.length === 1 ? '' : 's'}
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          <div
            data-testid={
              bucket.groupId != null ? `group-spend-${bucket.groupId}` : 'group-spend-ungrouped'
            }
            style={{ fontSize: 15, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}
          >
            {formatSpend(spend, currencySymbol, currencyCode)}
          </div>
          <div style={{ fontSize: 11, color: 'var(--muted)' }}>group spend</div>
        </div>
      </div>
      {open &&
        bucket.experiments.map((exp, idx) => (
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
        padding: nested ? '16px 24px 16px 48px' : '20px 24px',
        borderBottom: isLast ? 'none' : '1px solid var(--line)',
        // Round the last row's bottom so its full-bleed hover fill follows the
        // card's rounded bottom corners.
        borderBottomLeftRadius: isLast ? 'var(--radius)' : undefined,
        borderBottomRightRadius: isLast ? 'var(--radius)' : undefined,
        cursor: 'pointer',
        transition: 'background 0.15s',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14, minWidth: 0 }}>
        {/* Fixed gutter reserves space so titles align whether or not a dot shows. */}
        <div style={{ width: 9, flexShrink: 0, display: 'flex', justifyContent: 'center', paddingTop: 9 }}>
          {exp.needs_attention && <AttentionDot reason={exp.attention_reason} />}
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 11, flexWrap: 'wrap' }}>
            <span
              style={{
                fontFamily: 'var(--font-head)',
                fontSize: 18,
                fontWeight: 600,
                letterSpacing: '-0.01em',
              }}
            >
              {exp.internal_name || exp.name}
            </span>
            <StatusLabel status={exp.status} size="sm" />
            <span
              data-testid={`experiment-method-${exp.assistance_method || 'none'}`}
              style={{
                border: '1px solid var(--faint)',
                borderRadius: 999,
                padding: '2px 8px',
                font: '600 11px var(--font-body)',
                color: 'var(--muted)',
                background: 'var(--surface-2)',
              }}
            >
              {methodLabel(exp.assistance_method || 'none')}
            </span>
            {exp.wave && !nested && (
              <span
                data-testid={`experiment-wave-${exp.wave}`}
                style={{
                  border: '1px solid var(--faint)',
                  borderRadius: 999,
                  padding: '2px 8px',
                  font: '600 11px var(--font-mono)',
                  color: 'var(--muted)',
                  background: 'var(--surface-2)',
                }}
              >
                {exp.wave}
              </span>
            )}
          </div>
          {exp.internal_name && (
            <div style={{ marginTop: 5, fontSize: 13, color: 'var(--muted)' }}>Public: {exp.name}</div>
          )}
          <div style={{ marginTop: 3, fontSize: 13, color: 'var(--muted)' }}>
            {exp.question_count} questions · {exp.rating_count} ratings
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexShrink: 0 }}>
        <div style={{ width: 96, textAlign: 'right' }}>
          <div style={{ fontSize: 16, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
            {formatSpend(exp.spend_minor_units, currencySymbol, currencyCode)}
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)', letterSpacing: '0.02em' }}>spent</div>
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
function AttentionDot({ reason }: { reason: string | null }) {
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
        data-testid="experiment-attention-dot"
        title={reason ? undefined : 'Needs attention'}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        style={{
          width: 9,
          height: 9,
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
  const [newDatasetWaves, setNewDatasetWaves] = useState('');
  const [newGroupWave, setNewGroupWave] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const selectedGroup = groups.find((g) => g.id === value.group_id) ?? null;
  const selectedDataset =
    datasetMode === 'existing' ? datasets.find((d) => d.id === datasetId) ?? null : null;
  const datasetWaves = selectedDataset?.waves ?? [];
  const typedWaves = useMemo(() => parseWaveList(newDatasetWaves), [newDatasetWaves]);
  const pickerWaves = datasetMode === 'new' ? typedWaves : datasetWaves;

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
          wavesForGroup = typedWaves;
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
          <label
            htmlFor="experiment-group"
            style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}
          >
            Experiment group{' '}
            <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span>
          </label>
          <select
            id="experiment-group"
            data-testid="group-picker"
            value={groupMode === 'new' ? 'new' : groupMode === 'existing' && value.group_id != null ? String(value.group_id) : ''}
            onChange={(e) => {
              const next = e.target.value;
              if (next === 'new') {
                setGroupMode('new');
                onChange({ ...value, group_id: null });
                return;
              }
              if (next === '') {
                setGroupMode('none');
                onChange({ ...value, group_id: null });
                return;
              }
              setGroupMode('existing');
              onChange({ ...value, group_id: Number(next) });
            }}
            style={{
              width: '100%',
              padding: '11px 13px',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              font: '400 15px var(--font-body)',
              color: 'var(--ink)',
            }}
          >
            <option value="">No group (scratch / pilot)</option>
            {groups.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name} · {group.dataset_name} · {group.wave}
              </option>
            ))}
            <option value="new">Create new group…</option>
          </select>
          <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7 }}>
            Groups are a dataset × wave. Skip this for scratch work.
          </div>
        </div>

        {groupMode === 'new' && (
          <div
            style={{
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              padding: '14px 14px 2px',
              marginBottom: 16,
              background: 'var(--surface-2)',
            }}
          >
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
              <label
                htmlFor="new-group-dataset"
                style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}
              >
                Dataset
              </label>
              <select
                id="new-group-dataset"
                data-testid="new-group-dataset"
                value={datasetMode === 'new' ? 'new' : datasetId === '' ? '' : String(datasetId)}
                onChange={(e) => {
                  const next = e.target.value;
                  if (next === 'new') {
                    setDatasetMode('new');
                    setDatasetId('');
                    setNewGroupWave('');
                    return;
                  }
                  setDatasetMode('existing');
                  setDatasetId(next ? Number(next) : '');
                  setNewGroupWave('');
                }}
                required
                style={{
                  width: '100%',
                  padding: '11px 13px',
                  border: '1px solid var(--faint)',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--surface)',
                  font: '400 15px var(--font-body)',
                  color: 'var(--ink)',
                }}
              >
                <option value="">Select a dataset…</option>
                {datasets.map((dataset) => (
                  <option key={dataset.id} value={dataset.id}>
                    {dataset.name}
                    {dataset.waves.length ? ` (${dataset.waves.join(', ')})` : ''}
                  </option>
                ))}
                <option value="new">Create new dataset…</option>
              </select>
            </div>
            {datasetMode === 'new' && (
              <>
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
                <Field
                  id="new-dataset-waves"
                  testId="new-dataset-waves-input"
                  label="Waves"
                  hint="Comma-separated tokens, e.g. fall25, sp26."
                  value={newDatasetWaves}
                  onChange={(v) => {
                    setNewDatasetWaves(v);
                    const next = parseWaveList(v);
                    if (newGroupWave && !next.includes(newGroupWave)) setNewGroupWave('');
                  }}
                  placeholder="fall25"
                  required
                />
              </>
            )}
            {pickerWaves.length > 1 && (
              <div style={{ marginBottom: 16 }}>
                <label
                  htmlFor="new-group-wave"
                  style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}
                >
                  Wave
                </label>
                <select
                  id="new-group-wave"
                  data-testid="new-group-wave"
                  value={newGroupWave}
                  onChange={(e) => setNewGroupWave(e.target.value)}
                  required
                  style={{
                    width: '100%',
                    padding: '11px 13px',
                    border: '1px solid var(--faint)',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--surface)',
                    font: '400 15px var(--font-body)',
                    color: 'var(--ink)',
                  }}
                >
                  <option value="">Select a wave…</option>
                  {pickerWaves.map((wave) => (
                    <option key={wave} value={wave}>
                      {wave}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {datasetMode === 'existing' && selectedDataset && datasetWaves.length === 0 && (
              <div style={{ fontSize: 12.5, color: 'var(--danger)', marginBottom: 16 }}>
                This dataset has no waves yet. Create a new dataset (or add waves via the API)
                before opening a group.
              </div>
            )}
          </div>
        )}

        <div style={{ marginBottom: 16 }}>
          <label
            htmlFor="assistance-method"
            style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}
          >
            Assistance method
          </label>
          <select
            id="assistance-method"
            data-testid="assistance-method-select"
            value={chosenMethod}
            onChange={(e) => onChange({ ...value, assistance_method: e.target.value })}
            style={{
              width: '100%',
              padding: '11px 13px',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              font: '400 15px var(--font-body)',
              color: 'var(--ink)',
            }}
          >
            {ASSISTANCE_METHODS.map((method) => {
              const already = methodsInGroup.has(method.value);
              return (
                <option key={method.value} value={method.value}>
                  {already ? `${method.label} — already in group` : method.label}
                </option>
              );
            })}
          </select>
          {selectedGroup && (
            <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7 }}>
              In this group:{' '}
              {ASSISTANCE_METHODS.filter((m) => !methodsInGroup.has(m.value))
                .map((m) => m.label)
                .join(', ') || 'every method is already used'}
              {ASSISTANCE_METHODS.some((m) => !methodsInGroup.has(m.value))
                ? ' still missing.'
                : '.'}
            </div>
          )}
          {methodAlreadyInGroup && (
            <div
              data-testid="duplicate-method-warning"
              style={{ fontSize: 12.5, color: AMBER, marginTop: 7 }}
            >
              This group already has a {methodLabel(chosenMethod)} experiment. Duplicates are
              allowed (param variants, re-collections) but usually you want a missing method.
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

function EmptyState({ text }: { text: string }) {
  return (
    <div style={{ padding: '52px 20px', textAlign: 'center', color: 'var(--muted)', fontSize: 14 }}>
      {text}
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
