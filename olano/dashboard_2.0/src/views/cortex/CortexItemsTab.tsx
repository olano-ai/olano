import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ChevronRight,
  Compass,
  CornerDownRight,
  Lightbulb,
  Package,
  Sparkles,
  FolderOpen,
  type LucideIcon,
} from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { usePersistentState } from "../../core/usePersistentState";
import { useUrlUpdate } from "../../core/useUrlParam";
import { useToast } from "../../ui/toast";
import { errMessage, relPast, statusColor } from "./helpers";
import { BusyButton, ConfirmModal, Empty } from "./ui";
import { AgentTag } from "./AgentTag";
import { CortexItemDrawer, type DrawerActions } from "./CortexItemDrawer";
import { cortexItemDraft, cortexThreadId, useOpenAgentChat } from "./chatHandoff";
import { STR } from "./strings";
import { startCortexJob, useCortexJobs } from "./cortexJobs";
import { formatDuration, useElapsed } from "./JobDrawer";
import type { CortexItem, CortexItemsListResponse, CortexJob, CortexJobRef, TabId } from "./types";

// Status sub-groups shown as a sub-filter within each Cortex section.
type StatusGroup = "active" | "promoted" | "discarded";
const STATUS_GROUPS: StatusGroup[] = ["active", "promoted", "discarded"];
const IN_GROUP: Record<StatusGroup, (s: string) => boolean> = {
  active: (s) => s === "active" || s === "exploring",
  promoted: (s) => s === "promoted",
  discarded: (s) => s === "discarded",
};

// Per-kind empty-state translation key.
const EMPTY_KEY: Record<string, string> = {
  idea: "items.empty.idea",
  brainstorm: "items.empty.brainstorm",
  prototype: "items.empty.prototype",
  project: "items.empty.project",
};

// Per-kind icon for the card header (mirrors _CORTEX_ICON in the original).
const KIND_ICON: Record<string, LucideIcon> = {
  idea: Lightbulb,
  brainstorm: Sparkles,
  prototype: Package,
  project: FolderOpen,
};

type Kind = "idea" | "brainstorm" | "prototype" | "project";

interface ConfirmState {
  verb: "discard" | "promote";
  item: CortexItem;
}

// A card's run ended badly within the hour: the card says so.
const FAILED_JOB = new Set(["error", "stopped", "interrupted"]);

