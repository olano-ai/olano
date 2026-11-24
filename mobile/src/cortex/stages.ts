/**
 * Cortex's pipeline as the app shows it: the four stages in the order an idea
 * moves through them, each with how many items it holds, and the items one
 * stage (or all of them) shows. Discarded items never count, and neither does
 * one that has been promoted: its successor is the item in the next stage.
 * An item whose promotion is still running is already listed under the stage
 * it is heading for, marked as moving, so a tap on "Make it an idea" takes
 * effect on the list at once rather than a minute later.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import type { CortexItem, CortexKind } from "../net/types.ts";

/** Brainstorm, then idea, prototype, project: the flow of things. */
export const STAGES: { kind: CortexKind; label: string }[] = [
  { kind: "brainstorm", label: "Brainstorms" },
  { kind: "idea", label: "Ideas" },
  { kind: "prototype", label: "Prototypes" },
  { kind: "project", label: "Projects" },
];

/** The stage after each one; a project is the end of the funnel. */
export const NEXT: Partial<Record<CortexKind, CortexKind>> = { brainstorm: "idea", idea: "prototype", prototype: "project" };

export const KIND_LABEL: Record<CortexKind, string> = {
  brainstorm: "Brainstorm",
  idea: "Idea",
  prototype: "Prototype",
  project: "Project",
};

/** "a brainstorm", "an idea": the label with its article, for a button or a notice. */
export function withArticle(kind: CortexKind): string {
  const label = KIND_LABEL[kind].toLowerCase();
  return `${/^[aeiou]/.test(label) ? "an" : "a"} ${label}`;
}

export type StageFilter = CortexKind | "all";

const KINDS = new Set<string>(STAGES.map((s) => s.kind));

/** A promotion that is queued or running: the item is on its way somewhere. */
export function isMoving(item: CortexItem): boolean {
  const job = item.job;
  return !!job && job.kind === "promote" && (job.status === "running" || job.status === "queued");
}

/**
 * The stage to list the item under: where its promotion is taking it while
 * that runs (the job says so; the funnel's next stage when it does not),
 * otherwise its own.
 */
export function shownKind(item: CortexItem): CortexKind {
  if (isMoving(item)) {
    const to = item.job?.detail?.to;
    if (typeof to === "string" && KINDS.has(to)) return to as CortexKind;
    return NEXT[item.kind] ?? item.kind;
  }
  return item.kind;
}

function live(items: readonly CortexItem[], agent: string | null): CortexItem[] {
  return items.filter((i) => i.status !== "discarded" && i.status !== "promoted" && (!agent || i.agent === agent));
}

/** How many items each stage holds, for the agent picked (or everyone). */
export function stageCounts(items: readonly CortexItem[], agent: string | null): Record<CortexKind, number> {
  const counts = { brainstorm: 0, idea: 0, prototype: 0, project: 0 } as Record<CortexKind, number>;
  for (const item of live(items, agent)) {
    const kind = shownKind(item);
    if (kind in counts) counts[kind] += 1;
  }
  return counts;
}

/** The items to list, newest first. */
export function itemsFor(items: readonly CortexItem[], stage: StageFilter, agent: string | null): CortexItem[] {
  const time = (i: CortexItem) => Date.parse(i.updated_at || i.created_at || "") || 0;
  return live(items, agent)
    .filter((i) => stage === "all" || shownKind(i) === stage)
    .sort((a, b) => time(b) - time(a));
}

/** Tapping a stage shows it; tapping the stage already shown goes back to all. */
export function toggleStage(current: StageFilter, tapped: CortexKind): StageFilter {
  return current === tapped ? "all" : tapped;
}

/** One key per item across agents: the index is per agent, so ids alone can collide. */
export function itemKey(item: Pick<CortexItem, "agent" | "id">): string {
  return `${item.agent ?? ""}:${item.id}`;
}

/** The list with these items dropped (a discard took effect, or is about to). */
export function withoutItems(items: readonly CortexItem[], keys: ReadonlySet<string>): CortexItem[] {
  return items.filter((i) => !keys.has(itemKey(i)));
}

/**
 * The list with one item shown as on its way to `to`, before the deployment
 * has answered: a queued promotion stamp that the next refresh replaces with
 * the real job.
 */
export function withMoving(items: readonly CortexItem[], key: string, to: CortexKind): CortexItem[] {
  return items.map((i) =>
    itemKey(i) === key ? { ...i, job: { id: "", kind: "promote", status: "queued", detail: { to } } } : i,
  );
}
