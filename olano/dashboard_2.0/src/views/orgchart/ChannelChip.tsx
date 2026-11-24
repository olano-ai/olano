import {
  Bell,
  Box,
  Braces,
  Building2,
  Feather,
  Hash,
  House,
  Mail,
  MessageCircle,
  MessageSquareText,
  MessagesSquare,
  Webhook,
  type LucideIcon,
} from "lucide-react";
import {
  channelBrand,
  channelStatus,
  type ChannelBrand,
  type ChannelStatus,
  type OrgChannel,
} from "./channelBrands";

// A messaging connector drawn as a small chip on an org chart card (and, larger,
// in the detail drawer). Brand chips carry the product's own colour and a
// simplified white mark; everything else is a neutral chip with a descriptive
// icon. Status rides a corner dot so a healthy chart stays quiet: amber while
// connecting/pairing, red (plus a ring) when it needs attention, greyed out
// when the agent itself is stopped, a pulse while a message is arriving.

const BRAND_BG: Partial<Record<ChannelBrand, string>> = {
  whatsapp: "#25D366",
  telegram: "#229ED9",
  discord: "#5865F2",
  slack: "#4A154B",
  signal: "#3A76F0",
  imessage: "#34C759",
  wechat: "#07C160",
};

const NEUTRAL_ICON: Partial<Record<ChannelBrand, LucideIcon>> = {
  email: Mail,
  sms: MessageSquareText,
  matrix: Hash,
  mattermost: MessagesSquare,
  wecom: Building2,
  feishu: Feather,
  dingtalk: Bell,
  qq: MessageCircle,
  homeassistant: House,
  webhook: Webhook,
  api: Braces,
  debox: Box,
};

function BrandGlyph({ brand, size }: { brand: ChannelBrand; size: number }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (brand) {
    case "whatsapp":
      return (
        <svg {...common} fill="none">
          <path d="M9.2 18.45A7.5 7.5 0 1 0 6.7 16.8L4.8 19.2Z" stroke="#fff" strokeWidth={2.2} strokeLinejoin="round" />
          <path d="M9.6 8.4c-.4 3 2.9 6.4 6 6l.8-1.5-1.9-1-.9.8a5 5 0 0 1-2.3-2.3l.8-.9-1-1.9z" fill="#fff" />
        </svg>
      );
    case "telegram":
      return (
        <svg {...common}>
          <path
            d="M3.8 11.4l15.6-6.1c.7-.3 1.4.2 1.2 1.1l-2.6 12.4c-.2.9-.8 1.1-1.5.7l-4-3-1.9 1.9c-.2.2-.4.3-.8.3l.3-4 7.3-6.6c.3-.3-.1-.4-.5-.2l-9 5.7-3.9-1.2c-.8-.3-.9-.8.1-1.2z"
            fill="#fff"
          />
        </svg>
      );
    case "discord":
      return (
        <svg {...common}>
          <path
            fillRule="evenodd"
            fill="#fff"
            d="M18.6 6.4A15 15 0 0 0 14.9 5.2l-.5 1a14 14 0 0 0-4.8 0l-.5-1a15 15 0 0 0-3.7 1.2C3.1 9.9 2.5 13.3 2.8 16.7a15 15 0 0 0 4.6 2.3l1-1.6a9.6 9.6 0 0 1-1.5-.7l.4-.3a10.7 10.7 0 0 0 9.4 0l.4.3c-.5.3-1 .5-1.5.7l1 1.6a15 15 0 0 0 4.6-2.3c.4-3.9-.7-7.3-2.6-10.3zM9.3 14.6c-.9 0-1.6-.8-1.6-1.8s.7-1.8 1.6-1.8 1.6.8 1.6 1.8-.7 1.8-1.6 1.8zm5.4 0c-.9 0-1.6-.8-1.6-1.8s.7-1.8 1.6-1.8 1.6.8 1.6 1.8-.7 1.8-1.6 1.8z"
          />
        </svg>
      );
    case "slack":
      return (
        <svg {...common}>
          <rect x="4" y="8.6" width="9" height="2.8" rx="1.4" fill="#36C5F0" />
          <rect x="12.6" y="4" width="2.8" height="9" rx="1.4" fill="#2EB67D" />
          <rect x="11" y="12.6" width="9" height="2.8" rx="1.4" fill="#ECB22E" />
          <rect x="8.6" y="11" width="2.8" height="9" rx="1.4" fill="#E01E5A" />
        </svg>
      );
    case "signal":
      return (
        <svg {...common} fill="none">
          <circle cx="12" cy="12" r="8" stroke="#fff" strokeWidth={2} strokeDasharray="3.4 1.8" />
          <circle cx="12" cy="12" r="4.6" fill="#fff" />
        </svg>
      );
    case "imessage":
      return (
        <svg {...common}>
          <path
            d="M12 4.5c-4.7 0-8.5 3.1-8.5 7 0 2.2 1.2 4.1 3.1 5.4-.2 1.2-.8 2.3-1.7 3.1 1.7 0 3.3-.6 4.5-1.6.8.2 1.7.3 2.6.3 4.7 0 8.5-3.1 8.5-7s-3.8-7.2-8.5-7.2z"
            fill="#fff"
          />
        </svg>
      );
    case "wechat":
      return (
        <svg {...common}>
          <ellipse cx="9.5" cy="10" rx="6.5" ry="5.2" fill="#fff" />
          <ellipse cx="15.5" cy="14.6" rx="5.3" ry="4.3" fill="#fff" stroke="#07C160" strokeWidth={1.2} />
          <circle cx="7.4" cy="9" r="0.9" fill="#07C160" />
          <circle cx="11.4" cy="9" r="0.9" fill="#07C160" />
        </svg>
      );
    default:
      return null;
  }
}

/** Only the two states worth interrupting a calm chart for carry a corner dot. */
const hasDot = (status: ChannelStatus): boolean => status === "connecting" || status === "error";

export function ChannelChip({
  channel,
  label,
  size = 18,
  active = false,
}: {
  channel: OrgChannel;
  /** Accessible name and tooltip, e.g. "WhatsApp · connected". */
  label: string;
  size?: number;
  /** The agent is answering a message that arrived on this connector. */
  active?: boolean;
}) {
  const brand = channelBrand(channel.type);
  const status = channelStatus(channel);
  const bg = BRAND_BG[brand];
  const glyph = Math.round(size * 0.66);
  const Icon = NEUTRAL_ICON[brand] ?? MessageCircle;
  return (
    <span
      className={`org-ch is-${status}${bg ? "" : " is-neutral"}${active ? " is-active" : ""}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28), background: bg }}
      role="img"
      aria-label={label}
      title={label}
    >
      {bg ? <BrandGlyph brand={brand} size={glyph} /> : <Icon size={glyph} strokeWidth={2} aria-hidden />}
      {hasDot(status) && <span className="org-ch-dot" aria-hidden />}
    </span>
  );
}

/**
 * The trailing "+N" chip when a card is too narrow for every connector. It
 * wears the worst status among the connectors it hides (`worstStatus`), so a
 * broken one is never folded out of sight.
 */
export function ChannelOverflowChip({
  count,
  label,
  status,
  size = 18,
}: {
  count: number;
  label: string;
  status: ChannelStatus;
  size?: number;
}) {
  return (
    <span
      className={`org-ch is-more is-${status}`}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28) }}
      role="img"
      aria-label={label}
      title={label}
    >
      +{count}
      {hasDot(status) && <span className="org-ch-dot" aria-hidden />}
    </span>
  );
}
