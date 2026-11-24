import {
  Activity,
  ArrowLeftRight,
  Brain,
  Clock,
  Globe,
  MessageCircle,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { CHANNEL_TONES, channelLabel, type T } from "./channelMeta";

// The channel vocabulary itself lives in channelMeta.ts (no React, so the
// pure thread logic can be unit-tested under Node); this module draws it.
// Re-exported so every existing importer keeps seeing one module.
export {
  CHANNEL_NAMES,
  CHANNEL_TONES,
  PROTOCOL_CHANNELS,
  channelLabel,
  isMessengerChannel,
  isPlatformChannel,
  type T,
} from "./channelMeta";

// ── origin pills ────────────────────────────────────────────────────────────
// Non-interactive origins only. An ordinary chat is the baseline the others
// are read against, so it gets no pill. Lives here (not in ChatView) because
// the simple shell's rail badges its thread rows with the same pills — one
// definition, so the two shells can never disagree about what SCHEDULED means.
// `cortex` is the one origin here that IS an ordinary human conversation: the
// person clicked "Chat about this" on a Pulse card or a Cortex item and the
// dashboard handed the thread to /chat. It earns a pill anyway — in a rail of
// look-alike dashboard chats, "this one came out of Cortex" is exactly what
// tells you which conversation you are looking at, and it carries the Cortex
// violet so the two surfaces read as one feature.
// `external` is a turn an OUTSIDE system sent over the inbound MCP endpoint or
// an A2A Agent Card: not a person on this dashboard, not one of our agents, so
// it must read differently from both — the channel pill beside it then names
// the protocol (MCP / A2A) the stranger used.
export const ORIGIN_ICONS: Record<string, LucideIcon> = {
  scheduled: Clock,
  heartbeat: Activity,
  peer: ArrowLeftRight,
  autonomous: Sparkles,
  cortex: Brain,
  external: Globe,
};

export function OriginPill({ origin, t }: { origin: string; t: T }) {
  const Icon = ORIGIN_ICONS[origin];
  if (!Icon) return null;
  const label = t(`thread.origin.${origin}`);
  return (
    <span className={`thread-origin thread-origin-${origin}`} title={label}>
      <Icon size={10} strokeWidth={2} />
      {label}
    </span>
  );
}

/**
 * The badge. Shares `.thread-origin`'s geometry with the origin pills so the
 * two read as one family, and adds a hairline ring so a channel badge is never
 * mistaken for an origin badge even where the hues sit close (WhatsApp's green
 * beside the AGENT pill's `--success`).
 *
 * No icon in the labelled form, unlike the origin pills. Theirs each carry a
 * DIFFERENT glyph (clock / activity / swap / sparkle), so the icon is half the
 * meaning; a channel badge would show the same generic bubble on all twenty,
 * costing ~13px of a 240px rail row to say nothing the word beside it doesn't.
 *
 * `compact` is the exception — it drops the word, so the glyph is all that is
 * left to mark the row as "…and it came from somewhere". Used when a row
 * already carries an origin pill, so two badges never crowd out the title.
 */
export function ChannelPill({
  channel,
  t,
  compact = false,
}: {
  channel: string;
  t: T;
  compact?: boolean;
}) {
  const tone = CHANNEL_TONES[channel];
  if (!tone) return null;
  const label = channelLabel(channel, t);
  return (
    <span
      className={`thread-origin thread-channel thread-channel-${tone}`}
      title={t("thread.channel.via", { channel: label })}
    >
      {compact ? <MessageCircle size={10} strokeWidth={2} /> : label}
    </span>
  );
}
