import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, UploadAbortedError } from '../api';
import StatusLabel from './StatusLabel';
import type {
  DatasetMetaField,
  Experiment,
  ExperimentRound,
  ExperimentStats,
  PilotStudyCreate,
  RecommendationResponse,
  Screener,
  Upload,
} from '../types';
import { ExperimentExclusionPicker } from './experiment-detail/ExclusionPicker';
import {
  StepperTabs,
  type StepDef,
  type StepStatus,
  type TabKey,
} from './experiment-detail/StepperTabs';
import {
  Banner,
  Field,
  ProgressBar,
  SectionCard,
  Toast,
  ToggleSwitch,
  inputStyle,
  primaryButton,
  secondaryButton,
  textareaStyle,
} from './experiment-detail/ui';
import {
  rewardDecimals,
  rewardHintText,
  rewardInputToMinor,
  rewardMinorToInput,
} from './experiment-detail/reward';

// Labels shown to admins in the Instructions & prompts panel. Order matches
// the CSV `#META:` JSON shape that researchers see in the colab guide.
const DATASET_META_LABELS: Record<DatasetMetaField, string> = {
  description: 'Dataset description',
  system_prompt: 'AI system prompt',
  human_prompt_prefix: 'Question prefix (shown above)',
  human_prompt_suffix: 'Question suffix (shown below)',
  prolific_pool: 'Prolific participant pool',
};

const DATASET_META_HINTS: Record<DatasetMetaField, string> = {
  description:
    'Shown to raters on the intro screen and inherited as the default by each round on Launch on Prolific. Edit a round in Launch to override for that round only. Supports markdown.',
  system_prompt:
    'Used to generate AI answers and assistance. Plain text; line breaks preserved.',
  human_prompt_prefix: 'Rendered above every question. Plain text; line breaks preserved.',
  human_prompt_suffix: 'Rendered below every question. Plain text; line breaks preserved.',
  prolific_pool:
    'For your reference when configuring Prolific filters; not sent to the Prolific API.',
};

// Placeholder text shown inside empty fields — matches the mock's design of
// letting the field's example serve as its own hint before the user types.
const DATASET_META_PLACEHOLDERS: Partial<Record<DatasetMetaField, string>> = {
  human_prompt_prefix: 'e.g. When you see the text below, do you think x or y?',
  human_prompt_suffix: 'e.g. Is the statement above true or false?',
  prolific_pool: 'e.g. uk_representative_sample',
};

// Fields grouped by *where they surface* — a researcher editing the tab is
// usually thinking "how do I change what raters see on the splash?" or "the
// prompt around each question", not "the ordering the CSV uses". Group names
// answer that question; single-field groups (system prompt, pool) still get a
// header so someone with assistance off knows they can skip the AI row.
const META_FIELD_GROUPS: {
  header: string;
  fields: { field: DatasetMetaField; kind: 'input' | 'textarea'; minHeight?: number }[];
}[] = [
  {
    header: 'Instructions',
    fields: [
      { field: 'description', kind: 'textarea', minHeight: 140 },
      { field: 'system_prompt', kind: 'textarea', minHeight: 140 },
    ],
  },
  {
    header: 'Per-question framing',
    fields: [
      { field: 'human_prompt_prefix', kind: 'textarea', minHeight: 90 },
      { field: 'human_prompt_suffix', kind: 'textarea', minHeight: 90 },
    ],
  },
  {
    header: 'Deployment',
    fields: [{ field: 'prolific_pool', kind: 'input' }],
  },
];

interface ExperimentDetailProps {
  experiment: Experiment;
  allExperiments: Experiment[];
  onBack: () => void;
  onDeleted: () => void;
  onRefresh: () => Promise<unknown>;
}

// Small square pencil button used to open an inline name editor in the header.
function PencilButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 26,
        height: 26,
        padding: 0,
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--surface)',
        color: 'var(--muted)',
        cursor: 'pointer',
        flex: '0 0 auto',
      }}
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z" />
      </svg>
    </button>
  );
}

// Inline text editor for the header names. Enter saves, Escape cancels. `big`
// styles the input to match the h1 title; otherwise it matches the muted
// subtitle line.
function NameEditor({
  value,
  onChange,
  onSave,
  onCancel,
  saving,
  placeholder,
  ariaLabel,
  big = false,
}: {
  value: string;
  onChange: (v: string) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  placeholder?: string;
  ariaLabel: string;
  big?: boolean;
}) {
  const iconButton: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 30,
    height: 30,
    padding: 0,
    borderRadius: 'var(--radius-sm)',
    cursor: saving ? 'not-allowed' : 'pointer',
    flex: '0 0 auto',
    fontSize: 14,
  };
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0 }}>
      <input
        autoFocus
        type="text"
        value={value}
        placeholder={placeholder}
        maxLength={255}
        disabled={saving}
        aria-label={ariaLabel}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            onSave();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            onCancel();
          }
        }}
        style={{
          ...inputStyle,
          flex: 1,
          minWidth: 0,
          ...(big
            ? {
                fontFamily: 'var(--font-head)',
                fontWeight: 600,
                fontSize: 24,
                letterSpacing: '-0.01em',
                padding: '6px 10px',
              }
            : { fontSize: 14, padding: '5px 9px' }),
        }}
      />
      <button
        type="button"
        onClick={onSave}
        disabled={saving}
        title="Save"
        aria-label="Save name"
        style={{
          ...iconButton,
          border: '1px solid var(--accent)',
          background: 'var(--accent)',
          color: '#fff',
          opacity: saving ? 0.6 : 1,
        }}
      >
        ✓
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={saving}
        title="Cancel"
        aria-label="Cancel editing name"
        style={{
          ...iconButton,
          border: '1px solid var(--faint)',
          background: 'var(--surface)',
          color: 'var(--muted)',
        }}
      >
        ✕
      </button>
    </div>
  );
}

// The four setup steps, in the order the user should complete them. Overview
// and Danger sit outside this sequence.
const SETUP_STEPS: { key: TabKey; label: string }[] = [
  { key: 'questions', label: 'Questions' },
  { key: 'instructions', label: 'Instructions & prompts' },
  { key: 'assistance', label: 'Rater assistance' },
  { key: 'launch', label: 'Launch on Prolific' },
];

