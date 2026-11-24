// Connector catalog: pure data describing every messaging/connector
// integration's form fields and setup guide. Every user-facing string here
// (`label`, `help`, `placeholder`, select option labels, guide text) is a
// TRANSLATION KEY into the `catalog.*` section of `./strings.ts`, resolved
// via `useT(STR)` at render time — never literal display text. This file
// stays pure data + key references; `ConnectorForm.tsx` and
// `ConnectorGuide.tsx` do the `t()` resolution.
export type ConnectorFieldKind =
  | "text"
  // A credential the agent config never carries: it lives in the vault under
  // `vaultKey`, scoped to the agent, and the connector resolves it at boot.
  // The connector panel edits it against the vault API directly.
  | "vaultSecret"
  | "secret"
  | "number"
  // A number the runtime picks for you unless you insist. Renders an
  // Auto/Manual switch; "Auto" is the empty value, so the key is simply
  // omitted from the saved config and the backend's own allocation wins.
  // Keeps per-agent resource allocation (ports) off the user's plate while
  // leaving an escape hatch for the one install that needs a fixed value.
  | "autoNumber"
  | "bool"
  | "list"
  | "jsonObject"
  | "select"
  | "readonly"
  // The address the runtime already serves this connector on, resolved from
  // the browser's own origin and the agent's name — shown with a copy button
  // instead of asked for. Display-only: it holds no value, so it is skipped
  // everywhere a value is read or written (see `isDisplayOnlyKind`).
  | "endpointUrl";
export type ConnectorFieldGroup = "creds" | "allow" | "behavior" | "advanced";

/**
 * Kinds that render information rather than collect it. They never appear in
 * `initialValues`, never reach `buildConnectorConfig`, never count toward a
 * group's "N set" badge, and are filtered out of the onboarding wizard. One
 * predicate so a new display-only kind cannot be added to some of those and
 * missed in the rest — which is how it would end up as a bare text input the
 * user can type into.
 */
export function isDisplayOnlyKind(kind?: ConnectorFieldKind): boolean {
  return kind === "readonly" || kind === "endpointUrl";
}

export interface ConnectorFieldDef {
  key: string;
  /** Translation key resolved via `t()` — see file header. */
  label: string;
  kind?: ConnectorFieldKind;
  group?: ConnectorFieldGroup;
  extraKey?: string;
  /** Translation key, or (for pure example tokens/URLs) a literal value shown as-is in every language. */
  placeholder?: string;
  /** Translation key. */
  help?: string;
  rows?: number;
  required?: boolean;
  snowflake?: boolean;
  roleList?: boolean;
  /** Minimum accepted value for `kind: "number"` / `"autoNumber"` fields. */
  min?: number;
  /** Maximum accepted value for `kind: "number"` / `"autoNumber"` fields. */
  max?: number;
  /** Translation key for the "leave it to the runtime" choice on `kind: "autoNumber"`. */
  autoLabel?: string;
  /** Value prefilled when the user switches an `autoNumber` to Manual. */
  manualDefault?: string;
  /**
   * Choices for `kind: "select"`. The FIRST option must describe the backend
   * default: an untouched select keeps the form value empty, renders as that
   * first option, and omits the key from the saved config entirely.
   * `label` is a translation key.
   */
  options?: Array<{ value: string; label: string }>;
  /**
   * The vault key this credential lives under, scoped to this agent. Every
   * agent uses the same key name — the per-agent override is what keeps their
   * values apart, and it is what makes two agents two different bots.
   * `olano/config/channel_secrets.py` owns the authoritative map and the
   * runtime resolves exactly this key at boot, so the two must stay in step.
   */
  vaultKey?: string;
  /**
   * Offer to mint the value for `kind: "vaultSecret"` fields whose credential
   * WE issue rather than a remote service — there is nothing to copy from a
   * provider's console, so the alternative is telling the user to go run
   * `openssl` in a terminal. `"hex32"` is 32 random bytes as hex, generated in
   * the browser with `crypto.getRandomValues` and saved through the normal
   * vault write, so the plaintext still never lands in the agent config.
   */
  generate?: "hex32";
  /**
   * For `kind: "vaultSecret"` fields whose connector can authenticate WITHOUT
   * the vault entry (weixin rides the QR-linked login persisted server-side):
   * what the empty state means. Without this the field shows the default red
   * "Not set — this connector will not start", which for such connectors is
   * false and sends users hunting for a token that doesn't exist. Translation key.
   */
  missingHint?: string;
  /**
   * An earlier version of this connector's form saved the field at the top
   * level of the channel, where the runtime never passes it to the adapter
   * (only `extra` and a few named keys survive `PlatformConfig.from_dict`).
   * With this set, an edit reads the old spot when `extra` lacks the key, and
   * the next save writes it under `extraKey`, so nothing the operator typed is
   * lost in the move. The runtime folds the same keys at boot
   * (`runtime/server.py:_LEGACY_TOP_LEVEL_EXTRA`) until then.
   */
  legacyTopLevel?: boolean;
}

export interface ConnectorGuide {
  /** Translation key for the guide's short subject line (also chips[0]). */
  titleKey: string;
  /** Translation key for the one-line summary shown above the steps. */
  summaryKey: string;
  /** Translation keys, one per step body; step titles ("Step N") are generated at render time. */
  stepKeys: string[];
}

export interface ConnectorDefinition {
  type: string;
  /**
   * The picker's key for this row. Equal to `type` for every ordinary
   * connector; a *variant* row (Lark, which rides the `platform:feishu`
   * adapter) is `"<type>@<variant id>"`, so two rows can share one wire type.
   * The saved channel always carries `type`, never the id.
   */
  id: string;
  /** Translation key for the connector's display name in the type dropdown. */
  label: string;
  /**
   * Variant rows only: which variant this is, its short display name
   * (translation key, for connector-list rows that print "Feishu"-style
   * names), and the form values the row fixes on select. The preset is also
   * how a saved connector is matched back to its row when edited, Lark is
   * `{ domain: "lark" }` under `platform:feishu`.
   */
  variant?: { id: string; name: string; preset: Record<string, string> };
  kind: "legacy" | "platform";
  fields: ConnectorFieldDef[];
  guide: ConnectorGuide;
  /**
   * Groups pushed below the rest of the form and rendered collapsed, because
   * this connector's primary setup path never touches them — WhatsApp links by
   * QR, so its bridge script/port/session knobs are overrides, not the happy
   * path. A group the agent has already set still renders open, so an existing
   * override (or a bad one that needs fixing) is never hidden.
   */
  secondaryGroups: ConnectorFieldGroup[];
  /**
   * The tools this connector brings, listed in its panel with an on/off
   * switch each (see `ConnectorToolDef`). Empty for a connector whose only
   * job is the conversation itself.
   */
  tools: ConnectorToolDef[];
}

/**
 * A tool a connector brings, with its switch in the connector panel. The
 * everyday ones come with the connector: on for every agent that has it
 * unless `disabled_tools` names them. An `admin` one acts on the whole
 * account behind the connector (every agent on it shares the result), so it
 * is off until an administrator lists it in the agent's `tools`.
 * `connectorToolSwitches` / `applyConnectorToolSwitches` are the two rules.
 */
export interface ConnectorToolDef {
  name: string;
  /** Translation key of the one-line description shown beside the switch. */
  help: string;
  admin?: boolean;
}

export interface CatalogChannel {
  type: string;
  label?: string;
  kind?: string;
  env_vars?: string[];
  /** Duplicated/superseded connector — hidden from the picker for new connectors. */
  deprecated?: boolean;
  /** Boots but is unverified — also hidden from the picker for new connectors. */
  hidden?: boolean;
  note?: string;
  /**
   * Extra picker rows this adapter earns (`platforms/catalog.py:_VARIANTS`):
   * same type and credentials, a preset of `extra` keys the form fixes.
   */
  variants?: Array<{ id: string; label?: string; preset: Record<string, string> }>;
}

export interface CatalogChannelsResponse {
  channels: CatalogChannel[];
}

export type ConnectorFormValue = string | boolean;
export type ConnectorFormValues = Record<string, ConnectorFormValue>;

/** Translation-function shape used to resolve catalog text and build validation messages. */
export type CatalogT = (key: string, vars?: Record<string, string>) => string;

/** Translation keys — resolve with `t()` before rendering. */
export const CONNECTOR_GROUP_LABELS: Record<ConnectorFieldGroup, string> = {
  creds: "catalog.group.creds",
  allow: "catalog.group.allow",
  behavior: "catalog.group.behavior",
  advanced: "catalog.group.advanced",
};

// The tools each connector lists in its panel (`ConnectorDefinition.tools`).
// Declared above the definitions for the same reason as SECONDARY_GROUPS.
const twilioTool = (name: string, admin = false): ConnectorToolDef => ({
  name,
  help: `catalog.whatsappTwilio.tool.${name}`,
  admin,
});
// The Meta Cloud API connector lists the same tools (they are one toolkit and
// work on either connector), minus the two that read a Twilio account.
const metaTool = (name: string, admin = false): ConnectorToolDef => ({
  name,
  help: `catalog.whatsappMetaApi.tool.${name}`,
  admin,
});
const CONNECTOR_TOOLS: Record<string, ConnectorToolDef[]> = {
  "platform:whatsapp_meta_api": [
    metaTool("whatsapp_template_list"),
    metaTool("whatsapp_template_send"),
    metaTool("whatsapp_message_status"),
    metaTool("whatsapp_window_status"),
    metaTool("whatsapp_template_create", true),
    metaTool("whatsapp_template_submit", true),
    metaTool("whatsapp_template_status", true),
    metaTool("whatsapp_template_delete", true),
    metaTool("whatsapp_sender_status", true),
    metaTool("whatsapp_profile_update", true),
  ],
  "platform:whatsapp_twilio": [
    twilioTool("whatsapp_template_list"),
    twilioTool("whatsapp_template_send"),
    twilioTool("whatsapp_message_status"),
    twilioTool("whatsapp_window_status"),
    twilioTool("whatsapp_template_create", true),
    twilioTool("whatsapp_template_submit", true),
    twilioTool("whatsapp_template_status", true),
    twilioTool("whatsapp_template_delete", true),
    twilioTool("whatsapp_sender_status", true),
    twilioTool("whatsapp_profile_update", true),
    twilioTool("twilio_account_balance", true),
    twilioTool("twilio_usage_summary", true),
  ],
};

/**
 * See `ConnectorDefinition.secondaryGroups`. Declared above the definitions
 * because `def()` reads it while the module's `CONNECTOR_DEFINITIONS` array is
 * being evaluated — a `const` further down the file is still in its temporal
 * dead zone by then, which throws and takes the whole view down.
 */
const SECONDARY_GROUPS: Record<string, ConnectorFieldGroup[]> = {
  "platform:whatsapp": ["creds"],
  // WeChat links by QR (the live card above the form) — the bot-id/token
  // fields are the advanced manual fallback, so they render collapsed below,
  // exactly like WhatsApp's bridge overrides. A field the agent already set
  // still renders open (see ConnectorForm), so the auto-pinned bot_id stays
  // visible when editing a paired connector.
  "platform:weixin": ["creds"],
};

