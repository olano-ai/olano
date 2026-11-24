import { useEffect, useState, useSyncExternalStore } from "react";
import { getStreamState, subscribeStreamState } from "../agentStream";
import { useT } from "../i18n";
import { STR } from "./strings";

// Slim, non-blocking indicator for the window where the shared /api/stream
// EventSource is re-establishing — e.g. right after a phone unlock. The blur
// ConnectivityOverlay covers real outages (health-poll driven, 4s grace); this
// pill is stream-driven and can be more eager (2s) precisely because it blocks
// nothing. The 2s latch keeps it invisible through the sub-second reconnects
// the multiplexed stream was built to absorb (see agentStream.ts).
const SHOW_AFTER_MS = 2000;

export function StreamStatusPill({ suppressed }: { suppressed: boolean }) {
  const state = useSyncExternalStore(subscribeStreamState, getStreamState);
  const [show, setShow] = useState(false);
  const t = useT(STR);

  useEffect(() => {
    if (state !== "connecting") {
      setShow(false);
      return;
    }
    const id = window.setTimeout(() => setShow(true), SHOW_AFTER_MS);
    return () => window.clearTimeout(id);
  }, [state]);

  if (!show || suppressed) return null;
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
        animation: "pill-fade var(--t) var(--ease)",
      }}
    >
      <span className="spinner" style={{ width: 12, height: 12 }} />
      {t("pill.reconnecting")}
      <style>{`@keyframes pill-fade { from { opacity: 0; transform: translate(-50%, -6px) } to { opacity: 1; transform: translate(-50%, 0) } }`}</style>
    </div>
  );
}
