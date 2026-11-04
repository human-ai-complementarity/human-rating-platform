// Math rendering for experiments with `render_math` on: TeX between $…$,
// $$…$$, \(…\) or \[…\] is typeset with KaTeX as MathML, which browsers lay
// out natively, so neither the card nor the standalone document tab needs
// KaTeX's stylesheet or fonts.
import { useEffect, useState } from 'react';
import type { Options as MarkdownOptions } from 'react-markdown';

export type MathSegment =
  | { kind: 'text'; text: string }
  | { kind: 'math'; tex: string; display: boolean };

export interface MathRenderer {
  toHtml: (tex: string, display: boolean) => string;
  // For the Markdown path: remark-math parses the delimiters before Markdown
  // can read TeX's _ and \ as emphasis and escapes.
  remarkPlugins: NonNullable<MarkdownOptions['remarkPlugins']>;
  rehypePlugins: NonNullable<MarkdownOptions['rehypePlugins']>;
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

let loading: Promise<MathRenderer> | null = null;

/** KaTeX and the Markdown plugins, fetched once and only for math experiments. */
export function loadMathRenderer(): Promise<MathRenderer> {
  loading ??= Promise.all([import('katex'), import('remark-math'), import('rehype-katex')]).then(
    ([katex, remarkMath, rehypeKatex]) => {
      // throwOnError off: a typo in one formula renders that formula in red,
      // not a blank card.
      const options = { throwOnError: false, output: 'mathml' as const };
      return {
        toHtml: (tex, display) =>
          katex.default.renderToString(tex, { ...options, displayMode: display }),
        remarkPlugins: [remarkMath.default],
        rehypePlugins: [[rehypeKatex.default, options]],
      };
    },
  );
  return loading;
}

/** The renderer once loaded when `enabled`; null while loading or when off. */
export function useMathRenderer(enabled: boolean): MathRenderer | null {
  const [renderer, setRenderer] = useState<MathRenderer | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void loadMathRenderer().then((loaded) => {
      if (!cancelled) setRenderer(loaded);
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return enabled ? renderer : null;
}