function field(
  key: string,
  label: string,
  group: ConnectorFieldGroup,
  opts: Omit<ConnectorFieldDef, "key" | "label" | "group"> = {},
): ConnectorFieldDef {
  return { key, label, group, ...opts };
}

const classicTelegram: ConnectorFieldDef[] = [
  field("token", "catalog.telegram.token.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${TELEGRAM_BOT_TOKEN}",
    vaultKey: "TELEGRAM_BOT_TOKEN",
    help: "catalog.telegram.token.help",
  }),
  field("allowed_users", "catalog.telegramClassic.allowedUsers.label", "allow", {
    kind: "list",
    rows: 3,
    placeholder: "123456789",
    help: "catalog.telegramClassic.allowedUsers.help",
  }),
];

const classicDiscord: ConnectorFieldDef[] = [
  field("token", "catalog.discordClassic.token.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${DISCORD_BOT_TOKEN}",
    vaultKey: "DISCORD_BOT_TOKEN",
    help: "catalog.discordClassic.token.help",
  }),
  field("guild_ids", "catalog.discordClassic.guildIds.label", "allow", {
    kind: "list",
    rows: 3,
    snowflake: true,
    placeholder: "1484827831707107300",
    help: "catalog.discordClassic.guildIds.help",
  }),
  field("allowed_channels", "catalog.discordClassic.allowedChannels.label", "allow", {
    kind: "list",
    rows: 3,
    snowflake: true,
    placeholder: "1484827833078513700",
    help: "catalog.discordClassic.allowedChannels.help",
  }),
  field("allowed_roles", "catalog.discordClassic.allowedRoles.label", "allow", {
    kind: "list",
    rows: 2,
    roleList: true,
    placeholder: "agent-users",
    help: "catalog.discordClassic.allowedRoles.help",
  }),
];

const classicSlack: ConnectorFieldDef[] = [
  field("bot_token", "catalog.slackClassic.botToken.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${SLACK_BOT_TOKEN}",
    vaultKey: "SLACK_BOT_TOKEN",
    help: "catalog.slackClassic.botToken.help",
  }),
  field("app_token", "catalog.slackClassic.appToken.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${SLACK_APP_TOKEN}",
    vaultKey: "SLACK_APP_TOKEN",
    help: "catalog.slackClassic.appToken.help",
  }),
];

const classicWhatsapp: ConnectorFieldDef[] = [
  field("bridge_script", "catalog.whatsappClassic.bridgeScript.label", "creds", {
    placeholder: "whatsapp-bridge/index.js",
    help: "catalog.whatsappClassic.bridgeScript.help",
  }),
];

const emailFields: ConnectorFieldDef[] = [
  field("imap_host", "catalog.email.imapHost.label", "creds", {
    placeholder: "imap.gmail.com",
    required: true,
    help: "catalog.email.imapHost.help",
  }),
  field("imap_port", "catalog.email.imapPort.label", "creds", {
    kind: "number",
    placeholder: "993",
    help: "catalog.email.imapPort.help",
  }),
  field("imap_user", "catalog.email.imapUser.label", "creds", {
    placeholder: "you@gmail.com",
    required: true,
    help: "catalog.email.imapUser.help",
  }),
  field("imap_password", "catalog.email.imapPassword.label", "creds", {
    kind: "vaultSecret",
    placeholder: "${GMAIL_APP_PASSWORD}",
    vaultKey: "EMAIL_IMAP_PASSWORD",
    required: true,
    help: "catalog.email.imapPassword.help",
  }),
  field("smtp_host", "catalog.email.smtpHost.label", "creds", {
    placeholder: "smtp.gmail.com",
    help: "catalog.email.smtpHost.help",
  }),
  field("smtp_port", "catalog.email.smtpPort.label", "creds", {
    kind: "number",
    placeholder: "587",
    help: "catalog.email.smtpPort.help",
  }),
  field("poll_interval_seconds", "catalog.email.pollIntervalSeconds.label", "behavior", {
    kind: "number",
    placeholder: "60",
    help: "catalog.email.pollIntervalSeconds.help",
  }),
  field("default_recipient", "catalog.email.defaultRecipient.label", "behavior", {
    placeholder: "you@gmail.com",
    help: "catalog.email.defaultRecipient.help",
  }),
  field("allowed_senders", "catalog.email.allowedSenders.label", "allow", {
    kind: "list",
    rows: 2,
    placeholder: "friend@example.com",
    help: "catalog.email.allowedSenders.help",
  }),
];

const platformTelegram: ConnectorFieldDef[] = [
  ...classicTelegram.slice(0, 1),
  field("allowed_users", "catalog.telegram.allowedUsers.label", "allow", {
    kind: "list",
    extraKey: "allowed_users",
    rows: 3,
    placeholder: "123456789\n@yourhandle",
    help: "catalog.telegram.allowedUsers.help",
  }),
  field("owner_users", "catalog.telegram.ownerUsers.label", "allow", {
    kind: "list",
    extraKey: "owner_users",
    rows: 2,
    placeholder: "123456789",
    help: "catalog.telegram.ownerUsers.help",
  }),
  field("free_response_chats", "catalog.telegram.freeResponseChats.label", "behavior", {
    kind: "list",
    extraKey: "free_response_chats",
    rows: 2,
    placeholder: "-1001234567890",
    help: "catalog.telegram.freeResponseChats.help",
  }),
  field("group_respond", "catalog.telegram.groupRespond.label", "behavior", {
    kind: "select",
    extraKey: "group_respond",
    options: [
      { value: "all", label: "catalog.telegram.groupRespond.opt.all" },
      { value: "mentions", label: "catalog.telegram.groupRespond.opt.mentions" },
      { value: "none", label: "catalog.telegram.groupRespond.opt.none" },
    ],
    help: "catalog.telegram.groupRespond.help",
  }),
  field("group_listen", "catalog.telegram.groupListen.label", "behavior", {
    kind: "select",
    extraKey: "group_listen",
    options: [
      { value: "off", label: "catalog.telegram.groupListen.opt.off" },
      { value: "all", label: "catalog.telegram.groupListen.opt.all" },
      { value: "mentions", label: "catalog.telegram.groupListen.opt.mentions" },
    ],
    help: "catalog.telegram.groupListen.help",
  }),
  // ── Telegram Business (secretary mode) ─────────────────────────────
  field("business_mode", "catalog.telegram.businessMode.label", "behavior", {
    kind: "bool",
    extraKey: "business_mode",
    help: "catalog.telegram.businessMode.help",
  }),
  field("business_auto_reply", "catalog.telegram.businessAutoReply.label", "behavior", {
    kind: "bool",
    extraKey: "business_auto_reply",
    help: "catalog.telegram.businessAutoReply.help",
  }),
  field("business_thinking_message", "catalog.telegram.businessThinkingMessage.label", "behavior", {
    kind: "bool",
    extraKey: "business_thinking_message",
    help: "catalog.telegram.businessThinkingMessage.help",
  }),
  field("business_reply_delay_seconds", "catalog.telegram.businessReplyDelaySeconds.label", "behavior", {
    kind: "number",
    extraKey: "business_reply_delay_seconds",
    placeholder: "30",
    min: 0,
    help: "catalog.telegram.businessReplyDelaySeconds.help",
  }),
  field("business_allowed_customers", "catalog.telegram.businessAllowedCustomers.label", "allow", {
    kind: "list",
    extraKey: "business_allowed_customers",
    rows: 2,
    placeholder: "123456789\n@customer",
    help: "catalog.telegram.businessAllowedCustomers.help",
  }),
  // ── Agent-to-agent (bot-to-bot) messaging ──────────────────────────
  field("allow_bot_messages", "catalog.telegram.allowBotMessages.label", "behavior", {
    kind: "bool",
    extraKey: "allow_bot_messages",
    help: "catalog.telegram.allowBotMessages.help",
  }),
  field("allowed_bot_usernames", "catalog.telegram.allowedBotUsernames.label", "allow", {
    kind: "list",
    extraKey: "allowed_bot_usernames",
    rows: 2,
    placeholder: "@peer_agent_bot",
    help: "catalog.telegram.allowedBotUsernames.help",
  }),
  // ── Business account management (all default off) ──────────────────
  field("business_manage_profile", "catalog.telegram.businessManageProfile.label", "advanced", {
    kind: "bool",
    extraKey: "business_manage_profile",
    help: "catalog.telegram.businessManageProfile.help",
  }),
  field("business_manage_gifts", "catalog.telegram.businessManageGifts.label", "advanced", {
    kind: "bool",
    extraKey: "business_manage_gifts",
    help: "catalog.telegram.businessManageGifts.help",
  }),
  field("manage_bots", "catalog.telegram.manageBots.label", "advanced", {
    kind: "bool",
    extraKey: "manage_bots",
    help: "catalog.telegram.manageBots.help",
  }),
  field("bot_loop_max_turns", "catalog.telegram.botLoopMaxTurns.label", "advanced", {
    kind: "number",
    extraKey: "bot_loop_max_turns",
    placeholder: "6",
    help: "catalog.telegram.botLoopMaxTurns.help",
  }),
  field("bot_rate_limit_per_minute", "catalog.telegram.botRateLimitPerMinute.label", "advanced", {
    kind: "number",
    extraKey: "bot_rate_limit_per_minute",
    placeholder: "10",
    help: "catalog.telegram.botRateLimitPerMinute.help",
  }),
  // ── Routing extras the adapter reads (previously config-file only) ─
  field("ignored_threads", "catalog.telegram.ignoredThreads.label", "advanced", {
    kind: "list",
    extraKey: "ignored_threads",
    rows: 2,
    placeholder: "42",
    help: "catalog.telegram.ignoredThreads.help",
  }),
  field("mention_patterns", "catalog.telegram.mentionPatterns.label", "advanced", {
    kind: "list",
    extraKey: "mention_patterns",
    rows: 2,
    placeholder: "^merli\\b",
    help: "catalog.telegram.mentionPatterns.help",
  }),
  field("base_url", "catalog.telegram.baseUrl.label", "advanced", {
    extraKey: "base_url",
    placeholder: "https://api.telegram.org/bot",
    help: "catalog.telegram.baseUrl.help",
  }),
];

