import { useState } from 'react';
import type { DatasetMetaField, Experiment, StudyFlag, Upload } from '../../types';
import { STUDY_FLAG_EFFECTS, flagsWarn } from '../../types';
import { OutsideToolsPolicy } from '../StudyInstructions';
import { EthicsPanel } from './EthicsPanel';
import { Banner, Field, ToggleSwitch, inputStyle, primaryButton, textareaStyle } from './ui';

/** Everything the Instructions & settings step edits, saved in one request. */
export interface SettingsForm {
  description: string;
  system_prompt: string;
  human_prompt_prefix: string;
  human_prompt_suffix: string;
  prolific_pool: string;
  is_markdown: boolean;
  render_math: boolean;
  block_copy_paste: boolean;
  forbid_outside_tools: boolean;
  study_flags: StudyFlag[];
  content_warning_details: string;
}

const TEXT_FIELDS: DatasetMetaField[] = [
  'description',
  'system_prompt',
  'human_prompt_prefix',
  'human_prompt_suffix',
  'prolific_pool',
];
const FLAG_FIELDS = ['is_markdown', 'render_math', 'block_copy_paste', 'forbid_outside_tools'] as const;

export function settingsFormFrom(experiment: Experiment): SettingsForm {
  return {
    description: experiment.description ?? '',
    system_prompt: experiment.system_prompt ?? '',
    human_prompt_prefix: experiment.human_prompt_prefix ?? '',
    human_prompt_suffix: experiment.human_prompt_suffix ?? '',
    prolific_pool: experiment.prolific_pool ?? '',
    is_markdown: experiment.is_markdown,
    render_math: experiment.render_math,
    block_copy_paste: experiment.block_copy_paste,
    forbid_outside_tools: experiment.forbid_outside_tools,
    study_flags: experiment.study_flags,
    content_warning_details: experiment.content_warning_details ?? '',
  };
}

/** Details only mean something for a warned study, so clearing the flags clears them. */
export function settingsPayload(form: SettingsForm) {
  return {
    ...form,
    content_warning_details: flagsWarn(form.study_flags) ? form.content_warning_details : '',
  };
}

/** Text compares trimmed, as the server stores it. */
export function settingsDirty(form: SettingsForm, experiment: Experiment): boolean {
  const saved = settingsPayload(settingsFormFrom(experiment));
  const sent = settingsPayload(form);
  return (
    TEXT_FIELDS.some((field) => sent[field].trim() !== saved[field].trim()) ||
    FLAG_FIELDS.some((field) => sent[field] !== saved[field]) ||
    [...sent.study_flags].sort().join() !== [...saved.study_flags].sort().join() ||
    sent.content_warning_details.trim() !== saved.content_warning_details.trim()
  );
}

export function settingsDetailsMissing(form: SettingsForm): boolean {
  return flagsWarn(form.study_flags) && form.content_warning_details.trim() === '';
}

const FIELD_LABELS: Record<DatasetMetaField, string> = {
  description: 'Dataset description',
  system_prompt: 'AI system prompt',
  human_prompt_prefix: 'Question prefix (shown above)',
  human_prompt_suffix: 'Question suffix (shown below)',
  prolific_pool: 'Prolific participant pool',
};

const FIELD_HINTS: Record<DatasetMetaField, string> = {
  description:
    'The rater instructions: shown to raters on the intro screen and behind the Instructions button, in every round. Not the Prolific listing, which is set per round on Launch on Prolific. Supports markdown.',
  system_prompt: 'Used to generate AI answers and assistance. Raters never see it. Plain text; line breaks preserved.',
  human_prompt_prefix:
    'Rendered above every question. Plain text with line breaks preserved, or Markdown when "Render questions as Markdown" is on.',
  human_prompt_suffix:
    'Rendered below every question. Plain text with line breaks preserved, or Markdown when "Render questions as Markdown" is on.',
  prolific_pool: 'For your reference when configuring Prolific filters; not sent to the Prolific API.',
};

const FIELD_PLACEHOLDERS: Partial<Record<DatasetMetaField, string>> = {
  human_prompt_prefix: 'e.g. When you see the text below, do you think x or y?',
  human_prompt_suffix: 'e.g. Is the statement above true or false?',
  prolific_pool: 'e.g. uk_representative_sample',
};

type SectionId = 'intro' | 'framing' | 'display' | 'rules' | 'ai' | 'consent' | 'notes';

const SECTIONS: { id: SectionId; title: string }[] = [
  { id: 'intro', title: 'Intro text' },
  { id: 'framing', title: 'Question framing' },
  { id: 'display', title: 'Question display' },
  { id: 'rules', title: 'Rater rules' },
  { id: 'ai', title: 'AI prompt' },
  { id: 'consent', title: 'Consent and content' },
  { id: 'notes', title: 'Notes' },
];

const wordCount = (text: string) => text.trim().split(/\s+/).filter(Boolean).length;
const onOff = (value: boolean) => (value ? 'on' : 'off');

