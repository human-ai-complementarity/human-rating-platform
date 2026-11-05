import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { buildRaterPreviewUrl } from '../raterPreview';
import type { Analytics as AnalyticsType, ExperimentStatus } from '../types';
import StatusLabel from './StatusLabel';
import {
  BackLink,
  Banner,
  StatCell,
  ToggleSwitch,
  secondaryButton,
} from './experiment-detail/ui';

// The summary numbers sit above the tabs on every view, so the tabs only
// switch the breakdown below them. `/analytics` with no tab shows questions.
export const ANALYTICS_TABS = ['questions', 'raters'] as const;
export type AnalyticsTab = (typeof ANALYTICS_TABS)[number];

const TAB_LABELS: Record<AnalyticsTab, string> = { questions: 'Questions', raters: 'Raters' };

interface AnalyticsProps {
  experimentId: number;
  experimentName: string;
  experimentStatus: ExperimentStatus;
  activeTab: AnalyticsTab;
  onTabChange: (tab: AnalyticsTab) => void;
  onBack: () => void;
}

const formatTime = (seconds: number | undefined | null): string => {
  if (seconds === undefined || seconds === null || isNaN(seconds)) return '-';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = (seconds % 60).toFixed(0);
  return `${mins}m ${secs}s`;
};

const formatNumber = (num: number | undefined | null, decimals = 1): string => {
  if (num === undefined || num === null || isNaN(num)) return '-';
  return num.toFixed(decimals);
};

const formatDate = (iso: string | null): string => (iso ? new Date(iso).toLocaleString() : '-');

function TimedOutPill() {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        minHeight: 20,
        padding: '0 8px',
        borderRadius: 999,
        background: 'var(--warn-soft)',
        color: 'var(--warn)',
        font: '600 10.5px var(--font-mono)',
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      Timed out
    </span>
  );
}

