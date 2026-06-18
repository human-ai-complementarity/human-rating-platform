interface RaterIntroProps {
  experimentName: string;
  description: string | null;
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
  description: {
    fontSize: '16px',
    lineHeight: 1.6,
    color: '#444',
    whiteSpace: 'pre-wrap' as const,
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
  description,
  assistanceInstructions,
  onContinue,
}: RaterIntroProps) {
  return (
    <div style={styles.card}>
      <h1 style={styles.title}>{experimentName}</h1>
      {description && (
        <>
          <div style={styles.sectionLabel}>Description</div>
          <div style={styles.description}>{description}</div>
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
