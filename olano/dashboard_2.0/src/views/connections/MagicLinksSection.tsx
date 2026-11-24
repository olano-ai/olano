// Auth handshake links — the "sign in from your phone" affordance.
//
// The instance mints a short-lived, PIN-protected page at
// https://<host>/api/connect/<token>; the person who receives it opens it on
// their own device and completes a device flow / OAuth sign-in, or hands over
// an API key straight into the vault. Nothing sensitive travels through the
// chat, and this dashboard never holds the token: `url` + `pin` come back from
// the mint call ONCE and are unrecoverable afterwards — hence the deliberately
// loud one-time panel below.
//
// Every endpoint here is admin-gated server-side, so the whole section is
// hidden for members (and folds itself away on a 403 anyway).

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Copy, ExternalLink, Link2, ShieldAlert, X } from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import type { Agent } from "../../core/types";
import { CONN_SERVICES, OWNER_LOGIN_SECTIONS } from "./types";
import type {
  AuthStatusResponse,
  MagicLinkBinding,
  MagicLinkBindingsResponse,
  MagicLinkFlowInfo,
  MagicLink,
  MagicLinksResponse,
  MagicLinkStatus,
  MintedMagicLink,
  Scope,
} from "./types";
import { STR } from "./strings";

type T = (key: string, vars?: Record<string, string>) => string;

function errDetail(err: unknown, t: T): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return t("err.unknown");
}

// Services the connector-oauth flow offers a picker for. Google has its own
// dedicated flow, so it is not a connector-broker choice.
const CONNECTOR_FLOW_SERVICES = CONN_SERVICES.filter((s) => s.id !== "google");

// Newest first. The runtime already sorts this way; re-sorting keeps the list
// right if that ever stops being true.
const byNewest = (a: MagicLink, b: MagicLink) => b.created_at.localeCompare(a.created_at);

const TERMINAL: ReadonlySet<MagicLinkStatus> = new Set<MagicLinkStatus>([
  "done",
  "error",
  "expired",
  "revoked",
]);

/**
 * The status to show. Listing a link doesn't sweep it, so one whose deadline
 * passed while nobody opened it still reports `pending` alongside
 * `expired: true` — the page it points at would say "expired", and so do we
 * (which also stops it from looking revocable, or keeping the poll alive).
 */
function effectiveStatus(link: MagicLink): MagicLinkStatus {
  return link.expired && !TERMINAL.has(link.status) ? "expired" : link.status;
}

// At most this many rows — the runtime prunes finished links after 24h, but a
// busy day would still bury the rest of the page.
const MAX_ROWS = 10;
// Statuses advance server-side (a background watcher drives each flow) and
// there is no SSE event for them, so poll while anything is still live.
const POLL_MS = 15000;

function statusTone(status: MagicLinkStatus): { background: string; color: string } {
  switch (status) {
    case "done":
      return { background: "var(--success-soft)", color: "var(--success)" };
    case "in_progress":
      return { background: "var(--accent-soft)", color: "var(--accent)" };
    case "pending":
      return { background: "var(--warn-soft)", color: "var(--warn)" };
    case "error":
      return { background: "var(--danger-soft)", color: "var(--danger)" };
    default:
      return { background: "var(--surface-3)", color: "var(--text-3)" };
  }
}

function expiryLabel(link: MagicLink, t: T): string {
  if (link.expired) return t("ml.expired");
  const ms = new Date(link.expires_at).getTime() - Date.now();
  if (!Number.isFinite(ms)) return "";
  if (ms <= 0) return t("ml.expired");
  const mins = Math.max(1, Math.round(ms / 60000));
  return mins < 60
    ? t("ml.expiresMin", { n: String(mins) })
    : t("ml.expiresHour", { n: String(Math.round(mins / 60)) });
}

function requesterLabel(link: MagicLink): string {
  const { channel, username } = link.requested_by ?? {};
  return [channel, username].filter(Boolean).join(" · ");
}

// Compact locale date for a binding's "linked on" stamp; "" for a bad/absent
// value so the meta line just drops the segment.
function boundAtLabel(iso: string): string {
  if (!iso) return "";
  const dt = new Date(iso);
  return Number.isNaN(dt.getTime()) ? "" : dt.toLocaleDateString();
}

