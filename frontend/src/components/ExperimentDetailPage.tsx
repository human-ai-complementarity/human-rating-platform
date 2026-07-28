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
    // A non-numeric route segment (typo, stale bookmark) can't be a valid id.
    // Bail early so the backend's int-path validation (a 422 with array-shaped
    // detail) never leaks raw JSON into the error box — a numeric-but-missing
    // id still reaches the server and gets a clean "Experiment not found" 404.
    const id = Number(experimentId);
    if (!experimentId || !Number.isInteger(id)) {
      setError('Experiment not found');
      setLoading(false);
      return;
    }
    try {
      // Resolve the current experiment by id directly (works for archived ones,
      // which the list hides). The list is still fetched — with archived
      // included — to populate the exclusion-target picker in ExperimentDetail.
      const [exp, experiments] = await Promise.all([
        api.getExperiment(id),
        api.listExperiments({ includeArchived: true }),
      ]);
      setExperiment(exp);
      setAllExperiments(experiments);
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
