import { useState, useEffect, useMemo, useRef } from 'react';
import type { Question } from '../types';

const LONG_CONTEXT_SEPARATOR_PATTERN = /\r?\n\r?\n--- QUESTION ---\r?\n/g;
const OPTION_LABEL_PATTERN = /(?:^|\r?\n)\s*(?:\(?[A-Z]\)?[.)]|[A-Z]:)\s+/g;
const openedLongContextDocumentKeys = new Set<string>();
// 5-point unipolar Likert scale for self-reported confidence (index 0 -> value 1).
const CONFIDENCE_LABELS = ['Not at all', 'Slightly', 'Moderately', 'Very', 'Completely'];

interface QuestionCardProps {
  question: Question;
  onSubmit: (answer: string, confidence: number, timeStarted: string) => Promise<void>;
  disabled?: boolean;
  assistanceAnswer?: string | null;
  assistanceActive?: boolean;
}

type QuestionDisplay = {
  documentText: string | null;
  questionText: string;
};

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function parseQuestionDisplay(questionText: string): QuestionDisplay {
  const separators = Array.from(questionText.matchAll(LONG_CONTEXT_SEPARATOR_PATTERN));
  const separator = separators[separators.length - 1];
  if (!separator || separator.index === undefined) {
    return { documentText: null, questionText };
  }

  const documentText = questionText.slice(0, separator.index).trim();
  const displayQuestion = questionText
    .slice(separator.index + separator[0].length)
    .trim();

  if (!documentText || !displayQuestion) {
    return { documentText: null, questionText };
  }

  return { documentText, questionText: displayQuestion };
}

function parseOptions(rawOptions: string | null): string[] {
  if (!rawOptions) {
    return [];
  }

  if (rawOptions.includes('|')) {
    return rawOptions.split('|').map(option => option.trim()).filter(Boolean);
  }

  const labeledOptionStarts = Array.from(rawOptions.matchAll(OPTION_LABEL_PATTERN))
    .map(match => match.index ?? 0);
  if (labeledOptionStarts.length > 1) {
    return labeledOptionStarts
      .map((start, index) => rawOptions.slice(start, labeledOptionStarts[index + 1]).trim())
      .filter(Boolean);
  }

  const lineOptions = rawOptions.split(/\r?\n+/).map(option => option.trim()).filter(Boolean);
  if (lineOptions.length > 1) {
    return lineOptions;
  }

  return rawOptions.split(',').map(option => option.trim()).filter(Boolean);
}

