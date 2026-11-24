import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { chatPath } from "../../core/routeUrls";
import { subscribeStream } from "../../core/agentStream";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useConfirm } from "../../ui/confirm";
import { useToast } from "../../ui/toast";
import { CopyButton } from "../agents/CopyButton";
import { Field, Modal } from "../agents/Modal";
import { STR as ACTIVITY_STR } from "../activity/strings";
import { externalParty, isExternalKind, normName, type PairThread } from "../activity/threads";
import { fmtRel, ProtocolBadge } from "../activity/ui";
import { STR } from "./strings";

/**
 * Admin panel for external access: where callers reach agents, the bearer
 * tokens they present, and the OAuth clients that signed in. Shared by the
 * Config section (fleet view) and an agent's External access tab (filtered to
 * that agent). Everything here talks to `/api/mcp-server/*`, which is
 * admin-only — a member sees a one-line note instead of the panel.
 */

export interface ExternalStatus {
  enabled: boolean;
  endpoint: string | null;
  metadata_url: string | null;
  public_origin_known: boolean;
  https: boolean;
  require_https: boolean;
  serving: boolean;
  exposed_agents: string[];
  tools: string[];
  tokens: { total: number; active: number };
  oauth_enabled: boolean;
  oauth_issuer: string | null;
  oauth_dynamic_registration: boolean;
  oauth_clients: number;
  a2a: {
    enabled: boolean;
    endpoint_template: string | null;
    serving: boolean;
    exposed_agents: string[];
    cards: Record<string, string>;
  } | null;
}

export interface TokenRow {
  id: string;
  label: string;
  agents: string[];
  kind: string;
  client_id: string;
  subject: string;
  created_at: string;
  created_by: string;
  expires_at: string | null;
  last_used_at: string | null;
  active: boolean;
  expired: boolean;
  revoked: boolean;
}

interface ClientRow {
  client_id: string;
  client_name?: string;
  client_uri?: string;
  redirect_uris?: string[];
  registered_at?: number;
  active_tokens: number;
  active_refresh_tokens: number;
}

