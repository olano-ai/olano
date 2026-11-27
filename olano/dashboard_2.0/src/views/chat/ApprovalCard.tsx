import { AlertTriangle, Check, CheckCheck, Globe, X } from "lucide-react";
import { useT } from "../../core/i18n";
import type { ApprovalAction } from "../../core/types";
import { needsPersonEveryTime } from "../browser/approval";
import { displayUrl } from "../browser/LiveScreen";
import { STR } from "./strings";
import { summarizeApproval } from "./approvalSummary";

export type ApprovalChoice = "once" | "all" | "reject";

export interface ApprovalState {
  actions: ApprovalAction[];
  resolved: boolean;
  // How it was resolved; `auto` marks a batch approved by a prior "Accept all".
  choice?: ApprovalChoice;
  auto?: boolean;
  // The stream carrying this approval died before the user decided — the
  // backend auto-rejected it, so the card renders as expired (no buttons).
  expired?: boolean;
}

const RISK_COLOR: Record<string, string> = {
  high: "var(--danger)",
  medium: "var(--warn)",
  low: "var(--text-3)",
};

function argsString(a: ApprovalAction): string {
  const raw = a.args ?? a.tool_input ?? a.input ?? "";
  if (raw === "" || raw === null || raw === undefined) return "";
  if (typeof raw === "string") return raw;
  try {
    return JSON.stringify(raw, null, 2);
  } catch {
    return String(raw);
  }
}

interface BrowserAsk {
  action: string;
  site: string;
  why: string;
  sendsTo: string;
  /** A rule change: the rules as they would be after it (one per line). */
  details: string;
}

function capitalize(text: string): string {
  const v = text.trim();
  return v ? v.charAt(0).toUpperCase() + v.slice(1) + (/[.!?]$/.test(v) ? "" : ".") : "";
}

/** A browser approval in words: what the agent will do, where, and why it
 *  asks. The args are the ones olano/browser/consequence.py sends. */
function browserAsk(a: ApprovalAction): BrowserAsk | null {
  if (!needsPersonEveryTime([a])) return null;
  const raw = a.args ?? a.tool_input ?? a.input;
  if (!raw || typeof raw !== "object") return null;
  const args = raw as Record<string, unknown>;
  const action = typeof args.action === "string" ? args.action.trim() : "";
  if (!action) return null;
  const str = (k: string) => (typeof args[k] === "string" ? (args[k] as string) : "");
  return {
    action: action.charAt(0).toUpperCase() + action.slice(1),
    site: displayUrl(str("url")),
    why: capitalize(str("why_approval")),
    sendsTo: displayUrl(str("form_sends_to")),
    details: str("details").trim(),
  };
}