function buildLongContextDocumentHtml(question: Question, documentText: string): string {
  const title = `Document for Question ${question.question_id}`;

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>${escapeHtml(title)}</title>
  <style>
    body {
      margin: 0;
      background: #f6f7f9;
      color: #222;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 32px 24px;
    }
    h1 {
      margin: 0 0 20px;
      font-size: 22px;
      line-height: 1.3;
      font-weight: 650;
    }
    pre {
      box-sizing: border-box;
      width: 100%;
      margin: 0;
      padding: 24px;
      border: 1px solid #dfe3e8;
      border-radius: 8px;
      background: #fff;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
  </style>
</head>
<body>
  <main>
    <h1>${escapeHtml(title)}</h1>
    <pre>${escapeHtml(documentText)}</pre>
  </main>
</body>
</html>`;
}

function QuestionCard({ question, onSubmit, disabled = false, assistanceAnswer = null, assistanceActive = false }: QuestionCardProps) {
  const [selectedAnswer, setSelectedAnswer] = useState('');
  const [freeTextAnswer, setFreeTextAnswer] = useState('');
  const [confidence, setConfidence] = useState(3);
  const [submitting, setSubmitting] = useState(false);
  const [documentOpenBlocked, setDocumentOpenBlocked] = useState(false);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const timeStartedRef = useRef(new Date().toISOString());
  const display = useMemo(
    () => parseQuestionDisplay(question.question_text),
    [question.question_text]
  );

  useEffect(() => {
    setSelectedAnswer('');
    setFreeTextAnswer('');
    setConfidence(3);
    setSubmitting(false);
    setDocumentOpenBlocked(false);
    timeStartedRef.current = new Date().toISOString();
  }, [question.id]);

  useEffect(() => {
    if (!display.documentText) {
      setDocumentUrl(null);
      return;
    }

    const html = buildLongContextDocumentHtml(question, display.documentText);
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    setDocumentUrl(url);

    return () => {
      URL.revokeObjectURL(url);
      setDocumentUrl(null);
    };
  }, [display.documentText, question]);

  useEffect(() => {
    if (!documentUrl) {
      return;
    }

    const documentKey = `${question.id}:${question.question_id}`;
    if (openedLongContextDocumentKeys.has(documentKey)) {
      return;
    }
    openedLongContextDocumentKeys.add(documentKey);

    const openedWindow = window.open(documentUrl, '_blank');
    if (openedWindow) {
      openedWindow.opener = null;
    }
    setDocumentOpenBlocked(openedWindow === null);
  }, [documentUrl, question.id, question.question_id]);

  // Prefill with AI's suggested answer when assistance completes
  useEffect(() => {
    if (!assistanceAnswer) return;
    if (question.question_type === 'FT') {
      setFreeTextAnswer(assistanceAnswer);
    } else {
      setSelectedAnswer(assistanceAnswer);
    }
  }, [assistanceAnswer]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = async () => {
    const answer = question.question_type === 'FT' ? freeTextAnswer : selectedAnswer;

    if (!answer.trim()) {
      alert('Please provide an answer');
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit(answer, confidence, timeStartedRef.current);
    } finally {
      setSubmitting(false);
    }
  };

  const options = parseOptions(question.options);

  const isMC = question.question_type === 'MC' && options.length > 0;
  const canSubmit = !disabled && (isMC ? !!selectedAnswer : !!freeTextAnswer.trim());

  const styles = {
    card: {
      background: '#fff',
      borderRadius: '12px',
      border: '1px solid #e0e0e0',
      padding: '32px',
      boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
    },
    questionId: {
      fontSize: '12px',
      fontWeight: 500,
      color: '#888',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      marginBottom: '12px',
    },
    parentBox: {
      background: '#f0f4f8',
      border: '1px solid #d6e0ea',
      borderRadius: '8px',
      padding: '16px 20px',
      marginBottom: '20px',
    },
    parentLabel: {
      fontSize: '11px',
      fontWeight: 600,
      color: '#5a7896',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.5px',
      marginBottom: '6px',
    },
    parentText: {
      fontSize: '15px',
      lineHeight: 1.5,
      color: '#3a4a5c',
      whiteSpace: 'pre-wrap' as const,
      margin: 0,
    },
    questionText: {
      fontSize: '20px',
      lineHeight: 1.5,
      color: '#333',
      marginBottom: '28px',
      whiteSpace: 'pre-wrap' as const,
    },
    documentLink: {
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '10px 14px',
      background: '#f8f9fa',
      border: '1px solid #d6dce2',
      borderRadius: '8px',
      color: '#2f6fae',
      fontSize: '14px',
      fontWeight: 500,
      textDecoration: 'none',
      marginBottom: '20px',
    },
    documentNotice: {
      fontSize: '14px',
      color: '#666',
      marginTop: '-8px',
      marginBottom: '16px',
    },
    optionsGrid: {
      display: 'flex',
      flexDirection: 'column' as const,
      gap: '10px',
      marginBottom: '24px',
    },
    option: {
      padding: '16px 20px',
      background: '#f8f9fa',
      border: '2px solid transparent',
      borderRadius: '8px',
      cursor: 'pointer',
      fontSize: '15px',
      textAlign: 'left' as const,
      transition: 'all 0.15s',
      color: '#333',
    },
    optionSelected: {
      background: '#e3f2fd',
      borderColor: '#4a90d9',
      color: '#333',
    },
    textarea: {
      width: '100%',
      padding: '16px',
      border: '1px solid #ddd',
      borderRadius: '8px',
      fontSize: '15px',
      lineHeight: 1.5,
      resize: 'vertical' as const,
      marginBottom: '24px',
      boxSizing: 'border-box' as const,
    },
    confidenceSection: {
      marginBottom: '24px',
      padding: '20px',
      background: '#f8f9fa',
      borderRadius: '8px',
    },
    confidenceLabel: {
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'center',
      marginBottom: '12px',
    },
    confidenceTitle: {
      fontSize: '14px',
      fontWeight: 500,
      color: '#333',
    },
    confidenceValue: {
      fontSize: '18px',
      fontWeight: 600,
      color: '#4a90d9',
    },
    slider: {
      width: '100%',
      height: '8px',
      borderRadius: '4px',
      appearance: 'none' as const,
      background: '#ddd',
      outline: 'none',
      cursor: 'pointer',
    },
    sliderLabels: {
      position: 'relative' as const,
      height: '14px',
      marginTop: '8px',
      fontSize: '11px',
      color: '#888',
    },
    sliderLabel: {
      position: 'absolute' as const,
      whiteSpace: 'nowrap' as const,
    },
    submitButton: {
      width: '100%',
      padding: '16px',
      background: canSubmit ? '#4a90d9' : '#ccc',
      color: '#fff',
      border: 'none',
      borderRadius: '8px',
      fontSize: '16px',
      fontWeight: 500,
      cursor: canSubmit ? 'pointer' : 'not-allowed',
      transition: 'background 0.15s',
    },
  };

  return (
    <div style={styles.card}>
      <div style={styles.questionId}>Question {question.question_id}</div>

      {question.parent_question_text && (
        <div style={styles.parentBox}>
          <div style={styles.parentLabel}>Context</div>
          <p style={styles.parentText}>{question.parent_question_text}</p>
        </div>
      )}

      {display.documentText && documentUrl && (
        <>
          <a
            href={documentUrl}
            target="_blank"
            rel="noreferrer"
            style={styles.documentLink}
          >
            Open document in new tab
          </a>
          {documentOpenBlocked && (
            <p style={styles.documentNotice}>
              Your browser blocked the automatic document tab. Use the document link before answering.
            </p>
          )}
        </>
      )}

      <p style={styles.questionText}>{display.questionText}</p>

      {assistanceActive && assistanceAnswer == null && (
        <p style={{ fontSize: '14px', color: '#888', fontStyle: 'italic', marginBottom: '16px' }}>
          Answer the questions on the right to help inform your response.
        </p>
      )}

      {!assistanceActive && (isMC ? (
        <div style={styles.optionsGrid}>
          {options.map((option, index) => (
            <button
              key={index}
              style={{
                ...styles.option,
                ...(selectedAnswer === option ? styles.optionSelected : {}),
              }}
              onClick={() => setSelectedAnswer(option)}
              onMouseEnter={(e) => {
                if (selectedAnswer !== option) {
                  e.currentTarget.style.background = '#f0f0f0';
                }
              }}
              onMouseLeave={(e) => {
                if (selectedAnswer !== option) {
                  e.currentTarget.style.background = '#f8f9fa';
                }
              }}
            >
              {option}
            </button>
          ))}
        </div>
      ) : (
        <textarea
          value={freeTextAnswer}
          onChange={(e) => setFreeTextAnswer(e.target.value)}
          placeholder="Type your answer here..."
          rows={4}
          style={styles.textarea}
        />
      ))}

      {!assistanceActive && (
        <div style={styles.confidenceSection}>
          <div style={styles.confidenceLabel}>
            <span style={styles.confidenceTitle}>How confident are you?</span>
            <span style={styles.confidenceValue}>{CONFIDENCE_LABELS[confidence - 1]} confident</span>
          </div>
          <input
            type="range"
            min="1"
            max="5"
            value={confidence}
            onChange={(e) => setConfidence(parseInt(e.target.value))}
            style={styles.slider}
          />
          <div style={styles.sliderLabels}>
            {CONFIDENCE_LABELS.map((label, i) => {
              const pct = (i / (CONFIDENCE_LABELS.length - 1)) * 100;
              const isFirst = i === 0;
              const isLast = i === CONFIDENCE_LABELS.length - 1;
              return (
                <span
                  key={label}
                  style={{
                    ...styles.sliderLabel,
                    left: `${pct}%`,
                    transform: isFirst ? 'none' : isLast ? 'translateX(-100%)' : 'translateX(-50%)',
                    fontWeight: confidence === i + 1 ? 600 : 400,
                    color: confidence === i + 1 ? '#4a90d9' : '#888',
                  }}
                >
                  {label}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {(!assistanceActive) && (
        <button
          onClick={handleSubmit}
          disabled={submitting || !canSubmit}
          style={styles.submitButton}
        >
          {submitting ? 'Submitting...' : 'Submit Answer'}
        </button>
      )}
    </div>
  );
}

export default QuestionCard;