function ExperimentDetail({
  experiment,
  allExperiments,
  onBack,
  onDeleted,
  onRefresh,
}: ExperimentDetailProps) {
  const navigate = useNavigate();
  // ── Data state ─────────────────────────────────────────────────────────
  const [stats, setStats] = useState<ExperimentStats | null>(null);
  // Spend starts from the last-known list value and is refreshed in the
  // background from Prolific when the experiment opens (covers closed rounds).
  const [spendMinor, setSpendMinor] = useState<number | null>(
    experiment.spend_minor_units ?? null,
  );
  const [spendSyncing, setSpendSyncing] = useState(false);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [rounds, setRounds] = useState<ExperimentRound[]>([]);
  const [recommendation, setRecommendation] = useState<RecommendationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // Success toasts auto-dismiss. Track the pending timer so rapid-fire actions
  // don't let an earlier timer clear a later toast prematurely.
  const successTimerRef = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (successTimerRef.current !== null) window.clearTimeout(successTimerRef.current);
    };
  }, []);
  const clearSuccess = useCallback(() => {
    if (successTimerRef.current !== null) {
      window.clearTimeout(successTimerRef.current);
      successTimerRef.current = null;
    }
    setSuccess(null);
  }, []);
  const showSuccess = useCallback((message: string, durationMs: number = 3000) => {
    if (successTimerRef.current !== null) window.clearTimeout(successTimerRef.current);
    setSuccess(message);
    successTimerRef.current = window.setTimeout(() => {
      setSuccess(null);
      successTimerRef.current = null;
    }, durationMs);
  }, []);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  // Non-null only while an upload is in flight. `phase` splits the wait into
  // the part we can measure (bytes on the wire) and the part we can't (server
  // parsing and inserting rows), so the UI can say which one is happening.
  const [uploadState, setUploadState] = useState<UploadState | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const [includePreview, setIncludePreview] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [tab, setTab] = useState<TabKey>('overview');

  // Inline name editing in the header. `editingName` tracks which of the two
  // names (public `name` vs private `internal_name`) is open for edit, if any;
  // `nameDraft` holds the working value until Save.
  const [editingName, setEditingName] = useState<null | 'name' | 'internal_name'>(null);
  const [nameDraft, setNameDraft] = useState('');
  const [savingName, setSavingName] = useState(false);

  const handleStartEditName = (field: 'name' | 'internal_name') => {
    setError(null);
    setEditingName(field);
    setNameDraft(field === 'name' ? experiment.name : experiment.internal_name ?? '');
  };

  const handleSaveName = async () => {
    if (editingName === null) return;
    const trimmed = nameDraft.trim();
    if (editingName === 'name' && !trimmed) {
      setError('Public name cannot be empty.');
      return;
    }
    setSavingName(true);
    setError(null);
    try {
      await api.updateExperiment(experiment.id, {
        assistance_method: experiment.assistance_method,
        assistance_params: experiment.assistance_params ?? null,
        ...(editingName === 'name' ? { name: trimmed } : { internal_name: trimmed }),
      });
      showSuccess(
        editingName === 'name' ? 'Public name updated.' : 'Internal name updated.',
        2000,
      );
      setEditingName(null);
      await onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update name');
    } finally {
      setSavingName(false);
    }
  };

  const [publishingRoundId, setPublishingRoundId] = useState<number | null>(null);
  const [closingRoundId, setClosingRoundId] = useState<number | null>(null);
  const [editingRoundId, setEditingRoundId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<{
    description: string;
    estimated_completion_time: number;
    places: number;
    study_label: PilotStudyCreate['study_label'];
    screeners: Screener[];
    excluded_experiment_ids: number[];
  }>({
    description: '',
    estimated_completion_time: 60,
    places: 1,
    study_label: 'annotation',
    screeners: [],
    excluded_experiment_ids: [],
  });
  const otherExperiments = allExperiments.filter((e) => e.id !== experiment.id);
  // Reward is kept as a raw input string (not a number) so the user can type
  // "9.99" without each keystroke round-tripping through parse-then-format
  // and snapping the value back to "9.00". Converted to minor units only at
  // the submit boundary in handleSaveEditRound / handleRunPilot.
  const [editRewardInput, setEditRewardInput] = useState<string>('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [prolificEnabled, setProlificEnabled] = useState<'loading' | boolean>('loading');
  const [platformStatusMessage, setPlatformStatusMessage] = useState<string | null>(null);
  const [currencyCode, setCurrencyCode] = useState<string | null>(null);
  const [currencySymbol, setCurrencySymbol] = useState<string | null>(null);
  const [pilotForm, setPilotForm] = useState<Omit<PilotStudyCreate, 'reward'>>({
    description: experiment.description ?? '',
    estimated_completion_time: 60,
    pilot_places: 5,
    device_compatibility: ['desktop'],
    study_label: 'annotation',
    screeners: ['ai_taskers', 'fact_checkers', 'approval_rate'],
    excluded_experiment_ids: [],
  });
  // Keep the pilot description in sync with the experiment's dataset-level
  // description while the admin hasn't typed their own version. Set true on
  // any admin edit to the pilot description field; unset on a successful pilot
  // launch (the form is hidden after that anyway, but the reset lets a future
  // re-open pick up prop changes again).
  const pilotDescriptionDirtyRef = useRef(false);
  useEffect(() => {
    if (pilotDescriptionDirtyRef.current) return;
    setPilotForm((prev) => ({ ...prev, description: experiment.description ?? '' }));
  }, [experiment.description]);
  const [pilotRewardInput, setPilotRewardInput] = useState<string>('9.00');
  // Re-format the pilot default once when the workspace currency arrives,
  // so a JPY workspace sees "900" (≈¥900) instead of "9.00" (≈¥9). Guarded
  // by a ref so we never clobber what the researcher has typed.
  const pilotRewardInitedRef = useRef(false);
  useEffect(() => {
    if (currencyCode !== null && !pilotRewardInitedRef.current) {
      pilotRewardInitedRef.current = true;
      setPilotRewardInput(rewardMinorToInput(900, currencyCode));
    }
  }, [currencyCode]);

  const [humanAsATool, setHumanAsATool] = useState(
    experiment.assistance_method === 'human_as_a_tool'
  );
  const [topNEnabled, setTopNEnabled] = useState(experiment.assistance_method === 'top_n');
  const [topNValue, setTopNValue] = useState<number>(
    Number(experiment.assistance_params?.n ?? 3)
  );
  const [confidenceMethod, setConfidenceMethod] = useState<string>(
    (experiment.assistance_params?.confidence_method as string) ?? 'self_report'
  );

  // Resync toggles when the experiment prop changes. Without this the useState
  // initializers only run at mount, so any refresh path that keeps the component
  // mounted (e.g. the CSV upload flow, saveAssistanceMethod's own refresh) would
  // let local state drift from the server-side assistance_method.
  useEffect(() => {
    setHumanAsATool(experiment.assistance_method === 'human_as_a_tool');
    setTopNEnabled(experiment.assistance_method === 'top_n');
    setTopNValue(Number(experiment.assistance_params?.n ?? 3));
    setConfidenceMethod(
      (experiment.assistance_params?.confidence_method as string) ?? 'self_report'
    );
  }, [experiment.assistance_method, experiment.assistance_params]);

  // Assistance step is considered "done" once the user has opened the panel
  // and made an explicit choice (even if that choice is "None"). We track
  // this per-experiment in sessionStorage so the stepper doesn't nag on
  // repeat visits within a session; the model's assistance_method itself
  // doesn't distinguish "default none" from "explicitly picked none".
  const assistanceSessionKey = `assist-visited-${experiment.id}`;
  const [assistanceTouched, setAssistanceTouched] = useState<boolean>(
    () => sessionStorage.getItem(assistanceSessionKey) === '1',
  );
  const markAssistanceTouched = () => {
    sessionStorage.setItem(assistanceSessionKey, '1');
    setAssistanceTouched(true);
  };

  // Dataset metadata edits live in local form state and are committed via Save.
  const [metaForm, setMetaForm] = useState<Record<DatasetMetaField, string>>({
    description: experiment.description ?? '',
    system_prompt: experiment.system_prompt ?? '',
    human_prompt_prefix: experiment.human_prompt_prefix ?? '',
    human_prompt_suffix: experiment.human_prompt_suffix ?? '',
    prolific_pool: experiment.prolific_pool ?? '',
  });
  const [savingMeta, setSavingMeta] = useState(false);
  const metaFormDirtyRef = useRef(false);

  useEffect(() => {
    if (metaFormDirtyRef.current) return;
    setMetaForm({
      description: experiment.description ?? '',
      system_prompt: experiment.system_prompt ?? '',
      human_prompt_prefix: experiment.human_prompt_prefix ?? '',
      human_prompt_suffix: experiment.human_prompt_suffix ?? '',
      prolific_pool: experiment.prolific_pool ?? '',
    });
  }, [
    experiment.description,
    experiment.system_prompt,
    experiment.human_prompt_prefix,
    experiment.human_prompt_suffix,
    experiment.prolific_pool,
  ]);

  // ── Data-loading effects ───────────────────────────────────────────────
  const loadStats = useCallback(async () => {
    try {
      const data = await api.getExperimentStats(experiment.id, { includePreview });
      setStats(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  }, [experiment.id, includePreview]);

  const loadUploads = useCallback(async () => {
    try {
      const data = await api.listUploads(experiment.id);
      setUploads(data);
    } catch (err) {
      setUploads([]);
      setError(err instanceof Error ? err.message : 'Failed to load uploads');
    }
  }, [experiment.id]);

  // Refresh spend from Prolific in the background and update the card. Gated on
  // prolificEnabled (the product is Prolific-only); keeps the last-known value
  // if the refresh fails.
  const syncSpend = useCallback(async () => {
    if (prolificEnabled !== true) return;
    setSpendSyncing(true);
    try {
      const { spend_minor_units } = await api.syncExperimentSpend(experiment.id);
      setSpendMinor(spend_minor_units);
    } catch {
      // keep the last-known spend
    } finally {
      setSpendSyncing(false);
    }
  }, [experiment.id, prolificEnabled]);

  const loadRounds = useCallback(async () => {
    try {
      const data = await api.listExperimentRounds(experiment.id);
      setRounds(data);
      // Every round (re)load also refreshes spend, so the card stays current
      // after publish/close/edit without a separate trigger per handler.
      void syncSpend();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load round history');
    }
  }, [experiment.id, syncSpend]);

  const loadRecommendation = useCallback(async () => {
    try {
      const data = await api.getRecommendation(experiment.id, { includePreview });
      setRecommendation(data);
    } catch (err) {
      setRecommendation(null);
      setError(err instanceof Error ? err.message : 'Failed to load recommendation');
    }
  }, [experiment.id, includePreview]);

  useEffect(() => {
    loadStats();
    loadUploads();
    api.getPlatformStatus()
      .then((s) => {
        setProlificEnabled(s.prolific_enabled);
        setCurrencyCode(s.currency_code);
        setCurrencySymbol(s.currency_symbol);
        setPlatformStatusMessage(null);
      })
      .catch(() => {
        setProlificEnabled(false);
        setPlatformStatusMessage('Unable to load platform status. Assuming Prolific is disabled.');
      });
  }, [loadStats, loadUploads]);

  useEffect(() => {
    if (prolificEnabled === true) {
      loadRounds();
      loadRecommendation();
    }
  }, [prolificEnabled, loadRounds, loadRecommendation]);

  // ── Handlers ───────────────────────────────────────────────────────────
  const handleSaveMeta = async () => {
    setError(null);
    setSavingMeta(true);
    try {
      await api.updateExperiment(experiment.id, {
        assistance_method: experiment.assistance_method,
        assistance_params: experiment.assistance_params ?? null,
        description: metaForm.description,
        system_prompt: metaForm.system_prompt,
        human_prompt_prefix: metaForm.human_prompt_prefix,
        human_prompt_suffix: metaForm.human_prompt_suffix,
        prolific_pool: metaForm.prolific_pool,
      });
      showSuccess('Instructions & prompts saved.', 2000);
      metaFormDirtyRef.current = false;
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save instructions & prompts');
    } finally {
      setSavingMeta(false);
    }
  };

  const saveAssistanceMethod = async (
    method: 'none' | 'top_n' | 'human_as_a_tool',
    params?: Record<string, unknown>
  ) => {
    await api.updateExperiment(experiment.id, {
      assistance_method: method,
      assistance_params: params,
    });
    setTopNEnabled(method === 'top_n');
    setHumanAsATool(method === 'human_as_a_tool');
    markAssistanceTouched();
    // Refresh so the `experiment` prop reflects the new assistance state
    // before any follow-up handler runs. handleSaveMeta re-sends
    // `experiment.assistance_method` alongside its own fields; without an
    // awaited refresh, a Save-metadata click right after a toggle would send
    // the pre-toggle value and reset the server.
    await onRefresh();
  };

  const handleTopNToggle = async () => {
    const next = !topNEnabled;
    try {
      await saveAssistanceMethod(next ? 'top_n' : 'none', next ? { n: topNValue } : undefined);
      showSuccess(`Top-N assistance ${next ? 'enabled' : 'disabled'}`, 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update assistance method');
    }
  };

  const handleTopNChange = async (value: number) => {
    const nextValue = Math.max(1, Math.min(10, value));
    setTopNValue(nextValue);
    if (!topNEnabled) return;
    try {
      await saveAssistanceMethod('top_n', { n: nextValue });
      showSuccess('Top-N setting updated', 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update Top-N setting');
    }
  };

  const handleHumanAsAToolToggle = async () => {
    const next = !humanAsATool;
    try {
      await saveAssistanceMethod(
        next ? 'human_as_a_tool' : 'none',
        next ? { confidence_method: confidenceMethod } : undefined
      );
      showSuccess(`Human-as-a-Tool ${next ? 'enabled' : 'disabled'}`, 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update assistance method');
    }
  };

  const handleNoAssistance = async () => {
    try {
      await saveAssistanceMethod('none');
      showSuccess('Assistance disabled — raters answer unaided.', 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update assistance method');
    }
  };

  const handleConfidenceMethodChange = async (method: string) => {
    setConfidenceMethod(method);
    try {
      await saveAssistanceMethod('human_as_a_tool', { confidence_method: method });
      showSuccess('Confidence method updated', 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update confidence method');
    }
  };

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile || uploadState) return;

    if (experiment.rating_count > 0) {
      if (!window.confirm(
        `This experiment already has ${experiment.rating_count} ratings. ` +
        `Uploading will ADD more questions (not replace existing ones). Continue?`
      )) {
        return;
      }
    }

    setError(null);
    clearSuccess();

    const form = e.target as HTMLFormElement;
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setUploadState({ phase: 'sending', loaded: 0, total: uploadFile.size });

    try {
      const result = await api.uploadQuestions(experiment.id, uploadFile, {
        signal: controller.signal,
        onProgress: ({ loaded, total }) => {
          const size = total ?? uploadFile.size;
          setUploadState({
            phase: loaded >= size ? 'processing' : 'sending',
            loaded,
            total: size,
          });
        },
      });
      // Clear before the success toast and the refresh round trips below.
      // Leaving it to `finally` would paint "Processing on the server / don't
      // close this tab" alongside "Uploaded N questions", with the form still
      // disabled, for as long as those reloads take.
      setUploadState(null);

      const parts: string[] = [result.message];
      if (result.meta_applied.length > 0) {
        parts.push(
          `Applied metadata: ${result.meta_applied.map((f) => DATASET_META_LABELS[f]).join(', ')}.`,
        );
      }
      if (result.meta_conflicts.length > 0) {
        parts.push(
          `Kept existing values (this upload declared different ${result.meta_conflicts
            .map((f) => DATASET_META_LABELS[f])
            .join(', ')}).`,
        );
      }
      showSuccess(parts.join(' '));
      setUploadFile(null);
      form.reset();
      await loadStats();
      await loadUploads();
      if (prolificEnabled === true) await loadRecommendation();
      onRefresh();
    } catch (err) {
      // A cancel is the admin's own doing — clearing the progress UI is
      // feedback enough, so don't raise it as an error.
      if (!(err instanceof UploadAbortedError)) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
    } finally {
      uploadAbortRef.current = null;
      setUploadState(null);
    }
  };

  const handleCancelUpload = () => uploadAbortRef.current?.abort();

  const handleDelete = async () => {
    if (deleting) return;
    const prolificWarning = rounds.length > 0
      ? ' Linked Prolific studies for every round will also be deleted.'
      : '';
    if (!window.confirm(`Delete "${experiment.name}"? This cannot be undone.${prolificWarning}`)) {
      return;
    }
    setDeleting(true);
    try {
      await api.deleteExperiment(experiment.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      setDeleting(false);
    }
  };

  const handleFinish = async () => {
    if (!window.confirm(
      `Mark "${experiment.name}" as finished? This is permanent — no more rounds ` +
      'can be launched, and this experiment becomes selectable by others as an ' +
      'exclusion source.'
    )) {
      return;
    }
    setError(null);
    setFinishing(true);
    try {
      await api.finishExperiment(experiment.id);
      showSuccess('Experiment marked as finished.');
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to mark experiment as finished');
    } finally {
      setFinishing(false);
    }
  };

  const handlePublishRound = async (roundId: number, roundNumber: number) => {
    if (!window.confirm('Publish this study on Prolific? Participants will be able to start immediately.')) {
      return;
    }
    setError(null);
    setPublishingRoundId(roundId);
    try {
      await api.publishExperimentRound(experiment.id, roundId);
      showSuccess(`Round ${roundNumber === 0 ? 'pilot' : roundNumber} published on Prolific!`);
      await loadRounds();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to publish study');
    } finally {
      setPublishingRoundId(null);
    }
  };

  const handleStartEditRound = (round: ExperimentRound) => {
    setEditingRoundId(round.id);
    setEditForm({
      description: round.description,
      estimated_completion_time: round.estimated_completion_time,
      places: round.places_requested,
      study_label: round.study_label ?? 'annotation',
      screeners: round.screeners ?? [],
      excluded_experiment_ids: round.excluded_experiment_ids ?? [],
    });
    setEditRewardInput(rewardMinorToInput(round.reward, currencyCode));
    setError(null);
  };

  const handleCancelEditRound = () => {
    setEditingRoundId(null);
  };

  const handleSaveEditRound = async (roundId: number) => {
    setError(null);
    setSavingEdit(true);
    try {
      const rewardMinor = rewardInputToMinor(editRewardInput, currencyCode);
      const { description, estimated_completion_time, places, study_label, screeners, excluded_experiment_ids } = editForm;
      await api.editExperimentRound(experiment.id, roundId, {
        description,
        estimated_completion_time,
        places,
        study_label,
        screeners,
        excluded_experiment_ids,
        ...(rewardMinor > 0 ? { reward: rewardMinor } : {}),
      });
      showSuccess('Round updated on Prolific.');
      setEditingRoundId(null);
      await loadRounds();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update round');
    } finally {
      setSavingEdit(false);
    }
  };

  const handleCloseRound = async (roundId: number, roundNumber: number) => {
    if (!window.confirm('Close this round on Prolific? New rounds stay blocked until the current round is closed.')) {
      return;
    }
    setError(null);
    setClosingRoundId(roundId);
    try {
      await api.closeExperimentRound(experiment.id, roundId);
      showSuccess(`Round ${roundNumber === 0 ? 'pilot' : roundNumber} closed on Prolific!`);
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to close round');
    } finally {
      setClosingRoundId(null);
    }
  };

  const [discardingRoundId, setDiscardingRoundId] = useState<number | null>(null);

  const handleDiscardRound = async (roundId: number, roundNumber: number) => {
    const label = roundNumber === 0 ? 'pilot' : `Round ${roundNumber}`;
    if (!window.confirm(
      `Discard the ${label} draft? This deletes the unpublished Prolific study and lets you rebuild the round from scratch.`
    )) {
      return;
    }
    setError(null);
    setDiscardingRoundId(roundId);
    try {
      await api.discardExperimentRound(experiment.id, roundId);
      showSuccess(`${label.charAt(0).toUpperCase() + label.slice(1)} draft discarded.`);
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to discard round');
    } finally {
      setDiscardingRoundId(null);
    }
  };

  const handleRunPilot = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api.runPilotStudy(experiment.id, {
        ...pilotForm,
        reward: rewardInputToMinor(pilotRewardInput, currencyCode),
      });
      showSuccess('Pilot draft created on Prolific. Publish it when ready.', 4000);
      pilotDescriptionDirtyRef.current = false;
      onRefresh();
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create pilot study');
    }
  };

  const handleRunRound = async () => {
    if (!recommendation) return;
    const places = recommendation.recommended_places;
    setError(null);
    try {
      await api.runExperimentRound(experiment.id, places);
      showSuccess(`Round ${nextRoundNumber} draft created on Prolific. Publish it when ready.`, 4000);
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create study round');
    }
  };

  // ── Derived state ──────────────────────────────────────────────────────
  const latestRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
  const latestRoundClosed = latestRound
    ? ['AWAITING_REVIEW', 'COMPLETED'].includes(latestRound.prolific_study_status)
    : false;
  const nextRoundNumber = latestRound ? latestRound.round_number + 1 : 1;
  const roundLaunchBlockedMessage = !latestRoundClosed && latestRound
    ? `Waiting for ${latestRound.round_number === 0 ? 'the pilot round' : `Round ${latestRound.round_number}`} to close. Current status: ${latestRound.prolific_study_status}.`
    : null;
  const isLocked = experiment.status !== 'DRAFT';
  const isFinished = experiment.status === 'FINISHED';
  const canFinish = useMemo(
    () =>
      experiment.status === 'LAUNCH'
      && rounds.length > 0
      && rounds.every((r) => ['AWAITING_REVIEW', 'COMPLETED'].includes(r.prolific_study_status)),
    [experiment.status, rounds],
  );
  const lockedHint = isFinished
    ? 'Locked — experiment is finished.'
    : 'Locked — config freezes once the first round is published on Prolific.';

  // Setup checklist state. Each step's "done"-ness is derived; the "current"
  // step is the first non-done step in sequence. Everything after `current`
  // is "todo".
  const stepDone = {
    questions: experiment.question_count > 0,
    instructions: Boolean(experiment.description?.trim()),
    assistance: assistanceTouched || experiment.assistance_method !== 'none',
    launch: experiment.status !== 'DRAFT',
  } as const;
  const currentStepKey = ((): TabKey | null => {
    for (const s of SETUP_STEPS) {
      if (!stepDone[s.key as keyof typeof stepDone]) return s.key;
    }
    return null;
  })();
  const stepDefs: StepDef[] = SETUP_STEPS.map((s, i) => {
    const done = stepDone[s.key as keyof typeof stepDone];
    const status: StepStatus = done
      ? 'done'
      : s.key === currentStepKey
        ? 'current'
        : 'todo';
    return { key: s.key, index: i + 1, label: s.label, status };
  });
  const stepsCompletedCount = stepDefs.filter((s) => s.status === 'done').length;

  // ── Render ─────────────────────────────────────────────────────────────
  return (
    <div className="admin-page">
      {/* Header row: back button, title, status pill, launch action shortcut. */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 20, marginBottom: 26 }}>
        <button
          type="button"
          onClick={onBack}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '8px 14px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface)',
            fontSize: 13.5,
            fontWeight: 600,
            color: 'var(--muted)',
            whiteSpace: 'nowrap',
            marginTop: 4,
            cursor: 'pointer',
          }}
        >
          ← Back
        </button>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            {editingName === 'internal_name' ? (
              <NameEditor
                value={nameDraft}
                onChange={setNameDraft}
                onSave={handleSaveName}
                onCancel={() => setEditingName(null)}
                saving={savingName}
                big
                placeholder="Internal name (optional)"
                ariaLabel="Internal name"
              />
            ) : (
              <>
                <h1
                  style={{
                    fontFamily: 'var(--font-head)',
                    fontWeight: 600,
                    fontSize: 28,
                    letterSpacing: '-0.01em',
                    margin: 0,
                  }}
                >
                  {experiment.internal_name || experiment.name}
                </h1>
                <PencilButton
                  onClick={() => handleStartEditName('internal_name')}
                  label="Edit internal name"
                />
                <StatusLabel status={experiment.status} />
              </>
            )}
          </div>
          {/* Public name always shown so it stays editable even before an
              internal name is set (when the title above already is the public
              name). */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              margin: '6px 0 0',
              fontSize: 14,
              color: 'var(--muted)',
            }}
          >
            {editingName === 'name' ? (
              <>
                <span style={{ whiteSpace: 'nowrap' }}>Public name (shown to raters):</span>
                <NameEditor
                  value={nameDraft}
                  onChange={setNameDraft}
                  onSave={handleSaveName}
                  onCancel={() => setEditingName(null)}
                  saving={savingName}
                  placeholder="Public name"
                  ariaLabel="Public name"
                />
              </>
            ) : (
              <>
                <span>Public name (shown to raters): {experiment.name}</span>
                <PencilButton
                  onClick={() => handleStartEditName('name')}
                  label="Edit public name"
                />
              </>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 2 }}>
          <a
            data-testid="export-link"
            href={api.getExportUrl(experiment.id, { includePreview })}
            download
            style={{ ...secondaryButton, textDecoration: 'none' }}
          >
            Export CSV
          </a>
          <button
            type="button"
            onClick={() => navigate(`/admin/experiments/${experiment.id}/analytics`)}
            style={primaryButton}
          >
            View analytics
          </button>
          {experiment.status === 'LAUNCH' && (
            <button
              data-testid="finish-experiment-button"
              onClick={handleFinish}
              disabled={!canFinish || finishing}
              title={
                canFinish
                  ? 'Marks the experiment as finished. Permanent.'
                  : 'All rounds must be closed on Prolific before finishing.'
              }
              style={{
                ...secondaryButton,
                color: 'var(--accent-soft-ink)',
                background: 'var(--accent-soft)',
                borderColor: 'var(--accent-soft)',
                opacity: canFinish && !finishing ? 1 : 0.5,
                cursor: canFinish && !finishing ? 'pointer' : 'not-allowed',
              }}
            >
              {finishing ? 'Finishing…' : 'Mark as finished'}
            </button>
          )}
        </div>
      </div>

      <StepperTabs
        active={tab}
        onChange={setTab}
        steps={stepDefs}
        onDeleteClick={handleDelete}
        deleting={deleting}
      />

      {/* Toasts float over the layout so they don't shove panel content down
          when they appear/disappear. Anchored top-center so they land in the
          researcher's line of sight after a save/upload without occluding the
          bottom action row. */}
      {(error || success) && (
        <div
          style={{
            position: 'fixed',
            // AdminShell header is ~48px tall (14px padding × 2 + ~20px content
            // + 1px border). 68px leaves ~20px of breathing room below it.
            top: 68,
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 40,
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
            width: 'min(520px, calc(100vw - 32px))',
            pointerEvents: 'none',
          }}
        >
          {error && (
            <div style={{ pointerEvents: 'auto' }}>
              <Toast tone="danger" onDismiss={() => setError(null)}>{error}</Toast>
            </div>
          )}
          {success && (
            <div style={{ pointerEvents: 'auto' }}>
              <Toast tone="ok" onDismiss={clearSuccess}>{success}</Toast>
            </div>
          )}
        </div>
      )}

      <div style={{ paddingTop: 20, minHeight: 520 }}>
        {tab === 'overview' && (
          <OverviewPanel
            experiment={experiment}
            stats={stats}
            spendMinor={spendMinor}
            spendSyncing={spendSyncing}
            currencySymbol={currencySymbol}
            currencyCode={currencyCode}
            includePreview={includePreview}
            onTogglePreview={setIncludePreview}
            steps={stepDefs}
            currentStepKey={currentStepKey}
            stepsCompletedCount={stepsCompletedCount}
            onGoto={setTab}
          />
        )}
        {tab === 'questions' && (
          <QuestionsPanel
            experiment={experiment}
            uploads={uploads}
            uploadFile={uploadFile}
            uploadState={uploadState}
            onFileChange={setUploadFile}
            onSubmit={handleUpload}
            onCancelUpload={handleCancelUpload}
            isLocked={isLocked}
            onBack={() => setTab('overview')}
            onNext={() => setTab('instructions')}
          />
        )}
        {tab === 'instructions' && (
          <MetadataPanel
            experiment={experiment}
            uploads={uploads}
            metaForm={metaForm}
            onMetaChange={(field, value) => {
              metaFormDirtyRef.current = true;
              setMetaForm({ ...metaForm, [field]: value });
            }}
            onSave={handleSaveMeta}
            savingMeta={savingMeta}
            isLocked={isLocked}
            lockedHint={lockedHint}
            onBack={() => setTab('questions')}
            onNext={() => setTab('assistance')}
          />
        )}
        {tab === 'assistance' && (
          <AssistanceModePanel
            method={
              humanAsATool ? 'human_as_a_tool' : topNEnabled ? 'top_n' : 'none'
            }
            topNValue={topNValue}
            confidenceMethod={confidenceMethod}
            systemPrompt={metaForm.system_prompt}
            onSystemPromptChange={(v) => {
              metaFormDirtyRef.current = true;
              setMetaForm({ ...metaForm, system_prompt: v });
            }}
            onSaveSystemPrompt={handleSaveMeta}
            onPickNone={handleNoAssistance}
            onToggleTopN={handleTopNToggle}
            onTopNChange={handleTopNChange}
            onToggleHumanAsATool={handleHumanAsAToolToggle}
            onConfidenceMethodChange={handleConfidenceMethodChange}
            isLocked={isLocked}
            lockedHint={lockedHint}
            onBack={() => setTab('instructions')}
            onNext={() => setTab('launch')}
          />
        )}
        {tab === 'launch' && (
          <LaunchPanel
            experiment={experiment}
            prolificEnabled={prolificEnabled}
            platformStatusMessage={platformStatusMessage}
            currencyCode={currencyCode}
            currencySymbol={currencySymbol}
            rounds={rounds}
            recommendation={recommendation}
            latestRoundClosed={latestRoundClosed}
            nextRoundNumber={nextRoundNumber}
            roundLaunchBlockedMessage={roundLaunchBlockedMessage}
            editingRoundId={editingRoundId}
            editForm={editForm}
            editRewardInput={editRewardInput}
            savingEdit={savingEdit}
            publishingRoundId={publishingRoundId}
            closingRoundId={closingRoundId}
            discardingRoundId={discardingRoundId}
            onEditFormChange={setEditForm}
            onEditRewardChange={setEditRewardInput}
            onStartEditRound={handleStartEditRound}
            onCancelEditRound={handleCancelEditRound}
            onSaveEditRound={handleSaveEditRound}
            onPublishRound={handlePublishRound}
            onCloseRound={handleCloseRound}
            onDiscardRound={handleDiscardRound}
            pilotForm={pilotForm}
            onPilotChange={setPilotForm}
            pilotRewardInput={pilotRewardInput}
            onPilotRewardChange={setPilotRewardInput}
            onRunPilot={handleRunPilot}
            onRunRound={handleRunRound}
            otherExperiments={otherExperiments}
            onBack={() => setTab('assistance')}
          />
        )}
      </div>
    </div>
  );
}

// ── Overview ────────────────────────────────────────────────────────────

function OverviewPanel({
  experiment,
  stats,
  spendMinor,
  spendSyncing,
  currencySymbol,
  currencyCode,
  includePreview,
  onTogglePreview,
  steps,
  currentStepKey,
  stepsCompletedCount,
  onGoto,
}: {
  experiment: Experiment;
  stats: ExperimentStats | null;
  spendMinor: number | null;
  spendSyncing: boolean;
  currencySymbol: string | null;
  currencyCode: string | null;
  includePreview: boolean;
  onTogglePreview: (v: boolean) => void;
  steps: StepDef[];
  currentStepKey: TabKey | null;
  stepsCompletedCount: number;
  onGoto: (t: TabKey) => void;
}) {
  const targetRatings = stats ? stats.total_questions * experiment.num_ratings_per_question : 0;
  // Ratings-based progress: a question with 2 of 3 ratings counts as 2/3 done
  // rather than 0, so the bar reflects work collected, not just questions
  // that crossed the finish line. effective_ratings caps each question at
  // its target, so overshoot on one question can't mask a shortfall on
  // another — 100% means every question is individually at target.
  // Floor rather than round: at 3671 of 3681 the bar rounded up to a full 100%
  // while 10 questions were still short, contradicting the question count
  // printed beside it. Flooring reaches 100 only once every question is at
  // target, which is what the line above promises.
  const completePct = stats && targetRatings > 0
    ? Math.min(100, Math.floor((stats.effective_ratings / targetRatings) * 100))
    : 0;
  // Once a main round has launched the experiment leaves DRAFT and setup is
  // no longer the user's job — the Overview becomes a monitoring dashboard.
  // Hide the checklist entirely rather than showing a stale "Setup complete"
  // card that competes with live stats for attention.
  const showSetupCard = experiment.status === 'DRAFT';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {showSetupCard && (
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            padding: '24px 26px',
            boxShadow: 'var(--shadow)',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              justifyContent: 'space-between',
              gap: 24,
            }}
          >
            <div>
              <div
                style={{
                  font: '600 12px/1 var(--font-mono)',
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  color: 'var(--muted)',
                }}
              >
                Setup · {stepsCompletedCount} of {steps.length} steps done
              </div>
              <h2
                style={{
                  fontFamily: 'var(--font-head)',
                  fontWeight: 600,
                  fontSize: 20,
                  margin: '8px 0 3px',
                }}
              >
                {currentStepKey === null
                  ? 'Setup complete — launch when ready'
                  : 'Finish setup, then launch on Prolific'}
              </h2>
              <p style={{ margin: 0, fontSize: 13.5, color: 'var(--muted)' }}>
                Complete each step in order. Launch is the final step.
              </p>
            </div>
            {currentStepKey && (
              <button
                type="button"
                onClick={() => onGoto(currentStepKey)}
                style={{
                  ...primaryButton,
                  padding: '12px 22px',
                  whiteSpace: 'nowrap',
                }}
              >
                Resume setup →
              </button>
            )}
          </div>
          <div style={{ height: 1, background: 'var(--line)', margin: '20px 0' }} />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
            {steps.map((s) => (
              <StepChip key={s.key} step={s} onClick={() => onGoto(s.key)} />
            ))}
          </div>
        </div>
      )}

      {/* Stats grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 14 }}>
        <StatTile label="Questions" value={stats?.total_questions ?? 0} />
        <StatTile label="Complete" value={stats?.questions_complete ?? 0} />
        <StatTile label="Ratings" value={stats?.total_ratings ?? 0} />
        <StatTile label="Raters" value={stats?.total_raters ?? 0} />
        <SpendTile
          minorUnits={spendMinor}
          symbol={currencySymbol}
          currencyCode={currencyCode}
          syncing={spendSyncing}
        />
      </div>

      {/* Progress + preview toggle */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 20, alignItems: 'start' }}>
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            padding: 24,
            boxShadow: 'var(--shadow)',
          }}
        >
          <div
            style={{
              font: '600 12px/1 var(--font-mono)',
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
              marginBottom: 16,
            }}
          >
            Completion progress
          </div>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              fontSize: 13.5,
              marginBottom: 8,
            }}
          >
            <span style={{ color: 'var(--muted)' }}>
              {(stats?.effective_ratings ?? 0).toLocaleString()} of {targetRatings.toLocaleString()} ratings
              toward target · {stats?.questions_complete ?? 0} of {stats?.total_questions ?? 0} questions
              at target ({experiment.num_ratings_per_question} each)
            </span>
            <span style={{ font: '600 13px var(--font-mono)' }}>{completePct}%</span>
          </div>
          <ProgressBar pct={completePct} />
          <div style={{ height: 1, background: 'var(--line)', margin: '22px 0' }} />
          <div style={{ fontSize: 13.5, color: 'var(--muted)', lineHeight: 1.6 }}>
            {stats && stats.total_ratings > 0
              ? `Live data from ${stats.total_raters} raters. Toggle preview data below to include preview sessions in stats and exports.`
              : 'No ratings collected yet. Finish setup and publish the pilot on the Launch step to begin recruiting.'}
          </div>
        </div>
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            padding: '22px 24px',
            boxShadow: 'var(--shadow)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 6 }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>Include preview data</div>
            </div>
            <ToggleSwitch
              testId="include-preview-toggle"
              checked={includePreview}
              onChange={() => onTogglePreview(!includePreview)}
            />
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.5 }}>
            Show data from preview sessions in stats, analytics, exports, and round
            recommendations.
          </div>
        </div>
      </div>
    </div>
  );
}

