// How much of a bot's WORK the simple shell shows, and the pieces that render it.
//
// Three levels, the same three the dashboard chat has: `quiet` (replies only),
// `normal` (one compact line per tool call), `verbose` (full call/result cards
// plus subagent and peer transcripts). It is a reading preference, so it lives
// in localStorage — per browser, not per account and not per bot.
//
// One module because the 1:1 chat and the group thread must render the same
// trail from the same preference plumbing. They had one copy each of the header
// button and the tool markup, and the group had neither — which is how a group
// chat, the one place where watching bots work is the entire point, ended up
// showing only the final paragraph.
//
// The two surfaces keep SEPARATE keys and different defaults on purpose:
//
//   - a 1:1 chat defaults to `quiet` — you asked one bot a question and you
//     want its answer;
//   - a group defaults to `normal` — the collaboration is the thing you opened
//     it to watch, and a group that hides every handoff and every peer call
//     reads like three strangers monologuing.

import { useCallback, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { CheckIcon, SubIcon, ToolsIcon } from "../../ui/icons";
import { Markdown } from "../../ui/Markdown";
import { useToast } from "../../ui/toast";
import { nextDetail, type ChatDetail } from "../chat/prefs";
import type { Sub, ToolCall } from "../chat/sessionStore";
import { parseTaskArgs, peekText } from "../chat/toolPeek";
import { BotMark } from "./BotMark";
import { STR } from "./strings";

/** Where each surface's level is stored. Separate keys, different defaults. */
export const CHAT_DETAIL_KEY = "lc.simple.detail";
export const GROUP_DETAIL_KEY = "lc.simple.groupDetail";

export function readDetail(key: string, fallback: ChatDetail): ChatDetail {
  try {
    const raw = localStorage.getItem(key);
    return raw === "quiet" || raw === "normal" || raw === "verbose" ? raw : fallback;
  } catch {
    // storage disabled or corrupt — the surface's own default.
    return fallback;
  }
}

export function writeDetail(key: string, detail: ChatDetail): void {
  try {
    localStorage.setItem(key, detail);
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

/**
 * The current level for one surface, plus the setter both the header button and
 * the `/quiet` `/normal` `/verbose` commands go through.
 *
 * The confirmation toast lives here rather than at the two call sites so a
 * level change says the same thing however it was made.
 */
export function useDetail(
  key: string,
  fallback: ChatDetail,
): [ChatDetail, (next: ChatDetail) => void, () => void] {
  const t = useT(STR);
  const toast = useToast();
  const [detail, setDetail] = useState<ChatDetail>(() => readDetail(key, fallback));
  const change = useCallback(
    (next: ChatDetail) => {
      setDetail(next);
      writeDetail(key, next);
      toast.push(t("detail.set", { level: t(`detail.${next}`) }));
    },
    [key, t, toast],
  );
  const cycle = useCallback(() => change(nextDetail(readDetail(key, fallback))), [
    change,
    key,
    fallback,
  ]);
  return [detail, change, cycle];
}

/**
 * The header control: cycles quiet → normal → verbose.
 *
 * The visible label names the CURRENT level (it is a state indicator first);
 * the title explains all three and that a click advances to the next, per the
 * help-text rule — a cycle button whose tooltip names only one state is a
 * mystery box.
 */
export function DetailButton({ detail, onCycle }: { detail: ChatDetail; onCycle: () => void }) {
  const t = useT(STR);
  return (
    <button
      type="button"
      className="simple-head-btn"
      onClick={onCycle}
      data-detail={detail}
      data-testid="simple-detail-btn"
      aria-label={t("detail.cycleAria", { level: t(`detail.${detail}`) })}
      title={t("detail.help")}
    >
      <SlidersHorizontal size={15} strokeWidth={1.75} />
      <span className="simple-head-new-label">{t(`detail.${detail}`)}</span>
    </button>
  );
}

/**
 * What a bot did on its way to a reply, drawn at the reader's chosen level.
 *
 * `quiet` renders nothing at all — not an empty wrapper, `null` — so a
 * tool-only turn can be skipped entirely by the caller. `normal` is one
 * labelled line per call with its most salient input. `verbose` adds the raw
 * arguments, the result, and any subagent or peer transcript.
 *
 * A `task` call is labelled by the subagent it delegates to rather than by the
 * tool's name: "Asked researcher" is what happened; "Called task" is trivia.
 */
export function ToolTrail({
  tools,
  subs,
  detail,
}: {
  tools: ToolCall[];
  subs: Sub[];
  detail: ChatDetail;
}) {
  const t = useT(STR);
  if (detail === "quiet") return null;
  const named = tools.filter((c) => c.name);
  const showSubs = detail === "verbose";
  if (named.length === 0 && !(showSubs && subs.length)) return null;
  return (
    <>
      {named.map((c) =>
        detail === "verbose" ? (
          // The wrapper is the trail's unit at this level (a call plus its
          // result), and it is what the spacing rule in simple.css keys on to
          // put a gap between the trail and the reply that follows it.
          <div key={c.id} className="simple-tool-group">
            <div className="tool-card simple-tool-card">
              <ToolsIcon size={12} /> {c.name}({(c.args || "").slice(0, 400)})
            </div>
            {c.hasResult && (
              <div className="tool-card simple-tool-card">
                <CheckIcon size={12} /> {(c.result || "").slice(0, 600) || c.name}
              </div>
            )}
          </div>
        ) : (
          <CompactToolLine key={c.id} call={c} t={t} />
        ),
      )}
      {showSubs &&
        subs.map((s) => (
          <div key={s.path} className="simple-tool-group">
            <div className="muted simple-sub-path">
              <SubIcon size={12} /> {s.path}
            </div>
            <div className="bubble assistant">
              <Markdown text={s.text} />
            </div>
          </div>
        ))}
    </>
  );
}

/**
 * A member reaching another bot directly. Mirrors the runner's own
 * `_PEER_TOOL_NAMES` — the set that decides which calls get announced early and
 * whose text gets rerouted as peer output, so the two must agree about what
 * counts as a peer call.
 */
const PEER_TOOLS = new Set(["talk_to_agent", "talk_to"]);

/**
 * Who a peer call is aimed at, out of its arguments.
 *
 * The runner announces a peer call as soon as the target is readable rather
 * than waiting for the whole argument object (that is the point of announcing
 * it — the wait is what the reader needs to be told about), so the args can be
 * a partial JSON fragment. The regex is the fallback for exactly that window.
 */
function peerTarget(args: string): string {
  try {
    const obj = JSON.parse(args) as Record<string, unknown>;
    if (obj && typeof obj === "object") return String(obj.agent_name || obj.agent || "").trim();
  } catch {
    // Still streaming — fall through to the partial match.
  }
  const m = /"agent_name"\s*:\s*"([^"]+)"/.exec(args);
  return m ? m[1].trim() : "";
}

/**
 * The peers a member is WAITING ON right now: a peer call announced, no result
 * back yet. Deduped case-insensitively, because the name in the arguments is
 * whatever the model typed ("Markie") and the resolver is case-insensitive.
 */
export function pendingPeers(tools: ToolCall[]): string[] {
  const out: string[] = [];
  for (const c of tools) {
    if (!PEER_TOOLS.has(c.name) || c.hasResult) continue;
    const peer = peerTarget(c.args || "");
    if (peer && !out.some((n) => n.toLowerCase() === peer.toLowerCase())) out.push(peer);
  }
  return out;
}

/** The roster entry for a peer, matched the way the backend matches it. */
function findAgent(agents: Map<string, Agent> | undefined, name: string): Agent | undefined {
  if (!agents) return undefined;
  const exact = agents.get(name);
  if (exact) return exact;
  const lower = name.toLowerCase();
  for (const [key, agent] of agents) if (key.toLowerCase() === lower) return agent;
  return undefined;
}

/**
 * "Maria is thinking…" — the bot a member has just handed the question to.
 *
 * A group turn used to go silent for as long as a peer took to answer: the
 * `talk_to_agent` row appeared, and then nothing, because the caller's own
 * model is suspended until the peer returns and the peer's words are rerouted
 * out of the caller's reply. A minute of nothing under a member's face reads as
 * a hang, when what is actually happening is the most interesting thing in the
 * room — one bot waiting on another.
 *
 * Rendered at EVERY detail level, quiet included: this is attribution ("who is
 * talking now"), which a group exists to show, not the tool chrome the detail
 * levels gate. It clears itself when the result lands.
 */
export function PeerThinking({
  tools,
  asker,
  agentsByName,
}: {
  tools: ToolCall[];
  /** The member doing the asking — named in the row's explanation. */
  asker: string;
  agentsByName?: Map<string, Agent>;
}) {
  const t = useT(STR);
  const peers = pendingPeers(tools);
  if (peers.length === 0) return null;
  return (
    <>
      {peers.map((peer) => {
        const agent = findAgent(agentsByName, peer);
        const name = agent?.name || peer;
        return (
          <div
            key={peer.toLowerCase()}
            className="simple-working simple-peer-thinking"
            role="status"
            title={t("peer.thinkingHelp", { name, asker })}
          >
            <BotMark name={name} avatarUrl={agent?.avatar_url} emoji={agent?.emoji} size={18} />
            <span>{t("peer.thinking", { name })}</span>
            <span className="typing" aria-hidden>
              <span />
              <span />
              <span />
            </span>
          </div>
        );
      })}
    </>
  );
}

function CompactToolLine({
  call,
  t,
}: {
  call: ToolCall;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const isTask = call.name === "task";
  const info = isTask ? parseTaskArgs(call.args) : null;
  const label = isTask
    ? t("detail.subagent", { name: info?.subagent || t("detail.subagentFallback") })
    : t("detail.tool", { name: call.name });
  const peek = peekText(isTask ? info?.description || "" : call.args || "");
  return (
    // The row is one line and truncates with an ellipsis — it is a rhythm of
    // steps, not a log. The full text goes on the tooltip so a truncated
    // argument is still readable without switching detail levels: before the
    // column was clamped this row simply grew to whatever the arguments were
    // and dragged a horizontal scrollbar under the whole conversation.
    <div className="tool-activity" title={peek ? `${label} — ${peek}` : label}>
      {isTask ? <SubIcon size={12} /> : <ToolsIcon size={12} />}
      <span className="tool-activity-label">{label}</span>
      {peek && <span className="tool-activity-peek">{peek}</span>}
    </div>
  );
}
