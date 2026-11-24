import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { chatPath } from "../../core/routeUrls";
import { api, ApiError } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { AutoApprovals } from "./AutoApprovals";
import { DebugTraceCard } from "./DebugTraceCard";
import { HumanCheckSetting } from "./HumanCheckSetting";
import { STR } from "./strings";

// The agent's Browser tab: what its browser is doing, its saved sign-ins and
// the actions it may take without asking. Watching and taking control happen in
// the chat's browser window (ChatBrowserDock), where the agent asks for help;
// this tab only offers Hand back, for a person who left the chat holding control.

interface Availability {
  requested: string;
  mode: string;
  provider: string;
  reason: string;
  enabled: boolean;
}

interface SessionSnap {
  agent: string;
  kind: string;
  controller: string;
  attention: string;
  url: string;
  viewers: number;
}

interface AgentBrowser {
  availability: Availability;
  session: SessionSnap | null;
  live_view: boolean;
  signed_in_state: boolean;
}

export function BrowserTab({ agentName }: { agentName: string }) {
  const t = useT(STR);
  const toast = useToast();
  const { canAdminAgent, user } = useAuth();
  const [data, setData] = useState<AgentBrowser | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .get<AgentBrowser>(`/api/agents/${encodeURIComponent(agentName)}/browser`)
      .then(setData)
      .catch((err) =>
        toast.push(err instanceof ApiError ? err.detail : String(err), "error"),
      );
  }, [agentName, toast]);

  useEffect(load, [load]);
  useEffect(
    () =>
      subscribeStream((frame) => {
        if (frame.type !== "browser_session") return;
        const ev = frame.data as { agent?: string } | undefined;
        if (ev?.agent === agentName) load();
      }),
    [agentName, load],
  );

  async function post(path: string, body?: unknown, okMsg?: string) {
    setBusy(true);
    try {
      await api.post(
        `/api/agents/${encodeURIComponent(agentName)}/browser/${path}`,
        body,
      );
      if (okMsg) toast.push(okMsg);
      load();
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : String(err), "error");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="card" style={{ padding: 18 }} />;
  const avail = data.availability;
  const sess = data.session;

  return (
    <>
      <div className="card" style={{ padding: 18 }} data-testid="browser-tab">
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 13 }}>
          {t("br.tab.intro")}
        </p>

        {!avail.enabled ? (
          <p style={{ margin: 0 }} data-testid="browser-off">
            {t("br.tab.off")} {user?.is_admin ? t("br.tab.offAdmin") : ""}
            {avail.reason ? (
              <span
                className="muted"
                style={{ display: "block", fontSize: 12.5, marginTop: 4 }}
              >
                {t("br.tab.reason", { reason: avail.reason })}
              </span>
            ) : null}
          </p>
        ) : !sess ? (
          <p style={{ margin: 0 }}>{t("br.tab.noSession")}</p>
        ) : (
          <>
            {sess.attention && (
              <div
                className="card"
                role="status"
                style={{
                  padding: 12,
                  marginBottom: 12,
                  borderColor: "var(--warn, var(--accent))",
                }}
              >
                {t("br.tab.attention", { reason: sess.attention })}
              </div>
            )}
            <dl
              className="kv"
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "4px 12px",
                margin: 0,
              }}
            >
              <dt className="muted">{t("br.tab.kind")}</dt>
              <dd style={{ margin: 0 }}>
                {sess.kind === "remote"
                  ? t("br.provider.remote")
                  : t(`br.effective.${sess.kind}`)}
              </dd>
              <dt className="muted">{t("br.tab.url")}</dt>
              <dd style={{ margin: 0, overflowWrap: "anywhere" }}>
                {sess.url || "about:blank"}
              </dd>
              <dt className="muted">{t("br.tab.controller")}</dt>
              <dd style={{ margin: 0 }}>
                {sess.controller === "user"
                  ? t("br.tab.controller.user")
                  : t("br.tab.controller.agent")}
              </dd>
            </dl>
            <p className="muted" style={{ fontSize: 12.5, margin: "12px 0 0" }}>
              {data.live_view
                ? t("br.tab.liveInChat")
                : t("br.tab.headlessNote")}
            </p>
            <div
              className="row"
              style={{ gap: 8, marginTop: 12, flexWrap: "wrap" }}
            >
              {data.live_view && (
                <Link
                  className="btn primary"
                  data-testid="browser-open-chat"
                  to={chatPath(agentName)}
                >
                  {t("br.tab.openChat")}
                </Link>
              )}
              {data.live_view && sess.controller === "user" && (
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() => post("control", { who: "agent" })}
                >
                  {t("br.tab.handBack")}
                </button>
              )}
              <button
                className="btn"
                disabled={busy}
                onClick={() => post("close", undefined, t("br.tab.closed"))}
              >
                {t("br.tab.close")}
              </button>
            </div>
          </>
        )}

        <div
          className="row"
          style={{
            gap: 10,
            marginTop: 14,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <span className="muted" style={{ fontSize: 12.5 }}>
            {t("br.tab.saved", {
              state: data.signed_in_state
                ? t("br.tab.saved.yes")
                : t("br.tab.saved.no"),
            })}
          </span>
          {data.signed_in_state && canAdminAgent(agentName) && (
            <button
              className="btn ghost sm"
              disabled={busy}
              onClick={() => {
                if (window.confirm(t("br.tab.forget.confirm")))
                  void post("forget", undefined, t("br.tab.forgotten"));
              }}
            >
              {t("br.tab.forget")}
            </button>
          )}
        </div>
      </div>
      <HumanCheckSetting agentName={agentName} />
      {canAdminAgent(agentName) && <DebugTraceCard agentName={agentName} browserOpen={!!sess} />}
      <AutoApprovals agentName={agentName} />
    </>
  );
}