function StepChip({ step, onClick }: { step: StepDef; onClick: () => void }) {
  const isDone = step.status === 'done';
  const isCurrent = step.status === 'current';
  const bg = isDone ? 'var(--accent-soft)' : 'var(--surface)';
  const border = isDone
    ? '1px solid var(--accent-soft)'
    : isCurrent
      ? '2px solid var(--accent)'
      : '1px solid var(--faint)';
  const labelColor = isDone ? 'var(--accent-soft-ink)' : 'var(--ink)';
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        textAlign: 'left',
        background: bg,
        border,
        borderRadius: 'var(--radius-sm)',
        padding: isCurrent ? '13px 15px' : '14px 16px',
        cursor: 'pointer',
        font: 'inherit',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
        <StepDot status={step.status} index={step.index} />
        <span
          style={{
            font: '600 10px var(--font-mono)',
            letterSpacing: '0.1em',
            color: isCurrent ? 'var(--accent)' : isDone ? 'var(--accent-soft-ink)' : 'var(--muted)',
          }}
        >
          {isCurrent ? `STEP ${step.index} · NEXT` : `STEP ${step.index}`}
        </span>
      </div>
      <div style={{ fontSize: 13.5, fontWeight: 600, color: labelColor }}>{step.label}</div>
    </button>
  );
}

