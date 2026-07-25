import { useState, useEffect, useMemo, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import type { Experiment, ExperimentCreate, ExperimentStatus } from '../types';
import StatusLabel from './StatusLabel';
import RowActionMenu from './RowActionMenu';
import ConfirmDialog from './ConfirmDialog';

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
const FILTER_STORAGE_KEY = 'hrp.experiments.filters.v1';
type Filters = { query: string; statusFilter: StatusTab; needsOnly: boolean };
const DEFAULT_FILTERS: Filters = { query: '', statusFilter: 'ALL', needsOnly: false };

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
    };
  } catch {
    return DEFAULT_FILTERS;
  }
}

// Guard against a missing spend field (e.g. a frontend/backend version skew):
// treat it as 0 rather than rendering "NaN".
const formatSpend = (minorUnits: number | undefined, symbol: string) =>
  `${symbol}${((minorUnits ?? 0) / 100).toFixed(2)}`;

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

  const [query, setQuery] = useState(() => loadFilters().query);
  const [statusFilter, setStatusFilter] = useState<StatusTab>(() => loadFilters().statusFilter);
  const [needsOnly, setNeedsOnly] = useState(() => loadFilters().needsOnly);

  // Delete is the one destructive/irreversible action, so it still confirms;
  // archive/restore apply immediately with a toast (per the mock).
  const [pendingDelete, setPendingDelete] = useState<Experiment | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [newExperiment, setNewExperiment] = useState<ExperimentCreate>({
    name: '',
    internal_name: '',
    num_ratings_per_question: 3,
    prolific_completion_url: '',
  });

  useEffect(() => {
    loadExperiments();
    api
      .getPlatformStatus()
      .then((s) => setCurrencySymbol(s.currency_symbol || '$'))
      .catch(() => {});
    return () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, []);

  // Persist filters so they survive a refresh.
  useEffect(() => {
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify({ query, statusFilter, needsOnly }));
  }, [query, statusFilter, needsOnly]);

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

  const flash = (message: string) => {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  };

  const label = (exp: Experiment) => exp.internal_name || exp.name;

  const handleArchiveToggle = async (exp: Experiment) => {
    const toArchived = !exp.archived_at;
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

  const handleCreateExperiment = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      // Backend normalises whitespace/empty → null for internal_name on both
      // create and update, so we just forward the form value as-typed.
      const created = await api.createExperiment(newExperiment);
      setNewExperiment({
        name: '',
        internal_name: '',
        num_ratings_per_question: 3,
        prolific_completion_url: '',
      });
      navigate(`/admin/experiments/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const archivedCount = useMemo(
    () => experiments.filter((e) => Boolean(e.archived_at)).length,
    [experiments],
  );

  // Client-side filtering mirrors the design mock: ARCHIVED flips the source to
  // archived rows; otherwise we filter non-archived rows by status, needs-only,
  // and a name substring against both the internal and public name.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const showingArchived = statusFilter === 'ARCHIVED';
    // Boolean() so a missing/undefined archived_at counts as active, not
    // archived (which would otherwise hide every row under the Archived tab).
    const source = experiments.filter((e) =>
      showingArchived ? Boolean(e.archived_at) : !e.archived_at,
    );
    return source.filter((e) => {
      if (statusFilter !== 'ALL' && statusFilter !== 'ARCHIVED' && e.status !== statusFilter)
        return false;
      if (needsOnly && !e.needs_attention) return false;
      if (q && !((e.internal_name || '').toLowerCase().includes(q) || e.name.toLowerCase().includes(q)))
        return false;
      return true;
    });
  }, [experiments, query, statusFilter, needsOnly]);

  const totalSpendMinor = useMemo(
    () => filtered.reduce((sum, e) => sum + (e.spend_minor_units ?? 0), 0),
    [filtered],
  );

  const filtersActive = query.trim() !== '' || statusFilter !== 'ALL' || needsOnly;

  const clearFilters = () => {
    setQuery('');
    setStatusFilter('ALL');
    setNeedsOnly(false);
  };

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
        <CreatePanel value={newExperiment} onChange={setNewExperiment} onSubmit={handleCreateExperiment} />
        <ListPanel
          experiments={filtered}
          loading={loading}
          currencySymbol={currencySymbol}
          totalSpendLabel={formatSpend(totalSpendMinor, currencySymbol)}
          query={query}
          onQueryChange={setQuery}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          archivedCount={archivedCount}
          needsOnly={needsOnly}
          onToggleNeeds={() => setNeedsOnly((v) => !v)}
          filtersActive={filtersActive}
          onClearFilters={clearFilters}
          onSelect={(exp) => navigate(`/admin/experiments/${exp.id}`)}
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
  totalSpendLabel,
  query,
  onQueryChange,
  statusFilter,
  onStatusFilterChange,
  archivedCount,
  needsOnly,
  onToggleNeeds,
  filtersActive,
  onClearFilters,
  onSelect,
  onArchiveToggle,
  onDelete,
}: {
  experiments: Experiment[];
  loading: boolean;
  currencySymbol: string;
  totalSpendLabel: string;
  query: string;
  onQueryChange: (value: string) => void;
  statusFilter: StatusTab;
  onStatusFilterChange: (value: StatusTab) => void;
  archivedCount: number;
  needsOnly: boolean;
  onToggleNeeds: () => void;
  filtersActive: boolean;
  onClearFilters: () => void;
  onSelect: (exp: Experiment) => void;
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
        ) : (
          experiments.map((exp, idx) => (
            <ExperimentRow
              key={exp.id}
              exp={exp}
              currencySymbol={currencySymbol}
              isLast={idx === experiments.length - 1}
              onSelect={() => onSelect(exp)}
              onArchiveToggle={() => onArchiveToggle(exp)}
              onDelete={() => onDelete(exp)}
            />
          ))
        )}
      </div>
    </section>
  );
}

function ExperimentRow({
  exp,
  currencySymbol,
  isLast,
  onSelect,
  onArchiveToggle,
  onDelete,
}: {
  exp: Experiment;
  currencySymbol: string;
  isLast: boolean;
  onSelect: () => void;
  onArchiveToggle: () => void;
  onDelete: () => void;
}) {
  const isArchived = Boolean(exp.archived_at);
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
        padding: '20px 24px',
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
            {formatSpend(exp.spend_minor_units, currencySymbol)}
          </div>
          <div style={{ fontSize: 12, color: 'var(--muted)', letterSpacing: '0.02em' }}>spent</div>
        </div>
        <RowActionMenu
          label={`Actions for ${exp.internal_name || exp.name}`}
          actions={[
            { label: 'View', testId: 'row-action-view', onSelect },
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
}: {
  value: ExperimentCreate;
  onChange: (v: ExperimentCreate) => void;
  onSubmit: (e: React.FormEvent) => void;
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
      <SectionHeader label="Create new" />
      <form onSubmit={onSubmit} style={{ padding: 24 }}>
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
          style={{
            width: '100%',
            padding: 13,
            background: 'var(--accent)',
            color: 'var(--accent-ink)',
            border: 'none',
            borderRadius: 'var(--radius-sm)',
            fontWeight: 600,
            fontSize: 15,
            cursor: 'pointer',
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
