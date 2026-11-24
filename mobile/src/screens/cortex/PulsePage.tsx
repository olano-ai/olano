/**
 * Pulse: what the agents found on their own and want you to know, newest
 * first. **Do it** carries out the suggested next step, **Later** brings the
 * card back in a day, **Dismiss** drops it. A card about work the agent
 * already did on its own (its action is `restore_backup`) offers **Keep** and
 * **Undo** instead: there is nothing left to do, only to accept or revert.
 * Cards that need an approval wait in the Inbox instead, since an agent is
 * blocked on those.
 *
 * Every tap is answered at once. Later and Dismiss take the card off the
 * screen on the tap and tell the deployment after (it comes back with an
 * error if that fails). Do it, Keep and Undo wait for the answer, because the
 * answer is the news: the pressed button spins and the card dims while it is
 * out, then the card leaves and a toast says what happened.
 */

import {
  IonBackButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonNote,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonToast,
} from "@ionic/react";
import { sparklesOutline } from "ionicons/icons";
import { useState } from "react";

import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import type { PulseItem } from "../../net/types.ts";
import { invalidate, patchResource, refresh } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { ActionButton } from "../../ui/ActionButton.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { ago } from "../../ui/time.ts";
import { AgentChip } from "../inbox/InboxPage.tsx";
import { PULSE_KEY, usePulseUpdates } from "./usePulse.ts";

type Verb = "approve" | "reject" | "defer" | "undo";

/** The card is about work already applied: approving keeps it, undo reverts it. */
export function isApplied(item: PulseItem): boolean {
  return item.action?.type === "restore_backup";
}

/** What the toast says once the deployment has answered. */
export function doneMessage(verb: Verb, item: PulseItem, result: { message?: string; job_id?: string }, who: string): string {
  if (verb === "defer") return "Back tomorrow";
  if (verb === "reject") return "Dismissed";
  if (verb === "undo") return result.message || "Reverted";
  if (result.job_id) return `${who} is on it. The result will show up here when it is done.`;
  if (isApplied(item)) return result.message || "Kept";
  return result.message || "Done";
}

export function PulsePage() {
  const [updates, reload] = usePulseUpdates();
  const [toast] = useIonToast();
  // The card whose request is out, and what it was asked.
  const [pending, setPending] = useState<{ id: string; verb: Verb } | null>(null);

  function remove(item: PulseItem) {
    patchResource<PulseItem[]>(PULSE_KEY, (list) => (list ?? []).filter((p) => p.id !== item.id));
  }

  async function act(item: PulseItem, verb: Verb) {
    if (pending) return;
    const who = item.agent || "The agent";
    const quick = verb === "defer" || verb === "reject";
    void haptic("light");
    // Later and Dismiss cost nothing to undo, so the card goes on the tap.
    if (quick) remove(item);
    else setPending({ id: item.id, verb });
    try {
      const result = await currentApi().pulseAct(verb, item);
      if (!quick) remove(item);
      void haptic("success");
      void toast({ message: doneMessage(verb, item, result, who), duration: quick ? 1500 : 3000 });
      // A "Do it" that queued a worker shows up under Runs; an approval row may have moved too.
      if (!quick) invalidate("approvals");
    } catch (e) {
      void haptic("warning");
      void toast({ message: messageOf(e), duration: 4000, color: "danger" });
      void refresh(PULSE_KEY);
    } finally {
      setPending((current) => (current?.id === item.id ? null : current));
    }
  }

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/cortex" text="" />
          </IonButtons>
          <IonTitle>Pulse</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>
        <div className="olano-cards">
          {updates.error && !updates.data && <ErrorNote message={updates.error} onRetry={() => void reload()} />}
          {!updates.data && !updates.error && (
            <div className="olano-empty">
              <IonSpinner name="crescent" />
            </div>
          )}
          {updates.data?.map((item) => {
            const busy = pending?.id === item.id ? pending.verb : null;
            const applied = isApplied(item);
            return (
              <div key={item.id} className={busy ? "olano-card olano-card-busy" : "olano-card"} aria-busy={busy ? true : undefined}>
                <div className="olano-card-head">
                  <AgentChip name={item.agent} />
                  {item.priority === "urgent" && <span className="olano-risk">Urgent</span>}
                  <IonNote>{ago(item.created_at)}</IonNote>
                </div>
                <h3>{item.title || item.content}</h3>
                {item.title && item.content && <p>{item.content}</p>}
                {item.suggested_next_step && <p className="olano-muted">Next: {item.suggested_next_step}</p>}
                <div className="olano-approval-buttons">
                  <ActionButton fill="clear" color="medium" disabled={busy !== null} onClick={() => void act(item, "defer")}>
                    Later
                  </ActionButton>
                  {applied ? (
                    <ActionButton
                      fill="clear"
                      color="medium"
                      busy={busy === "undo"}
                      busyLabel="Undoing…"
                      disabled={busy !== null}
                      onClick={() => void act(item, "undo")}
                    >
                      Undo
                    </ActionButton>
                  ) : (
                    <ActionButton fill="clear" color="medium" disabled={busy !== null} onClick={() => void act(item, "reject")}>
                      Dismiss
                    </ActionButton>
                  )}
                  <ActionButton
                    fill="clear"
                    busy={busy === "approve"}
                    busyLabel={applied ? "Keeping…" : "Doing it…"}
                    disabled={busy !== null}
                    onClick={() => void act(item, "approve")}
                  >
                    {applied ? "Keep" : "Do it"}
                  </ActionButton>
                </div>
              </div>
            );
          })}
          {updates.data && updates.data.length === 0 && (
            <Empty icon={sparklesOutline} title="All caught up" body="Agents post what they find here: news, research and ideas worth your attention." />
          )}
        </div>
      </IonContent>
    </IonPage>
  );
}
