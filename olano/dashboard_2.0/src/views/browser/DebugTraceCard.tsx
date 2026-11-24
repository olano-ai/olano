import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

// "Debug trace": what this agent's browser and a site said to each other
// around a "Verify you are human" check (olano/browser/debug_trace.py). It
// starts by itself when the browser waits for a person on a check, or by hand
// here; the server reads it and names the likely cause, and the whole record
// downloads as JSON to send to whoever is debugging. Agent admins only: it
// lists the addresses the browser visited (never cookie values or query values).

interface Finding {
  code: string;
  text: string;
}

interface Trace {
  reason: string;
  started_at: number;
  stopped_at: number | null;
  events: unknown[];
  analysis: { findings: Finding[]; exit_addresses: string[] };
}

export function DebugTraceCard({ agentName, browserOpen }: { agentName: string; browserOpen: boolean }) {
  const t = useT(STR);
  const toast = useToast();
  const [trace, setTrace] = useState<Trace | null>(null);
  const [busy, setBusy] = useState(false);
  const url = `/api/agents/${encodeURIComponent(agentName)}/browser/debug-trace`;

  const load = useCallback(() => {
    api
      .get<Trace>(url)
      .then(setTrace)
      .catch(() => setTrace(null));
  }, [url]);

  useEffect(load, [load]);

  async function act(action: "start" | "stop") {
    setBusy(true);
    try {
      setTrace(await api.post<Trace>(url, { action }));
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    try {
      const fresh = await api.get<Trace>(url);
      setTrace(fresh);
      const blob = new Blob([JSON.stringify(fresh, null, 1)], { type: "application/json" });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = `browser-trace-${agentName}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(href);
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    }
  }

  const recording = !!trace && trace.stopped_at === null;
  const findings = trace?.analysis?.findings ?? [];
  return (
    <section className="card browser-aa" data-testid="browser-debug-trace">
      <h3 className="browser-aa-title">{t("br.trace.title")}</h3>
      <p className="muted browser-aa-help">{t("br.trace.hint")}</p>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 }}>
        {recording ? (
          <button className="btn sm" disabled={busy} onClick={() => void act("stop")} data-testid="trace-stop">
            {t("br.trace.stop")}
          </button>
        ) : (
          <button
            className="btn sm"
            disabled={busy || !browserOpen}
            title={browserOpen ? undefined : t("br.trace.needsBrowser")}
            onClick={() => void act("start")}
            data-testid="trace-start"
          >
            {t("br.trace.start")}
          </button>
        )}
        {trace && (
          <button className="btn ghost sm" onClick={() => void download()} data-testid="trace-download">
            {t("br.trace.download")}
          </button>
        )}
        {trace && (
          <span className="muted" style={{ fontSize: 12.5 }}>
            {t(recording ? "br.trace.recording" : "br.trace.recorded", {
              count: String(trace.events.length),
              when: new Date(trace.started_at * 1000).toLocaleTimeString(),
            })}
          </span>
        )}
      </div>
      {findings.length > 0 && (
        <ul className="browser-aa-readonly" data-testid="trace-findings" style={{ marginTop: 10 }}>
          {findings.map((f) => (
            <li key={f.code}>{f.text}</li>
          ))}
        </ul>
      )}
    </section>
  );
}
