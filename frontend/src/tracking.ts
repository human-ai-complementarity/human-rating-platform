import type { CaptureResult } from 'posthog-js';

const KEY = ((import.meta.env.VITE_PUBLIC_POSTHOG_KEY as string | undefined) ?? '').trim();

// Prolific IDs are 24 hex characters.
const PROLIFIC_ID = /\b[0-9a-f]{24}\b/gi;
const redact = <T,>(value: T): T =>
  value === undefined ? value : JSON.parse(JSON.stringify(value).replace(PROLIFIC_ID, '[redacted]'));

function redactProlificIds(event: CaptureResult | null): CaptureResult | null {
  if (!event) return event;
  return { ...event, properties: redact(event.properties), $set: redact(event.$set), $set_once: redact(event.$set_once) };
}

// Cookieless: PostHog stores nothing in the browser and never identifies raters.
export async function startTracking(): Promise<void> {
  if (!KEY || navigator.webdriver) return; // Ignore automated browsers (`navigator.webdriver`)
  const { default: posthog } = await import('posthog-js');
  posthog.init(KEY, {
    api_host: (import.meta.env.VITE_PUBLIC_POSTHOG_HOST as string | undefined) || 'https://us.i.posthog.com',
    defaults: '2026-05-30',
    cookieless_mode: 'always',
    person_profiles: 'never',
    autocapture: true,
    mask_all_text: true,
    mask_all_element_attributes: false,
    capture_performance: { web_vitals: true },
    capture_exceptions: true,
    disable_session_recording: true,
    disable_surveys: true,
    mask_personal_data_properties: true,
    custom_personal_data_properties: ['PROLIFIC_PID', 'STUDY_ID', 'SESSION_ID'],
    before_send: redactProlificIds,
  });
}
