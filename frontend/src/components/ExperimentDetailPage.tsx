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
      <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '24px' }}>
        <div style={{ background: '#fff', borderRadius: '8px', border: '1px solid #e0e0e0', padding: '40px', textAlign: 'center', color: '#666' }}>
          Loading...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '24px' }}>
        <div style={{ background: '#fff', borderRadius: '8px', border: '1px solid #f5c6cb', padding: '40px', textAlign: 'center', color: '#dc3545' }}>
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
