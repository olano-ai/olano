import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, X } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useToast } from "../../ui/toast";
import {
  describeOutcome,
  errMessage,
  formatTs,
  hasMarkdown,
  isFileRef,
  priorityColor,
  relFuture,
  statusColor,
  type Outcome,
} from "./helpers";
import { BusyButton, Empty, Markdown } from "./ui";
import { AgentTag } from "./AgentTag";
import { pulseDraft, pulseThreadId, useOpenAgentChat } from "./chatHandoff";
import { SkillPanel } from "./SkillPanel";
import { STR } from "./strings";
import type {
  ApprovePulseResponse,
  PulseItem,
  PulseListResponse,
  SelfLearningStatusResponse,
  StatusFilter,
  TabId,
} from "./types";

// Tabs that carry actionable queue items get the pending/approved/rejected
// sub-filter. Matches PULSE_ITEM_TABS.
const ITEM_TABS: TabId[] = ["pulse", "skills", "improve"];

export type PulseVerb = "approve" | "reject" | "defer" | "undo";

interface PulseTabProps {
  tab: TabId;
  lane: string;
  // The app-wide selection — the fallback owner for a row that names none
  // (an older backend). Every row the fleet endpoints return names its own.
  agent: string;
  // Agents whose rows are filtered out (the page's agents filter).
  hidden: Set<string>;
  // bumps whenever the parent wants a forced reload (e.g. after Think now / SSE).
  reloadKey: number;
  onCount: (tab: TabId, n: number) => void;
}

function fileLinkTarget(ref: string, agent: string): string {
  return `/files?path=${encodeURIComponent(ref)}&agent=${encodeURIComponent(agent)}`;
}