const platformDiscord: ConnectorFieldDef[] = [
  field("token", "catalog.discord.token.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${DISCORD_BOT_TOKEN}",
    vaultKey: "DISCORD_BOT_TOKEN",
    help: "catalog.discord.token.help",
  }),
  field("guild_ids", "catalog.discord.guildIds.label", "allow", {
    kind: "list",
    extraKey: "guild_ids",
    snowflake: true,
    rows: 3,
    placeholder: "1484827831707107300",
    help: "catalog.discord.guildIds.help",
  }),
  field("allowed_channels", "catalog.discord.allowedChannels.label", "allow", {
    kind: "list",
    extraKey: "allowed_channels",
    snowflake: true,
    rows: 3,
    placeholder: "1484827833078513700",
    help: "catalog.discord.allowedChannels.help",
  }),
  field("allowed_roles", "catalog.discord.allowedRoles.label", "allow", {
    kind: "list",
    extraKey: "allowed_roles",
    snowflake: true,
    rows: 2,
    placeholder: "1484827834012345600",
    help: "catalog.discord.allowedRoles.help",
  }),
  field("group_respond", "catalog.discord.groupRespond.label", "behavior", {
    kind: "select",
    extraKey: "group_respond",
    options: [
      { value: "mentions", label: "catalog.discord.groupRespond.opt.mentions" },
      { value: "all", label: "catalog.discord.groupRespond.opt.all" },
      { value: "none", label: "catalog.discord.groupRespond.opt.none" },
    ],
    help: "catalog.discord.groupRespond.help",
  }),
  field("group_listen", "catalog.discord.groupListen.label", "behavior", {
    kind: "select",
    extraKey: "group_listen",
    options: [
      { value: "off", label: "catalog.discord.groupListen.opt.off" },
      { value: "all", label: "catalog.discord.groupListen.opt.all" },
      { value: "mentions", label: "catalog.discord.groupListen.opt.mentions" },
    ],
    help: "catalog.discord.groupListen.help",
  }),
];

const platformSlack: ConnectorFieldDef[] = [
  field("token", "catalog.slack.token.label", "creds", {
    kind: "vaultSecret",
    required: true,
    placeholder: "${SLACK_BOT_TOKEN}",
    vaultKey: "SLACK_BOT_TOKEN",
    help: "catalog.slack.token.help",
  }),
  field("app_token", "catalog.slack.appToken.label", "creds", {
    kind: "vaultSecret",
    required: true,
    extraKey: "app_token",
    placeholder: "${SLACK_APP_TOKEN}",
    vaultKey: "SLACK_APP_TOKEN",
    help: "catalog.slack.appToken.help",
  }),
  field("allowed_users", "catalog.slack.allowedUsers.label", "allow", {
    kind: "list",
    extraKey: "allowed_users",
    rows: 2,
    placeholder: "U07ABCDEFG",
    help: "catalog.slack.allowedUsers.help",
  }),
  field("group_respond", "catalog.slack.groupRespond.label", "behavior", {
    kind: "select",
    extraKey: "group_respond",
    options: [
      { value: "mentions", label: "catalog.slack.groupRespond.opt.mentions" },
      { value: "all", label: "catalog.slack.groupRespond.opt.all" },
      { value: "none", label: "catalog.slack.groupRespond.opt.none" },
    ],
    help: "catalog.slack.groupRespond.help",
  }),
  field("group_listen", "catalog.slack.groupListen.label", "behavior", {
    kind: "select",
    extraKey: "group_listen",
    options: [
      { value: "off", label: "catalog.slack.groupListen.opt.off" },
      { value: "all", label: "catalog.slack.groupListen.opt.all" },
      { value: "mentions", label: "catalog.slack.groupListen.opt.mentions" },
    ],
    help: "catalog.slack.groupListen.help",
  }),
];

const platformWhatsapp: ConnectorFieldDef[] = [
  field("bridge_warning", "catalog.whatsapp.bridgeWarning.label", "creds", {
    kind: "readonly",
    help: "catalog.whatsapp.bridgeWarning.help",
  }),
  field("bridge_script", "catalog.whatsapp.bridgeScript.label", "creds", {
    extraKey: "bridge_script",
    placeholder: "whatsapp-bridge/index.js",
    help: "catalog.whatsapp.bridgeScript.help",
  }),
  field("bridge_port", "catalog.whatsapp.bridgePort.label", "creds", {
    kind: "autoNumber",
    extraKey: "bridge_port",
    placeholder: "3006",
    min: 1024,
    max: 65535,
    autoLabel: "catalog.whatsapp.bridgePort.autoLabel",
    manualDefault: "3006",
    help: "catalog.whatsapp.bridgePort.help",
  }),
  field("session_path", "catalog.whatsapp.sessionPath.label", "creds", {
    extraKey: "session_path",
    placeholder: "~/.olano/platforms/whatsapp/session",
    help: "catalog.whatsapp.sessionPath.help",
  }),
  field("allowed_users", "catalog.whatsapp.allowedUsers.label", "allow", {
    kind: "list",
    extraKey: "allowed_users",
    rows: 3,
    placeholder: "+12025550188\n6512345678@s.whatsapp.net",
    help: "catalog.whatsapp.allowedUsers.help",
  }),
  field("owner_users", "catalog.whatsapp.ownerUsers.label", "allow", {
    kind: "list",
    extraKey: "owner_users",
    rows: 2,
    placeholder: "+12025550188",
    help: "catalog.whatsapp.ownerUsers.help",
  }),
  field("allowed_chats", "catalog.whatsapp.allowedChats.label", "allow", {
    kind: "list",
    extraKey: "allowed_chats",
    rows: 3,
    placeholder: "120363012345678901@g.us",
    help: "catalog.whatsapp.allowedChats.help",
  }),
  field("group_respond", "catalog.whatsapp.groupRespond.label", "behavior", {
    kind: "select",
    extraKey: "group_respond",
    options: [
      // Kept short so none truncates in the mobile select; the detail lives
      // in the help text, which wraps.
      { value: "all", label: "catalog.whatsapp.groupRespond.opt.all" },
      { value: "mentions", label: "catalog.whatsapp.groupRespond.opt.mentions" },
      { value: "none", label: "catalog.whatsapp.groupRespond.opt.none" },
    ],
    help: "catalog.whatsapp.groupRespond.help",
  }),
  field("group_listen", "catalog.whatsapp.groupListen.label", "behavior", {
    kind: "select",
    extraKey: "group_listen",
    options: [
      { value: "off", label: "catalog.whatsapp.groupListen.opt.off" },
      { value: "all", label: "catalog.whatsapp.groupListen.opt.all" },
      { value: "mentions", label: "catalog.whatsapp.groupListen.opt.mentions" },
    ],
    help: "catalog.whatsapp.groupListen.help",
  }),
  field("listen_only", "catalog.whatsapp.listenOnly.label", "behavior", {
    kind: "bool",
    extraKey: "listen_only",
    help: "catalog.whatsapp.listenOnly.help",
  }),
  // Read by runtime/server.py (platform handler) through
  // platforms/chat_approval.py; the reply is intercepted in
  // platforms/base.py:handle_message before the busy-session guard.
  field("chat_approvals", "catalog.whatsapp.chatApprovals.label", "behavior", {
    kind: "bool",
    extraKey: "chat_approvals",
    help: "catalog.whatsapp.chatApprovals.help",
  }),
  field("free_response_chats", "catalog.whatsapp.freeResponseChats.label", "behavior", {
    kind: "list",
    extraKey: "free_response_chats",
    rows: 2,
    placeholder: "120363012345678901@g.us",
    help: "catalog.whatsapp.freeResponseChats.help",
  }),
  field("mention_patterns", "catalog.whatsapp.mentionPatterns.label", "behavior", {
    kind: "list",
    extraKey: "mention_patterns",
    rows: 2,
    placeholder: "^merli\\b\n^hey bot",
    help: "catalog.whatsapp.mentionPatterns.help",
  }),
  field("reply_prefix", "catalog.whatsapp.replyPrefix.label", "behavior", {
    extraKey: "reply_prefix",
    placeholder: "[bot] ",
    help: "catalog.whatsapp.replyPrefix.help",
  }),
];