function StepDot({ status, index }: { status: StepStatus; index: number }) {
  if (status === 'done') {
    return (
      <span
        aria-hidden
        style={{
          width: 19,
          height: 19,
          borderRadius: '50%',
          background: 'var(--accent)',
          color: '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 11,
        }}
      >
        ✓
      </span>
    );
  }
  return (
    <span
      aria-hidden
      style={{
        width: 19,
        height: 19,
        borderRadius: '50%',
        background: 'var(--surface-2)',
        border: '1px solid var(--faint)',
        color: 'var(--muted)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 11,
      }}
    >
      {index}
    </span>
  );
}

function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        padding: '22px 24px',
        boxShadow: 'var(--shadow)',
      }}
    >
      <div style={{ font: '600 34px/1 var(--font-head)' }}>{value}</div>
      <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 5 }}>{label}</div>
    </div>
  );
}

/**
 * Spend counterpart to StatTile: a currency figure that hydrates in the
 * background from Prolific. Shows the last-known value with a subtle "syncing"
 * hint until the live cost lands. The figure is each study's Prolific cost
 * (rewards + fees) summed over rounds; it excludes bonuses paid separately.
 */
function SpendTile({
  minorUnits,
  symbol,
  currencyCode,
  syncing,
}: {
  minorUnits: number | null;
  symbol: string | null;
  currencyCode: string | null;
  syncing: boolean;
}) {
  // Zero-decimal currencies (JPY, KRW, …) have no minor unit, so the divisor
  // and decimal places come from rewardDecimals rather than a hardcoded /100.
  const decimals = rewardDecimals(currencyCode);
  const value =
    minorUnits == null
      ? '—'
      : `${symbol || '$'}${(minorUnits / 10 ** decimals).toFixed(decimals)}`;
  return (
    <div
      title="Prolific study cost (rewards + fees) across all rounds. Excludes bonuses paid separately."
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        padding: '22px 24px',
        boxShadow: 'var(--shadow)',
      }}
    >
      <div style={{ font: '600 34px/1 var(--font-head)', fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </div>
      <div
        style={{
          fontSize: 13,
          color: 'var(--muted)',
          marginTop: 5,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
        }}
      >
        Spend
        {syncing && <span style={{ fontSize: 11, color: 'var(--muted)' }}>· syncing…</span>}
      </div>
    </div>
  );
}

