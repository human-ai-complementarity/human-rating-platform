import { useEffect, useState, type CSSProperties } from 'react';
import { api } from '../../api';
import { buildRaterPreviewUrl } from '../../raterPreview';
import type {
  Experiment,
  PilotCheckFinding,
  PilotCheckProbe,
  PilotCheckRun,
  PilotCheckSeverity,
  PilotCheckState,
  PilotCheckSummary,
} from '../../types';
import { Banner, SectionCard, primaryButton, toneColor, type Tone } from './ui';

/**
 * The pilot check (docs/pilot-check.md) on the Launch tab: the latest run's
 * state, a button to run it, a stepper that lights each stage as the run
 * passes it, and the findings grouped by check with a link that opens each
 * flagged question in the participant preview.
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

const STATE_TONE: Record<PilotCheckState, Tone> = {
  not_run: 'warn',
  running: 'info',
  stale: 'warn',
  passed: 'ok',
  warnings: 'warn',
  failed: 'danger',
  aborted: 'danger',
};

const SEVERITY_ORDER: Record<PilotCheckSeverity, number> = { error: 0, warning: 1, info: 2 };
const SEVERITY_TONE: Record<PilotCheckSeverity, Tone> = {
  error: 'danger',
  warning: 'warn',
  info: 'info',
};

export function describeSummary(summary: PilotCheckSummary | null | undefined): string {
  if (!summary) return 'Pilot check state unknown.';
  if (summary.state === 'passed') return 'Pilot check passed.';
  return summary.reason ?? STATE_LABEL[summary.state];
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
  const [loaded, setLoaded] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped when this card starts a run, so the poll below picks it up.
  const [started, setStarted] = useState(0);

  const state: PilotCheckState = summary?.state ?? 'not_run';
  // An upload or an edit can start a run on its own; the refreshed summary
  // is how this card hears of it, and it then polls that run to its end.
  const summaryRunning = state === 'running';

  // Load the latest run; while it is in flight keep polling, and tell the
  // parent once it lands so the summary follows. Keyed on `started` and on
  // the summary turning to running, not the summary itself, so a run fetches
  // once rather than once per refresh.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async (wasRunning: boolean) => {
      try {
        const latest = await api.getPilotCheck(experiment.id);
        if (cancelled) return;
        setRun(latest);
        setLoaded(true);
        if (latest?.status === 'running') {
          timer = window.setTimeout(() => void poll(true), POLL_MS);
        } else if (wasRunning) {
          onRunStateChange();
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load the pilot check');
        setLoaded(true);
        // A blip mid-run must not strand the card at "Running…": keep polling.
        if (wasRunning) timer = window.setTimeout(() => void poll(true), POLL_MS);
      }
    };
    void poll(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [experiment.id, started, summaryRunning, onRunStateChange]);

  const handleRun = async () => {
    setError(null);
    setStarting(true);
    try {
      setRun(await api.runPilotCheck(experiment.id));
      setStarted((n) => n + 1);
      onRunStateChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start the pilot check');
    } finally {
      setStarting(false);
    }
  };

  const polling = state === 'running' || run?.status === 'running';
  const canRun = !polling && !starting && experiment.status !== 'FINISHED';
  const probes = new Map((run?.assistance_steps ?? []).map((probe) => [probe.question_id, probe]));

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
          <Banner tone={STATE_TONE[state]}>
            <div data-testid="pilot-check-banner">
              <strong>{describeSummary(summary)}</strong>
              {run?.finished_at && !polling && (
                <span style={{ color: 'var(--muted)' }}>
                  {' '}
                  Last run {new Date(run.finished_at).toLocaleString()}
                  {run.requested_by && ` by ${run.requested_by}`}.
                </span>
              )}
              {summary?.needs_acknowledgement && (
                <div style={{ marginTop: 4, fontSize: 12.5 }}>
                  Launching will ask you to acknowledge this.
                </div>
              )}
            </div>
          </Banner>
        </div>

        {run && <Stepper run={run} />}

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
          {starting ? 'Starting…' : polling ? 'Running…' : run ? 'Re-run check' : 'Run check'}
        </button>

        {loaded && run && run.status !== 'running' && (
          <div style={{ marginTop: 18 }}>
            {run.findings.length === 0 ? (
              <div style={{ fontSize: 13, color: 'var(--muted)' }}>No findings.</div>
            ) : (
              <div data-testid="pilot-check-findings" style={{ display: 'grid', gap: 14 }}>
                {groupFindings(run.findings).map(({ label, findings }) => (
                  <div key={label}>
                    <div
                      style={{
                        font: '600 11px/1 var(--font-mono)',
                        letterSpacing: '0.12em',
                        textTransform: 'uppercase',
                        color: 'var(--muted)',
                        marginBottom: 6,
                      }}
                    >
                      {label}
                    </div>
                    <div style={{ display: 'grid', gap: 6 }}>
                      {findings.map((finding, index) => (
                        <FindingRow
                          key={index}
                          finding={finding}
                          experimentId={experiment.id}
                          probes={probes}
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

// Grouped by check in the backend's report order, worst finding first within
// a group and worst group first, so an error is never below a note.
function groupFindings(findings: PilotCheckFinding[]) {
  const groups = new Map<string, PilotCheckFinding[]>();
  for (const finding of findings) {
    const label = finding.check_label || finding.check;
    groups.set(label, [...(groups.get(label) ?? []), finding]);
  }
  const worst = (list: PilotCheckFinding[]) => Math.min(...list.map((f) => SEVERITY_ORDER[f.severity]));
  return [...groups]
    .map(([label, list]) => ({
      label,
      findings: list.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]),
    }))
    .sort((a, b) => worst(a.findings) - worst(b.findings));
}

// One row per problem. A question-level finding names every question it
// applies to: a preview link for each, and what the AI returned on each.
function FindingRow({
  finding,
  experimentId,
  probes,
}: {
  finding: PilotCheckFinding;
  experimentId: number;
  probes: Map<number, PilotCheckProbe>;
}) {
  const ids = finding.question_ids;
  const withProbes = ids.filter((id) => probes.has(id));
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
          color: toneColor(SEVERITY_TONE[finding.severity]),
          padding: '4px 0',
          minWidth: 58,
        }}
      >
        {finding.severity}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ color: 'var(--ink)' }}>{finding.message}</div>
        {finding.detail && <pre style={preStyle}>{finding.detail}</pre>}
        {ids.length > 0 && (
          <div
            data-testid="pilot-check-finding-questions"
            style={{ marginTop: 6, fontSize: 12.5, display: 'flex', flexWrap: 'wrap', gap: '2px 8px' }}
          >
            {ids.length === 1 ? null : (
              <span style={{ color: 'var(--muted)' }}>
                {ids.length} questions, open in preview:
              </span>
            )}
            {ids.map((id) => (
              <a
                key={id}
                href={buildRaterPreviewUrl(experimentId, id)}
                target="_blank"
                rel="noreferrer"
                aria-label={`Open question ${id} in preview`}
                style={{ color: 'var(--accent)' }}
              >
                {ids.length === 1 ? `Open question ${id} in preview` : id}
              </a>
            ))}
          </div>
        )}
        {withProbes.length > 0 && (
          <details style={{ marginTop: 6, fontSize: 12.5 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--accent)' }}>What the AI returned</summary>
            {withProbes.map((id) => (
              <ProbeDump key={id} probe={probes.get(id)!} label={withProbes.length > 1 ? `question ${id}` : null} />
            ))}
          </details>
        )}
      </div>
    </div>
  );
}

function ProbeDump({ probe, label }: { probe: PilotCheckProbe; label: string | null }) {
  return (
    <pre data-testid="pilot-check-probe" style={{ ...preStyle, maxHeight: 280, overflow: 'auto' }}>
      {(label ? `${label} · ` : '') +
        `step: ${probe.step_type} · ${probe.latency_ms} ms` +
        (probe.failure_reason ? ` · ${probe.failure_reason}` : '') +
        (probe.failure_detail ? `\n${probe.failure_detail}` : '') +
        `\n${JSON.stringify(probe.payload, null, 2)}`}
    </pre>
  );
}

const preStyle: CSSProperties = {
  margin: '6px 0 0',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  font: '400 12px/1.5 var(--font-mono)',
  color: 'var(--muted)',
};

// One dot per stage on a line. A dot lights with a glow once its stage has
// started, pulses while it is the current one (animation in index.css, with
// a reduced-motion fallback), and turns red if the run died there.
function Stepper({ run }: { run: PilotCheckRun }) {
  const at = Math.max(
    0,
    run.stages.findIndex((stage) => stage.id === run.stage),
  );
  const statusOf = (i: number) => {
    if (run.status !== 'running' && run.status !== 'aborted') return 'done';
    if (i < at) return 'done';
    if (i > at) return 'pending';
    return run.status === 'running' ? 'active' : 'failed';
  };
  return (
    <div
      data-testid="pilot-check-stepper"
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${run.stages.length}, 1fr)`,
        margin: '4px 0 16px',
        padding: '12px 4px 4px',
      }}
    >
      {run.stages.map((stage, i) => {
        const status = statusOf(i);
        const lit = status !== 'pending';
        const color = status === 'failed' ? 'var(--danger)' : lit ? 'var(--accent)' : 'var(--faint)';
        return (
          <div
            key={stage.id}
            data-testid={`pilot-check-step-${stage.id}`}
            data-status={status}
            style={{ position: 'relative', textAlign: 'center', minWidth: 0 }}
          >
            {i > 0 && (
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  top: 7,
                  right: '50%',
                  width: '100%',
                  height: 2,
                  background: lit ? 'var(--accent)' : 'var(--faint)',
                  transition: 'background 300ms',
                }}
              />
            )}
            <div
              aria-hidden
              className={status === 'active' ? 'pilot-step-active' : undefined}
              style={
                {
                  '--step-color': color,
                  position: 'relative',
                  zIndex: 1, // above the next stage's connector, which reaches into this cell
                  width: 16,
                  height: 16,
                  margin: '0 auto',
                  borderRadius: '50%',
                  boxSizing: 'border-box',
                  background: lit ? color : 'var(--surface)',
                  border: `2px solid ${color}`,
                  boxShadow: lit
                    ? `0 0 0 4px color-mix(in srgb, ${color} 22%, transparent), 0 0 10px ${color}`
                    : 'none',
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
            >
              {stage.label}
            </div>
            {status === 'active' && run.stage_detail && (
              <div
                data-testid="pilot-check-stage-detail"
                style={{ marginTop: 3, fontSize: 11.5, color: 'var(--muted)' }}
              >
                {run.stage_detail}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function StatePill({ state }: { state: PilotCheckState }) {
  const color = toneColor(STATE_TONE[state]);
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