const platformFields: Record<string, ConnectorFieldDef[]> = {
  "platform:telegram": platformTelegram,
  "platform:discord": platformDiscord,
  "platform:slack": platformSlack,
  "platform:whatsapp": platformWhatsapp,
  // Every plain field lives under `extra` (the adapter reads nothing else);
  // `legacyTopLevel` carries values the first version of this form saved at
  // the top level, where they never reached the adapter.
  "platform:whatsapp_twilio": [
    field("account_sid", "catalog.whatsappTwilio.accountSid.label", "creds", {
      extraKey: "account_sid",
      legacyTopLevel: true,
      required: true,
      placeholder: "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      help: "catalog.whatsappTwilio.accountSid.help",
    }),
    field("auth_token", "catalog.whatsappTwilio.authToken.label", "creds", {
      kind: "vaultSecret",
      required: true,
      placeholder: "${TWILIO_AUTH_TOKEN}",
      vaultKey: "TWILIO_AUTH_TOKEN",
      help: "catalog.whatsappTwilio.authToken.help",
    }),
    field("from_number", "catalog.whatsappTwilio.fromNumber.label", "creds", {
      extraKey: "from_number",
      legacyTopLevel: true,
      required: true,
      placeholder: "+14155238886",
      help: "catalog.whatsappTwilio.fromNumber.help",
    }),
    // Nothing to fill in: the adapter registers the runtime's own public
    // address with Twilio at connect (platforms/whatsapp_twilio.py). Said out
    // loud because every other Twilio integration asks for a webhook URL, and
    // an operator looking for that field should find why there is none.
    field("inbound", "catalog.whatsappTwilio.inbound.label", "creds", {
      kind: "readonly",
      help: "catalog.whatsappTwilio.inbound.help",
    }),
    // No template field: outside WhatsApp's 24-hour window the agent picks an
    // approved template itself (tools/whatsapp_template_tools.py), and an
    // ordinary message there is refused with the reason.
    field("service_sid", "catalog.whatsappTwilio.serviceSid.label", "advanced", {
      extraKey: "service_sid",
      legacyTopLevel: true,
      placeholder: "ISxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      help: "catalog.whatsappTwilio.serviceSid.help",
    }),
    field("mcs_region", "catalog.whatsappTwilio.mcsRegion.label", "advanced", {
      extraKey: "mcs_region",
      legacyTopLevel: true,
      placeholder: "us1",
      help: "catalog.whatsappTwilio.mcsRegion.help",
    }),
    field("allowed_users", "catalog.whatsappTwilio.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      legacyTopLevel: true,
      rows: 3,
      placeholder: "+15551234567",
      help: "catalog.whatsappTwilio.allowedUsers.help",
    }),
    // Read by runtime/server.py (the only trust source on this connector;
    // allowed_users is a customer list here) and admitted by the adapter's
    // _is_allowed even when allowed_users omits it.
    field("owner_users", "catalog.whatsappTwilio.ownerUsers.label", "allow", {
      kind: "list",
      extraKey: "owner_users",
      rows: 2,
      placeholder: "+15551234567",
      help: "catalog.whatsappTwilio.ownerUsers.help",
    }),
    field("chat_approvals", "catalog.whatsappTwilio.chatApprovals.label", "behavior", {
      kind: "bool",
      extraKey: "chat_approvals",
      help: "catalog.whatsappTwilio.chatApprovals.help",
    }),
  ],
  // Meta's own Cloud API (platforms/whatsapp_meta_api.py). The token and the
  // app secret are vaulted (config/channel_secrets.py); the two IDs are plain
  // settings under `extra`. Nothing to paste into Meta: the adapter points the
  // number's webhooks at this agent at connect.
  "platform:whatsapp_meta_api": [
    field("access_token", "catalog.whatsappMetaApi.accessToken.label", "creds", {
      kind: "vaultSecret",
      required: true,
      placeholder: "${WHATSAPP_META_ACCESS_TOKEN}",
      vaultKey: "WHATSAPP_META_ACCESS_TOKEN",
      help: "catalog.whatsappMetaApi.accessToken.help",
    }),
    field("waba_id", "catalog.whatsappMetaApi.wabaId.label", "creds", {
      extraKey: "waba_id",
      required: true,
      placeholder: "102290129340398",
      help: "catalog.whatsappMetaApi.wabaId.help",
    }),
    field("phone_number_id", "catalog.whatsappMetaApi.phoneNumberId.label", "creds", {
      extraKey: "phone_number_id",
      required: true,
      placeholder: "106540352242922",
      help: "catalog.whatsappMetaApi.phoneNumberId.help",
    }),
    field("app_secret", "catalog.whatsappMetaApi.appSecret.label", "creds", {
      kind: "vaultSecret",
      placeholder: "${WHATSAPP_META_APP_SECRET}",
      vaultKey: "WHATSAPP_META_APP_SECRET",
      missingHint: "catalog.whatsappMetaApi.appSecret.missingHint",
      help: "catalog.whatsappMetaApi.appSecret.help",
    }),
    field("inbound", "catalog.whatsappMetaApi.inbound.label", "creds", {
      kind: "readonly",
      help: "catalog.whatsappMetaApi.inbound.help",
    }),
    field("allowed_users", "catalog.whatsappMetaApi.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 3,
      placeholder: "+6591234567",
      help: "catalog.whatsappMetaApi.allowedUsers.help",
    }),
    field("owner_users", "catalog.whatsappMetaApi.ownerUsers.label", "allow", {
      kind: "list",
      extraKey: "owner_users",
      rows: 2,
      placeholder: "+6591234567",
      help: "catalog.whatsappMetaApi.ownerUsers.help",
    }),
    field("chat_approvals", "catalog.whatsappMetaApi.chatApprovals.label", "behavior", {
      kind: "bool",
      extraKey: "chat_approvals",
      help: "catalog.whatsappMetaApi.chatApprovals.help",
    }),
    field("mark_read", "catalog.whatsappMetaApi.markRead.label", "behavior", {
      kind: "select",
      extraKey: "mark_read",
      options: [
        { value: "true", label: "catalog.whatsappMetaApi.markRead.opt.on" },
        { value: "false", label: "catalog.whatsappMetaApi.markRead.opt.off" },
      ],
      help: "catalog.whatsappMetaApi.markRead.help",
    }),
    field("quote_replies", "catalog.whatsappMetaApi.quoteReplies.label", "behavior", {
      kind: "select",
      extraKey: "quote_replies",
      options: [
        { value: "false", label: "catalog.whatsappMetaApi.quoteReplies.opt.off" },
        { value: "true", label: "catalog.whatsappMetaApi.quoteReplies.opt.on" },
      ],
      help: "catalog.whatsappMetaApi.quoteReplies.help",
    }),
    field("graph_version", "catalog.whatsappMetaApi.graphVersion.label", "advanced", {
      extraKey: "graph_version",
      placeholder: "v25.0",
      help: "catalog.whatsappMetaApi.graphVersion.help",
    }),
  ],
  "platform:email": [
    field("env_help", "catalog.emailPlatform.envHelp.label", "creds", {
      kind: "readonly",
      help: "catalog.emailPlatform.envHelp.help",
    }),
    field("allowed_users", "catalog.emailPlatform.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "friend@example.com",
      help: "catalog.emailPlatform.allowedUsers.help",
    }),
  ],
  "platform:signal": [
    field("http_url", "catalog.signal.httpUrl.label", "creds", {
      extraKey: "http_url",
      required: true,
      placeholder: "http://127.0.0.1:8080",
      help: "catalog.signal.httpUrl.help",
    }),
    field("account", "catalog.signal.account.label", "creds", {
      extraKey: "account",
      required: true,
      placeholder: "+15551234567",
      help: "catalog.signal.account.help",
    }),
    field("ignore_stories", "catalog.signal.ignoreStories.label", "behavior", {
      kind: "select",
      extraKey: "ignore_stories",
      options: [
        { value: "true", label: "catalog.signal.ignoreStories.opt.true" },
        { value: "false", label: "catalog.signal.ignoreStories.opt.false" },
      ],
      help: "catalog.signal.ignoreStories.help",
    }),
    field("manage_daemon", "catalog.signal.manageDaemon.label", "behavior", {
      kind: "bool",
      extraKey: "manage_daemon",
      help: "catalog.signal.manageDaemon.help",
    }),
    field("signal_cli_path", "catalog.signal.signalCliPath.label", "advanced", {
      extraKey: "signal_cli_path",
      placeholder: "signal-cli",
      help: "catalog.signal.signalCliPath.help",
    }),
    field("data_dir", "catalog.signal.dataDir.label", "advanced", {
      extraKey: "data_dir",
      placeholder: "/home/you/.local/share/signal-cli",
      help: "catalog.signal.dataDir.help",
    }),
  ],
  "platform:matrix": [
    field("homeserver", "catalog.matrix.homeserver.label", "creds", {
      extraKey: "homeserver",
      required: true,
      placeholder: "https://matrix.example.org",
      help: "catalog.matrix.homeserver.help",
    }),
    field("token", "catalog.matrix.token.label", "creds", {
      kind: "vaultSecret",
      required: true,
      placeholder: "${MATRIX_ACCESS_TOKEN}",
      vaultKey: "MATRIX_ACCESS_TOKEN",
      help: "catalog.matrix.token.help",
    }),
    field("user_id", "catalog.matrix.userId.label", "creds", {
      extraKey: "user_id",
      placeholder: "@bot:example.org",
      help: "catalog.matrix.userId.help",
    }),
    field("encryption", "catalog.matrix.encryption.label", "behavior", {
      kind: "bool",
      extraKey: "encryption",
      help: "catalog.matrix.encryption.help",
    }),
    field("device_id", "catalog.matrix.deviceId.label", "behavior", {
      extraKey: "device_id",
      placeholder: "LIONCLAW-MERLI",
      help: "catalog.matrix.deviceId.help",
    }),
    field("allowed_users", "catalog.matrix.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "@alice:example.org",
      help: "catalog.matrix.allowedUsers.help",
    }),
    field("default_chat_id", "catalog.matrix.defaultChatId.label", "behavior", {
      extraKey: "default_chat_id",
      placeholder: "!abcdef:example.org",
      help: "catalog.matrix.defaultChatId.help",
    }),
    field("group_respond", "catalog.matrix.groupRespond.label", "behavior", {
      kind: "select",
      extraKey: "group_respond",
      options: [
        { value: "mentions", label: "catalog.matrix.groupRespond.opt.mentions" },
        { value: "all", label: "catalog.matrix.groupRespond.opt.all" },
        { value: "none", label: "catalog.matrix.groupRespond.opt.none" },
      ],
      help: "catalog.matrix.groupRespond.help",
    }),
    field("group_listen", "catalog.matrix.groupListen.label", "behavior", {
      kind: "select",
      extraKey: "group_listen",
      options: [
        { value: "off", label: "catalog.matrix.groupListen.opt.off" },
        { value: "all", label: "catalog.matrix.groupListen.opt.all" },
        { value: "mentions", label: "catalog.matrix.groupListen.opt.mentions" },
      ],
      help: "catalog.matrix.groupListen.help",
    }),
  ],
  "platform:mattermost": [
    field("url", "catalog.mattermost.url.label", "creds", {
      extraKey: "url",
      required: true,
      placeholder: "https://chat.example.com",
      help: "catalog.mattermost.url.help",
    }),
    field("token", "catalog.mattermost.token.label", "creds", {
      kind: "vaultSecret",
      required: true,
      placeholder: "${MATTERMOST_TOKEN}",
      vaultKey: "MATTERMOST_TOKEN",
      help: "catalog.mattermost.token.help",
    }),
    field("allowed_users", "catalog.mattermost.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "7abc1de2fgh3ij4klm5nop6qr\n@alice",
      help: "catalog.mattermost.allowedUsers.help",
    }),
    field("default_chat_id", "catalog.mattermost.defaultChatId.label", "behavior", {
      extraKey: "default_chat_id",
      placeholder: "catalog.mattermost.defaultChatId.placeholder",
      help: "catalog.mattermost.defaultChatId.help",
    }),
    field("reply_mode", "catalog.mattermost.replyMode.label", "behavior", {
      kind: "select",
      extraKey: "reply_mode",
      options: [
        { value: "off", label: "catalog.mattermost.replyMode.opt.off" },
        { value: "thread", label: "catalog.mattermost.replyMode.opt.thread" },
      ],
      help: "catalog.mattermost.replyMode.help",
    }),
    field("group_respond", "catalog.mattermost.groupRespond.label", "behavior", {
      kind: "select",
      extraKey: "group_respond",
      options: [
        { value: "mentions", label: "catalog.mattermost.groupRespond.opt.mentions" },
        { value: "all", label: "catalog.mattermost.groupRespond.opt.all" },
        { value: "none", label: "catalog.mattermost.groupRespond.opt.none" },
      ],
      help: "catalog.mattermost.groupRespond.help",
    }),
    field("group_listen", "catalog.mattermost.groupListen.label", "behavior", {
      kind: "select",
      extraKey: "group_listen",
      options: [
        { value: "off", label: "catalog.mattermost.groupListen.opt.off" },
        { value: "all", label: "catalog.mattermost.groupListen.opt.all" },
        { value: "mentions", label: "catalog.mattermost.groupListen.opt.mentions" },
      ],
      help: "catalog.mattermost.groupListen.help",
    }),
  ],
  "platform:homeassistant": [
    field("url", "catalog.homeassistant.url.label", "creds", {
      extraKey: "url",
      required: true,
      placeholder: "http://homeassistant.local:8123",
      help: "catalog.homeassistant.url.help",
    }),
    field("token", "catalog.homeassistant.token.label", "creds", {
      kind: "secret",
      required: true,
      placeholder: "${HASS_TOKEN}",
      help: "catalog.homeassistant.token.help",
    }),
    field("watch_all", "catalog.homeassistant.watchAll.label", "behavior", {
      kind: "bool",
      extraKey: "watch_all",
      help: "catalog.homeassistant.watchAll.help",
    }),
    field("watch_domains", "catalog.homeassistant.watchDomains.label", "behavior", {
      kind: "list",
      extraKey: "watch_domains",
      rows: 2,
      placeholder: "binary_sensor\nperson",
      help: "catalog.homeassistant.watchDomains.help",
    }),
    field("watch_entities", "catalog.homeassistant.watchEntities.label", "behavior", {
      kind: "list",
      extraKey: "watch_entities",
      rows: 2,
      placeholder: "binary_sensor.front_door",
      help: "catalog.homeassistant.watchEntities.help",
    }),
    field("ignore_entities", "catalog.homeassistant.ignoreEntities.label", "behavior", {
      kind: "list",
      extraKey: "ignore_entities",
      rows: 2,
      placeholder: "sensor.noisy_power_meter",
      help: "catalog.homeassistant.ignoreEntities.help",
    }),
    field("cooldown_seconds", "catalog.homeassistant.cooldownSeconds.label", "behavior", {
      kind: "number",
      extraKey: "cooldown_seconds",
      placeholder: "30",
      help: "catalog.homeassistant.cooldownSeconds.help",
    }),
  ],
  "platform:sms": [
    field("twilio_help", "catalog.sms.twilioHelp.label", "creds", {
      kind: "readonly",
      help: "catalog.sms.twilioHelp.help",
    }),
    field("webhook_url", "catalog.sms.webhookUrl.label", "creds", {
      extraKey: "webhook_url",
      required: true,
      placeholder: "https://example.com/webhooks/twilio",
      help: "catalog.sms.webhookUrl.help",
    }),
    field("webhook_host", "catalog.sms.webhookHost.label", "behavior", {
      extraKey: "webhook_host",
      placeholder: "0.0.0.0",
      help: "catalog.sms.webhookHost.help",
    }),
    field("webhook_port", "catalog.sms.webhookPort.label", "behavior", {
      kind: "number",
      extraKey: "webhook_port",
      placeholder: "8080",
      help: "catalog.sms.webhookPort.help",
    }),
    field("allowed_users", "catalog.sms.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "+15551234567",
      help: "catalog.sms.allowedUsers.help",
    }),
  ],
  "platform:webhook": [
    field("host", "catalog.webhook.host.label", "creds", {
      extraKey: "host",
      placeholder: "0.0.0.0",
      help: "catalog.webhook.host.help",
    }),
    field("port", "catalog.webhook.port.label", "creds", {
      kind: "number",
      extraKey: "port",
      placeholder: "8644",
      help: "catalog.webhook.port.help",
    }),
    field("rate_limit", "catalog.webhook.rateLimit.label", "behavior", {
      kind: "number",
      extraKey: "rate_limit",
      placeholder: "30",
      help: "catalog.webhook.rateLimit.help",
    }),
    field("max_body_bytes", "catalog.webhook.maxBodyBytes.label", "behavior", {
      kind: "number",
      extraKey: "max_body_bytes",
      placeholder: "1048576",
      help: "catalog.webhook.maxBodyBytes.help",
    }),
    field("routes", "catalog.webhook.routes.label", "advanced", {
      kind: "jsonObject",
      extraKey: "routes",
      rows: 8,
      placeholder: '{\n  "github": {\n    "secret": "${GITHUB_WEBHOOK_SECRET}",\n    "prompt": "Review {repository.full_name} #{number}"\n  }\n}',
      help: "catalog.webhook.routes.help",
    }),
  ],
  "platform:dingtalk": [
    field("client_id", "catalog.dingtalk.clientId.label", "creds", {
      kind: "secret",
      extraKey: "client_id",
      required: true,
      placeholder: "${DINGTALK_CLIENT_ID}",
      help: "catalog.dingtalk.clientId.help",
    }),
    field("client_secret", "catalog.dingtalk.clientSecret.label", "creds", {
      kind: "secret",
      extraKey: "client_secret",
      required: true,
      placeholder: "${DINGTALK_CLIENT_SECRET}",
      help: "catalog.dingtalk.clientSecret.help",
    }),
    field("group_respond", "catalog.dingtalk.groupRespond.label", "behavior", {
      kind: "select",
      extraKey: "group_respond",
      options: [
        { value: "all", label: "catalog.dingtalk.groupRespond.opt.all" },
        { value: "mentions", label: "catalog.dingtalk.groupRespond.opt.mentions" },
        { value: "none", label: "catalog.dingtalk.groupRespond.opt.none" },
      ],
      help: "catalog.dingtalk.groupRespond.help",
    }),
    field("group_listen", "catalog.dingtalk.groupListen.label", "behavior", {
      kind: "select",
      extraKey: "group_listen",
      options: [
        { value: "off", label: "catalog.dingtalk.groupListen.opt.off" },
        { value: "all", label: "catalog.dingtalk.groupListen.opt.all" },
        { value: "mentions", label: "catalog.dingtalk.groupListen.opt.mentions" },
      ],
      help: "catalog.dingtalk.groupListen.help",
    }),
    field("free_response_chats", "catalog.dingtalk.freeResponseChats.label", "behavior", {
      kind: "list",
      extraKey: "free_response_chats",
      rows: 2,
      placeholder: "cidAbCdEfGh123==",
      help: "catalog.dingtalk.freeResponseChats.help",
    }),
    field("mention_patterns", "catalog.dingtalk.mentionPatterns.label", "behavior", {
      kind: "list",
      extraKey: "mention_patterns",
      rows: 2,
      placeholder: "^merli\\b",
      help: "catalog.dingtalk.mentionPatterns.help",
    }),
    field("allowed_users", "catalog.dingtalk.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "manager1234\n01234567891011",
      help: "catalog.dingtalk.allowedUsers.help",
    }),
    field("owner_users", "catalog.dingtalk.ownerUsers.label", "allow", {
      kind: "list",
      extraKey: "owner_users",
      rows: 2,
      placeholder: "manager1234",
      help: "catalog.dingtalk.ownerUsers.help",
    }),
  ],
  "platform:feishu": [
    field("app_id", "catalog.feishu.appId.label", "creds", {
      kind: "secret",
      extraKey: "app_id",
      required: true,
      placeholder: "${FEISHU_APP_ID}",
      help: "catalog.feishu.appId.help",
    }),
    field("app_secret", "catalog.feishu.appSecret.label", "creds", {
      kind: "secret",
      extraKey: "app_secret",
      required: true,
      placeholder: "${FEISHU_APP_SECRET}",
      help: "catalog.feishu.appSecret.help",
    }),
    // Which brand's cloud the app lives in. The adapter reads `extra.domain`
    // (`feishu` default, `lark` = open.larksuite.com); the Lark picker row is
    // this same form with the field preset to `lark`, see `LARK_VARIANT`.
    field("domain", "catalog.feishu.domain.label", "behavior", {
      kind: "select",
      extraKey: "domain",
      options: [
        { value: "feishu", label: "catalog.feishu.domain.opt.feishu" },
        { value: "lark", label: "catalog.feishu.domain.opt.lark" },
      ],
      help: "catalog.feishu.domain.help",
    }),
    field("connection_mode", "catalog.feishu.connectionMode.label", "behavior", {
      kind: "select",
      extraKey: "connection_mode",
      options: [
        { value: "websocket", label: "catalog.feishu.connectionMode.opt.websocket" },
        { value: "webhook", label: "catalog.feishu.connectionMode.opt.webhook" },
      ],
      help: "catalog.feishu.connectionMode.help",
    }),
    field("verification_token", "catalog.feishu.verificationToken.label", "behavior", {
      kind: "secret",
      extraKey: "verification_token",
      placeholder: "catalog.feishu.verificationToken.placeholder",
      help: "catalog.feishu.verificationToken.help",
    }),
    field("encrypt_key", "catalog.feishu.encryptKey.label", "behavior", {
      kind: "secret",
      extraKey: "encrypt_key",
      placeholder: "catalog.feishu.encryptKey.placeholder",
      help: "catalog.feishu.encryptKey.help",
    }),
  ],
  "platform:wecom": [
    field("bot_id", "catalog.wecom.botId.label", "creds", {
      extraKey: "bot_id",
      required: true,
      placeholder: "${WECOM_BOT_ID}",
      help: "catalog.wecom.botId.help",
    }),
    field("secret", "catalog.wecom.secret.label", "creds", {
      kind: "vaultSecret",
      extraKey: "secret",
      required: true,
      placeholder: "${WECOM_SECRET}",
      vaultKey: "WECOM_SECRET",
      help: "catalog.wecom.secret.help",
    }),
    field("websocket_url", "catalog.wecom.websocketUrl.label", "behavior", {
      extraKey: "websocket_url",
      placeholder: "wss://openws.work.weixin.qq.com",
      help: "catalog.wecom.websocketUrl.help",
    }),
    field("dm_policy", "catalog.wecom.dmPolicy.label", "behavior", {
      kind: "select",
      extraKey: "dm_policy",
      options: [
        { value: "open", label: "catalog.wecom.dmPolicy.opt.open" },
        { value: "allowlist", label: "catalog.wecom.dmPolicy.opt.allowlist" },
        { value: "disabled", label: "catalog.wecom.dmPolicy.opt.disabled" },
      ],
      help: "catalog.wecom.dmPolicy.help",
    }),
    field("group_policy", "catalog.wecom.groupPolicy.label", "behavior", {
      kind: "select",
      extraKey: "group_policy",
      options: [
        { value: "open", label: "catalog.wecom.groupPolicy.opt.open" },
        { value: "allowlist", label: "catalog.wecom.groupPolicy.opt.allowlist" },
        { value: "disabled", label: "catalog.wecom.groupPolicy.opt.disabled" },
      ],
      help: "catalog.wecom.groupPolicy.help",
    }),
    field("allow_from", "catalog.wecom.allowFrom.label", "allow", {
      kind: "list",
      extraKey: "allow_from",
      rows: 2,
      placeholder: "zhang_san",
      help: "catalog.wecom.allowFrom.help",
    }),
    field("group_allow_from", "catalog.wecom.groupAllowFrom.label", "allow", {
      kind: "list",
      extraKey: "group_allow_from",
      rows: 2,
      placeholder: "wrkSFfCgAAxxxxxx",
      help: "catalog.wecom.groupAllowFrom.help",
    }),
  ],
  "platform:weixin": [
    field("qr_login_help", "catalog.weixin.qrLoginHelp.label", "creds", {
      kind: "readonly",
      help: "catalog.weixin.qrLoginHelp.help",
    }),
    field("bot_id", "catalog.weixin.botId.label", "creds", {
      extraKey: "bot_id",
      placeholder: "catalog.weixin.botId.placeholder",
      help: "catalog.weixin.botId.help",
    }),
    field("app_token", "catalog.weixin.appToken.label", "creds", {
      kind: "vaultSecret",
      extraKey: "app_token",
      placeholder: "catalog.weixin.appToken.placeholder",
      vaultKey: "WEIXIN_APP_TOKEN",
      missingHint: "catalog.weixin.appToken.missingHint",
      help: "catalog.weixin.appToken.help",
    }),
  ],
  "platform:bluebubbles": [
    field("server_url", "catalog.bluebubbles.serverUrl.label", "creds", {
      extraKey: "server_url",
      required: true,
      placeholder: "http://192.168.1.10:1234",
      help: "catalog.bluebubbles.serverUrl.help",
    }),
    field("password", "catalog.bluebubbles.password.label", "creds", {
      kind: "vaultSecret",
      extraKey: "password",
      required: true,
      placeholder: "${BLUEBUBBLES_PASSWORD}",
      vaultKey: "BLUEBUBBLES_PASSWORD",
      help: "catalog.bluebubbles.password.help",
    }),
    field("webhook_host", "catalog.bluebubbles.webhookHost.label", "behavior", {
      extraKey: "webhook_host",
      placeholder: "127.0.0.1",
      help: "catalog.bluebubbles.webhookHost.help",
    }),
    field("webhook_port", "catalog.bluebubbles.webhookPort.label", "behavior", {
      kind: "number",
      extraKey: "webhook_port",
      placeholder: "8645",
      help: "catalog.bluebubbles.webhookPort.help",
    }),
  ],
  "platform:qqbot": [
    field("app_id", "catalog.qqbot.appId.label", "creds", {
      extraKey: "app_id",
      required: true,
      placeholder: "${QQ_APP_ID}",
      help: "catalog.qqbot.appId.help",
    }),
    field("client_secret", "catalog.qqbot.clientSecret.label", "creds", {
      kind: "vaultSecret",
      extraKey: "client_secret",
      required: true,
      placeholder: "${QQ_CLIENT_SECRET}",
      vaultKey: "QQ_CLIENT_SECRET",
      help: "catalog.qqbot.clientSecret.help",
    }),
    field("markdown_support", "catalog.qqbot.markdownSupport.label", "behavior", {
      kind: "select",
      extraKey: "markdown_support",
      options: [
        { value: "true", label: "catalog.qqbot.markdownSupport.opt.true" },
        { value: "false", label: "catalog.qqbot.markdownSupport.opt.false" },
      ],
      help: "catalog.qqbot.markdownSupport.help",
    }),
    field("dm_policy", "catalog.qqbot.dmPolicy.label", "behavior", {
      kind: "select",
      extraKey: "dm_policy",
      options: [
        { value: "open", label: "catalog.qqbot.dmPolicy.opt.open" },
        { value: "allowlist", label: "catalog.qqbot.dmPolicy.opt.allowlist" },
        { value: "disabled", label: "catalog.qqbot.dmPolicy.opt.disabled" },
      ],
      help: "catalog.qqbot.dmPolicy.help",
    }),
    field("allow_from", "catalog.qqbot.allowFrom.label", "allow", {
      kind: "list",
      extraKey: "allow_from",
      rows: 2,
      placeholder: "A1B2C3D4E5F6...",
      help: "catalog.qqbot.allowFrom.help",
    }),
    field("group_policy", "catalog.qqbot.groupPolicy.label", "behavior", {
      kind: "select",
      extraKey: "group_policy",
      options: [
        { value: "open", label: "catalog.qqbot.groupPolicy.opt.open" },
        { value: "allowlist", label: "catalog.qqbot.groupPolicy.opt.allowlist" },
        { value: "disabled", label: "catalog.qqbot.groupPolicy.opt.disabled" },
      ],
      help: "catalog.qqbot.groupPolicy.help",
    }),
    field("group_allow_from", "catalog.qqbot.groupAllowFrom.label", "allow", {
      kind: "list",
      extraKey: "group_allow_from",
      rows: 2,
      placeholder: "F6E5D4C3B2A1...",
      help: "catalog.qqbot.groupAllowFrom.help",
    }),
  ],
  "platform:debox": [
    field("botmother_help", "catalog.debox.botmotherHelp.label", "creds", {
      kind: "readonly",
      help: "catalog.debox.botmotherHelp.help",
    }),
    field("token", "catalog.debox.token.label", "creds", {
      kind: "vaultSecret",
      required: true,
      placeholder: "catalog.debox.token.placeholder",
      vaultKey: "DEBOX_APP_KEY",
      help: "catalog.debox.token.help",
    }),
    field("api_secret", "catalog.debox.apiSecret.label", "creds", {
      kind: "vaultSecret",
      extraKey: "api_secret",
      placeholder: "catalog.debox.apiSecret.placeholder",
      vaultKey: "DEBOX_APP_SECRET",
      help: "catalog.debox.apiSecret.help",
    }),
    field("parse_mode", "catalog.debox.parseMode.label", "behavior", {
      kind: "select",
      extraKey: "parse_mode",
      options: [
        { value: "HTML", label: "catalog.debox.parseMode.opt.HTML" },
        { value: "text", label: "catalog.debox.parseMode.opt.text" },
      ],
      help: "catalog.debox.parseMode.help",
    }),
    field("group_respond", "catalog.debox.groupRespond.label", "behavior", {
      kind: "select",
      extraKey: "group_respond",
      options: [
        { value: "all", label: "catalog.debox.groupRespond.opt.all" },
        { value: "mentions", label: "catalog.debox.groupRespond.opt.mentions" },
        { value: "none", label: "catalog.debox.groupRespond.opt.none" },
      ],
      help: "catalog.debox.groupRespond.help",
    }),
    field("group_listen", "catalog.debox.groupListen.label", "behavior", {
      kind: "select",
      extraKey: "group_listen",
      options: [
        { value: "off", label: "catalog.debox.groupListen.opt.off" },
        { value: "all", label: "catalog.debox.groupListen.opt.all" },
        { value: "mentions", label: "catalog.debox.groupListen.opt.mentions" },
      ],
      help: "catalog.debox.groupListen.help",
    }),
    field("allowed_users", "catalog.debox.allowedUsers.label", "allow", {
      kind: "list",
      extraKey: "allowed_users",
      rows: 2,
      placeholder: "uvg2p6ho",
      help: "catalog.debox.allowedUsers.help",
    }),
    field("owner_users", "catalog.debox.ownerUsers.label", "allow", {
      kind: "list",
      extraKey: "owner_users",
      rows: 2,
      placeholder: "uvg2p6ho",
      help: "catalog.debox.ownerUsers.help",
    }),
    field("transport", "catalog.debox.transport.label", "advanced", {
      kind: "select",
      extraKey: "transport",
      options: [
        { value: "auto", label: "catalog.debox.transport.opt.auto" },
        { value: "polling", label: "catalog.debox.transport.opt.polling" },
        { value: "webhook", label: "catalog.debox.transport.opt.webhook" },
      ],
      help: "catalog.debox.transport.help",
    }),
    field("webhook_key", "catalog.debox.webhookKey.label", "advanced", {
      kind: "secret",
      extraKey: "webhook_key",
      placeholder: "${DEBOX_WEBHOOK_KEY}",
      help: "catalog.debox.webhookKey.help",
    }),
    field("port", "catalog.debox.port.label", "advanced", {
      kind: "number",
      extraKey: "port",
      placeholder: "8650",
      help: "catalog.debox.port.help",
    }),
    field("mention_sender", "catalog.debox.mentionSender.label", "advanced", {
      kind: "bool",
      extraKey: "mention_sender",
      help: "catalog.debox.mentionSender.help",
    }),
  ],
  // Setup is two things: mint a key, copy the URL. The runtime derives the
  // bind host and port itself and the public URL does not depend on either,
  // so they sit in Advanced — present for the LAN case, absent from the path
  // everyone else takes.
  "platform:api_server": [
    field("api_base_url", "catalog.apiServer.baseUrl.label", "creds", {
      kind: "endpointUrl",
      help: "catalog.apiServer.baseUrl.help",
    }),
    field("api_key", "catalog.apiServer.apiKey.label", "creds", {
      kind: "vaultSecret",
      generate: "hex32",
      placeholder: "${OLANO_API_KEY}",
      vaultKey: "OLANO_API_KEY",
      help: "catalog.apiServer.apiKey.help",
    }),
    field("host", "catalog.apiServer.host.label", "advanced", {
      extraKey: "host",
      placeholder: "127.0.0.1",
      help: "catalog.apiServer.host.help",
    }),
    field("port", "catalog.apiServer.port.label", "advanced", {
      // The runtime derives a per-agent port from the agent's name; Manual is
      // the escape hatch, not the default.
      kind: "autoNumber",
      extraKey: "port",
      min: 1,
      max: 65535,
      autoLabel: "catalog.apiServer.port.auto",
      manualDefault: "8642",
      help: "catalog.apiServer.port.help",
    }),
  ],
};