// ── section ───────────────────────────────────────────────────────────────

export function MagicLinksSection({
  scope = "system",
  agent = null,
  agents,
}: {
  /** Current Connections scope — seeds the modal's scope picker. Defaults to
      "system" when the section is hosted outside Connections (e.g. Config). */
  scope?: Scope;
  /** Selected agent when scope === "agent"; null in system scope. */
  agent?: string | null;
  agents: Agent[];
}) {
  const t = useT(STR);
  const toast = useToast();
  const confirm = useConfirm();
  const [data, setData] = useState<MagicLinksResponse | null>(null);
  const [flows, setFlows] = useState<MagicLinkFlowInfo[]>([]);
  // Set when the runtime refuses this reader (member on a multi-user install)
  // or has no handshake routes at all — either way there is nothing to show.
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  // Chat identities linked to dashboard accounts (owner-login). null until the
  // first fetch resolves or fails — either way the block stays out of the DOM
  // rather than flashing an empty state. This whole section only mounts for
  // admins, so no extra gating is needed here.
  const [bindings, setBindings] = useState<MagicLinkBinding[] | null>(null);

  const load = useCallback(() => {
    api
      .get<MagicLinksResponse>("/api/magic-links/links?include_terminal=true")
      .then((r) => {
        setData(r);
        setError(null);
      })
      .catch((err) => {
        if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
          setHidden(true);
          return;
        }
        setError(errDetail(err, t));
      });
  }, [t]);

  useEffect(load, [load]);

  const loadBindings = useCallback(() => {
    api
      .get<MagicLinkBindingsResponse>("/api/magic-links/bindings")
      .then((r) => setBindings(r.bindings ?? []))
      .catch(() => setBindings(null));
  }, []);

  useEffect(loadBindings, [loadBindings]);

  // The flow list is static per deployment — fetch it once, alongside the links.
  useEffect(() => {
    api
      .get<{ flows: MagicLinkFlowInfo[] }>("/api/magic-links/flows")
      .then((r) => setFlows(r.flows ?? []))
      .catch(() => setFlows([]));
  }, []);

  const links = useMemo(() => [...(data?.links ?? [])].sort(byNewest), [data]);
  const live = useMemo(() => links.some((l) => !TERMINAL.has(effectiveStatus(l))), [links]);

  // Poll only while something can still change; a settled list is left alone.
  useEffect(() => {
    if (hidden || !live) return;
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [hidden, live, load]);

  const flowLabel = useCallback(
    (id: string) => flows.find((f) => f.id === id)?.label ?? id,
    [flows],
  );

  async function revoke(link: MagicLink) {
    const ok = await confirm({ message: t("ml.confirmRevoke"), danger: true });
    if (!ok) return;
    try {
      await api.del(`/api/magic-links/links/${encodeURIComponent(link.id)}`);
      toast.push(t("ml.toast.revoked"));
      load();
    } catch (err) {
      toast.push(t("ml.toast.revokeFailed", { detail: errDetail(err, t) }), "error");
    }
  }

  async function revokeBinding(binding: MagicLinkBinding) {
    const ok = await confirm({ message: t("ml.bind.confirmRevoke", { key: binding.key }), danger: true });
    if (!ok) return;
    try {
      // The key holds a colon ("telegram:123…"), so it must be encoded.
      await api.del(`/api/magic-links/bindings/${encodeURIComponent(binding.key)}`);
      toast.push(t("ml.bind.toast.revoked"));
      loadBindings();
    } catch (err) {
      toast.push(t("ml.bind.toast.revokeFailed", { detail: errDetail(err, t) }), "error");
    }
  }

  if (hidden) return null;

  const available = data?.available ?? false;
  const shown = links.slice(0, MAX_ROWS);

  return (
    <section className="card" style={{ padding: 18, marginTop: 24 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        {/* Shrinkable basis so the button rides on the same line at desktop
            width and drops below it on a phone. */}
        <div style={{ flex: "1 1 320px", minWidth: 0 }}>
          <h2 style={{ margin: 0, fontSize: 15 }}>{t("ml.title")}</h2>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5, lineHeight: 1.5 }}>
            {t("ml.hint")}
          </p>
        </div>
        {/* No button when minting would always fail — the reason takes its place. */}
        {available && (
          <button className="btn primary" style={{ gap: 6, flexShrink: 0 }} onClick={() => setModalOpen(true)}>
            <Link2 size={15} strokeWidth={1.75} />
            {t("ml.send")}
          </button>
        )}
      </div>

      {data && !available && (
        <div
          className="row"
          style={{
            marginTop: 12,
            gap: 8,
            alignItems: "flex-start",
            padding: "10px 12px",
            borderRadius: "var(--r-sm)",
            background: "var(--warn-soft)",
            border: "1px solid var(--warn)",
            fontSize: 12.5,
            lineHeight: 1.5,
            color: "var(--text-2)",
          }}
        >
          <span style={{ color: "var(--warn)", display: "inline-flex", flexShrink: 0, marginTop: 1 }}>
            <ShieldAlert size={15} strokeWidth={1.75} />
          </span>
          <span style={{ minWidth: 0 }}>{data.reason}</span>
        </div>
      )}

      {error && (
        <div className="error-text" style={{ marginTop: 12, fontSize: 12.5 }}>
          {t("ml.loadFailed", { detail: error })}
        </div>
      )}

      {!error && data && links.length === 0 && (
        <p className="muted" style={{ margin: "14px 0 0", fontSize: 12.5 }}>
          {t("ml.empty")}
        </p>
      )}

      {shown.length > 0 && (
        <div style={{ marginTop: 12 }}>
          {shown.map((link) => (
            <LinkRow
              key={link.id}
              link={link}
              label={flowLabel(link.flow)}
              onRevoke={() => void revoke(link)}
            />
          ))}
          {links.length > shown.length && (
            <p className="muted" style={{ margin: "10px 0 0", fontSize: 11.5 }}>
              {t("ml.more", { n: String(links.length - shown.length) })}
            </p>
          )}
        </div>
      )}

      {/* Chat identities already linked to a dashboard account via owner-login.
          Independent of the pending-links list above and of `available` — a
          linked account can be revoked even while minting is switched off. */}
      {bindings !== null && (
        <div style={{ marginTop: 20, borderTop: "1px solid var(--border)", paddingTop: 16 }}>
          <h3 style={{ margin: 0, fontSize: 13.5, fontWeight: 600 }}>{t("ml.bind.title")}</h3>
          <p className="muted" style={{ margin: "3px 0 0", fontSize: 12, lineHeight: 1.5 }}>
            {t("ml.bind.hint")}
          </p>
          {bindings.length === 0 ? (
            <p className="muted" style={{ margin: "12px 0 0", fontSize: 12.5 }}>
              {t("ml.bind.empty")}
            </p>
          ) : (
            <div style={{ marginTop: 8 }}>
              {bindings.map((b) => (
                <BindingRow key={b.key} binding={b} onRevoke={() => void revokeBinding(b)} />
              ))}
            </div>
          )}
        </div>
      )}

      {modalOpen && (
        <SendLinkModal
          flows={flows}
          agents={agents}
          defaultAgent={scope === "agent" ? (agent ?? "") : ""}
          onClose={() => setModalOpen(false)}
          onMinted={load}
        />
      )}
    </section>
  );
}

