import { useEffect, useState } from "react";
import { api } from "./api";

// What this deployment's money surfaces need to know, from GET /api/credits:
// which billing books it keeps, the credit vocabulary, and the unit it says it
// displays in.
//
// **Two books, never mixed.** Olano credits price Olano models and services and
// nothing else — the gateway's own authoritative charge. Dollars price what the
// customer's own provider bills them for calls on their own API keys. Every
// figure the dashboard renders belongs to exactly one of them, so every figure
// has a fixed unit: fmtCreditsAmount or fmtUsd. There is no third formatter
// that converts between them (see the note further down for why, and for what
// that means for `display`).
//
// The formatters at the bottom of this file read the snapshot below, which is
// why it is a module singleton and not React state: they are called from chart
// tick formatters and tooltip factories that aren't components and can't hold
// hooks. Components that render money call useCostDisplay() so they re-render
// once the snapshot lands (and if the deployment's plan changes which books it
// keeps); the formatters then read the fresh snapshot.

export type CostDisplayMode = "usd" | "credits";

export interface CostDisplay {
  display: CostDisplayMode;
  creditsPerUsd: number;
  unitName: string;
  unitNamePlural: string;
  /** Decimals for a credit amount >= 1. 0 at the launch rate (100/USD). */
  decimals: number;
  /**
   * Which of the two accounting books this deployment actually keeps — the
   * deployment shape, not a preference. A manual install is pure BYOK;
   * managed plans run both since 2026-08 (BYOK opened to all tiers; an
   * unrefreshed pre-change artifact can still arrive credits-only). Surfaces
   * that render money use these to decide which SECTIONS exist at all: a
   * credits-only box must never show a dollar API-cost card (there is no
   * provider bill), and a BYOK-only box must never show credits (there is no
   * account). Both true ⇒ show both.
   */
  creditsEnabled: boolean;
  byokEnabled: boolean;
}

/** The billing books a surface may render, in the order they should appear. */
export type BillingBook = "credits" | "byok";

/**
 * Which books a deployment keeps. Both false (a payload that predates the
 * flags, or an unreachable endpoint) is treated as "both" rather than "none":
 * hiding every number is a worse failure than showing an empty section.
 */
export function billingBooks(cd: CostDisplay): BillingBook[] {
  if (!cd.creditsEnabled && !cd.byokEnabled) return ["credits", "byok"];
  const books: BillingBook[] = [];
  if (cd.creditsEnabled) books.push("credits");
  if (cd.byokEnabled) books.push("byok");
  return books;
}

// USD until the first response lands. Deliberately not "credits with rate 1":
// a wrong rate would render believable-but-false numbers, while dollars are
// what the backend actually recorded.
const DEFAULTS: CostDisplay = {
  display: "usd",
  creditsPerUsd: 100,
  unitName: "credit",
  unitNamePlural: "credits",
  decimals: 0,
  // Both books until the deployment says otherwise — see billingBooks().
  creditsEnabled: true,
  byokEnabled: true,
};

let current: CostDisplay = DEFAULTS;
let pending: Promise<CostDisplay> | null = null;
let version = 0;
const listeners = new Set<() => void>();

interface CreditsResponse {
  display?: string;
  credits_per_usd?: number | null;
  unit_name?: string | null;
  unit_name_plural?: string | null;
  decimals?: number | null;
  credits_enabled?: boolean | null;
  byok_enabled?: boolean | null;
}

function normalize(raw: CreditsResponse): CostDisplay {
  const rate = Number(raw.credits_per_usd);
  const decimals = Number(raw.decimals);
  return {
    display: raw.display === "credits" ? "credits" : "usd",
    // A non-positive or missing rate falls back to the launch rate rather than
    // to 0 — multiplying every cost by zero is the one failure mode that looks
    // like real data ("you've spent nothing").
    creditsPerUsd: Number.isFinite(rate) && rate > 0 ? rate : DEFAULTS.creditsPerUsd,
    unitName: raw.unit_name?.trim() || DEFAULTS.unitName,
    unitNamePlural: raw.unit_name_plural?.trim() || DEFAULTS.unitNamePlural,
    decimals: Number.isFinite(decimals) ? Math.max(0, Math.min(6, Math.trunc(decimals))) : 0,
    // A backend that predates these keys sends neither; `!== false` keeps both
    // books visible there instead of blanking the money surfaces on upgrade.
    creditsEnabled: raw.credits_enabled !== false,
    byokEnabled: raw.byok_enabled !== false,
  };
}

/** The current snapshot — safe to call from non-component code (formatters). */
export function costDisplay(): CostDisplay {
  return current;
}

