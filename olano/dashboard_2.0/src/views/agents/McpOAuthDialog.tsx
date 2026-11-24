// Interactive OAuth (authorization-code + PKCE) for a remote MCP server, driven
// entirely from the setup panel. Mirrors CodexConnectDialog's start→poll shape:
// POST /mcp/oauth/start kicks off a live connection on the runtime; we poll
// /mcp/oauth/poll/{id} until an authorization URL appears, the user opens it and
// approves, the browser redirect lands on /api/mcp/oauth/callback (same origin,
// proxied to the runtime), and the flow completes with stored tokens.

import { useEffect, useRef, useState } from "react";
import { ExternalLink } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useDialogA11y } from "../../ui/dialogA11y";
import { STR } from "./strings";
import type { McpOAuthSession, McpServer } from "./types";

const TERMINAL = new Set(["authorized", "error", "expired"]);

function dotColor(status: string): string {
  if (status === "authorized") return "var(--success)";
  if (status === "error" || status === "expired") return "var(--danger)";
  return "var(--warn)";
}

export function McpOAuthDialog({
  agentName,
  server,
  onClose,
  onAuthorized,
}: {
  agentName: string;
  server: McpServer;
  onClose: () => void;
  onAuthorized: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [session, setSession] = useState<McpOAuthSession | null>(null);
  const [status, setStatus] = useState<string>("starting");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // `server` is computed inline by the parent (a new object every render) and
  // `onAuthorized` re-renders the parent. Reading them through refs keeps the
  // start effect's deps to just `agentName` (stable), so the flow starts once
  // per mount and never restarts on a parent re-render (which would re-connect
  // with the freshly-stored tokens and spam "Authorized"). The `cancelled` flag
  // is LOCAL to each effect run — under React StrictMode (dev) the effect runs
  // twice; a per-run flag lets the second run poll cleanly instead of inheriting
  // a stale "cancelled" from the first run's cleanup.
  const serverRef = useRef(server);
  serverRef.current = server;
  const onAuthorizedRef = useRef(onAuthorized);
  onAuthorizedRef.current = onAuthorized;

  const [clientId, setClientId] = useState<string>((server.oauth_client_id as string) || "");
  const [clientSecret, setClientSecret] = useState<string>((server.oauth_client_secret as string) || "");
  const [restartKey, setRestartKey] = useState(0);
  const clientIdRef = useRef(clientId);
  clientIdRef.current = clientId;
  const clientSecretRef = useRef(clientSecret);
  clientSecretRef.current = clientSecret;
  // The redirect URI the user must register with the provider (HubSpot, etc.).
  // The runtime resolves it with relay→origin precedence, so a relay-backed
  // deployment surfaces the shared https callback instead of this localhost
  // origin (which a provider would reject / can't reach from the sign-in device).
  const [redirectUri, setRedirectUri] = useState(`${window.location.origin}/api/mcp/oauth/callback`);
  useEffect(() => {
    let cancelled = false;
    api
      .get<{ redirect_uri: string }>(
        `/api/mcp/oauth/redirect-uri?redirect_base=${encodeURIComponent(window.location.origin)}`,
      )
      .then((r) => {
        if (!cancelled && r?.redirect_uri) setRedirectUri(r.redirect_uri);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Shared dialog behavior (Escape via the stack, focus trap/restore) — a raw
  // window listener here would fire alongside the stack's topmost dialog and
  // close two dialogs on one Escape.
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogA11y(panelRef, onClose);

  useEffect(() => {
    let cancelled = false;

    const poll = async (sessionId: string) => {
      while (!cancelled) {
        await new Promise((r) => window.setTimeout(r, 1500));
        if (cancelled) return;
        let result: McpOAuthSession;
        try {
          result = await api.get<McpOAuthSession>(
            `/api/agents/${encodeURIComponent(agentName)}/mcp/oauth/poll/${sessionId}`,
          );
        } catch (e) {
          if (cancelled) return;
          // 404 = the session is gone (runtime restarted mid-flow). Surface it
          // instead of polling a dead id forever.
          if (e instanceof ApiError && e.status === 404) {
            setStatus("error");
            setErrorMsg(t("mcpoauth.sessionLost"));
            return;
          }
          continue;
        }
        if (cancelled) return;
        setSession(result);
        setStatus(result.status);
        if (result.status === "authorized") {
          toast.push(result.message || t("mcpoauth.authorized"));
          onAuthorizedRef.current();
          return;
        }
        if (result.status === "error" || result.status === "expired") {
          setErrorMsg(result.error || t("mcpoauth.failed"));
          return;
        }
      }
    };

    const start = async () => {
      try {
        const body = {
          ...serverRef.current,
          auth: "oauth",
          oauth_client_id: clientIdRef.current || undefined,
          oauth_client_secret: clientSecretRef.current || undefined,
          redirect_base: window.location.origin,
        };
        const s = await api.post<McpOAuthSession>(
          `/api/agents/${encodeURIComponent(agentName)}/mcp/oauth/start`,
          body,
        );
        if (cancelled) return;
        setSession(s);
        setStatus(s.status);
        if (s.status === "error") {
          setErrorMsg(s.error || t("mcpoauth.startFailed"));
          return;
        }
        void poll(s.session_id);
      } catch (e) {
        if (cancelled) return;
        setStatus("error");
        setErrorMsg(e instanceof Error ? e.message : t("mcpoauth.startFailed"));
      }
    };
    void start();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, restartKey]);

  const authUrl = session?.authorization_url || null;
  const logs = session?.logs || [];
  const statusLabel =
    status === "starting"
      ? t("mcpoauth.starting")
      : status === "awaiting_authorization"
        ? t("mcpoauth.awaiting")
        : status === "exchanging"
          ? t("mcpoauth.exchanging")
          : status === "authorized"
            ? t("mcpoauth.authorized")
            : status === "expired"
              ? t("mcpoauth.expired")
              : status === "error"
                ? t("mcpoauth.error")
                : status;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="card modal-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 520,
          maxWidth: "100%",
          padding: 22,
          overflow: "auto",
          overscrollBehavior: "contain",
        }}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 16 }}>{t("mcpoauth.title")}</h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 13, wordBreak: "break-all" }}>
              {server.url}
            </p>
          </div>
          <button className="btn" onClick={onClose}>
            {t("btn.cancel")}
          </button>
        </div>

        <div className="row" style={{ marginTop: 18, gap: 8 }}>
          <span
            style={{ width: 10, height: 10, borderRadius: "50%", background: dotColor(status), flex: "0 0 auto" }}
          />
          <span style={{ fontSize: 13 }}>{statusLabel}</span>
        </div>

        <div
          style={{
            marginTop: 14,
            padding: 16,
            borderRadius: "var(--r)",
            background: status === "authorized" ? "var(--success-soft)" : "var(--code-bg)",
            border: `1px solid ${status === "authorized" ? "var(--success)" : "var(--code-border)"}`,
            textAlign: "center",
          }}
        >
          {status === "authorized" ? (
            <span style={{ color: "var(--success)", fontSize: 14, fontWeight: 600 }}>
              {t("mcpoauth.authorizedBig", { count: String(session?.tool_count ?? 0) })}
            </span>
          ) : status === "error" && session?.error_code === "dcr_unsupported" ? (
            <div style={{ textAlign: "left" }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>{t("mcpoauth.needsClientTitle")}</div>
              <div className="muted" style={{ fontSize: 13, marginBottom: 10 }}>{t("mcpoauth.needsClientBody")}</div>
              <label className="muted" style={{ fontSize: 12 }}>{t("mcpmodal.clientId")}</label>
              <input
                className="input"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                style={{ fontFamily: "var(--font-mono)", fontSize: 13, marginBottom: 8 }}
              />
              <label className="muted" style={{ fontSize: 12 }}>{t("mcpmodal.clientSecret")}</label>
              <input
                className="input"
                type="password"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                style={{ fontFamily: "var(--font-mono)", fontSize: 13, marginBottom: 8 }}
              />
              <div className="muted" style={{ fontSize: 12, margin: "8px 0" }}>
                {t("mcpmodal.redirectHint")} <code>{redirectUri}</code>
              </div>
              <button
                className="btn primary"
                disabled={!clientId.trim()}
                onClick={() => {
                  setStatus("starting");
                  setErrorMsg(null);
                  setRestartKey((k) => k + 1);
                }}
              >
                {t("mcpoauth.retry")}
              </button>
            </div>
          ) : status === "error" || status === "expired" ? (
            <span style={{ color: "var(--danger)", fontSize: 13 }}>{errorMsg}</span>
          ) : authUrl ? (
            <span className="muted" style={{ fontSize: 13 }}>{t("mcpoauth.clickAuthorize")}</span>
          ) : (
            <span className="muted" style={{ fontSize: 13 }}>{t("mcpoauth.preparing")}</span>
          )}
        </div>

        {authUrl && !TERMINAL.has(status) ? (
          <a
            className="btn primary"
            href={authUrl}
            target="_blank"
            rel="noreferrer"
            style={{ marginTop: 14, width: "100%", justifyContent: "center", gap: 6, textDecoration: "none" }}
          >
            <ExternalLink size={15} strokeWidth={1.75} />
            {t("mcpoauth.openPage")}
          </a>
        ) : null}

        {!TERMINAL.has(status) ? (
          <ol style={{ marginTop: 16, paddingLeft: 20, fontSize: 13, color: "var(--text-2)" }}>
            <li>{t("mcpoauth.step1")}</li>
            <li>{t("mcpoauth.step2")}</li>
            <li>{t("mcpoauth.step3")}</li>
          </ol>
        ) : null}

        {/* Live flow log — always visible: this is the "full logging" surface. */}
        <div style={{ marginTop: 16 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            {t("mcpoauth.logTitle")}
          </div>
          <pre
            style={{
              margin: 0,
              padding: 10,
              borderRadius: "var(--r-sm)",
              background: "var(--code-bg)",
              border: "1px solid var(--code-border)",
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              color: "var(--text-3)",
              maxHeight: 180,
              overflow: "auto",
              whiteSpace: "pre-wrap",
            }}
          >
            {logs.length
              ? logs
                  .map((l) => `${(l.level || "info").toUpperCase().padEnd(5)} ${l.message}`)
                  .join("\n")
              : t("mcpoauth.noLog")}
          </pre>
        </div>

        {status === "authorized" ? (
          <button className="btn primary" style={{ marginTop: 16, width: "100%" }} onClick={onClose}>
            {t("btn.done")}
          </button>
        ) : null}
      </div>
    </div>
  );
}
