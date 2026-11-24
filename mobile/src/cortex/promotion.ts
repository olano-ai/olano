/**
 * What the item page says about an item's promotion: nothing for an item at
 * rest, that it is on its way (and the step the agent is on), that it has
 * arrived (and where), or that the last attempt failed (and why). One answer
 * for the banner, the button and the list row, so the three never disagree.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import type { CortexItem, CortexKind } from "../net/types.ts";
import { isMoving, KIND_LABEL, NEXT, shownKind, withArticle } from "./stages.ts";

export type PromotionState =
  | { phase: "moving"; to: CortexKind; step: string }
  | { phase: "promoted"; to: CortexKind; id: string }
  | { phase: "failed"; to: CortexKind; error: string }
  | null;

const KINDS = new Set<string>(Object.keys(KIND_LABEL));

function asKind(value: unknown): CortexKind | null {
  return typeof value === "string" && KINDS.has(value) ? (value as CortexKind) : null;
}

/** A run that ended badly: the item page says so instead of offering the same tap again. */
export const FAILED_JOB = new Set(["error", "stopped", "interrupted"]);

export function promotionState(item: CortexItem): PromotionState {
  if (isMoving(item)) {
    const step = item.job?.last_step;
    const text = step?.text ? step.text.replace(/\s+/g, " ").trim() : "";
    return { phase: "moving", to: shownKind(item), step: step?.tool && text ? `${step.tool}: ${text}` : text };
  }
  if (item.status === "promoted") {
    const links = item.links ?? {};
    return {
      phase: "promoted",
      to: asKind(links.promoted_to_kind) ?? NEXT[item.kind] ?? item.kind,
      id: typeof links.promoted_to_id === "string" ? links.promoted_to_id : "",
    };
  }
  const job = item.job;
  if (job && job.kind === "promote" && FAILED_JOB.has(job.status)) {
    const to = asKind(job.detail?.to) ?? NEXT[item.kind] ?? item.kind;
    const error = (job.error || "").trim();
    return {
      phase: "failed",
      to,
      error: error || (job.status === "stopped" ? "The run was stopped." : "The run was interrupted."),
    };
  }
  return null;
}

/** The lines the banner shows for each state, in the agent's voice. */
export function promotionText(state: Exclude<PromotionState, null>, agent: string): { title: string; body: string } {
  const who = agent || "The agent";
  const to = withArticle(state.to);
  switch (state.phase) {
    case "moving":
      return {
        title: `${who} is making this ${to}…`,
        body: state.step || "This usually takes a minute or two. You can leave; the list shows it under its new stage.",
      };
    case "promoted":
      return { title: `Now ${to}`, body: `${who} carried this forward. The ${KIND_LABEL[state.to].toLowerCase()} has its own page.` };
    case "failed":
      return { title: `Could not make it ${to}`, body: state.error };
  }
}
