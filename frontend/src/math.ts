// Math rendering for experiments with `render_math` on: TeX between $…$,
// $$…$$, \(…\) or \[…\] is typeset with KaTeX as MathML, which browsers lay
// out natively, so neither the card nor the standalone document tab needs
// KaTeX's stylesheet or fonts. Plain text is split with `splitMath`. Markdown
// (`is_markdown` on as well) goes through `maskMath` and `remarkMaskedMath`,
// which find the same formulas by the same rules.
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
  return scanMath(text, false).map((segment) =>
    segment.kind === 'text'
      ? segment
      : { kind: 'math', tex: segment.tex, display: segment.display },
  );
}

type ScannedSegment =
  | { kind: 'text'; text: string }
  // `source` is the formula as written, delimiters included.
  | { kind: 'math'; tex: string; display: boolean; source: string };

// `keepEscapes` leaves \$ as written, for Markdown to unescape itself.
function scanMath(text: string, keepEscapes: boolean): ScannedSegment[] {
  const segments: ScannedSegment[] = [];
  let buffer = '';
  // The formula runs from `from` to `to`, its delimiters `width` wide.
  const pushMath = (from: number, to: number, width: number, display: boolean) => {
    if (buffer) segments.push({ kind: 'text', text: buffer });
    buffer = '';
    segments.push({
      kind: 'math',
      tex: text.slice(from + width, to - width),
      display,
      source: text.slice(from, to),
    });
  };

  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    const next = text[i + 1];
    if (ch === '\\' && next === '$') {
      buffer += keepEscapes ? '\\$' : '$';
      i += 2;
      continue;
    }
    if (ch === '\\' && (next === '(' || next === '[')) {
      const close = next === '(' ? '\\)' : '\\]';
      const end = text.indexOf(close, i + 2);
      if (end !== -1 && text.slice(i + 2, end).trim()) {
        pushMath(i, end + 2, 2, next === '[');
        i = end + 2;
        continue;
      }
    }
    if (ch === '$' && next === '$') {
      const end = text.indexOf('$$', i + 2);
      if (end !== -1 && text.slice(i + 2, end).trim()) {
        pushMath(i, end + 2, 2, true);
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
        pushMath(i, end + 1, 1, false);
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

// Markdown with math. A Markdown parser would mangle TeX before any math
// plugin saw it: \( loses its backslash, a_1 and b_1 pair up as emphasis, a |
// splits a table cell. So each formula is swapped for an opaque token before
// parsing (`maskMath`) and swapped back in the parsed tree
// (`remarkMaskedMath`). The token is a formula's index between two
// private-use characters, which Markdown reads as an ordinary word.
const TOKEN_OPEN = '';
const TOKEN_CLOSE = '';
const TOKEN_PATTERN = /(\d+)/g;

export interface MaskedFormula {
  tex: string;
  display: boolean;
  /** The formula as written, to put back where a token landed in code. */
  source: string;
}

export interface MaskedMarkdown {
  markdown: string;
  formulas: MaskedFormula[];
}

const FENCE_PATTERN = /^ {0,3}(`{3,}|~{3,})/;

// Fenced code blocks and inline code spans, as [start, end) ranges: a $ inside
// code is code, and math never pairs across it. Indented code blocks are not
// found here; `remarkMaskedMath` restores any token that lands in one.
function codeRanges(markdown: string): Array<[number, number]> {
  const fences: Array<[number, number]> = [];
  let fence: string | null = null;
  let fenceStart = 0;
  let pos = 0;
  for (const line of markdown.split('\n')) {
    const match = FENCE_PATTERN.exec(line);
    if (match) {
      const rest = line.slice(match[0].length);
      if (fence === null) {
        // A backtick fence's info string has no backticks (that is a code span).
        if (match[1][0] !== '`' || !rest.includes('`')) {
          fence = match[1];
          fenceStart = pos;
        }
      } else if (match[1][0] === fence[0] && match[1].length >= fence.length && !rest.trim()) {
        fences.push([fenceStart, pos + line.length]);
        fence = null;
      }
    }
    pos += line.length + 1;
  }
  // An unclosed fence runs to the end of the document.
  if (fence !== null) fences.push([fenceStart, markdown.length]);

  const ranges: Array<[number, number]> = [];
  const runLength = (at: number) => {
    let end = at;
    while (markdown[end] === '`') end++;
    return end - at;
  };
  // Code spans in [from, to): a backtick run closed by the next run of the
  // same length in the same paragraph.
  const addSpans = (from: number, to: number) => {
    let i = from;
    while (i < to) {
      if (markdown[i] === '\\') {
        i += 2;
        continue;
      }
      if (markdown[i] !== '`') {
        i++;
        continue;
      }
      const length = runLength(i);
      let close = -1;
      for (let j = i + length; j < to; j++) {
        if (markdown[j] === '\n' && /^[ \t]*(\n|$)/.test(markdown.slice(j + 1, to))) break;
        if (markdown[j] !== '`') continue;
        const candidate = runLength(j);
        if (candidate === length) {
          close = j;
          break;
        }
        j += candidate - 1;
      }
      if (close === -1) {
        i += length;
      } else {
        ranges.push([i, close + length]);
        i = close + length;
      }
    }
  };
  let cursor = 0;
  for (const [start, end] of fences) {
    addSpans(cursor, start);
    ranges.push([start, end]);
    cursor = end;
  }
  addSpans(cursor, markdown.length);
  return ranges;
}

/** Swap each formula outside code for a token Markdown passes through. */
export function maskMath(markdown: string): MaskedMarkdown {
  const source = markdown.replace(/[]/g, '');
  const formulas: MaskedFormula[] = [];
  let masked = '';
  const mask = (from: number, to: number) => {
    for (const segment of scanMath(source.slice(from, to), true)) {
      if (segment.kind === 'text') {
        masked += segment.text;
      } else {
        masked += `${TOKEN_OPEN}${formulas.length}${TOKEN_CLOSE}`;
        formulas.push({ tex: segment.tex, display: segment.display, source: segment.source });
      }
    }
  };
  let cursor = 0;
  for (const [start, end] of codeRanges(source)) {
    mask(cursor, start);
    masked += source.slice(start, end);
    cursor = end;
  }
  mask(cursor, source.length);
  return { markdown: masked, formulas };
}

/** The `data-` property a formula's element carries its index in. */
export const FORMULA_PROPERTY = 'dataFormula';

// The few fields of a Markdown syntax tree node this touches.
interface TreeNode {
  type: string;
  value?: unknown;
  url?: unknown;
  title?: unknown;
  alt?: unknown;
  children?: TreeNode[];
  data?: unknown;
}

/**
 * A remark plugin for Markdown that went through `maskMath`. A token in text
 * becomes a `span` element carrying the formula's index, for the renderer to
 * typeset; a token anywhere else (code, a link target, raw HTML) becomes the
 * formula as written.
 */
export function remarkMaskedMath(formulas: MaskedFormula[]) {
  const restore = (value: string) =>
    value.replace(TOKEN_PATTERN, (token, index) => formulas[Number(index)]?.source ?? token);

  const visit = (node: TreeNode) => {
    for (const key of ['value', 'url', 'title', 'alt'] as const) {
      const field = node[key];
      if (typeof field === 'string' && !(key === 'value' && node.type === 'text')) {
        node[key] = restore(field);
      }
    }
    if (!node.children) return;
    node.children = node.children.flatMap((child): TreeNode[] => {
      if (child.type !== 'text' || typeof child.value !== 'string') {
        visit(child);
        return [child];
      }
      const parts: TreeNode[] = [];
      let last = 0;
      for (const match of child.value.matchAll(TOKEN_PATTERN)) {
        const index = Number(match[1]);
        if (!formulas[index]) continue;
        if (match.index > last) {
          parts.push({ type: 'text', value: child.value.slice(last, match.index) });
        }
        parts.push({
          type: 'formula',
          data: { hName: 'span', hProperties: { [FORMULA_PROPERTY]: String(index) } },
        });
        last = match.index + match[0].length;
      }
      if (last === 0) return [child];
      if (last < child.value.length) parts.push({ type: 'text', value: child.value.slice(last) });
      return parts;
    });
  };
  return () => (tree: unknown) => visit(tree as TreeNode);
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
