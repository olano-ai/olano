// Approvals inbox, surfaced as an Overview section (formerly the standalone
// /approvals tab). Gated actions that an autonomous (scheduled/cognition) or
// delegated A2A turn deferred for a human decision land here; approving or
// rejecting resolves the blocked turn's future (security/hitl.py) via
// POST /api/hitl/resolve. The backing store + SSE wiring (approvalsStore) is
// unchanged — only the surface moved from a dedicated tab into Overview.

import { useCallback, useEffect, useState } from "react";
import { Check, X, AlertTriangle } from "lucide-react";
import { api } from "../../core/api";
import { useT, type LocalDict } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useApprovals, resolveApproval, type PendingApproval } from "../approvals/approvalsStore";

const STR: LocalDict = {
  en: {
    "title": "Approvals",
    "subtitle": "Gated actions an agent paused for a person's decision: background runs, delegated work, Gmail watch turns, and messages from people who are not the agent's owners.",
    "history.title": "Approval history",
    "history.hint": "Every approval decision, newest first: what was asked, the outcome, who decided and where. It includes actions an owner's own messages had approved without asking, and decisions answered in a WhatsApp chat. Kept permanently in audit/approvals.jsonl in the Olano home; members see only the agents they have access to.",
    "history.allAgents": "All agents",
    "history.agentFilter": "Agent",
    "history.empty": "No approval decisions recorded yet.",
    "history.loadFailed": "Could not load the approval history.",
    "history.refresh": "Refresh",
    "history.by": "by {who}",
    "history.through": "through {via}",
    "history.asked": "asked {when}",
    "history.decided": "decided {when}",
    "status.approved": "approved",
    "status.rejected": "rejected",
    "status.timed_out": "timed out",
    "status.abandoned": "abandoned",
    "status.pending": "waiting",
    "via.dashboard": "the dashboard",
    "via.trusted-sender": "an owner's message (approved without asking)",
    "via.timeout": "no answer in time",
    "via.undeliverable": "a chat prompt that could not be delivered",
    "empty": "No pending approvals.",
    "approve": "Approve",
    "reject": "Reject",
    "origin": "from {source}",
    "via": "via {path}",
    "err.resolve": "Could not submit the decision — the approval is back in the inbox.",
  },
  zh: {
    "title": "审批",
    "subtitle": "智能体暂停、等待人工决定的受控操作:后台运行、委派任务、Gmail 监视回合,以及来自非所有者的消息。",
    "history.title": "审批历史",
    "history.hint": "所有审批决定,最新的在前:请求了什么、结果、由谁决定以及在哪里决定。包括所有者本人消息未经询问就批准的操作,以及在 WhatsApp 聊天中答复的决定。永久保存在 Olano 主目录的 audit/approvals.jsonl 中;成员只能看到其有权访问的智能体。",
    "history.allAgents": "所有智能体",
    "history.agentFilter": "智能体",
    "history.empty": "尚无审批决定记录。",
    "history.loadFailed": "无法加载审批历史。",
    "history.refresh": "刷新",
    "history.by": "决定人 {who}",
    "history.through": "途径 {via}",
    "history.asked": "请求于 {when}",
    "history.decided": "决定于 {when}",
    "status.approved": "已批准",
    "status.rejected": "已拒绝",
    "status.timed_out": "已超时",
    "status.abandoned": "已放弃",
    "status.pending": "等待中",
    "via.dashboard": "仪表盘",
    "via.trusted-sender": "所有者的消息(未经询问即批准)",
    "via.timeout": "未及时答复",
    "via.undeliverable": "无法送达的聊天提示",
    "empty": "没有待处理的审批。",
    "approve": "批准",
    "reject": "拒绝",
    "origin": "来自 {source}",
    "via": "经由 {path}",
    "err.resolve": "无法提交决定——该审批已回到收件箱。",
  },
  es: {
    "title": "Aprobaciones",
    "subtitle": "Acciones controladas que un agente pausó para que una persona decida: ejecuciones en segundo plano, trabajo delegado, turnos de la vigilancia de Gmail y mensajes de quienes no son propietarios del agente.",
    "history.title": "Historial de aprobaciones",
    "history.hint": "Cada decisión de aprobación, la más reciente primero: qué se pidió, el resultado, quién decidió y dónde. Incluye las acciones que los propios mensajes de un propietario aprobaron sin preguntar y las decisiones respondidas en un chat de WhatsApp. Se guarda de forma permanente en audit/approvals.jsonl en el directorio de Olano; los miembros solo ven los agentes a los que tienen acceso.",
    "history.allAgents": "Todos los agentes",
    "history.agentFilter": "Agente",
    "history.empty": "Aún no hay decisiones de aprobación registradas.",
    "history.loadFailed": "No se pudo cargar el historial de aprobaciones.",
    "history.refresh": "Actualizar",
    "history.by": "por {who}",
    "history.through": "a través de {via}",
    "history.asked": "pedido {when}",
    "history.decided": "decidido {when}",
    "status.approved": "aprobado",
    "status.rejected": "rechazado",
    "status.timed_out": "expirado",
    "status.abandoned": "abandonado",
    "status.pending": "en espera",
    "via.dashboard": "el panel",
    "via.trusted-sender": "un mensaje de un propietario (aprobado sin preguntar)",
    "via.timeout": "sin respuesta a tiempo",
    "via.undeliverable": "un aviso de chat que no se pudo entregar",
    "empty": "No hay aprobaciones pendientes.",
    "approve": "Aprobar",
    "reject": "Rechazar",
    "origin": "de {source}",
    "via": "vía {path}",
    "err.resolve": "No se pudo enviar la decisión — la aprobación volvió a la bandeja.",
  },
};