const guideSummaries: Record<string, ConnectorGuide> = {
  telegram: guide("catalog.guide.telegram.title", "catalog.guide.telegram.summary", [
    "catalog.guide.telegram.step1",
    "catalog.guide.telegram.step2",
    "catalog.guide.telegram.step3",
    "catalog.guide.telegram.step4",
    "catalog.guide.telegram.step5",
    "catalog.guide.telegram.step6",
  ]),
  discord: guide("catalog.guide.discord.title", "catalog.guide.discord.summary", [
    "catalog.guide.discord.step1",
    "catalog.guide.discord.step2",
    "catalog.guide.discord.step3",
    "catalog.guide.discord.step4",
  ]),
  slack: guide("catalog.guide.slack.title", "catalog.guide.slack.summary", [
    "catalog.guide.slack.step1",
    "catalog.guide.slack.step2",
    "catalog.guide.slack.step3",
    "catalog.guide.slack.step4",
  ]),
  whatsapp: guide("catalog.guide.whatsapp.title", "catalog.guide.whatsapp.summary", [
    "catalog.guide.whatsapp.step1",
    "catalog.guide.whatsapp.step2",
    "catalog.guide.whatsapp.step3",
    "catalog.guide.whatsapp.step4",
  ]),
  email: guide("catalog.guide.email.title", "catalog.guide.email.summary", [
    "catalog.guide.email.step1",
    "catalog.guide.email.step2",
    "catalog.guide.email.step3",
    "catalog.guide.email.step4",
  ]),
  signal: guide("catalog.guide.signal.title", "catalog.guide.signal.summary", [
    "catalog.guide.signal.step1",
    "catalog.guide.signal.step2",
    "catalog.guide.signal.step3",
    "catalog.guide.signal.step4",
  ]),
  matrix: guide("catalog.guide.matrix.title", "catalog.guide.matrix.summary", [
    "catalog.guide.matrix.step1",
    "catalog.guide.matrix.step2",
    "catalog.guide.matrix.step3",
    "catalog.guide.matrix.step4",
  ]),
  mattermost: guide("catalog.guide.mattermost.title", "catalog.guide.mattermost.summary", [
    "catalog.guide.mattermost.step1",
    "catalog.guide.mattermost.step2",
    "catalog.guide.mattermost.step3",
    "catalog.guide.mattermost.step4",
  ]),
  homeassistant: guide("catalog.guide.homeassistant.title", "catalog.guide.homeassistant.summary", [
    "catalog.guide.homeassistant.step1",
    "catalog.guide.homeassistant.step2",
    "catalog.guide.homeassistant.step3",
    "catalog.guide.homeassistant.step4",
  ]),
  sms: guide("catalog.guide.sms.title", "catalog.guide.sms.summary", [
    "catalog.guide.sms.step1",
    "catalog.guide.sms.step2",
    "catalog.guide.sms.step3",
    "catalog.guide.sms.step4",
  ]),
  whatsapp_twilio: guide("catalog.guide.whatsappTwilio.title", "catalog.guide.whatsappTwilio.summary", [
    "catalog.guide.whatsappTwilio.step1",
    "catalog.guide.whatsappTwilio.step2",
    "catalog.guide.whatsappTwilio.step3",
    "catalog.guide.whatsappTwilio.step4",
    "catalog.guide.whatsappTwilio.step5",
    "catalog.guide.whatsappTwilio.step6",
  ]),
  whatsapp_meta_api: guide("catalog.guide.whatsappMetaApi.title", "catalog.guide.whatsappMetaApi.summary", [
    "catalog.guide.whatsappMetaApi.step1",
    "catalog.guide.whatsappMetaApi.step2",
    "catalog.guide.whatsappMetaApi.step3",
    "catalog.guide.whatsappMetaApi.step4",
    "catalog.guide.whatsappMetaApi.step5",
    "catalog.guide.whatsappMetaApi.step6",
  ]),
  webhook: guide("catalog.guide.webhook.title", "catalog.guide.webhook.summary", [
    "catalog.guide.webhook.step1",
    "catalog.guide.webhook.step2",
    "catalog.guide.webhook.step3",
    "catalog.guide.webhook.step4",
  ]),
  dingtalk: guide("catalog.guide.dingtalk.title", "catalog.guide.dingtalk.summary", [
    "catalog.guide.dingtalk.step1",
    "catalog.guide.dingtalk.step2",
    "catalog.guide.dingtalk.step3",
    "catalog.guide.dingtalk.step4",
  ]),
  feishu: guide("catalog.guide.feishu.title", "catalog.guide.feishu.summary", [
    "catalog.guide.feishu.step1",
    "catalog.guide.feishu.step2",
    "catalog.guide.feishu.step3",
    "catalog.guide.feishu.step4",
  ]),
  wecom: guide("catalog.guide.wecom.title", "catalog.guide.wecom.summary", [
    "catalog.guide.wecom.step1",
    "catalog.guide.wecom.step2",
    "catalog.guide.wecom.step3",
    "catalog.guide.wecom.step4",
  ]),
  weixin: guide("catalog.guide.weixin.title", "catalog.guide.weixin.summary", [
    "catalog.guide.weixin.step1",
    "catalog.guide.weixin.step2",
    "catalog.guide.weixin.step3",
    "catalog.guide.weixin.step4",
  ]),
  bluebubbles: guide("catalog.guide.bluebubbles.title", "catalog.guide.bluebubbles.summary", [
    "catalog.guide.bluebubbles.step1",
    "catalog.guide.bluebubbles.step2",
    "catalog.guide.bluebubbles.step3",
    "catalog.guide.bluebubbles.step4",
  ]),
  qqbot: guide("catalog.guide.qqbot.title", "catalog.guide.qqbot.summary", [
    "catalog.guide.qqbot.step1",
    "catalog.guide.qqbot.step2",
    "catalog.guide.qqbot.step3",
    "catalog.guide.qqbot.step4",
  ]),
  debox: guide("catalog.guide.debox.title", "catalog.guide.debox.summary", [
    "catalog.guide.debox.step1",
    "catalog.guide.debox.step2",
    "catalog.guide.debox.step3",
    "catalog.guide.debox.step4",
  ]),
  api_server: guide("catalog.guide.apiServer.title", "catalog.guide.apiServer.summary", [
    "catalog.guide.apiServer.step1",
    "catalog.guide.apiServer.step2",
    "catalog.guide.apiServer.step3",
    "catalog.guide.apiServer.step4",
  ]),
};

