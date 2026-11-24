import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useT } from "../i18n";
import { setErrorToastSuppression, useToast } from "../../ui/toast";
import { useServerHealth, type BootProgress } from "./useServerHealth";
import { consoleLink, deploymentKind, type DeploymentKind } from "./deployment";
import { StreamStatusPill } from "./StreamStatusPill";
import { STR } from "./strings";

// App-wide runtime-connectivity monitor. When the Olano runtime (:8765) is
// stopped, restarting, or still working through its first boot, every polling
// view fails at once — so instead of a flood of error toasts we show ONE calm,
// blurred, non-dismissible overlay (mirroring the Vault unlock modal), suppress
// error toasts while it's up, and auto-recover the moment the server answers.
//
// What the overlay OFFERS depends on who operates the box, because the wrong
// answer is worse than none:
//
//   • managed (an Olano cloud instance) — the human has no shell there and
//     Docker already restarts the runtime on its own. So: no terminal command,
//     no restart button, just an honest "it's coming back" and a link to the
//     console. Telling a cloud customer to run `olano server start` sent
//     them looking for a terminal that does not exist for them.
//   • self-hosted — their machine, their terminal. Keep the copyable start
//     command, plus a real restart button IF the dashboard is served by a
//     process that exposes /__control/restart (the Node proxy or the Vite dev
//     plugin). The managed stack serves the SPA through Caddy, whose
//     `try_files {path} /index.html` fallback answers ANY unknown GET with the
//     index page — so a bare `res.ok` check saw a control plane on every cloud
//     instance and offered a button whose POST could only ever 405. The ping
//     must therefore prove itself with the JSON body, not the status code.
//
// What the overlay must NOT cover is a runtime that is merely still booting its
// agents. The API binds before the boot loop, so during it the dashboard, login
// and every already-booted agent work normally — blocking all of that behind a
// non-dismissible blur for the length of a boot (minutes on a many-agent host)
// reported a healthy system as a broken one. That state gets `BootProgressPill`
// instead: same information, no blocking, and it counts down "6 of 15 ready" so
// the wait has a visible end.

const GRACE_MS = 4000; // don't flash the overlay on a brief blip
const ESCALATE_MS = 20000; // after this, show guidance/restart for a real outage
const START_CMD = "olano server start";

export function ServerMonitorProvider({ children }: { children: ReactNode }) {
  const { health, booting, progress } = useServerHealth(2500);
  const { push } = useToast();
  const t = useT(STR);

  const [, forceTick] = useState(0);
  const troubledSinceRef = useRef<number | null>(null);
  const shownRef = useRef(false);
  const [restarting, setRestarting] = useState(false);
  const [restartFailed, setRestartFailed] = useState(false);
  const [canRestart, setCanRestart] = useState(false);

  // Managed vs self-hosted. Resolved from the hostname plus the last successful
  // probe, so it is still known while the runtime is unreachable — which is the
  // only moment it matters.
  const kind = useMemo<DeploymentKind>(() => deploymentKind(), []);
  const managed = kind === "managed";

  // Stable refs for the toast/translator so the transition effect below doesn't
  // re-run on every ToastProvider render or language switch.
  const pushRef = useRef(push);
  pushRef.current = push;
  const tRef = useRef(t);
  tRef.current = t;

  // Feature-detect the restart control endpoint once (served by the dashboard
  // process, not proxied to the runtime — so it answers even when :8765 is down).
  // A managed instance never has one, so don't even ask.
  useEffect(() => {
    if (managed) return;
    let alive = true;
    fetch("/__control/ping", { method: "GET" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => alive && setCanRestart(body?.ok === true))
      .catch(() => alive && setCanRestart(false));
    return () => {
      alive = false;
    };
  }, [managed]);

  // React to online/trouble transitions: suppress error toasts while the server
  // isn't answering, and announce recovery (only if the overlay was actually
  // shown, so a sub-grace blip doesn't produce a spurious "back online" toast).
  useEffect(() => {
    if (health === "offline" || health === "starting") {
      if (troubledSinceRef.current === null) troubledSinceRef.current = Date.now();
      setErrorToastSuppression(true);
      forceTick((n) => n + 1);
    } else if (health === "online") {
      setErrorToastSuppression(false);
      if (shownRef.current) pushRef.current(tRef.current("backOnline"), "info");
      troubledSinceRef.current = null;
      shownRef.current = false;
      setRestarting(false);
      setRestartFailed(false);
      forceTick((n) => n + 1);
    }
    // "checking" → leave current state untouched
  }, [health]);

  // Tick once a second while down so the elapsed counter and grace/escalate
  // thresholds advance without waiting for the next health poll.
  useEffect(() => {
    if (health === "online" || health === "checking") return;
    const id = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [health]);

  // Never leave error toasts globally suppressed if the provider unmounts.
  useEffect(() => () => setErrorToastSuppression(false), []);

  const handleRestart = useCallback(() => {
    setRestarting(true);
    setRestartFailed(false);
    fetch("/__control/restart", { method: "POST" })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        // Success: keep "Restarting…"; the health poll will flip to online and
        // close the overlay on its own.
      })
      .catch(() => {
        setRestarting(false);
        setRestartFailed(true);
      });
  }, []);

  const elapsed = troubledSinceRef.current ? Date.now() - troubledSinceRef.current : 0;
  const show = troubledSinceRef.current !== null && elapsed >= GRACE_MS;
  if (show) shownRef.current = true;

  return (
    <>
      {children}
      {/* Stream-reconnect pill: non-blocking, suppressed while the full
          connectivity overlay is up (the overlay already says it all). */}
      <StreamStatusPill suppressed={show || booting} />
      {/* Agents still booting: informative, never blocking. Suppressed if the
          server actually goes unreachable — that overlay outranks it. */}
      {booting && !show && <BootProgressPill progress={progress} />}
      {show && (
        <ConnectivityOverlay
          seconds={Math.floor(elapsed / 1000)}
          // "starting" is a server that answered and said it is still booting,
          // or one too busy to answer at all — never escalate that into an
          // outage report, however long it takes.
          starting={health === "starting"}
          escalated={health === "offline" && elapsed >= ESCALATE_MS}
          managed={managed}
          canRestart={canRestart}
          restarting={restarting}
          restartFailed={restartFailed}
          onRestart={handleRestart}
        />
      )}
    </>
  );
}