// The one-line live status under a card whose run is in progress: how long it
// has been going and the step it is on.
function CardJobLine({ job }: { job: CortexJob | CortexJobRef }) {
  const elapsed = useElapsed(job.started_at, true, job.duration_seconds ?? 0);
  const step = job.last_step;
  return (
    <div
      className="muted"
      data-testid="cortex-card-job-step"
      style={{ marginTop: 8, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
    >
      {formatDuration(elapsed)}
      {step ? ` · ${step.tool ? `${step.tool}: ` : ""}${step.text.replace(/\s+/g, " ")}` : ""}
    </div>
  );
}

// The list is FLEET-WIDE (`/api/cortex/items?all_agents=true`): every agent the
// caller may see, each item stamped with its owner. The agents filter is
// applied here, on the rows, so toggling an agent never costs a round-trip,
// and every action on a card — discard, promote, the chat hand-off, the
// Files links — goes to the card's own agent, never the picker's.
export function CortexItemsTab({
  tab,
  kind,
  agent,
  hidden,
  reloadKey,
  overCap,
  onCount,
  onCountsChanged,
}: {
  tab: TabId;
  kind: Kind;
  // The app-wide selection — the fallback owner for an item that names none
  // (an older backend).
  agent: string;
  // Agents whose items are filtered out (the page's agents filter).
  hidden: Set<string>;
  reloadKey: number;
  // Today's deep-run budget is exhausted — gate the worker-spawning action
  // (promote) behind a confirmation. Explore more is a chat turn, not a worker.
  overCap: boolean;
  onCount: (tab: TabId, n: number) => void;
  // Refresh all tab counters (promote/discard change cross-tab totals).
  onCountsChanged: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const openChat = useOpenAgentChat();
  // Background runs (Create project) are server-side Cortex jobs: a card reads
  // its state from here, so it survives closing the panel, a reload, another
  // tab, and its button follows the running job instead of starting another.
  const jobs = useCortexJobs();
  const [items, setItems] = useState<CortexItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The open item drawer is the URL's `?item=<id>&owner=<agent>` (the owner
  // rides along because the detail endpoint is per agent), so a reload or a
  // copied link reopens it, opening one is a Back step and Back closes it.
  // It is also remembered per kind, so a left-nav round-trip (a bare
  // /cortex/<agent>/<tab>) still re-opens the detail you were in.
  const [params] = useSearchParams();
  const updateUrl = useUrlUpdate();
  const urlItem = params.get("item");
  const urlOwner = params.get("owner");
  const open = useMemo(
    () => (urlItem ? { agent: urlOwner || agent, id: urlItem } : null),
    [urlItem, urlOwner, agent],
  );
  const [remembered, setRemembered] = usePersistentState<{ agent: string; id: string } | null>(
    `cortex.items.open.${kind}`,
    null,
  );
  const arrivedRef = useRef(false);
  useEffect(() => {
    if (arrivedRef.current) return;
    arrivedRef.current = true;
    if (!urlItem && remembered) updateUrl({ item: remembered.id, owner: remembered.agent }, { history: "replace" });
    // Arrival only: after this the URL alone decides.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (arrivedRef.current) setRemembered(open);
  }, [open, setRemembered]);
  const setOpen = useCallback(
    (next: { agent: string; id: string } | null) =>
      updateUrl({ item: next?.id ?? null, owner: next?.agent ?? null }, { history: "push" }),
    [updateUrl],
  );
  const ownerOf = (it: CortexItem) => it.agent || agent;
  // The card whose request is out, and which button asked: that button
  // spins with what it is doing and the card dims until the answer lands.
  const [busy, setBusy] = useState<{ id: string; verb: ConfirmState["verb"] } | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  // Active/promoted/discarded sub-filter, persisted per kind across nav.
  const [group, setGroup] = usePersistentState<StatusGroup>(`cortex.items.group.${kind}`, "active");

  // `silent` keeps the current list mounted while refreshing. Resetting to the
  // spinner collapses the page, which shrinks the shared <main> scroller and
  // makes the browser clamp scrollTop to the top — the jump you see after a
  // discard. A silent refresh swaps the data in place, so the scroll holds.
  const load = useCallback(
    (opts?: { silent?: boolean }) => {
      if (!opts?.silent) setItems(null);
      setError(null);
      api
        .get<CortexItemsListResponse>(`/api/cortex/items?kind=${kind}&include_discarded=true&all_agents=true`)
        .then((res) => setItems(res.items || []))
        .catch((e) => setError(errMessage(e, t("items.toast.loadFailed"))));
      // The cards read their run state from the jobs store; bring it up to
      // the same moment as the list so the two never disagree for a poll.
      jobs.refresh();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [kind, tab],
  );

  // `items` is the fleet as fetched; the agents filter is applied here, at
  // render, so toggling an agent is instant and never refetches.
  const visibleItems = useMemo(
    () => (items ?? []).filter((it) => !it.agent || !hidden.has(it.agent)),
    [items, hidden],
  );
  // Badge counts the active (non-discarded) work, regardless of sub-filter.
  useEffect(() => {
    if (items === null) return;
    onCount(tab, visibleItems.filter((it) => it.status !== "discarded").length);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onCount is stable
  }, [items, visibleItems, tab]);

  // Initial load shows the spinner. reloadKey bumps (SSE / Think-now / Refresh)
  // refresh in place so the list never collapses and the scroll position holds.
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

  // A project is never "promoted" (it IS the promotion target) — hide that chip.
  const visibleGroups: StatusGroup[] = kind === "project" ? ["active", "discarded"] : STATUS_GROUPS;
  // Tolerate a stale persisted value (e.g. "promoted" carried onto the Projects tab).
  const effectiveGroup: StatusGroup = visibleGroups.includes(group) ? group : "active";

  // Counts per sub-group (for the filter chips) + the filtered list to render.
  const groupCounts: Record<StatusGroup, number> = {
    active: visibleItems.filter((it) => IN_GROUP.active(it.status)).length,
    promoted: visibleItems.filter((it) => IN_GROUP.promoted(it.status)).length,
    discarded: visibleItems.filter((it) => IN_GROUP.discarded(it.status)).length,
  };
  const shown = visibleItems.filter((it) => IN_GROUP[effectiveGroup](it.status));

  async function discard(item: CortexItem) {
    setBusy({ id: item.id, verb: "discard" });
    try {
      await api.post(`/api/cortex/items/${encodeURIComponent(item.id)}/discard`, { agent: ownerOf(item) });
      // The card leaves the Active list the moment the server agrees, not a
      // refresh later: it is the answer to the click, so it must be visible
      // at once. The silent reload then brings the server's own row.
      setItems((prev) =>
        prev ? prev.map((it) => (it.id === item.id && ownerOf(it) === ownerOf(item) ? { ...it, status: "discarded" } : it)) : prev,
      );
      toast.push(t("items.toast.discarded"), "success");
      // Discarded from its own drawer: the drawer's item is gone from the
      // Active list, so the drawer goes with it.
      if (open && open.id === item.id && open.agent === ownerOf(item)) setOpen(null);
      load({ silent: true });
      onCountsChanged();
    } catch (e) {
      toast.push(errMessage(e, t("items.toast.discardFailed")), "error");
    } finally {
      setBusy(null);
    }
  }

  // Every action that asks the agent to do work goes to the chat view on the
  // item's own thread, with the composer pre-filled (never sent) so the person
  // says what they want. "Explore more" used to spawn a background worker whose
  // result arrived as a toast; the same research now happens in a conversation
  // the person can steer.
  function goChat(item: CortexItem, intent: "chat" | "explore" | "run") {
    // The card's own agent: the hand-off switches the picker to it.
    openChat(ownerOf(item), cortexThreadId(item.id), cortexItemDraft(t, intent, item));
  }

  // Discard and promote confirm first (promote also names the spent budget).
  function startAction(verb: ConfirmState["verb"], item: CortexItem) {
    setConfirm({ verb, item });
  }

  // The item's running Create project / Explore job, if any. The live store
  // is the answer once it has one (it moves the moment a run starts or ends;
  // the list's stamp is what the server knew when the list was fetched and
  // would keep a card on "Creating project..." after the run ended). Before
  // the store's first answer, the stamp is the best information there is.
  function runningJob(it: CortexItem): CortexJob | CortexJobRef | null {
    const owner = ownerOf(it);
    const live = jobs.liveFor(owner, "promote", it.id) ?? jobs.liveFor(owner, "explore", it.id);
    if (live) return live;
    if (!jobs.ready && it.job?.status === "running") return it.job;
    return null;
  }

  // Create project runs in the background on the server. The request answers
  // at once with the job (or the job already running for this item: a second
  // tap never starts a second promotion), the panel that opens is a view of
  // it, and closing that panel leaves the run and the card's progress going.
  async function runPromote(item: CortexItem) {
    const owner = ownerOf(item);
    setBusy({ id: item.id, verb: "promote" });
    try {
      const { job, alreadyRunning } = await startCortexJob(
        `/api/cortex/items/${encodeURIComponent(item.id)}/promote`,
        { agent: owner },
      );
      jobs.track(job);
      // The run's panel takes over from the item's drawer.
      if (open && open.id === item.id && open.agent === owner) setOpen(null);
      jobs.openJob({ id: job.id, agent: owner });
      toast.push(t(alreadyRunning ? "items.toast.alreadyRunning" : "items.toast.projectStarted"));
    } catch (e) {
      toast.push(errMessage(e, t("items.toast.promoteFailed")), "error");
      load({ silent: true });
      onCountsChanged();
    } finally {
      setBusy(null);
    }
  }

  // The drawer acts through the same confirm, request, busy state and job as
  // the item's card, read off the list's own row. An item the list does not
  // hold (an older link, a row of a hidden agent) gets no actions.
  function drawerActions(target: { agent: string; id: string }): DrawerActions | undefined {
    const row = (items ?? []).find((it) => it.id === target.id && ownerOf(it) === target.agent);
    if (!row) return undefined;
    const settled = row.status === "promoted" || row.status === "discarded";
    return {
      busy: busy && busy.id === row.id ? busy.verb : null,
      job: runningJob(row),
      canPromote: kind !== "project" && !settled,
      canDiscard: !settled,
      onPromote: () => startAction("promote", row),
      onDiscard: () => startAction("discard", row),
      onOpenJob: (job) => jobs.openJob({ id: job.id, agent: target.agent }),
    };
  }

  return (
    <div>
      <div className="row" style={{ marginBottom: 14, gap: 4, alignItems: "center" }}>
        {/* status sub-groups (active / promoted / discarded) */}
        {visibleGroups.map((g) => (
          <button
            key={g}
            className={g === effectiveGroup ? "btn primary" : "btn"}
            style={{ fontSize: 13 }}
            onClick={() => setGroup(g)}
          >
            {t(`items.group.${g}`)}
            {groupCounts[g] ? (
              <span
                className="badge"
                style={{ marginLeft: 4, ...(g === effectiveGroup ? { background: "color-mix(in srgb, var(--accent-fg) 25%, transparent)", color: "var(--accent-fg)" } : {}) }}
              >
                {groupCounts[g]}
              </span>
            ) : null}
          </button>
        ))}
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => load({ silent: true })}>
          {t("items.refresh")}
        </button>
      </div>

      {error ? (
        <Empty title={t("drawer.loadFailedTitle")} sub={error} />
      ) : items === null ? (
        <div className="spinner" style={{ marginTop: 16 }} />
      ) : shown.length === 0 ? (
        visibleItems.length === 0 ? (
          <Empty title={EMPTY_KEY[kind] ? t(EMPTY_KEY[kind]) : t("items.empty.fallback")} />
        ) : (
          <Empty title={t(`items.group.empty.${effectiveGroup}`)} />
        )
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {shown.map((it) => {
            const promoted = it.status === "promoted";
            const discarded = it.status === "discarded";
            const job = runningJob(it);
            const waiting = busy && busy.id === it.id ? busy.verb : null;
            const locked = waiting !== null || !!job;
            const desc = it.summary || it.preview || "";
            const owner = ownerOf(it);
            const lastFailed =
              !job && !promoted && it.job && FAILED_JOB.has(it.job.status) ? it.job : null;
            const jobLabel = job?.kind === "explore" ? t("items.exploringNow") : t("items.creatingProject");
            return (
              <div
                className={waiting ? "card cortex-card is-busy" : "card cortex-card"}
                key={`${owner}:${it.id}`}
                data-agent={owner}
                aria-busy={waiting ? true : undefined}
                onClick={() => setOpen({ agent: owner, id: it.id })}
                title={t("items.cardOpen")}
                style={{ padding: 16, display: "flex", flexDirection: "column", cursor: "pointer" }}
              >
                <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                  <div className="row" style={{ gap: 7, alignItems: "flex-start", minWidth: 0 }}>
                    <span aria-hidden style={{ flexShrink: 0, color: "var(--accent)", display: "inline-flex", paddingTop: 1 }}>
                      {(() => {
                        const Glyph = KIND_ICON[it.kind] || Package;
                        return <Glyph size={15} strokeWidth={1.75} />;
                      })()}
                    </span>
                    <strong style={{ fontSize: 14 }}>{it.title}</strong>
                  </div>
                  <span className="row" style={{ gap: 6, flexShrink: 0 }}>
                    <AgentTag name={owner} title={t("agentTag.title", { name: owner })} />
                    {typeof it.rating?.score === "number" ? (
                      <span
                        className="badge"
                        data-testid="cortex-rating"
                        title={
                          t("items.ratingHint", {
                            recommendation: it.rating.recommendation || "keep",
                          }) + (it.rating.rationale ? `: ${it.rating.rationale}` : "")
                        }
                      >
                        {it.rating.score}/10
                      </span>
                    ) : null}
                    {job ? (
                      <span
                        className="badge"
                        data-testid="cortex-card-job"
                        title={t("items.job.runningTitle")}
                        style={{ color: "var(--accent)", display: "inline-flex", alignItems: "center", gap: 5 }}
                      >
                        <span className="spinner" style={{ width: 10, height: 10, borderWidth: 1.5 }} />
                        {jobLabel}
                      </span>
                    ) : (
                      <span className="badge" style={{ color: statusColor(it.status) }}>
                        {it.status}
                      </span>
                    )}
                  </span>
                </div>

                <div
                  className="muted"
                  style={{
                    marginTop: 6,
                    fontSize: 13,
                    lineHeight: 1.5,
                    fontStyle: desc ? undefined : "italic",
                    display: "-webkit-box",
                    WebkitLineClamp: 3,
                    WebkitBoxOrient: "vertical",
                    overflow: "hidden",
                  }}
                >
                  {desc || t("items.noDescription")}
                </div>

                <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 11 }}>{relPast(it.updated_at)}</span>
                  {it.source ? (
                    <span className="badge" style={{ fontSize: 10 }}>{it.source}</span>
                  ) : null}
                  {(it.tags || []).slice(0, 4).map((tag) => (
                    <span key={tag} className="badge" style={{ fontSize: 10 }}>
                      {tag}
                    </span>
                  ))}
                </div>

                {job ? <CardJobLine job={job} /> : null}

                {lastFailed ? (
                  <div
                    data-testid="cortex-card-job-failed"
                    style={{ marginTop: 8, fontSize: 12, color: "var(--danger)", display: "flex", gap: 6, alignItems: "flex-start" }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <AlertTriangle size={13} strokeWidth={1.75} style={{ flexShrink: 0, marginTop: 1 }} />
                    <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                      {t("items.job.lastFailed", {
                        status: t(`jobs.status.${lastFailed.status}`).toLowerCase(),
                        error: (lastFailed.error || "").slice(0, 180),
                      })}{" "}
                      <button
                        type="button"
                        className="link-button"
                        style={{ background: "none", border: "none", padding: 0, color: "var(--accent)", cursor: "pointer", font: "inherit" }}
                        onClick={() => jobs.openJob({ id: lastFailed.id, agent: owner })}
                      >
                        {t("items.job.viewLog")}
                      </button>
                    </span>
                  </div>
                ) : null}

                {promoted && it.links?.promoted_to ? (
                  <div className="muted" style={{ marginTop: 8, fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
                    <CornerDownRight size={13} strokeWidth={1.75} />
                    <Link
                      to={`/files?path=${encodeURIComponent(it.links.promoted_to)}&agent=${encodeURIComponent(owner)}`}
                      style={{ fontFamily: "var(--font-mono)" }}
                      title={t("items.openProjectFolder")}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {it.links.promoted_to}
                    </Link>
                  </div>
                ) : null}

                <div
                  className="row"
                  style={{ marginTop: 12, flexWrap: "wrap", gap: 6, alignItems: "center" }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button className="btn" onClick={() => setOpen({ agent: owner, id: it.id })}>
                    {t("items.open")}
                  </button>
                  {job ? (
                    // The run is going: this follows it, it never starts another.
                    <button
                      className="btn primary"
                      data-testid="cortex-card-job-button"
                      title={t("items.job.runningTitle")}
                      onClick={() => jobs.openJob({ id: job.id, agent: owner })}
                      style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                    >
                      <span
                        className="spinner"
                        style={{ width: 12, height: 12, borderWidth: 1.5, borderColor: "color-mix(in srgb, var(--accent-fg) 35%, transparent)", borderTopColor: "var(--accent-fg)" }}
                      />
                      {jobLabel}
                    </button>
                  ) : kind !== "project" && !promoted && !discarded ? (
                    <BusyButton
                      className="btn primary"
                      busy={waiting === "promote"}
                      busyLabel={t("items.busy.promote")}
                      disabled={locked}
                      onClick={() => startAction("promote", it)}
                    >
                      {t("items.createProject")}
                    </BusyButton>
                  ) : null}
                  {kind !== "project" && !promoted && !discarded ? (
                    <button className="btn" title={t("items.exploreHint")} onClick={() => goChat(it, "explore")}>
                      <Compass size={13} strokeWidth={1.75} style={{ marginRight: 4, verticalAlign: "-2px" }} />
                      {t("items.explore")}
                    </button>
                  ) : null}
                  {kind === "prototype" && !discarded ? (
                    <button className="btn" title={t("items.sendToAgentHint")} onClick={() => goChat(it, "run")}>
                      {t("items.sendToAgent")}
                    </button>
                  ) : null}
                  <button className="btn" title={t("items.chatHint")} onClick={() => goChat(it, "chat")}>
                    {t("items.chat")}
                  </button>
                  {!promoted && !discarded ? (
                    <BusyButton
                      className="btn danger"
                      busy={waiting === "discard"}
                      busyLabel={t("items.busy.discard")}
                      disabled={locked}
                      onClick={() => startAction("discard", it)}
                    >
                      {t("items.discard")}
                    </BusyButton>
                  ) : null}
                  <span style={{ flex: 1 }} />
                  <ChevronRight size={16} strokeWidth={1.75} className="muted" aria-hidden />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {open ? (
        <CortexItemDrawer
          key={`${open.agent}:${open.id}`}
          agent={open.agent}
          itemId={open.id}
          onClose={() => setOpen(null)}
          actions={drawerActions(open)}
        />
      ) : null}

      {confirm ? (
        <ConfirmModal
          title={
            confirm.verb === "discard" ? t("items.confirm.discard.title") : t("items.confirm.promote.title")
          }
          confirmLabel={confirm.verb === "discard" ? t("items.confirm.discard.title") : t("items.createProject")}
          danger={confirm.verb === "discard"}
          message={
            (confirm.verb !== "discard" && overCap ? t("items.confirm.budget") + "\n\n" : "") +
            (confirm.verb === "discard"
              ? t("items.confirm.discard.message", { title: confirm.item.title })
              : t("items.confirm.promote.message", { title: confirm.item.title }))
          }
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const c = confirm;
            setConfirm(null);
            if (c.verb === "discard") void discard(c.item);
            else void runPromote(c.item);
          }}
        />
      ) : null}

    </div>
  );
}
