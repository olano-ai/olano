/**
 * Credits and usage, read-only: the deployment's balance and what each Olano
 * service spent, as Olano Cloud reports them (the dashboard's Overview cards).
 * The app shows the numbers and nothing to buy with: a purchase button, or a
 * link to one, is not allowed in an iPhone app outside the US storefront
 * (App Store rule 3.1.1), so the app leaves buying to the dashboard.
 */

import type { CreditsBalance, CreditsDisplay, ServiceUsage } from "../net/types.ts";

export interface CreditUnit {
  one: string;
  many: string;
  decimals: number;
}

/** The deployment's credit vocabulary, or "credit"/"credits" with whole numbers. */
export function creditUnit(display: CreditsDisplay | null | undefined): CreditUnit {
  return {
    one: display?.unit_name || "credit",
    many: display?.unit_name_plural || "credits",
    decimals: typeof display?.decimals === "number" && display.decimals >= 0 ? display.decimals : 0,
  };
}

/** An amount of credits as the dashboard writes it: "1,250 credits", "0.004 credits". */
export function formatCredits(amount: number, unit: CreditUnit): string {
  if (!Number.isFinite(amount) || amount === 0) return `0 ${unit.many}`;
  const word = Math.abs(amount) === 1 ? unit.one : unit.many;
  const abs = Math.abs(amount);
  if (abs < 0.01) return `${amount.toFixed(4)} ${word}`;
  if (abs < 1) return `${amount.toFixed(3)} ${word}`;
  const digits = amount.toLocaleString("en-US", {
    minimumFractionDigits: unit.decimals,
    maximumFractionDigits: unit.decimals,
  });
  return `${digits} ${word}`;
}

/** How much of the month's included credits is used, 0 to 100; null without an allowance. */
export function usedPercent(balance: CreditsBalance): number | null {
  const included = balance.monthly_included ?? 0;
  if (included <= 0) return null;
  return Math.min(100, Math.max(0, ((balance.monthly_used ?? 0) / included) * 100));
}

/** Running low: under 5% of the monthly allowance, and never more than 50 left. */
export function isLow(balance: CreditsBalance): boolean {
  return (balance.balance ?? 0) <= Math.max(50, (balance.monthly_included ?? 0) * 0.05);
}

/** "Nov 1" for the day the included credits renew, or null when unknown. */
export function renewsOn(balance: CreditsBalance, locale?: string): string | null {
  if (!balance.period_end) return null;
  const date = new Date(balance.period_end);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(locale, { month: "short", day: "numeric" });
}

/** The services that spent anything, biggest first. */
export function spendingServices(usage: ServiceUsage | null | undefined): NonNullable<ServiceUsage["services"]> {
  return (usage?.services ?? []).filter((s) => s.credits > 0).sort((a, b) => b.credits - a.credits);
}
