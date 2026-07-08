import { useCallback, useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../api';
import ExperimentDetail from './ExperimentDetail';
import type { Experiment } from '../types';

function ExperimentDetailPage() {
  const { experimentId } = useParams<{ experimentId: string }>();
  const navigate = useNavigate();
  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [allExperiments, setAllExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Deliberately does NOT flip `loading` — that would unmount ExperimentDetail
  // and blow away its local UI state (the active tab, unsaved metaForm edits,
  // etc.) on every post-mutation refresh. The initial-mount effect below owns
  // the loading spinner. See 65d14ac.
  const loadExperiment = useCallback(async () => {
    try {
      const experiments = await api.listExperiments();
      setAllExperiments(experiments);
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

  const handleBack = () => {
    navigate('/admin');
  };

  const handleDeleted = () => {
    navigate('/admin');
  };

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
    <ExperimentDetail
      experiment={experiment}
      allExperiments={allExperiments}
      onBack={handleBack}
      onDeleted={handleDeleted}
      onRefresh={loadExperiment}
    />
  );
}

export default ExperimentDetailPage;