// ── Questions panel ─────────────────────────────────────────────────────

type UploadState = {
  /** 'sending' = bytes on the wire; 'processing' = server parsing + inserting. */
  phase: 'sending' | 'processing';
  loaded: number;
  total: number;
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
}

// Live progress for an in-flight upload. The transfer half is measured; the
// server half has no progress feed, so it gets an indeterminate bar and an
// elapsed clock — enough to show the request is still alive on a big file.
function UploadProgressPanel({
  upload,
  filename,
  onCancel,
}: {
  upload: UploadState;
  filename: string;
  onCancel: () => void;
}) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  const sending = upload.phase === 'sending';
  const pct = upload.total > 0 ? Math.min(100, Math.round((upload.loaded / upload.total) * 100)) : 0;

  return (
    <div
      data-testid="upload-progress"
      style={{
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        padding: '18px 22px',
        background: 'var(--surface)',
        boxShadow: 'var(--shadow)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 12,
          marginBottom: 10,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 3 }}>
            {sending ? 'Uploading' : 'Processing on the server'}
          </div>
          <div
            style={{
              font: '400 12.5px var(--font-mono)',
              color: 'var(--muted)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {filename}
          </div>
        </div>
        <span style={{ font: '600 14px var(--font-mono)', flex: '0 0 auto' }}>
          {sending ? `${pct}%` : formatElapsed(elapsed)}
        </span>
      </div>

      <ProgressBar
        pct={sending ? pct : 'indeterminate'}
        transition="width 120ms linear"
        fillTestId="upload-progress-bar"
      />

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          marginTop: 10,
        }}
      >
        <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.5 }}>
          {sending ? (
            <>
              {formatBytes(upload.loaded)} of {formatBytes(upload.total)} sent ·{' '}
              {formatElapsed(elapsed)} elapsed
            </>
          ) : (
            <>
              All {formatBytes(upload.total)} received. Parsing rows and writing questions — large
              files can take a few minutes. Don't close this tab.
            </>
          )}
        </div>
        {sending && (
          <button
            type="button"
            data-testid="upload-cancel-button"
            onClick={onCancel}
            style={{ ...secondaryButton, flex: '0 0 auto' }}
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}

function QuestionsPanel({
  experiment,
  uploads,
  uploadFile,
  uploadState,
  onFileChange,
  onSubmit,
  onCancelUpload,
  isLocked,
  onBack,
  onNext,
}: {
  experiment: Experiment;
  uploads: Upload[];
  uploadFile: File | null;
  uploadState: UploadState | null;
  onFileChange: (f: File | null) => void;
  onSubmit: (e: React.FormEvent) => void;
  onCancelUpload: () => void;
  isLocked: boolean;
  onBack: () => void;
  onNext: () => void;
}) {
  const uploading = uploadState !== null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {uploads.length > 0 && (
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            overflow: 'hidden',
            boxShadow: 'var(--shadow)',
          }}
        >
          {uploads.map((upload, idx) => {
            const metaKeys = upload.dataset_meta
              ? (Object.keys(upload.dataset_meta) as DatasetMetaField[])
              : [];
            return (
              <div
                key={upload.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '14px 20px',
                  borderBottom: idx === uploads.length - 1 ? 'none' : '1px solid var(--line)',
                }}
              >
                <span
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 8,
                    background: 'var(--accent-soft)',
                    color: 'var(--accent-soft-ink)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    font: '600 10px var(--font-mono)',
                    flex: '0 0 auto',
                  }}
                >
                  {upload.filename.endsWith('.parquet') ? 'PQT' : 'CSV'}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      font: '500 14px var(--font-mono)',
                      color: 'var(--ink)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {upload.filename}
                  </div>
                  <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 3 }}>
                    {upload.question_count} questions
                    {metaKeys.length > 0 && (
                      <span title={`upload declared metadata: ${metaKeys.join(', ')}`}>
                        {' · '}
                        {metaKeys.length} metadata field{metaKeys.length === 1 ? '' : 's'}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {experiment.rating_count > 0 && !isLocked && (
        <Banner tone="warn">
          <strong>Note:</strong> Uploading adds questions, doesn't replace existing ones.
        </Banner>
      )}
      {isLocked && (
        <Banner tone="warn">
          <strong>Locked:</strong> the item set is frozen — no more questions can be added.
        </Banner>
      )}

      <form
        onSubmit={onSubmit}
        style={{
          border: '1px dashed var(--faint)',
          borderRadius: 'var(--radius)',
          padding: '20px 22px',
          background: 'var(--surface)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>
              Add questions from CSV or Parquet
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.55, marginBottom: 10 }}>
              Required: <span style={{ fontFamily: 'var(--font-mono)' }}>question_id</span>,{' '}
              <span style={{ fontFamily: 'var(--font-mono)' }}>question_text</span>. Optional:{' '}
              <span style={{ fontFamily: 'var(--font-mono)' }}>
                gt_answer, options, question_type, metadata, parent_question_id
              </span>
              . Supports long-context rows and files up to 200 MB. Optional dataset metadata:{' '}
              <span style={{ fontFamily: 'var(--font-mono)' }}>#META:</span> JSON line at the top of
              a CSV, or a <span style={{ fontFamily: 'var(--font-mono)' }}>dataset_meta</span> key
              in the Parquet schema's key-value metadata.
            </div>
            <input
              id="upload-csv"
              data-testid="upload-csv-input"
              type="file"
              accept=".csv,.parquet"
              disabled={isLocked || uploading}
              onChange={(e) => onFileChange(e.target.files?.[0] || null)}
              style={{ fontSize: 14, opacity: isLocked || uploading ? 0.5 : 1 }}
            />
            {uploadFile && !uploading && (
              <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 8 }}>
                Ready to upload{' '}
                <span style={{ fontFamily: 'var(--font-mono)' }}>{formatBytes(uploadFile.size)}</span>
                .
              </div>
            )}
          </div>
          <button
            data-testid="upload-csv-button"
            type="submit"
            disabled={!uploadFile || isLocked || uploading}
            style={{
              ...primaryButton,
              opacity: uploadFile && !isLocked && !uploading ? 1 : 0.55,
              cursor: uploadFile && !isLocked && !uploading ? 'pointer' : 'not-allowed',
            }}
          >
            {uploading ? 'Uploading…' : 'Upload'}
          </button>
        </div>

        {uploadState && (
          <div style={{ marginTop: 18 }}>
            <UploadProgressPanel
              upload={uploadState}
              filename={uploadFile?.name ?? 'file'}
              onCancel={onCancelUpload}
            />
          </div>
        )}
      </form>

      <StepNav
        stepIndex={1}
        totalSteps={4}
        onBack={onBack}
        backLabel="← Overview"
        onNext={onNext}
        nextLabel="Continue: Instructions & prompts →"
      />
    </div>
  );
}

// ── Instructions & prompts panel ─────────────────────────────────────────

function MetadataPanel({
  experiment,
  uploads,
  metaForm,
  onMetaChange,
  onSave,
  savingMeta,
  isLocked,
  lockedHint,
  onBack,
  onNext,
}: {
  experiment: Experiment;
  uploads: Upload[];
  metaForm: Record<DatasetMetaField, string>;
  onMetaChange: (field: DatasetMetaField, value: string) => void;
  onSave: () => void;
  savingMeta: boolean;
  isLocked: boolean;
  lockedHint: string;
  onBack: () => void;
  onNext: () => void;
}) {
  const renderFieldCard = (
    field: DatasetMetaField,
    kind: 'input' | 'textarea',
    options: { minHeight?: number; trailing?: React.ReactNode } = {},
  ) => {
    const current = (experiment[field] ?? '') as string;
    const conflicts = uploads.filter((u) => {
      const declared = u.dataset_meta?.[field];
      return declared !== undefined && declared !== current;
    });
    return (
      <section
        key={field}
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: 'var(--shadow)',
          padding: '22px 24px',
        }}
      >
        <Field
          id={`meta-${field}`}
          label={DATASET_META_LABELS[field]}
          disabled={isLocked}
          hint={
            <>
              {DATASET_META_HINTS[field]}
              {conflicts.length > 0 && (
                <div style={{ color: 'var(--warn)', marginTop: 6 }}>
                  Declared differently by: {conflicts.map((c) => c.filename).join(', ')}.
                </div>
              )}
            </>
          }
        >
          {kind === 'textarea' ? (
            <textarea
              id={`meta-${field}`}
              value={metaForm[field]}
              placeholder={DATASET_META_PLACEHOLDERS[field]}
              disabled={isLocked}
              onChange={(e) => onMetaChange(field, e.target.value)}
              style={{
                ...textareaStyle,
                minHeight: options.minHeight,
                cursor: isLocked ? 'not-allowed' : 'text',
                opacity: isLocked ? 0.7 : 1,
              }}
            />
          ) : (
            <input
              id={`meta-${field}`}
              type="text"
              value={metaForm[field]}
              placeholder={DATASET_META_PLACEHOLDERS[field]}
              disabled={isLocked}
              onChange={(e) => onMetaChange(field, e.target.value)}
              style={{
                ...inputStyle,
                fontFamily: 'var(--font-mono)',
                cursor: isLocked ? 'not-allowed' : 'text',
                opacity: isLocked ? 0.7 : 1,
              }}
            />
          )}
        </Field>
        {options.trailing}
      </section>
    );
  };

  const saveButton = (
    <button
      type="button"
      onClick={onSave}
      disabled={savingMeta || isLocked}
      style={{
        ...primaryButton,
        marginLeft: 'auto',
        opacity: savingMeta || isLocked ? 0.6 : 1,
        cursor: savingMeta || isLocked ? 'not-allowed' : 'pointer',
      }}
    >
      {savingMeta ? 'Saving…' : 'Save instructions & prompts'}
    </button>
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <Banner tone="info">
        Auto-populated from the first upload that declares dataset metadata. Edits here
        always win — later uploads that disagree are noted but never overwrite.
      </Banner>
      {isLocked && <Banner tone="warn">{lockedHint}</Banner>}

      {META_FIELD_GROUPS.map((group, groupIdx) => {
        const isLastGroup = groupIdx === META_FIELD_GROUPS.length - 1;
        return (
          <div key={group.header} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div
              style={{
                font: '600 11px/1 var(--font-mono)',
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
                marginTop: groupIdx === 0 ? 0 : 6,
              }}
            >
              {group.header}
            </div>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns:
                  group.fields.length === 1 ? '1fr' : '1fr 1fr',
                gap: 18,
              }}
            >
              {group.fields.map(({ field, kind, minHeight }) =>
                renderFieldCard(field, kind, {
                  minHeight,
                  trailing:
                    isLastGroup && field === group.fields[group.fields.length - 1].field
                      ? <div style={{ display: 'flex', marginTop: -8 }}>{saveButton}</div>
                      : undefined,
                }),
              )}
            </div>
          </div>
        );
      })}

      <StepNav
        stepIndex={2}
        totalSteps={4}
        onBack={onBack}
        backLabel="← Questions"
        onNext={onNext}
        nextLabel="Continue: Rater assistance →"
      />
    </div>
  );
}

