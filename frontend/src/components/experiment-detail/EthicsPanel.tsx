import { useEffect, useRef, useState } from 'react';
import { api } from '../../api';
import type { Experiment, StudyFlag, TermsPreview, TermsStatus } from '../../types';
import { STUDY_FLAGS, STUDY_FLAG_EFFECTS, flagsDebrief, flagsWarn } from '../../types';
import { Banner, Field, SectionCard, primaryButton, secondaryButton, textareaStyle } from './ui';

const DOCUMENT_LABELS: Record<string, string> = {
  'consent/standard': 'standard consent',
  'consent/warned': 'warned consent',
  debrief: 'debrief',
};

/**
 * Study flags, content warning details, and the statements they imply.
 *
 * The statements themselves live in the terms source (a GCS folder in
 * production) and are read live here; the platform never edits them. Each
 * flag maps independently onto Prolific's settings, a prescreener, which
 * consent statement raters see, and whether the study ends on a debrief
 * (backend/services/terms/flags.py is the one table). The versions are
 * pinned at first publish, after which this section is read-only.
 */
export function EthicsPanel({
  experiment,
  isLocked,
  lockedHint,
  onSaved,
}: {
  experiment: Experiment;
  isLocked: boolean;
  lockedHint: string;
  onSaved: () => void;
}) {
  const [flags, setFlags] = useState<StudyFlag[]>(experiment.study_flags);
  const [details, setDetails] = useState(experiment.content_warning_details ?? '');
  const [terms, setTerms] = useState<TermsStatus | null>(null);
  const [termsError, setTermsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [preview, setPreview] = useState<TermsPreview | null>(null);
  const [previewKind, setPreviewKind] = useState<'consent' | 'debrief' | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  // Re-sync when the experiment refreshes underneath us (after a save, or a
  // publish that pinned versions) unless the admin is mid-edit. The flag is
  // a ref, not a dependency: clearing it after a save must not re-run this
  // against the stale prop before the refreshed experiment has arrived.
  const dirtyRef = useRef(false);
  const setDirty = (value: boolean) => {
    dirtyRef.current = value;
  };
  const flagsKey = experiment.study_flags.join(',');
  useEffect(() => {
    if (dirtyRef.current) return;
    setFlags(experiment.study_flags);
    setDetails(experiment.content_warning_details ?? '');
  }, [flagsKey, experiment.study_flags, experiment.content_warning_details]);

  useEffect(() => {
    let cancelled = false;
    api
      .getTermsStatus()
      .then((status) => {
        if (cancelled) return;
        setTerms(status);
        setTermsError(status.ok ? null : status.error ?? 'The terms source could not be read.');
      })
      .catch((err) => {
        if (cancelled) return;
        setTermsError(err instanceof Error ? err.message : 'The terms source could not be read.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const warned = flagsWarn(flags);
  const debrief = flagsDebrief(flags);
  const detailsMissing = warned && details.trim() === '';
  const pinned = experiment.consent_statement_ref !== null;
  const sourceUnset = terms !== null && !terms.ok && /not set/i.test(terms.error ?? '');

  const toggleFlag = (flag: StudyFlag, on: boolean) => {
    setDirty(true);
    setSaved(false);
    setFlags((current) =>
      on
        ? STUDY_FLAGS.filter((f) => f === flag || current.includes(f))
        : current.filter((f) => f !== flag),
    );
  };

  const handleSave = async () => {
    setSaveError(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.updateExperiment(experiment.id, {
        assistance_method: experiment.assistance_method,
        assistance_params: experiment.assistance_params ?? null,
        study_flags: flags,
        // Details only mean something for a warned study; clearing the flags
        // clears them too, so a later duplicate does not carry stale text.
        content_warning_details: warned ? details : '',
      });
      setDirty(false);
      setSaved(true);
      onSaved();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save consent settings');
    } finally {
      setSaving(false);
    }
  };

  const openPreview = async (kind: 'consent' | 'debrief') => {
    setPreviewError(null);
    try {
      // The same selection a save would send, so the preview and the saved
      // result never differ: details only travel with a warning flag.
      const result = await api.getTermsPreview(experiment.id, {
        study_flags: flags,
        content_warning_details: warned ? details : '',
      });
      setPreview(result);
      setPreviewKind(kind);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Could not render the preview');
    }
  };

  const consentDocument = warned ? 'consent/warned' : 'consent/standard';
  const versionOf = (document: string) => terms?.ok ? terms.documents[document] : undefined;

  return (
    <SectionCard header="Consent and content">
      <p style={{ fontSize: 13.5, color: 'var(--muted)', margin: '0 0 16px', lineHeight: 1.55 }}>
        Raters agree to a consent statement before they see anything about the study. Statements
        come from the team's terms source and are versioned there. Tick what this study exposes
        raters to: sensitive or explicit content makes Prolific show a content warning, adds the
        harmful-content prescreener, and switches to the consent statement written for it;
        deception adds Prolific's deception prescreener and ends the study on a debrief.
      </p>

      {isLocked && <Banner tone="warn">{lockedHint}</Banner>}
      {termsError && (
        <Banner tone="warn">
          {sourceUnset
            ? 'No terms source is configured (TERMS__SOURCE_URL). Raters see a placeholder consent statement; a study with any flag set cannot be published until it is.'
            : `The terms source could not be read: ${termsError}. You can still save; publishing will be refused until it is reachable.`}
        </Banner>
      )}
      {pinned && (
        <div
          data-testid="ethics-pinned"
          style={{ fontSize: 13, color: 'var(--muted)', margin: '0 0 14px' }}
        >
          Pinned at publish: consent {experiment.consent_statement_ref}
          {experiment.debrief_statement_ref ? `, debrief ${experiment.debrief_statement_ref}` : ''}.
          Every rater in this study sees these versions.
        </div>
      )}

      <Field label="Study flags" disabled={isLocked} hint="Any combination. Leave all unticked for an ordinary study.">
        <div
          data-testid="ethics-flags"
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 9,
            padding: '10px 12px',
            border: '1px solid var(--faint)',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--surface-2)',
            opacity: isLocked ? 0.75 : 1,
          }}
        >
          {STUDY_FLAGS.map((flag) => (
            <label
              key={flag}
              style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: isLocked ? 'not-allowed' : 'pointer', fontSize: 13.5 }}
            >
              <input
                type="checkbox"
                data-testid={`ethics-flag-${flag}`}
                checked={flags.includes(flag)}
                disabled={isLocked}
                onChange={(e) => toggleFlag(flag, e.target.checked)}
                style={{ width: 16, height: 16, flex: '0 0 auto', margin: '2px 0 0 0', padding: 0 }}
              />
              <span style={{ lineHeight: 1.4 }}>
                <strong>{STUDY_FLAG_EFFECTS[flag].label}</strong>
                <span style={{ color: 'var(--muted)' }}> — {STUDY_FLAG_EFFECTS[flag].hint}</span>
              </span>
            </label>
          ))}
        </div>
      </Field>

      {!pinned && (
        <div
          data-testid="ethics-documents"
          style={{ fontSize: 13, color: 'var(--muted)', margin: '-6px 0 14px' }}
        >
          Statements: {DOCUMENT_LABELS[consentDocument]}
          {versionOf(consentDocument) ? ` v${versionOf(consentDocument)}` : ''}
          {debrief ? `, ${DOCUMENT_LABELS.debrief}${versionOf('debrief') ? ` v${versionOf('debrief')}` : ''}` : ''}
          , the current versions in the terms source. Publishing pins them.
        </div>
      )}

      {warned && (
        <Field
          id="ethics-details"
          label="Content warning details"
          disabled={isLocked}
          hint="Required. Sent to Prolific, shown at the top of the study description, and merged into the consent statement. Say what kind of material raters will see."
        >
          <textarea
            id="ethics-details"
            data-testid="ethics-details"
            value={details}
            disabled={isLocked}
            placeholder="e.g. Some passages describe violence against people in detail."
            onChange={(e) => {
              setDirty(true);
              setSaved(false);
              setDetails(e.target.value);
            }}
            style={{ ...textareaStyle, minHeight: 90 }}
          />
        </Field>
      )}

      {saveError && <Banner tone="danger">{saveError}</Banner>}
      {saved && <Banner tone="ok">Consent settings saved.</Banner>}
      {previewError && <Banner tone="danger">{previewError}</Banner>}

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        <button
          type="button"
          data-testid="ethics-preview-consent"
          onClick={() => void openPreview('consent')}
          style={secondaryButton}
        >
          Preview consent
        </button>
        {debrief && (
          <button
            type="button"
            data-testid="ethics-preview-debrief"
            onClick={() => void openPreview('debrief')}
            style={secondaryButton}
          >
            Preview debrief
          </button>
        )}
        <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>
          {pinned ? 'Previews show the pinned versions.' : 'Previews use the settings selected above, saved or not.'}
        </span>
        <button
          type="button"
          data-testid="ethics-save"
          onClick={() => void handleSave()}
          disabled={saving || isLocked || detailsMissing}
          style={{
            ...primaryButton,
            marginLeft: 'auto',
            opacity: saving || isLocked || detailsMissing ? 0.6 : 1,
            cursor: saving || isLocked || detailsMissing ? 'not-allowed' : 'pointer',
          }}
        >
          {saving ? 'Saving…' : 'Save consent settings'}
        </button>
      </div>

      {preview && previewKind && (
        <div
          role="dialog"
          aria-modal="true"
          data-testid="ethics-preview-dialog"
          onClick={() => setPreview(null)}
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.45)',
            zIndex: 60,
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'center',
            padding: '40px 16px',
            overflowY: 'auto',
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--surface)',
              borderRadius: 'var(--radius)',
              boxShadow: 'var(--shadow)',
              maxWidth: 720,
              width: '100%',
              padding: '28px 32px',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 12,
                marginBottom: 16,
                font: '600 11px/1 var(--font-mono)',
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
              }}
            >
              {previewKind === 'consent' ? 'Consent' : 'Debrief'} ·{' '}
              {previewKind === 'consent' ? preview.consent_ref : preview.debrief_ref ?? 'none'}
              {preview.pinned ? ' · pinned' : ' · live from source'}
              <button
                type="button"
                onClick={() => setPreview(null)}
                style={{ ...secondaryButton, marginLeft: 'auto' }}
              >
                Close
              </button>
            </div>
            {previewKind === 'debrief' && !preview.debrief_html ? (
              <p style={{ color: 'var(--muted)' }}>This experiment has no debrief screen.</p>
            ) : (
              <div
                className="rater-consent-statement"
                style={{ fontSize: 15, lineHeight: 1.65 }}
                dangerouslySetInnerHTML={{
                  __html:
                    previewKind === 'consent' ? preview.consent_html : preview.debrief_html ?? '',
                }}
              />
            )}
          </div>
        </div>
      )}
    </SectionCard>
  );
}
