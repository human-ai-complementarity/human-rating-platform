import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { api } from '../../api';
import { buildRaterPreviewUrl } from '../../raterPreview';
import type {
  Experiment,
  PilotCheckFinding,
  PilotCheckProbe,
  PilotCheckRun,
  PilotCheckSeverity,
  PilotCheckStage,
  PilotCheckState,
  PilotCheckSummary,
} from '../../types';
import { Banner, SectionCard, primaryButton, secondaryButton } from './ui';

/**
 * The pilot check (docs/pilot-check.md) on the Launch tab: the latest run's
 * state, a button to run it, and its findings grouped by check with a link
 * that opens each flagged question in the participant preview.
 *
 * The summary comes with the experiment (`experiment.pilot_check`), computed
 * by the backend's launch gate itself; the run detail is fetched here. While
 * a run is in flight the card polls until it lands.
 */

const POLL_MS = 3000;

const STATE_LABEL: Record<PilotCheckState, string> = {
  not_run: 'Not run',
  running: 'Running',
  stale: 'Stale',
  passed: 'Passed',
  warnings: 'Warnings',
  failed: 'Failed',
  aborted: 'Did not finish',
};

const STATE_TONE: Record<PilotCheckState, 'info' | 'ok' | 'warn' | 'danger'> = {
  not_run: 'warn',
  running: 'info',
  stale: 'warn',
  passed: 'ok',
  warnings: 'warn',
  failed: 'danger',
  aborted: 'danger',
};

const SEVERITY_ORDER: Record<PilotCheckSeverity, number> = { error: 0, warning: 1, info: 2 };

const CHECK_LABEL: Record<string, string> = {
  consent_configured: 'Consent statement',
  launch_blockers: 'Launch blockers',
  markdown_mode: 'Markdown mode',
  question_renders: 'Question rendering',
  assistance_output: 'Assistance output',
  assistance_health: 'Assistance health',
  instructions_coherent: 'Instructions',
  question_coherent: 'Question coherence',
  assistance_useful: 'Assistance usefulness',
};

export function checkLabel(name: string): string {
  return CHECK_LABEL[name] ?? name.replace(/_/g, ' ');
}

export function describeSummary(summary: PilotCheckSummary | null | undefined): string {
  if (!summary) return 'Pilot check state unknown.';
  const { state, counts, reason } = summary;
  if (state === 'passed') return 'Pilot check passed.';
  if (state === 'warnings' && !summary.needs_acknowledgement) {
    return `Pilot check passed with ${counts.warning} warning${counts.warning === 1 ? '' : 's'}.`;
  }
  return reason ?? STATE_LABEL[state];
}

