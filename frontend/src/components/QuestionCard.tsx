import { useState, useEffect, useMemo, useRef } from 'react';
import type { CSSProperties } from 'react';
import type { Question } from '../types';
import { primaryButton, textareaStyle } from './experiment-detail/ui';

const LONG_CONTEXT_SEPARATOR_PATTERN = /\r?\n\r?\n--- QUESTION ---\r?\n/g;
// Parent context longer than this is a document, not a preamble, so it goes
// behind the "open in new tab" link instead of inline in the card. The two
// real cases sit orders of magnitude apart (a sub-question preamble runs tens
// of characters, a long-context document runs six figures), so the exact value
// only matters if a dataset ever lands in between.
const INLINE_CONTEXT_MAX_CHARS = 2000;
const OPTION_LABEL_PATTERN = /(?:^|\r?\n)\s*(?:\(?[A-Z]\)?[.)]|[A-Z]:)\s+/g;
// 5-point unipolar Likert scale for self-reported confidence (index 0 -> value 1).
const CONFIDENCE_LABELS = ['Not at all', 'Slightly', 'Moderately', 'Very', 'Completely'];

interface QuestionCardProps {
  question: Question;
  onSubmit: (answer: string, confidence: number, timeStarted: string) => Promise<void>;
  disabled?: boolean;
  assistanceAnswer?: string | null;
  assistanceActive?: boolean;
  // Researcher-supplied framing from the dataset metadata. `humanPromptPrefix`
  // is rendered above the question text, `humanPromptSuffix` below it. Both are
  // constant for the session and either may be null.
  humanPromptPrefix?: string | null;
  humanPromptSuffix?: string | null;
}