// ── one row of the recent-links list ──────────────────────────────────────
// Nothing here is secret: the API returns no token and no PIN, by design.

function LinkRow({
  link,
  label,
  onRevoke,
}: {
  link: MagicLink;
  label: string;
  onRevoke: () => void;
}) {
  const t = useT(STR);
  const status = effectiveStatus(link);
  const terminal = TERMINAL.has(status);
  const requester = requesterLabel(link);
  const outcome = status === "error" ? link.error : status === "done" ? link.result : "";

  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
        flexWrap: "wrap",
        padding: "10px 0",
        borderTop: "1px solid var(--border)",
      }}
    >
      <div style={{ flex: "1 1 220px", minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>{label}</div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 2, lineHeight: 1.5, wordBreak: "break-word" }}>
          {link.service && (
            <span style={{ fontFamily: "var(--font-mono)" }}>{link.service}</span>
          )}
          {link.service && (requester || link.agent) ? " · " : ""}
          {requester}
          {link.agent ? `${requester ? " · " : ""}${t("ml.agent", { name: link.agent })}` : ""}
        </div>
        {outcome && (
          <div
            style={{
              fontSize: 11.5,
              marginTop: 2,
              lineHeight: 1.5,
              wordBreak: "break-word",
              color: status === "error" ? "var(--danger)" : "var(--text-3)",
            }}
          >
            {outcome}
          </div>
        )}
      </div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
        <span className="badge" style={statusTone(status)}>
          {t(`ml.status.${status}`)}
        </span>
        {!terminal && (
          <span className="muted" style={{ fontSize: 11.5 }}>
            {expiryLabel(link, t)}
          </span>
        )}
        {!terminal && (
          <button className="btn danger" onClick={onRevoke} style={{ fontSize: 12, padding: "4px 10px" }}>
            {t("ml.revoke")}
          </button>
        )}
      </div>
    </div>
  );
}