function formatWhen(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

export function PilotCheckCard({
  experiment,
  summary,
  onRunStateChange,
}: {
  experiment: Experiment;
  summary: PilotCheckSummary | null | undefined;
  // The parent refreshes the experiment (and so the summary) when a run lands.
  onRunStateChange: () => void;
}) {
  const [run, setRun] = useState<PilotCheckRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSteps, setShowSteps] = useState<Record<number, boolean>>({});

  const state: PilotCheckState = summary?.state ?? 'not_run';
  const polling = state === 'running' || run?.status === 'running';
  // The parent's refresh callback may change identity on every render; the
  // poll reads it through a ref so the interval is not torn down each time.
  const onRunStateChangeRef = useRef(onRunStateChange);
  useEffect(() => {
    onRunStateChangeRef.current = onRunStateChange;
  }, [onRunStateChange]);

  // Load the latest run on mount and whenever the summary points at a new one.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.getPilotCheck(experiment.id).then(
      (latest) => {
        if (cancelled) return;
        setRun(latest);
        setLoading(false);
      },
      (err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load the pilot check');
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [experiment.id, summary?.run_id]);

  // Poll while a run is in flight; tell the parent when it lands.
  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const latest = await api.getPilotCheck(experiment.id);
        if (cancelled) return;
        setRun(latest);
        if (latest && latest.status !== 'running') {
          window.clearInterval(timer);
          onRunStateChangeRef.current();
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to refresh the pilot check');
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [polling, experiment.id]);

  const handleRun = async () => {
    setError(null);
    setStarting(true);
    try {
      const started = await api.runPilotCheck(experiment.id);
      setRun(started);
      onRunStateChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start the pilot check');
    } finally {
      setStarting(false);
    }
  };

  const grouped = useMemo(() => groupFindings(run?.findings ?? []), [run]);
  const probesByQuestion = useMemo(() => {
    const map = new Map<number, PilotCheckProbe>();
    for (const probe of run?.assistance_steps ?? []) map.set(probe.question_id, probe);
    return map;
  }, [run]);

  const canRun = !polling && !starting && experiment.status !== 'FINISHED';
  const runLabel = polling ? 'Running…' : run ? 'Re-run check' : 'Run check';
  const bannerTone = STATE_TONE[state];
  const countsLine =
    summary && summary.run_id !== null && state !== 'running'
      ? `${summary.counts.error} error${summary.counts.error === 1 ? '' : 's'}, ` +
        `${summary.counts.warning} warning${summary.counts.warning === 1 ? '' : 's'}, ` +
        `${summary.counts.info} note${summary.counts.info === 1 ? '' : 's'}`
      : null;

  return (
    <div data-testid="pilot-check-card">
      <SectionCard
        header={
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span>Pilot check</span>
            <StatePill state={state} />
          </div>
        }
      >
        <p style={{ fontSize: 13.5, color: 'var(--muted)', margin: '0 0 12px' }}>
          Samples {run?.sample_size ?? 'a few'} questions, walks each through what a rater sees,
          runs the assistance method on it, and flags anything a rater would trip over. Run it
          before creating a study, and again after changing instructions or questions.
        </p>

        <div style={{ marginBottom: 12 }}>
          <Banner tone={bannerTone}>
            <div data-testid="pilot-check-banner">
              <strong>{describeSummary(summary)}</strong>
              {run && !polling && (
                <span style={{ color: 'var(--muted)' }}>
                  {' '}
                  {run.finished_at && `Last run ${formatWhen(run.finished_at)}`}
                  {run.requested_by && ` by ${run.requested_by}`}
                  {countsLine && `: ${countsLine}.`}
                </span>
              )}
              {summary?.needs_acknowledgement && summary.gate_mode === 'warn' && (
                <div style={{ marginTop: 4, fontSize: 12.5 }}>
                  Launching will ask you to acknowledge this.
                </div>
              )}
              {summary?.needs_acknowledgement && summary.gate_mode === 'block' && (
                <div style={{ marginTop: 4, fontSize: 12.5 }}>
                  Launching is blocked until the check passes.
                </div>
              )}
            </div>
          </Banner>
        </div>

        <PilotCheckStepper
          run={run}
          state={state}
          assisted={experiment.assistance_method !== 'none'}
        />

        {error && (
          <div role="alert" style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 10 }}>
            {error}
          </div>
        )}
        {run?.status === 'aborted' && run.error && (
          <div style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 10 }}>{run.error}</div>
        )}

        <button
          type="button"
          data-testid="run-pilot-check-button"
          onClick={handleRun}
          disabled={!canRun}
          style={{
            ...primaryButton,
            ...(canRun ? {} : { opacity: 0.5, cursor: 'not-allowed' }),
          }}
        >
          {starting ? 'Starting…' : runLabel}
        </button>

        {!loading && run && run.status !== 'running' && (
          <div style={{ marginTop: 18 }}>
            {grouped.length === 0 ? (
              <div style={{ fontSize: 13, color: 'var(--muted)' }}>No findings.</div>
            ) : (
              <div data-testid="pilot-check-findings" style={{ display: 'grid', gap: 14 }}>
                {grouped.map(({ check, findings }) => (
                  <div key={check}>
                    <div
                      style={{
                        font: '600 11px/1 var(--font-mono)',
                        letterSpacing: '0.12em',
                        textTransform: 'uppercase',
                        color: 'var(--muted)',
                        marginBottom: 6,
                      }}
                    >
                      {checkLabel(check)}
                    </div>
                    <div style={{ display: 'grid', gap: 6 }}>
                      {findings.map((finding, index) => (
                        <FindingRow
                          key={`${check}-${index}`}
                          finding={finding}
                          experimentId={experiment.id}
                          probe={
                            finding.question_id !== null
                              ? probesByQuestion.get(finding.question_id)
                              : undefined
                          }
                          showProbe={
                            finding.question_id !== null && !!showSteps[finding.question_id]
                          }
                          onToggleProbe={() => {
                            if (finding.question_id === null) return;
                            const qid = finding.question_id;
                            setShowSteps((prev) => ({ ...prev, [qid]: !prev[qid] }));
                          }}
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>
              Sampled {run.sample_question_ids.length} question
              {run.sample_question_ids.length === 1 ? '' : 's'}
              {run.judge_model ? ` · judged by ${run.judge_model}` : ' · no judge available'}
              {run.terms_ref ? ` · consent ${run.terms_ref.split(':')[0]}` : ''}
            </div>
          </div>
        )}
      </SectionCard>
    </div>
  );
}

function groupFindings(findings: PilotCheckFinding[]): { check: string; findings: PilotCheckFinding[] }[] {
  const byCheck = new Map<string, PilotCheckFinding[]>();
  for (const finding of findings) {
    const list = byCheck.get(finding.check) ?? [];
    list.push(finding);
    byCheck.set(finding.check, list);
  }
  const groups = [...byCheck.entries()].map(([check, list]) => ({
    check,
    findings: [...list].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]),
  }));
  // Worst groups first, so an error is never below a note.
  const worst = (list: PilotCheckFinding[]) => Math.min(...list.map((f) => SEVERITY_ORDER[f.severity]));
  return groups.sort((a, b) => worst(a.findings) - worst(b.findings));
}

function FindingRow({
  finding,
  experimentId,
  probe,
  showProbe,
  onToggleProbe,
}: {
  finding: PilotCheckFinding;
  experimentId: number;
  probe: PilotCheckProbe | undefined;
  showProbe: boolean;
  onToggleProbe: () => void;
}) {
  const color =
    finding.severity === 'error'
      ? 'var(--danger)'
      : finding.severity === 'warning'
        ? 'var(--warn)'
        : 'var(--muted)';
  return (
    <div
      data-testid={`pilot-check-finding-${finding.severity}`}
      style={{
        display: 'flex',
        gap: 10,
        alignItems: 'flex-start',
        fontSize: 13.5,
        lineHeight: 1.5,
        padding: '8px 10px',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--surface-2)',
      }}
    >
      <span
        style={{
          font: '600 10px/1 var(--font-mono)',
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color,
          padding: '4px 0',
          minWidth: 58,
        }}
      >
        {finding.severity}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ color: 'var(--ink)' }}>{finding.message}</div>
        {finding.detail && (
          <pre
            style={{
              margin: '6px 0 0',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              font: '400 12px/1.5 var(--font-mono)',
              color: 'var(--muted)',
            }}
          >
            {finding.detail}
          </pre>
        )}
        {finding.question_id !== null && (
          <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 12.5 }}>
            <a
              href={buildRaterPreviewUrl(experimentId, finding.question_id)}
              target="_blank"
              rel="noreferrer"
              style={{ color: 'var(--accent)' }}
            >
              Open question {finding.question_id} in preview
            </a>
            {probe && (
              <button
                type="button"
                onClick={onToggleProbe}
                style={{
                  ...secondaryButton,
                  padding: '2px 8px',
                  fontSize: 12,
                  lineHeight: 1.4,
                }}
              >
                {showProbe ? 'Hide what the AI returned' : 'Show what the AI returned'}
              </button>
            )}
          </div>
        )}
        {probe && showProbe && (
          <pre
            data-testid="pilot-check-probe"
            style={{
              margin: '8px 0 0',
              maxHeight: 280,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              font: '400 11.5px/1.5 var(--font-mono)',
              color: 'var(--ink)',
              background: 'var(--surface)',
              border: '1px solid var(--faint)',
              borderRadius: 'var(--radius-sm)',
              padding: 10,
            }}
          >
            {`step: ${probe.step_type} · ${probe.latency_ms} ms` +
              (probe.failure_reason ? ` · ${probe.failure_reason}` : '') +
              (probe.failure_detail ? `\n${probe.failure_detail}` : '') +
              `\n${JSON.stringify(probe.payload, null, 2)}`}
          </pre>
        )}
      </div>
    </div>
  );
}

