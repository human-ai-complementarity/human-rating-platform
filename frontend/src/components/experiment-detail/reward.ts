/**
 * Currency helpers for the pilot/round reward field.
 *
 * Prolific's API stores reward in the minor unit of the workspace currency.
 * For 2-decimal currencies (USD, GBP, EUR, …) that's cents; for zero-decimal
 * currencies (JPY, KRW, VND, …) the minor unit IS the major unit, so dividing
 * by 100 would render ¥900 as "9.00" and a user typing "900" would send
 * ¥90,000 — a silent 100× overpayment. We branch on the workspace currency.
 */
const ZERO_DECIMAL_CURRENCIES = new Set(['JPY', 'KRW', 'VND', 'CLP', 'ISK']);

export function rewardDecimals(currencyCode: string | null): number {
  return currencyCode !== null && ZERO_DECIMAL_CURRENCIES.has(currencyCode) ? 0 : 2;
}

export function rewardMinorToInput(minorUnits: number, currencyCode: string | null): string {
  if (!Number.isFinite(minorUnits) || minorUnits <= 0) return '';
  const decimals = rewardDecimals(currencyCode);
  return (minorUnits / 10 ** decimals).toFixed(decimals);
}

export function rewardInputToMinor(value: string, currencyCode: string | null): number {
  const parsed = parseFloat(value);
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.round(parsed * 10 ** rewardDecimals(currencyCode));
}

/**
 * Live derived-info under the reward field: per-hour rate (matches Prolific's
 * own UI cue for whether the reward meets their minimum) and the participant
 * subtotal. Returns null when nothing meaningful can be shown yet.
 */
export function rewardHintText(
  rewardInput: string,
  currencyCode: string | null,
  currencySymbol: string | null,
  durationMinutes: number,
  places: number,
): string | null {
  const minor = rewardInputToMinor(rewardInput, currencyCode);
  if (minor <= 0) return null;
  const decimals = rewardDecimals(currencyCode);
  const major = minor / 10 ** decimals;
  const symbol = currencySymbol ?? '';
  const parts: string[] = [];
  if (durationMinutes > 0) {
    const hourly = major / (durationMinutes / 60);
    parts.push(`${symbol}${hourly.toFixed(decimals)}/hour`);
  }
  if (places > 0) {
    const total = major * places;
    parts.push(
      `Total: ${symbol}${total.toFixed(decimals)} for ${places} ${
        places === 1 ? 'rater' : 'raters'
      }`,
    );
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}
