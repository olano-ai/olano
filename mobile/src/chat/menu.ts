/**
 * What the conversation's "..." menu offers, grouped the way iOS groups a
 * settings screen: things about this conversation, things about the agent,
 * then the rest. Pure (no DOM, no Ionic), so `node --test` covers which rows
 * show when; `ui/MenuSheet.tsx` draws it.
 *
 * Rules the layout follows:
 * - Every row has an icon and a plain-text label; colour is for state, not
 *   for every word.
 * - A row that opens another screen has a chevron (`opens: "screen"`); a row
 *   that does something here has none.
 * - A setting shows its current value on the right (Mode: Auto), so nobody
 *   has to open it to find out.
 * - A row that is not available says why only when that helps; otherwise it
 *   is simply not there.
 */

export type MenuAction =
  | "new"
  | "mode"
  | "rename"
  | "compact"
  | "conversations"
  | "files"
  | "scheduled"
  | "info"
  | "handsFree"
  | "addAgent";

export type MenuIcon =
  | "new"
  | "mode"
  | "rename"
  | "compact"
  | "conversations"
  | "files"
  | "scheduled"
  | "info"
  | "handsFree"
  | "addAgent";

export interface MenuRow {
  action: MenuAction;
  icon: MenuIcon;
  label: string;
  /** A second line under the label, for what is not obvious from it. */
  detail?: string;
  /** The current value, on the right. */
  value?: string;
  /** "screen": navigates (chevron). "sheet": opens another sheet once this one has closed. "now": acts in place. */
  opens: "screen" | "sheet" | "now";
}

export interface MenuSection {
  title: string;
  rows: MenuRow[];
}

export interface MenuContext {
  /** The conversation exists on the deployment (not a new, unsent one). */
  hasThread: boolean;
  modeLabel: string;
  /** Group chats are on and there is another agent to add. */
  canAddAgent: boolean;
  handsFreeOn: boolean;
  /** The admin shows Files on phones (config.yaml mobile_app). */
  showFiles: boolean;
}

export function conversationMenu(ctx: MenuContext): MenuSection[] {
  const conversation: MenuRow[] = [
    { action: "new", icon: "new", label: "New Conversation", opens: "screen" },
    { action: "mode", icon: "mode", label: "Mode", value: ctx.modeLabel, opens: "sheet" },
  ];
  if (ctx.hasThread) {
    conversation.push({ action: "rename", icon: "rename", label: "Rename", opens: "sheet" });
    conversation.push({
      action: "compact",
      icon: "compact",
      label: "Compact",
      detail: "Summarize earlier messages to free up the agent's memory",
      opens: "now",
    });
  }

  const agent: MenuRow[] = [
    { action: "conversations", icon: "conversations", label: "All Conversations", opens: "screen" },
  ];
  if (ctx.showFiles) agent.push({ action: "files", icon: "files", label: "Files", opens: "screen" });
  agent.push({ action: "scheduled", icon: "scheduled", label: "Scheduled Tasks", opens: "screen" });
  agent.push({ action: "info", icon: "info", label: "Agent Info", opens: "screen" });

  const more: MenuRow[] = [];
  if (!ctx.handsFreeOn) {
    more.push({ action: "handsFree", icon: "handsFree", label: "Hands-Free Mode", detail: "Talk and listen without touching the phone", opens: "now" });
  }
  if (ctx.hasThread && ctx.canAddAgent) {
    more.push({ action: "addAgent", icon: "addAgent", label: "Add an Agent", detail: "Turn this into a group chat", opens: "sheet" });
  }

  const sections: MenuSection[] = [
    { title: "Conversation", rows: conversation },
    { title: "Agent", rows: agent },
  ];
  if (more.length) sections.push({ title: "More", rows: more });
  return sections;
}
