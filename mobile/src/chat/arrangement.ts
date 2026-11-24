/**
 * How the Chats tab lays out the agents: Pinned, the person's own folders,
 * then All agents, each a section that folds shut.
 *
 * The rules (one home per agent, pin order, the hand-made order inside a
 * section, folder names) are the dashboard sidebar's own
 * (views/nav/botArrangement.ts through shared/dashboard.ts), so the app and
 * the dashboard arrange agents the same way. This module only turns an
 * arrangement into what the screen draws, and says where an agent can move.
 *
 * Pure, so `node --test` covers it.
 */

import { homeOf, membersOf, type Arrangement, type BotHome, type FolderNameError } from "../shared/dashboard.ts";

export interface ChatSection {
  /** "pinned", "all" or a folder id: also the id `folded` lists. */
  id: string;
  kind: "pinned" | "folder" | "all";
  title: string;
  /** Agent names, in the order the section shows them. */
  agents: string[];
  /** Folded shut (never while searching: a match must be visible). */
  folded: boolean;
}

/**
 * The sections to draw for `roster` (the agents this person can see). With
 * `matches`, only the agents it accepts, and only sections holding one.
 *
 * Pinned shows once something is pinned. A folder shows even while empty, so
 * a folder just made is there to move agents into. All agents shows unless
 * every agent has a home elsewhere.
 */
export function chatSections(a: Arrangement, roster: readonly string[], matches?: (name: string) => boolean): ChatSection[] {
  const searching = !!matches;
  const keep = (names: string[]) => (matches ? names.filter(matches) : names);
  const out: ChatSection[] = [];
  const folded = (id: string) => !searching && a.folded.includes(id);

  const pinned = keep(membersOf(a, "pinned", roster));
  if (pinned.length) out.push({ id: "pinned", kind: "pinned", title: "Pinned", agents: pinned, folded: folded("pinned") });

  for (const folder of a.groups) {
    const agents = keep(membersOf(a, { group: folder.id }, roster));
    if (searching && !agents.length) continue;
    out.push({ id: folder.id, kind: "folder", title: folder.name, agents, folded: folded(folder.id) });
  }

  const rest = keep(membersOf(a, "all", roster));
  if (rest.length) {
    // "All agents" next to Pinned or folders; on its own it needs no heading.
    out.push({ id: "all", kind: "all", title: "All agents", agents: rest, folded: folded("all") });
  }
  return out;
}

/** True when the list is nothing but All agents, which then needs no heading. */
export function onlyAllAgents(sections: readonly ChatSection[]): boolean {
  return sections.length === 1 && sections[0].kind === "all";
}

export interface MoveTarget {
  home: BotHome;
  label: string;
  /** Where the agent already is. */
  current: boolean;
}

/** Where an agent can be moved: Pinned, each folder, All agents. */
export function moveTargets(a: Arrangement, name: string): MoveTarget[] {
  const here = homeOf(a, name);
  const same = (home: BotHome) =>
    typeof home === "string" ? here === home : typeof here !== "string" && here.group === home.group;
  const targets: BotHome[] = ["pinned", ...a.groups.map((g) => ({ group: g.id })), "all"];
  return targets.map((home) => ({
    home,
    label: home === "pinned" ? "Pinned" : home === "all" ? "All agents" : a.groups.find((g) => g.id === home.group)?.name || "Folder",
    current: same(home),
  }));
}

/** The words for a folder name the arrangement refused. */
export function folderNameProblem(error: FolderNameError | "missing"): string {
  switch (error) {
    case "empty":
      return "Give the folder a name.";
    case "duplicate":
      return "You already have a folder with that name.";
    case "missing":
      return "That folder is gone.";
    default:
      return "That name can't be used.";
  }
}