// ── one row of the linked-accounts list ───────────────────────────────────
// Nothing here is secret either: the bindings API returns no token or session.

function BindingRow({
  binding,
  onRevoke,
}: {
  binding: MagicLinkBinding;
  onRevoke: () => void;
}) {
  const t = useT(STR);
  // Localised bind method, falling back to the raw value for any method the UI
  // doesn't know a phrase for.
  const viaKey = `ml.bind.via.${binding.bound_via}`;
  const viaName = t(viaKey) === viaKey ? binding.bound_via : t(viaKey);
  const meta = [binding.email, t("ml.bind.via", { via: viaName }), boundAtLabel(binding.bound_at)]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
        flexWrap: "wrap",
        padding: "10px 0",
        borderTop: "1px solid var(--border)",
      }}
    >
      <div style={{ flex: "1 1 220px", minWidth: 0 }}>
        <div
          style={{ fontSize: 13, fontWeight: 600, fontFamily: "var(--font-mono)", wordBreak: "break-word" }}
        >
          {binding.key}
        </div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 2, lineHeight: 1.5, wordBreak: "break-word" }}>
          {meta}
        </div>
      </div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
        <button className="btn danger" onClick={onRevoke} style={{ fontSize: 12, padding: "4px 10px" }}>
          {t("ml.bind.revoke")}
        </button>
      </div>
    </div>
  );
}

// ── mint modal ────────────────────────────────────────────────────────────

