/**
 * What "Talk it over" puts in the message box of a Cortex item's conversation,
 * the first time: what the person is there for, which item, and the full path
 * of its folder so the agent opens it rather than hunting, ending in "User: "
 * for their own words. Nothing is sent until they send it, and a conversation
 * that already has messages is reopened as it is.
 *
 * The draft is the dashboard's (`views/cortex/handoffDraft.ts`, through
 * shared/dashboard.ts). Its English wording lives in the dashboard's
 * `views/cortex/strings.ts`, which the app cannot import (it reaches React
 * through a type), so the wording is repeated here and draft.test.ts fails
 * when the two differ.
 */

import type { CortexItem } from "../net/types.ts";
import { cortexItemDraft, type DraftIntent } from "../shared/dashboard.ts";

export const DRAFT_STRINGS: Record<string, string> = {
  "draft.chat": 'The user wants to chat about this {kind}: "{title}".{context}\n\nUser: ',
  "draft.explore": 'The user wants to explore more about this {kind}: "{title}".{context}\n\nUser: ',
  "draft.run": 'The user wants to run/try this {kind}: "{title}".{context}\n\nUser: ',
  "draft.fix": 'The user wants this self-check finding investigated and, where safe, fixed: "{title}".{context}\n\nUser: ',
  "draft.artifacts": "\nArtifacts (full workspace path):\n{paths}",
  "draft.details": "\nDetails: {details}",
  "draft.whyNow": "\nWhy now: {whyNow}",
  "draft.item": "item",
};

function t(key: string, vars: Record<string, string> = {}): string {
  return (DRAFT_STRINGS[key] ?? key).replace(/\{(\w+)\}/g, (_, name: string) => vars[name] ?? "");
}

/** The opening draft for talking about a Cortex item. */
export function itemDraft(item: Pick<CortexItem, "kind" | "title" | "path" | "abs_path">, intent: DraftIntent = "chat"): string {
  return cortexItemDraft(t, intent, { kind: item.kind, title: item.title, path: item.path ?? "", abs_path: item.abs_path });
}
