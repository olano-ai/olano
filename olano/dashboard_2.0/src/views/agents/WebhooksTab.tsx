// Per-agent webhooks tab — outbound and inbound.
//
// Outbound: POST agent events to an external URL (signed, rate-limited, retried).
// Inbound:  expose a self-authenticating ingress URL that an external sender
//           POSTs arbitrary payloads to; the agent processes each one using a
//           user-written prompt with the full payload appended.
//
// Both share list / add / edit / delete, a live "Test" button, and a per-webhook
// delivery-history viewer. Self-contained (its own modal + logs dialog).

import { useCallback, useEffect, useState } from "react";
import {
  Pencil,
  Trash2,
  Send,
  History,
  Webhook,
  CheckCircle2,
  XCircle,
  Copy,
  ArrowUpRight,
  ArrowDownLeft,
} from "lucide-react";
import { api, ApiError } from "../../core/api";
import { useToast } from "../../ui/toast";
import { Modal, Field, ConfirmModal } from "./Modal";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import type { AgentConfig } from "./types";

type Direction = "outbound" | "inbound";

interface WebhookStats {
  total: number;
  ok: number;
  failed: number;
  last_ts: string | null;
  last_ok: boolean | null;
  last_status: number | null;
}

interface WebhookView {
  id: string;
  name: string;
  direction: Direction;
  url: string;
  events: string[];
  enabled: boolean;
  has_secret: boolean;
  headers: Record<string, string>;
  timeout_seconds: number;
  max_retries: number;
  rate_limit_per_minute: number;
  verify_tls: boolean;
  block_private_addresses: boolean;
  // inbound-only
  prompt?: string;
  respond?: boolean;
  accumulate_thread?: boolean;
  ingress_path?: string;
  stats?: WebhookStats;
  invalid?: string;
}

interface TestResult {
  ok: boolean;
  status_code: number | null;
  duration_ms: number | null;
  attempts: number;
  error: string | null;
  response_body?: string;
  // inbound
  response?: string | null;
  message?: string;
}

interface DeliveryLog {
  id: string;
  ts: string;
  event_type: string;
  ok: boolean;
  status_code: number | null;
  duration_ms: number | null;
  attempts: number;
  is_test: boolean;
  direction?: string;
  error: string | null;
}

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : fallback;
}

function fullIngressUrl(path: string): string {
  try {
    return window.location.origin + path;
  } catch {
    return path;
  }
}

