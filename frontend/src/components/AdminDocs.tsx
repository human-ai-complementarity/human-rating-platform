import { Children, isValidElement, useEffect, useMemo, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import adminGuide from '../docs/admin-guide.md?raw';

type TocEntry = { id: string; text: string; level: 1 | 2 };

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[`*_]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

function textOf(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === 'string' || typeof child === 'number') return String(child);
      if (isValidElement<{ children?: ReactNode }>(child)) return textOf(child.props.children);
      return '';
    })
    .join('');
}

// Top-level (#) headings are the guide's parts; ## headings are its sections.
// Both get anchors so the contents list can jump to them.
function buildToc(markdown: string): TocEntry[] {
  const entries: TocEntry[] = [];
  let inFence = false;
  for (const line of markdown.split('\n')) {
    if (line.startsWith('```')) inFence = !inFence;
    if (inFence) continue;
    const match = /^(#{1,2}) (.+)$/.exec(line);
    if (match) {
      const text = match[2].replace(/[`*_]/g, '').trim();
      entries.push({ id: slugify(text), text, level: match[1].length as 1 | 2 });
    }
  }
  return entries;
}

function AdminDocs() {
  const toc = useMemo(() => buildToc(adminGuide), []);
  const [activeId, setActiveId] = useState(toc[0]?.id ?? '');

  // Highlight the section whose heading most recently scrolled past the top.
  useEffect(() => {
    const headings = toc
      .map((entry) => document.getElementById(entry.id))
      .filter((el): el is HTMLElement => el !== null);
    if (headings.length === 0 || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (records) => {
        const visible = records
          .filter((r) => r.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      { rootMargin: '0px 0px -70% 0px' },
    );
    headings.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [toc]);

  const jumpTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setActiveId(id);
  };

  const heading =
    (Tag: 'h1' | 'h2') =>
    ({ children }: { children?: ReactNode }) => (
      <Tag id={slugify(textOf(children))} className="docs-anchor">
        {children}
      </Tag>
    );

  return (
    <div className="admin-page">
      <div className="docs-layout">
        <nav className="docs-toc" aria-label="On this page">
          <ol>
            {toc.map((entry) => (
              <li key={entry.id} className={entry.level === 1 ? 'docs-toc-part' : undefined}>
                <a
                  href={`#${entry.id}`}
                  aria-current={activeId === entry.id ? 'true' : undefined}
                  onClick={(e) => {
                    e.preventDefault();
                    jumpTo(entry.id);
                  }}
                >
                  {entry.text}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        <label className="docs-toc-sm">
          <span>On this page</span>
          <select value={activeId} onChange={(e) => jumpTo(e.target.value)}>
            {toc.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.level === 2 ? `  ${entry.text}` : entry.text}
              </option>
            ))}
          </select>
        </label>

        <article className="docs-article">
          <div className="admin-docs markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: heading('h1'),
                h2: heading('h2'),
                // Wide tables scroll inside their own box instead of the page.
                table: ({ children }) => (
                  <div className="docs-tablebox">
                    <table>{children}</table>
                  </div>
                ),
              }}
            >
              {adminGuide}
            </ReactMarkdown>
          </div>
        </article>
      </div>
    </div>
  );
}

export default AdminDocs;
