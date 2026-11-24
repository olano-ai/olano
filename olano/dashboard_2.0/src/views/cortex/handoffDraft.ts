import type { CortexItem, PulseItem } from "./types";

// What lands in the composer of a NEW Cortex thread (see chatHandoff.ts).
//
// It frames the conversation for the agent — what the person is here to do,
// which item, and the full workspace path to its artifacts so the agent opens
// them rather than hunting — and ends with "User: " so the person's own words
// come next. Nothing here is sent until they press Send; the framing is a
// prefix to their message.
//
// Pure (no React, no router) so `handoffDraft.test.ts` can run it under Node.

/** What the button asks of the agent. */
export type DraftIntent = "chat" | "explore" | "run" | "fix";

type T = (key: string, vars?: Record<string, string>) => string;

function draftFor(
  t: T,
  intent: DraftIntent,
  vars: { kind: string; title: string; artifacts: string[]; details?: string; whyNow?: string },
): string {
  const context =
    (vars.artifacts.length ? t("draft.artifacts", { paths: vars.artifacts.join("\n") }) : "") +
    (vars.details ? t("draft.details", { details: vars.details }) : "") +
    (vars.whyNow ? t("draft.whyNow", { whyNow: vars.whyNow }) : "");
  return t(`draft.${intent}`, { kind: vars.kind, title: vars.title, context });
}

/**
 * The full path of a Cortex item's folder. `abs_path` comes from the API; an
 * older item that stored only its relative `path` still gets that, which is
 * better than a draft naming nothing.
 */
export function cortexItemArtifacts(item: Pick<CortexItem, "path" | "abs_path">): string[] {
  const p = (item.abs_path || item.path || "").trim();
  return p ? [p] : [];
}

/** Draft for an idea / brainstorm / prototype / project. */
export function cortexItemDraft(
  t: T,
  intent: DraftIntent,
  item: Pick<CortexItem, "kind" | "title" | "path" | "abs_path">,
): string {
  return draftFor(t, intent, {
    kind: item.kind || t("draft.item"),
    title: item.title,
    artifacts: cortexItemArtifacts(item),
  });
}

/**
 * A Pulse card's artifacts are its `references` — workspace-relative paths the
 * producer named — made absolute against the card's own agent's workspace.
 * An already-absolute reference (or a URL) passes through untouched.
 */
export function pulseArtifacts(item: Pick<PulseItem, "references" | "workspace_root">): string[] {
  const root = (item.workspace_root || "").replace(/\/+$/, "");
  return (item.references || [])
    .map((r) => String(r || "").trim())
    .filter(Boolean)
    .map((r) => (root && !r.startsWith("/") && !/^[a-z]+:\/\//i.test(r) ? `${root}/${r}` : r));
}

/** Draft for a Pulse card (`chat`) or a self-check finding (`fix`). */
export function pulseDraft(
  t: T,
  intent: DraftIntent,
  item: Pick<PulseItem, "title" | "content" | "why_now" | "references" | "workspace_root">,
): string {
  return draftFor(t, intent, {
    kind: "pulse",
    title: item.title || item.content || "",
    artifacts: pulseArtifacts(item),
    // A pulse has no folder of its own, so its body IS the context.
    details: item.content && item.content !== item.title ? item.content : "",
    whyNow: item.why_now || "",
  });
}
