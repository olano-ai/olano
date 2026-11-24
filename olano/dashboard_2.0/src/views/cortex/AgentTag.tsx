// "Whose is this" on a Cortex card.
//
// The Cortex tabs list every agent's items at once, so each card names its
// owner: the agent's avatar and display name, drawn the way the sidebar draws
// them, so the eye that already knows Merli's face finds Merli's ideas without
// reading. Falls back to the bare name for an agent no longer in the roster
// (deleted since the item was made) — the item is still real.

import { useCurrentAgent } from "../../core/currentAgent";
import { BotMark } from "../agents/BotMark";
import { agentLabel } from "../agents/helpers";

export function AgentTag({ name, title }: { name: string; title?: string }) {
  const { agents } = useCurrentAgent();
  if (!name) return null;
  const bot = agents.find((a) => a.name === name);
  return (
    <span className="badge cortex-agent-tag" title={title ?? name} data-agent={name}>
      {bot ? <BotMark name={name} avatarUrl={bot.avatar_url} emoji={bot.emoji} size={14} /> : null}
      <span className="cortex-agent-tag-name">{bot ? agentLabel(bot) : name}</span>
    </span>
  );
}
