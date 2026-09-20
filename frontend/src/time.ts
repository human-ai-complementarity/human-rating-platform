/**
 * The backend serialises timestamps without a `Z` in some paths, and
 * `new Date()` reads a bare ISO string as *local* time. On a machine outside
 * UTC that silently shifts every session deadline by the offset, so every
 * timestamp from the API goes through here before being parsed.
 */
export function withUtcSuffix(timestamp: string): string {
  return timestamp.endsWith('Z') ? timestamp : `${timestamp}Z`;
}

/** Minutes between two API timestamps, rounded to the nearest whole minute. */
export function minutesBetween(startIso: string, endIso: string): number {
  const start = new Date(withUtcSuffix(startIso)).getTime();
  const end = new Date(withUtcSuffix(endIso)).getTime();
  return Math.round((end - start) / 60000);
}
