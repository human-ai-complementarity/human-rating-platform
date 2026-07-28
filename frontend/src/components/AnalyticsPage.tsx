import { useEffect, useState } from 'react';
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

  useEffect(() => {
    setLoading(true);
    setExperiment(null);
    setError(null);

    // Guard against out-of-order resolution when :experimentId changes while
    // the page stays mounted (back/forward between two experiments' analytics):
    // a stale response must not overwrite the fresh experiment.
    let cancelled = false;

    (async () => {
      try {
        // Include archived experiments so analytics deeplinks keep resolving
        // after an experiment is archived.
        const experiments = await api.listExperiments({ includeArchived: true });
        if (cancelled) return;
        const exp = experiments.find(e => e.id === parseInt(experimentId || '0'));
        if (exp) {
          setExperiment(exp);
        } else {
          setError('Experiment not found');
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [experimentId]);

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
