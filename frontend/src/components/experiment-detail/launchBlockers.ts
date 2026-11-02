/**
 * Words the launch gate's blockers as a clause, the way the backend's
 * `launch_blocker_summary` words its 400.
 *
 * Every blocker names something the experiment is missing, except the one a
 * `preview-testing` tag raises (#174): a preview-only draft from the pipeline's
 * testing mode is not missing anything, it must simply never reach Prolific.
 */
export const TESTING_PREVIEW_BLOCKER = 'testing preview';

export function launchBlockerSummary(blockers: string[]): string {
  const missing = blockers.filter((blocker) => blocker !== TESTING_PREVIEW_BLOCKER);
  const parts = blockers.includes(TESTING_PREVIEW_BLOCKER) ? ["it's a testing preview"] : [];
  if (missing.length > 0) parts.push(`it's missing ${missing.join(', ')}`);
  return parts.join(' and ');
}