// Declared above `baseDefinitions` on purpose: that array calls
// `platformLabel()` while the module is still evaluating. The function
// declaration hoists, but the `const` table it reads does not — leaving it
// further down the file put it in the temporal dead zone and threw
// `Cannot access 'PLATFORM_NAMESPACE' before initialization` on import,
// taking down every consumer of this catalog.
/**
 * type ("platform:xxx") → the `catalog.<namespace>` prefix used for that
 * connector's field/guide keys throughout this file. Explicit rather than
 * derived: most slugs camelCase onto their own namespace (dingtalk, feishu,
 * wecom, weixin, bluebubbles, qqbot, debox, webhook, signal, matrix,
 * mattermost, homeassistant, sms, api_server → apiServer), but "email" is the
 * one case where the platform adapter's namespace ("emailPlatform") differs
 * from its own slug, because the legacy connector already owns the bare
 * "email" namespace for its (very different) IMAP/SMTP fields — see
 * `catalog.email.*` vs `catalog.emailPlatform.*` in `./strings.ts`. Keeping
 * this a lookup table, not a camelCase transform, means a future connector
 * with an irregular slug can't silently collide with an existing key the way
 * a purely algorithmic derivation did here for "email".
 */
const PLATFORM_NAMESPACE: Record<string, string> = {
  telegram: "telegram",
  discord: "discord",
  slack: "slack",
  whatsapp: "whatsapp",
  whatsapp_twilio: "whatsappTwilio",
  whatsapp_meta_api: "whatsappMetaApi",
  email: "emailPlatform",
  signal: "signal",
  matrix: "matrix",
  mattermost: "mattermost",
  homeassistant: "homeassistant",
  sms: "sms",
  webhook: "webhook",
  dingtalk: "dingtalk",
  feishu: "feishu",
  wecom: "wecom",
  weixin: "weixin",
  bluebubbles: "bluebubbles",
  qqbot: "qqbot",
  debox: "debox",
  api_server: "apiServer",
};

