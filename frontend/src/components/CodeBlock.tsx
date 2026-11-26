import type { ReactNode } from 'react';
import type { ExtraProps } from 'react-markdown';

type HastNode = { type: string; value?: string; children?: HastNode[] };

function textOf(node: HastNode): string {
  if (node.type === 'text') return node.value ?? '';
  return (node.children ?? []).map(textOf).join('');
}

// A fenced code block with a line-number gutter. The block scrolls sideways
// and never wraps, so the gutter stays level with the code line for line. The
// numbers are drawn by CSS from `data-lines` (a pseudo-element), which keeps
// them out of a selection: a rater who copies the code gets only the code.
export function CodeBlock({ node, children }: { children?: ReactNode } & ExtraProps) {
  // The fence's closing line break is not a line of its own.
  const code = node ? textOf(node as HastNode).replace(/\n$/, '') : '';
  const lineCount = code.split('\n').length;
  const numbers = Array.from({ length: lineCount }, (_, i) => i + 1).join('\n');
  return (
    <div className="code-block">
      <div className="code-gutter" aria-hidden="true" data-lines={numbers} />
      <pre>{children}</pre>
    </div>
  );
}