function SendLinkModal({
  flows,
  agents,
  defaultAgent,
  onClose,
  onMinted,
}: {
  flows: MagicLinkFlowInfo[];
  agents: Agent[];
  /** "" ⇒ system scope. */
  defaultAgent: string;
  onClose: () => void;
  /** Fired once a link exists, so the list behind the modal picks it up. */
  onMinted: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const panelRef = useRef<HTMLDivElement>(null);
  useDialogA11y(panelRef, onClose);

  const flowIds = useMemo(() => flows.map((f) => f.id), [flows]);
  const [flow, setFlow] = useState(() =>
    flowIds.includes("connector-oauth") ? "connector-oauth" : (flowIds[0] ?? "connector-oauth"),
  );
  const [service, setService] = useState(CONNECTOR_FLOW_SERVICES[0]?.id ?? "");
  // connector-device: which service's sign-in code the page shows. The
  // candidates come from the runtime — a service is offered only when its
  // status `methods` lists "device" — so the picker can never mint a link the
  // adapter would refuse. System-scope status is enough: the methods are a
  // property of the provider, not of the scope.
  const [authStatus, setAuthStatus] = useState<AuthStatusResponse | null>(null);
  useEffect(() => {
    api
      .cachedGet<AuthStatusResponse>("/api/auth/status")
      .then(setAuthStatus)
      .catch(() => setAuthStatus({}));
  }, []);
  const deviceServices = useMemo(
    () => CONN_SERVICES.filter((s) => authStatus?.[s.id]?.methods?.includes("device")),
    [authStatus],
  );
  const [deviceServicePick, setDeviceServicePick] = useState("");
  // Derived rather than synced in an effect: the first candidate until the
  // person picks one, and never a value the list no longer offers.
  const deviceService = deviceServices.some((s) => s.id === deviceServicePick)
    ? deviceServicePick
    : (deviceServices[0]?.id ?? "");
  const [agent, setAgent] = useState(defaultAgent);
  const [shared, setShared] = useState(false);
  const [mcpUrl, setMcpUrl] = useState("");
  const [filePath, setFilePath] = useState("");
  const [secretName, setSecretName] = useState("");
  const [reason, setReason] = useState("");
  const [provider, setProvider] = useState("");
  // owner-login: which dashboard section the signed-in link lands on.
  const [section, setSection] = useState<string>("overview");
  // channel-connect: which messengers this customer's page offers.
  const [connectPlatforms, setConnectPlatforms] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  // The minted link. Held ONLY here, only until this modal closes — the runtime
  // stores a hash and can never hand the url/pin back.
  const [minted, setMinted] = useState<MintedMagicLink | null>(null);

  const selected = flows.find((f) => f.id === flow);
  // owner-login carries its own agent field (only for section=prompt), so it
  // opts out of the generic scope picker below; codex-device signs the
  // deployment's own Codex login in and has no scope at all. connector-device
  // (and its github-device alias) take a scope exactly like connector-oauth.
  const takesAgent = flow !== "codex-device" && flow !== "owner-login";
  // Flows whose result is stored (or applied) per agent, so one is mandatory.
  // edit-file is the exception: an absolute path names a file outside any
  // workspace, so it needs no agent (the backend gates that on admin rights).
  const needsAgent = flow === "mcp-oauth" || flow === "channel-connect" || flow === "edit-prompt";

  const canSubmit =
    !busy &&
    !!flow &&
    (flow !== "connector-oauth" || !!service) &&
    (flow !== "connector-device" || !!deviceService) &&
    (flow !== "mcp-oauth" || (!!mcpUrl.trim() && !!agent)) &&
    (flow !== "secret-entry" || !!secretName.trim()) &&
    (flow !== "edit-prompt" || !!agent) &&
    (flow !== "edit-file" ||
      (!!filePath.trim() && (filePath.trim().startsWith("/") || !!agent))) &&
    (flow !== "channel-connect" || (!!agent && connectPlatforms.length > 0)) &&
    // The prompt section opens a specific agent's prompt, so it needs one.
    (flow !== "owner-login" || section !== "prompt" || !!agent);

  function buildParams(): Record<string, unknown> {
    const params: Record<string, unknown> = {};
    switch (flow) {
      case "connector-oauth":
        params.service = service;
        if (agent) {
          params.agent = agent;
          params.shared = shared;
        }
        break;
      case "connector-device":
        params.service = deviceService;
        if (agent) {
          params.agent = agent;
          params.shared = shared;
        }
        break;
      case "github-device":
        // Legacy alias of connector-device with the service pinned to GitHub
        // on the runtime — same scope params, no service.
        if (agent) {
          params.agent = agent;
          params.shared = shared;
        }
        break;
      case "google":
        if (agent) params.agent = agent;
        break;
      case "mcp-oauth":
        params.url = mcpUrl.trim();
        params.agent = agent;
        break;
      case "secret-entry":
        params.name = secretName.trim().toUpperCase();
        if (reason.trim()) params.reason = reason.trim();
        if (agent) params.agent = agent;
        break;
      case "provider-key":
        if (provider.trim()) params.provider = provider.trim().toLowerCase();
        if (agent) params.agent = agent;
        break;
      case "owner-login":
        params.section = section;
        // Only the prompt section is agent-scoped; drop it otherwise.
        if (section === "prompt" && agent) params.agent = agent;
        break;
      case "edit-prompt":
        params.agent = agent;
        break;
      case "edit-file":
        params.path = filePath.trim();
        if (agent) params.agent = agent;
        break;
      case "channel-connect":
        params.agent = agent;
        params.platforms = connectPlatforms;
        break;
      default:
        break; // codex-device takes no parameters
    }
    return params;
  }

  async function mint() {
    if (!canSubmit) return;
    setBusy(true);
    setErrorText(null);
    try {
      const link = await api.post<MintedMagicLink>("/api/magic-links/links", {
        flow,
        params: buildParams(),
      });
      setMinted(link);
      onMinted();
    } catch (err) {
      setErrorText(errDetail(err, t));
    } finally {
      setBusy(false);
    }
  }


  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.push(t("ml.toast.copied"));
    } catch {
      toast.push(t("panel.copyFailed"), "error");
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="card modal-card"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("ml.modal.title")}
        onClick={(e) => e.stopPropagation()}
        style={{
          // Viewport-relative, NOT `maxWidth: "100%"`: .modal-overlay is a grid
          // whose auto track grows to the card's max-content width, so a
          // percentage max-width resolves against 520px and a phone gets a
          // card wider than its screen (the right-hand buttons fall off).
          // 40px = the overlay's 20px padding on each side.
          width: "min(520px, calc(100vw - 40px))",
          overflow: "auto",
          overscrollBehavior: "contain",
          padding: 22,
        }}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
          <div style={{ minWidth: 0 }}>
            <h2 style={{ margin: 0, fontSize: 16 }}>{t("ml.modal.title")}</h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5, lineHeight: 1.5 }}>
              {minted ? t("ml.result.intro") : t("ml.modal.intro")}
            </p>
          </div>
          <button className="btn" onClick={onClose} aria-label={t("action.cancel")} style={{ flexShrink: 0 }}>
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        {minted ? (
          <MintedPanel minted={minted} onCopy={copy} onClose={onClose} />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 14 }}>
            <Field label={t("ml.field.flow")} hint={selected?.instructions}>
              <select className="input" value={flow} onChange={(e) => setFlow(e.target.value)}>
                {flows.length === 0 && <option value={flow}>{flow}</option>}
                {flows.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.label}
                  </option>
                ))}
              </select>
            </Field>

            {flow === "connector-oauth" && (
              <Field label={t("ml.field.service")}>
                <select className="input" value={service} onChange={(e) => setService(e.target.value)}>
                  {CONNECTOR_FLOW_SERVICES.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {flow === "connector-device" && (
              <Field
                label={t("ml.field.service")}
                hint={deviceServices.length > 0 ? t("ml.field.service.device.hint") : t("ml.field.service.device.none")}
              >
                <select
                  className="input"
                  value={deviceService}
                  onChange={(e) => setDeviceServicePick(e.target.value)}
                  disabled={deviceServices.length === 0}
                >
                  {deviceServices.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {flow === "mcp-oauth" && (
              <Field label={t("ml.field.mcpUrl")} hint={t("ml.field.mcpUrl.hint")}>
                <input
                  className="input"
                  placeholder="https://mcp.example.com/sse"
                  value={mcpUrl}
                  onChange={(e) => setMcpUrl(e.target.value)}
                  style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}
                />
              </Field>
            )}

            {flow === "edit-file" && (
              <Field label={t("ml.field.path")} hint={t("ml.field.path.hint")}>
                <input
                  className="input"
                  placeholder="notes/PLAN.md"
                  value={filePath}
                  onChange={(e) => setFilePath(e.target.value)}
                  style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}
                />
              </Field>
            )}

            {flow === "secret-entry" && (
              <>
                <Field label={t("ml.field.secretName")} hint={t("ml.field.secretName.hint")}>
                  <input
                    className="input"
                    placeholder="STRIPE_API_KEY"
                    value={secretName}
                    onChange={(e) => setSecretName(e.target.value)}
                    style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}
                  />
                </Field>
                <Field label={t("ml.field.reason")} hint={t("ml.field.reason.hint")}>
                  <input
                    className="input"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </Field>
              </>
            )}

            {flow === "provider-key" && (
              <Field label={t("ml.field.provider")} hint={t("ml.field.provider.hint")}>
                <input
                  className="input"
                  placeholder="openai"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  style={{ fontFamily: "var(--font-mono)", fontSize: 12.5 }}
                />
              </Field>
            )}

            {flow === "channel-connect" && (
              <Field label={t("ml.field.platforms")} hint={t("ml.field.platforms.hint")}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {(selected?.platforms ?? []).map((p) => (
                    <label key={p.platform} className="row" style={{ gap: 6, fontSize: 13 }}>
                      <input
                        type="checkbox"
                        checked={connectPlatforms.includes(p.platform)}
                        onChange={(e) =>
                          setConnectPlatforms((prev) =>
                            e.target.checked
                              ? [...prev, p.platform]
                              : prev.filter((x) => x !== p.platform),
                          )
                        }
                      />
                      <span>
                        {p.label}
                        <span className="muted" style={{ marginLeft: 6, fontSize: 11.5 }}>
                          {p.mode === "qr" ? t("ml.mode.qr") : t("ml.mode.token")}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              </Field>
            )}

            {flow === "owner-login" && (
              <>
                <Field label={t("ml.field.section")} hint={t("ml.field.section.hint")}>
                  <select className="input" value={section} onChange={(e) => setSection(e.target.value)}>
                    {OWNER_LOGIN_SECTIONS.map((s) => (
                      <option key={s} value={s}>
                        {t(`ml.section.${s}`)}
                      </option>
                    ))}
                  </select>
                </Field>
                {/* Only the prompt section is agent-scoped; the field appears
                    (and is required) just for it. */}
                {section === "prompt" && (
                  <Field label={t("ml.field.agent")} hint={t("ml.field.olAgent.hint")}>
                    <select
                      className="input"
                      value={agent}
                      onChange={(e) => setAgent(e.target.value)}
                      style={!agent ? { borderColor: "var(--danger)" } : undefined}
                    >
                      <option value="">{t("ml.agent.select")}</option>
                      {agents.map((a) => (
                        <option key={a.name} value={a.name}>
                          {a.name}
                        </option>
                      ))}
                    </select>
                  </Field>
                )}
              </>
            )}

            {takesAgent && (
              <Field label={t("ml.field.agent")} hint={t("ml.field.agent.hint")}>
                <select
                  className="input"
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                  style={needsAgent && !agent ? { borderColor: "var(--danger)" } : undefined}
                >
                  <option value="">{needsAgent ? t("ml.agent.select") : t("ml.agent.system")}</option>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {(flow === "connector-oauth" || flow === "connector-device" || flow === "github-device") && !!agent && (
              <label className="row" style={{ gap: 6, fontSize: 13, alignItems: "flex-start" }}>
                <input
                  type="checkbox"
                  checked={shared}
                  onChange={(e) => setShared(e.target.checked)}
                  style={{ marginTop: 3 }}
                />
                <span style={{ minWidth: 0 }}>{t("share.toggle")}</span>
              </label>
            )}

            {errorText && (
              <div className="error-text" style={{ fontSize: 12.5, lineHeight: 1.5 }}>
                {errorText}
              </div>
            )}

            <div className="row" style={{ flexWrap: "wrap" }}>
              <button className="btn primary" disabled={!canSubmit} onClick={() => void mint()}>
                {busy ? t("ml.action.minting") : t("ml.action.mint")}
              </button>
              <button className="btn" disabled={busy} onClick={onClose}>
                {t("action.cancel")}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// The one-time reveal. Loud on purpose: neither value can be fetched again, and
// the link is useless to the recipient without the PIN.
function MintedPanel({
  minted,
  onCopy,
  onClose,
}: {
  minted: MintedMagicLink;
  onCopy: (text: string) => Promise<void>;
  onClose: () => void;
}) {
  const t = useT(STR);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 14 }}>
      <div
        className="row"
        style={{
          gap: 8,
          alignItems: "flex-start",
          padding: "10px 12px",
          borderRadius: "var(--r-sm)",
          background: "var(--warn-soft)",
          border: "1px solid var(--warn)",
          fontSize: 12.5,
          lineHeight: 1.5,
          color: "var(--text-2)",
        }}
      >
        <span style={{ color: "var(--warn)", display: "inline-flex", flexShrink: 0, marginTop: 1 }}>
          <ShieldAlert size={15} strokeWidth={1.75} />
        </span>
        <span style={{ minWidth: 0 }}>{minted.pin ? t("ml.result.once") : t("ml.result.onceNoPin")}</span>
      </div>

      <div>
        <div style={valueLabelStyle}>{t("ml.result.url")}</div>
        <div style={{ display: "flex", alignItems: "stretch", gap: 6 }}>
          <div
            style={{
              flex: 1,
              minWidth: 0,
              fontSize: 11.5,
              fontFamily: "var(--font-mono)",
              color: "var(--text-2)",
              background: "var(--code-bg)",
              border: "1px solid var(--code-border)",
              borderRadius: "var(--r-xs)",
              padding: "7px 9px",
              wordBreak: "break-all",
              userSelect: "all",
            }}
          >
            {minted.url}
          </div>
          <InlineCopyButton text={minted.url} label={t("ml.action.copyLink")} onCopy={onCopy} />
        </div>
      </div>

      {minted.pin && (
        <div>
          <div style={valueLabelStyle}>{t("ml.result.pin")}</div>
          <div style={{ display: "flex", alignItems: "stretch", gap: 6 }}>
            <div
              style={{
                flex: 1,
                minWidth: 0,
                fontFamily: "var(--font-mono)",
                fontSize: 22,
                fontWeight: 700,
                letterSpacing: 4,
                userSelect: "all",
                color: "var(--text)",
                background: "var(--code-bg)",
                border: "1px solid var(--code-border)",
                borderRadius: "var(--r-xs)",
                padding: "7px 9px",
                textAlign: "center",
              }}
            >
              {minted.pin}
            </div>
            <InlineCopyButton text={minted.pin} label={t("ml.action.copyCode")} onCopy={onCopy} />
          </div>
        </div>
      )}

      <div className="muted" style={{ fontSize: 12, lineHeight: 1.5 }}>
        {t("ml.result.expiry", { n: String(minted.ttl_minutes) })}
      </div>

      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button className="btn primary" style={{ gap: 6 }} onClick={() => void onCopy(minted.url)}>
          <Copy size={15} strokeWidth={1.75} />
          {t("ml.action.copyLink")}
        </button>
        {minted.pin && (
          <button className="btn" style={{ gap: 6 }} onClick={() => void onCopy(minted.pin)}>
            <Copy size={15} strokeWidth={1.75} />
            {t("ml.action.copyCode")}
          </button>
        )}
        <a
          className="btn"
          href={minted.url}
          target="_blank"
          rel="noopener noreferrer"
          style={{ gap: 6, textDecoration: "none" }}
        >
          <ExternalLink size={15} strokeWidth={1.75} />
          {t("ml.action.openBrowser")}
        </a>
        <button className="btn" onClick={onClose} style={{ marginLeft: "auto" }}>
          {t("ml.action.done")}
        </button>
      </div>
    </div>
  );
}

// A small icon button that copies `text` and briefly flips to a "Copied" tick.
// Rendered flush to the right of the link / PIN value it belongs to.
function InlineCopyButton({
  text,
  label,
  onCopy,
}: {
  text: string;
  label: string;
  onCopy: (text: string) => Promise<void>;
}) {
  return (
    <button
      className="btn"
      aria-label={label}
      title={label}
      onClick={() => void onCopy(text)}
      style={{ flexShrink: 0, padding: "0 11px", display: "inline-flex", alignItems: "center" }}
    >
      <Copy size={15} strokeWidth={1.75} />
    </button>
  );
}

// ── local form bits (ConfigView's Field is not exported) ──────────────────

// Mirrors ConnectPanel's `fieldLabelStyle` in ConnectionsView.
const valueLabelStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: "var(--text-3)",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  marginBottom: 6,
};

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label style={{ ...valueLabelStyle, display: "block" }}>{label}</label>
      {children}
      {hint && (
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4, lineHeight: 1.5 }}>
          {hint}
        </div>
      )}
    </div>
  );
}
