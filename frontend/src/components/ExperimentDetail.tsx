import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import Analytics from './Analytics';
import type {
  Experiment,
  ExperimentRound,
  ExperimentStats,
  PilotStudyCreate,
  RecommendationResponse,
  Upload,
} from '../types';

interface ExperimentDetailProps {
  experiment: Experiment;
  onBack: () => void;
  onDeleted: () => void;
  onRefresh: () => void;
}

// Prolific's API stores reward in the minor unit of the workspace currency.
// For 2-decimal currencies (USD, GBP, EUR, …) that's cents; for zero-decimal
// currencies (JPY, KRW, VND, …) the minor unit IS the major unit, so dividing
// by 100 would render ¥900 as "9.00" and a user typing "900" would send
// ¥90,000 — a silent 100× overpayment. We branch on the workspace currency.
const ZERO_DECIMAL_CURRENCIES = new Set(['JPY', 'KRW', 'VND', 'CLP', 'ISK']);

function rewardDecimals(currencyCode: string | null): number {
  return currencyCode !== null && ZERO_DECIMAL_CURRENCIES.has(currencyCode) ? 0 : 2;
}

function rewardMinorToInput(minorUnits: number, currencyCode: string | null): string {
  if (!Number.isFinite(minorUnits) || minorUnits <= 0) return '';
  const decimals = rewardDecimals(currencyCode);
  return (minorUnits / 10 ** decimals).toFixed(decimals);
}

function rewardInputToMinor(value: string, currencyCode: string | null): number {
  const parsed = parseFloat(value);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.round(parsed * 10 ** rewardDecimals(currencyCode));
}