function ConflictNote({ uploads }: { uploads: Upload[] }) {
  if (uploads.length === 0) return null;
  return (
    <div style={{ color: 'var(--warn)', marginTop: 6 }}>
      Declared differently by: {uploads.map((u) => u.filename).join(', ')}.
    </div>
  );
}

function ToggleRow({
  id,
  label,
  hint,
  checked,
  onChange,
  disabled,
  children,
}: {
  id: string;
  label: string;
  hint: React.ReactNode;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="xd-settings-row" style={{ opacity: disabled ? 0.7 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
        <div style={{ minWidth: 0 }}>
          <div id={`${id}-label`} style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.4 }}>
            {label}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.55, marginTop: 2 }}>
            {hint}
          </div>
        </div>
        <ToggleSwitch
          testId={id}
          ariaLabelledBy={`${id}-label`}
          checked={checked}
          disabled={disabled}
          onChange={() => onChange(!checked)}
        />
      </div>
      {children}
    </div>
  );
}

export function SettingsPanel({
  experiment,
  uploads,
  form,
  onChange,
  dirty,
  saving,
  onSave,
  isLocked,
  lockedHint,
  footer,
}: {
  experiment: Experiment;
  uploads: Upload[];
  form: SettingsForm;
  onChange: (patch: Partial<SettingsForm>) => void;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  isLocked: boolean;
  lockedHint: string;
  footer: React.ReactNode;
}) {
  const [closed, setClosed] = useState<ReadonlySet<SectionId>>(new Set());
  const toggleSection = (id: SectionId) =>
    setClosed((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  const textConflicts = (field: DatasetMetaField) =>
    uploads.filter((u) => {
      const declared = u.dataset_meta?.[field];
      return declared !== undefined && declared !== (experiment[field] ?? '');
    });
  const markdownConflicts = uploads.filter(
    (u) => u.dataset_meta?.render_markdown !== undefined && u.dataset_meta.render_markdown !== experiment.is_markdown,
  );
  const mathConflicts = uploads.filter(
    (u) => u.dataset_meta?.render_math !== undefined && u.dataset_meta.render_math !== experiment.render_math,
  );
  const sectionConflicts: Record<SectionId, boolean> = {
    intro: textConflicts('description').length > 0,
    framing: textConflicts('human_prompt_prefix').length + textConflicts('human_prompt_suffix').length > 0,
    display: markdownConflicts.length + mathConflicts.length > 0,
    rules: false,
    ai: textConflicts('system_prompt').length > 0,
    consent: false,
    notes: textConflicts('prolific_pool').length > 0,
  };

  const hasPrefix = form.human_prompt_prefix.trim() !== '';
  const hasSuffix = form.human_prompt_suffix.trim() !== '';
  const rules = [
    form.forbid_outside_tools && 'Internet and AI tools forbidden',
    form.block_copy_paste && 'Copy and paste blocked',
  ].filter(Boolean);
  const summaries: Record<SectionId, string> = {
    intro: form.description.trim() ? `${wordCount(form.description)} words` : 'Empty',
    framing:
      hasPrefix && hasSuffix ? 'Prefix and suffix' : hasPrefix ? 'Prefix only' : hasSuffix ? 'Suffix only' : 'None',
    display: `Markdown ${onOff(form.is_markdown)} · Math ${onOff(form.render_math)}`,
    rules: rules.length ? rules.join(' · ') : 'No restrictions',
    ai: form.system_prompt.trim() ? `${wordCount(form.system_prompt)} words` : 'Empty',
    consent: form.study_flags.length
      ? form.study_flags.map((flag) => STUDY_FLAG_EFFECTS[flag].label).join(' · ')
      : 'Standard consent, no flags',
    notes: form.prolific_pool.trim() || 'Empty',
  };

  const textField = (field: DatasetMetaField, kind: 'input' | 'textarea', minHeight?: number) => (
    <Field
      id={`meta-${field}`}
      label={FIELD_LABELS[field]}
      disabled={isLocked}
      hint={
        <>
          {FIELD_HINTS[field]}
          <ConflictNote uploads={textConflicts(field)} />
        </>
      }
    >
      {kind === 'textarea' ? (
        <textarea
          id={`meta-${field}`}
          value={form[field]}
          placeholder={FIELD_PLACEHOLDERS[field]}
          disabled={isLocked}
          onChange={(e) => onChange({ [field]: e.target.value })}
          style={{ ...textareaStyle, minHeight, cursor: isLocked ? 'not-allowed' : 'text' }}
        />
      ) : (
        <input
          id={`meta-${field}`}
          type="text"
          value={form[field]}
          placeholder={FIELD_PLACEHOLDERS[field]}
          disabled={isLocked}
          onChange={(e) => onChange({ [field]: e.target.value })}
          style={{ ...inputStyle, fontFamily: 'var(--font-mono)', cursor: isLocked ? 'not-allowed' : 'text' }}
        />
      )}
    </Field>
  );

  const bodies: Record<SectionId, React.ReactNode> = {
    intro: textField('description', 'textarea', 160),
    framing: (
      <div className="xd-grid-collapse" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 18px' }}>
        {textField('human_prompt_prefix', 'textarea', 90)}
        {textField('human_prompt_suffix', 'textarea', 90)}
      </div>
    ),
    display: (
      <>
        <ToggleRow
          id="meta-is_markdown"
          label="Render questions as Markdown"
          hint={
            <>
              Code blocks, tables, etc. will be rendered as styled elements, in the question prefix and suffix too.
              <ConflictNote uploads={markdownConflicts} />
            </>
          }
          checked={form.is_markdown}
          onChange={(value) => onChange({ is_markdown: value })}
          disabled={isLocked}
        />
        <ToggleRow
          id="meta-render_math"
          label="Render math"
          hint={
            <>
              TeX between $…$, $$…$$, \(…\) or \[…\] in the question, its options, its context and the document
              is typeset. Leave off when questions use $ for money.
              <ConflictNote uploads={mathConflicts} />
            </>
          }
          checked={form.render_math}
          onChange={(value) => onChange({ render_math: value })}
          disabled={isLocked}
        />
      </>
    ),
    rules: (
      <>
        <ToggleRow
          id="meta-forbid_outside_tools"
          label="Strictly forbid internet and AI tools"
          hint="Changes one line of the rater's instruction screen. It is an instruction only: nothing checks what a rater does in another tab."
          checked={form.forbid_outside_tools}
          onChange={(value) => onChange({ forbid_outside_tools: value })}
          disabled={isLocked}
        >
          <div
            data-testid="outside-tools-preview"
            style={{
              marginTop: 10,
              padding: '10px 12px',
              background: 'var(--surface-2)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 13,
              lineHeight: 1.55,
            }}
          >
            <div
              style={{
                font: '600 10px/1 var(--font-mono)',
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                color: 'var(--muted)',
                marginBottom: 6,
              }}
            >
              Raters read
            </div>
            <OutsideToolsPolicy
              forbid={form.forbid_outside_tools}
              studyProvidesAi={experiment.assistance_method !== 'none'}
            />
          </div>
        </ToggleRow>
        <ToggleRow
          id="meta-block_copy_paste"
          label="Block copy and paste on the rating screen"
          hint="Raters cannot select, copy or cut anything on the rating screen, or paste into an answer. A deterrent rather than a lock: a screenshot still works."
          checked={form.block_copy_paste}
          onChange={(value) => onChange({ block_copy_paste: value })}
          disabled={isLocked}
        />
      </>
    ),
    ai: textField('system_prompt', 'textarea', 140),
    consent: (
      <EthicsPanel
        experiment={experiment}
        flags={form.study_flags}
        details={form.content_warning_details}
        onFlagsChange={(study_flags) => onChange({ study_flags })}
        onDetailsChange={(content_warning_details) => onChange({ content_warning_details })}
        isLocked={isLocked}
      />
    ),
    notes: textField('prolific_pool', 'input'),
  };

  const detailsMissing = settingsDetailsMissing(form);
  const saveDisabled = saving || isLocked || detailsMissing;
  const status = isLocked
    ? 'Locked'
    : detailsMissing
      ? 'Content warning details are required before saving.'
      : dirty
        ? 'Unsaved changes'
        : 'All changes saved';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <Banner tone="info">
        Auto-populated from the first upload that declares dataset metadata. Edits here
        always win — later uploads that disagree are noted but never overwrite.
      </Banner>
      {isLocked && <Banner tone="warn">{lockedHint}</Banner>}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {SECTIONS.map(({ id, title }) => {
          const open = !closed.has(id);
          return (
            <section key={id} id={`settings-${id}`} data-testid={`settings-section-${id}`} className="xd-settings-section">
              <h3 style={{ margin: 0 }}>
                <button
                  type="button"
                  className="xd-settings-head"
                  aria-expanded={open}
                  aria-controls={`settings-${id}-body`}
                  onClick={() => toggleSection(id)}
                >
                  <span className="xd-settings-title">{title}</span>
                  <span className="xd-settings-summary" data-testid={`settings-summary-${id}`}>
                    {summaries[id]}
                    {sectionConflicts[id] && <span style={{ color: 'var(--warn)' }}> · upload disagrees</span>}
                  </span>
                  <span aria-hidden className="xd-settings-chevron" />
                </button>
              </h3>
              {/* Hidden, not unmounted: the consent section keeps what it
                  read from the terms source while collapsed. */}
              <div id={`settings-${id}-body`} className="xd-settings-body" hidden={!open}>
                {bodies[id]}
              </div>
            </section>
          );
        })}

        <div className="xd-settings-savebar">
          <span
            data-testid="settings-status"
            style={{ fontSize: 13, color: detailsMissing || (dirty && !isLocked) ? 'var(--warn)' : 'var(--muted)' }}
          >
            {status}
          </span>
          <button
            type="button"
            data-testid="settings-save"
            onClick={onSave}
            disabled={saveDisabled}
            style={{
              ...primaryButton,
              opacity: saveDisabled ? 0.6 : 1,
              cursor: saveDisabled ? 'not-allowed' : 'pointer',
            }}
          >
            {saving ? 'Saving…' : 'Save settings'}
          </button>
        </div>
      </div>

      {footer}
    </div>
  );
}
