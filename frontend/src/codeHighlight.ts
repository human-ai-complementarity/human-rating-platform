// Syntax highlighting for fenced code in Markdown questions. The highlighter
// and its grammars are fetched the first time a question with a fence renders,
// like KaTeX in `math.ts`, so admin pages and questions without code never
// load them.
import { useEffect, useState } from 'react';
import type { Options } from 'react-markdown';

export type CodePlugins = NonNullable<Options['rehypePlugins']>;

// The opening line of a fenced code block.
const FENCE_PATTERN = /^ {0,3}(`{3,}|~{3,})/m;

export function hasCodeFence(markdown: string): boolean {
  return FENCE_PATTERN.test(markdown);
}

let loaded: CodePlugins | null = null;
let loading: Promise<CodePlugins> | null = null;

export function loadCodeHighlighter(): Promise<CodePlugins> {
  loading ??= import('rehype-highlight').then(
    (module) => {
      // Colours a block by the language on its fence (```python). A fence with
      // no language, or one the highlighter does not know, stays plain.
      loaded = [module.default];
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

/**
 * The rehype plugins that colour code, or null while they load, when the load
 * failed, or when `enabled` is false. Code renders plain, still numbered, in
 * the meantime.
 */
export function useCodeHighlighter(enabled: boolean): CodePlugins | null {
  const [plugins, setPlugins] = useState<CodePlugins | null>(loaded);

  useEffect(() => {
    if (!enabled || plugins) return;
    let cancelled = false;
    loadCodeHighlighter().then(
      (ready) => {
        if (!cancelled) setPlugins(ready);
      },
      (error: unknown) => {
        console.warn('The code highlighter failed to load; showing code uncoloured.', error);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [enabled, plugins]);

  return enabled ? plugins : null;
}
