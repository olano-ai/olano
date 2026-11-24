import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Globe, Maximize2, Minimize2, Minus, X } from "lucide-react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { useT } from "../../core/i18n";
import {
  displayUrl,
  type LiveBrowser,
  LiveControls,
  LiveScreen,
  liveStatusKey,
  useLiveBrowser,
} from "./LiveScreen";
import { type DockDismissal, dockHidden, rebaseDismissal } from "./dockActivity";
import { clockText } from "./dockClock";
import { STR } from "./strings";

// The chat's browser dock: while the agent you are chatting with has a live
// browser open, a small window in the corner of the conversation shows it,
// with the agent's cursor moving to what it clicks. Clicking the window (or
// its expand button) opens the same stream large, where a person can take
// control and hand back; collapsing returns to the small window. One socket
// serves every layout, so switching never reconnects.
//
// It appears on the server's browser_session events (opened, closed, attention,
// page) and on mount, only in live mode (headless has nothing to show), and
// hides for good once the session closes. "Hide" dismisses it until the agent
// browses again: the session outlives the turn, so the next question's first
// browser call brings it back (dockActivity.ts), as does a new session.
// "Minimize" folds it into a pill
// (remembered per browser); on a phone it always starts as the pill, because a
// window over a 390px conversation hides the very message it should accompany,
// and tapping the pill opens the full-screen view directly.
//
// When the agent asked for a person (browser_request_user: sign in, a code, a
// CAPTCHA) and that person takes control, handing back is their answer:
// `onHandBack` lets the chat tell the agent so it carries on, instead of the
// conversation sitting still until they think to type "done".
//
// A site asking for a person ("Verify you are human": olano/browser/human_check.py)
// shows as its own banner: Verify now opens the large view with control
// already taken, Skip tells the waiting agent to move on, and a countdown says
// how long the agent waits. Nobody presses Hand back: once the page gets past
// the check the server hands control back and the agent carries on, so the
// large view folds away by itself.
//
// An approval the agent is waiting for (a form submit, purchase or send) is
// answered in the conversation, so while one is pending the dock says so and
// offers to show it, and an expanded view collapses back to the corner and
// scrolls the card into view: a person watching the browser large would
// otherwise never see the question, and the agent would wait until it timed out.

interface HumanCheck {
  site: string;
  mode: string;
  deadline: number | null;
}

interface AgentBrowser {
  live_view: boolean;
  session: {
    opened_at: number;
    controller: string;
    attention: string;
    url?: string;
    human_check?: HumanCheck | null;
  } | null;
}

/** The window's one-line status: shorter than the Browser tab's, which
 *  explains the controls and would be cut off at this width. */
function dockStatusKey(live: LiveBrowser): string {
  if (live.phase === "open") return live.controller === "user" ? "br.dock.st.user" : "br.dock.st.agent";
  return liveStatusKey(live);
}

const MINIMIZED_KEY = "olano.browserDock.minimized";
const PHONE_QUERY = "(max-width: 720px)";

