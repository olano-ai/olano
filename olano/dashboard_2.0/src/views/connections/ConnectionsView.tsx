import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Cable, Check, Copy, ExternalLink, Search } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { parseRoute, viewPath } from "../../core/routeUrls";
import { usePersistentState } from "../../core/usePersistentState";
import { subscribeStream } from "../../core/agentStream";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import type { Agent } from "../../core/types";
import { CONN_SERVICES, scopeLabel } from "./types";
import type {
  AuthStatus,
  AuthStatusResponse,
  ConnectionService,
  DeviceSessionInfo,
  OAuthSessionInfo,
  Scope,
  VaultSecret,
} from "./types";
import { SetupGuideModal } from "./SetupGuideModal";
import { STR } from "./strings";

type T = (key: string, vars?: Record<string, string>) => string;

function errDetail(err: unknown, t: T): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return t("err.unknown");
}

// SSE frame off /api/stream. We react to agent-lifecycle events (keeps the
// per-agent picker fresh) and to connectors-catalog syncs (can flip cards to
// the official one-click panel).
interface StreamEvent {
  type?: string;
  catalog?: string;
}
const AGENT_LIFECYCLE_EVENTS = new Set(["agent_installed", "agent_deleted", "agent_started", "agent_stopped"]);

export function ConnectionsView() {
  const toast = useToast();
  const t = useT(STR);
  const confirm = useConfirm();

  // Which connections are showing is the address: /connections is the
  // system-wide scope, /connections/<agent> that agent's own (core/routeUrls.ts),
  // so a reload, a copied link and Back/Forward all land on the same scope, and
  // links from elsewhere (an agent's Gmail watch, a bell notification) open the
  // agent they name. The last choice is still remembered for this session, so
  // the sidebar's bare /connections returns to it, as before.
  const location = useLocation();
  const navigate = useNavigate();
  const urlAgentName = parseRoute(location.pathname).agent;
  const [rememberedScope, setRememberedScope] = usePersistentState<Scope>("connections.scope", "system");
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [rememberedAgent, setRememberedAgent] = usePersistentState<string>("connections.selectedAgent", "");
  // "Per agent" with no agent to name (none exist yet) cannot be an address;
  // it is the one case that stays local.
  const noAgentToName = agents !== null && agents.length === 0;
  const scope: Scope = urlAgentName ? "agent" : rememberedScope === "agent" && noAgentToName ? "agent" : "system";
  const selectedAgent = urlAgentName ?? rememberedAgent;
  const arrivedRef = useRef(false);
  useEffect(() => {
    if (arrivedRef.current) return;
    arrivedRef.current = true;
    if (!urlAgentName && rememberedScope === "agent" && rememberedAgent) {
      navigate(viewPath("connections", { agent: rememberedAgent }), { replace: true });
    }
    // Arrival only: after this the URL alone decides.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Remember what the URL shows, so a later bare visit returns to it.
  useEffect(() => {
    if (!arrivedRef.current) return;
    if (urlAgentName) {
      setRememberedScope("agent");
      setRememberedAgent(urlAgentName);
    } else if (!noAgentToName) {
      setRememberedScope("system");
    }
  }, [urlAgentName, noAgentToName, setRememberedScope, setRememberedAgent]);
  const setScope = useCallback(
    (next: Scope) => {
      if (next === scope) return;
      if (next === "system") {
        navigate("/connections");
        return;
      }
      const names = (agents ?? []).map((a) => a.name);
      const target = names.includes(rememberedAgent) ? rememberedAgent : names[0];
      if (target) navigate(viewPath("connections", { agent: target }));
      else {
        // No agent to name yet: the roster is still loading (the click lands
        // once it has, below), or there is none at all.
        pendingAgentScopeRef.current = agents === null;
        setRememberedScope("agent");
      }
    },
    [scope, agents, rememberedAgent, navigate, setRememberedScope],
  );
  // "Per agent" clicked before the roster answered: finish the move now.
  const pendingAgentScopeRef = useRef(false);
  useEffect(() => {
    if (!pendingAgentScopeRef.current || agents === null) return;
    pendingAgentScopeRef.current = false;
    const names = agents.map((a) => a.name);
    const target = names.includes(rememberedAgent) ? rememberedAgent : names[0];
    if (target && !urlAgentName) navigate(viewPath("connections", { agent: target }));
  }, [agents, rememberedAgent, urlAgentName, navigate]);
  const setSelectedAgent = useCallback(
    (name: string) => {
      if (name && name !== urlAgentName) navigate(viewPath("connections", { agent: name }));
    },
    [urlAgentName, navigate],
  );
  const [status, setStatus] = useState<AuthStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Which card has a connect panel open, and which of its sign-in methods
  // (null = none). One open panel at a time across the whole grid.
  const [openPanel, setOpenPanel] = useState<{ id: string; kind: PanelKind } | null>(null);
  // Service id whose setup guide modal is open (null = none).
  const [guideFor, setGuideFor] = useState<string | null>(null);
  const [search, setSearch] = useState("");



  // ── load the agent list (used by the per-agent scope picker) ─────────────
  const loadAgents = useCallback(() => {
    api
      .cachedGet<Agent[]>("/api/agents")
      .then((list) => {
        setAgents(list);
        setRememberedAgent((cur) => cur || (list[0]?.name ?? ""));
      })
      .catch(() => {
        setAgents([]);
        toast.push(t("toast.agentsFailed"), "error");
      });
  }, [toast, t, setRememberedAgent]);

  useEffect(loadAgents, [loadAgents]);

  const agentParam = scope === "agent" && selectedAgent ? selectedAgent : null;

  // ── load connection status for the current scope ─────────────────────────
  // `quiet` refreshes in place without flipping the loading state: the
  // spinner unmounts every card (and any half-finished connect panel with
  // them), which is fine on scope changes but not for a background
  // catalog-sync nudge while someone is mid-authorization.
  const loadStatus = useCallback(
    (quiet = false) => {
      // Per-agent scope with no agent selected: nothing to fetch.
      if (scope === "agent" && !agentParam) {
        setStatus(null);
        setError(null);
        setLoading(false);
        return;
      }
      if (!quiet) {
        setLoading(true);
        setError(null);
      }
      const path = agentParam
        ? `/api/auth/status?agent=${encodeURIComponent(agentParam)}`
        : "/api/auth/status";
      api
        .get<AuthStatusResponse>(path)
        .then((s) => setStatus(s))
        .catch((err) => {
          if (quiet) return; // keep showing the last good statuses
          setStatus(null);
          setError(errDetail(err, t));
        })
        .finally(() => {
          if (!quiet) setLoading(false);
        });
    },
    [agentParam, scope, t],
  );

  useEffect(loadStatus, [loadStatus]);

  // Live refresh on stream events. Agent lifecycle keeps the per-agent picker
  // fresh (the event IS the news that the roster changed, so bust the shared
  // cache first — a cachedGet alone could be answered ≤5s-stale and miss the
  // change for good). A freshly synced connectors catalog can add official
  // Olano apps — quiet-reload statuses so cards flip to the one-click panel
  // without a manual page reload (the sync lands via the hourly poll, the
  // auth-status freshness kick, or Config's "Sync catalogs now").
  useEffect(() => {
    return subscribeStream((frame) => {
      const event = frame as StreamEvent;
      if (event.type && AGENT_LIFECYCLE_EVENTS.has(event.type)) {
        api.invalidate("/api/agents");
        loadAgents();
      }
      if (event.type === "catalog_updated" && event.catalog === "connectors") {
        loadStatus(true);
      }
    });
  }, [loadAgents, loadStatus]);

  // Switching scope/agent collapses any open connect panel.
  useEffect(() => {
    setOpenPanel(null);
  }, [scope, selectedAgent]);

  async function disconnect(svc: ConnectionService) {
    const prompt = agentParam
      ? t("confirm.disconnect.agent", { name: svc.name, agent: agentParam })
      : t("confirm.disconnect.system", { name: svc.name });
    const ok = await confirm({ message: prompt, danger: true });
    if (!ok) return;
    try {
      const path = agentParam
        ? `/api/auth/${svc.id}?agent=${encodeURIComponent(agentParam)}`
        : `/api/auth/${svc.id}`;
      await api.del(path);
      toast.push(t("toast.disconnected"));
      loadStatus();
    } catch (err) {
      toast.push(t("toast.disconnectFailed", { detail: errDetail(err, t) }), "error");
    }
  }

  // Toggle the shared flag on an already-connected per-agent token.
  async function setShared(svc: ConnectionService, shared: boolean) {
    if (!agentParam) return;
    try {
      await api.patch(`/api/auth/${svc.id}/share`, { agent: agentParam, shared });
      toast.push(shared ? t("toast.shared") : t("toast.unshared"));
      loadStatus();
    } catch (err) {
      toast.push(t("toast.shareFailed", { detail: errDetail(err, t) }), "error");
    }
  }

  const agentsLoading = agents === null;
  const noAgents = agents !== null && agents.length === 0;

  // Every service has a card in both scopes (a token is a per-scope record
  // whatever method minted it); the list is only narrowed by the search box.
  const visibleServices = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return CONN_SERVICES;
    // Match what the card actually shows — name, description, capability
    // chips — plus the id, so "x_twitter" and "microsoft365" find their cards.
    return CONN_SERVICES.filter((svc) =>
      `${svc.name} ${svc.id} ${t(`svc.${svc.id}.desc`)} ${svc.caps.join(" ")}`
        .toLowerCase()
        .includes(q),
    );
  }, [search, t]);

  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        {/* .topbar is space-between, so this sits flush right like Agents'. */}
        <div style={{ position: "relative", minWidth: 0 }}>
          <input
            className="input"
            placeholder={t("search.placeholder")}
            aria-label={t("search.placeholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 220, maxWidth: "100%", paddingLeft: 30 }}
          />
          <span
            style={{
              position: "absolute",
              left: 10,
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--text-4)",
              display: "inline-flex",
            }}
          >
            <Search size={14} strokeWidth={1.75} />
          </span>
        </div>
      </div>
      <div className="page" style={{ maxWidth: 880 }}>
        <h1>{t("title")}</h1>
        <p className="muted">
          {t("intro.pre")}
          <strong>{t("intro.system")}</strong>
          {t("intro.mid")}
          <strong>{t("intro.peragent")}</strong>
          {t("intro.post")}
        </p>

        {/* scope tabs */}
        <div className="row" style={{ marginTop: 16, flexWrap: "wrap" }}>
          <button className={scope === "system" ? "btn primary" : "btn"} onClick={() => setScope("system")}>
            {t("scope.system")}
          </button>
          <button className={scope === "agent" ? "btn primary" : "btn"} onClick={() => setScope("agent")}>
            {t("scope.agent")}
          </button>
          {scope === "agent" && (
            <label className="muted" style={{ fontSize: 12, fontWeight: 600, marginLeft: 4 }}>
              {t("scope.agentLabel")}
            </label>
          )}
          {scope === "agent" && (
            <select
              className="input"
              style={{ width: 240 }}
              value={selectedAgent}
              onChange={(e) => setSelectedAgent(e.target.value)}
              disabled={agentsLoading || noAgents}
            >
              {agentsLoading && <option value="">{t("agents.loading")}</option>}
              {noAgents && <option value="">{t("agents.none")}</option>}
              {!selectedAgent && !agentsLoading && !noAgents && <option value="">{t("agents.select")}</option>}
              {(agents ?? []).map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* body states */}
        {loading ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : error ? (
          <div className="card" style={{ marginTop: 20, padding: 18 }}>
            <div className="error-text">{t("status.loadError", { error })}</div>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn" onClick={() => loadStatus()}>
                {t("action.retry")}
              </button>
            </div>
          </div>
        ) : scope === "agent" && noAgents ? (
          <p className="muted" style={{ marginTop: 24 }}>
            {t("empty.noAgents")}
          </p>
        ) : scope === "agent" && !agentParam ? (
          <p className="muted" style={{ marginTop: 24 }}>
            {t("empty.selectAgent")}
          </p>
        ) : visibleServices.length === 0 ? (
          <div
            className="card"
            style={{ marginTop: 24, padding: "48px 24px", textAlign: "center", background: "var(--surface)" }}
          >
            <div style={{ display: "flex", justifyContent: "center", color: "var(--text-4)" }}>
              <Cable size={38} strokeWidth={1.5} />
            </div>
            <div style={{ fontWeight: 600, fontSize: 16, marginTop: 8 }}>{t("empty.noMatches")}</div>
            <p className="muted" style={{ marginTop: 4 }}>
              {t("empty.trySearch", { query: search.trim() })}
            </p>
            <div className="row" style={{ justifyContent: "center", marginTop: 16 }}>
              <button className="btn" onClick={() => setSearch("")}>
                {t("empty.clearSearch")}
              </button>
            </div>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gap: 16,
              marginTop: 20,
              gridTemplateColumns: "repeat(auto-fill,minmax(360px,1fr))",
            }}
          >
            {visibleServices.map((svc) => (
              <ServiceCard
                key={svc.id}
                svc={svc}
                status={status?.[svc.id]}
                scope={scope}
                agent={agentParam}
                panel={openPanel?.id === svc.id ? openPanel.kind : null}
                onOpen={(kind) => setOpenPanel({ id: svc.id, kind })}
                onClose={() => setOpenPanel(null)}
                onDisconnect={() => void disconnect(svc)}
                onToggleShare={(s) => void setShared(svc, s)}
                onShowGuide={() => svc.guideId && setGuideFor(svc.guideId)}
                onConnected={() => {
                  setOpenPanel(null);
                  loadStatus();
                }}
              />
            ))}
          </div>
        )}

      </div>

      {guideFor && <SetupGuideModal guideId={guideFor} onClose={() => setGuideFor(null)} />}
    </>
  );
}