// Inline HITL approval card. The runner is paused server-side until the user
// decides; `onDecide` POSTs the decision to /api/chat/approval.
//  • Accept once  — approve this batch, keep asking for later ones
//  • Accept all   — approve this batch and auto-approve the rest of this turn
//  • Reject       — deny this batch
export function ApprovalCard({
  state,
  onDecide,
  plain = false,
}: {
  state: ApprovalState;
  onDecide: (choice: ApprovalChoice) => void;
  /** The simple shell: say what the agent wants to do in words, with the
   *  tool name and raw arguments behind a disclosure (approvalSummary.ts). */
  plain?: boolean;
}) {
  const t = useT(STR);
  const { actions, resolved, choice, auto } = state;

  if (resolved) {
    if (state.expired) {
      return (
        <div className="approval-card is-resolved is-reject">
          <div className="approval-head">
            <AlertTriangle size={14} />
            <span className="approval-title">{t("approval.expired")}</span>
          </div>
        </div>
      );
    }
    const key = auto ? "approval.autoApproved" : choice === "reject" ? "approval.rejected" : choice === "all" ? "approval.acceptedAll" : "approval.accepted";
    const reject = choice === "reject";
    return (
      <div className={`approval-card is-resolved${reject ? " is-reject" : ""}`}>
        <div className="approval-head">
          {reject ? <X size={14} /> : <Check size={14} />}
          <span className="approval-title">{t(key)}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="approval-card">
      <div className="approval-head">
        <AlertTriangle size={14} />
        <span className="approval-title">{t("approval.needed")}</span>
        <span className="muted" style={{ fontSize: 11 }}>
          {t(actions.length === 1 ? "approval.blocked.one" : "approval.blocked.many", { n: String(actions.length) })}
        </span>
      </div>
      <div className="approval-list">
        {actions.map((a, i) => {
          const ask = browserAsk(a);
          if (ask) {
            return (
              <div className="approval-row approval-browser" key={i} data-testid="approval-browser">
                <div className="approval-browser-action">{ask.action}</div>
                {ask.site && (
                  <div className="approval-browser-site">
                    <Globe size={12} aria-hidden="true" /> <span>{ask.site}</span>
                  </div>
                )}
                {ask.why && <div className="approval-browser-why">{ask.why}</div>}
                {ask.sendsTo && ask.sendsTo !== ask.site && (
                  <div className="approval-browser-why">{t("approval.browserSendsTo", { site: ask.sendsTo })}</div>
                )}
                {ask.details && (
                  <pre className="approval-browser-details" data-testid="approval-browser-details">
                    {ask.details}
                  </pre>
                )}
                <div className="approval-browser-note">{t("approval.browserNote")}</div>
              </div>
            );
          }
          const args = argsString(a);
          if (plain) {
            const sum = summarizeApproval(a.name, a.args ?? a.tool_input ?? a.input);
            return (
              <div className="approval-row approval-plain" key={i} data-testid="approval-plain">
                <div className="approval-plain-say">
                  {t(`approval.say.${sum.say}`, { tool: sum.tool, service: sum.service || sum.tool })}
                </div>
                {sum.target && (
                  <div className="approval-plain-target">
                    <span className="muted">{t(`approval.target.${sum.target.key}`)}:</span>{" "}
                    <span className="approval-plain-value">{sum.target.value.slice(0, 200)}</span>
                  </div>
                )}
                <details className="approval-plain-details">
                  <summary>{t("approval.details")}</summary>
                  <div className="approval-row-head">
                    <span className="approval-row-name">{a.name || "tool"}</span>
                    {a.risk && (
                      <span className="badge" style={{ padding: "0 6px", color: RISK_COLOR[a.risk] || "var(--text-3)" }}>
                        {a.risk}
                      </span>
                    )}
                  </div>
                  {args && <pre className="approval-row-args">{args.slice(0, 600)}</pre>}
                </details>
              </div>
            );
          }
          return (
            <div className="approval-row" key={i}>
              <div className="approval-row-head">
                <span className="approval-row-name">{a.name || "tool"}</span>
                {a.risk && (
                  <span
                    className="badge"
                    style={{ padding: "0 6px", color: RISK_COLOR[a.risk] || "var(--text-3)" }}
                  >
                    {a.risk}
                  </span>
                )}
                {a.interrupt_id && <span className="approval-row-id">{String(a.interrupt_id).slice(0, 8)}</span>}
              </div>
              {args && <pre className="approval-row-args">{args.slice(0, 600)}</pre>}
            </div>
          );
        })}
      </div>
      {plain && !needsPersonEveryTime(actions) && <div className="approval-plain-note">{t("approval.plainNote")}</div>}
      <div className="approval-actions">
        <button className="btn" style={{ padding: "4px 11px", fontSize: 12 }} onClick={() => onDecide("reject")}>
          <X size={13} /> {t("approval.reject")}
        </button>
        {/* A browser submit, purchase or send is asked every time, so "all"
            would promise something it never does (views/browser/approval.ts). */}
        {!needsPersonEveryTime(actions) && (
          <button className="btn" style={{ padding: "4px 11px", fontSize: 12 }} onClick={() => onDecide("all")}>
            <CheckCheck size={13} /> {t("approval.acceptAll")}
          </button>
        )}
        <button className="btn primary" style={{ padding: "4px 11px", fontSize: 12 }} onClick={() => onDecide("once")}>
          <Check size={13} /> {t("approval.acceptOnce")}
        </button>
      </div>
    </div>
  );
}