function Analytics({
  experimentId,
  experimentName,
  experimentStatus,
  activeTab,
  onTabChange,
  onBack,
}: AnalyticsProps) {
  const [analytics, setAnalytics] = useState<AnalyticsType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includePreview, setIncludePreview] = useState(false);
  // Phones get cards instead of the wide tables; only one is rendered.
  const narrow = useMediaQuery('(max-width: 640px)');

  const loadAnalytics = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await api.getExperimentAnalytics(experimentId, { includePreview });
      setAnalytics(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [experimentId, includePreview]);

  useEffect(() => {
    loadAnalytics();
  }, [loadAnalytics]);

  const backLink = <BackLink label="Experiment" onClick={onBack} />;

  if (loading && !analytics) {
    return (
      <div className="admin-page">
        {backLink}
        <div className="an-empty">Loading analytics…</div>
      </div>
    );
  }

  // A failed first load has nothing to show; a failed refresh keeps the data
  // already on screen and shows the error above it (see below).
  if (error && !analytics) {
    return (
      <div className="admin-page">
        {backLink}
        <Banner tone="danger">{error}</Banner>
        <button type="button" onClick={loadAnalytics} style={{ ...secondaryButton, marginTop: 14 }}>
          Try again
        </button>
      </div>
    );
  }

  if (!analytics) {
    return null;
  }

  const { overview } = analytics;

  return (
    <div className="admin-page">
      {backLink}
      <div className="an-head">
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="an-eyebrow">Analytics</span>
            <StatusLabel status={experimentStatus} size="sm" />
          </div>
          <h1 className="xd-h1 an-title">
            <span className="visually-hidden">Analytics: </span>
            {experimentName}
          </h1>
        </div>
        <div className="an-actions">
          <span id="include-preview-analytics" style={{ fontSize: 13.5, color: 'var(--muted)' }}>
            Include preview data
          </span>
          <ToggleSwitch
            checked={includePreview}
            onChange={() => setIncludePreview((v) => !v)}
            ariaLabelledBy="include-preview-analytics"
          />
          <button
            type="button"
            onClick={loadAnalytics}
            disabled={loading}
            style={{ ...secondaryButton, display: 'inline-flex', alignItems: 'center', gap: 8 }}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />
            </svg>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: 16 }}>
          <Banner tone="danger">Couldn't refresh: {error}</Banner>
        </div>
      )}

      <div className="stat-strip an-stats">
        <StatCell label="Ratings" value={overview.total_ratings} />
        <StatCell label="Questions" value={overview.total_questions} />
        <StatCell label="Raters" value={overview.total_raters} />
        <StatCell
          label="Ran out of time"
          value={overview.timed_out_raters}
          tone={overview.timed_out_raters > 0 ? 'warn' : undefined}
          testId="timed-out-raters"
        />
        <StatCell
          label="Avg confidence"
          value={
            <>
              {formatNumber(overview.avg_confidence)}
              <span style={{ fontSize: 14, fontWeight: 500, color: 'var(--muted)' }}> / 5</span>
            </>
          }
        />
        <StatCell label="Avg response time" value={formatTime(overview.avg_response_time_seconds || 0)} />
      </div>
      {/* Fastest/slowest only once there is something to compare. */}
      {overview.total_ratings > 0 &&
        (overview.min_response_time_seconds || overview.max_response_time_seconds) ? (
        <p style={{ margin: '10px 0 0', fontSize: 13, color: 'var(--muted)' }}>
          Fastest answer {formatTime(overview.min_response_time_seconds)} · slowest{' '}
          {formatTime(overview.max_response_time_seconds)}
        </p>
      ) : null}

      <div className="an-tabs" role="tablist" aria-label="Breakdown">
        {ANALYTICS_TABS.map((tab) => {
          const count = tab === 'questions' ? analytics.questions.length : analytics.raters.length;
          return (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={activeTab === tab}
              onClick={() => onTabChange(tab)}
            >
              {TAB_LABELS[tab]}
              <span className="an-count" aria-hidden="true">
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {activeTab === 'questions' && (
        <section className="an-card" aria-label="Per-question analytics">
          {analytics.questions.length === 0 ? (
            <div className="an-empty">No ratings yet.</div>
          ) : !narrow ? (
            <div className="an-tablebox">
              <table className="an-table">
                <thead>
                  <tr>
                    <th scope="col">Question</th>
                    <th scope="col" className="an-num">Ratings</th>
                    <th scope="col" className="an-num">Avg time</th>
                    <th scope="col" className="an-num">Avg conf.</th>
                    <th scope="col">Answers</th>
                  </tr>
                </thead>
                <tbody>
                  {analytics.questions.map((q) => (
                    <tr key={q.question_id}>
                      <td style={{ maxWidth: 440 }}>
                        <a
                          href={buildRaterPreviewUrl(experimentId, q.question_db_id)}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="Open this question in the rater preview"
                          data-testid={`question-preview-link-${q.question_db_id}`}
                          className="an-qid"
                        >
                          {q.question_id}
                        </a>
                        <div className="an-ellipsis" title={q.question_text}>
                          {q.question_text}
                        </div>
                      </td>
                      <td className="an-num">{q.num_ratings}</td>
                      <td className="an-num">{formatTime(q.avg_response_time_seconds)}</td>
                      <td className="an-num">{q.avg_confidence.toFixed(1)}</td>
                      <td>
                        <AnswerChips distribution={q.answer_distribution} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <ul className="an-cards">
              {analytics.questions.map((q) => (
                <li key={q.question_id}>
                  <a
                    href={buildRaterPreviewUrl(experimentId, q.question_db_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open this question in the rater preview"
                    data-testid={`question-preview-link-${q.question_db_id}`}
                    className="an-qid"
                  >
                    {q.question_id}
                  </a>
                  <div style={{ marginTop: 2, fontSize: 13.5 }}>{q.question_text}</div>
                  <div style={{ marginTop: 8 }}>
                    <AnswerChips distribution={q.answer_distribution} />
                  </div>
                  <div className="an-card-meta">
                    {q.num_ratings} ratings · {formatTime(q.avg_response_time_seconds)} avg · conf.{' '}
                    {q.avg_confidence.toFixed(1)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {activeTab === 'raters' && (
        // Shows Prolific IDs: PostHog must not capture clicks here.
        <section className="an-card ph-no-capture" aria-label="Per-rater analytics">
          {analytics.raters.length === 0 ? (
            <div className="an-empty">No raters yet.</div>
          ) : !narrow ? (
            <div className="an-tablebox">
              <table className="an-table">
                <thead>
                  <tr>
                    <th scope="col">Prolific ID</th>
                    <th scope="col">Study ID</th>
                    <th scope="col" className="an-num">Ratings</th>
                    <th scope="col" className="an-num">Total time</th>
                    <th scope="col" className="an-num">Avg time</th>
                    <th scope="col" className="an-num">Avg conf.</th>
                    <th scope="col">Started</th>
                    <th scope="col">Consent</th>
                  </tr>
                </thead>
                <tbody>
                  {analytics.raters.map((r) => (
                    <tr key={r.prolific_id}>
                      <td>
                        <span className="an-mono">{r.prolific_id}</span>
                        {r.timed_out && (
                          <span style={{ marginLeft: 8 }}>
                            <TimedOutPill />
                          </span>
                        )}
                      </td>
                      <td className="an-mono" style={{ color: 'var(--muted)' }}>
                        {r.study_id || '-'}
                      </td>
                      <td className="an-num">{r.num_ratings}</td>
                      <td className="an-num">{formatTime(r.total_response_time_seconds)}</td>
                      <td className="an-num">{formatTime(r.avg_response_time_seconds)}</td>
                      <td className="an-num">{r.avg_confidence.toFixed(1)}</td>
                      <td style={{ whiteSpace: 'nowrap', color: 'var(--muted)', fontSize: 12.5 }}>
                        {formatDate(r.session_start)}
                      </td>
                      <td
                        style={{ color: 'var(--muted)', fontSize: 12.5 }}
                        title={r.consented_at ? formatDate(r.consented_at) : undefined}
                      >
                        {r.consent_version ?? '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <ul className="an-cards">
              {analytics.raters.map((r) => (
                <li key={r.prolific_id}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
                    <span className="an-mono an-ellipsis">{r.prolific_id}</span>
                    {r.timed_out && <TimedOutPill />}
                  </div>
                  <dl className="an-kv">
                    <div>
                      <dt>Ratings</dt>
                      <dd>{r.num_ratings}</dd>
                    </div>
                    <div>
                      <dt>Avg time</dt>
                      <dd>{formatTime(r.avg_response_time_seconds)}</dd>
                    </div>
                    <div>
                      <dt>Avg conf.</dt>
                      <dd>{r.avg_confidence.toFixed(1)}</dd>
                    </div>
                  </dl>
                  <div className="an-card-meta">
                    Started {formatDate(r.session_start)} · {formatTime(r.total_response_time_seconds)}{' '}
                    total
                  </div>
                  <div className="an-card-meta">
                    Study <span className="an-mono">{r.study_id || '-'}</span> · consent{' '}
                    <span title={r.consented_at ? formatDate(r.consented_at) : undefined}>
                      {r.consent_version ?? '-'}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

function AnswerChips({ distribution }: { distribution: Record<string, number> }) {
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {Object.entries(distribution).map(([answer, count]) => (
        <span key={answer} className="an-chip" title={answer}>
          {answer.length > 15 ? answer.substring(0, 15) + '…' : answer}: {count}
        </span>
      ))}
    </div>
  );
}

export default Analytics;
