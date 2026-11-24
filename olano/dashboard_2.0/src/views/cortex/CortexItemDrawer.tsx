import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { errMessage, relPast, statusColor } from "./helpers";
import { BusyButton, Drawer, Empty, Markdown, ProgressBar } from "./ui";
import { AgentTag } from "./AgentTag";
import { cortexItemDraft, cortexThreadId, useOpenAgentChat } from "./chatHandoff";
import { STR } from "./strings";
import type { CortexItemDetailResponse, CortexJob, CortexJobRef } from "./types";

// What the drawer can do to its item, handed in by the list that owns the
// item's state: the same confirm, request, busy state and job as the card's
// buttons, so acting from the drawer and from the card can never differ. The
// drawer used to offer only Chat and Close, and a person reading an item had
// to close it and find its card again to create the project or discard it.
export interface DrawerActions {
  // The verb whose request is out for this item, if any.
  busy: "discard" | "promote" | null;
  // The item's running background run, if any: the button follows it.
  job: CortexJob | CortexJobRef | null;
  canPromote: boolean;
  canDiscard: boolean;
  onPromote: () => void;
  onDiscard: () => void;
  onOpenJob: (job: CortexJob | CortexJobRef) => void;
}

export function CortexItemDrawer({
  agent,
  itemId,
  onClose,
  actions,
}: {
  agent: string;
  itemId: string;
  onClose: () => void;
  actions?: DrawerActions;
}) {
  const t = useT(STR);
  const openChat = useOpenAgentChat();
  const [detail, setDetail] = useState<CortexItemDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    api
      .get<CortexItemDetailResponse>(
        `/api/cortex/items/${encodeURIComponent(itemId)}?agent=${encodeURIComponent(agent)}`,
      )
      .then(setDetail)
      .catch((e) => setError(errMessage(e, t("drawer.toast.detailFailed"))));
  }, [agent, itemId, t]);

  useEffect(load, [load]);

  const item = detail?.item;
  const progress = detail?.progress ?? null;
  const isProject = item?.kind === "project";
  const isPromoted = item?.status === "promoted";
  const job = actions?.job ?? null;
  const busy = actions?.busy ?? null;
  const locked = busy !== null || !!job;
  const jobLabel = job?.kind === "explore" ? t("items.exploringNow") : t("items.creatingProject");

  // "Chat" and "Explore more" both hand the item's own thread to the chat view
  // and close the drawer — the conversation belongs on a full page, not in a
  // 560px panel. The composer is pre-filled with what the button asks for
  // (never sent); Explore more used to run a background worker from here.
  function goChat(intent: "chat" | "explore") {
    if (!item) return;
    openChat(agent, cortexThreadId(item.id), cortexItemDraft(t, intent, item));
    onClose();
  }

  return (
    <Drawer
      title={item?.title ?? t("drawer.loading")}
      sub={
        item
          ? t("drawer.sub", { kind: item.kind, status: item.status, when: relPast(item.updated_at) })
          : undefined
      }
      onClose={onClose}
      width={560}
      footer={
        item ? (
          <>
            {actions?.canDiscard ? (
              <BusyButton
                className="btn danger"
                data-testid="cortex-drawer-discard"
                busy={busy === "discard"}
                busyLabel={t("items.busy.discard")}
                disabled={locked}
                onClick={actions.onDiscard}
                style={{ marginRight: "auto" }}
              >
                {t("items.discard")}
              </BusyButton>
            ) : null}
            {!isProject && !isPromoted ? (
              <button
                className="btn"
                onClick={() => goChat("explore")}
                title={t("drawer.exploreHint")}
                disabled={locked}
                style={actions?.canDiscard ? undefined : { marginRight: "auto" }}
              >
                {t("drawer.exploreMore")}
              </button>
            ) : null}
            <button className="btn" onClick={() => goChat("chat")} title={t("drawer.chatHint")} disabled={locked}>
              {t("drawer.chat")}
            </button>
            {job ? (
              // The run is going: this follows it, it never starts another.
              <button
                className="btn primary"
                data-testid="cortex-drawer-job-button"
                title={t("items.job.runningTitle")}
                onClick={() => actions?.onOpenJob(job)}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                <span
                  className="spinner"
                  style={{ width: 12, height: 12, borderWidth: 1.5, borderColor: "color-mix(in srgb, var(--accent-fg) 35%, transparent)", borderTopColor: "var(--accent-fg)" }}
                />
                {jobLabel}
              </button>
            ) : actions?.canPromote ? (
              <BusyButton
                className="btn primary"
                data-testid="cortex-drawer-promote"
                busy={busy === "promote"}
                busyLabel={t("items.busy.promote")}
                disabled={locked}
                onClick={actions.onPromote}
              >
                {t("items.createProject")}
              </BusyButton>
            ) : null}
            <button className={job || actions?.canPromote ? "btn" : "btn primary"} onClick={onClose}>
              {t("drawer.close")}
            </button>
          </>
        ) : null
      }
    >
      {error ? (
        <Empty title={t("drawer.loadFailedTitle")} sub={error} />
      ) : !detail || !item ? (
        <div className="spinner" />
      ) : (
        <>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <AgentTag name={agent} title={t("agentTag.title", { name: agent })} />
            <span className="badge">{item.kind}</span>
            {job ? (
              <span
                className="badge"
                data-testid="cortex-drawer-job"
                title={t("items.job.runningTitle")}
                style={{ color: "var(--accent)", display: "inline-flex", alignItems: "center", gap: 5 }}
              >
                <span className="spinner" style={{ width: 10, height: 10, borderWidth: 1.5 }} />
                {jobLabel}
              </span>
            ) : (
              <span className="badge" style={{ color: statusColor(item.status) }}>
                {item.status}
              </span>
            )}
            {typeof item.rating?.score === "number" ? (
              <span className="badge" data-testid="cortex-rating">
                {item.rating.score}/10 · {item.rating.recommendation || "keep"}
              </span>
            ) : null}
          </div>

          {item.rating && typeof item.rating.score === "number" ? (
            <div className="muted" style={{ marginTop: 10, fontSize: 12, lineHeight: 1.5 }}>
              {item.rating.rationale ? <div>{item.rating.rationale}</div> : null}
              {item.rating.next_step ? (
                <div>
                  <strong>{t("drawer.nextStep")}</strong> {item.rating.next_step}
                </div>
              ) : null}
            </div>
          ) : null}

          {isProject && progress && progress.total ? (
            <div style={{ marginTop: 14 }}>
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                <span className="muted" style={{ fontSize: 12 }}>
                  {t("drawer.planProgress")}
                </span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {progress.done} / {progress.total} ({progress.pct}%)
                </span>
              </div>
              <ProgressBar pct={progress.pct} />
            </div>
          ) : null}

          <div style={{ marginTop: 16 }}>
            <Markdown source={detail.detail || t("drawer.noDetail")} />
          </div>

          {detail.files && detail.files.length ? (
            <div style={{ marginTop: 16 }}>
              <div className="muted" style={{ fontSize: 12, marginBottom: 6, fontWeight: 600 }}>
                {t("drawer.files")}
              </div>
              <div style={{ display: "grid", gap: 2 }}>
                {detail.files.map((f) => (
                  <Link
                    key={f}
                    to={`/files?path=${encodeURIComponent(f)}&agent=${encodeURIComponent(agent)}`}
                    style={{ fontFamily: "var(--font-mono)", fontSize: 11, wordBreak: "break-all" }}
                  >
                    {f}
                  </Link>
                ))}
              </div>
            </div>
          ) : null}
        </>
      )}
    </Drawer>
  );
}