function platformLabel(type: string): string {
  // Translation key, not text — see `catalog.<namespace>.defLabel` entries in
  // `./strings.ts`, one per platform adapter.
  const slug = type.replace(/^platform:/, "");
  return `catalog.${PLATFORM_NAMESPACE[slug] || slug}.defLabel`;
}

/**
 * Lark is Feishu's international edition: the same `FeishuAdapter`, pointed at
 * open.larksuite.com by `extra.domain: "lark"`. It had its own picker row once,
 * folded into "Feishu / Lark" to shorten the list, which left Lark unreachable
 * from the dashboard, because the form had no domain field. It is back as a
 * *variant*: the Feishu definition with `domain` preset, its own label so a
 * Lark tenant finds it under L. Mirrors `platforms/catalog.py:_VARIANTS`; the
 * live catalog row carries the same variant, and `supportedCatalogChannels`
 * expands it, so the static list only has to agree when the fetch fails.
 */
const VARIANTS: Record<string, Array<{ id: string; label: string; name: string; preset: Record<string, string> }>> = {
  "platform:feishu": [{ id: "lark", label: "catalog.lark.defLabel", name: "catalog.lark.name", preset: { domain: "lark" } }],
};

function variantDef(base: ConnectorDefinition, v: { id: string; label: string; name: string; preset: Record<string, string> }): ConnectorDefinition {
  return { ...base, id: `${base.type}@${v.id}`, label: v.label, variant: { id: v.id, name: v.name, preset: { ...v.preset } } };
}

const baseDefinitions: ConnectorDefinition[] = [
  def("telegram", "catalog.telegramClassic.defLabel", "legacy", classicTelegram),
  def("discord", "catalog.discordClassic.defLabel", "legacy", classicDiscord),
  def("slack", "catalog.slackClassic.defLabel", "legacy", classicSlack),
  def("whatsapp", "catalog.whatsappClassic.defLabel", "legacy", classicWhatsapp),
  def("email", "catalog.email.defLabel", "legacy", emailFields),
  def("imap", "catalog.imap.defLabel", "legacy", emailFields, "email"),
  ...Object.entries(platformFields).flatMap(([type, fields]) => {
    const base = def(type, platformLabel(type), "platform", fields);
    return [base, ...(VARIANTS[type] || []).map((v) => variantDef(base, v))];
  }),
];

// Alphabetical by the label the user actually reads in the dropdown — not by
// internal type key, which buried e.g. "Inbound webhooks" (webhook) between
// Telegram and WeCom and floated "OpenAI-compatible HTTP API" (api_server) to
// the top. Sorted here on the (stable, English) translation KEY rather than
// resolved text: the picker is re-sorted with real i18n text at render time by
// whichever consumer needs a specific language's alphabetical order — this
// module-level order only needs to be deterministic, not localized.
const byLabel = (a: ConnectorDefinition, b: ConnectorDefinition) =>
  a.label.localeCompare(b.label, undefined, { sensitivity: "base" });

export const CONNECTOR_DEFINITIONS = baseDefinitions.sort(byLabel);
// Keyed by wire type, base rows only: the definition a saved `type` edits with.
export const CONNECTOR_DEFINITION_BY_TYPE = new Map(CONNECTOR_DEFINITIONS.filter((d) => !d.variant).map((d) => [d.type, d]));
// Keyed by picker id, variants included: what the type dropdown selects.
export const CONNECTOR_DEFINITION_BY_ID = new Map(CONNECTOR_DEFINITIONS.map((d) => [d.id, d]));

export function getConnectorDefinition(type: string): ConnectorDefinition | undefined {
  return CONNECTOR_DEFINITION_BY_TYPE.get(type);
}

export function getConnectorDefinitionById(id: string): ConnectorDefinition | undefined {
  return CONNECTOR_DEFINITION_BY_ID.get(id) || CONNECTOR_DEFINITION_BY_TYPE.get(id);
}

/** The form value a saved channel holds for `field` (top-level or under `extra`). */
function savedValue(field: ConnectorFieldDef, config: Record<string, unknown>): unknown {
  const extra = isRecord(config.extra) ? config.extra : {};
  if (!field.extraKey) return config[field.key];
  const value = extra[field.extraKey];
  // See `legacyTopLevel`: show what an earlier form saved in the wrong place,
  // so the next save moves it under `extra` instead of dropping it.
  return value === undefined && field.legacyTopLevel ? config[field.key] : value;
}

/**
 * The picker row a saved connector belongs to: the variant whose every preset
 * value the config carries (a Feishu connector with `extra.domain: "lark"` is
 * the Lark row), else the base definition for its type. `undefined` for a type
 * this build has no definition for.
 */
export function connectorDefinitionForConfig(type: string, config: Record<string, unknown>): ConnectorDefinition | undefined {
  const base = getConnectorDefinition(type);
  if (!base) return undefined;
  for (const d of CONNECTOR_DEFINITIONS) {
    if (d.type !== type || !d.variant) continue;
    const matches = Object.entries(d.variant.preset).every(([key, want]) => {
      const f = d.fields.find((x) => x.key === key);
      return f ? String(savedValue(f, config) ?? "") === want : false;
    });
    if (matches) return d;
  }
  return base;
}

/**
 * Short display name for a saved connector's row (translation key when the
 * connector is a variant, "Lark", else `undefined`, and the caller keeps its
 * type-derived name). Lets the connector list tell a Lark row from a Feishu one
 * without every other row changing.
 */
export function connectorVariantName(type: string, config: Record<string, unknown>): string | undefined {
  return connectorDefinitionForConfig(type, config)?.variant?.name;
}