// --- Stepper ---------------------------------------------------------------

const STAGE_LABEL: Record<PilotCheckStage, string> = {
  sampling: 'Sample questions',
  assistance: 'Probe assistance',
  checks: 'Check rendering',
  instructions_coherent: 'Review instructions',
  question_coherent: 'Review questions',
  assistance_useful: 'Review assistance',
  done: 'Result',
};

const STAGE_HINT: Record<PilotCheckStage, string> = {
  sampling: 'Pick questions and read the consent statement',
  assistance: 'Run the assistance method on each one',
  checks: 'Consent, rendering, options and launch blockers',
  instructions_coherent: 'A model reads the instructions as a rater would',
  question_coherent: 'A model reads each sampled question as a rater would',
  assistance_useful: 'A model judges whether the assistance helps on each question',
  done: 'Findings',
};

type StepStatus = 'pending' | 'active' | 'done' | 'failed';
type StepTone = 'ok' | 'warn' | 'danger';

// The plan before any run exists; a run carries its own.
function defaultStages(assisted: boolean): PilotCheckStage[] {
  return assisted
    ? [
        'sampling',
        'assistance',
        'checks',
        'instructions_coherent',
        'question_coherent',
        'assistance_useful',
        'done',
      ]
    : ['sampling', 'checks', 'instructions_coherent', 'question_coherent', 'done'];
}

