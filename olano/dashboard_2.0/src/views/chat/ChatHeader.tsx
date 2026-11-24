import { useCallback, useMemo, useRef, useState } from "react";
import { Brain, ChevronDown, Cpu, Gem, History, Info, KeyRound, Plus, UserPlus, Wrench, Zap } from "lucide-react";
import { useT } from "../../core/i18n";
import { AnchoredPopover } from "../../ui/AnchoredPopover";
import { fmtCreditsAmount, fmtUsd } from "../../core/costDisplay";
import { useCatalog, usableModelOptions, modelReasoningMeta } from "../agents/catalog";
import { useModelOptionHint } from "../agents/ModelOptions";
import { modelContextWindow } from "./models";
import type { ChatDetail, ChatMode } from "./prefs";
import { STR } from "./strings";
import { McpIndicator } from "./McpIndicator";
import { ChannelPill, isPlatformChannel } from "./channels";

type Translate = (key: string, vars?: Record<string, string>) => string;

export interface Usage {
  /** Cumulative input tokens billed across the whole conversation.
   *
   *  Still accumulated, never rendered: the dashboard shows work volume in
   *  model calls rather than tokens (see the header's chips). Kept because it
   *  costs nothing to carry and is what a token view would need if one ever
   *  comes back — the SSE frame reports it either way. */
  input: number;
  /** Cumulative output tokens billed across the whole conversation. Same
   *  contract as `input`: recorded, not displayed. */
  output: number;
  /** Cumulative USD cost across the whole conversation. */
  usd?: number;
  /** Cumulative olano credits (Olano-managed turns; the gateway's own charge —
   *  a separate accounting system from `usd`, never converted or summed). */
  credits?: number;
  /** Model calls across the conversation — one per model sub-call, so a turn
   *  that ran three tool round-trips counts three (same definition as the
   *  Insights "Model calls" stat). */
  calls?: number;
  /** Tool calls across the conversation, MCP tools included. */
  toolCalls?: number;
  /** Last turn's input tokens — the current context-window fill (drives the
   *  context bar). Distinct from `input`, which is the running total. */
  context?: number;
}

/** Natural width of the header's chip dropdowns. `AnchoredPopover` caps it at
 *  the viewport, so this is a preference rather than a promise. */
const MENU_MIN_W = 300;

const MODE_DOT: Record<ChatMode, string> = {
  auto: "var(--success)",
  plan: "var(--warn)",
  manual: "var(--danger)",
  act: "var(--accent)",
};

