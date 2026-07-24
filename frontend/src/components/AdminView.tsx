import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import type { Experiment, ExperimentCreate, ExperimentStatus } from '../types';
import StatusLabel from './StatusLabel';
import RowActionMenu from './RowActionMenu';
import ConfirmDialog from './ConfirmDialog';

type ListTab = 'active' | 'archived';

// A pending row action awaiting confirmation in the modal.
type PendingAction = {
  type: 'delete' | 'archive' | 'unarchive';
  exp: Experiment;
};

/**
 * Admin dashboard: create-new panel on the left, existing experiments on the
 * right. Clicking a row navigates into ExperimentDetail. The `internal_name`
 * (researcher-facing) is preferred as the row label — the public name still
 * shows underneath so the two aren't confused.
 */
function AdminView() {
  const navigate = useNavigate();
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<ListTab>('active');
  const [statusFilter, setStatusFilter] = useState<ExperimentStatus | 'ALL'>('ALL');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);

  const [newExperiment, setNewExperiment] = useState<ExperimentCreate>({
    name: '',
    internal_name: '',
    num_ratings_per_question: 3,
    prolific_completion_url: '',
  });

  // Debounce the search box so we fire one request after typing settles,
  // not one per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    loadExperiments();
    // loadExperiments reads tab/statusFilter/debouncedSearch off state; those
    // are the only inputs that change what the server returns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, statusFilter, debouncedSearch]);

  const filtersActive = statusFilter !== 'ALL' || debouncedSearch.trim() !== '';

  const loadExperiments = async () => {
    try {
      setLoading(true);
      const data = await api.listExperiments({
        archived: tab === 'archived',
        status: statusFilter === 'ALL' ? undefined : statusFilter,
        search: debouncedSearch.trim() || undefined,
      });
      setExperiments(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  // Runs the confirmed row action (delete / archive / unarchive), then reloads
  // the current tab so the row moves to (or disappears from) the right list.
  const runPendingAction = async () => {
    if (!pending) return;
    setError(null);
    setBusy(true);
    try {
      if (pending.type === 'delete') {
        await api.deleteExperiment(pending.exp.id);
      } else if (pending.type === 'archive') {
        await api.archiveExperiment(pending.exp.id);
      } else {
        await api.unarchiveExperiment(pending.exp.id);
      }
      setPending(null);
      await loadExperiments();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setPending(null);
    } finally {
      setBusy(false);
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
        <p
          style={{ margin: '6px 0 0', fontSize: 15, color: 'var(--muted)' }}
        >
          Create and manage your rating experiments.
        </p>
      </div>

      {error && <ErrorBanner text={error} />}

      <div style={{ display: 'grid', gridTemplateColumns: '410px 1fr', gap: 28, alignItems: 'start' }}>
        <CreatePanel
          value={newExperiment}
          onChange={setNewExperiment}
          onSubmit={handleCreateExperiment}
        />
        <ListPanel
          experiments={experiments}
          loading={loading}
          tab={tab}
          onTabChange={setTab}
          search={search}
          onSearchChange={setSearch}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          filtersActive={filtersActive}
          onSelect={(exp) => navigate(`/admin/experiments/${exp.id}`)}
          onRequestAction={(type, exp) => setPending({ type, exp })}
        />
      </div>

      {pending && (
        <ConfirmDialog
          {...confirmCopy(pending)}
          tone={pending.type === 'delete' ? 'danger' : 'default'}
          busy={busy}
          onConfirm={runPendingAction}
          onCancel={() => {
            setBusy(false);
            setPending(null);
          }}
        />
      )}
    </div>
  );
}

// Modal wording per action. Delete is destructive (danger); archive/unarchive
// are reversible state changes.
function confirmCopy(pending: PendingAction): {
  title: string;
  message: React.ReactNode;
  confirmLabel: string;
} {
  const label = pending.exp.internal_name || pending.exp.name;
  if (pending.type === 'delete') {
    return {
      title: 'Delete experiment',
      message: (
        <>
          Delete <strong>{label}</strong>? This permanently removes its questions, ratings, and
          any linked Prolific studies. This cannot be undone.
        </>
      ),
      confirmLabel: 'Delete',
    };
  }
  if (pending.type === 'archive') {
    return {
      title: 'Archive experiment',
      message: (
        <>
          Archive <strong>{label}</strong>? It will be hidden from the active list but kept
          intact. You can restore it from the Archived tab.
        </>
      ),
      confirmLabel: 'Archive',
    };
  }
  return {
    title: 'Restore experiment',
    message: (
      <>
        Restore <strong>{label}</strong> to the active list?
      </>
    ),
    confirmLabel: 'Restore',
  };
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

const STATUS_FILTERS: { value: ExperimentStatus | 'ALL'; label: string }[] = [
  { value: 'ALL', label: 'All statuses' },
  { value: 'DRAFT', label: 'Draft' },
  { value: 'LAUNCH', label: 'Launched' },
  { value: 'FINISHED', label: 'Finished' },
];

function ListPanel({
  experiments,
  loading,
  tab,
  onTabChange,
  search,
  onSearchChange,
  statusFilter,
  onStatusFilterChange,
  filtersActive,
  onSelect,
  onRequestAction,
}: {
  experiments: Experiment[];
  loading: boolean;
  tab: ListTab;
  onTabChange: (tab: ListTab) => void;
  search: string;
  onSearchChange: (value: string) => void;
  statusFilter: ExperimentStatus | 'ALL';
  onStatusFilterChange: (value: ExperimentStatus | 'ALL') => void;
  filtersActive: boolean;
  onSelect: (exp: Experiment) => void;
  onRequestAction: (type: PendingAction['type'], exp: Experiment) => void;
}) {
  const emptyText = filtersActive
    ? 'No experiments match your filters.'
    : tab === 'archived'
      ? 'No archived experiments.'
      : 'No experiments yet. Create one to get started.';

  return (
    <section
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
      }}
    >
      <div
        style={{
          display: 'flex',
          gap: 4,
          padding: '10px 16px 0',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <ListTabButton label="Active" active={tab === 'active'} onClick={() => onTabChange('active')} />
        <ListTabButton
          label="Archived"
          active={tab === 'archived'}
          onClick={() => onTabChange('archived')}
        />
      </div>
      <div
        style={{
          display: 'flex',
          gap: 10,
          padding: '14px 16px',
          borderBottom: '1px solid var(--line)',
        }}
      >
        <input
          type="search"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Search by name…"
          aria-label="Search experiments by name"
          style={{
            flex: 1,
            minWidth: 0,
            padding: '8px 11px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface)',
            font: '400 13.5px var(--font-body)',
            color: 'var(--ink)',
          }}
        />
        <select
          value={statusFilter}
          onChange={(e) => onStatusFilterChange(e.target.value as ExperimentStatus | 'ALL')}
          aria-label="Filter by status"
          style={{
            padding: '8px 11px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface)',
            font: '400 13.5px var(--font-body)',
            color: 'var(--ink)',
            cursor: 'pointer',
          }}
        >
          {STATUS_FILTERS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
      {loading ? (
        <EmptyState text="Loading…" />
      ) : experiments.length === 0 ? (
        <EmptyState text={emptyText} />
      ) : (
        <div>
          {experiments.map((exp, idx) => (
            <ExperimentRow
              key={exp.id}
              exp={exp}
              onClick={() => onSelect(exp)}
              onRequestAction={onRequestAction}
              isLast={idx === experiments.length - 1}
            />
          ))}
        </div>
      )}
    </section>
  );
}

/** Underlined tab button toggling the Active / Archived list. */
function ListTabButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: '12px 14px',
        border: 'none',
        borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
        background: 'transparent',
        color: active ? 'var(--ink)' : 'var(--muted)',
        font: '600 13px var(--font-body)',
        cursor: 'pointer',
        marginBottom: -1,
      }}
    >
      {label}
    </button>
  );
}

function ExperimentRow({
  exp,
  onClick,
  onRequestAction,
  isLast,
}: {
  exp: Experiment;
  onClick: () => void;
  onRequestAction: (type: PendingAction['type'], exp: Experiment) => void;
  isLast: boolean;
}) {
  const isArchived = exp.archived_at !== null;
  return (
    <div
      onClick={onClick}
      onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--surface-2)')}
      onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
      style={{
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        gap: 18,
        padding: '18px 24px',
        borderBottom: isLast ? 'none' : '1px solid var(--line)',
        cursor: 'pointer',
        transition: 'background 0.15s',
      }}
    >
      {exp.needs_attention && <AttentionDot reason={exp.attention_reason} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
          <span
            style={{
              fontWeight: 700,
              fontSize: 15.5,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {exp.internal_name || exp.name}
          </span>
          <StatusLabel status={exp.status} size="sm" />
        </div>
        {exp.internal_name && (
          <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 2 }}>
            Public: {exp.name}
          </div>
        )}
        <div style={{ fontSize: 13, color: 'var(--muted)' }}>
          {exp.question_count} questions · {exp.rating_count} ratings
        </div>
      </div>
      <RowActionMenu
        label={`Actions for ${exp.internal_name || exp.name}`}
        actions={[
          { label: 'View', testId: 'row-action-view', onSelect: onClick },
          isArchived
            ? {
                label: 'Restore',
                testId: 'row-action-unarchive',
                onSelect: () => onRequestAction('unarchive', exp),
              }
            : {
                label: 'Archive',
                testId: 'row-action-archive',
                onSelect: () => onRequestAction('archive', exp),
              },
          {
            label: 'Delete',
            tone: 'danger',
            testId: 'row-action-delete',
            onSelect: () => onRequestAction('delete', exp),
          },
        ]}
      />
    </div>
  );
}

/**
 * Amber "action needed" dot floated in the top-left corner of an experiment
 * row when the backend flags a pending admin action. Tucked into the left
 * gutter so it clears the title text (which starts at the 24px content
 * padding). Hovering reveals the reason, mirroring the StatusLabel tooltip.
 * Only rendered when there's something to flag (the caller guards on
 * `needs_attention`).
 */
function AttentionDot({ reason }: { reason: string | null }) {
  const [hovered, setHovered] = useState(false);

  if (!reason) return null;

  return (
    <span
      style={{ position: 'absolute', top: 12, left: 9, display: 'inline-flex' }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <span
        role="img"
        aria-label={`Action needed: ${reason}`}
        tabIndex={0}
        data-testid="experiment-attention-dot"
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        style={{
          width: 11,
          height: 11,
          borderRadius: '50%',
          background: 'var(--warn)',
          boxShadow: '0 0 0 4px var(--warn-soft)',
          cursor: 'help',
        }}
      />
      {hovered && (
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
              color: 'var(--warn-soft)',
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
    <div style={{ padding: '40px 20px', textAlign: 'center', color: 'var(--muted)', fontSize: 14 }}>
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
      <label
        htmlFor={id}
        style={{ display: 'block', fontSize: 13, fontWeight: 600, marginBottom: 7 }}
      >
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
      {hint && (
        <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 7 }}>{hint}</div>
      )}
    </div>
  );
}

export default AdminView;