// Live derived-info under the reward field: per-hour rate (matches Prolific's
// own UI cue for whether the reward meets their minimum) and the participant
// subtotal. Returns null when nothing meaningful can be shown yet.
function rewardHintText(
  rewardInput: string,
  currencyCode: string | null,
  currencySymbol: string | null,
  durationMinutes: number,
  places: number,
): string | null {
  const minor = rewardInputToMinor(rewardInput, currencyCode);
  if (minor <= 0) return null;
  const decimals = rewardDecimals(currencyCode);
  const major = minor / 10 ** decimals;
  const symbol = currencySymbol ?? '';
  const parts: string[] = [];
  if (durationMinutes > 0) {
    const hourly = major / (durationMinutes / 60);
    parts.push(`${symbol}${hourly.toFixed(decimals)}/hour`);
  }
  if (places > 0) {
    const total = major * places;
    parts.push(`Total: ${symbol}${total.toFixed(decimals)} for ${places} ${places === 1 ? 'rater' : 'raters'}`);
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

interface AssistanceMethods {
  searchResults: boolean;
  selectedEvidence: boolean;
  aiConfidence: boolean;
  aiChatAssistant: boolean;
}

function ExperimentDetail({ experiment, onBack, onDeleted, onRefresh }: ExperimentDetailProps) {
  const [stats, setStats] = useState<ExperimentStats | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [showAnalytics, setShowAnalytics] = useState(false);
  const [includePreview, setIncludePreview] = useState(false);
  const [publishingRoundId, setPublishingRoundId] = useState<number | null>(null);
  const [closingRoundId, setClosingRoundId] = useState<number | null>(null);
  const [editingRoundId, setEditingRoundId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<{
    description: string;
    estimated_completion_time: number;
    places: number;
  }>({ description: '', estimated_completion_time: 60, places: 1 });
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
  const [rounds, setRounds] = useState<ExperimentRound[]>([]);
  const [recommendation, setRecommendation] = useState<RecommendationResponse | null>(null);
  const [pilotForm, setPilotForm] = useState<Omit<PilotStudyCreate, 'reward'>>({
    description: '',
    estimated_completion_time: 60,
    pilot_places: 5,
    device_compatibility: ['desktop'],
    study_label: 'annotation',
  });
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
  };

  const handleTopNToggle = async () => {
    const next = !topNEnabled;
    try {
      await saveAssistanceMethod(next ? 'top_n' : 'none', next ? { n: topNValue } : undefined);
      setSuccess(`Top N assistance ${next ? 'enabled' : 'disabled'}`);
      setTimeout(() => setSuccess(null), 2000);
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
      setSuccess('Top N setting updated');
      setTimeout(() => setSuccess(null), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update Top N setting');
    }
  };

  const handleHumanAsAToolToggle = async () => {
    const next = !humanAsATool;
    try {
      await saveAssistanceMethod(
        next ? 'human_as_a_tool' : 'none',
        next ? { confidence_method: confidenceMethod } : undefined
      );
      setSuccess(`Human-as-a-tool ${next ? 'enabled' : 'disabled'}`);
      setTimeout(() => setSuccess(null), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update assistance method');
    }
  };

  const handleConfidenceMethodChange = async (method: string) => {
    setConfidenceMethod(method);
    try {
      await saveAssistanceMethod('human_as_a_tool', { confidence_method: method });
      setSuccess('Confidence method updated');
      setTimeout(() => setSuccess(null), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update confidence method');
    }
  };

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

  const loadRounds = useCallback(async () => {
    try {
      const data = await api.listExperimentRounds(experiment.id);
      setRounds(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load round history');
    }
  }, [experiment.id]);

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

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile) return;

    if (experiment.rating_count > 0) {
      if (!window.confirm(
        `This experiment already has ${experiment.rating_count} ratings. ` +
        `Uploading will ADD more questions (not replace existing ones). Continue?`
      )) {
        return;
      }
    }

    setError(null);
    setSuccess(null);

    try {
      const result = await api.uploadQuestions(experiment.id, uploadFile);
      setSuccess(result.message);
      setUploadFile(null);
      (e.target as HTMLFormElement).reset();
      await loadStats();
      await loadUploads();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const handleDelete = async () => {
    const prolificWarning = rounds.length > 0
      ? ' Linked Prolific studies for every round will also be deleted.'
      : '';
    if (window.confirm(`Delete "${experiment.name}"? This cannot be undone.${prolificWarning}`)) {
      try {
        await api.deleteExperiment(experiment.id);
        onDeleted();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
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
      setSuccess(`Round ${roundNumber === 0 ? 'pilot' : roundNumber} published on Prolific!`);
      setTimeout(() => setSuccess(null), 3000);
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
      // A blank reward input converts to 0, which the backend rejects (ge=1).
      // Treat that as "don't change the reward" rather than sending an invalid
      // value — the user can still edit the other fields without retyping it.
      const rewardMinor = rewardInputToMinor(editRewardInput, currencyCode);
      await api.editExperimentRound(experiment.id, roundId, {
        ...editForm,
        ...(rewardMinor > 0 ? { reward: rewardMinor } : {}),
      });
      setSuccess('Round updated on Prolific.');
      setTimeout(() => setSuccess(null), 3000);
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
      setSuccess(`Round ${roundNumber === 0 ? 'pilot' : roundNumber} closed on Prolific!`);
      setTimeout(() => setSuccess(null), 3000);
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to close round');
    } finally {
      setClosingRoundId(null);
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
      setSuccess('Pilot draft created on Prolific. Publish it when ready.');
      setTimeout(() => setSuccess(null), 4000);
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
    if (!window.confirm(`Launch a new round with ${places} Prolific places?`)) return;
    setError(null);
    try {
      await api.runExperimentRound(experiment.id, places);
      setSuccess(`Round ${nextRoundNumber} draft created on Prolific. Publish it when ready.`);
      setTimeout(() => setSuccess(null), 4000);
      await loadRounds();
      await loadRecommendation();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create study round');
    }
  };

  const latestRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
  const latestRoundClosed = latestRound
    ? ['AWAITING_REVIEW', 'COMPLETED'].includes(latestRound.prolific_study_status)
    : false;
  const nextRoundNumber = latestRound ? latestRound.round_number + 1 : 1;
  const roundLaunchBlockedMessage = !latestRoundClosed && latestRound
    ? `Waiting for ${latestRound.round_number === 0 ? 'the pilot round' : `Round ${latestRound.round_number}`} to close. Current status: ${latestRound.prolific_study_status}.`
    : null;


  if (showAnalytics) {
    return (
      <Analytics
        experimentId={experiment.id}
        experimentName={experiment.name}
        onBack={() => setShowAnalytics(false)}
      />
    );
  }

  const styles = {
    container: {
      maxWidth: '1200px',
      margin: '0 auto',
      padding: '24px',
    },
    header: {
      display: 'flex',
      alignItems: 'center',
      gap: '16px',
      marginBottom: '24px',
      paddingBottom: '16px',
      borderBottom: '1px solid #e0e0e0',
    },
    backButton: {
      background: 'none',
      border: '1px solid #ddd',
      padding: '8px 16px',
      borderRadius: '6px',
      cursor: 'pointer',
      color: '#666',
    },
    title: {
      margin: 0,
      fontSize: '24px',
      fontWeight: 600,
    },
    grid: {
      display: 'grid',
      gridTemplateColumns: '1fr 1fr',
      gap: '24px',
    },
    column: {
      display: 'flex',
      flexDirection: 'column' as const,
      gap: '20px',
    },
    section: {
      background: '#fff',
      borderRadius: '8px',
      border: '1px solid #e0e0e0',
      overflow: 'hidden',
    },
    sectionHeader: {
      padding: '16px 20px',
      borderBottom: '1px solid #e0e0e0',
      background: '#fafafa',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: '12px',
    },
    sectionTitle: {
      margin: 0,
      fontSize: '14px',
      fontWeight: 600,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      color: '#555',
    },
    statusBadge: {
      display: 'inline-flex',
      alignItems: 'center',
      borderRadius: '999px',
      padding: '4px 10px',
      fontSize: '12px',
      fontWeight: 600,
      letterSpacing: '0.2px',
    },
    sectionBody: {
      padding: '20px',
    },
    infoBanner: {
      borderRadius: '8px',
      padding: '12px 14px',
      marginBottom: '16px',
      fontSize: '13px',
      lineHeight: 1.5,
    },
    statsGrid: {
      display: 'grid',
      gridTemplateColumns: 'repeat(2, 1fr)',
      gap: '16px',
    },
    statItem: {
      textAlign: 'center' as const,
      padding: '16px',
      background: '#f8f9fa',
      borderRadius: '6px',
    },
    statValue: {
      fontSize: '28px',
      fontWeight: 700,
      color: '#333',
    },
    statLabel: {
      fontSize: '12px',
      color: '#666',
      marginTop: '4px',
    },
    buttonGroup: {
      display: 'flex',
      gap: '10px',
      marginTop: '16px',
    },
    primaryButton: {
      flex: 1,
      padding: '12px 16px',
      background: '#4a90d9',
      color: '#fff',
      border: 'none',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
      fontWeight: 500,
    },
    secondaryButton: {
      flex: 1,
      padding: '12px 16px',
      background: '#fff',
      color: '#333',
      border: '1px solid #ddd',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
      fontWeight: 500,
    },
    disabledButton: {
      background: '#cbd5e1',
      color: '#475569',
      cursor: 'not-allowed',
      opacity: 0.7,
      boxShadow: 'none',
    },
    inputGroup: {
      marginBottom: '16px',
    },
    label: {
      display: 'block',
      fontSize: '13px',
      fontWeight: 500,
      color: '#333',
      marginBottom: '6px',
    },
    input: {
      width: '100%',
      padding: '10px 12px',
      border: '1px solid #ddd',
      borderRadius: '6px',
      fontSize: '14px',
      fontFamily: 'monospace',
      background: '#f8f9fa',
      cursor: 'pointer',
      boxSizing: 'border-box' as const,
    },
    // Prefixed input. The wrapper carries the input's visual chrome (border,
    // padding, background) and uses flex with `alignItems: 'baseline'` so the
    // prefix glyph and the input text share a single text baseline — no
    // absolute positioning, no line-box math. The inner <input> is stripped
    // of its own chrome so the wrapper is the only thing the user sees.
    rewardInputWrapper: {
      display: 'flex',
      alignItems: 'baseline',
      gap: '4px',
      width: '100%',
      padding: '10px 12px',
      border: '1px solid #ddd',
      borderRadius: '6px',
      background: '#f8f9fa',
      fontSize: '14px',
      fontFamily: 'monospace',
      boxSizing: 'border-box' as const,
    },
    rewardInputPrefix: {
      color: '#666',
      flexShrink: 0,
      pointerEvents: 'none' as const,
    },
    rewardInputField: {
      flex: 1,
      minWidth: 0,
      border: 'none',
      background: 'transparent',
      padding: 0,
      margin: 0,
      fontSize: 'inherit',
      fontFamily: 'inherit',
      lineHeight: 'inherit',
      color: '#333',
      outline: 'none',
    },
    hint: {
      fontSize: '12px',
      color: '#888',
      marginTop: '6px',
    },
    uploadList: {
      marginBottom: '16px',
    },
    uploadItem: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      padding: '10px 12px',
      background: '#f8f9fa',
      borderRadius: '4px',
      marginBottom: '8px',
      fontSize: '13px',
    },
    warning: {
      background: '#fff3cd',
      border: '1px solid #ffc107',
      borderRadius: '6px',
      padding: '12px',
      marginBottom: '16px',
      fontSize: '13px',
    },
    dangerSection: {
      borderColor: '#f5c6cb',
    },
    dangerHeader: {
      background: '#fff5f5',
    },
    dangerButton: {
      background: '#dc3545',
      color: '#fff',
      border: 'none',
      padding: '10px 20px',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
    },
    toggleRow: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
      padding: '16px 0',
      borderBottom: '1px solid #eee',
    },
    toggleInfo: {
      flex: 1,
      paddingRight: '16px',
    },
    toggleLabel: {
      fontSize: '14px',
      fontWeight: 500,
      color: '#333',
      marginBottom: '4px',
    },
    toggleDescription: {
      fontSize: '12px',
      color: '#888',
      lineHeight: 1.4,
    },
    toggle: {
      position: 'relative' as const,
      width: '44px',
      height: '24px',
      flexShrink: 0,
    },
    toggleTrack: {
      width: '44px',
      height: '24px',
      borderRadius: '12px',
      cursor: 'pointer',
      transition: 'background 0.2s',
    },
    toggleThumb: {
      position: 'absolute' as const,
      top: '2px',
      width: '20px',
      height: '20px',
      borderRadius: '50%',
      background: '#fff',
      boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
      transition: 'left 0.2s',
    },
    comingSoon: {
      fontSize: '10px',
      color: '#888',
      background: '#f0f0f0',
      padding: '2px 6px',
      borderRadius: '4px',
      marginLeft: '8px',
    },
  };

  const prolificStatusMeta = prolificEnabled === true
    ? {
        badgeLabel: 'Enabled',
        badgeStyle: { background: '#e8f6ed', color: '#166534' },
        bannerStyle: { ...styles.infoBanner, background: '#eefbf3', border: '1px solid #72c08f', color: '#166534' },
        message: 'Prolific is enabled. Each launch creates an unpublished Prolific draft: start with the pilot, then close each round before creating the next one.',
      }
    : prolificEnabled === 'loading'
      ? {
          badgeLabel: 'Checking...',
          badgeStyle: { background: '#f1f3f5', color: '#495057' },
          bannerStyle: { ...styles.infoBanner, background: '#f8f9fa', border: '1px solid #d0d7de', color: '#495057' },
          message: 'Checking Prolific mode for this environment...',
        }
      : {
          badgeLabel: 'Disabled',
          badgeStyle: { background: '#f8f0f0', color: '#9f1239' },
          bannerStyle: { ...styles.infoBanner, background: '#fff5f5', border: '1px solid #f1b8be', color: '#9f1239' },
          message: 'Prolific is disabled for this environment. Configure a Prolific API token to enable paid rounds.',
        };

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <button onClick={onBack} style={styles.backButton}>← Back</button>
        <div>
          <h1 style={styles.title}>{experiment.internal_name || experiment.name}</h1>
          {experiment.internal_name && (
            <div style={{ fontSize: '13px', color: '#666', marginTop: '4px' }}>
              Public name (shown to raters): {experiment.name}
            </div>
          )}
        </div>
      </div>

      {error && <div className="error" style={{ marginBottom: '16px' }}>{error}</div>}
      {success && <div className="success" style={{ marginBottom: '16px' }}>{success}</div>}

      {/* Two Column Grid */}
      <div style={styles.grid}>
        {/* Left Column */}
        <div style={styles.column}>
          {/* Overview Stats */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <h2 style={styles.sectionTitle}>Overview</h2>
            </div>
            <div style={styles.sectionBody}>
              {stats && (
                <>
                  <div style={styles.statsGrid}>
                    <div style={styles.statItem}>
                      <div style={styles.statValue}>{stats.total_questions}</div>
                      <div style={styles.statLabel}>Questions</div>
                    </div>
                    <div style={styles.statItem}>
                      <div style={styles.statValue}>{stats.questions_complete}</div>
                      <div style={styles.statLabel}>Complete</div>
                    </div>
                    <div style={styles.statItem}>
                      <div style={styles.statValue}>{stats.total_ratings}</div>
                      <div style={styles.statLabel}>Ratings</div>
                    </div>
                    <div style={styles.statItem}>
                      <div style={styles.statValue}>{stats.total_raters}</div>
                      <div style={styles.statLabel}>Raters</div>
                    </div>
                  </div>
                  <div style={{ ...styles.toggleRow, borderBottom: 'none', paddingBottom: '8px' }}>
                    <div style={styles.toggleInfo}>
                      <div style={styles.toggleLabel}>Include preview data</div>
                      <div style={styles.toggleDescription}>
                        Show data from preview sessions in stats, analytics, exports, and round recommendations.
                      </div>
                    </div>
                    <div style={styles.toggle}>
                      <div
                        data-testid="include-preview-toggle"
                        style={{
                          ...styles.toggleTrack,
                          background: includePreview ? '#4a90d9' : '#ddd',
                        }}
                        onClick={() => setIncludePreview(!includePreview)}
                      />
                      <div
                        style={{
                          ...styles.toggleThumb,
                          left: includePreview ? '22px' : '2px',
                        }}
                      />
                    </div>
                  </div>
                  <div style={styles.buttonGroup}>
                    <button style={styles.primaryButton} onClick={() => setShowAnalytics(true)}>
                      View Analytics
                    </button>
                    <a
                      data-testid="export-link"
                      href={api.getExportUrl(experiment.id, { includePreview })}
                      download
                      style={{ flex: 1 }}
                    >
                      <button style={{ ...styles.secondaryButton, width: '100%' }}>
                        Export CSV
                      </button>
                    </a>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* Prolific Study Rounds */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <h2 style={styles.sectionTitle}>Prolific Workflow</h2>
              <span
                data-testid="prolific-mode-badge"
                style={{ ...styles.statusBadge, ...prolificStatusMeta.badgeStyle }}
              >
                {prolificStatusMeta.badgeLabel}
              </span>
            </div>
            <div style={styles.sectionBody}>
              <div
                data-testid="prolific-mode-notice"
                style={prolificStatusMeta.bannerStyle}
              >
                {prolificStatusMeta.message}
                {platformStatusMessage && (
                  <div style={{ marginTop: '8px' }}>
                    {platformStatusMessage}
                  </div>
                )}
              </div>

              {/* Preview link always available */}
              <div style={{ ...styles.inputGroup, marginBottom: '20px' }}>
                <button
                  data-testid="preview-participant-button"
                  onClick={() => {
                    const previewId = `preview_${Date.now()}`;
                    const url = `${window.location.origin}/rate?experiment_id=${experiment.id}&PROLIFIC_PID=${previewId}&STUDY_ID=preview&SESSION_ID=preview&preview=true`;
                    window.open(url, '_blank');
                  }}
                  style={styles.secondaryButton}
                >
                  Preview as Participant
                </button>
              </div>

              {prolificEnabled === true && (
                <>
                  {/* Existing rounds list */}
                  {rounds.length > 0 && (
                  <div data-testid="study-rounds-list" style={{ marginBottom: '20px' }}>
                    {rounds.map((round) => (
                      <div key={round.id} style={{
                        padding: '12px',
                        background: '#f8f9fa',
                        borderRadius: '6px',
                        marginBottom: '8px',
                        border: '1px solid #e0e0e0',
                      }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <div>
                            <span style={{ fontWeight: 600, fontSize: '14px' }}>
                              {round.round_number === 0 ? 'Pilot Round' : `Round ${round.round_number}`}
                            </span>
                            <span style={{ marginLeft: '8px', fontSize: '12px', color: '#666' }}>
                              {round.places_requested} places
                            </span>
                          </div>
                          <span style={{
                            padding: '3px 8px',
                            borderRadius: '4px',
                            fontSize: '12px',
                            fontWeight: 500,
                            background: round.prolific_study_status === 'ACTIVE' ? '#d4edda'
                              : ['COMPLETED', 'AWAITING_REVIEW'].includes(round.prolific_study_status) ? '#d1ecf1'
                              : '#fff3cd',
                            color: round.prolific_study_status === 'ACTIVE' ? '#155724'
                              : ['COMPLETED', 'AWAITING_REVIEW'].includes(round.prolific_study_status) ? '#0c5460'
                              : '#856404',
                          }}>
                            {round.prolific_study_status}
                          </span>
                        </div>
                        <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
                          <button
                            onClick={() => window.open(round.prolific_study_url, '_blank')}
                            style={{ ...styles.secondaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px' }}
                          >
                            Open on Prolific
                          </button>
                          {round.prolific_study_status === 'UNPUBLISHED' && editingRoundId !== round.id && (
                            <button
                              data-testid={`edit-round-${round.round_number}`}
                              onClick={() => handleStartEditRound(round)}
                              style={{ ...styles.secondaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px' }}
                            >
                              Edit
                            </button>
                          )}
                          {round.prolific_study_status === 'UNPUBLISHED' && (
                            <button
                              data-testid={`publish-round-${round.round_number}`}
                              onClick={() => handlePublishRound(round.id, round.round_number)}
                              disabled={publishingRoundId === round.id || editingRoundId === round.id}
                              style={{ ...styles.primaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px' }}
                            >
                              {publishingRoundId === round.id ? 'Publishing...' : 'Publish'}
                            </button>
                          )}
                          {!['UNPUBLISHED', 'AWAITING_REVIEW', 'COMPLETED'].includes(round.prolific_study_status) && (
                            <button
                              data-testid={`close-round-${round.round_number}`}
                              onClick={() => handleCloseRound(round.id, round.round_number)}
                              disabled={closingRoundId === round.id}
                              style={{ ...styles.primaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px', background: '#5f6b7a' }}
                            >
                              {closingRoundId === round.id ? 'Closing...' : 'Close Round'}
                            </button>
                          )}
                        </div>
                        {editingRoundId === round.id && (
                          <div
                            data-testid={`edit-round-form-${round.round_number}`}
                            style={{ marginTop: '12px', padding: '12px', background: '#f8f9fa', borderRadius: '6px', border: '1px solid #e0e0e0' }}
                          >
                            <div style={{ ...styles.inputGroup, marginBottom: '8px' }}>
                              <label style={{ ...styles.label, fontSize: '12px' }}>Description</label>
                              <textarea
                                data-testid={`edit-round-description-${round.round_number}`}
                                value={editForm.description}
                                onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                                rows={3}
                                style={{ ...styles.input, fontFamily: 'inherit', resize: 'vertical' as const }}
                              />
                            </div>
                            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '8px', marginBottom: '8px' }}>
                              <div>
                                <label style={{ ...styles.label, fontSize: '12px' }}>Time (min)</label>
                                <input
                                  data-testid={`edit-round-time-${round.round_number}`}
                                  type="number"
                                  min="1"
                                  value={editForm.estimated_completion_time}
                                  onChange={(e) => setEditForm({ ...editForm, estimated_completion_time: parseInt(e.target.value) || 0 })}
                                  style={styles.input}
                                />
                              </div>
                              <div>
                                <label style={{ ...styles.label, fontSize: '12px' }}>
                                  Reward{currencyCode ? ` (${currencyCode})` : ''}
                                </label>
                                <div className="reward-input" style={styles.rewardInputWrapper}>
                                  {currencySymbol && (
                                    <span style={styles.rewardInputPrefix}>{currencySymbol}</span>
                                  )}
                                  <input
                                    data-testid={`edit-round-reward-${round.round_number}`}
                                    type="text"
                                    inputMode="decimal"
                                    value={editRewardInput}
                                    onChange={(e) => {
                                      if (e.target.value === '' || /^[0-9]*\.?[0-9]*$/.test(e.target.value)) {
                                        setEditRewardInput(e.target.value);
                                      }
                                    }}
                                    style={styles.rewardInputField}
                                  />
                                </div>
                              </div>
                              <div>
                                <label style={{ ...styles.label, fontSize: '12px' }}>Places</label>
                                <input
                                  data-testid={`edit-round-places-${round.round_number}`}
                                  type="number"
                                  min="1"
                                  value={editForm.places}
                                  onChange={(e) => setEditForm({ ...editForm, places: parseInt(e.target.value) || 0 })}
                                  style={styles.input}
                                />
                              </div>
                            </div>
                            {(() => {
                              const text = rewardHintText(
                                editRewardInput,
                                currencyCode,
                                currencySymbol,
                                editForm.estimated_completion_time,
                                editForm.places,
                              );
                              return text ? <div style={{ ...styles.hint, marginBottom: '8px' }}>{text}</div> : null;
                            })()}
                            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                              <button
                                data-testid={`edit-round-cancel-${round.round_number}`}
                                onClick={handleCancelEditRound}
                                disabled={savingEdit}
                                style={{ ...styles.secondaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px' }}
                              >
                                Cancel
                              </button>
                              <button
                                data-testid={`edit-round-save-${round.round_number}`}
                                onClick={() => handleSaveEditRound(round.id)}
                                disabled={savingEdit}
                                style={{ ...styles.primaryButton, flex: 'none', padding: '6px 12px', fontSize: '12px' }}
                              >
                                {savingEdit ? 'Saving...' : 'Save'}
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                  )}

                  {/* Recommendation panel (when pilot has data) */}
                  {recommendation && recommendation.avg_time_per_question_seconds > 0 && (
                  <div data-testid="recommendation-panel" style={{
                    padding: '12px',
                    background: recommendation.is_complete ? '#d4edda' : '#f0f7ff',
                    borderRadius: '6px',
                    marginBottom: '16px',
                    fontSize: '13px',
                  }}>
                    {recommendation.is_complete ? (
                      <strong style={{ color: '#155724' }}>All questions have enough ratings!</strong>
                    ) : (
                      <>
                        <div style={{ marginBottom: '6px' }}>
                          <strong>Recommendation for next round</strong>
                        </div>
                        <div style={{ color: '#444', lineHeight: 1.6 }}>
                          Avg time/question: <strong>{recommendation.avg_time_per_question_seconds.toFixed(0)}s</strong>
                          {' · '}Remaining actions: <strong>{recommendation.remaining_rating_actions}</strong>
                          {' · '}Hours left: <strong>{recommendation.total_hours_remaining.toFixed(1)}</strong>
                        </div>
                        <button
                          data-testid="launch-round-button"
                          onClick={handleRunRound}
                          disabled={!latestRoundClosed}
                          style={{
                            ...styles.primaryButton,
                            ...(!latestRoundClosed ? styles.disabledButton : {}),
                            marginTop: '10px',
                            width: 'auto',
                            padding: '8px 16px',
                          }}
                        >
                          Create Round {nextRoundNumber} Draft ({recommendation.recommended_places} places)
                        </button>
                        {roundLaunchBlockedMessage && (
                          <div style={{ marginTop: '8px', color: '#666', lineHeight: 1.5 }}>
                            {roundLaunchBlockedMessage}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                  )}

                  {/* Pilot form — shown when no pilot exists yet */}
                  {rounds.length === 0 && (
                  <form onSubmit={handleRunPilot}>
                    <div style={{ fontSize: '13px', color: '#555', marginBottom: '12px' }}>
                      Create the first unpublished round with the study configuration you want to reuse. Pilot timing data drives the recommended size for later rounds.
                    </div>
                    <div style={styles.inputGroup}>
                      <label htmlFor="pilot-description" style={styles.label}>Study Description</label>
                      <textarea
                        id="pilot-description"
                        data-testid="pilot-description-input"
                        value={pilotForm.description}
                        onChange={(e) => setPilotForm({ ...pilotForm, description: e.target.value })}
                        placeholder={"Describe the task for Prolific participants...\n\nMarkdown is supported: # heading, ## subheading, **bold**, *italic*, ~~strike~~, -/1. lists. Blank lines separate paragraphs."}
                        required
                        style={{ ...styles.input, minHeight: '120px', resize: 'vertical' as const, fontFamily: 'inherit' }}
                      />
                      <div style={styles.hint}>
                        Markdown is converted to Prolific's HTML subset (headings, bold/italic/strike, lists, paragraphs). Links and images are not supported by Prolific.
                      </div>
                    </div>
                    <div style={styles.inputGroup}>
                      <label htmlFor="pilot-study-label" style={styles.label}>Study Label</label>
                      <select
                        id="pilot-study-label"
                        data-testid="pilot-study-label-select"
                        value={pilotForm.study_label}
                        onChange={(e) => setPilotForm({ ...pilotForm, study_label: e.target.value as PilotStudyCreate['study_label'] })}
                        style={styles.input}
                      >
                        <option value="annotation">Annotation</option>
                        <option value="survey">Survey</option>
                        <option value="decision_making_task">Decision-making task</option>
                        <option value="writing_task">Writing task</option>
                        <option value="interview">Interview</option>
                        <option value="other">Other</option>
                      </select>
                      <div style={styles.hint}>Categorises the study on Prolific; participants see this tag when browsing.</div>
                    </div>
                    <div style={styles.inputGroup}>
                      <label htmlFor="pilot-estimated-completion-time" style={styles.label}>Estimated Completion Time (minutes)</label>
                      <input
                        id="pilot-estimated-completion-time"
                        data-testid="pilot-estimated-completion-time-input"
                        type="number"
                        value={pilotForm.estimated_completion_time}
                        onChange={(e) => setPilotForm({ ...pilotForm, estimated_completion_time: parseInt(e.target.value) || 0 })}
                        min="1"
                        required
                        style={styles.input}
                      />
                    </div>
                    <div style={styles.inputGroup}>
                      <label htmlFor="pilot-reward" style={styles.label}>
                        Reward{currencyCode ? ` (${currencyCode})` : ''}
                      </label>
                      <div className="reward-input" style={styles.rewardInputWrapper}>
                        {currencySymbol && (
                          <span style={styles.rewardInputPrefix}>{currencySymbol}</span>
                        )}
                        <input
                          id="pilot-reward"
                          data-testid="pilot-reward-input"
                          type="text"
                          inputMode="decimal"
                          value={pilotRewardInput}
                          onChange={(e) => {
                            if (e.target.value === '' || /^[0-9]*\.?[0-9]*$/.test(e.target.value)) {
                              setPilotRewardInput(e.target.value);
                            }
                          }}
                          required
                          style={styles.rewardInputField}
                        />
                      </div>
                      <div style={styles.hint}>
                        {rewardHintText(
                          pilotRewardInput,
                          currencyCode,
                          currencySymbol,
                          pilotForm.estimated_completion_time,
                          pilotForm.pilot_places,
                        ) ?? "Enter the amount as you'd see it on Prolific."}
                      </div>
                    </div>
                    <div style={styles.inputGroup}>
                      <label htmlFor="pilot-places" style={styles.label}>Number of Raters</label>
                      <input
                        id="pilot-places"
                        data-testid="pilot-places-input"
                        type="number"
                        value={pilotForm.pilot_places}
                        onChange={(e) => setPilotForm({ ...pilotForm, pilot_places: parseInt(e.target.value) || 0 })}
                        min="1"
                        required
                        style={styles.input}
                      />
                      <div style={styles.hint}>Each rater does 1 hour. 5 is a good default for timing calibration.</div>
                    </div>
                    <button data-testid="run-pilot-button" type="submit" style={styles.primaryButton}>
                      Create Pilot Draft
                    </button>
                  </form>
                  )}

                  {experiment.prolific_completion_url && (
                  <div style={{ ...styles.inputGroup, marginTop: rounds.length === 0 ? '16px' : '0' }}>
                    <label style={styles.label}>Completion URL</label>
                    <input
                      data-testid="completion-url-input"
                      type="text"
                      value={experiment.prolific_completion_url}
                      readOnly
                      style={styles.input}
                    />
                    <div style={styles.hint}>Raters redirect here when finished.</div>
                  </div>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Danger Zone */}
          <div style={{ ...styles.section, ...styles.dangerSection }}>
            <div style={{ ...styles.sectionHeader, ...styles.dangerHeader }}>
              <h2 style={{ ...styles.sectionTitle, color: '#dc3545' }}>Danger Zone</h2>
            </div>
            <div style={styles.sectionBody}>
              <p style={{ fontSize: '13px', color: '#666', marginBottom: '12px' }}>
                Permanently delete this experiment and all associated data.
              </p>
              <button onClick={handleDelete} style={styles.dangerButton}>
                Delete Experiment
              </button>
            </div>
          </div>
        </div>

        {/* Right Column */}
        <div style={styles.column}>
          {/* Questions */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <h2 style={styles.sectionTitle}>Questions</h2>
            </div>
            <div style={styles.sectionBody}>
              {/* Uploaded files list */}
              {uploads.length > 0 && (
                <div style={styles.uploadList}>
                  {uploads.map((upload) => (
                    <div key={upload.id} style={styles.uploadItem}>
                      <span style={{ fontFamily: 'monospace' }}>{upload.filename}</span>
                      <span style={{ color: '#666' }}>
                        {upload.question_count} questions
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {/* Warning if ratings exist */}
              {experiment.rating_count > 0 && (
                <div style={styles.warning}>
                  <strong>Note:</strong> Uploading adds questions, doesn't replace existing ones.
                </div>
              )}

              {/* Upload form */}
              <form onSubmit={handleUpload}>
                <div style={styles.inputGroup}>
                  <label htmlFor="upload-csv" style={styles.label}>Add Questions from CSV</label>
                  <input
                    id="upload-csv"
                    data-testid="upload-csv-input"
                    type="file"
                    accept=".csv"
                    onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
                    style={{ fontSize: '14px' }}
                  />
                  <div style={styles.hint}>
                    Required: question_id, question_text. Optional: gt_answer, options, question_type, metadata, parent_question_id. Supports long-context rows and files up to 200MB.
                  </div>
                </div>
                <button
                  data-testid="upload-csv-button"
                  type="submit"
                  disabled={!uploadFile}
                  style={{
                    ...styles.primaryButton,
                    opacity: uploadFile ? 1 : 0.5,
                    cursor: uploadFile ? 'pointer' : 'not-allowed',
                  }}
                >
                  Upload CSV
                </button>
              </form>
            </div>
          </div>

          {/* Rater Assistance Methods */}
          <div style={styles.section}>
            <div style={styles.sectionHeader}>
              <h2 style={styles.sectionTitle}>Rater Assistance Methods</h2>
            </div>
            <div style={styles.sectionBody}>
              <div style={{ ...styles.infoBanner, background: '#f8fafc', border: '1px solid #dbe3ec', color: '#475569' }}>
                Choose one assistance mode for this experiment. Changes apply to new participant assistance sessions.
              </div>

              {/* Top N Toggle */}
              <div style={styles.toggleRow}>
                <div style={styles.toggleInfo}>
                  <div style={styles.toggleLabel}>Top N Suggestions</div>
                  <div style={styles.toggleDescription}>
                    AI ranks the most likely answers and shows raters a short ordered list before they submit their own rating.
                  </div>
                </div>
                <div style={styles.toggle}>
                  <div
                    style={{
                      ...styles.toggleTrack,
                      background: topNEnabled ? '#4a90d9' : '#ddd',
                    }}
                    onClick={handleTopNToggle}
                  />
                  <div
                    style={{
                      ...styles.toggleThumb,
                      left: topNEnabled ? '22px' : '2px',
                    }}
                  />
                </div>
              </div>

              {topNEnabled && (
                <div style={{ padding: '12px 0 16px 0', borderBottom: '1px solid #f0f0f0', marginBottom: '4px' }}>
                  <label htmlFor="top-n-input" style={{ fontSize: '13px', fontWeight: 500, color: '#555', display: 'block', marginBottom: '8px' }}>
                    Suggestions to show
                  </label>
                  <input
                    id="top-n-input"
                    type="number"
                    min="1"
                    max="10"
                    value={topNValue}
                    onChange={e => handleTopNChange(parseInt(e.target.value) || 1)}
                    style={{
                      padding: '8px 12px',
                      border: '1px solid #ddd',
                      borderRadius: '6px',
                      fontSize: '13px',
                      color: '#333',
                      background: '#fff',
                      width: '120px',
                      boxSizing: 'border-box',
                    }}
                  />
                  <div style={styles.hint}>For multiple-choice questions, this is capped by the number of available options.</div>
                </div>
              )}

              {/* Human-as-a-Tool Toggle */}
              <div style={styles.toggleRow}>
                <div style={styles.toggleInfo}>
                  <div style={styles.toggleLabel}>Human-as-a-Tool</div>
                  <div style={styles.toggleDescription}>
                    AI decomposes each question into subtasks. Raters answer each subtask, then the AI synthesises a final recommendation.
                  </div>
                </div>
                <div style={styles.toggle}>
                  <div
                    style={{
                      ...styles.toggleTrack,
                      background: humanAsATool ? '#4a90d9' : '#ddd',
                    }}
                    onClick={handleHumanAsAToolToggle}
                  />
                  <div
                    style={{
                      ...styles.toggleThumb,
                      left: humanAsATool ? '22px' : '2px',
                    }}
                  />
                </div>
              </div>

              {/* Confidence method sub-selector (shown when Human-as-a-Tool is on) */}
              {humanAsATool && (
                <div style={{ padding: '12px 0 16px 0', borderBottom: '1px solid #f0f0f0', marginBottom: '4px' }}>
                  <label style={{ fontSize: '13px', fontWeight: 500, color: '#555', display: 'block', marginBottom: '8px' }}>
                    Confidence method
                  </label>
                  <select
                    value={confidenceMethod}
                    onChange={e => handleConfidenceMethodChange(e.target.value)}
                    style={{
                      padding: '8px 12px',
                      border: '1px solid #ddd',
                      borderRadius: '6px',
                      fontSize: '13px',
                      color: '#333',
                      background: '#fff',
                      cursor: 'pointer',
                      width: '100%',
                      maxWidth: '320px',
                    }}
                  >
                    <option value="self_report">Self-report — single call, fastest</option>
                    <option value="sampling">Sampling — K samples + clustering, most accurate</option>
                    <option value="self_consistency">Self-consistency — K samples, majority vote</option>
                  </select>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default ExperimentDetail;
