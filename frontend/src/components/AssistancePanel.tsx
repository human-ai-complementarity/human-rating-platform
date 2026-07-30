import { useState, useEffect } from 'react';
import { api } from '../api';
import type { AssistanceStep, Subtask } from '../types';
import { Banner, textareaStyle } from './experiment-detail/ui';

interface AssistancePanelProps {
  sessionToken: string;
  questionId: number;
  onSessionId: (sessionId: number) => void;
  onStepChange: (step: AssistanceStep | null) => void;
  // The options the rater sees, used to map a Top-N suggestion onto a real
  // option. Empty for free-response questions.
  questionOptions?: string[];
  // The rater's current answer, so the picked suggestion stays highlighted.
  selectedAnswer?: string;
  onSelectAnswer?: (answer: string) => void;
}

type TopNCandidate = NonNullable<AssistanceStep['payload']['candidates']>[number];

// The backend and the rater UI parse `question.options` independently, so match
// on the answer text first and fall back to the 1-based index the LLM returned.
// Returns null when the suggestion maps onto no option the rater can pick.
function resolveCandidateAnswer(candidate: TopNCandidate, options: string[]): string | null {
  if (options.length === 0) return candidate.answer;
  if (options.includes(candidate.answer)) return candidate.answer;
  const index = candidate.option_index;
  if (typeof index === 'number' && index >= 1 && index <= options.length) {
    return options[index - 1];
  }
  return null;
}

const monoLabel = {
  font: '600 10px/1 var(--font-mono)',
  letterSpacing: '0.16em',
  textTransform: 'uppercase' as const,
  color: 'var(--muted)',
};

function OptionTile({
  selected,
  children,
  onClick,
}: {
  selected: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '10px 14px',
        background: selected ? 'var(--accent-soft)' : 'var(--surface)',
        border: `1px solid ${selected ? 'var(--accent)' : 'var(--faint)'}`,
        borderRadius: 'var(--radius-sm)',
        cursor: 'pointer',
        fontSize: 14,
        textAlign: 'left',
        color: 'var(--ink)',
        font: '500 14px/1.4 var(--font-body)',
        width: '100%',
      }}
    >
      <span
        aria-hidden
        style={{
          width: 15,
          height: 15,
          borderRadius: '50%',
          border: `2px solid ${selected ? 'var(--accent)' : 'var(--faint)'}`,
          flex: '0 0 auto',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--surface)',
        }}
      >
        {selected && (
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              background: 'var(--accent)',
            }}
          />
        )}
      </span>
      <span style={{ flex: 1 }}>{children}</span>
    </button>
  );
}

