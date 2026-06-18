interface RaterIntroProps {
  experimentName: string;
  // Pre-rendered HTML from the server (markdown converted via to_prolific_html).
  // Rendered with dangerouslySetInnerHTML — safe because the converter only
  // emits a whitelisted set of Prolific-allowed tags with all input escaped.
  descriptionHtml: string | null;
  assistanceInstructions: string | null;
  onContinue: () => void;
}

const styles = {
  card: {
    background: '#fff',
    borderRadius: '12px',
    border: '1px solid #e0e0e0',
    padding: '40px',
  },
  title: {
    fontSize: '24px',
    fontWeight: 600,
    color: '#333',
    margin: '0 0 20px 0',
  },
  sectionLabel: {
    fontSize: '13px',
    fontWeight: 600,
    color: '#4a90d9',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
    marginBottom: '8px',
  },
  // Hosts converter-emitted HTML (<p>, <h1>, <ul>, etc.). Default whitespace
  // handling — the converter wraps each line in its own block element so we
  // don't need pre-wrap.
  description: {
    fontSize: '16px',
    lineHeight: 1.6,
    color: '#444',
    marginBottom: '24px',
  },
  methodBox: {
    background: '#f0f4f8',
    border: '1px solid #d0dae3',
    borderRadius: '8px',
    padding: '16px 20px',
    marginBottom: '28px',
  },
  methodLabel: {
    fontSize: '13px',
    fontWeight: 600,
    color: '#4a90d9',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
    marginBottom: '8px',
  },
  methodText: {
    fontSize: '15px',
    lineHeight: 1.6,
    color: '#333',
    whiteSpace: 'pre-wrap' as const,
  },
  continueButton: {
    width: '100%',
    padding: '14px',
    background: '#4a90d9',
    color: '#fff',
    border: 'none',
    borderRadius: '8px',
    fontSize: '16px',
    fontWeight: 500,
    cursor: 'pointer',
  },
};

function RaterIntro({
  experimentName,
  descriptionHtml,
  assistanceInstructions,
  onContinue,
}: RaterIntroProps) {
  return (
    <div style={styles.card}>
      <h1 style={styles.title}>{experimentName}</h1>
      {descriptionHtml && (
        <>
          <div style={styles.sectionLabel}>Description</div>
          <div
            style={styles.description}
            dangerouslySetInnerHTML={{ __html: descriptionHtml }}
          />
        </>
      )}
      {assistanceInstructions && (
        <div style={styles.methodBox}>
          <div style={styles.methodLabel}>How this study works</div>
          <div style={styles.methodText}>{assistanceInstructions}</div>
        </div>
      )}
      <button type="button" style={styles.continueButton} onClick={onContinue}>
        Continue
      </button>
    </div>
  );
}

export default RaterIntro;
