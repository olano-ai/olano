/**
 * Inbox: what is waiting on you (approvals), the deployment's alerts, and
 * the issue tracker. The agents' Pulse updates live in Cortex.
 */

import {
  IonButton,
  IonContent,
  IonHeader,
  IonIcon,
  IonItem,
  IonItemOption,
  IonItemOptions,
  IonItemSliding,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSegment,
  IonSegmentButton,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import {
  alertCircle,
  checkmarkCircle,
  checkmarkDoneOutline,
  informationCircle,
  notificationsOutline,
  shieldCheckmarkOutline,
  trash,
  warning,
} from "ionicons/icons";
import { useState } from "react";

import { actionsFor, type AlertAction } from "../../inbox/alertActions.ts";
import type { HitlPending, Notification, NotificationList, PulseItem } from "../../net/types.ts";
import { startDraft } from "../../native/shareIn.ts";
import { session } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { invalidate, patchResource, refresh } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { ActionButton } from "../../ui/ActionButton.tsx";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { ago } from "../../ui/time.ts";
import { useAgents } from "../chats/AgentsPage.tsx";
import { IssuesList } from "./IssuesList.tsx";
import { useHitl, useNotifications, usePulseApprovals } from "./useInbox.ts";
import { inboxSections } from "../../state/appLayout.ts";
import { useAppLayout } from "../../state/layoutStore.ts";

type Segment = "approvals" | "alerts" | "issues";

export function AgentChip({ name }: { name?: string | null }) {
  const [agents] = useAgents();
  const agent = agents.data?.find((a) => a.name === name);
  if (!name) return null;
  return (
    <span className="olano-agent-chip">
      {agent && <Avatar agent={agent} size={18} />}
      {agent?.display_name || name}
    </span>
  );
}

function ApprovalCard({
  title,
  body,
  agent,
  risk,
  when,
  onAnswer,
}: {
  title: string;
  body?: string;
  agent?: string;
  risk?: string;
  when?: string;
  onAnswer: (approve: boolean) => Promise<void>;
}) {
  // Which answer is on its way, so that button spins and the other waits.
  const [busy, setBusy] = useState<"approve" | "decline" | null>(null);
  async function answer(approve: boolean) {
    setBusy(approve ? "approve" : "decline");
    void haptic("light");
    try {
      await onAnswer(approve);
      void haptic(approve ? "success" : "light");
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className={busy ? "olano-card olano-card-busy" : "olano-card"} aria-busy={busy ? true : undefined}>
      <div className="olano-card-head">
        <AgentChip name={agent} />
        {risk === "high" && <span className="olano-risk">High risk</span>}
        <IonNote>{ago(when)}</IonNote>
      </div>
      <h3>{title}</h3>
      {body && <p>{body}</p>}
      <div className="olano-approval-buttons">
        <ActionButton fill="outline" color="medium" busy={busy === "decline"} busyLabel="Declining…" disabled={busy !== null} onClick={() => void answer(false)}>
          Decline
        </ActionButton>
        <ActionButton busy={busy === "approve"} busyLabel="Approving…" disabled={busy !== null} onClick={() => void answer(true)}>
          Approve
        </ActionButton>
      </div>
    </div>
  );
}

/** The one line under an alert that says what tapping it offers. */
function AlertHint({ note, isAdmin, agentLabel }: { note: Notification; isAdmin: boolean; agentLabel: (name: string) => string }) {
  const actions = actionsFor(note, { isAdmin });
  const first = actions[0];
  if (!first) return null;
  if (first.kind === "dashboard") return <p className="olano-alert-hint dashboard">Fixed on the dashboard: {first.label}</p>;
  const text =
    first.kind === "issue"
      ? `Open issue #${first.id}`
      : first.kind === "repair_skills"
        ? "Repair the folders"
        : first.kind === "ask_agent"
          ? `Ask ${agentLabel(first.agent)} to fix it`
          : "Open Cortex";
  return <p className="olano-alert-hint">{text}</p>;
}

const LEVEL_ICON: Record<Notification["level"], { icon: string; color: string }> = {
  error: { icon: alertCircle, color: "danger" },
  warn: { icon: warning, color: "warning" },
  success: { icon: checkmarkCircle, color: "success" },
  info: { icon: informationCircle, color: "medium" },
};

export function InboxPage() {
  const layout = useAppLayout();
  const sections = inboxSections(layout);
  const [picked, setSegment] = useState<Segment>("approvals");
  // A section the admin hid is never the one on screen.
  const segment: Segment = sections.includes(picked) ? picked : (sections[0] ?? "approvals");
  const [hitl, reloadHitl] = useHitl();
  const [pulse, reloadPulse] = usePulseApprovals();
  const [notes, reloadNotes] = useNotifications();
  const [toast] = useIonToast();
  const [sheet] = useIonActionSheet();
  const router = useIonRouter();
  const [agents] = useAgents();
  const me = useStore(session).me;
  const isAdmin = me ? me.user.is_admin || me.multi_user === false : false;

  const approvals = (hitl.data?.length ?? 0) + (pulse.data?.length ?? 0);
  const unread = notes.data?.unread ?? 0;

  function reloadAll() {
    return Promise.all([reloadHitl(), reloadPulse(), reloadNotes(), invalidate("issues:list")]).then(() => undefined);
  }

  async function resolveHitl(item: HitlPending, approve: boolean) {
    try {
      await currentApi().hitlResolve(item.id, approve);
      patchResource<HitlPending[]>("approvals:hitl", (list) => (list ?? []).filter((p) => p.id !== item.id));
    } catch (e) {
      void toast({ message: messageOf(e), duration: 3000, color: "danger" });
      void reloadHitl();
    }
  }

  async function actOnPulse(item: PulseItem, action: "approve" | "reject" | "defer", key: string) {
    try {
      const result = await currentApi().pulseAct(action, item);
      patchResource<PulseItem[]>(key, (list) => (list ?? []).filter((p) => p.id !== item.id));
      if (result.message) void toast({ message: result.message, duration: 2500 });
    } catch (e) {
      void toast({ message: messageOf(e), duration: 3000, color: "danger" });
      void refresh(key);
    }
  }

  async function markAllRead() {
    try {
      await currentApi().markRead();
      await reloadNotes();
    } catch (e) {
      void toast({ message: messageOf(e), duration: 3000, color: "danger" });
    }
  }

  function agentLabel(name: string): string {
    const agent = agents.data?.find((a) => a.name === name);
    return agent ? displayName(agent) : name;
  }

  function actionText(action: AlertAction): string {
    switch (action.kind) {
      case "issue":
        return `Open Issue #${action.id}`;
      case "repair_skills":
        return "Repair Skill Folders";
      case "ask_agent":
        return `Ask ${agentLabel(action.agent)} to Fix It`;
      case "cortex":
        return "Open Cortex";
      case "dashboard":
        return action.label;
    }
  }

  async function repairSkills(note: Notification, agent: string | null) {
    try {
      const result = await currentApi().repairSkillNames(agent, true);
      void haptic("success");
      const skipped = result.skipped ?? [];
      const parts = [result.count ? `Repaired ${result.count} skill${result.count === 1 ? "" : "s"}.` : "Nothing to repair."];
      if (skipped.length) parts.push(`Left alone: ${skipped.map((s) => `${s.name} (${s.skipped})`).join("; ")}.`);
      void toast({ message: parts.join(" "), duration: skipped.length ? 8000 : 3000 });
      if (!skipped.length) void dismiss(note);
    } catch (e) {
      void toast({ message: messageOf(e, "The skills could not be repaired."), duration: 4000, color: "danger" });
    }
  }

  function perform(note: Notification, action: AlertAction) {
    switch (action.kind) {
      case "issue":
        router.push(`/inbox/issues/${action.id}`);
        break;
      case "repair_skills":
        void repairSkills(note, action.agent);
        break;
      case "ask_agent":
        startDraft(action.agent, action.draft);
        router.push(`/chats/${encodeURIComponent(action.agent)}/new`);
        break;
      case "cortex":
        // A tab switch, like tapping the tab: "root" would wipe every tab's history.
        router.push("/cortex", "none", "push");
        break;
      case "dashboard":
        break;
    }
  }

  function openAlert(note: Notification) {
    if (!note.read) {
      patchResource<NotificationList>("notifications", (prev) => ({
        notifications: (prev?.notifications ?? []).map((n) => (n.id === note.id ? { ...n, read: true } : n)),
        unread: Math.max(0, (prev?.unread ?? 0) - 1),
      }));
      void currentApi()
        .markRead([note.id])
        .catch(() => void reloadNotes());
    }
    const actions = actionsFor(note, { isAdmin });
    const dashboard = actions.find((a) => a.kind === "dashboard");
    // Never offer a way into a screen this app does not show.
    const doable = actions.filter(
      (a) => a.kind !== "dashboard" && (a.kind !== "cortex" || layout.cortexTab) && (a.kind !== "issue" || sections.includes("issues")),
    );
    // An issue is all an assignment is about: open it.
    if (doable.length === 1 && doable[0].kind === "issue") return perform(note, doable[0]);
    void sheet({
      header: note.title,
      subHeader: dashboard ? `This one is fixed on the dashboard, under ${dashboard.label}.` : doable.length ? undefined : note.body,
      buttons: [
        ...doable.map((action) => ({ text: actionText(action), handler: () => perform(note, action) })),
        { text: "Dismiss Alert", role: "destructive", handler: () => void dismiss(note) },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  async function dismiss(note: Notification) {
    patchResource("notifications", (prev: { notifications: Notification[]; unread: number } | undefined) => ({
      notifications: (prev?.notifications ?? []).filter((n) => n.id !== note.id),
      unread: Math.max(0, (prev?.unread ?? 0) - (note.read ? 0 : 1)),
    }));
    await currentApi().dismissNotification(note.id).catch(() => void reloadNotes());
  }

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonTitle>Inbox</IonTitle>
        </IonToolbar>
        {/* One section needs no switcher. */}
        {sections.length > 1 && (
        <IonToolbar>
          <IonSegment value={segment} onIonChange={(e) => setSegment((e.detail.value as Segment) ?? "approvals")}>
            {sections.includes("approvals") && (
              <IonSegmentButton value="approvals">
                <IonLabel>Approvals{approvals ? ` (${approvals})` : ""}</IonLabel>
              </IonSegmentButton>
            )}
            {sections.includes("alerts") && (
              <IonSegmentButton value="alerts">
                <IonLabel>Alerts{unread ? ` (${unread})` : ""}</IonLabel>
              </IonSegmentButton>
            )}
            {sections.includes("issues") && (
              <IonSegmentButton value="issues">
                <IonLabel>Issues</IonLabel>
              </IonSegmentButton>
            )}
          </IonSegment>
        </IonToolbar>
        )}
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reloadAll().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {segment === "approvals" && (
          <div className="olano-cards">
            {(hitl.error || pulse.error) && !hitl.data && !pulse.data && (
              <ErrorNote message={hitl.error || pulse.error || ""} onRetry={() => void reloadAll()} />
            )}
            {hitl.data?.map((item) => (
              <ApprovalCard
                key={`h${item.id}`}
                title={item.description || item.action}
                body={item.description ? item.action : undefined}
                agent={item.agent}
                risk={item.risk}
                when={item.created_at}
                onAnswer={(approve) => resolveHitl(item, approve)}
              />
            ))}
            {pulse.data?.map((item) => (
              <ApprovalCard
                key={`p${item.id}`}
                title={item.title || item.content || "Approval"}
                body={item.why_now || (item.title ? item.content : undefined)}
                agent={item.agent}
                when={item.created_at}
                onAnswer={(approve) => actOnPulse(item, approve ? "approve" : "reject", "approvals:pulse")}
              />
            ))}
            {hitl.data && pulse.data && approvals === 0 && (
              <Empty icon={shieldCheckmarkOutline} title="Nothing to approve" body="When an agent needs your OK before acting, it waits here." />
            )}
          </div>
        )}

        {segment === "alerts" && (
          <>
            {unread > 0 && (
              <div className="olano-inline-actions">
                <IonButton fill="clear" size="small" onClick={() => void markAllRead()}>
                  <IonIcon slot="start" icon={checkmarkDoneOutline} />
                  Mark all read
                </IonButton>
              </div>
            )}
            {notes.error && !notes.data && <ErrorNote message={notes.error} onRetry={() => void reloadNotes()} />}
            {notes.data && notes.data.notifications.length === 0 && (
              <Empty icon={notificationsOutline} title="No alerts" body="Problems that need someone, like a disconnected service, show up here." />
            )}
            {notes.data && notes.data.notifications.length > 0 && (
              <IonList inset>
                {notes.data.notifications.map((note) => {
                  const level = LEVEL_ICON[note.level] ?? LEVEL_ICON.info;
                  return (
                    <IonItemSliding key={note.id}>
                      <IonItem
                        button
                        detail
                        className={note.read ? "olano-read" : undefined}
                        onClick={() => openAlert(note)}
                      >
                        <IonIcon slot="start" icon={level.icon} color={level.color} />
                        <IonLabel className="ion-text-wrap">
                          <h3>{note.title}</h3>
                          {note.body && <p>{note.body}</p>}
                          <p className="olano-muted">
                            {note.agent ? `${note.agent} · ` : ""}
                            {ago(note.updated_at || note.created_at)}
                            {note.count && note.count > 1 ? ` · ${note.count} times` : ""}
                          </p>
                          <AlertHint note={note} isAdmin={isAdmin} agentLabel={agentLabel} />
                        </IonLabel>
                      </IonItem>
                      <IonItemOptions side="end">
                        <IonItemOption color="danger" expandable onClick={() => void dismiss(note)}>
                          <IonIcon slot="icon-only" icon={trash} />
                        </IonItemOption>
                      </IonItemOptions>
                    </IonItemSliding>
                  );
                })}
              </IonList>
            )}
          </>
        )}
        {segment === "issues" && <IssuesList />}
      </IonContent>
    </IonPage>
  );
}