/** One status per stage, from where the run is. */
export function stepStatuses(stages: PilotCheckStage[], run: PilotCheckRun | null): StepStatus[] {
  if (!run) return stages.map(() => 'pending');
  const at = Math.max(0, stages.indexOf(run.stage ?? 'sampling'));
  if (run.status === 'running') {
    return stages.map((_, i) => (i < at ? 'done' : i === at ? 'active' : 'pending'));
  }
  if (run.status === 'aborted') {
    return stages.map((_, i) => (i < at ? 'done' : i === at ? 'failed' : 'pending'));
  }
  return stages.map(() => 'done');
}

const KEYFRAMES = `
@keyframes pilot-step-glow {
  0%, 100% { box-shadow: 0 0 0 4px var(--step-halo), 0 0 10px var(--step-color); }
  50% { box-shadow: 0 0 0 7px var(--step-halo), 0 0 22px var(--step-color); }
}`;

function PilotCheckStepper({
  run,
  state,
  assisted,
}: {
  run: PilotCheckRun | null;
  state: PilotCheckState;
  assisted: boolean;
}) {
  const stages: PilotCheckStage[] =
    run && run.stages.length > 0 ? run.stages : defaultStages(assisted);
  const statuses = stepStatuses(stages, run);
  // The final dot takes the outcome's colour once the run has landed.
  const outcome: StepTone =
    run?.status === 'passed' ? 'ok' : run?.status === 'warnings' ? 'warn' : 'danger';
  const finished = !!run && run.status !== 'running';
  const toneColor = (tone: StepTone) =>
    tone === 'ok' ? 'var(--accent)' : tone === 'warn' ? 'var(--warn)' : 'var(--danger)';

  return (
    <div
      data-testid="pilot-check-stepper"
      data-state={state}
      style={{ margin: '4px 0 16px', padding: '12px 4px 4px' }}
    >
      <style>{KEYFRAMES}</style>
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(${stages.length}, 1fr)` }}>
        {stages.map((stage, i) => {
          const status = statuses[i];
          const isLast = i === stages.length - 1;
          const color =
            status === 'failed'
              ? 'var(--danger)'
              : status === 'pending'
                ? 'var(--faint)'
                : isLast && finished
                  ? toneColor(outcome)
                  : 'var(--accent)';
          const lit = status === 'done' || status === 'active' || status === 'failed';
          const halo = `color-mix(in srgb, ${color} 22%, transparent)`;
          // A connector is lit once the step it leads to has started.
          const leftLit = i > 0 && statuses[i] !== 'pending';
          const rightLit = i < stages.length - 1 && statuses[i + 1] !== 'pending';
          const detail = status === 'active' ? run?.stage_detail : null;
          return (
            <div
              key={stage}
              data-testid={`pilot-check-step-${stage}`}
              data-status={status}
              style={{ position: 'relative', textAlign: 'center', minWidth: 0 }}
            >
              {i > 0 && (
                <div
                  aria-hidden
                  style={{
                    position: 'absolute',
                    top: 7,
                    left: 0,
                    width: '50%',
                    height: 2,
                    background: leftLit ? 'var(--accent)' : 'var(--faint)',
                    transition: 'background 300ms',
                  }}
                />
              )}
              {i < stages.length - 1 && (
                <div
                  aria-hidden
                  style={{
                    position: 'absolute',
                    top: 7,
                    left: '50%',
                    width: '50%',
                    height: 2,
                    background: rightLit ? 'var(--accent)' : 'var(--faint)',
                    transition: 'background 300ms',
                  }}
                />
              )}
              <div
                aria-hidden
                style={
                  {
                    '--step-color': color,
                    '--step-halo': halo,
                    position: 'relative',
                    width: 16,
                    height: 16,
                    margin: '0 auto',
                    borderRadius: '50%',
                    background: lit ? color : 'var(--surface)',
                    border: `2px solid ${lit ? color : 'var(--faint)'}`,
                    boxSizing: 'border-box',
                    boxShadow: lit ? `0 0 0 4px ${halo}, 0 0 10px ${color}` : 'none',
                    animation: status === 'active' ? 'pilot-step-glow 1.6s ease-in-out infinite' : 'none',
                    transition: 'background 300ms, border-color 300ms, box-shadow 300ms',
                  } as CSSProperties
                }
              />
              <div
                style={{
                  marginTop: 10,
                  fontSize: 12.5,
                  fontWeight: status === 'active' ? 600 : 500,
                  color: status === 'pending' ? 'var(--muted)' : 'var(--ink)',
                  lineHeight: 1.3,
                  padding: '0 4px',
                }}
                title={STAGE_HINT[stage]}
              >
                {STAGE_LABEL[stage]}
              </div>
              {detail && (
                <div
                  data-testid="pilot-check-stage-detail"
                  style={{ marginTop: 3, fontSize: 11.5, color: 'var(--muted)' }}
                >
                  {detail}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function StatePill({ state }: { state: PilotCheckState }) {
  const tone = STATE_TONE[state];
  const color =
    tone === 'ok'
      ? 'var(--accent)'
      : tone === 'danger'
        ? 'var(--danger)'
        : tone === 'warn'
          ? 'var(--warn)'
          : 'var(--muted)';
  return (
    <span
      data-testid="pilot-check-state"
      style={{
        font: '600 10px/1 var(--font-mono)',
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        padding: '4px 8px',
        borderRadius: 999,
        border: `1px solid ${color}`,
        color,
      }}
    >
      {STATE_LABEL[state]}
    </span>
  );
}