const RISK_COLOR: Record<string, string> = {
  high: "var(--danger)",
  medium: "var(--warn)",
  low: "var(--text-3)",
};

function argsPreview(args: unknown): string {
  if (args === null || args === undefined || args === "") return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args);
  } catch {
    return String(args);
  }
}

function Row({ item, t }: { item: PendingApproval; t: (k: string, v?: Record<string, string>) => string }) {
  const args = argsPreview(item.args);
  const toast = useToast();
  // The store removes the card optimistically (and blocks double-submits), so
  // the only feedback needed here is the failure path.
  const decide = (approved: boolean) =>
    void resolveApproval(item.id, approved).catch(() => toast.push(t("err.resolve"), "error"));
  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8, padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 14, fontWeight: 600 }}>
          {item.action}
        </span>
        {item.risk && (
          <span className="badge" style={{ padding: "0 6px", color: RISK_COLOR[item.risk] || "var(--text-3)" }}>
            {item.risk}
          </span>
        )}
        <span className="muted" style={{ fontSize: 12 }}>{item.agent}</span>
        {item.root_source && (
          <span className="muted" style={{ fontSize: 12 }}>
            · {t("origin", { source: item.root_source })}
          </span>
        )}
        {item.call_path && item.call_path.length > 0 && (
          <span className="muted" style={{ fontSize: 12 }}>
            · {t("via", { path: item.call_path.join(" → ") })}
          </span>
        )}
      </div>
      {item.description && (
        <div className="muted" style={{ fontSize: 13 }}>{item.description}</div>
      )}
      {args && (
        <pre
          style={{
            margin: 0,
            fontSize: 12,
            background: "var(--surface-2)",
            padding: "6px 8px",
            borderRadius: 6,
            overflow: "auto",
            maxHeight: 120,
          }}
        >
          {args.slice(0, 800)}
        </pre>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <button
          className="btn"
          style={{ padding: "4px 12px", fontSize: 13 }}
          onClick={() => decide(false)}
        >
          <X size={14} /> {t("reject")}
        </button>
        <button
          className="btn primary"
          style={{ padding: "4px 12px", fontSize: 13 }}
          onClick={() => decide(true)}
        >
          <Check size={14} /> {t("approve")}
        </button>
      </div>
    </div>
  );
}

interface HistoryRow {
  id: string;
  agent: string;
  action: string;
  status: string;
  decided_by: string;
  via: string;
  note: string;
  args: unknown;
  requested_at?: string | null;
  decided_at?: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  approved: "var(--ok, var(--text-2))",
  rejected: "var(--danger)",
  timed_out: "var(--warn)",
  abandoned: "var(--text-3)",
  pending: "var(--text-3)",
};

function when(iso?: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "";
}

