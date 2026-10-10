import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api';
import type { ContentWarning, Experiment, TermsBundle, TermsPreview, TermsStatus } from '../../types';
import {
  Banner,
  Field,
  SectionCard,
  inputStyle,
  primaryButton,
  secondaryButton,
  textareaStyle,
} from './ui';

const WARNING_LABELS: Record<ContentWarning, string> = {
  none: 'None',
  sensitive: 'Sensitive content',
  explicit: 'Explicit content',
};

/**
 * Consent, content warning and debrief for one experiment.
 *
 * The statements themselves live in the terms source (a GCS folder in
 * production) and are read live here; the platform never edits them. An
 * experiment picks a bundle, optionally declares a Prolific content warning
 * with details, and pins the statement versions at first publish, after
 * which this section is read-only.
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
  const [warning, setWarning] = useState<ContentWarning>(experiment.content_warning);
  const [details, setDetails] = useState(experiment.content_warning_details ?? '');
  const [bundle, setBundle] = useState(experiment.terms_bundle);
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
  useEffect(() => {
    if (dirtyRef.current) return;
    setWarning(experiment.content_warning);
    setDetails(experiment.content_warning_details ?? '');
    setBundle(experiment.terms_bundle);
  }, [experiment.content_warning, experiment.content_warning_details, experiment.terms_bundle]);

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

  const bundles: TermsBundle[] = useMemo(() => terms?.bundles ?? [], [terms]);
  const compatible = useMemo(
    () => bundles.filter((b) => b.content_warnings.includes(warning)),
    [bundles, warning],
  );
  // Keep the saved bundle selectable even when the manifest can't be read,
  // so a save that only touches the details never silently changes it.
  const options =
    compatible.length > 0 || bundles.length > 0
      ? compatible
      : [{ key: bundle, label: bundle, content_warnings: [warning], consent_version: 0, debrief_version: null }];
  const bundleKnown = options.some((b) => b.key === bundle);
  // One bundle per warning level is the normal case, so the bundle is
  // resolved for the admin; the select appears only when the manifest offers
  // a real choice (a study with its own wording).
  const bundleChoice = compatible.length > 1;
  useEffect(() => {
    if (compatible.length === 0 || compatible.some((b) => b.key === bundle)) return;
    setBundle(compatible[0].key);
  }, [compatible, bundle]);

  const changeWarning = (next: ContentWarning) => {
    setDirty(true);
    setSaved(false);
    setWarning(next);
    const stillFits = bundles.find((b) => b.key === bundle)?.content_warnings.includes(next);
    if (!stillFits) {
      const first = bundles.find((b) => b.content_warnings.includes(next));
      if (first) setBundle(first.key);
    }
  };

  const detailsMissing = warning !== 'none' && details.trim() === '';

  const handleSave = async () => {
    setSaveError(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.updateExperiment(experiment.id, {
        assistance_method: experiment.assistance_method,
        assistance_params: experiment.assistance_params ?? null,
        content_warning: warning,
        content_warning_details: details,
        terms_bundle: bundle,
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
      const result = await api.getTermsPreview(experiment.id, {
        terms_bundle: bundle,
        content_warning: warning,
        content_warning_details: details,
      });
      setPreview(result);
      setPreviewKind(kind);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Could not render the preview');
    }
  };

  const pinned = experiment.consent_statement_ref !== null;
  const selectedBundle = bundles.find((b) => b.key === bundle);

  return (
    <SectionCard header="Consent and content">
      <p style={{ fontSize: 13.5, color: 'var(--muted)', margin: '0 0 16px', lineHeight: 1.55 }}>
        Raters agree to a consent statement before they see anything about the study. Statements
        come from the team's terms source and are versioned there; this experiment picks which
        bundle applies. A content warning makes Prolific show the warning on the study page,
        adds the harmful-content prescreener, switches to a bundle written for it, and adds a
        debrief screen at the end.
      </p>

      {isLocked && <Banner tone="warn">{lockedHint}</Banner>}
      {termsError && (
        <Banner tone="warn">
          The terms source could not be read: {termsError}. You can still save; publishing
          will be refused until it is reachable.
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

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: bundleChoice ? 'repeat(2, minmax(0, 380px))' : 'minmax(0, 380px)',
          gap: 18,
          opacity: isLocked ? 0.75 : 1,
        }}
      >
        <Field
          id="ethics-content-warning"
          label="Content warning"
          disabled={isLocked}
          hint="Mirrors Prolific's content-warning setting. Choose Sensitive or Explicit when raters may see disturbing material."
        >
          <select
            id="ethics-content-warning"
            data-testid="ethics-content-warning"
            value={warning}
            disabled={isLocked}
            onChange={(e) => changeWarning(e.target.value as ContentWarning)}
            style={{ ...inputStyle, maxWidth: 380, paddingRight: 36, cursor: isLocked ? 'not-allowed' : 'pointer' }}
          >
            {(Object.keys(WARNING_LABELS) as ContentWarning[]).map((key) => (
              <option key={key} value={key}>
                {WARNING_LABELS[key]}
              </option>
            ))}
          </select>
        </Field>

        {bundleChoice && (
          <Field
            id="ethics-bundle"
            label="Consent wording"
            disabled={isLocked}
            hint="This content warning has more than one set of statements; pick the one written for this study."
          >
          <select
            id="ethics-bundle"
            data-testid="ethics-bundle"
            value={bundle}
            disabled={isLocked}
            onChange={(e) => {
              setDirty(true);
              setSaved(false);
              setBundle(e.target.value);
            }}
            style={{ ...inputStyle, maxWidth: 380, paddingRight: 36, cursor: isLocked ? 'not-allowed' : 'pointer' }}
          >
            {!bundleKnown && <option value={bundle}>{bundle}</option>}
            {options.map((b) => (
              <option key={b.key} value={b.key}>
                {b.label}
              </option>
            ))}
          </select>
          </Field>
        )}
      </div>

      {!pinned && selectedBundle && (
        <div
          data-testid="ethics-bundle-resolved"
          style={{ fontSize: 13, color: 'var(--muted)', margin: '-6px 0 14px' }}
        >
          Statements: {selectedBundle.label} (consent v{selectedBundle.consent_version}
          {selectedBundle.debrief_version ? `, debrief v${selectedBundle.debrief_version}` : ''}),
          the current versions in the terms source. Publishing pins them.
        </div>
      )}

      {warning !== 'none' && (
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
        {warning !== 'none' && (
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
