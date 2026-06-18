import { useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import adminGuide from '../docs/admin-guide.md?raw';

function AdminDocs() {
  const navigate = useNavigate();

  const styles = {
    container: {
      maxWidth: '820px',
      margin: '0 auto',
      padding: '24px',
    },
    backLink: {
      display: 'inline-block',
      marginBottom: '16px',
      color: '#4a90d9',
      fontSize: '14px',
      cursor: 'pointer',
      background: 'none',
      border: 'none',
      padding: 0,
    },
    card: {
      background: '#fff',
      borderRadius: '8px',
      border: '1px solid #e0e0e0',
      padding: '32px 40px',
    },
  };

  return (
    <div style={styles.container}>
      <button
        type="button"
        style={styles.backLink}
        onClick={() => navigate('/admin')}
      >
        ← Back to experiments
      </button>
      <div style={styles.card}>
        <div className="admin-docs">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{adminGuide}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}

export default AdminDocs;
