// Slash-command registry for the chat composer. `descKey` resolves through the
// chat string dictionary; execution lives in ChatView (it needs the view's
// state + handlers). `args` documents the expected argument in the menu.

export type SlashGroup = "command" | "skill" | "custom";

export interface SlashCommand {
  name: string;
  args?: string;
  descKey?: string; // i18n key (built-in commands)
  desc?: string; // raw description (dynamic per-agent skill commands)
  group?: SlashGroup; // defaults to "command"
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: "help", descKey: "slash.help" },
  { name: "new", descKey: "slash.new" },
  { name: "skills", descKey: "slash.skills" },
  { name: "subagents", descKey: "slash.subagents" },
  { name: "tools", descKey: "slash.tools" },
  { name: "mcps", descKey: "slash.mcps" },
  { name: "reload-skills", descKey: "slash.reloadSkills" },
  { name: "reload-tools", descKey: "slash.reloadTools" },
  { name: "reload-mcps", descKey: "slash.reloadMcps" },
  { name: "reload-agent", descKey: "slash.reloadAgent" },
  { name: "reload-vault", descKey: "slash.reloadVault" },
  { name: "context", descKey: "slash.context" },
  { name: "compact", descKey: "slash.compact" },
  { name: "quiet", descKey: "slash.quiet" },
  { name: "normal", descKey: "slash.normal" },
  { name: "verbose", descKey: "slash.verbose" },
  { name: "auto", descKey: "slash.auto" },
  { name: "plan", descKey: "slash.plan" },
  { name: "manual", descKey: "slash.manual" },
  { name: "act", descKey: "slash.act" },
  { name: "rename", args: "<name>", descKey: "slash.rename" },
];

// Split "/rename My thread" → { head: "rename", arg: "My thread" }. Returns null
// when the text isn't a slash command.
export function parseSlash(raw: string): { head: string; arg: string } | null {
  if (!raw.startsWith("/")) return null;
  const trimmed = raw.slice(1).trim();
  const sp = trimmed.indexOf(" ");
  const head = (sp === -1 ? trimmed : trimmed.slice(0, sp)).toLowerCase();
  const arg = sp === -1 ? "" : trimmed.slice(sp + 1).trim();
  return { head, arg };
}