// "Your agents are still waking up" — a status, not an obstacle. Same pill
// vocabulary as StreamStatusPill (fixed, top-centre, non-modal) so the two read
// as one family; it disappears by itself when the last agent is booted.
function BootProgressPill({ progress }: { progress: BootProgress }) {
  const t = useT(STR);
  const label =
    progress.total !== null
      ? t("pill.booting.count", { n: String(progress.ready), total: String(progress.total) })
      : t("pill.booting");
  return (
    <div
      role="status"
      style={{
        position: "fixed",
        top: "calc(10px + env(safe-area-inset-top))",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 90,
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 14px",
        borderRadius: 999,
        background: "var(--surface-2)",
        border: "1px solid var(--border-strong)",
        boxShadow: "var(--shadow-lg)",
        fontSize: 12.5,
        color: "var(--text-2)",
        maxWidth: "calc(100vw - 24px)",
        animation: "pill-fade var(--t) var(--ease)",
      }}
    >
      <span className="spinner" style={{ width: 12, height: 12, flexShrink: 0 }} />
      <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {label}
      </span>
      <style>{`@keyframes pill-fade { from { opacity: 0; transform: translate(-50%, -6px) } to { opacity: 1; transform: translate(-50%, 0) } }`}</style>
    </div>
  );
}

function ConnectivityOverlay({
  seconds,
  starting,
  escalated,
  managed,
  canRestart,
  restarting,
  restartFailed,
  onRestart,
}: {
  seconds: number;
  starting: boolean;
  escalated: boolean;
  managed: boolean;
  canRestart: boolean;
  restarting: boolean;
  restartFailed: boolean;
  onRestart: () => void;
}) {
  const t = useT(STR);
  const [copied, setCopied] = useState(false);

  const copy = () => {
    void navigator.clipboard?.writeText(START_CMD);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  const title = starting ? t("starting.title") : escalated ? t("down.title") : t("title");
  const body = starting
    ? t("starting.body")
    : escalated
      ? managed
        ? t("down.body.managed")
        : t("down.body")
      : t("reconnecting");
  const console_ = managed ? consoleLink() : null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(5, 4, 12, 0.6)",
        backdropFilter: "blur(3px)",
        WebkitBackdropFilter: "blur(3px)",
        zIndex: 100,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        animation: "sm-fade var(--t) var(--ease)",
      }}
    >
      <div
        className="card"
        style={{
          padding: 24,
          width: 420,
          maxWidth: "100%",
          boxShadow: "var(--shadow-lg)",
          border: "1px solid var(--border-strong)",
          animation: "sm-pop var(--t) var(--ease)",
        }}
      >
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <span className="spinner" style={{ width: 16, height: 16 }} />
          <h1 style={{ margin: 0, fontSize: 17 }}>{title}</h1>
        </div>

        <p className="muted" style={{ margin: "10px 0 0", fontSize: 13 }}>
          {body}
        </p>
        <p className="muted" style={{ margin: "6px 0 0", fontSize: 12 }}>
          {/* "Disconnected 21s ago" is the wrong frame for a server that is
              coming up rather than one that went away. */}
          {t(starting ? "starting.elapsed" : "elapsed", { n: String(seconds) })}
        </p>

        {/* Self-hosted only: a shell the human actually has. A managed instance
            gets the console link instead — there is nothing to type there. */}
        {escalated && !managed && (
          <div style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 10 }}>
            {canRestart && (
              <button
                className="btn primary"
                disabled={restarting}
                onClick={onRestart}
                style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8 }}
              >
                {restarting && <span className="spinner" style={{ width: 14, height: 14 }} />}
                {restarting ? t("restarting") : t("restart")}
              </button>
            )}
            {restartFailed && (
              <div className="error-text" style={{ fontSize: 12.5 }}>{t("restart.failed")}</div>
            )}

            <div>
              <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{t("down.cmd")}</div>
              <div
                className="row"
                style={{
                  gap: 8,
                  justifyContent: "space-between",
                  background: "var(--surface-2)",
                  border: "1px solid var(--border)",
                  borderRadius: "var(--r-sm)",
                  padding: "8px 10px",
                }}
              >
                <code style={{ fontSize: 12.5 }}>{START_CMD}</code>
                <button
                  className="btn"
                  style={{ padding: "3px 9px", fontSize: 12 }}
                  onClick={copy}
                >
                  {copied ? t("copied") : t("copy")}
                </button>
              </div>
            </div>
          </div>
        )}

        {escalated && managed && console_ && (
          <div style={{ marginTop: 16 }}>
            <a className="btn" href={console_} target="_blank" rel="noopener noreferrer">
              {t("down.console")}
            </a>
          </div>
        )}
      </div>
      <style>{`
        @keyframes sm-fade { from { opacity: 0 } to { opacity: 1 } }
        @keyframes sm-pop { from { transform: translateY(8px) scale(0.98); opacity: 0 } to { transform: none; opacity: 1 } }
      `}</style>
    </div>
  );
}