export function WebhooksTab({
  cfg: _cfg,
  agentName,
  reload,
}: {
  cfg: AgentConfig;
  agentName: string;
  reload: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [items, setItems] = useState<WebhookView[]>([]);
  const [events, setEvents] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [edit, setEdit] = useState<WebhookView | { direction: Direction } | null>(null);
  const [del, setDel] = useState<WebhookView | null>(null);
  const [logsFor, setLogsFor] = useState<WebhookView | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const base = `/api/agents/${encodeURIComponent(agentName)}/webhooks`;

  const load = useCallback(() => {
    setLoading(true);
    api
      .get<{ webhooks: WebhookView[]; available_events: string[] }>(base)
      .then((r) => {
        setItems(r.webhooks || []);
        setEvents(r.available_events || []);
      })
      .catch((e) => toast.push(errText(e, t("wh.toast.loadFailed")), "error"))
      .finally(() => setLoading(false));
  }, [base, toast]);

  useEffect(() => {
    load();
  }, [load]);

  async function runTest(w: WebhookView) {
    setBusyId(w.id);
    try {
      const { result } = await api.post<{ result: TestResult }>(`${base}/${w.id}/test`, {});
      if (result.ok) {
        if (w.direction === "inbound") {
          toast.push(t("wh.toast.testInboundOk", { name: w.name }), "info");
        } else {
          toast.push(
            t("wh.toast.testOutboundOk", { name: w.name, status: String(result.status_code), ms: String(result.duration_ms) }),
            "info",
          );
        }
      } else {
        toast.push(t("wh.toast.testFailed", { name: w.name, error: result.error ?? t("wh.toast.failedWord") }), "error");
      }
      load();
    } catch (e) {
      toast.push(errText(e, t("wh.toast.testFailedGeneric")), "error");
    } finally {
      setBusyId(null);
    }
  }

  async function doDelete(w: WebhookView) {
    setBusyId(w.id);
    try {
      await api.del(`${base}/${w.id}`);
      toast.push(t("wh.toast.removed"), "success");
      setDel(null);
      load();
      reload();
    } catch (e) {
      toast.push(errText(e, t("wh.toast.deleteFailed")), "error");
    } finally {
      setBusyId(null);
    }
  }

  function copyIngress(w: WebhookView) {
    if (!w.ingress_path) return;
    const url = fullIngressUrl(w.ingress_path);
    navigator.clipboard?.writeText(url).then(
      () => toast.push(t("wh.toast.ingressCopied")),
      () => toast.push(url, "info"),
    );
  }

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div>
          <strong style={{ fontSize: 15 }}>{t("wh.title")}</strong>
          <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
            {t("wh.subtitle")}
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn row" style={{ gap: 6 }} onClick={() => setEdit({ direction: "inbound" })}>
            <ArrowDownLeft size={15} strokeWidth={1.75} /> {t("wh.addInbound")}
          </button>
          <button className="btn primary row" style={{ gap: 6 }} onClick={() => setEdit({ direction: "outbound" })}>
            <ArrowUpRight size={15} strokeWidth={1.75} /> {t("wh.addOutbound")}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="muted" style={{ padding: 24 }}>
          {t("wh.loading")}
        </div>
      ) : items.length === 0 ? (
        <div
          className="card"
          style={{ padding: 32, textAlign: "center", display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}
        >
          <Webhook size={34} strokeWidth={1.5} style={{ opacity: 0.5 }} />
          <strong>{t("wh.empty.title")}</strong>
          <span className="muted" style={{ fontSize: 13 }}>
            {t("wh.empty.hint")}
          </span>
        </div>
      ) : (
        items.map((w) => {
          const inbound = w.direction === "inbound";
          return (
            <div key={w.id} className="card" style={{ padding: "12px 14px", marginBottom: 10 }}>
              <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <span className="badge row" style={{ gap: 4, fontSize: 11 }}>
                      {inbound ? <ArrowDownLeft size={12} /> : <ArrowUpRight size={12} />}
                      {inbound ? t("wh.direction.inbound") : t("wh.direction.outbound")}
                    </span>
                    <strong style={{ fontSize: 14 }}>{w.name}</strong>
                    {w.enabled ? (
                      <span className="badge" style={{ color: "var(--success)" }}>{t("wh.status.enabled")}</span>
                    ) : (
                      <span className="badge" style={{ color: "var(--text-3)" }}>{t("wh.status.disabled")}</span>
                    )}
                    {!inbound && w.has_secret && (
                      <span className="badge" title={t("wh.badge.signedTitle")}>{t("wh.badge.signed")}</span>
                    )}
                    {inbound && w.respond && <span className="badge">{t("wh.badge.replies")}</span>}
                    {w.invalid && (
                      <span className="badge" style={{ color: "var(--danger)" }}>{t("wh.badge.invalid")}</span>
                    )}
                  </div>

                  {inbound ? (
                    <>
                      {w.prompt && (
                        <div className="muted" style={{ fontSize: 12, marginTop: 4, lineHeight: 1.4 }}>
                          {w.prompt.length > 140 ? w.prompt.slice(0, 140) + "…" : w.prompt}
                        </div>
                      )}
                      {w.ingress_path && (
                        <div
                          className="row"
                          style={{ gap: 6, marginTop: 6, alignItems: "center", maxWidth: "100%" }}
                        >
                          <code
                            style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
                          >
                            {fullIngressUrl(w.ingress_path)}
                          </code>
                          <button
                            className="btn ghost"
                            style={{ padding: "2px 6px" }}
                            title={t("wh.copyIngressTitle")}
                            onClick={() => copyIngress(w)}
                          >
                            <Copy size={13} strokeWidth={1.75} />
                          </button>
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      <div
                        className="muted"
                        style={{ fontSize: 12, fontFamily: "var(--font-mono)", marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {w.url}
                      </div>
                      <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                        {(w.events || []).map((ev) => (
                          <span key={ev} className="badge" style={{ fontSize: 11 }}>
                            {ev}
                          </span>
                        ))}
                      </div>
                    </>
                  )}

                  {w.stats && w.stats.total > 0 && (
                    <div className="row" style={{ gap: 12, marginTop: 8, fontSize: 12 }}>
                      <span className="row" style={{ gap: 4, color: "var(--success)" }}>
                        <CheckCircle2 size={13} /> {w.stats.ok}
                      </span>
                      <span className="row" style={{ gap: 4, color: w.stats.failed ? "var(--danger)" : "var(--text-3)" }}>
                        <XCircle size={13} /> {w.stats.failed}
                      </span>
                      {w.stats.last_ts && (
                        <span className="muted">last {new Date(w.stats.last_ts).toLocaleString()}</span>
                      )}
                    </div>
                  )}
                </div>

                <div className="row" style={{ gap: 4 }}>
                  <button
                    className="btn ghost row"
                    style={{ padding: "4px 8px", gap: 5 }}
                    disabled={busyId === w.id}
                    onClick={() => runTest(w)}
                    title={inbound ? t("wh.testTitleInbound") : t("wh.testTitleOutbound")}
                  >
                    <Send size={14} strokeWidth={1.75} /> {t("wh.test")}
                  </button>
                  <button className="btn ghost" style={{ padding: "4px 8px" }} title={t("wh.history")} onClick={() => setLogsFor(w)}>
                    <History size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn ghost" style={{ padding: "4px 8px" }} title={t("wh.edit")} onClick={() => setEdit(w)}>
                    <Pencil size={14} strokeWidth={1.75} />
                  </button>
                  <button className="btn danger" style={{ padding: "4px 8px" }} title={t("wh.remove")} onClick={() => setDel(w)}>
                    <Trash2 size={14} strokeWidth={1.75} />
                  </button>
                </div>
              </div>
            </div>
          );
        })
      )}

      {edit && (
        <WebhookModal
          agentName={agentName}
          allEvents={events}
          existing={"id" in edit ? edit : null}
          direction={edit.direction}
          onClose={() => setEdit(null)}
          onSaved={() => {
            setEdit(null);
            load();
            reload();
          }}
        />
      )}
      {logsFor && <LogsModal agentName={agentName} webhook={logsFor} onClose={() => setLogsFor(null)} />}
      {del && (
        <ConfirmModal
          title={t("wh.confirmRemove.title", { name: del.name })}
          message={t("wh.confirmRemove.msg")}
          confirmLabel={t("wh.remove")}
          danger
          busy={busyId === del.id}
          onConfirm={() => doDelete(del)}
          onClose={() => setDel(null)}
        />
      )}
    </>
  );
}

// ── Add / edit dialog ───────────────────────────────────────────────────────

function WebhookModal({
  agentName,
  allEvents,
  existing,
  direction,
  onClose,
  onSaved,
}: {
  agentName: string;
  allEvents: string[];
  existing: WebhookView | null;
  direction: Direction;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const inbound = direction === "inbound";
  const [name, setName] = useState(existing?.name ?? "");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [selected, setSelected] = useState<string[]>(existing?.events ?? ["*"]);
  const [secret, setSecret] = useState("");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [rateLimit, setRateLimit] = useState(String(existing?.rate_limit_per_minute ?? 60));
  const [maxRetries, setMaxRetries] = useState(String(existing?.max_retries ?? 2));
  const [timeout, setTimeoutS] = useState(String(existing?.timeout_seconds ?? 10));
  const [verifyTls, setVerifyTls] = useState(existing?.verify_tls ?? true);
  const [blockPrivate, setBlockPrivate] = useState(existing?.block_private_addresses ?? true);
  // inbound
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [respond, setRespond] = useState(existing?.respond ?? false);
  const [accumulate, setAccumulate] = useState(existing?.accumulate_thread ?? false);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);

  function toggleEvent(ev: string) {
    if (ev === "*") {
      setSelected(["*"]);
      return;
    }
    setSelected((cur) => {
      const without = cur.filter((e) => e !== "*");
      return without.includes(ev) ? without.filter((e) => e !== ev) : [...without, ev];
    });
  }

  function payload() {
    const body: Record<string, unknown> = {
      name: name.trim(),
      direction,
      enabled,
      rate_limit_per_minute: Number(rateLimit) || 0,
    };
    if (inbound) {
      body.prompt = prompt;
      body.respond = respond;
      body.accumulate_thread = accumulate;
    } else {
      body.url = url.trim();
      body.events = selected.length ? selected : ["*"];
      body.max_retries = Number(maxRetries) || 0;
      body.timeout_seconds = Number(timeout) || 10;
      body.verify_tls = verifyTls;
      body.block_private_addresses = blockPrivate;
      body.secret = secret ? secret : null;
    }
    return body;
  }

  async function runTest() {
    setBusy(true);
    setTestResult(null);
    try {
      const { result } = await api.post<{ result: TestResult }>(
        `/api/agents/${encodeURIComponent(agentName)}/webhooks/test`,
        payload(),
      );
      setTestResult(result);
    } catch (e) {
      toast.push(errText(e, t("wh.toast.testFailedGeneric")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    try {
      const base = `/api/agents/${encodeURIComponent(agentName)}/webhooks`;
      if (existing) {
        await api.put(`${base}/${existing.id}`, payload());
      } else {
        await api.post(base, payload());
      }
      toast.push(t("wh.toast.saved"), "success");
      onSaved();
    } catch (e) {
      toast.push(errText(e, t("wh.toast.saveFailed")), "error");
    } finally {
      setBusy(false);
    }
  }

  const canSave = inbound
    ? name.trim().length > 0
    : name.trim().length > 0 && /^https?:\/\//.test(url.trim());

  const directionWord = inbound ? t("wh.direction.inbound") : t("wh.direction.outbound");
  const title = existing
    ? t("wh.modal.titleEdit", { direction: directionWord })
    : t("wh.modal.titleAdd", { direction: directionWord });
  const subtitle = inbound ? t("wh.modal.subtitleInbound") : t("wh.modal.subtitleOutbound");

  return (
    <Modal
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      width={640}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </button>
          <button className="btn row" style={{ gap: 6 }} onClick={runTest} disabled={busy || !canSave}>
            <Send size={14} /> {t("wh.test")}
          </button>
          <button className="btn primary" onClick={save} disabled={busy || !canSave}>
            {busy ? t("wh.working") : t("vars.editor.save")}
          </button>
        </>
      }
    >
      <Field label={t("wh.field.name")}>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={inbound ? t("wh.field.namePlaceholderInbound") : t("wh.field.namePlaceholderOutbound")} />
      </Field>

      {inbound ? (
        <>
          {existing?.ingress_path && (
            <Field label={t("wh.field.ingressUrl")} hint={t("wh.field.ingressUrlHint")}>
              <input className="input" readOnly value={fullIngressUrl(existing.ingress_path)} onFocus={(e) => e.currentTarget.select()} style={{ fontFamily: "var(--font-mono)", fontSize: 12 }} />
            </Field>
          )}
          <Field
            label={t("wh.field.prompt")}
            hint={t("wh.field.promptHint")}
          >
            <textarea
              className="input"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={6}
              placeholder={t("wh.field.promptPlaceholder")}
              style={{ resize: "vertical", fontFamily: "inherit" }}
            />
          </Field>
          <div className="row" style={{ gap: 12 }}>
            <Field label={t("wh.field.rateLimit")} hint={t("wh.field.rateLimitHint")}>
              <input className="input" type="number" min={0} value={rateLimit} onChange={(e) => setRateLimit(e.target.value)} />
            </Field>
          </div>
          <div className="row" style={{ gap: 20, marginTop: 4, flexWrap: "wrap" }}>
            <label className="row" style={{ gap: 7, fontSize: 13 }}>
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> {t("wh.field.enabled")}
            </label>
            <label className="row" style={{ gap: 7, fontSize: 13 }} title={t("wh.field.replyTitle")}>
              <input type="checkbox" checked={respond} onChange={(e) => setRespond(e.target.checked)} /> {t("wh.field.reply")}
            </label>
            <label className="row" style={{ gap: 7, fontSize: 13 }} title={t("wh.field.accumulateTitle")}>
              <input type="checkbox" checked={accumulate} onChange={(e) => setAccumulate(e.target.checked)} /> {t("wh.field.accumulate")}
            </label>
          </div>
        </>
      ) : (
        <>
          <Field label={t("wh.field.url")} hint={t("wh.field.urlHint")}>
            <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={t("wh.field.urlPlaceholder")} />
          </Field>
          <Field label={t("wh.field.events")} hint={t("wh.field.eventsHint")}>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              <Chip label={t("wh.field.allEvents")} active={selected.includes("*")} onClick={() => toggleEvent("*")} />
              {allEvents.map((ev) => (
                <Chip key={ev} label={ev} active={selected.includes(ev)} onClick={() => toggleEvent(ev)} />
              ))}
            </div>
          </Field>
          <Field
            label={t("wh.field.secret")}
            hint={
              existing?.has_secret
                ? t("wh.field.secretHintSet")
                : t("wh.field.secretHintUnset")
            }
          >
            <input
              className="input"
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={existing?.has_secret ? t("wh.field.secretPlaceholderSet") : t("wh.field.secretPlaceholderUnset")}
              autoComplete="new-password"
            />
          </Field>
          <div className="row" style={{ gap: 12 }}>
            <Field label={t("wh.field.rateLimit")} hint={t("wh.field.rateLimitHint")}>
              <input className="input" type="number" min={0} value={rateLimit} onChange={(e) => setRateLimit(e.target.value)} />
            </Field>
            <Field label={t("wh.field.maxRetries")}>
              <input className="input" type="number" min={0} value={maxRetries} onChange={(e) => setMaxRetries(e.target.value)} />
            </Field>
            <Field label={t("wh.field.timeout")}>
              <input className="input" type="number" min={1} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} />
            </Field>
          </div>
          <div className="row" style={{ gap: 20, marginTop: 4, flexWrap: "wrap" }}>
            <label className="row" style={{ gap: 7, fontSize: 13 }}>
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> {t("wh.field.enabled")}
            </label>
            <label className="row" style={{ gap: 7, fontSize: 13 }}>
              <input type="checkbox" checked={verifyTls} onChange={(e) => setVerifyTls(e.target.checked)} /> {t("wh.field.verifyTls")}
            </label>
            <label className="row" style={{ gap: 7, fontSize: 13 }}>
              <input type="checkbox" checked={blockPrivate} onChange={(e) => setBlockPrivate(e.target.checked)} /> {t("wh.field.blockPrivate")}
            </label>
          </div>
        </>
      )}

      {testResult && (
        <div
          className="card"
          style={{ marginTop: 14, padding: "10px 14px", borderColor: testResult.ok ? "var(--success)" : "var(--danger)" }}
        >
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            {testResult.ok ? <CheckCircle2 size={16} color="var(--success)" /> : <XCircle size={16} color="var(--danger)" />}
            <strong style={{ fontSize: 13 }}>
              {inbound
                ? testResult.ok
                  ? t("wh.result.inboundOk")
                  : t("wh.result.failed")
                : `${testResult.ok ? t("wh.result.delivered") : t("wh.result.failed")}${testResult.status_code != null ? t("wh.result.httpStatus", { status: String(testResult.status_code) }) : ""}${testResult.duration_ms != null ? t("wh.result.durationMs", { ms: String(testResult.duration_ms) }) : ""}`}
            </strong>
          </div>
          {testResult.error && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {testResult.error}
            </div>
          )}
          {inbound && testResult.response && (
            <div style={{ marginTop: 8 }}>
              <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>{t("wh.result.agentReply")}</div>
              <pre style={{ fontSize: 11, maxHeight: 160, overflow: "auto", whiteSpace: "pre-wrap", margin: 0 }}>
                {testResult.response}
              </pre>
            </div>
          )}
          {!inbound && testResult.response_body && (
            <pre style={{ fontSize: 11, marginTop: 8, maxHeight: 120, overflow: "auto", whiteSpace: "pre-wrap", margin: "8px 0 0" }}>
              {testResult.response_body}
            </pre>
          )}
        </div>
      )}
    </Modal>
  );
}

function Chip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      className="badge"
      onClick={onClick}
      style={{
        cursor: "pointer",
        border: active ? "1px solid var(--accent)" : "1px solid var(--border)",
        color: active ? "var(--accent)" : "var(--text-3)",
        background: active ? "var(--accent-soft, transparent)" : "transparent",
      }}
    >
      {label}
    </button>
  );
}

// ── Delivery-history viewer ─────────────────────────────────────────────────

function LogsModal({
  agentName,
  webhook,
  onClose,
}: {
  agentName: string;
  webhook: WebhookView;
  onClose: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const [logs, setLogs] = useState<DeliveryLog[] | null>(null);

  useEffect(() => {
    api
      .get<{ logs: DeliveryLog[] }>(
        `/api/agents/${encodeURIComponent(agentName)}/webhooks/${webhook.id}/logs?limit=100`,
      )
      .then((r) => setLogs(r.logs || []))
      .catch((e) => toast.push(errText(e, t("wh.toast.loadFailed")), "error"));
  }, [agentName, webhook.id, toast, t]);

  const inbound = webhook.direction === "inbound";

  return (
    <Modal
      title={t("wh.logs.title", { name: webhook.name })}
      subtitle={inbound ? t("wh.logs.subtitleInbound") : webhook.url}
      onClose={onClose}
      width={720}
    >
      {logs === null ? (
        <div className="muted" style={{ padding: 20 }}>{t("wh.loading")}</div>
      ) : logs.length === 0 ? (
        <div className="muted" style={{ padding: 20 }}>
          {t("wh.logs.empty")}
        </div>
      ) : (
        <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--text-3)" }}>
              <th style={{ padding: "6px 8px" }}>{t("wh.logs.colTime")}</th>
              <th style={{ padding: "6px 8px" }}>{inbound ? t("wh.logs.colDirection") : t("wh.logs.colEvent")}</th>
              <th style={{ padding: "6px 8px" }}>{t("wh.logs.colResult")}</th>
              {!inbound && <th style={{ padding: "6px 8px" }}>{t("wh.logs.colStatus")}</th>}
              <th style={{ padding: "6px 8px" }}>{t("wh.logs.colDuration")}</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((l) => (
              <tr key={l.id} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "6px 8px", whiteSpace: "nowrap" }}>{new Date(l.ts).toLocaleString()}</td>
                <td style={{ padding: "6px 8px" }}>
                  {inbound ? (l.direction ?? t("wh.direction.inbound")) : l.event_type}
                  {l.is_test && (
                    <span className="badge" style={{ marginLeft: 6, fontSize: 10 }}>{t("wh.logs.testBadge")}</span>
                  )}
                </td>
                <td style={{ padding: "6px 8px", color: l.ok ? "var(--success)" : "var(--danger)" }}>
                  {l.ok ? t("wh.logs.ok") : (l.error ?? t("wh.logs.failedWord"))}
                </td>
                {!inbound && <td style={{ padding: "6px 8px" }}>{l.status_code ?? "—"}</td>}
                <td style={{ padding: "6px 8px" }}>{l.duration_ms != null ? `${l.duration_ms}ms` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  );
}
