import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import adminGuide from '../docs/admin-guide.md?raw';

function AdminDocs() {
  return (
    <div className="admin-page">
      <div
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--faint)',
          borderRadius: 'var(--radius)',
          boxShadow: 'var(--shadow)',
          padding: '36px 44px',
        }}
      >
        <div className="admin-docs">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{adminGuide}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}

export default AdminDocs;
