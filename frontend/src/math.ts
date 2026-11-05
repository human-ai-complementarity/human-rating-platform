// Math rendering for experiments with `render_math` on: TeX between $…$,
// $$…$$, \(…\) or \[…\] in plain text is typeset with KaTeX as MathML, which
// browsers lay out natively, so neither the card nor the standalone document
// tab needs KaTeX's stylesheet or fonts. The backend keeps `render_math` and
// `is_markdown` exclusive.
import { useEffect, useState } from 'react';

export type MathSegment =
  | { kind: 'text'; text: string }
  | { kind: 'math'; tex: string; display: boolean };

export interface MathRenderer {
  toHtml: (tex: string, display: boolean) => string;
}

const isSpace = (ch: string | undefined) => ch !== undefined && /\s/.test(ch);
const isDigit = (ch: string | undefined) => ch !== undefined && ch >= '0' && ch <= '9';

// The closing $ of an inline $…$ opened just before `start`, or -1. Pandoc's
// rule, so prices read as text: the opening $ is not followed by a space, the
// closing $ is not preceded by one or followed by a digit. "$5 and $10" and
// "$100, bringing your rent to $" stay text; "$R$" and "$50~MeV$" are math.
// Inline math never spans a line break or runs into a $$.
function inlineMathEnd(text: string, start: number): number {
  if (start >= text.length || isSpace(text[start])) return -1;
  for (let j = start; j < text.length; j++) {
    const ch = text[j];
    if (ch === '\n') return -1;
    if (ch === '\\') {
      j++;
      continue;
    }
    if (ch !== '$') continue;
    if (text[j + 1] === '$') return -1;
    if (j > start && !isSpace(text[j - 1]) && !isDigit(text[j + 1])) return j;
  }
  return -1;
}

/**
 * Split plain text into text and math segments. Text is kept as written except
 * that \$ reads as a literal $; an unmatched delimiter stays text.
 */
export function splitMath(text: string): MathSegment[] {
  const segments: MathSegment[] = [];
  let buffer = '';
  const pushMath = (tex: string, display: boolean) => {
    if (buffer) segments.push({ kind: 'text', text: buffer });
    buffer = '';
    segments.push({ kind: 'math', tex, display });
  };

  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    const next = text[i + 1];
    if (ch === '\\' && next === '$') {
      buffer += '$';
      i += 2;
      continue;
    }
    if (ch === '\\' && (next === '(' || next === '[')) {
      const close = next === '(' ? '\\)' : '\\]';
      const end = text.indexOf(close, i + 2);
      if (end !== -1 && text.slice(i + 2, end).trim()) {
        pushMath(text.slice(i + 2, end), next === '[');
        i = end + 2;
        continue;
      }
    }
    if (ch === '$' && next === '$') {
      const end = text.indexOf('$$', i + 2);
      if (end !== -1 && text.slice(i + 2, end).trim()) {
        pushMath(text.slice(i + 2, end), true);
        i = end + 2;
        continue;
      }
      buffer += '$$';
      i += 2;
      continue;
    }
    if (ch === '$') {
      const end = inlineMathEnd(text, i + 1);
      if (end !== -1) {
        pushMath(text.slice(i + 1, end), false);
        i = end + 1;
        continue;
      }
    }
    buffer += ch;
    i++;
  }
  if (buffer) segments.push({ kind: 'text', text: buffer });
  return segments;
}

let loaded: MathRenderer | null = null;
let loading: Promise<MathRenderer> | null = null;

/** KaTeX, fetched only for math experiments and kept once it has loaded. */
function loadMathRenderer(): Promise<MathRenderer> {
  loading ??= import('katex').then(
    (katex) => {
      // throwOnError off: a typo in one formula renders that formula in red,
      // not a blank card.
      const options = { throwOnError: false, output: 'mathml' as const };
      loaded = {
        toHtml: (tex, display) =>
          katex.default.renderToString(tex, { ...options, displayMode: display }),
      };
      return loaded;
    },
    (error: unknown) => {
      // Not cached: the next question tries again.
      loading = null;
      throw error;
    },
  );
  return loading;
}

export interface MathState {
  // False only while KaTeX is loading, so the card can wait instead of showing
  // raw TeX and reflowing once it arrives.
  ready: boolean;
  // Null when math is off, or when KaTeX failed to load: the card then shows
  // the text as written.
  renderer: MathRenderer | null;
}

/**
 * KaTeX for a question with `render_math` on. A failed load leaves this
 * question as plain text and is retried when `retryKey` (the question) changes.
 */
export function useMathRenderer(enabled: boolean, retryKey: unknown): MathState {
  const [renderer, setRenderer] = useState<MathRenderer | null>(loaded);
  const [failedFor, setFailedFor] = useState<unknown>(undefined);
  const failed = failedFor !== undefined && failedFor === retryKey;

  useEffect(() => {
    if (!enabled || renderer || failed) return;
    let cancelled = false;
    loadMathRenderer().then(
      (ready) => {
        if (!cancelled) setRenderer(ready);
      },
      (error: unknown) => {
        console.warn('KaTeX failed to load; showing TeX as written.', error);
        if (!cancelled) setFailedFor(retryKey);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [enabled, renderer, failed, retryKey]);

  if (!enabled) return { ready: true, renderer: null };
  return { ready: renderer !== null || failed, renderer };
}