function readMinimized(): boolean {
  try {
    return window.localStorage.getItem(MINIMIZED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeMinimized(value: boolean): void {
  try {
    window.localStorage.setItem(MINIMIZED_KEY, value ? "1" : "0");
  } catch {
    /* private mode or blocked storage: the preference just isn't remembered */
  }
}

function usePhone(): boolean {
  const [phone, setPhone] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(PHONE_QUERY).matches
      : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(PHONE_QUERY);
    const on = () => setPhone(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return phone;
}

export function ChatBrowserDock({
  agentName,
  browserCalls = 0,
  approvalPending = false,
  onShowApproval,
  onHandBack,
}: {
  agentName: string;
  /** Browsing calls the conversation shows; one more re-shows a hidden dock. */
  browserCalls?: number;
  approvalPending?: boolean;
  onShowApproval?: () => void;
  /** The person handed back control the agent had asked them to take. */
  onHandBack?: () => void;
}) {
  const t = useT(STR);
  const phone = usePhone();
  const [info, setInfo] = useState<AgentBrowser | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState<DockDismissal | null>(null);
  const [minimizedPref, setMinimizedPref] = useState(readMinimized);
  // Folded into the pill while an approval waits, so the window never sits
  // over the card it points at; not remembered, it unfolds once answered.
  const [foldedForApproval, setFoldedForApproval] = useState(false);
  const minimized = phone || minimizedPref || foldedForApproval;

  const load = useCallback(() => {
    if (!agentName) return;
    api
      .get<AgentBrowser>(`/api/agents/${encodeURIComponent(agentName)}/browser`)
      .then(setInfo)
      .catch(() => setInfo(null));
  }, [agentName]);

  useEffect(() => {
    setInfo(null);
    setExpanded(false);
    setDismissed(null);
    load();
  }, [load]);

  useEffect(
    () =>
      subscribeStream((frame) => {
        if (frame.type !== "browser_session") return;
        const ev = frame.data as { agent?: string } | undefined;
        if (ev?.agent === agentName) load();
      }),
    [agentName, load],
  );

  useEffect(() => {
    setDismissed((d) => rebaseDismissal(d, browserCalls));
  }, [browserCalls]);

  const session = info?.live_view ? info.session : null;
  const hidden = dockHidden(dismissed, session?.opened_at, browserCalls);
  const visible = !!session && !hidden;
  const live = useLiveBrowser(visible ? agentName : null);
  // Whether the control the person holds is control the agent asked them to
  // take (its request was showing when they pressed Take control).
  const answeringRequest = useRef(false);

  // A request for a person (sign-in, CAPTCHA) re-shows a dismissed dock.
  const attention = live.attention || session?.attention || "";
  const humanCheck = session?.human_check || null;
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!humanCheck?.deadline) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [humanCheck?.deadline]);
  // The check is over (passed, skipped, timed out): the server already handed
  // control back, so the large view the person verified in folds away.
  const hadCheck = useRef(false);
  useEffect(() => {
    if (humanCheck) {
      hadCheck.current = true;
    } else if (hadCheck.current) {
      hadCheck.current = false;
      setExpanded(false);
    }
  }, [humanCheck]);
  useEffect(() => {
    if (attention && hidden) setDismissed(null);
  }, [attention, hidden]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && live.controller !== "user") setExpanded(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded, live.controller]);

  useEffect(() => {
    if (!visible) setExpanded(false);
  }, [visible]);

  // A new approval: back to the corner (unless the person holds control and is
  // mid-action in the page) and the card into view; a dismissed dock returns.
  useEffect(() => {
    if (!approvalPending) {
      setFoldedForApproval(false);
      return;
    }
    if (hidden) setDismissed(null);
    setFoldedForApproval(true);
    if (expanded && live.controller !== "user") setExpanded(false);
    const id = window.setTimeout(() => onShowApproval?.(), 60);
    return () => window.clearTimeout(id);
    // Fires on the transition only; `expanded` changing afterwards must not
    // re-collapse a view the person opened again on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [approvalPending]);

  if (!visible || !session) return null;
  const controlling = live.controller === "user";
  const site = displayUrl(live.url || session.url || "");
  const needsYou = !!attention || !!humanCheck || approvalPending;

  const review = () => {
    if (!controlling) setExpanded(false);
    onShowApproval?.();
  };
  const setMin = (value: boolean) => {
    setMinimizedPref(value);
    writeMinimized(value);
  };

  const handBack = () => {
    live.send({ t: "control", who: "agent" });
    if (answeringRequest.current) {
      answeringRequest.current = false;
      onHandBack?.();
    }
  };
  const takeControl = () => {
    // Under a human check the waiting agent sees the page clear by itself;
    // a "handed back" message on top would only repeat it.
    answeringRequest.current = !!attention && !humanCheck;
    live.send({ t: "control", who: "user" });
  };
  const verifyNow = () => {
    setExpanded(true);
    if (!controlling) takeControl();
  };
  const skipCheck = () => {
    api
      .post(`/api/agents/${encodeURIComponent(agentName)}/browser/human-check/skip`)
      .then(() => {
        if (controlling) live.send({ t: "control", who: "agent" });
        setExpanded(false);
      })
      .catch(() => load());
  };

  const controlButton = controlling ? (
    <button className="btn primary sm" data-testid="dock-hand-back" onClick={handBack}>
      {t("br.tab.handBack")}
    </button>
  ) : (
    <button className="btn primary sm" data-testid="dock-take-control" onClick={takeControl}>
      {t("br.tab.take")}
    </button>
  );

  // What the agent is doing, or what it needs from the person, in one line.
  const checkWait = humanCheck
    ? humanCheck.deadline
      ? t("br.dock.checkLeft", { left: clockText(humanCheck.deadline - now) })
      : t("br.dock.checkNoLimit")
    : "";
  const status = approvalPending ? (
    <div className="browser-dock-banner" role="alert" data-testid="dock-approval">
      <AlertTriangle size={14} aria-hidden="true" />
      <span className="grow">{t("br.dock.approvalHint")}</span>
      {onShowApproval && (
        <button className="btn primary xs" data-testid="dock-review-approval" onClick={review}>
          {t("br.dock.review")}
        </button>
      )}
    </div>
  ) : humanCheck ? (
    <div className="browser-dock-banner check" role="alert" data-testid="dock-human-check">
      <AlertTriangle size={14} aria-hidden="true" />
      <span className="grow">
        {t("br.dock.check", { site: humanCheck.site })} <span className="muted">{checkWait}</span>
      </span>
      {!controlling && !expanded && (
        <button className="btn primary xs" data-testid="dock-verify" onClick={verifyNow}>
          {t("br.dock.verify")}
        </button>
      )}
      <button className="btn ghost xs" data-testid="dock-skip-check" onClick={skipCheck}>
        {t("br.dock.skip")}
      </button>
    </div>
  ) : attention ? (
    <div className="browser-dock-banner" role="status">
      <AlertTriangle size={14} aria-hidden="true" />
      <span className="grow">{t("br.tab.attention", { reason: attention })}</span>
      {!expanded && (
        <button className="btn primary xs" onClick={() => setExpanded(true)}>
          {t("br.tab.take")}
        </button>
      )}
    </div>
  ) : (
    <div className="browser-dock-status">
      <span className={`state-dot${controlling ? " user" : live.phase === "open" ? " on" : ""}`} />
      <span>{t(dockStatusKey(live), { reason: live.error })}</span>
    </div>
  );

  if (expanded) {
    return createPortal(
      <div
        className="browser-dock-overlay"
        role="dialog"
        aria-modal="true"
        aria-label={t("br.dock.title", { agent: agentName })}
        data-testid="browser-dock-expanded"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget && !controlling) setExpanded(false);
        }}
      >
        <div className="browser-dock-panel">
          <div className="browser-dock-head large">
            <span className="live-badge">
              <span className="live-dot" />
              {t("br.dock.live")}
            </span>
            <span className="agent">{agentName}</span>
            {site && (
              <span className="site" title={live.url}>
                <Globe size={12} aria-hidden="true" />
                <span>{site}</span>
              </span>
            )}
            <span className="spacer" />
            {controlButton}
            <button
              className="btn ghost sm icon"
              aria-label={t("br.dock.collapse")}
              title={t("br.dock.collapse")}
              data-testid="dock-collapse"
              disabled={controlling}
              onClick={() => setExpanded(false)}
            >
              <Minimize2 size={15} />
            </button>
          </div>
          {status}
          {live.phase === "open" && !controlling && !needsYou && (
            <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
              {t("br.dock.hint")}
            </p>
          )}
          <LiveControls live={live} />
          <LiveScreen live={live} interactive />
        </div>
      </div>,
      document.body,
    );
  }

  if (minimized) {
    return (
      <div
        className={`browser-pill${needsYou ? " needs-you" : ""}`}
        data-testid="browser-dock"
        role="complementary"
        aria-label={t("br.dock.title", { agent: agentName })}
      >
        <button
          className="browser-pill-main"
          data-testid="dock-expand"
          onClick={() => {
            if (phone) return setExpanded(true);
            setFoldedForApproval(false);
            setMin(false);
          }}
          title={phone ? t("br.dock.expand") : t("br.dock.restore")}
        >
          <span className="live-dot" />
          <span className="title">
            {approvalPending
              ? t("br.dock.pillApproval")
              : humanCheck
                ? t("br.dock.pillCheck")
                : attention
                ? t("br.dock.pillNeedsYou")
                : t("br.dock.pill")}
          </span>
        </button>
        {approvalPending && onShowApproval && (
          <button className="btn primary xs" data-testid="dock-review-approval" onClick={review}>
            {t("br.dock.review")}
          </button>
        )}
        {!approvalPending && humanCheck && (
          <button className="btn primary xs" data-testid="dock-verify" onClick={verifyNow}>
            {t("br.dock.verify")}
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      className={`browser-dock${needsYou ? " needs-you" : ""}`}
      data-testid="browser-dock"
      role="complementary"
      aria-label={t("br.dock.title", { agent: agentName })}
    >
      <div className="browser-dock-head">
        <span className="live-badge">
          <span className="live-dot" />
          {t("br.dock.live")}
        </span>
        <span className="agent title" title={agentName}>
          {agentName}
        </span>
        <span className="spacer" />
        <button
          className="btn ghost xs icon"
          aria-label={t("br.dock.expand")}
          title={t("br.dock.expand")}
          data-testid="dock-expand"
          onClick={() => setExpanded(true)}
        >
          <Maximize2 size={13} />
        </button>
        <button
          className="btn ghost xs icon"
          aria-label={t("br.dock.minimize")}
          title={t("br.dock.minimize")}
          data-testid="dock-minimize"
          onClick={() => setMin(true)}
        >
          <Minus size={13} />
        </button>
        <button
          className="btn ghost xs icon"
          aria-label={t("br.dock.hide")}
          title={t("br.dock.hide")}
          onClick={() => setDismissed({ openedAt: session.opened_at, calls: browserCalls })}
        >
          <X size={13} />
        </button>
      </div>
      {site && (
        <div className="browser-dock-site" title={live.url}>
          <Globe size={11} aria-hidden="true" />
          <span>{site}</span>
        </div>
      )}
      <div className="browser-dock-screen">
        <LiveScreen
          live={live}
          interactive={false}
          rounded={8}
          onActivate={() => setExpanded(true)}
          label={t("br.dock.expand")}
        />
        {!live.frame && (
          <span className="browser-dock-placeholder">{t(dockStatusKey(live), { reason: live.error })}</span>
        )}
        <span className="browser-dock-hover" aria-hidden="true">
          <Maximize2 size={14} /> {t("br.dock.openHint")}
        </span>
      </div>
      {status}
    </div>
  );
}