// The approval record (GET /api/hitl/history over security/hitl.py's
// append-only audit/approvals.jsonl). Collapsed by default and fetched on
// open, so the Overview page pays nothing for it; refetched whenever the
// pending inbox changes, because a decision just made belongs at the top.
function ApprovalHistory({
  t,
  pendingCount,
}: {
  t: (k: string, v?: Record<string, string>) => string;
  pendingCount: number;
}) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<HistoryRow[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [agent, setAgent] = useState("");

  const load = useCallback(() => {
    api
      .get<{ history: HistoryRow[] }>("/api/hitl/history?limit=100")
      .then((d) => {
        setRows(d.history || []);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, []);
  useEffect(() => {
    if (open) load();
  }, [open, load, pendingCount]);

  const agents = Array.from(new Set((rows || []).map((r) => r.agent).filter(Boolean))).sort();
  const shown = (rows || []).filter((r) => !agent || r.agent === agent);
  const viaLabel = (via: string) => {
    const key = `via.${via}`;
    const label = t(key);
    return label === key ? via : label;
  };

  return (
    <details
      style={{ marginTop: 16, maxWidth: 760 }}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      data-testid="approval-history"
    >
      <summary style={{ cursor: "pointer", fontSize: 14, fontWeight: 600 }}>{t("history.title")}</summary>
      <p className="muted" style={{ margin: "6px 0 10px", fontSize: 12, lineHeight: 1.5 }}>
        {t("history.hint")}
      </p>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
        <select
          className="input"
          aria-label={t("history.agentFilter")}
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          style={{ width: 200 }}
        >
          <option value="">{t("history.allAgents")}</option>
          {agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <button className="btn ghost" style={{ padding: "3px 10px", fontSize: 12 }} onClick={load}>
          {t("history.refresh")}
        </button>
      </div>
      {failed ? (
        <div className="muted" style={{ fontSize: 13 }}>{t("history.loadFailed")}</div>
      ) : rows && shown.length === 0 ? (
        <div className="muted" style={{ fontSize: 13 }}>{t("history.empty")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {shown.map((r) => {
            const raw = argsPreview(r.args);
            const args = raw === "{}" || raw === "[]" ? "" : raw;
            const note = r.note && r.note !== viaLabel(r.via) ? r.note : "";
            return (
              <div key={r.id} className="card" style={{ padding: 12, display: "flex", flexDirection: "column", gap: 4 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: 13, fontWeight: 600 }}>{r.action}</span>
                  <span className="badge" style={{ padding: "0 6px", color: STATUS_COLOR[r.status] || "var(--text-3)" }}>
                    {t(`status.${r.status}`)}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>{r.agent}</span>
                </div>
                <div className="muted" style={{ fontSize: 12, lineHeight: 1.5, overflowWrap: "anywhere" }}>
                  {[
                    r.decided_by ? t("history.by", { who: r.decided_by }) : "",
                    r.via ? t("history.through", { via: viaLabel(r.via) }) : "",
                    r.requested_at ? t("history.asked", { when: when(r.requested_at) }) : "",
                    r.decided_at ? t("history.decided", { when: when(r.decided_at) }) : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
                {note && <div className="muted" style={{ fontSize: 12 }}>{note}</div>}
                {args && (
                  <pre
                    style={{
                      margin: 0,
                      fontSize: 11.5,
                      background: "var(--surface-2)",
                      padding: "5px 8px",
                      borderRadius: 6,
                      overflow: "auto",
                      maxHeight: 90,
                    }}
                  >
                    {args.slice(0, 600)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </details>
  );
}

// Overview "Approvals" section. Always rendered (mirrors the always-on "Recent
// activity" section) with a compact empty state, so the inbox has a stable home
// now that the dedicated tab is gone. The sidebar Overview badge carries the
// live pending count app-wide.
export function ApprovalsSection() {
  const t = useT(STR);
  const pending = useApprovals();

  return (
    <section>
      <div className="row" style={{ gap: 8, marginTop: 28 }}>
        <h2 style={{ fontSize: 16, margin: 0 }}>{t("title")}</h2>
        {pending.length > 0 && <span className="badge">{pending.length}</span>}
      </div>
      <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5 }}>{t("subtitle")}</p>
      {pending.length === 0 ? (
        <div
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 8, padding: "16px 0" }}
        >
          <AlertTriangle size={15} /> {t("empty")}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12, maxWidth: 760 }}>
          {pending.map((item) => (
            <Row key={item.id} item={item} t={t} />
          ))}
        </div>
      )}
      <ApprovalHistory t={t} pendingCount={pending.length} />
    </section>
  );
}