function errText(e: unknown, fallback: string) {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

function fmtDate(iso: string | null | undefined, never: string): string {
  if (!iso) return never;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function useExternalStatus(): {
  status: ExternalStatus | null;
  forbidden: boolean;
  reload: () => void;
} {
  const [status, setStatus] = useState<ExternalStatus | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const reload = useCallback(() => {
    api
      .get<ExternalStatus>("/api/mcp-server/status")
      .then((s) => {
        setStatus(s);
        setForbidden(false);
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && (e.status === 403 || e.status === 401)) setForbidden(true);
      });
  }, []);
  useEffect(() => reload(), [reload]);
  return { status, forbidden, reload };
}

function agentSlug(name: string): string {
  return (name.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "agent").slice(0, 48);
}

export function EndpointsCard({ status, agentName }: { status: ExternalStatus; agentName?: string }) {
  const t = useT(STR);
  const rows: Array<[string, string]> = [];
  if (status.endpoint) {
    rows.push([t("ext.endpoint.mcp"), status.endpoint]);
    if (agentName) rows.push([t("ext.endpoint.mcpTool"), `ask_${agentSlug(agentName)}`]);
  }
  if (status.a2a?.endpoint_template) {
    if (agentName) {
      const base = status.a2a.endpoint_template.replace("<agent>", agentName);
      rows.push([t("ext.endpoint.a2a"), `${base}/.well-known/agent-card.json`]);
      rows.push([t("ext.endpoint.a2aRpc"), base]);
    } else {
      rows.push([t("ext.endpoint.a2a"), `${status.a2a.endpoint_template}/.well-known/agent-card.json`]);
    }
  }
  if (status.oauth_issuer) rows.push([t("ext.endpoint.oauth"), status.oauth_issuer]);
  rows.push([t("ext.endpoint.header"), "Authorization: Bearer <token>"]);
  return (
    <div className="card" style={{ padding: 14, marginTop: 12 }} data-testid="ext-endpoints">
      <h4 style={{ margin: "0 0 4px" }}>{t(agentName ? "ext.endpoints" : "ext.endpoints.fleet")}</h4>
      <p className="muted" style={{ fontSize: 12, lineHeight: 1.45, marginTop: 0 }}>
        {t(agentName ? "ext.endpoints.hint" : "ext.endpoints.fleet.hint")}
      </p>
      {!status.public_origin_known && (
        <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{t("ext.endpoint.unknown")}</p>
      )}
      <div style={{ display: "grid", gap: 8 }}>
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className="muted" style={{ fontSize: 12, minWidth: 180 }}>{label}</span>
            <code style={{ fontSize: 12, wordBreak: "break-all" }}>{value}</code>
            <CopyButton value={value} label={t("ext.copy")} t={t} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function StatusBadge({ serving, enabled, https, requireHttps, originKnown, surfaceServing = serving }: {
  serving: boolean;
  enabled: boolean;
  https: boolean;
  requireHttps: boolean;
  originKnown: boolean;
  /** The server's own verdict for the whole surface (`status.serving`). The server exempts
   *  loopback and private-network origins from Require HTTPS, so a plain-http origin it does
   *  serve must not read "Refused" here. Defaults to `serving`. */
  surfaceServing?: boolean;
}) {
  const t = useT(STR);
  let text = t("ext.status.off");
  let cls = "badge";
  if (enabled && !originKnown) text = t("ext.status.unknownOrigin");
  else if (enabled && !https && requireHttps && !surfaceServing) text = t("ext.status.notHttps");
  else if (serving) {
    text = t("ext.status.serving");
    cls = "badge admin";
  }
  return <span className={cls}>{text}</span>;
}

export function TokensCard({
  agentName,
  status,
  onChange,
}: {
  agentName?: string;
  status: ExternalStatus | null;
  onChange?: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [tokens, setTokens] = useState<TokenRow[] | null>(null);
  const [minting, setMinting] = useState(false);
  const load = useCallback(() => {
    api
      .get<TokenRow[]>("/api/mcp-server/tokens")
      .then(setTokens)
      .catch(() => setTokens([]));
  }, []);
  useEffect(() => load(), [load]);

  const visible = useMemo(() => {
    if (!tokens) return [];
    if (!agentName) return tokens;
    return tokens.filter((tk) => tk.agents.includes("*") || tk.agents.includes(agentName));
  }, [tokens, agentName]);

  async function revoke(tk: TokenRow) {
    const ok = await confirm({
      title: t("ext.tokens.revoke"),
      message: t("ext.tokens.revokeConfirm", { label: tk.label }),
      confirmLabel: t("ext.tokens.revoke"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/mcp-server/tokens/${encodeURIComponent(tk.id)}`);
      toast.push(t("ext.tokens.revoked"), "success");
      load();
      onChange?.();
    } catch (e) {
      toast.push(errText(e, t("ext.mint.err")), "error");
    }
  }

  return (
    <div className="card" style={{ padding: 14, marginTop: 12 }} data-testid="ext-tokens">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <h4 style={{ margin: 0 }}>{t("ext.tokens")}</h4>
        <button className="btn primary" type="button" onClick={() => setMinting(true)}>
          {t("ext.tokens.mint")}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 12, lineHeight: 1.45 }}>{t("ext.tokens.hint")}</p>
      {agentName && <p className="muted" style={{ fontSize: 12 }}>{t("ext.tokens.forAgent")}</p>}
      {visible.length === 0 ? (
        <p className="muted">{t("ext.tokens.empty")}</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="access-table" style={{ width: "100%", fontSize: 12.5 }}>
            <thead>
              <tr>
                <th>{t("ext.tokens.col.label")}</th>
                <th>{t("ext.tokens.col.agents")}</th>
                <th>{t("ext.tokens.col.kind")}</th>
                <th>{t("ext.tokens.col.expires")}</th>
                <th>{t("ext.tokens.col.lastUsed")}</th>
                <th>{t("ext.tokens.col.state")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((tk) => {
                const state = tk.revoked ? "revoked" : tk.expired ? "expired" : "active";
                return (
                  <tr key={tk.id} style={{ opacity: state === "active" ? 1 : 0.6 }}>
                    <td>
                      <strong>{tk.label}</strong>
                      <div className="muted" style={{ fontSize: 11 }}>{tk.id}</div>
                    </td>
                    <td>{tk.agents.join(", ")}</td>
                    <td>{tk.kind === "oauth" ? t("ext.tokens.kind.oauth") : t("ext.tokens.kind.manual")}</td>
                    <td>{fmtDate(tk.expires_at, t("ext.tokens.never"))}</td>
                    <td>{fmtDate(tk.last_used_at, "—")}</td>
                    <td>{t(`ext.tokens.state.${state}`)}</td>
                    <td style={{ textAlign: "right" }}>
                      {state === "active" && (
                        <button className="btn ghost" type="button" onClick={() => revoke(tk)}>
                          {t("ext.tokens.revoke")}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {minting && (
        <MintDialog
          agentName={agentName}
          status={status}
          onClose={() => setMinting(false)}
          onMinted={() => {
            load();
            onChange?.();
          }}
        />
      )}
    </div>
  );
}

function MintDialog({
  agentName,
  status,
  onClose,
  onMinted,
}: {
  agentName?: string;
  status: ExternalStatus | null;
  onClose: () => void;
  onMinted: () => void;
}) {
  const t = useT(STR);
  const [label, setLabel] = useState("");
  const [agents, setAgents] = useState(agentName ?? "");
  const [ttl, setTtl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [minted, setMinted] = useState<{ token: string; warning: string | null } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const names = agents
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      const res = await api.post<{ token: string; warning: string | null }>("/api/mcp-server/tokens", {
        label: label.trim(),
        agents: names,
        ttl_days: ttl.trim() === "" ? null : parseInt(ttl, 10),
      });
      setMinted(res);
      onMinted();
    } catch (err) {
      setError(errText(err, t("ext.mint.err")));
    } finally {
      setBusy(false);
    }
  }

  if (minted) {
    return (
      <Modal title={t("ext.mint.done.title")} onClose={onClose} footer={
        <button className="btn primary" type="button" onClick={onClose}>{t("ext.mint.close")}</button>
      }>
        <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>{t("ext.mint.done.body")}</p>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <code data-testid="ext-minted-token" style={{ fontSize: 12, wordBreak: "break-all", flex: 1 }}>{minted.token}</code>
          <CopyButton value={minted.token} label={t("ext.copy")} t={t} />
        </div>
        {minted.warning && (
          <p style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 10 }}>{minted.warning}</p>
        )}
      </Modal>
    );
  }

  const exposed = new Set([...(status?.exposed_agents ?? []), ...(status?.a2a?.exposed_agents ?? [])]);
  return (
    <Modal title={t("ext.mint.title")} onClose={onClose}>
      <form onSubmit={submit}>
        <Field label={t("ext.mint.label")} hint={t("ext.mint.label.hint")}>
          <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} required autoFocus />
        </Field>
        <Field label={t("ext.mint.agents")} hint={t("ext.mint.agents.hint")}>
          <textarea
            className="input"
            rows={3}
            value={agents}
            onChange={(e) => setAgents(e.target.value)}
            placeholder={exposed.size ? Array.from(exposed).join("\n") : "*"}
            required
          />
        </Field>
        <Field label={t("ext.mint.ttl")} hint={t("ext.mint.ttl.hint")}>
          <input className="input" type="number" min={0} max={3650} value={ttl} onChange={(e) => setTtl(e.target.value)} />
        </Field>
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="btn" type="button" onClick={onClose}>{t("ext.mint.close")}</button>
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? t("ext.mint.creating") : t("ext.mint.create")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ClientsCard({ onChange }: { onChange?: () => void }) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const load = useCallback(() => {
    api.get<ClientRow[]>("/api/mcp-server/oauth/clients").then(setClients).catch(() => setClients([]));
  }, []);
  useEffect(() => load(), [load]);

  async function forget(c: ClientRow) {
    const name = c.client_name || c.client_id;
    const ok = await confirm({
      title: t("ext.clients.forget"),
      message: t("ext.clients.forgetConfirm", { name }),
      confirmLabel: t("ext.clients.forget"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/mcp-server/oauth/clients/${encodeURIComponent(c.client_id)}`);
      toast.push(t("ext.clients.forgotten"), "success");
      load();
      onChange?.();
    } catch (e) {
      toast.push(errText(e, t("ext.mint.err")), "error");
    }
  }

  return (
    <div className="card" style={{ padding: 14, marginTop: 12 }} data-testid="ext-clients">
      <h4 style={{ margin: "0 0 4px" }}>{t("ext.clients")}</h4>
      <p className="muted" style={{ fontSize: 12, lineHeight: 1.45, marginTop: 0 }}>{t("ext.clients.hint")}</p>
      {!clients || clients.length === 0 ? (
        <p className="muted">{t("ext.clients.empty")}</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table className="access-table" style={{ width: "100%", fontSize: 12.5 }}>
            <thead>
              <tr>
                <th>{t("ext.clients.col.name")}</th>
                <th>{t("ext.clients.col.redirect")}</th>
                <th>{t("ext.clients.col.tokens")}</th>
                <th>{t("ext.clients.col.registered")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {clients.map((c) => (
                <tr key={c.client_id}>
                  <td>
                    <strong>{c.client_name || c.client_id}</strong>
                    <div className="muted" style={{ fontSize: 11 }}>{c.client_id}</div>
                  </td>
                  <td style={{ fontSize: 11.5, wordBreak: "break-all" }}>{(c.redirect_uris || []).join(", ")}</td>
                  <td>{c.active_tokens} / {c.active_refresh_tokens}</td>
                  <td>{c.registered_at ? new Date(c.registered_at * 1000).toLocaleDateString() : "—"}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn ghost" type="button" onClick={() => forget(c)}>{t("ext.clients.forget")}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}


/**
 * The last inbound MCP / A2A conversations with one agent, newest first — one
 * row per caller (the token or OAuth client it used), read from the same
 * rollup the Agent to Agent view lists. Both deep links are offered: the
 * exchange as the caller saw it (Agent to Agent) and the agent-side transcript
 * (Chat), because an operator debugging a call needs both halves.
 */
export function RecentCallsCard({ agentName }: { agentName: string }) {
  const t = useT(STR);
  const ta = useT(ACTIVITY_STR);
  const [rows, setRows] = useState<PairThread[] | null>(null);
  useEffect(() => {
    let alive = true;
    const me = normName(agentName);
    const load = () =>
      api
        .get<PairThread[]>("/api/a2a/threads?kind=external")
        .then((all) => {
          if (!alive) return;
          setRows(
            (all || [])
              .filter((row) => isExternalKind(row.kind) && (normName(row.b) === me || normName(row.a) === me))
              .slice(0, 10),
          );
        })
        .catch(() => {
          // A failed refresh keeps the rows already shown.
          if (alive) setRows((cur) => cur ?? []);
        });
    void load();
    // A call that arrives while the tab is open shows up without leaving and
    // coming back: the list used to load once, so it kept reading "No
    // external calls yet" beside a call the Agent to Agent view already had.
    let timer: number | undefined;
    const unsubscribe = subscribeStream((frame) => {
      const ev = frame as { type?: string; data?: { kind?: string; from?: string; to?: string } };
      if (ev.type !== "agent_message" || !isExternalKind(ev.data?.kind ?? "")) return;
      if (normName(ev.data?.from ?? "") !== me && normName(ev.data?.to ?? "") !== me) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void load(), 400);
    });
    return () => {
      alive = false;
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, [agentName]);
  if (rows === null) return null;
  return (
    <div className="card" style={{ padding: 14, marginTop: 12 }} data-testid="ext-recent-calls">
      <h4 style={{ margin: "0 0 4px" }}>{t("ext.recent")}</h4>
      <p className="muted" style={{ fontSize: 12, lineHeight: 1.45, marginTop: 0 }}>
        {t("ext.recent.hint")}
      </p>
      {rows.length === 0 ? (
        <div className="muted" style={{ fontSize: 12.5 }}>{t("ext.recent.empty")}</div>
      ) : (
        <div style={{ display: "grid", gap: 6 }}>
          {rows.map((row) => {
            const other = row.kind === "inbound" ? row.a : row.b;
            const ext = externalParty(other);
            const chatHref = row.thread_id
              ? chatPath(agentName, row.thread_id)
              : "";
            return (
              <div
                key={row.key}
                className="row"
                style={{ gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}
                data-testid="ext-recent-row"
              >
                <ProtocolBadge protocol={row.protocol || ext?.protocol} />
                <span className="muted" style={{ fontSize: 11 }}>
                  {t(row.kind === "inbound" ? "ext.recent.inbound" : "ext.recent.outbound")}
                </span>
                <span style={{ fontWeight: 600, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {ext ? ext.label : other}
                </span>
                <span className="muted">{fmtRel(ta, row.last_ts)}</span>
                <span className="muted">
                  {ta(row.messages === 1 ? "count.msgs.one" : "count.msgs.other", { n: String(row.messages) })}
                </span>
                <span style={{ marginLeft: "auto", display: "inline-flex", gap: 10 }}>
                  <Link to={`/activity?pair=${encodeURIComponent(row.key)}`} style={{ fontSize: 12 }}>
                    {t("ext.recent.open")}
                  </Link>
                  {chatHref && (
                    <Link to={chatHref} style={{ fontSize: 12 }}>
                      {t("ext.recent.chat")}
                    </Link>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