type QuestionDisplay = {
  /** Rendered behind the "open document in new tab" link. */
  documentText: string | null;
  /** Rendered inline in the "Context" box above the question. */
  inlineContext: string | null;
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

// Researcher-supplied framing is instructional, not decorative: raters are
// expected to read it before answering, so it gets a labelled callout rather
// than muted italics.
function PromptFraming({ text, style }: { text: string; style?: CSSProperties }) {
  return (
    <div
      style={{
        background: 'var(--accent-soft)',
        borderLeft: '3px solid var(--accent)',
        borderRadius: 'var(--radius-sm)',
        padding: '14px 18px',
        ...style,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: 'var(--accent-soft-ink)',
          marginBottom: 7,
        }}
      >
        Instructions
      </div>
      <p
        style={{
          fontSize: 16,
          lineHeight: 1.6,
          color: 'var(--ink)',
          margin: 0,
          whiteSpace: 'pre-wrap',
        }}
      >
        {text}
      </p>
    </div>
  );
}

// Splits `--- QUESTION ---`-delimited text into document and question. Returns
// a null document when the delimiter is absent or either side is empty.
function splitOnSeparator(questionText: string): {
  documentText: string | null;
  questionText: string;
} {
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

/**
 * Decides where a question's context is rendered.
 *
 * Two mechanisms feed this. `--- QUESTION ---` inside `question_text` splits a
 * document off inline, and `parent_question_id` stores the document as its own
 * row served back as `parent_question_text`. The parent shape is the direction
 * we're moving in (see issue #85); the separator is kept working until the
 * pipeline stops emitting it.
 *
 * A long parent is routed to the document link, which is the whole point of
 * this function. The separator branch takes precedence so no data that renders
 * a document today changes behaviour, which is what makes this additive.
 */
function parseQuestionDisplay(question: Question): QuestionDisplay {
  const split = splitOnSeparator(question.question_text);
  const parent = question.parent_question_text?.trim() || null;

  const parentIsDocument =
    parent !== null && split.documentText === null && parent.length > INLINE_CONTEXT_MAX_CHARS;

  return {
    documentText: split.documentText ?? (parentIsDocument ? parent : null),
    inlineContext: parentIsDocument ? null : parent,
    questionText: split.questionText,
  };
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
      background: #f7f5ef;
      color: #282420;
      font-family: "Public Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 40px 24px;
    }
    h1 {
      margin: 0 0 20px;
      font-family: "Source Serif 4", Georgia, serif;
      font-size: 26px;
      line-height: 1.25;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    pre {
      box-sizing: border-box;
      width: 100%;
      margin: 0;
      padding: 24px;
      border: 1px solid #e6e1d5;
      border-radius: 9px;
      background: #ffffff;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 14px/1.6 "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #282420;
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

function QuestionCard({ question, onSubmit, disabled = false, assistanceAnswer = null, assistanceActive = false, humanPromptPrefix = null, humanPromptSuffix = null }: QuestionCardProps) {
  const [selectedAnswer, setSelectedAnswer] = useState('');
  const [freeTextAnswer, setFreeTextAnswer] = useState('');
  const [confidence, setConfidence] = useState(3);
  const [submitting, setSubmitting] = useState(false);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const timeStartedRef = useRef(new Date().toISOString());
  const display = useMemo(
    () => parseQuestionDisplay(question),
    [question]
  );
  const options = useMemo(() => parseOptions(question.options), [question.options]);

  // An MC question with no usable options falls back to the free-text input, so
  // every answer read below has to follow the input that is actually rendered.
  const isMC = question.question_type === 'MC' && options.length > 0;

  useEffect(() => {
    setSelectedAnswer('');
    setFreeTextAnswer('');
    setConfidence(3);
    setSubmitting(false);
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

  // Prefill with AI's suggested answer when assistance completes
  useEffect(() => {
    if (!assistanceAnswer) return;
    if (isMC) {
      setSelectedAnswer(assistanceAnswer);
    } else {
      setFreeTextAnswer(assistanceAnswer);
    }
  }, [assistanceAnswer]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = async () => {
    const answer = isMC ? selectedAnswer : freeTextAnswer;

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

  const canSubmit = !disabled && (isMC ? !!selectedAnswer : !!freeTextAnswer.trim());

  return (
    <div
      style={{
        background: 'var(--surface)',
        borderRadius: 'var(--radius)',
        border: '1px solid var(--faint)',
        boxShadow: 'var(--shadow)',
        padding: 32,
      }}
    >
      <div
        style={{
          font: '600 11px/1 var(--font-mono)',
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
          color: 'var(--muted)',
          marginBottom: 16,
        }}
      >
        Question {question.question_id}
      </div>

      {display.inlineContext && (
        <div
          style={{
            background: 'var(--surface-2)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            padding: '14px 18px',
            marginBottom: 22,
          }}
        >
          <div
            style={{
              font: '600 10px/1 var(--font-mono)',
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              color: 'var(--muted)',
              marginBottom: 8,
            }}
          >
            Context
          </div>
          <p
            style={{
              fontSize: 15,
              lineHeight: 1.55,
              color: 'var(--ink)',
              whiteSpace: 'pre-wrap',
              margin: 0,
            }}
          >
            {display.inlineContext}
          </p>
        </div>
      )}

      {display.documentText && documentUrl && (
        <a
          href={documentUrl}
          target="_blank"
          rel="noreferrer"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            padding: '12px 18px',
            background: 'var(--accent)',
            border: '1px solid var(--accent)',
            borderRadius: 'var(--radius-sm)',
            color: 'var(--accent-ink)',
            fontSize: 15,
            fontWeight: 600,
            textDecoration: 'none',
            marginBottom: 20,
          }}
        >
          Open document in new tab ↗
        </a>
      )}

      {humanPromptPrefix && humanPromptPrefix.trim() && (
        <PromptFraming text={humanPromptPrefix} style={{ marginBottom: 20 }} />
      )}

      <p
        style={{
          fontFamily: 'var(--font-head)',
          fontSize: 22,
          lineHeight: 1.35,
          letterSpacing: '-0.005em',
          color: 'var(--ink)',
          marginTop: 0,
          marginBottom: 24,
          whiteSpace: 'pre-wrap',
        }}
      >
        {display.questionText}
      </p>

      {humanPromptSuffix && humanPromptSuffix.trim() && (
        <PromptFraming text={humanPromptSuffix} style={{ marginTop: -4, marginBottom: 24 }} />
      )}

      {assistanceActive && assistanceAnswer == null && (
        <p style={{ fontSize: 14, color: 'var(--muted)', fontStyle: 'italic', marginBottom: 16 }}>
          Answer the questions on the right to help inform your response.
        </p>
      )}

      {!assistanceActive && (isMC ? (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 10,
            marginBottom: 26,
          }}
        >
          {options.map((option, index) => {
            const selected = selectedAnswer === option;
            return (
              <button
                key={index}
                type="button"
                aria-pressed={selected}
                onClick={() => setSelectedAnswer(option)}
                onMouseEnter={(e) => {
                  if (!selected) e.currentTarget.style.background = 'var(--surface-2)';
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = 'var(--surface)';
                }}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 12,
                  padding: '14px 18px',
                  background: selected ? 'var(--accent-soft)' : 'var(--surface)',
                  border: `1px solid ${selected ? 'var(--accent)' : 'var(--faint)'}`,
                  borderRadius: 'var(--radius-sm)',
                  cursor: 'pointer',
                  fontSize: 15,
                  textAlign: 'left',
                  color: 'var(--ink)',
                  font: '500 15px/1.5 var(--font-body)',
                  transition: 'background 0.12s, border-color 0.12s',
                }}
              >
                <span
                  aria-hidden
                  style={{
                    width: 18,
                    height: 18,
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
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: '50%',
                        background: 'var(--accent)',
                      }}
                    />
                  )}
                </span>
                <span style={{ flex: 1 }}>{option}</span>
              </button>
            );
          })}
        </div>
      ) : (
        <textarea
          value={freeTextAnswer}
          onChange={(e) => setFreeTextAnswer(e.target.value)}
          placeholder="Type your answer here..."
          rows={4}
          style={{ ...textareaStyle, marginBottom: 26 }}
        />
      ))}

      {!assistanceActive && (
        <div
          style={{
            marginBottom: 24,
            padding: '18px 20px',
            background: 'var(--surface-2)',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'baseline',
              marginBottom: 14,
            }}
          >
            <span
              style={{
                font: '600 10px/1 var(--font-mono)',
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
              }}
            >
              Confidence
            </span>
            <span
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: 'var(--accent-soft-ink)',
              }}
            >
              {CONFIDENCE_LABELS[confidence - 1]}
            </span>
          </div>
          <input
            type="range"
            min="1"
            max="5"
            value={confidence}
            onChange={(e) => setConfidence(parseInt(e.target.value))}
            style={{
              width: '100%',
              height: 6,
              borderRadius: 4,
              appearance: 'none',
              background: 'var(--faint)',
              outline: 'none',
              cursor: 'pointer',
            }}
          />
          <div
            style={{
              position: 'relative',
              height: 14,
              marginTop: 10,
              fontSize: 11,
            }}
          >
            {CONFIDENCE_LABELS.map((label, i) => {
              const pct = (i / (CONFIDENCE_LABELS.length - 1)) * 100;
              const isFirst = i === 0;
              const isLast = i === CONFIDENCE_LABELS.length - 1;
              const isActive = confidence === i + 1;
              return (
                <span
                  key={label}
                  style={{
                    position: 'absolute',
                    whiteSpace: 'nowrap',
                    left: `${pct}%`,
                    transform: isFirst ? 'none' : isLast ? 'translateX(-100%)' : 'translateX(-50%)',
                    fontWeight: isActive ? 700 : 500,
                    color: isActive ? 'var(--accent-soft-ink)' : 'var(--muted)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10,
                    letterSpacing: '0.04em',
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
          type="button"
          onClick={handleSubmit}
          disabled={submitting || !canSubmit}
          style={{
            ...primaryButton,
            width: '100%',
            padding: '14px 22px',
            fontSize: 15,
            background: canSubmit ? 'var(--accent)' : 'var(--faint)',
            color: canSubmit ? 'var(--accent-ink)' : 'var(--muted)',
            cursor: canSubmit ? 'pointer' : 'not-allowed',
          }}
        >
          {submitting ? 'Submitting…' : 'Submit answer'}
        </button>
      )}
    </div>
  );
}

export default QuestionCard;