// ── Rater assistance panel ───────────────────────────────────────────────

function AssistanceModePanel({
  method,
  topNValue,
  confidenceMethod,
  systemPrompt,
  onSystemPromptChange,
  onSaveSystemPrompt,
  onPickNone,
  onToggleTopN,
  onTopNChange,
  onToggleHumanAsATool,
  onConfidenceMethodChange,
  isLocked,
  lockedHint,
  onBack,
  onNext,
}: {
  method: 'none' | 'top_n' | 'human_as_a_tool';
  topNValue: number;
  confidenceMethod: string;
  systemPrompt: string;
  onSystemPromptChange: (v: string) => void;
  onSaveSystemPrompt: () => void;
  onPickNone: () => void;
  onToggleTopN: () => void;
  onTopNChange: (v: number) => void;
  onToggleHumanAsATool: () => void;
  onConfidenceMethodChange: (v: string) => void;
  isLocked: boolean;
  lockedHint: string;
  onBack: () => void;
  onNext: () => void;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <p
        style={{
          margin: 0,
          fontSize: 14,
          color: 'var(--muted)',
          maxWidth: 640,
          lineHeight: 1.6,
        }}
      >
        Choose one assistance mode for this experiment. Changes apply to new participant
        assistance sessions.
      </p>
      {isLocked && <Banner tone="warn">{lockedHint}</Banner>}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
        <ModeCard
          selected={method === 'none'}
          disabled={isLocked}
          title="None"
          body="Raters answer unaided. The clean baseline condition."
          onClick={() => {
            if (isLocked) return;
            if (method !== 'none') onPickNone();
          }}
        />
        <ModeCard
          selected={method === 'top_n'}
          disabled={isLocked}
          title="Top-N suggestions"
          body="AI ranks the most likely answers and shows the shortlist, in random order, before the rater submits."
          onClick={() => {
            if (isLocked) return;
            if (method !== 'top_n') onToggleTopN();
          }}
        />
        <ModeCard
          selected={method === 'human_as_a_tool'}
          disabled={isLocked}
          title="Human-as-a-Tool"
          body="AI decomposes each question into subtasks. Raters answer each, then the AI synthesises a recommendation."
          onClick={() => {
            if (isLocked) return;
            if (method !== 'human_as_a_tool') onToggleHumanAsATool();
          }}
        />
      </div>

      {method === 'top_n' && (
        <SectionCard header="Top-N configuration">
          <Field
            id="top-n-input"
            label="Suggestions to show"
            hint="For multiple-choice questions, this is capped by the number of available options."
            disabled={isLocked}
          >
            <input
              id="top-n-input"
              type="number"
              min={1}
              max={10}
              value={topNValue}
              disabled={isLocked}
              onChange={(e) => onTopNChange(parseInt(e.target.value, 10) || 1)}
              style={{ ...inputStyle, width: 140 }}
            />
          </Field>
        </SectionCard>
      )}

      {method === 'human_as_a_tool' && (
        <SectionCard header="Human-as-a-Tool configuration">
          <Field label="Confidence method" disabled={isLocked}>
            <select
              value={confidenceMethod}
              disabled={isLocked}
              onChange={(e) => onConfidenceMethodChange(e.target.value)}
              style={{ ...inputStyle, maxWidth: 380, cursor: 'pointer' }}
            >
              <option value="self_report">Self-report — single call, fastest</option>
              <option value="sampling">Sampling — K samples + clustering, most accurate</option>
              <option value="self_consistency">Self-consistency — K samples, majority vote</option>
            </select>
          </Field>
        </SectionCard>
      )}

      <SectionCard padded={false}>
        <div style={{ padding: '22px 24px' }}>
          <div
            style={{
              font: '600 12px/1 var(--font-mono)',
              letterSpacing: '0.14em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
              marginBottom: 6,
            }}
          >
            Assistance prompt suffix
          </div>
          <p style={{ margin: '0 0 12px', fontSize: 12.5, color: 'var(--muted)' }}>
            Appended to the AI's system prompt for Top-N and Human-as-a-Tool. Ignored when no
            assistance is enabled.
          </p>
          <textarea
            value={systemPrompt}
            disabled={isLocked}
            onChange={(e) => onSystemPromptChange(e.target.value)}
            placeholder="e.g. Prefer the option that draws no distinction when the rule applies equally to both people."
            style={{
              ...textareaStyle,
              background: 'var(--surface-2)',
              height: 90,
              opacity: isLocked ? 0.6 : 1,
              cursor: isLocked ? 'not-allowed' : 'text',
            }}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
            <button
              type="button"
              onClick={onSaveSystemPrompt}
              disabled={isLocked}
              style={{
                ...secondaryButton,
                opacity: isLocked ? 0.6 : 1,
                cursor: isLocked ? 'not-allowed' : 'pointer',
              }}
            >
              Save prompt suffix
            </button>
          </div>
        </div>
      </SectionCard>

      <StepNav
        stepIndex={3}
        totalSteps={4}
        onBack={onBack}
        backLabel="← Instructions & prompts"
        onNext={onNext}
        nextLabel="Continue to launch →"
        highlightNext
      />
    </div>
  );
}

function ModeCard({
  selected,
  disabled,
  title,
  body,
  onClick,
}: {
  selected: boolean;
  disabled?: boolean;
  title: string;
  body: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        textAlign: 'left',
        border: selected ? '2px solid var(--accent)' : '1px solid var(--faint)',
        background: selected ? 'var(--accent-soft)' : 'var(--surface)',
        borderRadius: 'var(--radius)',
        padding: 20,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.7 : 1,
        boxShadow: 'var(--shadow)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 8 }}>
        <span
          aria-hidden
          style={{
            width: 16,
            height: 16,
            borderRadius: '50%',
            border: selected ? '5px solid var(--accent)' : '1px solid var(--faint)',
            background: 'var(--surface)',
          }}
        />
        <span
          style={{
            fontWeight: 700,
            fontSize: 15,
            color: selected ? 'var(--accent-soft-ink)' : 'var(--ink)',
          }}
        >
          {title}
        </span>
      </div>
      <div
        style={{
          fontSize: 13,
          lineHeight: 1.55,
          color: selected ? 'var(--accent-soft-ink)' : 'var(--muted)',
        }}
      >
        {body}
      </div>
    </button>
  );
}

// ── Launch on Prolific panel ─────────────────────────────────────────────

