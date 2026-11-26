import { Children, isValidElement, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
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

// Module-level so each render passes the same component types; per-render
// closures would remount every heading and leave the scroll-spy observing
// detached nodes.
const heading =
  (Tag: 'h1' | 'h2') =>
  ({ children }: { children?: ReactNode }) => (
    <Tag id={slugify(textOf(children))} className="docs-anchor">
      {children}
    </Tag>
  );

const MARKDOWN_COMPONENTS: Components = {
  h1: heading('h1'),
  h2: heading('h2'),
  // Wide tables scroll inside their own box instead of the page.
  table: ({ children }) => (
    <div className="docs-tablebox">
      <table>{children}</table>
    </div>
  ),
};

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

  // Narrow screens: the contents list folds into a sticky bar above the guide.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [menuOpen]);

  const jumpTo = (id: string) => {
    setMenuOpen(false);
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setActiveId(id);
  };

  const tocList = (
    <ol className="docs-toc-list">
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
  );
  const activeText = toc.find((entry) => entry.id === activeId)?.text ?? '';

  return (
    <div className="admin-page">
      <div className="docs-layout">
        <nav className="docs-toc" aria-label="On this page">
          {tocList}
        </nav>

        <div className="docs-toc-sm" ref={menuRef}>
          <button
            type="button"
            className="docs-toc-sm-toggle"
            aria-expanded={menuOpen}
            aria-controls="docs-toc-sm-menu"
            onClick={() => setMenuOpen((v) => !v)}
          >
            <span className="docs-toc-sm-label">On this page</span>
            <span className="docs-toc-sm-current">{activeText}</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
          {menuOpen && (
            <nav id="docs-toc-sm-menu" className="docs-toc-sm-menu" aria-label="On this page">
              {tocList}
            </nav>
          )}
        </div>

        <article className="docs-article">
          <div className="admin-docs markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
              {adminGuide}
            </ReactMarkdown>
          </div>
        </article>
      </div>
    </div>
  );
}

export default AdminDocs;