function CandidateCard({
  candidate,
  selected,
  onSelect,
}: {
  candidate: TopNCandidate;
  selected: boolean;
  onSelect?: () => void;
}) {
  const interactive = onSelect !== undefined;
  const cardStyle: React.CSSProperties = {
    display: 'block',
    width: '100%',
    border: `1px solid ${selected ? 'var(--accent)' : 'var(--faint)'}`,
    borderRadius: 'var(--radius-sm)',
    padding: '14px 16px',
    background: selected ? 'var(--accent-soft)' : 'var(--surface-2)',
    textAlign: 'left',
    cursor: interactive ? 'pointer' : 'default',
  };

  const content = (
    <>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        {interactive && (
          <span
            aria-hidden
            style={{
              width: 15,
              height: 15,
              borderRadius: '50%',
              border: `2px solid ${selected ? 'var(--accent)' : 'var(--faint)'}`,
              background: 'var(--surface)',
              flex: '0 0 auto',
              marginTop: 2,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            {selected && (
              <span
                style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--accent)' }}
              />
            )}
          </span>
        )}
        <span
          style={{
            font: '700 12px/1.5 var(--font-mono)',
            color: 'var(--accent-soft-ink)',
            letterSpacing: '0.04em',
            flex: '0 0 auto',
          }}
        >
          {candidate.rank}
        </span>
        <span style={{ flex: 1, fontSize: 14, fontWeight: 600, color: 'var(--ink)', lineHeight: 1.4 }}>
          {candidate.answer}
        </span>
      </div>
      {candidate.confidence !== undefined && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
          <div
            style={{
              flex: 1,
              height: 5,
              borderRadius: 999,
              background: 'var(--faint)',
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${candidate.confidence}%`,
                height: '100%',
                background: 'var(--accent)',
              }}
            />
          </div>
          <span style={{ ...monoLabel, color: 'var(--accent-soft-ink)', flex: '0 0 auto' }}>
            AI confidence {candidate.confidence}%
          </span>
        </div>
      )}
      {candidate.rationale && (
        <p style={{ margin: '8px 0 0', fontSize: 13, lineHeight: 1.55, color: 'var(--muted)' }}>
          {candidate.rationale}
        </p>
      )}
    </>
  );

  if (!interactive) {
    return <div style={cardStyle}>{content}</div>;
  }

  return (
    <button
      type="button"
      data-testid={`top-n-candidate-${candidate.rank}`}
      aria-pressed={selected}
      onClick={onSelect}
      style={{ ...cardStyle, font: 'inherit' }}
    >
      {content}
    </button>
  );
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
  return (
    <div>
      <p
        style={{
          fontSize: 14,
          fontWeight: 600,
          color: 'var(--ink)',
          margin: '0 0 10px',
          lineHeight: 1.45,
        }}
      >
        {subtask.question}
      </p>
      {subtask.my_answer && subtask.confidence !== undefined && (
        <p style={{ fontSize: 11, color: 'var(--muted)', margin: '0 0 10px', fontFamily: 'var(--font-mono)', letterSpacing: '0.06em' }}>
          AI confidence: {subtask.confidence}%
        </p>
      )}
      {subtask.type === 'binary' && (
        <div style={{ display: 'flex', gap: 8 }}>
          {['Yes', 'No'].map(opt => (
            <div key={opt} style={{ flex: 1 }}>
              <OptionTile selected={value === opt.toLowerCase()} onClick={() => onChange(opt.toLowerCase())}>
                {opt}
              </OptionTile>
            </div>
          ))}
        </div>
      )}
      {subtask.type === 'multiple_choice' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {(subtask.options ?? []).map(opt => (
            <OptionTile key={opt} selected={value === opt} onClick={() => onChange(opt)}>
              {opt}
            </OptionTile>
          ))}
        </div>
      )}
      {subtask.type === 'free_text' && (
        <textarea
          value={value}
          onChange={e => onChange(e.target.value)}
          rows={3}
          placeholder="Your answer..."
          style={{ ...textareaStyle, fontSize: 14, padding: '10px 12px' }}
        />
      )}
      {subtask.type === 'rating_scale' && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <input
            type="range"
            min="1"
            max="5"
            value={value || '3'}
            onChange={e => onChange(e.target.value)}
            style={{ flex: 1, accentColor: 'var(--accent)' }}
          />
          <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--accent-soft-ink)', minWidth: 26 }}>
            {value || '3'}/5
          </span>
        </div>
      )}
      <div
        style={{
          marginTop: 12,
          paddingTop: 12,
          borderTop: '1px solid var(--faint)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
          <span style={monoLabel}>Your confidence</span>
          <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--accent-soft-ink)' }}>{confidence}/5</span>
        </div>
        <input
          type="range"
          min="1"
          max="5"
          value={confidence}
          onChange={e => onConfidenceChange(parseInt(e.target.value))}
          style={{ width: '100%', accentColor: 'var(--accent)', margin: 0 }}
        />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            letterSpacing: '0.04em',
            color: 'var(--muted)',
            marginTop: 4,
          }}
        >
          <span>Not confident</span>
          <span>Very confident</span>
        </div>
      </div>
    </div>
  );
}

const panelStyle: React.CSSProperties = {
  background: 'var(--surface)',
  border: '1px solid var(--faint)',
  borderRadius: 'var(--radius)',
  boxShadow: 'var(--shadow)',
  overflow: 'hidden',
  height: '100%',
  display: 'flex',
  flexDirection: 'column',
  position: 'sticky',
  top: 24,
};

const panelHeaderStyle: React.CSSProperties = {
  padding: '16px 22px',
  borderBottom: '1px solid var(--line)',
  background: 'var(--surface-2)',
};

const stepLabelStyle: React.CSSProperties = {
  ...monoLabel,
  marginBottom: 6,
  display: 'block',
};

const panelTitleStyle: React.CSSProperties = {
  fontFamily: 'var(--font-head)',
  fontSize: 17,
  fontWeight: 600,
  color: 'var(--ink)',
  letterSpacing: '-0.005em',
  margin: 0,
};

const panelBodyStyle: React.CSSProperties = {
  padding: 22,
  flex: 1,
  overflowY: 'auto',
};

const subtaskCardStyle: React.CSSProperties = {
  border: '1px solid var(--faint)',
  borderRadius: 'var(--radius-sm)',
  padding: 16,
  marginBottom: 12,
  background: 'var(--surface-2)',
};

const answeredRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  gap: 12,
  padding: '8px 0',
  borderBottom: '1px solid var(--line)',
};

function AssistancePanel({
  sessionToken,
  questionId,
  onSessionId,
  onStepChange,
  questionOptions = [],
  selectedAnswer = '',
  onSelectAnswer,
}: AssistancePanelProps) {
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

  if (loading) {
    return (
      <div style={panelStyle}>
        <div style={panelHeaderStyle}>
          <span style={stepLabelStyle}>AI Assistance</span>
          <p style={panelTitleStyle}>Analyzing question…</p>
        </div>
        <div style={{ ...panelBodyStyle, textAlign: 'center', color: 'var(--muted)', fontSize: 14 }}>
          Preparing guidance, please wait…
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={panelStyle}>
        <div style={panelHeaderStyle}>
          <span style={stepLabelStyle}>AI Assistance</span>
          <p style={panelTitleStyle}>Could not load assistance</p>
        </div>
        <div style={panelBodyStyle}>
          <Banner tone="danger">{error}</Banner>
        </div>
      </div>
    );
  }

  if (!step || step.type === 'none' || step.type === 'skip') return null;

  if (step.type === 'display' && step.payload.kind === 'top_n') {
    const candidates = step.payload.candidates ?? [];
    return (
      <div style={panelStyle}>
        <div style={panelHeaderStyle}>
          <span style={stepLabelStyle}>AI Assistance</span>
          <p style={panelTitleStyle}>
            Top {step.payload.top_n ?? candidates.length} answers by AI confidence
          </p>
        </div>
        <div style={panelBodyStyle}>
          <p style={{ margin: '0 0 14px', fontSize: 13, lineHeight: 1.55, color: 'var(--muted)' }}>
            Ranked highest-confidence first. The percentages are the AI's own confidence
            scores, not ground truth.
            {onSelectAnswer && ' Click a suggestion to select it as your answer; you can still change it before submitting.'}
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {candidates.map(candidate => {
              const resolved = resolveCandidateAnswer(candidate, questionOptions);
              const selectable = Boolean(onSelectAnswer && resolved !== null);
              const selected = resolved !== null && resolved === selectedAnswer;
              return (
                <CandidateCard
                  key={`${candidate.rank}-${candidate.answer}`}
                  candidate={candidate}
                  selected={selected}
                  onSelect={selectable ? () => onSelectAnswer!(resolved!) : undefined}
                />
              );
            })}
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
      <div style={panelStyle}>
        <div style={panelHeaderStyle}>
          <span style={stepLabelStyle}>Analysis complete</span>
          <p style={panelTitleStyle}>AI analysis</p>
        </div>
        <div style={panelBodyStyle}>
          {synthesis && (
            <div style={{ marginBottom: 18 }}>
              <div style={{ ...monoLabel, marginBottom: 8 }}>Suggested answer</div>
              <Banner tone="ok" icon={false}>
                <span style={{ fontWeight: 600, color: 'var(--ink)' }}>{synthesis.answer}</span>
              </Banner>
            </div>
          )}
          <div style={{ ...monoLabel, marginBottom: 8 }}>Your answers</div>
          {completedHistory.map((round, ri) =>
            round.subtasks.map(st => (
              <div key={`${ri}-${st.index}`} style={answeredRowStyle}>
                <span style={{ fontSize: 13, color: 'var(--muted)', flex: 1, lineHeight: 1.45 }}>
                  {st.question}
                </span>
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: 'var(--ink)',
                    textAlign: 'right',
                  }}
                >
                  {round.answers[String(st.index)]?.answer ?? '—'}
                </span>
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
    <div style={panelStyle}>
      <div style={panelHeaderStyle}>
        <span style={stepLabelStyle}>
          Round {iteration} of up to {maxRounds}
        </span>
        <p style={panelTitleStyle}>Answer before rating</p>
      </div>
      <div style={panelBodyStyle}>
        {history.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ ...monoLabel, marginBottom: 8 }}>Previous answers</div>
            {history.map((round, ri) =>
              round.subtasks.map(st => (
                <div key={`${ri}-${st.index}`} style={{ ...answeredRowStyle, opacity: 0.7 }}>
                  <span style={{ fontSize: 13, color: 'var(--muted)', flex: 1, lineHeight: 1.45 }}>
                    {st.question}
                  </span>
                  <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--ink)', textAlign: 'right' }}>
                    {round.answers[String(st.index)]?.answer ?? '—'}
                  </span>
                </div>
              ))
            )}
          </div>
        )}

        {subtasks.map(st => (
          <div key={st.index} style={subtaskCardStyle}>
            <div style={{ ...monoLabel, color: 'var(--accent-soft-ink)', marginBottom: 10 }}>
              {st.index + 1} of {subtasks.length}
            </div>
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
          type="button"
          onClick={handleSubmit}
          disabled={!allAnswered || submitting}
          style={{
            marginTop: 4,
            width: '100%',
            padding: '12px 16px',
            background: allAnswered && !submitting ? 'var(--accent)' : 'var(--faint)',
            color: allAnswered && !submitting ? 'var(--accent-ink)' : 'var(--muted)',
            border: 'none',
            borderRadius: 'var(--radius-sm)',
            font: '600 14px var(--font-body)',
            cursor: allAnswered && !submitting ? 'pointer' : 'not-allowed',
          }}
        >
          {submitting ? 'Submitting…' : 'Submit answers'}
        </button>
      </div>
    </div>
  );
}

export default AssistancePanel;