// ── service card + connect panel ─────────────────────────────────────────
// Which inline panel a card has open: "oauth" = ConnectPanel (Olano's app or
// a bring-your-own OAuth app), "device" = DeviceConnectPanel (a one-time
// code). The runtime's per-service `methods` list decides which buttons a
// card offers, so a service that gains a device recipe needs no UI change.
type PanelKind = "oauth" | "device";

interface ServiceCardProps {
  svc: ConnectionService;
  status: AuthStatus | undefined;
  scope: Scope;
  agent: string | null;
  panel: PanelKind | null;
  onOpen: (kind: PanelKind) => void;
  onClose: () => void;
  onDisconnect: () => void;
  onToggleShare: (shared: boolean) => void;
  onShowGuide: () => void;
  onConnected: () => void;
}

function ServiceCard({
  svc,
  status,
  scope,
  agent,
  panel,
  onOpen,
  onClose,
  onDisconnect,
  onToggleShare,
  onShowGuide,
  onConnected,
}: ServiceCardProps) {
  const t = useT(STR);
  const connected = status?.connected ?? false;
  // A stored token the service refused: not connected, but not "never
  // connected" either, the card says so and offers a Reconnect.
  const needsReauth = !connected && Boolean(status?.needs_reauth);
  const label = status ? scopeLabel(status, t) : "";
  // A second sign-in button only for services whose provider declares a
  // device-code recipe (GitHub today). Absent `methods` = an older runtime
  // that only ever spoke OAuth.
  const hasDevice = status?.methods?.includes("device") ?? false;

  return (
    <div className="card" style={{ padding: 18 }}>
      {/* head */}
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
          <span
            style={{
              width: 38,
              height: 38,
              borderRadius: "var(--r-sm)",
              display: "grid",
              placeItems: "center",
              flexShrink: 0,
              background: "var(--surface-2)",
              color: "var(--text-2)",
            }}
          >
            <svc.icon size={20} strokeWidth={1.75} />
          </span>
          <div>
            <strong style={{ fontSize: 15 }}>{svc.name}</strong>
            <div className="muted" style={{ marginTop: 4, fontSize: 13, lineHeight: 1.5 }}>
              {t(`svc.${svc.id}.desc`)}
            </div>
          </div>
        </div>
        <div style={{ textAlign: "right", flexShrink: 0 }}>
          <span
            className="badge"
            style={
              connected
                ? { background: "var(--success-soft)", color: "var(--success)" }
                : needsReauth
                  ? { background: "var(--warn-soft)", color: "var(--warn)" }
                  : { background: "var(--surface-3)", color: "var(--text-3)" }
            }
          >
            <span
              style={{
                display: "inline-block",
                width: 7,
                height: 7,
                borderRadius: "50%",
                marginRight: 5,
                background: connected ? "var(--success)" : needsReauth ? "var(--warn)" : "var(--text-4)",
              }}
            />
            {connected ? t("badge.connected") : needsReauth ? t("badge.needsReconnect") : t("badge.notConnected")}
          </span>

          {connected && label && (
            <div style={{ fontSize: 10.5, color: "var(--text-4)", marginTop: 3 }}>{label}</div>
          )}
          {connected && status?.client_source === "official" && (
            <div style={{ fontSize: 10.5, color: "var(--text-4)", marginTop: 2 }}>
              {t("official.badge")}
            </div>
          )}
          {connected && status?.auth_mode === "device" && (
            <div style={{ fontSize: 10.5, color: "var(--text-4)", marginTop: 2 }}>
              {t("device.badge")}
            </div>
          )}
          {/* The signed-in login, for any provider that reports one. */}
          {connected && status?.account && (
            <div
              style={{ fontSize: 10.5, color: "var(--text-4)", marginTop: 2, fontFamily: "var(--font-mono)" }}
            >
              @{status.account}
            </div>
          )}
        </div>
      </div>

      {/* Why the badge reads "Needs reconnecting": the service's own words, on
          a line of its own under the head row. It is long and free-form, so
          it must not sit in the right-hand column: a fixed-width block there
          pushed the whole column off a 390px card. */}
      {needsReauth && (
        <div style={{ marginTop: 8, fontSize: 12, lineHeight: 1.5, color: "var(--warn)", wordBreak: "break-word" }}>
          {status?.auth_error ? status.auth_error : t("badge.needsReconnect.hint")}
        </div>
      )}

      {/* Capability chips — tool names, so monospace for accuracy. Once the
          service is connected these tools are live for the agent, and the chips
          take the same green the status badge above them carries; while it is
          not connected they are a preview of what connecting would grant, and
          stay neutral. */}
      <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 12 }}>
        {svc.caps.map((c) => (
          <span
            className="badge"
            key={c}
            style={{
              fontFamily: "var(--font-mono)",
              fontWeight: 500,
              fontSize: 10.5,
              ...(connected ? { background: "var(--success-soft)", color: "var(--success)" } : undefined),
            }}
          >
            {c}
          </span>
        ))}
      </div>

      {connected && status?.token_path && (
        <div style={{ marginTop: 10 }}>
          <div
            className="muted"
            style={{
              fontSize: 10,
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              marginBottom: 3,
            }}
          >
            {t("token.label")}
          </div>
          <div
            style={{
              fontSize: 11.5,
              fontFamily: "var(--font-mono)",
              color: "var(--text-3)",
              background: "var(--code-bg)",
              border: "1px solid var(--code-border)",
              borderRadius: "var(--r-xs)",
              padding: "5px 8px",
              wordBreak: "break-all",
            }}
            title={status.token_path}
          >
            {status.token_path}
          </div>
        </div>
      )}

      {/* Share toggle for an already-connected per-agent token. */}
      {connected && scope === "agent" && agent && status?.scope === "agent" && (
        <div style={{ marginTop: 10 }}>
          <label className="row" style={{ gap: 6, fontSize: 13 }}>
            <input
              type="checkbox"
              checked={status.shared ?? false}
              onChange={(e) => onToggleShare(e.target.checked)}
            />
            {t("share.toggle")}
          </label>
          <div className="muted" style={{ fontSize: 12, marginLeft: 22, marginTop: 2 }}>
            {t("share.hint")}
          </div>
        </div>
      )}

      {/* footer actions */}
      <div className="row" style={{ marginTop: 14, justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          {connected ? (
            <button className="btn danger" onClick={onDisconnect}>
              {t("action.disconnect")}
            </button>
          ) : (
            <>
              <button className="btn primary" onClick={() => onOpen("oauth")}>
                {needsReauth ? t("action.reconnect") : t("action.connect")}
              </button>
              {hasDevice && (
                <button
                  className="btn"
                  onClick={() => onOpen("device")}
                  title={t("card.signInWithCode.help", { name: svc.name })}
                >
                  {t("card.signInWithCode")}
                </button>
              )}
            </>
          )}
        </div>
        {svc.guideId && (
          <button className="btn" onClick={onShowGuide}>
            {t("action.setupGuide")}
          </button>
        )}
      </div>
      {/* What the second button is for — visible on touch screens too, where
          the title tooltip never shows. */}
      {!connected && hasDevice && (
        <div className="muted" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 8 }}>
          {t("card.signInWithCode.help", { name: svc.name })}
        </div>
      )}

      {/* Connect panels open only while disconnected. A device panel stays
          mounted after it succeeds so the confirmation (and its @account) is
          readable; the card badge refreshes behind it once "Done" is pressed. */}
      {panel === "device" && (
        <DeviceConnectPanel svc={svc} scope={scope} agent={agent} onClose={onClose} onConnected={onConnected} />
      )}
      {panel === "oauth" && !connected && (
        <ConnectPanel svc={svc} scope={scope} agent={agent} status={status} onClose={onClose} onConnected={onConnected} />
      )}
    </div>
  );
}

