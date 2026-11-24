import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { api } from "../../../core/api";
import { useT } from "../../../core/i18n";
import { STR } from "../strings";
import { fmtInt, fmtUsd, prettyModel } from "../helpers";
import type { TcRaw } from "./drilldownTypes";

// Right-side drawer showing the *raw* prompt context + response for one LLM call
// — the "ultimate raw data". Loads on open; gated server-side by thread visibility.

const ROLE_COLOR: Record<string, string> = {
  system: "var(--text-3)",
  user: "var(--accent)",
  assistant: "var(--success)",
  tool: "var(--info, #60a5fa)",
};

export function RawCallInspector({
  threadId,
  ns,
  seq,
  onClose,
}: {
  threadId: string;
  ns: string;
  seq: number;
  onClose: () => void;
}) {
  const t = useT(STR);
  const [data, setData] = useState<TcRaw | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    const q = `ns=${encodeURIComponent(ns)}`;
    api
      .get<TcRaw>(`/api/analytics/threadcost/threads/${encodeURIComponent(threadId)}/calls/${seq}/raw?${q}`)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "error"));
  }, [threadId, ns, seq]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="lc-raw-overlay" onClick={onClose}>
      <div className="lc-raw-drawer card" onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
          <strong style={{ fontSize: 14 }}>{t("drill.rawCall", { seq: String(seq) })}</strong>
          <button className="btn ghost" onClick={onClose} title={t("drill.closeEsc")} style={{ padding: 4 }}>
            <X size={16} />
          </button>
        </div>

        {error ? (
          <p className="muted">{error}</p>
        ) : !data ? (
          <div className="spinner" />
        ) : !data.found ? (
          <p className="muted">{t("drill.callUnavailable")}</p>
        ) : (
          <>
            <div className="row" style={{ gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
              {data.model && <span className="lc-chip">{prettyModel(data.model)}</span>}
              {data.usd != null && <span className="tabular" style={{ fontSize: 12 }}>{fmtUsd(data.usd)}</span>}
            </div>

            <div className="muted" style={{ fontSize: 11.5, marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
              {t("drill.promptContext")}
              {data.omitted_context ? t("drill.omittedMessages", { n: fmtInt(data.omitted_context) }) : ""}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {(data.request_messages ?? []).map((m, i) => (
                <div key={i} className="lc-raw-msg">
                  <div className="row" style={{ gap: 6, marginBottom: 3 }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: ROLE_COLOR[m.role] ?? "var(--text-2)" }}>
                      {m.role}
                      {m.name ? `:${m.name}` : ""}
                    </span>
                    {m.truncated && (
                      <span className="muted" style={{ fontSize: 10 }}>
                        {t("drill.truncatedChars", { n: fmtInt(m.full_len) })}
                      </span>
                    )}
                  </div>
                  <pre className="lc-raw-pre">{m.content || t("drill.emptyContent")}</pre>
                </div>
              ))}
            </div>

            <div className="muted" style={{ fontSize: 11.5, margin: "14px 0 6px", textTransform: "uppercase", letterSpacing: 0.4 }}>
              {t("drill.responseHeading")}
            </div>
            <div className="lc-raw-msg">
              <pre className="lc-raw-pre">{data.response?.content || t("drill.noTextToolOnly")}</pre>
              {(data.response?.tool_calls ?? []).map((tc, i) => (
                <div key={i} style={{ marginTop: 6 }}>
                  <span className="lc-chip" style={{ fontSize: 11 }}>{tc.name}</span>
                  <pre className="lc-raw-pre" style={{ marginTop: 4 }}>{tc.args}</pre>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
