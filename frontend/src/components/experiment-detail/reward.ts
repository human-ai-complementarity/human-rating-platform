/**
 * Currency helpers for the pilot/round reward field.
 *
 * Prolific's API stores reward in the minor unit of the workspace currency.
 * For 2-decimal currencies (USD, GBP, EUR, …) that's cents; for zero-decimal
 * currencies (JPY, KRW, VND, …) the minor unit IS the major unit, so dividing
 * by 100 would render ¥900 as "9.00" and a user typing "900" would send
 * ¥90,000 — a silent 100× overpayment. We branch on the workspace currency.
 */
import type { ProlificPricing } from '../../types';

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

/** Format minor units as a currency figure, e.g. 132300 -> "£1323.00". */
export function formatMinorUnits(
  minorUnits: number,
  currencyCode: string | null,
  currencySymbol: string | null,
): string {
  const decimals = rewardDecimals(currencyCode);
  return `${currencySymbol ?? ''}${(minorUnits / 10 ** decimals).toFixed(decimals)}`;
}

/**
 * What Prolific will actually charge for a study, in minor units.
 *
 * Prolific bills rewards, a platform fee on those rewards, and VAT on the fee
 * (not on the rewards). A study's `total_cost` is that sum, which is why the
 * reward subtotal alone reads ~30% low against Prolific's study page. Without
 * rates (`pricing` null, e.g. Prolific disabled) only `rewards` is known, so
 * fee/VAT come back as null and callers show the subtotal on its own.
 *
 * Estimates can land a penny or two off `total_cost`: Prolific rounds per
 * submission internally, so the launched study's own figure is authoritative.
 */
export function estimateStudyCost(
  rewardMinorUnits: number,
  places: number,
  pricing: ProlificPricing | null,
): { rewards: number; fees: number | null; vat: number | null; total: number } {
  const rewards = rewardMinorUnits * places;
  if (!pricing) return { rewards, fees: null, vat: null, total: rewards };
  const fees = Math.round(
    rewards * pricing.fees_percentage + pricing.fees_per_submission * places,
  );
  const vat = Math.round(fees * pricing.vat_percentage);
  return { rewards, fees, vat, total: rewards + fees + vat };
}

/**
 * Live derived-info under the reward field: per-hour rate (matches Prolific's
 * own UI cue for whether the reward meets their minimum) and the estimated
 * Prolific charge, broken out so the fee and VAT are visible rather than
 * folded into one number. Returns null when nothing meaningful can be shown yet.
 */
export function rewardHintText(
  rewardInput: string,
  currencyCode: string | null,
  currencySymbol: string | null,
  durationMinutes: number,
  places: number,
  pricing: ProlificPricing | null = null,
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
    const { rewards, fees, vat, total } = estimateStudyCost(minor, places, pricing);
    const money = (m: number) => formatMinorUnits(m, currencyCode, currencySymbol);
    const raters = `${places} ${places === 1 ? 'rater' : 'raters'}`;
    parts.push(
      fees === null
        ? `Rewards: ${money(rewards)} for ${raters}`
        : `Est. Prolific total: ${money(total)} for ${raters}` +
            ` (${money(rewards)} rewards + ${money(fees)} fee + ${money(vat ?? 0)} VAT)`,
    );
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}