export function supportedCatalogChannels(
  catalog: CatalogChannel[] | null,
  currentType?: string,
): ConnectorDefinition[] {
  if (!catalog?.length) {
    // No live catalog — apply the same suppression policy to the static list.
    return CONNECTOR_DEFINITIONS.filter((d) => !isSuppressedType(d.type) || d.type === currentType);
  }
  return catalog
    // Deprecated (duplicated by a maintained adapter) and hidden (boots, but
    // unverified) connectors stay editable when the agent already uses one,
    // but are never offered for new connectors.
    .filter((c) => !(c.deprecated || c.hidden) || c.type === currentType)
    .flatMap((c) => {
      const known = getConnectorDefinition(c.type);
      if (!known) return [];
      // A live catalog row's `label`, when present, is server-rendered text
      // (not a translation key) — `t()` returns unknown keys unchanged, so
      // callers can pass either through the same `t(definition.label)` call.
      const rows: ConnectorDefinition[] = [{ ...known, label: c.label || known.label }];
      // The row's variants (Lark under Feishu): the static definition of the
      // same id supplies the fields and the short name, the server the label.
      for (const v of c.variants || []) {
        const local = getConnectorDefinitionById(`${c.type}@${v.id}`);
        if (local?.variant) rows.push({ ...local, label: v.label || local.label });
      }
      return rows;
    })
    .sort(byLabel);
}

/**
 * Mirror of the backend's `platforms/catalog.py:hidden_channel_types()` — every
 * connector the picker must not offer for a NEW one, for either reason:
 *
 * - every classic `BaseChannel` type, each duplicated by a `platform:` adapter
 *   that supersedes it (`_DEPRECATED`);
 * - platform adapters that boot but are not verified end to end yet
 *   (`_UNVERIFIED`).
 *
 * Only consulted when the /api/catalog/channels fetch fails and the static list
 * is shown; the live catalog carries `deprecated` / `hidden` per row.
 */
const SUPPRESSED_TYPES = new Set([
  // Deprecated: superseded by the platform adapter of the same name.
  "telegram",
  "discord",
  "slack",
  "whatsapp",
  "email",
  "imap",
  // Hidden: unverified platform adapters.
  "platform:bluebubbles",
  "platform:email",
  "platform:homeassistant",
  "platform:matrix",
  "platform:mattermost",
  "platform:webhook",
]);

/** True when this connector must not be offered for a NEW connector. */
export function isSuppressedType(type: string): boolean {
  return SUPPRESSED_TYPES.has(type);
}

export function initialValues(definition: ConnectorDefinition, config: Record<string, unknown>): ConnectorFormValues {
  const values: ConnectorFormValues = {};
  const preset = definition.variant?.preset || {};
  for (const f of definition.fields) {
    if (isDisplayOnlyKind(f.kind)) continue;
    const source = savedValue(f, config);
    // A variant row fixes its preset fields for a NEW connector; a saved value
    // still wins, so editing never silently rewrites what the agent runs on.
    values[f.key] = source === undefined && f.key in preset ? preset[f.key] : valueToForm(source, f.kind);
  }
  return values;
}

/**
 * Build the channel payload.
 *
 * `t` resolves catalog translation keys into the current language for
 * validation error messages (e.g. "{label} is required.") — pass the `t`
 * from `useT(STR)` (the `views/agents/strings.ts` dict, which owns every
 * `catalog.*` key referenced by this file).
 *
 * `omitVaultSecrets` leaves every vault-backed credential out entirely — and
 * skips its required check, since "required" now means "must be in the vault",
 * not "must be in this form". The connector panel passes it because it stores
 * a typed credential in the vault itself, before this payload is sent (see
 * `pendingVaultSecrets`); the onboarding wizard does not, so a token typed
 * once during setup still rides along and the backend absorbs it into a
 * per-agent override on save.
 */
export function buildConnectorConfig(
  definition: ConnectorDefinition,
  values: ConnectorFormValues,
  t: CatalogT,
  opts: { omitVaultSecrets?: boolean } = {},
): { config?: Record<string, unknown>; error?: string } {
  const config: Record<string, unknown> = {};
  const extra: Record<string, unknown> = {};

  for (const f of definition.fields) {
    if (isDisplayOnlyKind(f.kind)) continue;
    if (opts.omitVaultSecrets && f.kind === "vaultSecret") continue;
    const raw = values[f.key];
    const parsed = parseFieldValue(f, raw, t);
    if (parsed.error) return { error: parsed.error };
    if (parsed.value === undefined) continue;
    if (f.extraKey) extra[f.extraKey] = parsed.value;
    else config[f.key] = parsed.value;
  }

  if (Object.keys(extra).length) config.extra = extra;
  return { config };
}

/**
 * Vault-backed credentials typed into the form and not stored yet.
 *
 * A vault field keeps what the user types as form state until Save, and Save
 * writes each of these to the agent's vault override *before* the connector
 * itself. The field used to keep its own draft behind a separate "Save to
 * vault" button, so a token typed there and followed by the form's Save was
 * dropped without a word, and the connector then refused to start for want of
 * the credential the user had just entered.
 */
export function pendingVaultSecrets(
  definition: ConnectorDefinition,
  values: ConnectorFormValues,
): Array<{ field: ConnectorFieldDef; vaultKey: string; value: string }> {
  const pending: Array<{ field: ConnectorFieldDef; vaultKey: string; value: string }> = [];
  for (const field of definition.fields) {
    if (field.kind !== "vaultSecret" || !field.vaultKey) continue;
    const raw = values[field.key];
    const value = typeof raw === "string" ? raw.trim() : "";
    if (value) pending.push({ field, vaultKey: field.vaultKey, value });
  }
  return pending;
}

/**
 * Each connector tool's switch, from the agent's config: an everyday tool is
 * on unless `disabled_tools` names it, an admin tool only when `tools` names
 * it (and `disabled_tools` does not).
 */
export function connectorToolSwitches(
  defs: ConnectorToolDef[],
  cfg: { tools?: string[]; disabled_tools?: string[] },
): Record<string, boolean> {
  const tools = new Set(cfg.tools || []);
  const disabled = new Set(cfg.disabled_tools || []);
  const out: Record<string, boolean> = {};
  for (const d of defs) {
    out[d.name] = d.admin ? tools.has(d.name) && !disabled.has(d.name) : !disabled.has(d.name);
  }
  return out;
}

/**
 * The agent's `tools` and `disabled_tools` after applying the switches: an
 * everyday tool switched off goes into `disabled_tools` (the only way to drop
 * a tool the platform grants by itself); an admin tool switched on goes into
 * `tools`, off comes out of it. Everything else in either list is kept, so a
 * stale copy of the config cannot clobber another edit: callers fetch the
 * agent fresh right before applying.
 */
export function applyConnectorToolSwitches(
  defs: ConnectorToolDef[],
  cfg: { tools?: string[]; disabled_tools?: string[] },
  state: Record<string, boolean>,
): { tools: string[]; disabled_tools: string[] } {
  const tools = new Set(cfg.tools || []);
  const disabled = new Set(cfg.disabled_tools || []);
  for (const d of defs) {
    const on = state[d.name];
    if (on === undefined) continue;
    if (d.admin) {
      if (on) tools.add(d.name);
      else tools.delete(d.name);
      disabled.delete(d.name);
    } else if (on) {
      disabled.delete(d.name);
    } else {
      disabled.add(d.name);
    }
  }
  return { tools: [...tools], disabled_tools: [...disabled].sort() };
}

export function isWhatsAppPairable(type: string): boolean {
  return type === "platform:whatsapp";
}

export function isSignalGuided(type: string): boolean {
  return type === "platform:signal";
}

export function isWeixinPairable(type: string): boolean {
  return type === "platform:weixin";
}

function def(type: string, label: string, kind: "legacy" | "platform", fields: ConnectorFieldDef[], guideKey = type.replace(/^platform:/, "")): ConnectorDefinition {
  return {
    type,
    id: type,
    label,
    kind,
    fields,
    secondaryGroups: SECONDARY_GROUPS[type] || [],
    tools: CONNECTOR_TOOLS[type] || [],
    guide:
      guideSummaries[guideKey] ||
      guide("catalog.guide.fallback.title", "catalog.guide.fallback.summary", [
        "catalog.guide.fallback.step1",
        "catalog.guide.fallback.step2",
        "catalog.guide.fallback.step3",
      ]),
  };
}

function guide(titleKey: string, summaryKey: string, stepKeys: string[]): ConnectorGuide {
  return { titleKey, summaryKey, stepKeys };
}

function valueToForm(value: unknown, kind: ConnectorFieldKind | undefined): ConnectorFormValue {
  // A vault-backed credential is never in the config to prefill from — and a
  // pre-convention config may still hold a stale ${...} reference, which must
  // not be shown as if it were the value.
  if (kind === "vaultSecret") return "";
  if (kind === "bool") return Boolean(value);
  if (Array.isArray(value)) return value.join("\n");
  if (kind === "jsonObject" && isRecord(value)) return JSON.stringify(value, null, 2);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return typeof value === "string" ? value : "";
}

function parseFieldValue(fieldDef: ConnectorFieldDef, value: ConnectorFormValue, t: CatalogT): { value?: unknown; error?: string } {
  const kind = fieldDef.kind || "text";
  if (kind === "bool") return { value: Boolean(value) };

  const label = t(fieldDef.label);
  const raw = typeof value === "string" ? value.trim() : "";
  if (!raw) {
    if (fieldDef.required) return { error: t("catalog.err.required", { label }) };
    return {};
  }

  if (kind === "select") {
    // Boolean-shaped selects (option values "true"/"false") keep the config
    // key typed as a real boolean, matching what a bool toggle used to write.
    if (raw === "true" || raw === "false") return { value: raw === "true" };
    return { value: raw };
  }

  if (kind === "number" || kind === "autoNumber") {
    const n = Number(raw);
    if (!Number.isFinite(n) || !Number.isInteger(n)) return { error: t("catalog.err.wholeNumber", { label }) };
    if (typeof fieldDef.min === "number" && n < fieldDef.min) {
      return { error: t("catalog.err.min", { label, min: String(fieldDef.min) }) };
    }
    if (typeof fieldDef.max === "number" && n > fieldDef.max) {
      return { error: t("catalog.err.max", { label, max: String(fieldDef.max) }) };
    }
    return { value: n };
  }

  if (kind === "list") {
    const list = raw
      .split(/[\n,]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (fieldDef.snowflake) {
      const bad = list.find((item) => !/^\d{17,20}$/.test(item));
      if (bad) return { error: t("catalog.err.snowflake", { label, value: bad }) };
    }
    return list.length ? { value: list } : {};
  }

  if (kind === "jsonObject") {
    try {
      const parsed = JSON.parse(raw) as unknown;
      return isRecord(parsed) ? { value: parsed } : { error: t("catalog.err.jsonObject", { label }) };
    } catch {
      return { error: t("catalog.err.jsonInvalid", { label }) };
    }
  }

  return { value: raw };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
