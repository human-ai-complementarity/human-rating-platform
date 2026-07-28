import { useCallback, useEffect, useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api';
import Analytics, { ANALYTICS_TABS, type AnalyticsTab } from './Analytics';
import type { Experiment } from '../types';

function AnalyticsPage() {
  const { experimentId, tab } = useParams<{ experimentId: string; tab: string }>();
  const navigate = useNavigate();
  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadExperiment = useCallback(async () => {
    try {
      // Include archived experiments so analytics deeplinks keep resolving
      // after an experiment is archived (mirrors ExperimentDetailPage).
      const experiments = await api.listExperiments({ includeArchived: true });
      const exp = experiments.find(e => e.id === parseInt(experimentId || '0'));
      if (exp) {
        setExperiment(exp);
      } else {
        setError('Experiment not found');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [experimentId]);

  useEffect(() => {
    setLoading(true);
    setExperiment(null);
    setError(null);
    loadExperiment();
  }, [loadExperiment]);

  // No `:tab` segment means overview; an unknown segment redirects to the
  // canonical overview URL instead of rendering a broken page.
  if (tab !== undefined && !ANALYTICS_TABS.includes(tab as AnalyticsTab)) {
    return <Navigate to={`/admin/experiments/${experimentId}/analytics`} replace />;
  }
  const activeTab: AnalyticsTab = (tab as AnalyticsTab) ?? 'overview';

  if (loading) {
    return (
      <div className="admin-page">
        <div
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius)',
            padding: 40,
            textAlign: 'center',
            color: 'var(--muted)',
            boxShadow: 'var(--shadow)',
          }}
        >
          Loading…
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="admin-page">
        <div
          style={{
            background: 'var(--danger-soft)',
            border: '1px solid var(--danger)',
            borderRadius: 'var(--radius)',
            padding: 40,
            textAlign: 'center',
            color: 'var(--danger)',
            boxShadow: 'var(--shadow)',
          }}
        >
          {error}
        </div>
      </div>
    );
  }

  if (!experiment) {
    return null;
  }

  return (
    <Analytics
      experimentId={experiment.id}
      experimentName={experiment.internal_name || experiment.name}
      activeTab={activeTab}
      onTabChange={(next) => navigate(`/admin/experiments/${experiment.id}/analytics/${next}`)}
      onBack={() => navigate(`/admin/experiments/${experiment.id}`)}
    />
  );
}

export default AnalyticsPage;
