/**
 * One issue from the deployment's tracker: what it asks, who has it, its
 * comments, and what a person does with one from a phone: comment, close or
 * reopen it, hand it to an agent (whose issue check picks it up), or talk it
 * over with an agent.
 */

import {
  IonBackButton,
  IonButton,
  IonButtons,
  IonContent,
  IonFooter,
  IonHeader,
  IonIcon,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSpinner,
  IonTextarea,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import { arrowUp, ellipsisHorizontal } from "ionicons/icons";
import { useState } from "react";

import { agentPrincipal, bareName, eventLine, issueDraft, principalKind } from "../../inbox/issues.ts";
import { haptic } from "../../native/haptics.ts";
import { startDraft } from "../../native/shareIn.ts";
import { messageOf } from "../../native/shell.ts";
import type { Issue, IssueEvent } from "../../net/types.ts";
import { useRouteParams } from "../../routeParams.ts";
import { invalidate, useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { ErrorNote } from "../../ui/Empty.tsx";
import { Markdown } from "../../ui/Markdown.tsx";
import { ago } from "../../ui/time.ts";
import { useAgents } from "../chats/AgentsPage.tsx";

type Detail = { issue: Issue; events: IssueEvent[] };

export function IssuePage() {
  const params = useRouteParams();
  const id = Number(params.issue);
  const key = `issue:${id}`;
  const [detail, reload] = useResource<Detail>(Number.isFinite(id) ? key : null, () => currentApi().issue(id), 15000);
  const [agents] = useAgents();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [toast] = useIonToast();
  const [sheet] = useIonActionSheet();
  const router = useIonRouter();
  const issue = detail.data?.issue;

  const name = (principal: string) => {
    if (principalKind(principal) === "agent") {
      const agent = agents.data?.find((a) => a.name === bareName(principal));
      if (agent) return displayName(agent);
    }
    return bareName(principal) || "nobody";
  };

  async function change(run: () => Promise<{ issue: Issue }>, done?: string) {
    setBusy(true);
    try {
      await run();
      void haptic("success");
      if (done) void toast({ message: done, duration: 2500 });
      invalidate(key);
      return true;
    } catch (e) {
      void toast({ message: messageOf(e), duration: 4000, color: "danger" });
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    const text = comment.trim();
    if (!text || busy) return;
    if (await change(() => currentApi().commentIssue(id, text))) setComment("");
  }

  function handTo() {
    const list = agents.data ?? [];
    void sheet({
      header: "Hand this issue to",
      subHeader: "The agent picks it up the next time it checks its issues, and comments here as it works.",
      buttons: [
        ...list.map((agent) => ({
          text: displayName(agent),
          handler: () =>
            void change(() => currentApi().assignIssue(id, agentPrincipal(agent.name)), `Assigned to ${displayName(agent)}.`),
        })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  function discuss() {
    if (!issue) return;
    const list = agents.data ?? [];
    const open = (agent: string) => {
      startDraft(agent, issueDraft(issue));
      router.push(`/chats/${encodeURIComponent(agent)}/new`);
    };
    const assigned = principalKind(issue.assignee) === "agent" ? bareName(issue.assignee) : null;
    if (assigned && list.some((a) => a.name === assigned)) return open(assigned);
    if (list.length === 1) return open(list[0].name);
    void sheet({
      header: "Talk it over with",
      buttons: [...list.map((agent) => ({ text: displayName(agent), handler: () => open(agent.name) })), { text: "Cancel", role: "cancel" }],
    });
  }

  function menu() {
    if (!issue) return;
    let next: (() => void) | null = null;
    void sheet({
      buttons: [
        { text: "Hand to an Agent", handler: () => void (next = handTo) },
        { text: "Talk It Over with an Agent", handler: () => void (next = discuss) },
        issue.state === "open"
          ? {
              text: "Close Issue",
              handler: () => void change(() => currentApi().closeIssue(id), "Closed."),
            }
          : { text: "Reopen Issue", handler: () => void change(() => currentApi().reopenIssue(id), "Reopened.") },
        { text: "Cancel", role: "cancel" },
      ],
      onDidDismiss: () => {
        const open = next;
        next = null;
        if (open) window.setTimeout(open, 0);
      },
    });
  }

  const assigneeAgent = issue && principalKind(issue.assignee) === "agent" ? agents.data?.find((a) => a.name === bareName(issue.assignee)) : undefined;

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/inbox" text="" />
          </IonButtons>
          <IonTitle>{Number.isFinite(id) ? `Issue #${id}` : "Issue"}</IonTitle>
          <IonButtons slot="end">
            <IonButton aria-label="Issue actions" disabled={!issue || busy} onClick={menu}>
              <IonIcon slot="icon-only" icon={ellipsisHorizontal} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>
        {detail.error && !detail.data && <ErrorNote message={detail.error} onRetry={() => void reload()} />}
        {!detail.data && !detail.error && (
          <div className="olano-empty">
            <IonSpinner name="crescent" />
          </div>
        )}
        {issue && (
          <div className="olano-issue">
            <h1>{issue.title}</h1>
            <div className="olano-issue-meta">
              <span className={`olano-issue-state ${issue.state}`}>{issue.state === "open" ? "Open" : "Closed"}</span>
              <span className="olano-issue-type">{issue.type}</span>
              {issue.labels.map((label) => (
                <span key={label} className="olano-issue-label">
                  {label}
                </span>
              ))}
            </div>
            <p className="olano-muted olano-issue-who">
              {name(issue.author)} opened it {ago(issue.created_at)}
              {" · "}
              {issue.assignee ? (
                <>
                  {assigneeAgent && <Avatar agent={assigneeAgent} size={16} />} {name(issue.assignee)} has it
                </>
              ) : (
                "nobody has it"
              )}
            </p>
            {issue.body.trim() && (
              <div className="olano-issue-body">
                <Markdown text={issue.body} />
              </div>
            )}

            <div className="olano-issue-events">
              {detail.data!.events.map((event) => {
                const line = eventLine(event, name);
                if (line !== null)
                  return (
                    <p key={event.id} className="olano-issue-event">
                      {line} · {ago(event.ts)}
                    </p>
                  );
                return (
                  <div key={event.id} className="olano-issue-comment">
                    <div className="olano-issue-comment-head">
                      <strong>{name(event.actor)}</strong>
                      <span className="olano-muted">{ago(event.ts)}</span>
                    </div>
                    <Markdown text={event.payload.body ?? ""} />
                  </div>
                );
              })}
            </div>

            {issue.state === "open" && !issue.assignee.startsWith("ai-") && (agents.data?.length ?? 0) > 0 && (
              <IonButton fill="outline" expand="block" onClick={handTo} disabled={busy}>
                Hand to an Agent
              </IonButton>
            )}
          </div>
        )}
      </IonContent>
      {issue && (
        <IonFooter className="olano-issue-composer">
          <IonToolbar>
            <IonTextarea
              aria-label="Comment"
              placeholder={issue.state === "open" ? "Add a comment" : "Comment on a closed issue"}
              autoGrow
              rows={1}
              value={comment}
              autocapitalize="sentences"
              onIonInput={(e) => setComment(String(e.detail.value ?? ""))}
            />
            <IonButtons slot="end">
              <IonButton aria-label="Post comment" disabled={!comment.trim() || busy} onClick={() => void send()}>
                {busy ? <IonSpinner name="crescent" /> : <IonIcon slot="icon-only" icon={arrowUp} />}
              </IonButton>
            </IonButtons>
          </IonToolbar>
        </IonFooter>
      )}
    </IonPage>
  );
}
