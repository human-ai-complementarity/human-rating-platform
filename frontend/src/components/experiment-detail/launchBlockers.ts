/**
 * The blocker a `preview-testing` tag raises (#174), by its API key. A testing
 * preview never goes to Prolific, so unlike the rest of the launch gate it
 * still applies after a pilot: the API refuses publishing a draft and creating
 * a round too.
 */
export const TESTING_PREVIEW_BLOCKER = 'testing preview';

/** The served fix for the testing-preview blocker, or null when it isn't raised. */
export function testingPreviewFix(blockers: string[], fixes: string[]): string | null {
  const index = blockers.indexOf(TESTING_PREVIEW_BLOCKER);
  return index === -1 ? null : (fixes[index] ?? null);
}
