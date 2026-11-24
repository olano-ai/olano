import { useEffect, useRef, useState } from "react";
import { getStreamState } from "../agentStream";
import { rememberDeploymentKind } from "./deployment";

export type Health = "checking" | "online" | "starting" | "offline";

// Poll the public /api/status readiness probe so the UI can tell "server
// starting/restarting/down" apart from auth or data errors. Shared by the login
// screen and the app-wide ServerMonitorProvider so there's one source of truth.
// `intervalMs` controls the cadence.
//
// Robustness: a single failed/aborted probe does NOT flip the UI offline. The
// status fetch shares the browser's per-host connection pool with the app's
// live `/api/stream` connection and other requests, so a momentary queue stall
// can abort one probe even though the server is perfectly healthy. We therefore
// debounce failures and treat the shared stream being "open" as direct proof
// the server is reachable, suppressing "offline" entirely while it is. This is
// what stops the spurious "Connection lost" flapping.
//
// Beyond that, HOW a probe fails carries most of the signal, and conflating the
// two modes is what made a healthy box look dead:
//
//   • unreachable — the reverse proxy answered 502/503/504, or the connection
//     was refused outright. Nothing is listening: a real outage.
//   • timeout — the connection was accepted but no response arrived in budget.
//     The runtime is UP and too busy to answer. That is the normal state of a
//     freshly provisioned instance, where first boot (image pulls, quick-setup,
//     agent build, skill/catalog sync) saturates a core for minutes while the
//     dashboard is already being served. Reporting that as "the server is
//     offline" sent people hunting for a shell to restart a server that had
//     never stopped.
//
// So timeouts resolve to "starting" and only escalate to "offline" after a run
// long enough to mean a genuine hang, while an explicitly unreachable runtime
// escalates quickly.
//
// `booting: true` is a DIFFERENT thing and is reported separately, not as a
// health state. It means the API is up and answering while the agent-boot loop
// is still working through the roster — the dashboard, login, and every agent
// already booted are fully usable. Folding it into `health` put a blocking
// overlay over a working dashboard for the length of a boot (minutes on a
// many-agent host), which is exactly the "server is down" story the comment
// above exists to avoid telling. Callers get `booting` and decide; nothing
// about it should block.
//
// It IS still latched internally, because a runtime mid-boot may drop
// connections outright: while `booting` was last seen true, a hard failure
// reads as "still coming up" rather than "went away".

// A busy first boot blows straight through a 4s budget, and each abort used to
// count as evidence of death. Give a slow answer room to arrive.
const PROBE_TIMEOUT_MS = 8000;
const UNREACHABLE_TO_OFFLINE = 3;
const TIMEOUT_TO_OFFLINE = 6;

/** Boot progress reported by the runtime while `booting` is true. */
export type BootProgress = { ready: number; total: number | null };

export type ServerHealth = {
  health: Health;
  /** The runtime is up and serving, but still booting agents. Never blocking. */
  booting: boolean;
  progress: BootProgress;
};

export function useServerHealth(intervalMs = 2500): ServerHealth {
  const [health, setHealth] = useState<Health>("checking");
  const [booting, setBooting] = useState(false);
  const [progress, setProgress] = useState<BootProgress>({ ready: 0, total: null });
  const aliveRef = useRef(true);
  const probingRef = useRef(false);
  // Consecutive failures since the last successful probe, by kind.
  const unreachableRef = useRef(0);
  const timeoutRef = useRef(0);
  // Latched once the runtime reports it is still booting, cleared when it
  // reports a completed boot. While set, even a hard failure reads as "still
  // coming up" rather than "went away".
  const bootingRef = useRef(false);
  useEffect(() => {
    aliveRef.current = true;
    let timer: ReturnType<typeof setTimeout>;
    async function probe() {
      if (probingRef.current) return;
      // Hidden tab: skip the network probe but keep the loop armed — the
      // visibilitychange listener below re-probes the moment the user returns,
      // so a backgrounded dashboard stops hitting /api/status every cycle.
      if (document.hidden) {
        timer = setTimeout(probe, intervalMs);
        return;
      }
      probingRef.current = true;
      let ok = false;
      let timedOut = false;
      let stillBooting = false;
      let bootProgress: BootProgress = { ready: 0, total: null };
      let to: ReturnType<typeof setTimeout> | undefined;
      try {
        const ctrl = new AbortController();
        to = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
        const res = await fetch("/api/status", {
          credentials: "include",
          signal: ctrl.signal,
          cache: "no-store",
        });
        ok = res.ok;
        if (ok) {
          // Body is small and already downloaded; a proxy that answered with
          // something other than the status JSON just leaves the extra signals
          // (booting, deployment kind) unset.
          try {
            const data = (await res.json()) as {
              booting?: boolean;
              agents?: string[];
              agents_total?: number | null;
              capabilities?: { deployment_enforced?: boolean };
            };
            stillBooting = data.booting === true;
            bootProgress = {
              ready: Array.isArray(data.agents) ? data.agents.length : 0,
              total: typeof data.agents_total === "number" ? data.agents_total : null,
            };
            rememberDeploymentKind(data.capabilities?.deployment_enforced);
          } catch {
            /* not the status payload — treat as a plain reachable answer */
          }
        }
      } catch (err) {
        // AbortError = our own timeout fired: connected but unanswered. Any
        // other throw is a connection-level failure.
        timedOut = err instanceof DOMException && err.name === "AbortError";
      } finally {
        if (to) clearTimeout(to);
        probingRef.current = false;
      }
      if (!aliveRef.current) return;
      if (ok) {
        unreachableRef.current = 0;
        timeoutRef.current = 0;
        bootingRef.current = stillBooting;
        // It answered, so it is online — whether or not it is still working
        // through the agent roster. `booting` rides alongside for the callers
        // that want to say so without blocking anything.
        setHealth("online");
        setBooting(stillBooting);
        setProgress(bootProgress);
      } else {
        if (timedOut) timeoutRef.current += 1;
        else unreachableRef.current += 1;
        // Live shared stream ⇒ server is reachable ⇒ never report trouble.
        if (getStreamState() !== "open") {
          const dead =
            !bootingRef.current &&
            (unreachableRef.current >= UNREACHABLE_TO_OFFLINE ||
              timeoutRef.current >= TIMEOUT_TO_OFFLINE);
          if (dead) setHealth("offline");
          else if (timedOut || bootingRef.current) setHealth("starting");
          // else: a first unreachable blip — keep the prior state.
        }
      }
      timer = setTimeout(probe, intervalMs);
    }
    function probeNow() {
      clearTimeout(timer);
      void probe();
    }
    probe();
    window.addEventListener("online", probeNow);
    window.addEventListener("focus", probeNow);
    document.addEventListener("visibilitychange", probeNow);
    return () => {
      aliveRef.current = false;
      clearTimeout(timer);
      window.removeEventListener("online", probeNow);
      window.removeEventListener("focus", probeNow);
      document.removeEventListener("visibilitychange", probeNow);
    };
  }, [intervalMs]);
  return { health, booting, progress };
}
