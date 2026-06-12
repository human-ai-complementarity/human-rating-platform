import { useState, useEffect } from 'react';
import { api } from '../api';
import type { AssistanceStep, Subtask } from '../types';

interface AssistancePanelProps {
  sessionToken: string;
  questionId: number;
  onSessionId: (sessionId: number) => void;
  onStepChange: (step: AssistanceStep | null) => void;
}

function SubtaskInput({
  subtask,
  value,
  confidence,
  onChange,
  onConfidenceChange,
}: {
  subtask: Subtask;
  value: string;
  confidence: number;
  onChange: (value: string) => void;
  onConfidenceChange: (value: number) => void;
}) {
  const styles = {
    prompt: {
      fontSize: '14px',
      fontWeight: 500 as const,
      color: '#333',
      marginBottom: '10px',
      lineHeight: 1.4,
    },
    binaryGroup: {
      display: 'flex',
      gap: '8px',
    },
    binaryBtn: (selected: boolean) => ({
      flex: 1,
      padding: '10px',
      border: `2px solid ${selected ? '#4a90d9' : '#ddd'}`,
      borderRadius: '8px',
      background: selected ? '#e3f2fd' : '#f8f9fa',
      color: '#333',
      fontSize: '14px',
      fontWeight: selected ? 600 : 400,
      cursor: 'pointer',
      textAlign: 'center' as const,
    }),
    radioOption: (selected: boolean) => ({
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      padding: '10px 14px',
      border: `2px solid ${selected ? '#4a90d9' : '#ddd'}`,
      borderRadius: '8px',
      background: selected ? '#e3f2fd' : '#f8f9fa',
      cursor: 'pointer',
      marginBottom: '6px',
      fontSize: '14px',
      color: '#333',
      textAlign: 'left' as const,
    }),
    textarea: {
      width: '100%',
      padding: '10px',
      border: '1px solid #ddd',
      borderRadius: '8px',
      fontSize: '14px',
      lineHeight: 1.5,
      resize: 'vertical' as const,
      boxSizing: 'border-box' as const,
    },
    sliderRow: {
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
    },
    sliderValue: {
      fontSize: '16px',
      fontWeight: 600,
      color: '#4a90d9',
      minWidth: '24px',
    },
  };

  return (
    <div>
      <p style={styles.prompt}>{subtask.question}</p>
      {subtask.my_answer && subtask.confidence !== undefined && (
        <p style={{ fontSize: '12px', color: '#aaa', marginBottom: '8px' }}>
          AI confidence: {subtask.confidence}%
        </p>
      )}
      {subtask.type === 'binary' && (
        <div style={styles.binaryGroup}>
          {['Yes', 'No'].map(opt => (
            <button
              key={opt}
              style={styles.binaryBtn(value === opt.toLowerCase())}
              onClick={() => onChange(opt.toLowerCase())}
            >
              {opt}
            </button>
          ))}
        </div>
      )}
      {subtask.type === 'multiple_choice' && (
        <div>
          {(subtask.options ?? []).map(opt => (
            <div key={opt} style={styles.radioOption(value === opt)} onClick={() => onChange(opt)}>
              <input type="radio" readOnly checked={value === opt} style={{ accentColor: '#4a90d9', width: 'auto', marginBottom: 0 }} />
              {opt}
            </div>
          ))}
        </div>
      )}
      {subtask.type === 'free_text' && (
        <textarea
          value={value}
          onChange={e => onChange(e.target.value)}
          rows={3}
          placeholder="Your answer..."
          style={styles.textarea}
        />
      )}
      {subtask.type === 'rating_scale' && (
        <div style={styles.sliderRow}>
          <input
            type="range"
            min="1"
            max="5"
            value={value || '3'}
            onChange={e => onChange(e.target.value)}
            style={{ flex: 1, accentColor: '#4a90d9' }}
          />
          <span style={styles.sliderValue}>{value || '3'}/5</span>
        </div>
      )}
      <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid #eee' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
          <span style={{ fontSize: '12px', color: '#888' }}>Your confidence</span>
          <span style={{ fontSize: '12px', fontWeight: 600, color: '#4a90d9' }}>{confidence}/5</span>
        </div>
        <input
          type="range"
          min="1"
          max="5"
          value={confidence}
          onChange={e => onConfidenceChange(parseInt(e.target.value))}
          style={{ width: '100%', accentColor: '#4a90d9', margin: 0 }}
        />
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11px', color: '#bbb', marginTop: '2px' }}>
          <span>Not confident</span>
          <span>Very confident</span>
        </div>
      </div>
    </div>
  );
}

