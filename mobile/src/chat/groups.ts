/**
 * Group chats in words: who wrote a line, who a message will reach, what a
 * group's row says in the Chats list.
 *
 * Pure, so `node --test` covers it.
 */

import type { Agent, GroupMessage, GroupSummary } from "../net/types.ts";

/** The most and fewest agents a group holds (the deployment's own limits). */
export const GROUP_MIN_MEMBERS = 2;
export const GROUP_MAX_MEMBERS = 6;

type Named = Pick<Agent, "name" | "display_name">;

function agentLabel(name: string, agents: readonly Named[]): string {
  const agent = agents.find((a) => a.name === name);
  return (agent?.display_name || "").trim() || name;
}

/**
 * Every member named with `@` in a message, in the order they were named,
 * without repeats: the order they answer in. The pattern mirrors the
 * runtime's (olano/groups/cascade.py MENTION_RE) and the dashboard's.
 */
export function mentionedMembers(text: string, members: readonly string[]): string[] {
  const byLower = new Map(members.map((m) => [m.toLowerCase(), m]));
  const found: string[] = [];
  for (const match of text.matchAll(/@([A-Za-z0-9_.-]*[A-Za-z0-9_])/g)) {
    const hit = byLower.get(match[1].toLowerCase());
    if (hit && !found.includes(hit)) found.push(hit);
  }
  return found;
}

/** Who will answer a draft: the members it names, or the first member when it names nobody. */
export function addressees(text: string, members: readonly string[]): string[] {
  const named = mentionedMembers(text, members);
  return named.length ? named : members.slice(0, 1);
}

/**
 * The `@word` being typed at the end of a draft, for suggestions: the text
 * after the last `@` when nothing but name characters follows it.
 */
export function mentionQuery(text: string): string | null {
  const match = /(?:^|\s)@([A-Za-z0-9_.-]*)$/.exec(text);
  return match ? match[1] : null;
}

/** The draft with the `@word` being typed completed to `@name `. */
export function completeMention(text: string, name: string): string {
  return text.replace(/@([A-Za-z0-9_.-]*)$/, `@${name} `);
}

/** Who wrote a line, as the reader sees it. */
export function authorLabel(message: Pick<GroupMessage, "author" | "author_kind" | "mine">, agents: readonly Named[]): string {
  if (message.author_kind === "human") return message.mine ? "You" : message.author.replace(/^human-/, "");
  if (message.author_kind === "system") return "";
  return agentLabel(message.author, agents);
}

/** A group's name as shown: its own, or its members when it has none. */
export function groupTitle(group: Pick<GroupSummary, "name" | "members">, agents: readonly Named[]): string {
  return group.name.trim() || defaultGroupName(group.members, agents);
}

/** A name for a new group made of these agents: "Atlas, Jane and Nova". */
export function defaultGroupName(members: readonly string[], agents: readonly Named[]): string {
  const names = members.map((m) => agentLabel(m, agents));
  if (names.length <= 1) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  if (names.length === 3) return `${names[0]}, ${names[1]} and ${names[2]}`;
  return `${names[0]}, ${names[1]} and ${names.length - 2} others`;
}

/** The line under a group's name in the Chats list: its last message, or who is in it. */
export function groupSubtitle(
  group: Pick<GroupSummary, "members">,
  last: GroupMessage | null | undefined,
  agents: readonly Named[],
): string {
  if (last && last.body.trim()) {
    const who = authorLabel(last, agents);
    const text = last.body.replace(/\s+/g, " ").trim();
    return who ? `${who}: ${text}` : text;
  }
  return group.members.map((m) => agentLabel(m, agents)).join(", ");
}

/** "To Jane" / "To Jane and Nova" for a person's message, from its addressee field. */
export function addresseeLabel(addressee: string, agents: readonly Named[]): string {
  const names = addressee
    .split(",")
    .map((n) => n.trim())
    .filter(Boolean)
    .map((n) => agentLabel(n, agents));
  if (!names.length) return "";
  if (names.length === 1) return `To ${names[0]}`;
  return `To ${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
