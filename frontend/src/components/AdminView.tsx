import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import type { Experiment, ExperimentCreate } from '../types';

function AdminView() {
  const navigate = useNavigate();
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [newExperiment, setNewExperiment] = useState<ExperimentCreate>({
    name: '',
    internal_name: '',
    num_ratings_per_question: 3,
    prolific_completion_url: '',
  });

  useEffect(() => {
    loadExperiments();
  }, []);

  const loadExperiments = async () => {
    try {
      setLoading(true);
      const data = await api.listExperiments();
      setExperiments(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  const handleCreateExperiment = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSuccess(null);

    try {
      const internal = (newExperiment.internal_name ?? '').trim();
      const created = await api.createExperiment({
        ...newExperiment,
        internal_name: internal === '' ? null : internal,
      });
      setNewExperiment({
        name: '',
        internal_name: '',
        num_ratings_per_question: 3,
        prolific_completion_url: '',
      });
      navigate(`/admin/experiments/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    }
  };

  const handleSelectExperiment = (exp: Experiment) => {
    navigate(`/admin/experiments/${exp.id}`);
  };

  const styles = {
    container: {
      maxWidth: '1200px',
      margin: '0 auto',
      padding: '24px',
    },
    header: {
      marginBottom: '32px',
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-end',
      gap: '16px',
    },
    title: {
      margin: 0,
      fontSize: '28px',
      fontWeight: 600,
      color: '#333',
    },
    subtitle: {
      margin: '8px 0 0 0',
      fontSize: '14px',
      color: '#666',
    },
    docsLink: {
      color: '#4a90d9',
      fontSize: '14px',
      textDecoration: 'none',
      background: 'none',
      border: 'none',
      cursor: 'pointer',
      padding: 0,
      whiteSpace: 'nowrap' as const,
    },
    grid: {
      display: 'grid',
      gridTemplateColumns: '1fr 1fr',
      gap: '24px',
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
      boxSizing: 'border-box' as const,
    },
    hint: {
      fontSize: '12px',
      color: '#888',
      marginTop: '6px',
    },
    primaryButton: {
      width: '100%',
      padding: '12px 16px',
      background: '#4a90d9',
      color: '#fff',
      border: 'none',
      borderRadius: '6px',
      cursor: 'pointer',
      fontSize: '14px',
      fontWeight: 500,
    },
    experimentList: {
      listStyle: 'none',
      margin: 0,
      padding: 0,
    },
    experimentItem: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      padding: '16px 20px',
      borderBottom: '1px solid #eee',
      cursor: 'pointer',
      transition: 'background 0.15s',
    },
    experimentName: {
      fontWeight: 500,
      color: '#333',
      marginBottom: '4px',
    },
    experimentMeta: {
      fontSize: '12px',
      color: '#888',
    },
    viewLink: {
      color: '#4a90d9',
      fontSize: '14px',
      fontWeight: 500,
    },
    emptyState: {
      padding: '40px 20px',
      textAlign: 'center' as const,
      color: '#888',
    },
  };

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <div>
          <h1 style={styles.title}>Experiments</h1>
          <p style={styles.subtitle}>Create and manage your rating experiments</p>
        </div>
        <button
          type="button"
          style={styles.docsLink}
          onClick={() => navigate('/admin/docs')}
        >
          How to run an experiment →
        </button>
      </div>

      {error && <div className="error" style={{ marginBottom: '16px' }}>{error}</div>}
      {success && <div className="success" style={{ marginBottom: '16px' }}>{success}</div>}

      {/* Two Column Grid */}
      <div style={styles.grid}>
        {/* Create Experiment */}
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h2 style={styles.sectionTitle}>Create New</h2>
          </div>
          <div style={styles.sectionBody}>
            <form onSubmit={handleCreateExperiment}>
              <div style={styles.inputGroup}>
                <label htmlFor="experiment-name" style={styles.label}>Public Name</label>
                <input
                  id="experiment-name"
                  data-testid="experiment-name-input"
                  type="text"
                  value={newExperiment.name}
                  onChange={(e) => setNewExperiment({ ...newExperiment, name: e.target.value })}
                  placeholder="e.g., Factuality Evaluation"
                  required
                  style={styles.input}
                />
                <div style={styles.hint}>Shown to raters on Prolific.</div>
              </div>
              <div style={styles.inputGroup}>
                <label htmlFor="experiment-internal-name" style={styles.label}>Internal Name (optional)</label>
                <input
                  id="experiment-internal-name"
                  data-testid="experiment-internal-name-input"
                  type="text"
                  value={newExperiment.internal_name ?? ''}
                  onChange={(e) => setNewExperiment({ ...newExperiment, internal_name: e.target.value })}
                  placeholder="e.g., Q2 Factuality Eval — Sander"
                  style={styles.input}
                />
                <div style={styles.hint}>Only visible to you and other researchers (in this dashboard and Prolific's researcher view).</div>
              </div>
              <div style={styles.inputGroup}>
                <label htmlFor="ratings-per-question" style={styles.label}>Ratings per Question</label>
                <input
                  id="ratings-per-question"
                  data-testid="ratings-per-question-input"
                  type="number"
                  value={newExperiment.num_ratings_per_question}
                  onChange={(e) => setNewExperiment({ ...newExperiment, num_ratings_per_question: parseInt(e.target.value) })}
                  min="1"
                  required
                  style={styles.input}
                />
                <div style={styles.hint}>How many different raters should evaluate each question.</div>
              </div>
              <div style={{ marginBottom: '16px', padding: '12px', background: '#f0f7ff', borderRadius: '6px', fontSize: '13px', color: '#555' }}>
                After creating the experiment and uploading questions, use the Prolific section to run a pilot study and launch rating rounds.
              </div>
              <button type="submit" style={styles.primaryButton}>
                Create Experiment
              </button>
            </form>
          </div>
        </div>

        {/* Experiments List */}
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h2 style={styles.sectionTitle}>Your Experiments</h2>
          </div>
          {loading ? (
            <div style={styles.emptyState}>Loading...</div>
          ) : experiments.length === 0 ? (
            <div style={styles.emptyState}>
              No experiments yet.<br />Create one to get started.
            </div>
          ) : (
            <div style={styles.experimentList}>
              {experiments.map((exp) => (
                <div
                  key={exp.id}
                  style={styles.experimentItem}
                  onClick={() => handleSelectExperiment(exp)}
                  onMouseEnter={(e) => e.currentTarget.style.background = '#f8f9fa'}
                  onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
                >
                  <div>
                    <div style={styles.experimentName}>{exp.internal_name || exp.name}</div>
                    {exp.internal_name && (
                      <div style={{ fontSize: '12px', color: '#666', marginBottom: '2px' }}>
                        Public: {exp.name}
                      </div>
                    )}
                    <div style={styles.experimentMeta}>
                      {exp.question_count} questions · {exp.rating_count} ratings
                    </div>
                  </div>
                  <span style={styles.viewLink}>View →</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default AdminView;