function load(): Promise<CostDisplay> {
  if (!pending) {
    pending = api
      .cachedGet<CreditsResponse>("/api/credits", 60_000)
      .then((raw) => {
        const next = normalize(raw || {});
        const changed =
          next.display !== current.display ||
          next.creditsPerUsd !== current.creditsPerUsd ||
          next.decimals !== current.decimals ||
          next.unitNamePlural !== current.unitNamePlural ||
          next.creditsEnabled !== current.creditsEnabled ||
          next.byokEnabled !== current.byokEnabled;
        current = next;
        if (changed) {
          version += 1;
          for (const listener of listeners) listener();
        }
        return next;
      })
      // Unreachable/erroring endpoint keeps the USD default: showing dollars is
      // always truthful, and an error here must not blank out the whole view.
      .catch(() => current);
  }
  return pending;
}

/** Re-read the mode after an action that can change it (Config → Save). */
export function refreshCostDisplay(): void {
  pending = null;
  api.invalidate("/api/credits");
  void load();
}

/**
 * Subscribe a component to the cost-display snapshot. The returned value also
 * doubles as the dependency that makes memoized chart/table renders recompute
 * when an admin flips the mode.
 */
export function useCostDisplay(): CostDisplay {
  const [snapshot, setSnapshot] = useState<CostDisplay>(current);

  useEffect(() => {
    let on = true;
    function onChange() {
      if (on) setSnapshot(current);
    }
    listeners.add(onChange);
    void load().then(() => onChange());
    return () => {
      on = false;
      listeners.delete(onChange);
    };
    // `version` is intentionally not a dep: listeners cover every change.
  }, []);

  return snapshot;
}

// ── formatters ──────────────────────────────────────────────────────────────
// These live here, next to the snapshot they depend on, rather than in
// views/insights/helpers: chat renders money too, and two copies of a formatter
// is exactly how one surface ends up still showing dollars after a mode switch.

/**
 * Dollars, always.
 *
 * The BYOK book is inherently a dollar book: it is a bill a provider sends the
 * customer in USD, and the drill-down's per-call figures are list-price
 * estimates of that same bill (a managed call prices to 0 there by design — see
 * analytics/threadcost/cost.py). Rendering it as "credits" would restate a
 * provider invoice in a unit Olano sells: a different number about a different
 * thing.
 */
export function fmtUsd(n: number): string {
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Credits carry the unit name because, unlike "$", a bare number gives the
// reader nothing to anchor on. Precision mirrors fmtUsd's ladder one rate-step
// down: whole credits once past 1 (at the launch rate a credit is a cent, so
// decimals there are noise), more digits as the amount shrinks.
function fmtCredits(credits: number, cd: CostDisplay): string {
  if (credits === 0) return `0 ${cd.unitNamePlural}`;
  const unit = Math.abs(credits) === 1 ? cd.unitName : cd.unitNamePlural;
  if (credits < 0.01) return `${credits.toFixed(4)} ${unit}`;
  if (credits < 1) return `${credits.toFixed(3)} ${unit}`;
  return `${credits.toLocaleString(undefined, {
    minimumFractionDigits: cd.decimals,
    maximumFractionDigits: cd.decimals,
  })} ${unit}`;
}

/**
 * Format an AUTHORITATIVE credits amount — the Olano gateway's own charge for
 * managed models and services — verbatim, never converted. Credits price Olano
 * models and services and nothing else; a dollar figure passed through here
 * would be a fabricated price for a call Olano never sold.
 */
export function fmtCreditsAmount(credits: number): string {
  return fmtCredits(credits, current);
}

/**
 * Dollar axis ticks. Precision comes from the *dataset max* so a chart of
 * sub-cent spend doesn't render every tick as a useless "$0.0000"; the precise
 * fmtUsd stays for tooltips and cards.
 */
export function usdAxisFormatter(maxUsd: number): (v: number) => string {
  if (!(maxUsd > 0)) return () => "$0";
  const decimals = maxUsd >= 1 ? 2 : maxUsd >= 0.1 ? 2 : maxUsd >= 0.01 ? 3 : 4;
  return (v: number) => (v === 0 ? "$0" : `$${v.toFixed(decimals)}`);
}

// There is deliberately no `fmtCost(usd)` / `costAxisFormatter` — no formatter
// that renders a recorded USD amount in `display`'s unit.
//
// Every money figure in the dashboard belongs to exactly one book, and a book
// fixes its own unit: managed spend is credits (fmtCreditsAmount, verbatim),
// BYOK spend is dollars (fmtUsd, never converted). Converting between them
// describes a different transaction, and a single conversation can hold both —
// switch the model mid-thread and every later turn lands in the other book — so
// even "what did this one thing cost" has no single-unit answer. The
// conversions that used to live here are why an agent's $118 provider estimate
// once headlined as "~118,447 credits" on a fleet whose whole credit spend was
// 122, and why a day that cost tens of credits read as "0.046 credits".
//
// A consequence worth knowing: `display` is therefore no longer read by any
// render. It is still resolved end to end (deployment plan → config.yaml →
// LIONCLAW_COST_DISPLAY → GET /api/credits) and still describes the
// deployment's stated unit, but nothing converts by it. Removing it is a
// product decision, not a cleanup — leave it until someone makes that call.