function LaunchPanel(props: {
  experiment: Experiment;
  prolificEnabled: boolean | 'loading';
  platformStatusMessage: string | null;
  currencyCode: string | null;
  currencySymbol: string | null;
  rounds: ExperimentRound[];
  recommendation: RecommendationResponse | null;
  latestRoundClosed: boolean;
  nextRoundNumber: number;
  roundLaunchBlockedMessage: string | null;
  editingRoundId: number | null;
  editForm: {
    description: string;
    estimated_completion_time: number;
    places: number;
    study_label: PilotStudyCreate['study_label'];
    screeners: Screener[];
    excluded_experiment_ids: number[];
  };
  editRewardInput: string;
  savingEdit: boolean;
  publishingRoundId: number | null;
  closingRoundId: number | null;
  discardingRoundId: number | null;
  onEditFormChange: (form: any) => void;
  onEditRewardChange: (v: string) => void;
  onStartEditRound: (r: ExperimentRound) => void;
  onCancelEditRound: () => void;
  onSaveEditRound: (id: number) => void;
  onPublishRound: (id: number, num: number) => void;
  onCloseRound: (id: number, num: number) => void;
  onDiscardRound: (id: number, num: number) => void;
  pilotForm: Omit<PilotStudyCreate, 'reward'>;
  onPilotChange: (form: Omit<PilotStudyCreate, 'reward'>) => void;
  pilotRewardInput: string;
  onPilotRewardChange: (v: string) => void;
  onRunPilot: (e: React.FormEvent) => void;
  onRunRound: () => void;
  otherExperiments: Experiment[];
  onBack: () => void;
}) {
  const {
    experiment,
    prolificEnabled,
    platformStatusMessage,
    currencyCode,
    currencySymbol,
    rounds,
    recommendation,
    latestRoundClosed,
    nextRoundNumber,
    roundLaunchBlockedMessage,
    editingRoundId,
    editForm,
    editRewardInput,
    savingEdit,
    publishingRoundId,
    closingRoundId,
    discardingRoundId,
    onEditFormChange,
    onEditRewardChange,
    onStartEditRound,
    onCancelEditRound,
    onSaveEditRound,
    onPublishRound,
    onCloseRound,
    onDiscardRound,
    pilotForm,
    onPilotChange,
    pilotRewardInput,
    onPilotRewardChange,
    onRunPilot,
    onRunRound,
    otherExperiments,
    onBack,
  } = props;

  // Only surface the Prolific-mode banner when it tells the researcher
  // something they need to act on. In prod Prolific is always enabled and
  // the "Prolific is enabled" copy is just noise; hide it in that case.
  const bannerContent =
    prolificEnabled === 'loading'
      ? {
          tone: 'info' as const,
          badgeText: 'Checking',
          text: 'Checking Prolific mode for this environment…',
        }
      : prolificEnabled === false
        ? {
            tone: 'danger' as const,
            badgeText: 'Disabled',
            text: 'Prolific is disabled for this environment. Configure a Prolific API token to enable paid rounds.',
          }
        : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* Hero banner marking this as the final step */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 20,
          background: 'var(--accent)',
          color: 'var(--accent-ink)',
          borderRadius: 'var(--radius)',
          padding: '22px 26px',
          boxShadow: 'var(--shadow)',
        }}
      >
        <span
          aria-hidden
          style={{
            width: 40,
            height: 40,
            borderRadius: '50%',
            background: 'rgba(255,255,255,0.16)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            font: '600 16px var(--font-mono)',
            flex: '0 0 auto',
          }}
        >
          4
        </span>
        <div style={{ flex: 1 }}>
          <div
            style={{
              font: '600 11px/1 var(--font-mono)',
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              opacity: 0.85,
            }}
          >
            Final step
          </div>
          <div style={{ fontFamily: 'var(--font-head)', fontWeight: 600, fontSize: 20, marginTop: 5 }}>
            Launch on Prolific
          </div>
          <div style={{ fontSize: 13.5, opacity: 0.9, marginTop: 3 }}>
            Start with the pilot, review results, then launch full rounds.
          </div>
        </div>
      </div>

      {bannerContent && (
        <div data-testid="prolific-mode-notice">
          <Banner tone={bannerContent.tone}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <span
                data-testid="prolific-mode-badge"
                style={{
                  font: '600 10px/1 var(--font-mono)',
                  letterSpacing: '0.14em',
                  textTransform: 'uppercase',
                  padding: '4px 8px',
                  borderRadius: 999,
                  background: 'rgba(0,0,0,0.08)',
                  color: 'inherit',
                }}
              >
                {bannerContent.badgeText}
              </span>
              <span>{bannerContent.text}</span>
            </div>
            {platformStatusMessage && (
              <div style={{ marginTop: 8 }}>{platformStatusMessage}</div>
            )}
          </Banner>
        </div>
      )}
      {prolificEnabled === true && platformStatusMessage && (
        <Banner tone="warn">{platformStatusMessage}</Banner>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 20, alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* Preview link is always available regardless of Prolific mode. */}
          <button
            data-testid="preview-participant-button"
            onClick={() => {
              const previewId = `preview_${Date.now()}`;
              const url = `${window.location.origin}/rate?experiment_id=${experiment.id}&PROLIFIC_PID=${previewId}&STUDY_ID=preview&SESSION_ID=preview&preview=true`;
              window.open(url, '_blank');
            }}
            style={{ ...secondaryButton, alignSelf: 'flex-start' }}
          >
            Preview as participant
          </button>

          {prolificEnabled === true && (
            <>
              {rounds.length > 0 && (
                <div data-testid="study-rounds-list" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {rounds.map((round) => (
                    <RoundCard
                      key={round.id}
                      round={round}
                      currencyCode={currencyCode}
                      currencySymbol={currencySymbol}
                      isEditing={editingRoundId === round.id}
                      editForm={editForm}
                      editRewardInput={editRewardInput}
                      savingEdit={savingEdit}
                      publishing={publishingRoundId === round.id}
                      closing={closingRoundId === round.id}
                      discarding={discardingRoundId === round.id}
                      onStartEdit={() => onStartEditRound(round)}
                      onCancelEdit={onCancelEditRound}
                      onSaveEdit={() => onSaveEditRound(round.id)}
                      onEditFormChange={onEditFormChange}
                      onEditRewardChange={onEditRewardChange}
                      onPublish={() => onPublishRound(round.id, round.round_number)}
                      onClose={() => onCloseRound(round.id, round.round_number)}
                      onDiscard={() => onDiscardRound(round.id, round.round_number)}
                      otherExperiments={otherExperiments}
                    />
                  ))}
                </div>
              )}

              {recommendation && recommendation.avg_time_per_question_seconds > 0 && (
                <RecommendationCard
                  recommendation={recommendation}
                  nextRoundNumber={nextRoundNumber}
                  latestRoundClosed={latestRoundClosed}
                  roundLaunchBlockedMessage={roundLaunchBlockedMessage}
                  onRunRound={onRunRound}
                />
              )}

              {rounds.length === 0 && (
                <PilotForm
                  pilotForm={pilotForm}
                  onPilotChange={onPilotChange}
                  pilotRewardInput={pilotRewardInput}
                  onPilotRewardChange={onPilotRewardChange}
                  currencyCode={currencyCode}
                  currencySymbol={currencySymbol}
                  onSubmit={onRunPilot}
                  otherExperiments={otherExperiments}
                  datasetDescription={experiment.description}
                />
              )}
            </>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {experiment.prolific_completion_url && (
            <SectionCard header="Completion URL">
              <div
                data-testid="completion-url-input"
                style={{
                  font: '400 12.5px var(--font-mono)',
                  color: 'var(--ink)',
                  background: 'var(--surface-2)',
                  border: '1px solid var(--faint)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '11px 13px',
                  wordBreak: 'break-all',
                }}
              >
                {experiment.prolific_completion_url}
              </div>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
                Raters redirect here when finished.
              </div>
            </SectionCard>
          )}
        </div>
      </div>

      <StepNav
        stepIndex={4}
        totalSteps={4}
        onBack={onBack}
        backLabel="← Rater assistance"
        onNext={null}
        nextLabel=""
      />
    </div>
  );
}

function RoundStatusPill({ status }: { status: string }) {
  const map: Record<string, { bg: string; ink: string }> = {
    ACTIVE: { bg: 'var(--accent-soft)', ink: 'var(--accent-soft-ink)' },
    COMPLETED: { bg: 'var(--accent-soft)', ink: 'var(--accent-soft-ink)' },
    AWAITING_REVIEW: { bg: 'var(--accent-soft)', ink: 'var(--accent-soft-ink)' },
    UNPUBLISHED: { bg: 'var(--warn-soft)', ink: 'var(--warn)' },
  };
  const c = map[status] ?? { bg: 'var(--surface-2)', ink: 'var(--muted)' };
  return (
    <span
      style={{
        padding: '3px 8px',
        borderRadius: 5,
        font: '600 10px var(--font-mono)',
        letterSpacing: '0.06em',
        background: c.bg,
        color: c.ink,
      }}
    >
      {status}
    </span>
  );
}

function RoundCard(props: {
  round: ExperimentRound;
  currencyCode: string | null;
  currencySymbol: string | null;
  isEditing: boolean;
  editForm: any;
  editRewardInput: string;
  savingEdit: boolean;
  publishing: boolean;
  closing: boolean;
  discarding: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onEditFormChange: (form: any) => void;
  onEditRewardChange: (v: string) => void;
  onPublish: () => void;
  onClose: () => void;
  onDiscard: () => void;
  otherExperiments: Experiment[];
}) {
  const {
    round,
    currencyCode,
    currencySymbol,
    isEditing,
    editForm,
    editRewardInput,
    savingEdit,
    publishing,
    closing,
    discarding,
    onStartEdit,
    onCancelEdit,
    onSaveEdit,
    onEditFormChange,
    onEditRewardChange,
    onPublish,
    onClose,
    onDiscard,
    otherExperiments,
  } = props;

  return (
    <SectionCard padded={false}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '16px 20px',
          background: 'var(--surface-2)',
          borderBottom: '1px solid var(--faint)',
        }}
      >
        <div style={{ flex: 1 }}>
          <span style={{ fontFamily: 'var(--font-head)', fontWeight: 600, fontSize: 16 }}>
            {round.round_number === 0 ? 'Pilot Round' : `Round ${round.round_number}`}
          </span>
          <span style={{ fontSize: 13, color: 'var(--muted)', marginLeft: 8 }}>
            {round.places_requested} places
          </span>
        </div>
        <RoundStatusPill status={round.prolific_study_status} />
        <button
          type="button"
          onClick={() => window.open(round.prolific_study_url, '_blank')}
          style={{ ...secondaryButton, padding: '7px 14px', fontSize: 13 }}
        >
          Open on Prolific
        </button>
        {round.prolific_study_status === 'UNPUBLISHED' && !isEditing && (
          <button
            type="button"
            data-testid={`edit-round-${round.round_number}`}
            onClick={onStartEdit}
            style={{ ...secondaryButton, padding: '7px 14px', fontSize: 13 }}
          >
            Edit
          </button>
        )}
        {round.prolific_study_status === 'UNPUBLISHED' && (
          <>
            <button
              type="button"
              data-testid={`discard-round-${round.round_number}`}
              onClick={onDiscard}
              disabled={discarding || isEditing || publishing}
              style={{
                background: 'none',
                border: 'none',
                padding: '7px 6px',
                cursor: discarding ? 'wait' : 'pointer',
                color: 'var(--danger)',
                font: '600 13px var(--font-body)',
                opacity: discarding ? 0.6 : 1,
              }}
            >
              {discarding ? 'Discarding…' : 'Discard draft'}
            </button>
            <button
              type="button"
              data-testid={`publish-round-${round.round_number}`}
              onClick={onPublish}
              disabled={publishing || isEditing || discarding}
              style={{ ...primaryButton, padding: '7px 16px', fontSize: 13 }}
            >
              {publishing ? 'Publishing…' : 'Publish'}
            </button>
          </>
        )}
        {!['UNPUBLISHED', 'AWAITING_REVIEW', 'COMPLETED'].includes(
          round.prolific_study_status,
        ) && (
          <button
            type="button"
            data-testid={`close-round-${round.round_number}`}
            onClick={onClose}
            disabled={closing}
            style={{ ...secondaryButton, padding: '7px 16px', fontSize: 13 }}
          >
            {closing ? 'Closing…' : 'Close round'}
          </button>
        )}
      </div>
      {isEditing && (
        <div
          data-testid={`edit-round-form-${round.round_number}`}
          style={{ padding: 20 }}
        >
          <Field
            label="Study description"
            id={`edit-round-description-${round.round_number}`}
            hint="Sent to Prolific and shown on the rater intro for anyone entering via this round's link. Overrides the dataset description for this round only."
          >
            <textarea
              data-testid={`edit-round-description-${round.round_number}`}
              value={editForm.description}
              onChange={(e) => onEditFormChange({ ...editForm, description: e.target.value })}
              style={{ ...textareaStyle, height: 88 }}
            />
          </Field>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
            <Field id={`edit-round-time-${round.round_number}`} label="Time (min)">
              <input
                data-testid={`edit-round-time-${round.round_number}`}
                type="number"
                min={1}
                value={editForm.estimated_completion_time}
                onChange={(e) =>
                  onEditFormChange({
                    ...editForm,
                    estimated_completion_time: parseInt(e.target.value, 10) || 0,
                  })
                }
                style={inputStyle}
              />
            </Field>
            <Field label={`Reward${currencyCode ? ` (${currencyCode})` : ''}`}>
              <div
                className="reward-input"
                style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  gap: 4,
                  width: '100%',
                  padding: '11px 13px',
                  border: '1px solid var(--faint)',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--surface)',
                  fontSize: 15,
                  fontFamily: 'var(--font-mono)',
                }}
              >
                {currencySymbol && (
                  <span style={{ color: 'var(--muted)', flexShrink: 0 }}>{currencySymbol}</span>
                )}
                <input
                  data-testid={`edit-round-reward-${round.round_number}`}
                  type="text"
                  inputMode="decimal"
                  value={editRewardInput}
                  onChange={(e) => {
                    if (e.target.value === '' || /^[0-9]*\.?[0-9]*$/.test(e.target.value)) {
                      onEditRewardChange(e.target.value);
                    }
                  }}
                  style={{
                    flex: 1,
                    minWidth: 0,
                    border: 'none',
                    background: 'transparent',
                    padding: 0,
                    fontSize: 'inherit',
                    fontFamily: 'inherit',
                    color: 'var(--ink)',
                    outline: 'none',
                  }}
                />
              </div>
            </Field>
            <Field id={`edit-round-places-${round.round_number}`} label="Places">
              <input
                data-testid={`edit-round-places-${round.round_number}`}
                type="number"
                min={1}
                value={editForm.places}
                onChange={(e) =>
                  onEditFormChange({ ...editForm, places: parseInt(e.target.value, 10) || 0 })
                }
                style={inputStyle}
              />
            </Field>
          </div>
          {(() => {
            const text = rewardHintText(
              editRewardInput,
              currencyCode,
              currencySymbol,
              editForm.estimated_completion_time,
              editForm.places,
            );
            return text ? (
              <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 12 }}>{text}</div>
            ) : null;
          })()}
          <Field label="Study label">
            <select
              data-testid={`edit-round-study-label-${round.round_number}`}
              value={editForm.study_label}
              onChange={(e) =>
                onEditFormChange({
                  ...editForm,
                  study_label: e.target.value as PilotStudyCreate['study_label'],
                })
              }
              style={{ ...inputStyle, cursor: 'pointer' }}
            >
              <option value="annotation">Annotation</option>
              <option value="survey">Survey</option>
              <option value="decision_making_task">Decision-making task</option>
              <option value="writing_task">Writing task</option>
              <option value="interview">Interview</option>
              <option value="other">Other</option>
            </select>
          </Field>
          <Field label="Pre-screeners">
            <ScreenerCheckboxes
              value={editForm.screeners}
              onChange={(next) => onEditFormChange({ ...editForm, screeners: next })}
              testIdPrefix={`edit-round-screener-${round.round_number}`}
            />
          </Field>
          <Field
            label="Exclude prior participants from"
            hint="Participants who joined any selected experiment will not see this study on Prolific."
          >
            <ExperimentExclusionPicker
              experiments={otherExperiments}
              selectedIds={editForm.excluded_experiment_ids}
              onChange={(ids) => onEditFormChange({ ...editForm, excluded_experiment_ids: ids })}
              testIdPrefix={`edit-round-exclusion-${round.round_number}`}
            />
          </Field>
          <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
            <button
              type="button"
              data-testid={`edit-round-cancel-${round.round_number}`}
              onClick={onCancelEdit}
              disabled={savingEdit}
              style={secondaryButton}
            >
              Cancel
            </button>
            <button
              type="button"
              data-testid={`edit-round-save-${round.round_number}`}
              onClick={onSaveEdit}
              disabled={savingEdit}
              style={primaryButton}
            >
              {savingEdit ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}
    </SectionCard>
  );
}