// The model chip is a dropdown: it shows the model this conversation will run
// (the agent default, or an override the user picked) and lets them switch it
// or reset to the agent's configured model. The override is honored by the
// backend (/api/chat/stream → astream_events override_model) and belongs to
// THIS conversation only (Session.model): a new chat opens on the default, a
// reopened thread continues on the model it last answered with.
function ModelPicker({
  model,
  defaultModel,
  onSelect,
  t,
}: {
  model: string;
  defaultModel: string;
  onSelect: (value: string) => void;
  t: Translate;
}) {
  const cat = useCatalog();
  const options = useMemo(() => usableModelOptions(cat.providers), [cat.providers]);
  const hint = useModelOptionHint();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  // An override is active when the shown model differs from the agent default.
  const isOverride = !!model && !!defaultModel && model !== defaultModel;
  const query = q.trim().toLowerCase();
  const filtered = query
    ? options.filter((o) => `${o.label} ${o.value}`.toLowerCase().includes(query))
    : options;

  function pick(value: string) {
    onSelect(value);
    setOpen(false);
    setQ("");
  }

  return (
    <div className="model-picker" ref={ref}>
      <button
        type="button"
        className={`chat-chip is-mono${isOverride ? " is-on" : ""}`}
        title={t("model.title")}
        onClick={() => setOpen((v) => !v)}
      >
        <Cpu size={12} strokeWidth={2} />
        {/* Long model ids ellipsize (chat-chip-label) so the chip can never
            get wider than a phone viewport. */}
        <span className="chat-chip-label">{model || t("model.none")}</span>
        <ChevronDown size={11} strokeWidth={2} style={{ marginLeft: 2, opacity: 0.7 }} />
      </button>
      <AnchoredPopover
        anchorRef={ref}
        open={open}
        onClose={close}
        minWidth={MENU_MIN_W}
        className="model-menu"
      >
        <input
          className="input model-search"
          autoFocus
          placeholder={t("model.search")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <div className="model-list">
          <button
            type="button"
            className={`model-item${!isOverride ? " selected" : ""}`}
            onClick={() => pick("")}
          >
            <span className="model-item-name">{t("model.default")}</span>
            {defaultModel && <span className="model-item-sub">{defaultModel}</span>}
          </button>
          {/* Olano tiers pinned first with their own accent — the managed,
              billed-in-credits defaults — separated from BYOK models. They
              stack name over a one-line "what to pick this for" hint; BYOK
              rows stay single-line (a provider model id explains itself). */}
          {filtered
            .filter((o) => o.olano)
            .map((o) => {
              const sub = hint(o);
              return (
                <button
                  key={o.value}
                  type="button"
                  className={`model-item is-olano is-stacked${o.value === model ? " selected" : ""}`}
                  onClick={() => pick(o.value)}
                >
                  <span className="model-item-name">{o.label}</span>
                  {sub && <span className="model-item-hint">{sub}</span>}
                </button>
              );
            })}
          {filtered.some((o) => o.olano) && filtered.some((o) => !o.olano) && (
            <div className="model-sep" role="separator" />
          )}
          {filtered
            .filter((o) => !o.olano)
            .map((o) => (
              <button
                key={o.value}
                type="button"
                className={`model-item${o.value === model ? " selected" : ""}`}
                onClick={() => pick(o.value)}
              >
                <span className="model-item-name">{o.label}</span>
              </button>
            ))}
          {filtered.length === 0 && <div className="model-empty">{t("model.empty")}</div>}
        </div>
      </AnchoredPopover>
    </div>
  );
}

// Reasoning-effort chip: rendered only when the conversation's effective model
// advertises effort levels in the catalog metadata, so it can never offer a
// level the model would reject. "" = the agent's configured effort (or the
// platform default for the model).
function EffortPicker({
  model,
  effort,
  onSelect,
  t,
}: {
  model: string;
  effort: string;
  onSelect: (value: string) => void;
  t: Translate;
}) {
  const cat = useCatalog();
  const meta = useMemo(() => modelReasoningMeta(cat.providers, model), [cat.providers, model]);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  if (!meta) return null;
  const isOverride = !!effort;
  const defaultLabel = meta.defaultEffort
    ? t("effort.defaultLevel", { level: meta.defaultEffort })
    : t("effort.modelDefault");

  function pick(value: string) {
    onSelect(value);
    setOpen(false);
  }

  return (
    <div className="model-picker" ref={ref}>
      <button
        type="button"
        className={`chat-chip is-mono${isOverride ? " is-on" : ""}`}
        title={t("effort.title")}
        onClick={() => setOpen((v) => !v)}
      >
        <Brain size={12} strokeWidth={2} /> {effort || meta.defaultEffort || t("effort.chip")}
        <ChevronDown size={11} strokeWidth={2} style={{ marginLeft: 2, opacity: 0.7 }} />
      </button>
      <AnchoredPopover
        anchorRef={ref}
        open={open}
        onClose={close}
        minWidth={MENU_MIN_W}
        className="model-menu"
      >
        <div className="model-list">
          <button
            type="button"
            className={`model-item${!isOverride ? " selected" : ""}`}
            onClick={() => pick("")}
          >
            <span className="model-item-name">{defaultLabel}</span>
          </button>
          {meta.efforts.map((lvl) => (
            <button
              key={lvl}
              type="button"
              className={`model-item${lvl === effort ? " selected" : ""}`}
              onClick={() => pick(lvl)}
            >
              <span className="model-item-name">{lvl}</span>
            </button>
          ))}
        </div>
      </AnchoredPopover>
    </div>
  );
}

// Persistent chip row above the message log: mode + verbose toggles, the active
// model (a picker), a context-window utilisation bar, and last-turn token counts.
export function ChatHeader({
  mode,
  detail,
  model,
  defaultModel,
  reasoningEffort,
  usage,
  agentName,
  channel,
  historyOpen,
  profileOpen,
  onToggleRail,
  onToggleProfile,
  onNewChat,
  onCycleMode,
  onCycleDetail,
  onSelectModel,
  onSelectEffort,
  onAddBot,
}: {
  mode: ChatMode;
  detail: ChatDetail;
  model: string;
  /** The agent's configured model — the "Agent default" / reset target. */
  defaultModel: string;
  /** Per-chat reasoning-effort override ("" = agent default). */
  reasoningEffort: string;
  usage: Usage | null;
  /** Agent system name — drives the MCP status indicator. */
  agentName: string;
  /** The open conversation's surface ("telegram", "dashboard", …). Badged only
   *  for messaging platforms — a dashboard chat is the baseline, so a pill
   *  saying so on every conversation would be noise, not information. */
  channel: string;
  /** Whether the threads-history drawer is currently open. */
  historyOpen: boolean;
  /** Whether the agent's profile drawer is currently open. */
  profileOpen: boolean;
  /** Open/close the threads-history drawer (the header's history button). */
  onToggleRail: () => void;
  /** Open/close the agent's profile card (the header's info button). */
  onToggleProfile: () => void;
  /** Start a fresh conversation — the header's `+`. */
  onNewChat: () => void;
  onCycleMode: () => void;
  onCycleDetail: () => void;
  onSelectModel: (value: string) => void;
  onSelectEffort: (value: string) => void;
  /** Turn this 1:1 into a group chat (opens the Add-a-bot dialog). Absent
   *  when it cannot work — groups off, or nobody else to add — so the chip
   *  is never drawn for a refusal. */
  onAddBot?: () => void;
}) {
  const t = useT(STR);
  const ctx = modelContextWindow(model);
  // The bar tracks the live context-window fill (last turn's input), not the
  // running input total — those diverge once `input` accumulates across turns.
  const used = usage?.context ?? 0;
  const usedPct = ctx ? Math.min(100, (used / ctx) * 100) : 0;
  const usedColor = usedPct > 85 ? "var(--danger)" : usedPct > 70 ? "var(--warn)" : "var(--accent)";
  // A freshly reopened thread has no live context fill yet (only running totals),
  // so hide the bar rather than imply an empty window; a brand-new chat (no usage)
  // legitimately shows 0%. The bar returns once a turn reports its context size.
  const showCtxBar = !!ctx && (usage == null || usage.context != null);

  return (
    <div className="chat-header">
      {/* The two conversation actions lead the row as a pair — `+` starts a
          conversation, the clock opens the list of them — in the theme's own
          accent and filled, so they read as controls rather than as two more
          status chips. Side by side because they are the same kind of thing:
          both are about WHICH conversation, where everything after them
          describes THIS one. The same pair the simple shell's chat header
          leads with, so a person moving between shells finds them where they
          left them. `+` used to step aside while the drawer was open; the
          drawer's backdrop covers this row anyway, so all that did was make
          the clock jump. Before this there was no `+` on a phone at all: the
          only entry point lived inside the drawer behind the clock, which is
          a discoverable place to *find* a chat and not an obvious place to
          *start* one. */}
      <button
        className="chat-chip is-icon is-action is-primary"
        onClick={onNewChat}
        title={t("chat.new")}
        aria-label={t("chat.new")}
        data-testid="chat-new"
      >
        <Plus size={15} strokeWidth={2.5} />
      </button>
      <button
        className={`chat-chip is-icon is-action is-primary${historyOpen ? " is-on" : ""}`}
        onClick={onToggleRail}
        title={historyOpen ? t("history.hide") : t("history.show")}
        aria-label={historyOpen ? t("history.hide") : t("history.show")}
        aria-expanded={historyOpen}
        aria-haspopup="dialog"
        data-testid="chat-history-toggle"
      >
        <History size={14} strokeWidth={2} />
      </button>

      {isPlatformChannel(channel) && <ChannelPill channel={channel} t={t} />}

      <button className="chat-chip" onClick={onCycleMode} title={t("mode.title")}>
        <span className="chat-chip-dot" style={{ background: MODE_DOT[mode] }} />
        {t(`mode.${mode}`)}
      </button>

      <button
        className={`chat-chip${detail !== "normal" ? " is-on" : ""}`}
        onClick={onCycleDetail}
        title={t("detail.title")}
      >
        <Zap size={12} strokeWidth={2} /> {t(`detail.${detail}`)}
      </button>

      <ModelPicker model={model} defaultModel={defaultModel} onSelect={onSelectModel} t={t} />

      <EffortPicker
        model={model || defaultModel}
        effort={reasoningEffort}
        onSelect={onSelectEffort}
        t={t}
      />

      {agentName && <McpIndicator agentName={agentName} />}

      {/* Beside the MCP status, where the simple shell's chat header keeps it
          too: everything to the left describes THIS conversation, and this is
          the one control that changes what the conversation is. */}
      {onAddBot && (
        <button
          className="chat-chip is-action"
          onClick={onAddBot}
          title={t("addbot.chipHelp")}
          aria-label={t("addbot.chip")}
          data-testid="chat-add-bot"
        >
          <UserPlus size={13} strokeWidth={2} /> {t("addbot.chip")}
        </button>
      )}

      {showCtxBar && (
        <span
          className="chat-chip is-context"
          title={t("context.title", { used: used.toLocaleString(), total: ctx.toLocaleString() })}
        >
          <span className="ctx-bar">
            <span className="ctx-fill" style={{ width: `${usedPct.toFixed(1)}%`, background: usedColor }} />
          </span>
          <span className="tabular">
            {(used / 1000).toFixed(1)}k / {(ctx / 1000).toFixed(0)}k
          </span>
        </span>
      )}

      {/* Billing split: TWO independent chips, not a choice between them.
          Credits are the Olano gateway's own charge for turns on Olano models;
          dollars are the provider's bill for turns on the customer's own keys.
          A conversation can hold both — the model picker changes the book
          mid-thread, and every turn after that lands in the other one — so
          rendering only the first non-zero figure hid whatever the switch
          produced. They are never summed and never converted into each other:
          each chip carries its own unit, and neither follows the deployment's
          display setting (a provider invoice restated in credits describes a
          different transaction). Running token totals are deliberately NOT a
          chip: what a live conversation needs is what it cost and how much work
          it did — the exact token split lives one click away in /context. */}
      {usage && (usage.credits ?? 0) > 0 && (
        <span className="chat-chip is-mono" title={t("cost.credits.title")}>
          <Gem size={11} strokeWidth={2.5} />
          {fmtCreditsAmount(usage.credits ?? 0)}
        </span>
      )}
      {usage && (usage.usd ?? 0) > 0 && (
        <span className="chat-chip is-mono" title={t("cost.title")}>
          {/* A key, not a dollar sign: fmtUsd already prints "$", and the two
              together rendered "$ $0.870". The key is also the better glyph —
              it says *whose* bill this is, matching Insights' "Your API keys"
              band, where the gem/key pair is the same distinction. */}
          <KeyRound size={11} strokeWidth={2.5} />
          {fmtUsd(usage.usd ?? 0)}
        </span>
      )}

      {usage && (usage.calls ?? 0) > 0 && (
        <span className="chat-chip is-mono" title={t("calls.title")}>
          <Cpu size={11} strokeWidth={2.5} />
          {(usage.calls ?? 0).toLocaleString()}
        </span>
      )}

      {usage && (usage.toolCalls ?? 0) > 0 && (
        <span className="chat-chip is-mono" title={t("toolCalls.title")}>
          <Wrench size={11} strokeWidth={2.5} />
          {(usage.toolCalls ?? 0).toLocaleString()}
        </span>
      )}

      {/* The agent's profile card closes the row, on the edge its drawer
          arrives from — where the simple shell's chat header keeps its own
          info button, so it is the same control in the same place in both
          shells. The tinted glyph, not the fill: it is about WHO you are
          talking to rather than which conversation, and a third filled chip
          would make the pair on the left read as three. */}
      <button
        className={`chat-chip is-icon is-action is-trailing${profileOpen ? " is-on" : ""}`}
        onClick={onToggleProfile}
        title={profileOpen ? t("profile.hide") : t("profile.show")}
        aria-label={profileOpen ? t("profile.hide") : t("profile.show")}
        aria-expanded={profileOpen}
        aria-haspopup="dialog"
        data-testid="chat-profile-toggle"
      >
        <Info size={14} strokeWidth={2} />
      </button>
    </div>
  );
}