function AssistancePanel({ sessionToken, questionId, onSessionId, onStepChange }: AssistancePanelProps) {
  const [step, setStep] = useState<AssistanceStep | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [answers, setAnswers] = useState<Record<number, { answer: string; confidence: number }>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStep(null);
    setAnswers({});
    setError(null);
    onStepChange(null);

    api
      .startAssistance(sessionToken, questionId)
      .then(s => {
        if (cancelled) return;
        setStep(s);
        onSessionId(s.session_id);
        onStepChange(s);
      })
      .catch(err => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load assistance');
        onStepChange(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, [sessionToken, questionId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Prefill high-confidence subtasks with the AI's answer; leave low-confidence blank.
  useEffect(() => {
    const subtasks = step?.payload?.subtasks ?? [];
    if (subtasks.length === 0) return;
    const threshold: number = step?.payload?.confidence_threshold ?? 75;
    const prefilled: Record<number, { answer: string; confidence: number }> = {};
    for (const st of subtasks) {
      if (st.my_answer && st.confidence !== undefined && st.confidence >= threshold) {
        prefilled[st.index] = {
          answer: st.my_answer!,
          confidence: Math.max(1, Math.round(st.confidence / 20)),
        };
      }
    }
    setAnswers(prefilled);
  }, [step?.payload?.subtasks, step?.payload?.confidence_threshold]);

  const handleSubmit = async () => {
    if (!step) return;
    setSubmitting(true);
    try {
      const result = await api.advanceAssistance(sessionToken, step.session_id, answers);
      setStep(result);
      onStepChange(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit answers');
    } finally {
      setSubmitting(false);
    }
  };

  const subtasks = step?.payload?.subtasks ?? [];
  const allAnswered =
    subtasks.length > 0 &&
    subtasks.every(st => {
      const val = answers[st.index];
      return val !== undefined && val.answer !== '';
    });

  const styles = {
    panel: {
      background: '#fff',
      border: '1px solid #e0e0e0',
      borderRadius: '12px',
      overflow: 'hidden',
      height: '100%',
      display: 'flex',
      flexDirection: 'column' as const,
    },
    panelHeader: {
      padding: '16px 20px',
      borderBottom: '1px solid #e0e0e0',
      background: '#fafafa',
    },
    stepLabel: {
      fontSize: '11px',
      fontWeight: 600,
      color: '#888',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      marginBottom: '2px',
    },
    panelTitle: {
      fontSize: '15px',
      fontWeight: 600,
      color: '#333',
      margin: 0,
    },
    panelBody: {
      padding: '20px',
      flex: 1,
      overflowY: 'auto' as const,
    },
    subtaskCard: {
      border: '1px solid #e8e8e8',
      borderRadius: '8px',
      padding: '16px',
      marginBottom: '12px',
      background: '#fafafa',
    },
    subtaskNumber: {
      fontSize: '11px',
      fontWeight: 600,
      color: '#4a90d9',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      marginBottom: '8px',
    },
    confidenceSection: {
      border: '1px solid #e8e8e8',
      borderRadius: '8px',
      padding: '16px',
      marginBottom: '12px',
      background: '#fafafa',
    },
    confidenceHeader: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      marginBottom: '10px',
    },
    confidenceTitle: {
      fontSize: '13px',
      fontWeight: 500,
      color: '#555',
    },
    confidenceValue: {
      fontSize: '15px',
      fontWeight: 600,
      color: '#4a90d9',
    },
    confidenceLabels: {
      display: 'flex',
      justifyContent: 'space-between',
      marginTop: '6px',
      fontSize: '11px',
      color: '#999',
    },
    submitBtn: (enabled: boolean) => ({
      marginTop: '4px',
      width: '100%',
      padding: '12px',
      background: enabled ? '#4a90d9' : '#e0e0e0',
      color: enabled ? '#fff' : '#999',
      border: 'none',
      borderRadius: '8px',
      fontSize: '14px',
      fontWeight: 600,
      cursor: enabled ? 'pointer' : 'not-allowed',
    }),
    synthesisSection: {
      marginBottom: '16px',
    },
    synthesisLabel: {
      fontSize: '11px',
      fontWeight: 600,
      color: '#888',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      marginBottom: '8px',
    },
    synthesisRecommendation: {
      background: '#e3f2fd',
      border: '1px solid #90caf9',
      borderRadius: '8px',
      padding: '12px 16px',
      fontSize: '14px',
      fontWeight: 600,
      color: '#1565c0',
      marginBottom: '12px',
    },
    synthesisReasoning: {
      fontSize: '13px',
      color: '#555',
      lineHeight: 1.6,
      background: '#f8f9fa',
      borderRadius: '8px',
      padding: '12px 16px',
    },
    answeredRow: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
      gap: '12px',
      padding: '8px 0',
      borderBottom: '1px solid #f0f0f0',
    },
    answeredPrompt: {
      fontSize: '13px',
      color: '#666',
      flex: 1,
    },
    answeredValue: {
      fontSize: '13px',
      fontWeight: 600,
      color: '#333',
      textAlign: 'right' as const,
    },
    continueHint: {
      marginTop: '16px',
      padding: '10px 14px',
      background: '#e8f5e9',
      border: '1px solid #a5d6a7',
      borderRadius: '8px',
      fontSize: '13px',
      color: '#2e7d32',
      fontWeight: 500,
    },
    loadingText: {
      fontSize: '14px',
      color: '#888',
      padding: '20px',
      textAlign: 'center' as const,
    },
    errorText: {
      fontSize: '13px',
      color: '#dc3545',
    },
    topNList: {
      display: 'flex',
      flexDirection: 'column' as const,
      gap: '10px',
    },
    topNCard: {
      border: '1px solid #d6e6f8',
      borderRadius: '8px',
      padding: '14px',
      background: '#f7fbff',
    },
    topNHeader: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-start',
      gap: '12px',
      marginBottom: '8px',
    },
    topNAnswer: {
      fontSize: '14px',
      fontWeight: 650,
      color: '#1f3f5b',
      lineHeight: 1.4,
    },
    topNBadge: {
      flexShrink: 0,
      fontSize: '12px',
      fontWeight: 650,
      color: '#2f6fae',
      background: '#e3f2fd',
      borderRadius: '999px',
      padding: '3px 8px',
    },
    topNRationale: {
      margin: 0,
      fontSize: '13px',
      lineHeight: 1.5,
      color: '#52616f',
    },
  };

  if (loading) {
    return (
      <div style={styles.panel}>
        <div style={styles.panelHeader}>
          <div style={styles.stepLabel}>AI Assistance</div>
          <p style={styles.panelTitle}>Analyzing question…</p>
        </div>
        <div style={styles.loadingText}>Preparing guidance, please wait…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={styles.panel}>
        <div style={styles.panelHeader}>
          <div style={styles.stepLabel}>AI Assistance</div>
          <p style={styles.panelTitle}>Could not load assistance</p>
        </div>
        <div style={styles.panelBody}>
          <p style={styles.errorText}>{error}</p>
        </div>
      </div>
    );
  }

  if (!step || step.type === 'none' || step.type === 'skip') return null;

  if (step.type === 'display' && step.payload.kind === 'top_n') {
    const candidates = step.payload.candidates ?? [];
    return (
      <div style={styles.panel}>
        <div style={styles.panelHeader}>
          <div style={styles.stepLabel}>AI Assistance</div>
          <p style={styles.panelTitle}>Top {step.payload.top_n ?? candidates.length} Suggestions</p>
        </div>
        <div style={styles.panelBody}>
          <div style={styles.topNList}>
            {candidates.map(candidate => (
              <div key={`${candidate.rank}-${candidate.answer}`} style={styles.topNCard}>
                <div style={styles.topNHeader}>
                  <div style={styles.topNAnswer}>
                    {candidate.rank}. {candidate.answer}
                  </div>
                  {candidate.confidence !== undefined && (
                    <span style={styles.topNBadge}>{candidate.confidence}%</span>
                  )}
                </div>
                {candidate.rationale && (
                  <p style={styles.topNRationale}>{candidate.rationale}</p>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (step.type === 'complete') {
    const synthesis = step.payload.synthesis;
    const completedHistory: Array<{ subtasks: Subtask[]; answers: Record<string, { answer: string; confidence?: number }> }> =
      step.payload.history ?? [];
    return (
      <div style={styles.panel}>
        <div style={styles.panelHeader}>
          <div style={styles.stepLabel}>Analysis complete</div>
          <p style={styles.panelTitle}>AI Analysis</p>
        </div>
        <div style={styles.panelBody}>
          {synthesis && (
            <div style={styles.synthesisSection}>
              <div style={styles.synthesisLabel}>Suggested Answer</div>
              <div style={styles.synthesisRecommendation}>{synthesis.answer}</div>
            </div>
          )}
          <div style={styles.synthesisLabel}>Your answers</div>
          {completedHistory.map((round, ri) =>
            round.subtasks.map(st => (
              <div key={`${ri}-${st.index}`} style={styles.answeredRow}>
                <span style={styles.answeredPrompt}>{st.question}</span>
                <span style={styles.answeredValue}>{round.answers[String(st.index)]?.answer ?? '—'}</span>
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  // ask_input state
  const iteration = step?.payload?.iteration ?? 1;
  const maxRounds = step?.payload?.max_rounds ?? 5;
  const history: Array<{ subtasks: Subtask[]; answers: Record<string, { answer: string; confidence?: number }> }> =
    step?.payload?.history ?? [];

  return (
    <div style={styles.panel}>
      <div style={styles.panelHeader}>
        <div style={styles.stepLabel}>
          Round {iteration} of up to {maxRounds}
        </div>
        <p style={styles.panelTitle}>Answer before rating</p>
      </div>
      <div style={styles.panelBody}>
        {/* Previous rounds collapsed */}
        {history.length > 0 && (
          <div style={{ marginBottom: '16px' }}>
            <div style={styles.synthesisLabel}>Previous answers</div>
            {history.map((round, ri) =>
              round.subtasks.map(st => (
                <div key={`${ri}-${st.index}`} style={{ ...styles.answeredRow, opacity: 0.7 }}>
                  <span style={styles.answeredPrompt}>{st.question}</span>
                  <span style={styles.answeredValue}>{round.answers[String(st.index)]?.answer ?? '—'}</span>
                </div>
              ))
            )}
          </div>
        )}

        {/* Current round */}
        {subtasks.map(st => (
          <div key={st.index} style={styles.subtaskCard}>
            <div style={styles.subtaskNumber}>{st.index + 1} of {subtasks.length}</div>
            <SubtaskInput
              subtask={st}
              value={answers[st.index]?.answer ?? ''}
              confidence={answers[st.index]?.confidence ?? 3}
              onChange={val => setAnswers(prev => ({ ...prev, [st.index]: { ...prev[st.index] ?? { confidence: 3 }, answer: val } }))}
              onConfidenceChange={val => setAnswers(prev => ({ ...prev, [st.index]: { ...prev[st.index] ?? { answer: '' }, confidence: val } }))}
            />
          </div>
        ))}
        <button
          style={styles.submitBtn(allAnswered && !submitting)}
          disabled={!allAnswered || submitting}
          onClick={handleSubmit}
        >
          {submitting ? 'Submitting…' : 'Submit Answers'}
        </button>
      </div>
    </div>
  );
}

export default AssistancePanel;