function RecommendationCard({
  recommendation,
  nextRoundNumber,
  latestRoundClosed,
  roundLaunchBlockedMessage,
  onRunRound,
}: {
  recommendation: RecommendationResponse;
  nextRoundNumber: number;
  latestRoundClosed: boolean;
  roundLaunchBlockedMessage: string | null;
  onRunRound: () => void;
}) {
  if (recommendation.is_complete) {
    return (
      <Banner tone="ok">
        <strong>All questions have enough ratings!</strong>
      </Banner>
    );
  }
  return (
    <div data-testid="recommendation-panel">
    <SectionCard header="Recommendation for next round">
      <div style={{ fontSize: 13.5, color: 'var(--ink)', lineHeight: 1.7, marginBottom: 12 }}>
        Avg time/question: <strong>{recommendation.avg_time_per_question_seconds.toFixed(0)}s</strong>
        {' · '}Remaining actions: <strong>{recommendation.remaining_rating_actions}</strong>
        {' · '}Hours left: <strong>{recommendation.total_hours_remaining.toFixed(1)}</strong>
      </div>
      {latestRoundClosed ? (
        <button
          data-testid="launch-round-button"
          onClick={onRunRound}
          style={primaryButton}
        >
          Create Round {nextRoundNumber} Draft ({recommendation.recommended_places} places)
        </button>
      ) : (
        roundLaunchBlockedMessage && (
          <div style={{ color: 'var(--muted)', fontSize: 13, lineHeight: 1.55 }}>
            {roundLaunchBlockedMessage}
          </div>
        )
      )}
    </SectionCard>
    </div>
  );
}

function PilotForm(props: {
  pilotForm: Omit<PilotStudyCreate, 'reward'>;
  onPilotChange: (form: Omit<PilotStudyCreate, 'reward'>) => void;
  pilotRewardInput: string;
  onPilotRewardChange: (v: string) => void;
  currencyCode: string | null;
  currencySymbol: string | null;
  onSubmit: (e: React.FormEvent) => void;
  otherExperiments: Experiment[];
  datasetDescription: string | null;
}) {
  const {
    pilotForm,
    onPilotChange,
    pilotRewardInput,
    onPilotRewardChange,
    currencyCode,
    currencySymbol,
    onSubmit,
    otherExperiments,
    datasetDescription,
  } = props;
  const prefilledFromDataset =
    !!datasetDescription && pilotForm.description === datasetDescription;
  return (
    <SectionCard header="Pilot round">
      <p style={{ fontSize: 13.5, color: 'var(--muted)', margin: '0 0 16px' }}>
        Create the first unpublished round with the study configuration you want to reuse. Pilot
        timing data drives the recommended size for later rounds.
      </p>
      <form onSubmit={onSubmit}>
        <Field
          id="pilot-description"
          label="Study description"
          hint={
            <>
              Sent to Prolific as this pilot's public description and shown on the rater intro.
              Overrides the dataset description for this pilot only. Markdown is converted to
              Prolific's HTML subset (headings, bold/italic/strike, lists, paragraphs). Links and
              images are not supported by Prolific.
              {prefilledFromDataset && (
                <div style={{ color: 'var(--accent-soft-ink)', marginTop: 6 }}>
                  Prefilled from the dataset description on Instructions &amp; prompts. Edit to
                  override for this pilot.
                </div>
              )}
            </>
          }
        >
          <textarea
            id="pilot-description"
            data-testid="pilot-description-input"
            value={pilotForm.description}
            onChange={(e) => onPilotChange({ ...pilotForm, description: e.target.value })}
            placeholder={
              "Describe the task for Prolific participants…\n\nMarkdown is supported: # heading, ## subheading, **bold**, *italic*, ~~strike~~, -/1. lists. Blank lines separate paragraphs."
            }
            required
            style={{ ...textareaStyle, minHeight: 120 }}
          />
        </Field>
        <Field
          id="pilot-study-label"
          label="Study label"
          hint="Categorises the study on Prolific; participants see this tag when browsing."
        >
          <select
            id="pilot-study-label"
            data-testid="pilot-study-label-select"
            value={pilotForm.study_label}
            onChange={(e) =>
              onPilotChange({
                ...pilotForm,
                study_label: e.target.value as PilotStudyCreate['study_label'],
              })
            }
            style={{ ...inputStyle, cursor: 'pointer' }}
          >
            <option value="annotation">Annotation</option>
            <option value="survey">Survey</option>
            <option value="decision_making_task">Decision-making task</option>
            <option value="writing_task">Writing task</option>
            <option value="interview">Interview</option>
            <option value="other">Other</option>
          </select>
        </Field>
        <Field label="Pre-screeners" hint="Default on. Screeners narrow the participant pool to higher-quality raters.">
          <ScreenerCheckboxes
            value={pilotForm.screeners}
            onChange={(next) => onPilotChange({ ...pilotForm, screeners: next })}
            testIdPrefix="pilot-screener"
          />
        </Field>
        <Field
          label="Exclude prior participants from"
          hint="Participants who joined any selected experiment will not see this study on Prolific. Main rounds inherit this list from the pilot."
        >
          <ExperimentExclusionPicker
            experiments={otherExperiments}
            selectedIds={pilotForm.excluded_experiment_ids}
            onChange={(ids) => onPilotChange({ ...pilotForm, excluded_experiment_ids: ids })}
            testIdPrefix="pilot-exclusion"
          />
        </Field>
        <Field id="pilot-estimated-completion-time" label="Estimated completion time (minutes)">
          <input
            id="pilot-estimated-completion-time"
            data-testid="pilot-estimated-completion-time-input"
            type="number"
            value={pilotForm.estimated_completion_time}
            onChange={(e) =>
              onPilotChange({
                ...pilotForm,
                estimated_completion_time: parseInt(e.target.value, 10) || 0,
              })
            }
            min={1}
            required
            style={inputStyle}
          />
        </Field>
        <Field
          id="pilot-reward"
          label={`Reward${currencyCode ? ` (${currencyCode})` : ''}`}
          hint={
            rewardHintText(
              pilotRewardInput,
              currencyCode,
              currencySymbol,
              pilotForm.estimated_completion_time,
              pilotForm.pilot_places,
            ) ?? "Enter the amount as you'd see it on Prolific."
          }
        >
          <div
            className="reward-input"
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 4,
              width: '100%',
              padding: '11px 13px',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              fontSize: 15,
              fontFamily: 'var(--font-mono)',
            }}
          >
            {currencySymbol && (
              <span style={{ color: 'var(--muted)', flexShrink: 0 }}>{currencySymbol}</span>
            )}
            <input
              id="pilot-reward"
              data-testid="pilot-reward-input"
              type="text"
              inputMode="decimal"
              value={pilotRewardInput}
              onChange={(e) => {
                if (e.target.value === '' || /^[0-9]*\.?[0-9]*$/.test(e.target.value)) {
                  onPilotRewardChange(e.target.value);
                }
              }}
              required
              style={{
                flex: 1,
                minWidth: 0,
                border: 'none',
                background: 'transparent',
                padding: 0,
                fontSize: 'inherit',
                fontFamily: 'inherit',
                color: 'var(--ink)',
                outline: 'none',
              }}
            />
          </div>
        </Field>
        <Field
          id="pilot-places"
          label="Number of raters"
          hint="Each rater does 1 hour. 5 is a good default for timing calibration."
        >
          <input
            id="pilot-places"
            data-testid="pilot-places-input"
            type="number"
            value={pilotForm.pilot_places}
            onChange={(e) =>
              onPilotChange({ ...pilotForm, pilot_places: parseInt(e.target.value, 10) || 0 })
            }
            min={1}
            required
            style={inputStyle}
          />
        </Field>
        <button data-testid="run-pilot-button" type="submit" style={{ ...primaryButton, width: '100%' }}>
          Create pilot draft
        </button>
      </form>
    </SectionCard>
  );
}

function ScreenerCheckboxes({
  value,
  onChange,
  testIdPrefix,
}: {
  value: Screener[];
  onChange: (next: Screener[]) => void;
  testIdPrefix: string;
}) {
  const items: [Screener, string, string][] = [
    ['ai_taskers', 'Qualified AI Taskers', 'Participants Prolific has vetted for AI tasks (labelling, evaluation, red-teaming).'],
    ['fact_checkers', 'Fact Checkers', "Prolific's Fact Checkers expert network."],
    ['approval_rate', 'High approval rate (≥80%)', '80%+ approval rate on past Prolific submissions.'],
  ];
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 9,
        padding: '10px 12px',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--surface-2)',
      }}
    >
      {items.map(([key, label, hint]) => {
        const checked = value.includes(key);
        return (
          <label
            key={key}
            style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer', fontSize: 13.5 }}
          >
            <input
              type="checkbox"
              data-testid={`${testIdPrefix}-${key}`}
              checked={checked}
              onChange={(e) => {
                const next = e.target.checked
                  ? Array.from(new Set([...value, key]))
                  : value.filter((s) => s !== key);
                onChange(next);
              }}
              style={{ width: 16, height: 16, flex: '0 0 auto', margin: '2px 0 0 0', padding: 0, cursor: 'pointer' }}
            />
            <span style={{ lineHeight: 1.4 }}>
              <strong>{label}</strong>
              <span style={{ color: 'var(--muted)' }}> — {hint}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}


// ── Shared step-navigation footer ────────────────────────────────────────

function StepNav({
  stepIndex,
  totalSteps,
  onBack,
  backLabel,
  onNext,
  nextLabel,
  highlightNext,
}: {
  stepIndex: number;
  totalSteps: number;
  onBack?: () => void;
  backLabel: string;
  onNext: (() => void) | null;
  nextLabel: string;
  highlightNext?: boolean;
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        borderTop: '1px solid var(--line)',
        paddingTop: 18,
        marginTop: 8,
      }}
    >
      {onBack ? (
        <button
          type="button"
          onClick={onBack}
          style={{ ...secondaryButton, color: 'var(--muted)' }}
        >
          {backLabel}
        </button>
      ) : (
        <span />
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <span
          style={{
            font: '600 11px var(--font-mono)',
            letterSpacing: '0.1em',
            color: 'var(--muted)',
          }}
        >
          STEP {stepIndex} OF {totalSteps}
          {highlightNext && ' · LAST BEFORE LAUNCH'}
        </span>
        {onNext && (
          <button type="button" onClick={onNext} style={primaryButton}>
            {nextLabel}
          </button>
        )}
      </div>
    </div>
  );
}

export default ExperimentDetail;
