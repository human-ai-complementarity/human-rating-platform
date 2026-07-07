import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Analytics as AnalyticsType } from '../types';
import { Banner } from './experiment-detail/ui';

interface AnalyticsProps {
  experimentId: number;
  experimentName: string;
  onBack: () => void;
}

function Analytics({ experimentId, experimentName, onBack }: AnalyticsProps) {
  const [analytics, setAnalytics] = useState<AnalyticsType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'overview' | 'questions' | 'raters'>('overview');
  const [includePreview, setIncludePreview] = useState(false);

  const loadAnalytics = useCallback(async () => {
    try {
      setLoading(true);
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
    tabs: {
      display: 'flex',
      gap: '4px',
      background: '#f0f0f0',
      padding: '4px',
      borderRadius: '8px',
      width: 'fit-content',
    },
    tab: {
      padding: '10px 20px',
      border: 'none',
      background: 'transparent',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
      fontWeight: 500,
      color: '#666',
      transition: 'all 0.15s',
    },
    tabActive: {
      background: '#fff',
      color: '#333',
      boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
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
    },
    sectionTitle: {
      margin: 0,
      fontSize: '14px',
      fontWeight: 600,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      color: '#555',
    },
    sectionBody: {
      padding: '20px',
    },
    statsGrid: {
      display: 'grid',
      gridTemplateColumns: 'repeat(4, 1fr)',
      gap: '16px',
    },
    statItem: {
      textAlign: 'center' as const,
      padding: '20px 16px',
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
    table: {
      width: '100%',
      borderCollapse: 'collapse' as const,
      fontSize: '14px',
    },
    th: {
      padding: '12px 16px',
      textAlign: 'left' as const,
      background: '#fafafa',
      borderBottom: '2px solid #e0e0e0',
      fontWeight: 600,
      fontSize: '12px',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      color: '#555',
    },
    thCenter: {
      textAlign: 'center' as const,
    },
    td: {
      padding: '12px 16px',
      borderBottom: '1px solid #eee',
    },
    tdCenter: {
      textAlign: 'center' as const,
    },
    tdMono: {
      fontFamily: 'monospace',
      fontSize: '13px',
    },
    badge: {
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: '4px',
      fontSize: '12px',
      background: '#e3f2fd',
    },
    refreshButton: {
      padding: '10px 20px',
      background: '#fff',
      color: '#333',
      border: '1px solid #ddd',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
    },
    emptyState: {
      padding: '40px 20px',
      textAlign: 'center' as const,
      color: '#888',
    },
  };

  if (loading) {
    return (
      <div style={styles.container}>
        <div style={styles.emptyState}>Loading analytics...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={styles.container}>
        <div style={{ marginBottom: 16 }}>
          <Banner tone="danger">{error}</Banner>
        </div>
        <button onClick={onBack} style={styles.backButton}>Back</button>
      </div>
    );
  }

  if (!analytics) {
    return null;
  }

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <button onClick={onBack} style={styles.backButton}>← Back</button>
        <h1 style={styles.title}>Analytics: {experimentName}</h1>
      </div>

      {/* Tabs + Filters */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div style={styles.tabs}>
          {(['overview', 'questions', 'raters'] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              style={{
                ...styles.tab,
                ...(activeTab === tab ? styles.tabActive : {}),
              }}
            >
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span style={{ fontSize: '13px', color: '#666' }}>Include preview data</span>
          <div style={{ position: 'relative' as const, width: '44px', height: '24px', flexShrink: 0 }}>
            <div
              style={{
                width: '44px',
                height: '24px',
                borderRadius: '12px',
                cursor: 'pointer',
                transition: 'background 0.2s',
                background: includePreview ? '#4a90d9' : '#ddd',
              }}
              onClick={() => setIncludePreview(!includePreview)}
            />
            <div
              style={{
                position: 'absolute' as const,
                top: '2px',
                width: '20px',
                height: '20px',
                borderRadius: '50%',
                background: '#fff',
                boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                transition: 'left 0.2s',
                left: includePreview ? '22px' : '2px',
                pointerEvents: 'none',
              }}
            />
          </div>
        </div>
      </div>

      {/* Overview Tab */}
      {activeTab === 'overview' && (
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h2 style={styles.sectionTitle}>Overview</h2>
          </div>
          <div style={styles.sectionBody}>
            <div style={styles.statsGrid}>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{analytics.overview.total_ratings}</div>
                <div style={styles.statLabel}>Total Ratings</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{analytics.overview.total_questions}</div>
                <div style={styles.statLabel}>Total Questions</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{analytics.overview.total_raters}</div>
                <div style={styles.statLabel}>Total Raters</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{formatNumber(analytics.overview.avg_confidence)}</div>
                <div style={styles.statLabel}>Avg Confidence</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{formatTime(analytics.overview.avg_response_time_seconds || 0)}</div>
                <div style={styles.statLabel}>Avg Response Time</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{formatTime(analytics.overview.min_response_time_seconds || 0)}</div>
                <div style={styles.statLabel}>Min Response Time</div>
              </div>
              <div style={styles.statItem}>
                <div style={styles.statValue}>{formatTime(analytics.overview.max_response_time_seconds || 0)}</div>
                <div style={styles.statLabel}>Max Response Time</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Questions Tab */}
      {activeTab === 'questions' && (
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h2 style={styles.sectionTitle}>Per-Question Analytics</h2>
          </div>
          {analytics.questions.length === 0 ? (
            <div style={styles.emptyState}>No ratings yet.</div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={styles.table}>
                <thead>
                  <tr>
                    <th style={styles.th}>Question ID</th>
                    <th style={styles.th}>Preview</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Ratings</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Avg Time</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Avg Confidence</th>
                    <th style={styles.th}>Answer Distribution</th>
                  </tr>
                </thead>
                <tbody>
                  {analytics.questions.map((q) => (
                    <tr key={q.question_id}>
                      <td style={{ ...styles.td, ...styles.tdMono }}>{q.question_id}</td>
                      <td style={{ ...styles.td, maxWidth: '250px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {q.question_text}
                      </td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{q.num_ratings}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{formatTime(q.avg_response_time_seconds)}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{q.avg_confidence.toFixed(1)}</td>
                      <td style={styles.td}>
                        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                          {Object.entries(q.answer_distribution).map(([answer, count]) => (
                            <span key={answer} style={styles.badge}>
                              {answer.length > 15 ? answer.substring(0, 15) + '...' : answer}: {count}
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Raters Tab */}
      {activeTab === 'raters' && (
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h2 style={styles.sectionTitle}>Per-Rater Analytics</h2>
          </div>
          {analytics.raters.length === 0 ? (
            <div style={styles.emptyState}>No raters yet.</div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={styles.table}>
                <thead>
                  <tr>
                    <th style={styles.th}>Prolific ID</th>
                    <th style={styles.th}>Study ID</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Questions</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Total Time</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Avg Time</th>
                    <th style={{ ...styles.th, ...styles.thCenter }}>Avg Confidence</th>
                    <th style={styles.th}>Session Start</th>
                  </tr>
                </thead>
                <tbody>
                  {analytics.raters.map((r) => (
                    <tr key={r.prolific_id}>
                      <td style={{ ...styles.td, ...styles.tdMono }}>{r.prolific_id}</td>
                      <td style={{ ...styles.td, ...styles.tdMono, color: '#888' }}>{r.study_id || '-'}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{r.num_ratings}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{formatTime(r.total_response_time_seconds)}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{formatTime(r.avg_response_time_seconds)}</td>
                      <td style={{ ...styles.td, ...styles.tdCenter }}>{r.avg_confidence.toFixed(1)}</td>
                      <td style={{ ...styles.td, fontSize: '12px', color: '#666' }}>
                        {r.session_start ? new Date(r.session_start + 'Z').toLocaleString() : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Refresh */}
      <div style={{ textAlign: 'center', marginTop: '24px' }}>
        <button onClick={loadAnalytics} style={styles.refreshButton}>
          Refresh Analytics
        </button>
      </div>
    </div>
  );
}

export default Analytics;
