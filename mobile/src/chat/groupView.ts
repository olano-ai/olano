/**
 * A group chat as the screen draws it: every line of the transcript, the
 * person's messages still on their way and the reply being written, each as
 * the chat's own message shape (chat/model.ts), plus who wrote it.
 *
 * Pure, so `node --test` covers it.
 */

import type { GroupMessage } from "../net/types.ts";
import type { GroupLiveTurn, ToolCall } from "../shared/dashboard.ts";
import type { ChatMessage } from "./model.ts";
import type { PendingGroupMessage } from "./groupConversation.ts";

export interface GroupItem {
  /** The agent's name, `human-<username>` for a person, "" for a system line. */
  author: string;
  kind: "human" | "agent" | "system";
  /** Written by the person reading. */
  mine: boolean;
  /** The members a person's message went to. */
  to: string[];
  message: ChatMessage;
}

function parseTs(iso: string): number | undefined {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? undefined : ms;
}

function splitAddressee(addressee: string): string[] {
  return addressee
    .split(",")
    .map((n) => n.trim())
    .filter(Boolean);
}

export function groupItems(
  messages: readonly GroupMessage[],
  pending: readonly PendingGroupMessage[],
  live: GroupLiveTurn | null,
  steps: Readonly<Record<number, ToolCall[]>>,
): GroupItem[] {
  const out: GroupItem[] = messages.map((m) => {
    const mine = m.author_kind === "human" && m.mine === true;
    const role: ChatMessage["role"] = m.author_kind === "system" ? "system" : mine ? "user" : "assistant";
    return {
      author: m.author,
      kind: m.author_kind,
      mine,
      to: m.author_kind === "human" ? splitAddressee(m.addressee) : [],
      message: {
        key: `g${m.id}`,
        role,
        body: m.body,
        tools: steps[m.id] ?? [],
        segments: m.author_kind === "agent" ? m.segments : undefined,
        media: m.media,
        ts: parseTs(m.ts),
      },
    };
  });
  for (const p of pending) {
    out.push({
      author: "",
      kind: "human",
      mine: true,
      to: p.addressees,
      message: { key: p.key, role: "user", body: p.body, tools: [], ts: p.ts, sending: true },
    });
  }
  if (live && (live.body || live.tools.length)) {
    out.push({
      author: live.author,
      kind: "agent",
      mine: false,
      to: [],
      message: {
        key: `live-${live.author}`,
        role: "assistant",
        body: live.body,
        segments: live.segments,
        tools: live.tools,
        media: live.media,
        streaming: true,
        ts: Date.now(),
      },
    });
  }
  return out;
}

/** Whether a line starts a new run of lines by someone else (and so shows who wrote it). */
export function startsRun(item: GroupItem, previous: GroupItem | undefined): boolean {
  if (!previous) return true;
  if (item.kind === "system" || previous.kind === "system") return true;
  if (item.mine !== previous.mine) return true;
  return !item.mine && item.author !== previous.author;
}
