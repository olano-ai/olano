import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";
import type { CodexSession, Provider } from "./types";

// Reproduces openCodexConnect() + startCodexDeviceAuth() + pollCodexDeviceAuth()
// from the original dashboard: a modal that runs OpenAI's device-code flow,
// shows the user code + verification URL, polls until approved, and lists the
// models that become available.
export function CodexConnectDialog({
  provider,
  onClose,
  onConnected,
}: {
  provider: Provider;
  onClose: () => void;
  onConnected: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [session, setSession] = useState<CodexSession | null>(null);
  const [phase, setPhase] = useState<"starting" | "pending" | "approved" | "error" | "expired">(
    "starting",
  );
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [showLog, setShowLog] = useState(false);
  const [copied, setCopied] = useState(false);
  // Set once the flow reaches a terminal state (approved / expired / error).
  // A settled dialog must never request a second device code — see the
  // "exactly once per mount" note on the flow effect below.
  const settled = useRef(false);
  // The in-flight POST /start, shared across effect runs within one mount.
  const pendingStart = useRef<Promise<CodexSession> | null>(null);

  const addLog = useCallback((line: string) => {
    setLog((prev) => [...prev, `${new Date().toLocaleTimeString()}  ${line}`]);
  }, []);

  // Callers pass inline callbacks, so `onConnected` (and `t`/`toast`) get a
  // fresh identity on every parent render. Read them through a ref so the flow
  // effect below can keep an empty-ish dep array and still call the current
  // versions. Declared before that effect so it is populated first on mount.
  const latest = useRef({ onConnected, t, toast });
  useEffect(() => {
    latest.current = { onConnected, t, toast };
  });

  // Close on Escape — matches the modal dismiss behaviour of the original.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Runs exactly once per mount. Every POST /start mints a *new* device code
  // and a new server-side poller, so this effect must not be restartable: it
  // depends only on the provider key, and reads its callbacks from `latest`.
  // Listing `onConnected` here used to break the flow at the finish line — the
  // onboarding wizard refreshes the provider catalog from that callback, so
  // approval re-rendered the parent, gave `onConnected` a new identity, tore
  // this effect down and started a second device flow. The modal fell back to
  // "waiting for approval" with a fresh code even though the tokens were
  // already saved and the provider behind the modal read as connected.
  useEffect(() => {
    if (settled.current) return;
    // Per-run flag, not a shared ref: under StrictMode's mount/unmount/mount
    // the second run would otherwise un-cancel the first, leaving two live
    // flows racing to render their own code.
    let cancelled = false;

    const runPoll = async (sessionId: string, intervalSec: number) => {
      const intervalMs = Math.max(1, intervalSec) * 1000;
      while (!cancelled) {
        await new Promise((r) => window.setTimeout(r, intervalMs));
        if (cancelled) return;
        let result: CodexSession;
        try {
          result = await api.get<CodexSession>(`/api/auth/openai-codex/poll/${sessionId}`);
        } catch {
          if (cancelled) return;
          addLog(latest.current.t("codex.log.pollFailed"));
          continue;
        }
        if (cancelled) return;
        addLog(latest.current.t("codex.log.pollStatus", { status: result.status }));
        if (result.status === "approved") {
          settled.current = true;
          setSession(result);
          setPhase("approved");
          latest.current.toast.push(latest.current.t("codex.connectedToast"));
          latest.current.onConnected();
          return;
        }
        if (result.status === "error" || result.status === "expired") {
          settled.current = true;
          setPhase(result.status);
          setErrorMsg(
            result.error_message ??
              (result.status === "expired"
                ? latest.current.t("codex.err.expired")
                : latest.current.t("codex.err.failed")),
          );
          return;
        }
        // still pending → keep looping
      }
    };

    const start = async () => {
      try {
        // One device code per mount. StrictMode's dev-only mount/unmount/mount
        // runs this effect twice, which would burn a second code and leave the
        // first server-side poller orphaned for its full 15-minute lifetime;
        // the re-run awaits the original request instead of issuing its own.
        if (!pendingStart.current) {
          addLog(latest.current.t("codex.log.requestingCode"));
          pendingStart.current = api.post<CodexSession>("/api/auth/openai-codex/start", {});
        }
        const s = await pendingStart.current;
        if (cancelled) return;
        setSession(s);
        if (s.status === "error") {
          settled.current = true;
          setPhase("error");
          setErrorMsg(s.error_message ?? latest.current.t("codex.err.startFlow"));
          return;
        }
        setPhase("pending");
        addLog(latest.current.t("codex.log.codeIssued", { code: s.user_code }));
        void runPoll(s.session_id, s.interval);
      } catch {
        if (cancelled) return;
        settled.current = true;
        setPhase("error");
        setErrorMsg(latest.current.t("codex.err.startCodex"));
        addLog(latest.current.t("codex.log.startFailed"));
      }
    };

    void start();
    return () => {
      cancelled = true;
    };
  }, [addLog, provider.key]);

  async function copyCode() {
    if (!session?.user_code) return;
    try {
      await navigator.clipboard.writeText(session.user_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.push(t("codex.err.copyCode"), "error");
    }
  }

  const statusDot =
    phase === "approved"
      ? "var(--success)"
      : phase === "pending" || phase === "starting"
        ? "var(--warn)"
        : "var(--danger)";
  const statusText =
    phase === "starting"
      ? t("codex.status.starting")
      : phase === "pending"
        ? t("codex.status.pending")
        : phase === "approved"
          ? t("codex.status.approved")
          : phase === "expired"
            ? t("codex.status.expired")
            : t("codex.status.error");

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" onClick={onClose}>
      <div
        className="card modal-card"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 460,
          maxWidth: "100%",
          padding: 22,
          overflow: "auto",
          overscrollBehavior: "contain",
        }}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 16 }}>{t("codex.connectTitle", { provider: provider.name })}</h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
              {t("codex.subtitle")}
            </p>
          </div>
          <button className="btn" onClick={onClose}>
            {t("codex.close")}
          </button>
        </div>

        {/* Status indicator */}
        <div className="row" style={{ marginTop: 18, gap: 8 }}>
          <span
            style={{ width: 10, height: 10, borderRadius: "50%", background: statusDot, flex: "0 0 auto" }}
          />
          <span style={{ fontSize: 13 }}>{statusText}</span>
        </div>

        {/* Code display area */}
        <div
          style={{
            marginTop: 14,
            padding: 16,
            borderRadius: "var(--r)",
            background: phase === "approved" ? "var(--success-soft)" : "var(--code-bg)",
            border: `1px solid ${phase === "approved" ? "var(--success)" : "var(--code-border)"}`,
            textAlign: "center",
          }}
        >
          {phase === "starting" ? (
            <span className="muted">{t("codex.status.starting")}</span>
          ) : phase === "error" || phase === "expired" ? (
            <span style={{ color: "var(--danger)", fontSize: 13 }}>{errorMsg}</span>
          ) : phase === "approved" ? (
            <span style={{ color: "var(--success)", fontSize: 14, fontWeight: 600 }}>
              {t("codex.connectedBig")}
            </span>
          ) : session?.user_code ? (
            <>
              <div
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 26,
                  fontWeight: 700,
                  letterSpacing: 4,
                  userSelect: "all",
                  color: "var(--text)",
                }}
              >
                {session.user_code}
              </div>
              <button
                className="btn"
                style={{ marginTop: 10, fontSize: 12, padding: "4px 10px" }}
                onClick={() => void copyCode()}
              >
                {copied ? t("codex.copied") : t("codex.copyCode")}
              </button>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </div>

        {/* Prominent primary CTA to open the device page (parity with the
            original's "Open Codex device page" button). */}
        {phase !== "approved" && phase !== "error" && phase !== "expired" && session?.verification_url ? (
          <a
            className="btn primary"
            href={session.verification_url}
            target="_blank"
            rel="noreferrer"
            style={{
              marginTop: 14,
              width: "100%",
              justifyContent: "center",
              gap: 6,
              textDecoration: "none",
            }}
          >
            <ExternalLink size={15} strokeWidth={1.75} />
            {t("codex.openDevicePage")}
          </a>
        ) : null}

        {/* Instructions */}
        {phase !== "approved" && phase !== "error" && phase !== "expired" ? (
          <ol style={{ marginTop: 16, paddingLeft: 20, fontSize: 13, color: "var(--text-2)" }}>
            <li>{t("codex.step1")}</li>
            <li>
              {t("codex.step2.before")}
              <code style={{ fontFamily: "var(--font-mono)" }}>{session?.user_code ?? "…"}</code>
              {t("codex.step2.after")}
            </li>
            <li>{t("codex.step3")}</li>
          </ol>
        ) : null}

        {/* Approved: show models */}
        {phase === "approved" && session?.models?.length ? (
          <div style={{ marginTop: 16 }}>
            <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
              {t("codex.availableModels", { count: String(session.models.length) })}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {session.models.map((m) => (
                <span
                  key={m}
                  className="badge"
                  style={{ fontFamily: "var(--font-mono)", fontWeight: 500 }}
                >
                  {m}
                </span>
              ))}
            </div>
          </div>
        ) : null}

        {/* Collapsible technical log */}
        <div style={{ marginTop: 18 }}>
          <button
            className="btn"
            style={{ fontSize: 12, padding: "4px 10px" }}
            onClick={() => setShowLog((v) => !v)}
          >
            {showLog ? t("codex.hideLog") : t("codex.showLog")}
          </button>
          {showLog ? (
            <pre
              style={{
                marginTop: 8,
                padding: 10,
                borderRadius: "var(--r-sm)",
                background: "var(--code-bg)",
                border: "1px solid var(--code-border)",
                fontFamily: "var(--font-mono)",
                fontSize: 11,
                color: "var(--text-3)",
                maxHeight: 160,
                overflow: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {log.length ? log.join("\n") : t("codex.noLog")}
            </pre>
          ) : null}
        </div>

        {phase === "approved" ? (
          <button className="btn primary" style={{ marginTop: 16 }} onClick={onClose}>
            {t("codex.done")}
          </button>
        ) : null}
      </div>
    </div>
  );
}
