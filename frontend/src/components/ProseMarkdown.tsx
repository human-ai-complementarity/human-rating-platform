import type { CSSProperties } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

// Open external links in a new tab to avoid losing in-progress work.
export const MarkdownLink: Components['a'] = ({ href, title, children }) => {
  const inPage = href?.startsWith('#') === true;
  return (
    <a
      href={href}
      title={title}
      target={inPage ? undefined : '_blank'}
      rel={inPage ? undefined : 'noopener noreferrer'}
    >
      {children}
    </a>
  );
};

// remark-breaks keeps a single newline as a line break, which is how
// researchers write these texts.
const PLUGINS = [remarkGfm, remarkBreaks];
const COMPONENTS: Components = {
  a: MarkdownLink,
  // A wide table scrolls inside the card instead of widening the page.
  table: ({ children }) => (
    <div className="question-markdown-table-scroll">
      <table>{children}</table>
    </div>
  ),
};

interface ProseMarkdownProps {
  source: string;
  className?: string;
  style?: CSSProperties;
}

/**
 * Researcher-written prose (consent and debrief statements, the study
 * description) rendered from Markdown. react-markdown shows raw HTML as
 * literal text and strips unsafe link protocols, so the source needs no
 * sanitising. Shares the Markdown question's rules (quotes, tables, wrapping).
 */
function ProseMarkdown({ source, className, style }: ProseMarkdownProps) {
  return (
    <div className={`question-markdown markdown ${className ?? ''}`.trim()} style={style}>
      <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {source}
      </ReactMarkdown>
    </div>
  );
}

export default ProseMarkdown;