// The list is FLEET-WIDE: `/api/pulse` and `/api/self-learning/status` walk
// every agent the caller may see when no `agent` is given and stamp each row
// with its owner; the agents filter is applied here, on the rows, so toggling
// an agent never costs a round-trip.
export function PulseTab({ tab, lane, agent, hidden, reloadKey, onCount }: PulseTabProps) {
  const toast = useToast();
  const t = useT(STR);
  const actionable = ITEM_TABS.includes(tab);
  const [statusFilter, setStatusFilter] = usePersistentState<StatusFilter>(
    `cortex.pulse.status.${tab}`,
    "pending",
  );
  const [items, setItems] = useState<PulseItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The card whose request is out, and which button asked: that button
  // spins with what it is doing and the card dims until the answer lands.
  const [busy, setBusy] = useState<{ id: string; verb: PulseVerb | "dismiss" } | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [skill, setSkill] = useState<{ path: string; item: PulseItem } | null>(null);

  const showActions = !actionable || statusFilter === "pending";

  // `items` is the fleet as fetched; the agents filter is applied at render
  // (`visible`), so toggling an agent is instant and never refetches.
  const visible = useMemo(
    () => (items ?? []).filter((it) => !it.agent || !hidden.has(it.agent)),
    [items, hidden],
  );
  useEffect(() => {
    if (items === null) return;
    if (tab === "checks" || showActions) onCount(tab, visible.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onCount is stable
  }, [items, visible, tab, showActions]);

  // `silent` refreshes without dropping to the spinner — keeps the list (and the
  // shared <main> scroll position) in place after an approve/reject/SSE refresh,
  // instead of collapsing the page and jumping the scroll to the top.
  const load = useCallback((opts?: { silent?: boolean }) => {
    if (!opts?.silent) setItems(null);
    setError(null);
    if (tab === "checks") {
      // Checks = ops lane + self-learning failures synthesised as rows.
      Promise.all([
        api
          .get<PulseListResponse>("/api/pulse?lane=ops")
          .catch(() => ({ items: [], lane: "ops", count: 0 }) as PulseListResponse),
        api
          .get<SelfLearningStatusResponse>("/api/self-learning/status")
          .catch(() => ({ services: [], count: 0 }) as SelfLearningStatusResponse),
      ])
        .then(([ops, sl]) => {
          const issues: PulseItem[] = (sl.services || []).flatMap((svc) =>
            Object.entries(svc.failures || {}).map(([stage, count]) => ({
              id: `sl:${svc.agent}:${stage}`,
              source: "self_learning",
              lane: "ops",
              type: "issue",
              priority: "high",
              title: t("pulse.slFailure", { stage }),
              content: svc.last_failure ? svc.last_failure.join(" — ") : t("pulse.slFailureCount", { count: String(count) }),
              why_now: "",
              suggested_next_step: "",
              references: [],
              status: "pending",
              created_at: "",
              action: null,
              metadata: {},
              agent: svc.agent,
              _kind: "sl_failure",
            })),
          );
          setItems([...issues, ...(ops.items || [])]);
        })
        .catch((e) => setError(errMessage(e, t("pulse.toast.checksFailed"))));
      return;
    }

    const params = new URLSearchParams({ lane });
    if (actionable) params.set("status", statusFilter);
    api
      .get<PulseListResponse>(`/api/pulse?${params.toString()}`)
      .then((res) => {
        let list = res.items || [];
        if (tab === "skills") {
          list = list.filter(
            (it) => it.metadata?.kind === "learned_skill" || (it.title || "").toLowerCase().includes("skill"),
          );
        } else if (tab === "improve") {
          list = list.filter(
            (it) => !(it.metadata?.kind === "learned_skill" || (it.title || "").toLowerCase().includes("skill")),
          );
        }
        setItems(list);
      })
      .catch((e) => setError(errMessage(e, t("pulse.toast.itemsFailed"))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lane, tab, actionable, statusFilter]);

  // Initial load / query change shows the spinner; reloadKey bumps refresh in
  // place so the list never collapses and the scroll position holds.
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);
  const reloadedOnce = useRef(false);
  useEffect(() => {
    if (!reloadedOnce.current) {
      reloadedOnce.current = true;
      return;
    }
    load({ silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  // A card the server has settled leaves the pending list at once: it is the
  // answer to the click, so it must be visible before the silent reload
  // brings the server's own list. An undo keeps the card (it is still the
  // record of what was reverted).
  function settle(item: PulseItem) {
    if (!actionable || statusFilter !== "pending") return;
    setItems((prev) => (prev ? prev.filter((it) => it.id !== item.id) : prev));
  }

  async function act(item: PulseItem, verb: PulseVerb) {
    setBusy({ id: item.id, verb });
    try {
      if (verb === "approve") {
        const res = await api.post<ApprovePulseResponse>("/api/pulse/approve", {
          item_id: item.id,
          agent: item.agent || agent,
        });
        settle(item);
        setOutcome(describeOutcome(res || {}, t));
      } else if (verb === "reject") {
        await api.post("/api/pulse/reject", { item_id: item.id, agent: item.agent || agent });
        settle(item);
        toast.push(t("pulse.toast.rejected"), "success");
      } else if (verb === "defer") {
        await api.post("/api/pulse/defer", { item_id: item.id, agent: item.agent || agent, hours: 24 });
        settle(item);
        toast.push(t("pulse.toast.deferred"), "success");
      } else {
        await api.post("/api/pulse/undo", { item_id: item.id, agent: item.agent || agent });
        toast.push(t("pulse.toast.reverted"), "success");
      }
      load({ silent: true });
    } catch (e) {
      toast.push(errMessage(e, t("pulse.toast.actionFailed", { verb })), "error");
    } finally {
      setBusy(null);
    }
  }

  // Acknowledge & clear a self-learning failure counter (the synthetic sl_failure
  // rows on the Checks tab have no real Pulse item to reject). The row id encodes
  // the stage as `sl:<agent>:<stage>` — send it so only THIS finding clears, not
  // every sibling-stage row for the same agent.
  async function dismissFailure(item: PulseItem) {
    setBusy({ id: item.id, verb: "dismiss" });
    const stage = item.id.startsWith("sl:") ? item.id.split(":").slice(2).join(":") : undefined;
    try {
      await api.post("/api/self-learning/reset-failures", { agent: item.agent || agent, stage });
      setItems((prev) => (prev ? prev.filter((it) => it.id !== item.id) : prev));
      toast.push(t("pulse.toast.dismissed"), "success");
      load({ silent: true });
    } catch (e) {
      toast.push(errMessage(e, t("pulse.toast.actionFailed", { verb: "dismiss" })), "error");
    } finally {
      setBusy(null);
    }
  }

  const isSkills = tab === "skills";
  const isChecks = tab === "checks";

  return (
    <div>
      {/* outcome card (auto-dismiss handled below) */}
      {outcome ? <OutcomeCard outcome={outcome} agent={agent} onDismiss={() => setOutcome(null)} /> : null}

      {/* status sub-filter (item tabs only) + refresh */}
      <div className="row" style={{ marginBottom: 14, gap: 4 }}>
        {actionable
          ? (["pending", "approved", "rejected"] as StatusFilter[]).map((s) => (
              <button
                key={s}
                className={s === statusFilter ? "btn primary" : "btn"}
                style={{ fontSize: 13 }}
                onClick={() => setStatusFilter(s)}
              >
                {t(`pulse.filter.${s}`)}
              </button>
            ))
          : null}
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => load({ silent: true })}>
          {t("pulse.refresh")}
        </button>
      </div>

      {error ? (
        <Empty title={t("pulse.cannotReach")} sub={error} />
      ) : items === null ? (
        <div className="spinner" style={{ marginTop: 16 }} />
      ) : visible.length === 0 ? (
        <EmptyForTab tab={tab} statusFilter={actionable ? statusFilter : null} />
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {visible.map((item) =>
            isSkills ? (
              <SkillRow
                key={item.id}
                item={item}
                showActions={showActions}
                busy={busy && busy.id === item.id ? busy.verb : null}
                agent={agent}
                onApprove={() => void act(item, "approve")}
                onReject={() => void act(item, "reject")}
                onOpen={(path) => setSkill({ path, item })}
              />
            ) : (
              <PulseCard
                key={item.id}
                item={item}
                agent={agent}
                ops={isChecks}
                showActions={showActions}
                busy={busy && busy.id === item.id ? busy.verb : null}
                onAct={act}
                onDismissFailure={dismissFailure}
              />
            ),
          )}
        </div>
      )}

      {skill ? (
        <SkillPanel
          path={skill.path}
          agent={skill.item.agent || agent}
          itemId={showActions ? skill.item.id : null}
          itemAgent={skill.item.agent || agent}
          onClose={() => setSkill(null)}
          onApproved={() => {
            setSkill(null);
            load({ silent: true });
          }}
        />
      ) : null}
    </div>
  );
}

// ── outcome card ──────────────────────────────────────────────────────────────

function OutcomeCard({
  outcome,
  agent,
  onDismiss,
}: {
  outcome: Outcome;
  agent: string;
  onDismiss: () => void;
}) {
  const t = useT(STR);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (outcome.tone === "pending") return;
    const timer = window.setTimeout(onDismiss, 12_000);
    return () => window.clearTimeout(timer);
  }, [outcome, onDismiss]);
  // The card sits above the list, and the button that produced it may be a
  // screen below: bring the answer to where it can be seen.
  useEffect(() => {
    ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [outcome]);

  const color = outcome.tone === "pending" ? "var(--warn)" : "var(--accent)";
  return (
    <div
      ref={ref}
      className="card"
      role="status"
      data-testid="pulse-outcome"
      style={{
        borderLeft: `3px solid ${color}`,
        padding: "12px 14px",
        marginBottom: 14,
        display: "flex",
        gap: 10,
        alignItems: "flex-start",
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 13.5 }}>{outcome.heading}</div>
        {outcome.detail ? (
          <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
            {outcome.detail}
          </div>
        ) : null}
        {outcome.fileRef ? (
          <Link
            to={fileLinkTarget(outcome.fileRef, agent)}
            style={{ marginTop: 8, display: "inline-flex", alignItems: "center", gap: 4, fontFamily: "var(--font-mono)", fontSize: 12 }}
          >
            {outcome.fileRef}
            <ArrowRight size={13} strokeWidth={1.75} />
          </Link>
        ) : null}
      </div>
      <button
        className="btn"
        style={{ padding: "2px 8px", display: "inline-flex", alignItems: "center" }}
        onClick={onDismiss}
        title={t("pulse.dismiss")}
        aria-label={t("pulse.dismiss")}
      >
        <X size={14} strokeWidth={1.75} />
      </button>
    </div>
  );
}

// ── pulse card ────────────────────────────────────────────────────────────────

function PulseCard({
  item,
  agent,
  ops,
  showActions,
  busy,
  onAct,
  onDismissFailure,
}: {
  item: PulseItem;
  agent: string;
  ops: boolean;
  showActions: boolean;
  // The verb whose request is out for this card, if any.
  busy: PulseVerb | "dismiss" | null;
  onAct: (item: PulseItem, verb: PulseVerb) => Promise<void>;
  onDismissFailure: (item: PulseItem) => void;
}) {
  const t = useT(STR);
  const openChat = useOpenAgentChat();
  const resolved = item.status && !["pending", "delivered"].includes(String(item.status).toLowerCase());
  const undoable = !!item.id && item.action?.type === "restore_backup";
  const isIssue = item._kind === "sl_failure";
  const bodyDifferent = item.title && item.content && item.title !== item.content;
  const showInlineActions = showActions && !!item.id && !isIssue;

  // "Chat" / "Fix with agent": continue in the chat view on this card's own
  // thread. A Pulse card names the agent it came from, which may not be the one
  // the picker is on — the hand-off switches to it. The composer draft differs
  // by lane: an ops finding frames a fix, an ordinary card a discussion. It is
  // pre-filled, never sent — the person finishes the message.
  const goChat = () =>
    openChat(item.agent || agent, pulseThreadId(item.id), pulseDraft(t, ops ? "fix" : "chat", item));

  const locked = busy !== null;

  return (
    <div className={locked ? "card is-busy" : "card"} aria-busy={locked || undefined} style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <strong style={{ fontSize: 15 }}>{item.title || item.content || t("pulse.card.untitled")}</strong>
        {item.created_at ? (
          <span className="muted" style={{ fontSize: 11, whiteSpace: "nowrap" }} title={item.created_at}>
            {formatTs(item.created_at)}
          </span>
        ) : null}
      </div>

      {bodyDifferent ? (
        hasMarkdown(item.content) ? (
          <Markdown source={item.content} style={{ marginTop: 8 }} />
        ) : (
          <div className="muted" style={{ marginTop: 8, fontSize: 13, whiteSpace: "pre-wrap" }}>
            {item.content}
          </div>
        )
      ) : null}

      {item.why_now ? (
        <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
          <strong>{t("pulse.card.whyNow")}</strong> {item.why_now}
        </div>
      ) : null}
      {item.suggested_next_step ? (
        <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
          <strong>{t("pulse.card.next")}</strong> {item.suggested_next_step}
        </div>
      ) : null}

      <div className="row" style={{ marginTop: 12, flexWrap: "wrap", gap: 6 }}>
        <span className="badge" style={{ color: priorityColor(item.priority) }}>
          {item.priority || t("pulse.priority.normal")}
        </span>
        {item.type ? <span className="badge">{item.type}</span> : null}
        {item.source ? (
          <span className="badge" style={{ fontFamily: "var(--font-mono)" }}>
            {item.source}
          </span>
        ) : null}
        {item.agent ? <AgentTag name={item.agent} title={t("agentTag.title", { name: item.agent })} /> : null}
        {resolved ? (
          <span className="badge" style={{ color: statusColor(String(item.status)) }}>
            {item.status}
          </span>
        ) : null}
        {(item.references || []).map((r, i) =>
          isFileRef(r) ? (
            <Link
              key={i}
              to={fileLinkTarget(r, item.agent || agent)}
              className="badge"
              style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}
              title={t("pulse.openFile")}
            >
              {r}
            </Link>
          ) : (
            <span key={i} className="badge" style={{ fontSize: 10 }}>
              {r}
            </span>
          ),
        )}
        {item.expires_at ? (
          <span className="muted" style={{ fontSize: 11 }}>
            {t("pulse.expires", { when: relFuture(item.expires_at) })}
          </span>
        ) : null}
      </div>

      {ops ? (
        // Self-Checks: findings ARE actionable — let the agent fix them (it now
        // has shell access) or dismiss the row once acknowledged.
        item.id ? (
          <div className="row" style={{ marginTop: 12, gap: 6, flexWrap: "wrap" }}>
            <button className="btn primary" disabled={locked} onClick={goChat} title={t("pulse.fixWithAgentHint")}>
              {t("pulse.fixWithAgent")}
            </button>
            <BusyButton
              className="btn danger"
              busy={busy === "dismiss" || busy === "reject"}
              busyLabel={t("pulse.busy.dismiss")}
              disabled={locked}
              onClick={() => (isIssue ? onDismissFailure(item) : void onAct(item, "reject"))}
            >
              {t("pulse.dismissIssue")}
            </BusyButton>
          </div>
        ) : null
      ) : (showInlineActions || undoable || item.id) ? (
        <div className="row" style={{ marginTop: 12, gap: 6, flexWrap: "wrap" }}>
          {showInlineActions ? (
            <>
              <BusyButton
                className="btn primary"
                busy={busy === "approve"}
                busyLabel={t(undoable ? "pulse.busy.keep" : "pulse.busy.approve")}
                disabled={locked}
                onClick={() => void onAct(item, "approve")}
              >
                {t(undoable ? "pulse.keep" : "pulse.approve")}
              </BusyButton>
              <BusyButton
                className="btn"
                busy={busy === "defer"}
                busyLabel={t("pulse.busy.defer")}
                disabled={locked}
                onClick={() => void onAct(item, "defer")}
              >
                {t("pulse.defer")}
              </BusyButton>
              {/* Work already applied is reverted with Undo below, not "rejected". */}
              {!undoable ? (
                <BusyButton
                  className="btn danger"
                  busy={busy === "reject"}
                  busyLabel={t("pulse.busy.reject")}
                  disabled={locked}
                  onClick={() => void onAct(item, "reject")}
                >
                  {t("pulse.reject")}
                </BusyButton>
              ) : null}
            </>
          ) : null}
          {undoable ? (
            <BusyButton
              className="btn danger"
              busy={busy === "undo"}
              busyLabel={t("pulse.busy.undo")}
              disabled={locked}
              onClick={() => void onAct(item, "undo")}
              title={t("pulse.undoHint")}
            >
              {t("pulse.undo")}
            </BusyButton>
          ) : null}
          {item.id && !isIssue ? (
            <button className="btn" disabled={locked} onClick={goChat} title={t("pulse.chatHint")}>
              {t("pulse.chat")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ── skill row ─────────────────────────────────────────────────────────────────

function SkillRow({
  item,
  showActions,
  busy,
  agent,
  onApprove,
  onReject,
  onOpen,
}: {
  item: PulseItem;
  showActions: boolean;
  busy: PulseVerb | "dismiss" | null;
  agent: string;
  onApprove: () => void;
  onReject: () => void;
  onOpen: (path: string) => void;
}) {
  const t = useT(STR);
  const primaryRef = (item.references || []).find((r) => isFileRef(r)) || "";
  const bodyDifferent = item.content && item.title !== item.content;
  const locked = busy !== null;
  return (
    <div
      className={locked ? "card is-busy" : "card"}
      aria-busy={locked || undefined}
      style={{
        padding: 16,
        borderLeft: "3px solid var(--accent)",
        cursor: primaryRef ? "pointer" : "default",
      }}
      onClick={primaryRef ? () => onOpen(primaryRef) : undefined}
      title={primaryRef ? t("skill.openTitle") : ""}
    >
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <strong style={{ fontSize: 14 }}>{item.title || t("skill.untitled")}</strong>
        <span className="row" style={{ gap: 8, flexShrink: 0 }}>
          {item.agent ? <AgentTag name={item.agent} title={t("agentTag.title", { name: item.agent })} /> : null}
          {item.created_at ? (
            <span className="muted" style={{ fontSize: 11 }} title={item.created_at}>
              {formatTs(item.created_at)}
            </span>
          ) : null}
        </span>
      </div>
      {bodyDifferent ? (
        hasMarkdown(item.content) ? (
          <Markdown source={item.content} style={{ marginTop: 6 }} />
        ) : (
          <div className="muted" style={{ marginTop: 6, fontSize: 13, whiteSpace: "pre-wrap" }}>
            {item.content}
          </div>
        )
      ) : null}
      {item.suggested_next_step ? (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          <strong>{t("skill.next")}</strong> {item.suggested_next_step}
        </div>
      ) : null}
      {(item.references || []).length ? (
        <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 4 }}>
          {(item.references || []).map((r, i) =>
            isFileRef(r) ? (
              <Link
                key={i}
                to={fileLinkTarget(r, item.agent || agent)}
                className="badge"
                style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}
                onClick={(e) => e.stopPropagation()}
              >
                {r}
              </Link>
            ) : (
              <span key={i} className="badge" style={{ fontSize: 10 }}>
                {r}
              </span>
            ),
          )}
        </div>
      ) : null}
      <div className="row" style={{ marginTop: 12, gap: 6 }} onClick={(e) => e.stopPropagation()}>
        {showActions ? (
          <>
            <BusyButton
              className="btn primary"
              busy={busy === "approve"}
              busyLabel={t("pulse.busy.keep")}
              disabled={locked}
              onClick={onApprove}
              title={t("skill.keep.title")}
            >
              {t("skill.keep")}
            </BusyButton>
            <BusyButton
              className="btn danger"
              busy={busy === "reject"}
              busyLabel={t("items.busy.discard")}
              disabled={locked}
              onClick={onReject}
              title={t("skill.discard.title")}
            >
              {t("skill.discard")}
            </BusyButton>
          </>
        ) : (
          <span className="badge" style={{ color: statusColor(String(item.status)) }}>
            {item.status}
          </span>
        )}
      </div>
    </div>
  );
}

// ── per-tab empty states ──────────────────────────────────────────────────────

function EmptyForTab({ tab, statusFilter }: { tab: TabId; statusFilter: StatusFilter | null }) {
  const t = useT(STR);
  if (statusFilter === "approved")
    return <Empty title={t("empty.approved.title")} sub={t("empty.approved.sub")} />;
  if (statusFilter === "rejected")
    return <Empty title={t("empty.rejected.title")} sub={t("empty.rejected.sub")} />;
  if (tab === "skills") return <Empty title={t("empty.skills.title")} sub={t("empty.skills.sub")} />;
  if (tab === "checks") return <Empty title={t("empty.checks.title")} sub={t("empty.checks.sub")} />;
  if (tab === "improve") return <Empty title={t("empty.improve.title")} sub={t("empty.improve.sub")} />;
  return <Empty title={t("empty.lane.title")} sub={t("empty.lane.sub")} />;
}