// ── device-code connect panel ─────────────────────────────────────────────
// Inline sub-panel for the "device" sign-in method (RFC 8628): the runtime
// asks the provider for a one-time code, this panel shows it with a link to
// the provider's device page, and the runtime polls the provider until the
// person approves there. Nothing to register — the provider's public client
// id is used — so it works from a phone or on a self-hosted box with no OAuth
// app. Generic over `svc`: any service whose status `methods` lists "device"
// gets it. Mirrors the OpenAI Codex connect dialog's phases.

// `idle` = the pre-connect step (per-agent scope only: the Share choice must
// be made before the code is minted, because it rides in the start body);
// the rest mirror DeviceSessionInfo.status on the runtime.
type DevicePhase = "idle" | DeviceSessionInfo["status"];

function DeviceConnectPanel({
  svc,
  scope,
  agent,
  onClose,
  onConnected,
}: {
  svc: ConnectionService;
  scope: Scope;
  agent: string | null;
  onClose: () => void;
  /** Reported on "Done" — closes the panel and reloads connection status. */
  onConnected: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const perAgent = scope === "agent" && !!agent;
  const [session, setSession] = useState<DeviceSessionInfo | null>(null);
  // System scope has nothing to decide, so the code is requested on open;
  // per-agent scope first offers the Share checkbox, like ConnectPanel does.
  const [started, setStarted] = useState(!perAgent);
  const [phase, setPhase] = useState<DevicePhase>(perAgent ? "idle" : "starting");
  const [shared, setShared] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // Bumped by "Try again"; re-runs the start effect from scratch.
  const [attempt, setAttempt] = useState(0);
  // Monotonic run token rather than a shared "cancelled" flag: a superseded
  // attempt (Try again, or StrictMode's double-mount in dev) must not keep
  // polling and overwrite the current run's state.
  const runRef = useRef(0);
  // Session still running server-side; cleared once it reaches a terminal
  // state so unmount doesn't cancel a sign-in that already succeeded.
  const liveSessionRef = useRef<string | null>(null);
  // Read at start time by the effect below without being one of its deps —
  // the checkbox is only editable before the code is requested anyway.
  const sharedRef = useRef(shared);
  sharedRef.current = shared;

  // Tell the runtime to stop polling the provider — otherwise dismissing the
  // panel would leave it holding a live device code for the rest of the
  // window (and single-flight would hand that stale code to the next click).
  const abortLive = useCallback(() => {
    const sessionId = liveSessionRef.current;
    if (!sessionId) return;
    liveSessionRef.current = null;
    void api.post(`/api/auth/${svc.id}/device/cancel/${sessionId}`, {}).catch(() => {});
  }, [svc.id]);

  useEffect(() => {
    if (!started) return;
    const run = ++runRef.current;
    const stale = () => runRef.current !== run;

    const poll = async (sessionId: string, intervalSec: number) => {
      const intervalMs = Math.max(1, intervalSec) * 1000;
      while (!stale()) {
        await new Promise((r) => window.setTimeout(r, intervalMs));
        if (stale()) return;
        let s: DeviceSessionInfo;
        try {
          s = await api.get<DeviceSessionInfo>(`/api/auth/${svc.id}/device/poll/${sessionId}`);
        } catch (e) {
          if (stale()) return;
          if (e instanceof ApiError && e.status === 404) {
            setPhase("error");
            setErrorMsg(t("panel.sessionLost"));
            return;
          }
          continue;
        }
        if (stale()) return;
        setSession(s);
        if (s.status === "approved") {
          liveSessionRef.current = null; // finished — nothing left to abort
          setPhase("approved");
          toast.push(t("toast.connected", { name: svc.name }));
          // Deliberately NOT reporting up yet: the parent's status reload
          // flips the view into its loading state, which unmounts this panel
          // (and would restart the flow). "Done" reports it instead, so the
          // success line — including which account signed in — is readable.
          return;
        }
        if (s.status === "error" || s.status === "expired") {
          liveSessionRef.current = null;
          setPhase(s.status);
          setErrorMsg(s.error_message ?? (s.status === "expired" ? t("device.expired") : t("device.failed")));
          return;
        }
        setPhase(s.status);
      }
    };

    const start = async () => {
      setPhase("starting");
      setErrorMsg(null);
      setSession(null);
      try {
        // Scope rides in the body: the token lands on the agent's own record
        // (optionally marked shared) or, with no agent, deployment-wide.
        const body = {
          agent: perAgent ? agent : null,
          shared: perAgent ? sharedRef.current : false,
        };
        const s = await api.post<DeviceSessionInfo>(`/api/auth/${svc.id}/device/start`, body);
        if (stale()) return;
        setSession(s);
        if (s.status === "error" || s.status === "expired") {
          setPhase(s.status);
          setErrorMsg(s.error_message ?? t("device.failed"));
          return;
        }
        liveSessionRef.current = s.session_id;
        setPhase(s.status);
        void poll(s.session_id, s.interval ?? 3);
      } catch (err) {
        if (stale()) return;
        setPhase("error");
        setErrorMsg(errDetail(err, t));
      }
    };

    void start();
    return () => {
      runRef.current++; // supersede this run's poll loop
      abortLive();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started, attempt, svc.id]);

  async function copyCode() {
    if (!session?.user_code) return;
    try {
      await navigator.clipboard.writeText(session.user_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.push(t("panel.copyFailed"), "error");
    }
  }

  const waiting = phase === "starting" || phase === "pending";
  const vars = { name: svc.name };

  return (
    <div
      style={{
        // Same inset sub-panel treatment as ConnectPanel.
        margin: "14px -18px -18px",
        padding: "14px 18px",
        borderTop: "1px solid var(--border)",
        background: "var(--surface-2)",
        borderBottomLeftRadius: "var(--r)",
        borderBottomRightRadius: "var(--r)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
        {t("device.intro", vars)}{" "}
        {perAgent ? t("device.scope.agent", { agent: agent ?? "" }) : t("device.scope.system")}
      </div>

      {/* Pre-connect step (per-agent scope): decide sharing, then ask for a code. */}
      {phase === "idle" && (
        <>
          <div>
            <label className="row" style={{ gap: 6 }}>
              <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
              {t("share.toggle")}
            </label>
            <div className="muted" style={{ fontSize: 12, marginLeft: 22, marginTop: 2 }}>
              {t("share.hint")}
            </div>
          </div>
          <div className="row">
            <button className="btn primary" onClick={() => setStarted(true)}>
              {t("device.getCode", vars)}
            </button>
            <button className="btn" onClick={onClose}>
              {t("action.cancel")}
            </button>
          </div>
        </>
      )}

      {/* One-time code display */}
      {phase !== "idle" && (
        <div
          style={{
            padding: 14,
            borderRadius: "var(--r-sm)",
            background: phase === "approved" ? "var(--success-soft)" : "var(--code-bg)",
            border: `1px solid ${phase === "approved" ? "var(--success)" : "var(--code-border)"}`,
            textAlign: "center",
          }}
        >
          {phase === "starting" ? (
            <span className="muted" style={{ fontSize: 13 }}>
              {t("device.requesting", vars)}
            </span>
          ) : phase === "approved" ? (
            <span style={{ color: "var(--success)", fontSize: 14, fontWeight: 600 }}>
              {t("panel.connected")}
              {session?.account ? ` (@${session.account})` : ""}
            </span>
          ) : phase === "error" || phase === "expired" ? (
            <span style={{ color: "var(--danger)", fontSize: 13, wordBreak: "break-word" }}>{errorMsg}</span>
          ) : session?.user_code ? (
            <>
              <div
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 24,
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
                style={{ marginTop: 8, fontSize: 12, padding: "4px 10px" }}
                onClick={() => void copyCode()}
              >
                {copied ? t("device.copied") : t("device.copyCode")}
              </button>
            </>
          ) : (
            <span className="muted">—</span>
          )}
        </div>
      )}

      {waiting && session?.verification_url && (
        <a
          className="btn primary"
          href={session.verification_url}
          target="_blank"
          rel="noreferrer"
          style={{ justifyContent: "center", gap: 6, textDecoration: "none" }}
        >
          <ExternalLink size={15} strokeWidth={1.75} />
          {t("device.open", vars)}
        </a>
      )}

      {waiting && (
        <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: "var(--text-2)" }}>
          <li>{t("device.step1", vars)}</li>
          <li>
            {t("device.step2.before")}
            <code style={{ fontFamily: "var(--font-mono)" }}>{session?.user_code ?? "…"}</code>
            {t("device.step2.after")}
          </li>
          <li>{t("device.step3", vars)}</li>
        </ol>
      )}

      {phase === "pending" && (
        <div className="row" style={{ gap: 6, fontSize: 13, color: "var(--text-2)" }}>
          <span className="spinner" />
          {t("device.waiting", vars)}
        </div>
      )}

      {phase !== "idle" && (
        <div className="row">
          {(phase === "error" || phase === "expired") && (
            <button className="btn primary" onClick={() => setAttempt((n) => n + 1)}>
              {t("device.retry")}
            </button>
          )}
          <button
            className={phase === "approved" ? "btn primary" : "btn"}
            onClick={phase === "approved" ? onConnected : onClose}
          >
            {phase === "approved" ? t("device.done") : t("action.cancel")}
          </button>
        </div>
      )}
    </div>
  );
}

// ── connect panel ─────────────────────────────────────────────────────────
interface ConnectPanelProps {
  svc: ConnectionService;
  scope: Scope;
  agent: string | null;
  status: AuthStatus | undefined;
  onClose: () => void;
  onConnected: () => void;
}

// Flow phases for the copy-the-link authorization. `idle` = before the user
// clicks Start; the rest mirror OAuthSessionInfo.status on the runtime.
type Phase = "idle" | "starting" | "awaiting_authorization" | "exchanging" | "authorized" | "error";

// Label style for generic connector credential fields (mirrors the google
// creds-path label below).
const fieldLabelStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "var(--text-3)",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  display: "block",
  marginBottom: 6,
};

function ConnectPanel({ svc, scope, agent, status, onClose, onConnected }: ConnectPanelProps) {
  const toast = useToast();
  const t = useT(STR);
  // One value per connector credential field (client_id, client_secret, plus
  // per-service params like shop/tenant); every service uses these now.
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [shared, setShared] = useState<boolean>(status?.shared ?? false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  // Official Olano app available for this service (connectors catalog + auth
  // relay on the runtime). Default path when present; the toggle below
  // reveals the classic bring-your-own-client form instead.
  const officialAvailable = status?.official_available ?? false;
  const [useOwnApp, setUseOwnApp] = useState(false);
  const official = officialAvailable && !useOwnApp;
  // From the start response: "relay" = central callback (no on-box redirect
  // URI involved), so the redirect-mismatch hint would only mislead. Falls
  // back to "direct" when a stale BYO vault blob outranks the official app.
  const [relayMode, setRelayMode] = useState(false);
  // Deterministic from the origin the user is on — localhost in dev, the
  // deployment's <name>.olano.run host on cloud (or an sslip.io dev box), or the
  // official olano.sg domain. This is exactly
  // what must be registered as an Authorized redirect URI on the Google OAuth
  // client, and it matches the `redirect_base` we send to the runtime.
  const callbackUri = `${window.location.origin}/api/auth/${svc.id}/callback`;
  // Set on unmount so an in-flight poll loop stops touching state.
  const cancelledRef = useRef(false);
  useEffect(() => {
    cancelledRef.current = false;
    return () => {
      cancelledRef.current = true;
    };
  }, []);

  const perAgent = scope === "agent" && !!agent;
  const busy = phase === "starting" || phase === "awaiting_authorization" || phase === "exchanging";

  async function fromVault() {
    const fields = svc.fields;
    if (!svc.credsEnv || !fields) return;
    try {
      const r = await api.get<VaultSecret>(`/api/vault/secrets/${encodeURIComponent(svc.credsEnv)}/reveal`);
      if (r.value) {
        // The vault holds a JSON blob ({"client_id": …, …}); fill whichever
        // form fields the blob provides. Google's blob may also be a full
        // downloaded client_secret.json ({"web": {...}} / {"installed": {...}})
        // — look inside the wrapper, mirroring the runtime's resolution.
        let blob: Record<string, unknown>;
        try {
          const parsed: unknown = JSON.parse(r.value);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
          blob = parsed as Record<string, unknown>;
          for (const wrapper of ["web", "installed"]) {
            const inner = blob[wrapper];
            if (typeof inner === "object" && inner !== null && !Array.isArray(inner)) {
              blob = inner as Record<string, unknown>;
              break;
            }
          }
        } catch {
          toast.push(t("toast.vaultBlobInvalid", { env: svc.credsEnv }), "error");
          return;
        }
        setFieldValues((cur) => {
          const next = { ...cur };
          for (const f of fields) {
            const v = blob[f.key];
            if (typeof v === "string" && v) next[f.key] = v;
          }
          return next;
        });
        toast.push(t("toast.vaultLoaded"));
      } else {
        toast.push(t("toast.vaultMissing", { env: svc.credsEnv }), "error");
      }
    } catch {
      toast.push(t("toast.vaultMissing", { env: svc.credsEnv }), "error");
    }
  }

  async function copyText(text: string, okKey: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.push(t(okKey));
    } catch {
      toast.push(t("panel.copyFailed"), "error");
    }
  }

  // Poll the runtime until the user finishes (or fails) authorization in the
  // browser they opened the link on. Mirrors McpOAuthDialog's poll loop.
  async function poll(sessionId: string) {
    while (!cancelledRef.current) {
      await new Promise((r) => window.setTimeout(r, 1500));
      if (cancelledRef.current) return;
      let s: OAuthSessionInfo;
      try {
        s = await api.get<OAuthSessionInfo>(`/api/auth/${svc.id}/poll/${sessionId}`);
      } catch (e) {
        if (cancelledRef.current) return;
        // 404 = the session is gone (runtime restarted mid-flow).
        if (e instanceof ApiError && e.status === 404) {
          setPhase("error");
          setErrorText(t("panel.sessionLost"));
          return;
        }
        continue;
      }
      if (cancelledRef.current) return;
      if (s.status === "authorized") {
        setPhase("authorized");
        toast.push(t("toast.connected", { name: svc.name }));
        onConnected();
        return;
      }
      if (s.status === "error") {
        setPhase("error");
        setErrorText(s.error || t("err.unknown"));
        return;
      }
      setPhase(s.status === "exchanging" ? "exchanging" : "awaiting_authorization");
    }
  }

  async function startAuth() {
    setPhase("starting");
    setErrorText(null);
    setAuthUrl(null);
    try {
      // Send the filled fields as a creds dict (empty creds = the runtime
      // falls back to the vault blob / env pair, then to the official Olano
      // app; google additionally to the legacy GOOGLE_OAUTH_CREDENTIALS
      // path). The official path deliberately sends {} so no half-typed BYO
      // field rides along. redirect_base pins the callback to the origin the
      // user is actually on (handles reverse-proxied hosts — <name>.olano.run
      // or sslip.io dev boxes — where request.base_url is wrong); the runtime
      // ignores it for official (relay-mode) connections.
      const creds: Record<string, string> = {};
      if (!official) {
        for (const f of svc.fields ?? []) {
          const v = (fieldValues[f.key] ?? "").trim();
          if (v) creds[f.key] = v;
        }
      }
      const body: Record<string, unknown> = {
        creds,
        agent: perAgent ? agent : null,
        shared: perAgent ? shared : false,
        redirect_base: window.location.origin,
      };
      const s = await api.post<OAuthSessionInfo>(`/api/auth/${svc.id}/start`, body);
      if (cancelledRef.current) return;
      if (s.status === "error") {
        setPhase("error");
        setErrorText(s.error || t("err.unknown"));
        return;
      }
      setRelayMode(s.auth_mode === "relay");
      setAuthUrl(s.authorization_url ?? null);
      setPhase("awaiting_authorization");
      void poll(s.session_id);
    } catch (err) {
      if (cancelledRef.current) return;
      setPhase("error");
      setErrorText(errDetail(err, t));
    }
  }

  const showForm = phase === "idle" || phase === "error";

  return (
    <div
      style={{
        // Inset sub-panel: bleed to the card edges and tint like surface-2 so
        // the connect flow reads as a distinct step (mirrors the old .conn-panel).
        margin: "14px -18px -18px",
        padding: "14px 18px",
        borderTop: "1px solid var(--border)",
        background: "var(--surface-2)",
        borderBottomLeftRadius: "var(--r)",
        borderBottomRightRadius: "var(--r)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      {perAgent && showForm && (
        <div>
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
            {t("share.toggle")}
          </label>
          <div className="muted" style={{ fontSize: 12, marginLeft: 22, marginTop: 2 }}>
            {t("share.hint")}
          </div>
        </div>
      )}

      {showForm && official && (
        <>
          {/* Official-app path: nothing to configure — no client fields, no
              redirect URI to register (the flow uses the central relay's). */}
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
            {t("official.intro", { name: svc.name })}
          </div>
          <div className="row">
            <button className="btn primary" disabled={busy} onClick={() => void startAuth()}>
              {t("official.connect")}
            </button>
            <button className="btn" disabled={busy} onClick={onClose}>
              {t("action.cancel")}
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy}
              onClick={() => setUseOwnApp(true)}
              style={{ marginLeft: "auto" }}
            >
              {t("official.useOwn")}
            </button>
          </div>
        </>
      )}

      {showForm && !official && (
        <>
          {officialAvailable && (
            <div className="row">
              <button
                className="btn"
                type="button"
                disabled={busy}
                onClick={() => setUseOwnApp(false)}
                style={{ fontSize: 12 }}
              >
                {t("official.useOfficial")}
              </button>
            </div>
          )}
          {/* Redirect URI to register — shown up front (and per-environment) so
              the user can configure it in Google Cloud before authorizing. */}
          <div
            style={{
              padding: "10px 12px",
              borderRadius: "var(--r-sm)",
              background: "var(--surface-3)",
              border: "1px solid var(--border)",
              display: "flex",
              flexDirection: "column",
              gap: 8,
            }}
          >
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>{t("panel.redirectTitle")}</div>
            <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
              {t("panel.redirectSetup")}
            </div>
            <div className="row" style={{ gap: 8, alignItems: "stretch" }}>
              <code
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 11.5,
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-2)",
                  background: "var(--code-bg)",
                  border: "1px solid var(--code-border)",
                  borderRadius: "var(--r-xs)",
                  padding: "6px 8px",
                  wordBreak: "break-all",
                }}
              >
                {callbackUri}
              </code>
              <button
                className="btn"
                type="button"
                onClick={() => void copyText(callbackUri, "panel.redirectCopied")}
                style={{ gap: 6, flexShrink: 0 }}
              >
                <Copy size={15} strokeWidth={1.75} />
                {t("panel.copyRedirect")}
              </button>
            </div>
            <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              {t("panel.redirectClientType")}
            </div>
          </div>

          {svc.fields ? (
            <>
              {/* One input per connector credential field; secrets are masked.
                  Empty form = the runtime falls back to the vault blob. */}
              {svc.fields.map((f) => (
                <div key={f.key}>
                  <label style={fieldLabelStyle}>
                    {t(f.labelKey)}
                    {f.required ? " *" : ""}
                  </label>
                  <input
                    className="input"
                    type={f.secret ? "password" : "text"}
                    autoComplete="off"
                    style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}
                    placeholder={f.placeholder ?? ""}
                    value={fieldValues[f.key] ?? ""}
                    onChange={(e) => setFieldValues((cur) => ({ ...cur, [f.key]: e.target.value }))}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !busy) void startAuth();
                    }}
                  />
                </div>
              ))}
              <div className="row">
                <button className="btn primary" disabled={busy} onClick={() => void startAuth()}>
                  {t("action.startAuth")}
                </button>
                <button className="btn" disabled={busy} onClick={onClose}>
                  {t("action.cancel")}
                </button>
                {svc.credsEnv && (
                  <button
                    className="btn"
                    type="button"
                    onClick={() => void fromVault()}
                    disabled={busy}
                    style={{ marginLeft: "auto" }}
                  >
                    {t("action.fromVault")}
                  </button>
                )}
              </div>
            </>
          ) : null}
        </>
      )}

      {/* Authorization step: link to open on any device + live status. */}
      {(phase === "awaiting_authorization" || phase === "exchanging") && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
            {t("panel.linkIntro")}
          </div>

          {authUrl && (
            <>
              <div className="row" style={{ gap: 8 }}>
                <a
                  className="btn primary"
                  href={authUrl}
                  target="_blank"
                  rel="noreferrer"
                  style={{ gap: 6, textDecoration: "none" }}
                >
                  <ExternalLink size={15} strokeWidth={1.75} />
                  {t("panel.openLink")}
                </a>
                <button
                  className="btn"
                  type="button"
                  onClick={() => void copyText(authUrl, "panel.copied")}
                  style={{ gap: 6 }}
                >
                  <Copy size={15} strokeWidth={1.75} />
                  {t("panel.copyLink")}
                </button>
              </div>
              <div
                style={{
                  fontSize: 11,
                  fontFamily: "var(--font-mono)",
                  color: "var(--text-3)",
                  background: "var(--code-bg)",
                  border: "1px solid var(--code-border)",
                  borderRadius: "var(--r-xs)",
                  padding: "6px 8px",
                  wordBreak: "break-all",
                }}
              >
                {authUrl}
              </div>
            </>
          )}

          {/* Relay-mode flows redirect to the central callback — the on-box
              redirect URI is not involved, so the mismatch hint would mislead. */}
          {!relayMode && (
            <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
              {t("panel.redirectHint")}{" "}
              <code style={{ wordBreak: "break-all" }}>{callbackUri}</code>
            </div>
          )}

          <div className="row" style={{ gap: 6, fontSize: 13, color: "var(--text-2)" }}>
            <span className="spinner" />
            {phase === "exchanging" ? t("panel.exchanging") : t("panel.awaiting")}
          </div>

          <div className="row">
            <button className="btn" onClick={onClose}>
              {t("action.cancel")}
            </button>
          </div>
        </div>
      )}

      {phase === "authorized" && (
        <div className="row" style={{ gap: 6, fontSize: 13, color: "var(--success)" }}>
          <Check size={15} strokeWidth={1.75} />
          {t("panel.connected")}
        </div>
      )}

      {phase === "error" && errorText && (
        <div className="row" style={{ gap: 6, fontSize: 13, color: "var(--danger)" }}>
          {t("panel.error", { detail: errorText })}
        </div>
      )}
    </div>
  );
}
