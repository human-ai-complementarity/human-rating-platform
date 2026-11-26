import { primaryButton } from './experiment-detail/ui';
import StudyInstructions, { type StudyInstructionsProps } from './StudyInstructions';

interface RaterIntroProps extends StudyInstructionsProps {
  experimentName: string;
  onContinue: () => void;
}

/**
 * The rater's first screen after landing from Prolific — an editorial
 * splash with the study name, description, and (when applicable) an
 * "How this study works" callout for AI-assisted rating methods.
 *
 * Serif headline, generous whitespace, single primary CTA. Matches the
 * Fieldbook aesthetic used across the researcher-facing surfaces so the
 * study feels part of a coherent product rather than a bolt-on form.
 * The same instructions stay available from the rating screen afterwards
 * (StudyInstructionsDialog).
 */
function RaterIntro({ experimentName, onContinue, ...instructions }: RaterIntroProps) {
  return (
    <div
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--faint)',
        borderRadius: 'var(--radius)',
        boxShadow: 'var(--shadow)',
        padding: 'clamp(24px, 6vw, 44px) var(--rater-card-pad) clamp(24px, 6vw, 40px)',
      }}
    >
      <div
        style={{
          font: '600 var(--text-label)/1 var(--font-mono)',
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          color: 'var(--accent-soft-ink)',
        }}
      >
        Rating study
      </div>

      <h1
        style={{
          fontFamily: 'var(--font-head)',
          fontWeight: 600,
          fontSize: 'var(--text-title)',
          lineHeight: 'var(--leading-heading)',
          letterSpacing: '-0.012em',
          color: 'var(--ink)',
          margin: '14px 0 24px',
        }}
      >
        {experimentName}
      </h1>

      <StudyInstructions {...instructions} />

      <button
        type="button"
        onClick={onContinue}
        style={{ ...primaryButton, width: '100%', padding: '13px 22px', fontSize: 'var(--text-body)' }}
      >
        Continue
      </button>
    </div>
  );
}

export default RaterIntro;
