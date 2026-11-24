/**
 * One Cortex item: what it is, how the agent rated it, the full write-up,
 * and the three things a person does with one: move it forward, discard it,
 * or talk it over with the agent. All three buttons sit together under the
 * rating. Opening the page marks the item read on this phone, which is what
 * clears its tint in the list.
 *
 * A tap is answered on the spot. The button pressed turns into a spinner
 * with what it is doing ("Making it an idea…") before the deployment has
 * answered, and a promotion, which is a model run of a minute or two, then
 * holds a banner under the title for as long as it runs (the step the agent
 * is on, when the run reports one), says where the item went when it is
 * done, with a button to open the new page, or says why it failed and
 * offers the tap again. The banner reads the item's own `job`, which the
 * detail endpoint stamps, so a page reopened later says the same thing.
 */

import {
  IonAlert,
  IonBackButton,
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonIcon,
  IonPage,
  IonProgressBar,
  IonRefresher,
  IonRefresherContent,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import { alertCircleOutline, arrowForwardCircleOutline, chatbubbleEllipsesOutline, checkmarkCircleOutline, trashOutline } from "ionicons/icons";
import { useEffect, useMemo, useState } from "react";

import type { CortexItemDetail } from "../../net/types.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { invalidate, patchResource, useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { ActionButton } from "../../ui/ActionButton.tsx";
import { displayName } from "../../ui/Avatar.tsx";
import { ErrorNote } from "../../ui/Empty.tsx";
import { Markdown } from "../../ui/Markdown.tsx";
import { ago } from "../../ui/time.ts";
import { useRouteParams } from "../../routeParams.ts";
import { dropFromList, KIND_LABEL, showMoving } from "./CortexPage.tsx";
import { itemKey, NEXT, withArticle } from "../../cortex/stages.ts";
import { promotionState, promotionText } from "../../cortex/promotion.ts";
import { markSeen } from "../../cortex/seen.ts";
import { itemDraft } from "../../cortex/draft.ts";
import { startDraft } from "../../native/shareIn.ts";
import { goBackOr } from "../../ui/navigation.ts";
import { useAgents } from "../chats/AgentsPage.tsx";

type Pending = "promote" | "discard" | null;

export function CortexItemPage() {
  const params = useRouteParams();
  const agent = params.agent ?? "";
  const id = params.item ?? "";
  const key = `cortex:item:${agent}:${id}`;
  const [detail, reload] = useResource<CortexItemDetail>(key, () => currentApi().cortexItem(agent, id), 15000);
  const [agents] = useAgents();
  const [pending, setPending] = useState<Pending>(null);
  const [confirming, setConfirming] = useState(false);
  const [toast] = useIonToast();
  const router = useIonRouter();
  const item = detail.data?.item;
  const next = item ? NEXT[item.kind] : undefined;
  const active = item && item.status !== "discarded" && item.status !== "promoted";
  const promotion = item ? promotionState(item) : null;
  const moving = promotion?.phase === "moving";
  const agentRow = agents.data?.find((a) => a.name === agent);
  // A bare agent name reads as a word ("nova"); the banner speaks of a person.
  const shownName = agentRow ? displayName(agentRow) : agent;
  const who = shownName === agent ? agent.charAt(0).toUpperCase() + agent.slice(1) : shownName;

  useEffect(() => {
    if (agent && id) markSeen(itemKey({ agent, id }));
  }, [agent, id]);

  async function promote() {
    if (!next || pending || moving) return;
    setPending("promote");
    void haptic("light");
    try {
      await currentApi().promoteItem(agent, id);
      void haptic("success");
      // The list shows it under the next stage at once, and this page keeps
      // saying it is on its way until the refresh brings the real job: the
      // detail endpoint stamps it, so the banner never flickers back to the
      // button between the answer and the reload.
      showMoving(itemKey({ agent, id }), next);
      const shown = detail.data;
      if (shown) {
        patchResource<CortexItemDetail>(key, (prev) => {
          const base = prev ?? shown;
          return { ...base, item: { ...base.item, job: { id: "", kind: "promote", status: "queued", detail: { to: next } } } };
        });
      }
      void toast({ message: `${who} is making it ${withArticle(next)}. You can leave this page.`, duration: 3000 });
      invalidate("cortex");
    } catch (e) {
      void haptic("warning");
      void toast({ message: messageOf(e), duration: 4000, color: "danger" });
      void reload();
    } finally {
      setPending(null);
    }
  }

  async function discard() {
    setConfirming(false);
    setPending("discard");
    try {
      await currentApi().discardItem(agent, id);
      void haptic("success");
      dropFromList(new Set([itemKey({ agent, id })]));
      invalidate("cortex");
      void toast({ message: "Discarded", duration: 1500 });
      // It is gone from the list it came from; that is where the eye goes next.
      goBackOr(router, "/cortex");
    } catch (e) {
      void haptic("warning");
      void toast({ message: messageOf(e), duration: 4000, color: "danger" });
    } finally {
      setPending(null);
    }
  }

  function openSuccessor() {
    if (promotion?.phase === "promoted" && promotion.id) {
      router.push(`/cortex/${encodeURIComponent(agent)}/${encodeURIComponent(promotion.id)}`);
    } else {
      goBackOr(router, "/cortex");
    }
  }

  const confirmButtons = useMemo(
    () => [
      { text: "Cancel", role: "cancel" },
      { text: "Discard", role: "destructive", handler: () => void discard() },
    ],
    // The handler reads nothing that changes while the alert is open.
    [agent, id],
  );

  const banner = promotion ? promotionText(promotion, who) : null;

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/cortex" text="Cortex" />
          </IonButtons>
          <IonTitle>{item ? KIND_LABEL[item.kind] ?? "Item" : "Item"}</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="ion-padding">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {detail.error && !detail.data && <ErrorNote message={detail.error} onRetry={() => void reload()} />}
        {!detail.data && !detail.error && (
          <div className="olano-center">
            <IonSpinner name="crescent" />
          </div>
        )}

        {item && (
          <article className="olano-item" aria-busy={moving || pending !== null || undefined}>
            <div className="olano-item-meta">
              <span className={`olano-kind ${item.kind}`}>{KIND_LABEL[item.kind] ?? item.kind}</span>
              {item.status !== "active" && <span className="olano-status">{item.status}</span>}
              <span className="olano-muted">{ago(item.updated_at || item.created_at)}</span>
            </div>
            <h1>{item.title}</h1>
            {item.summary && <p className="olano-item-summary">{item.summary}</p>}

            {promotion && banner && (
              <div className={`olano-item-banner ${promotion.phase}`} role="status" aria-live="polite" data-testid="cortex-item-banner">
                <span className="olano-item-banner-icon" aria-hidden="true">
                  {promotion.phase === "moving" ? (
                    <IonSpinner name="crescent" />
                  ) : (
                    <IonIcon icon={promotion.phase === "promoted" ? checkmarkCircleOutline : alertCircleOutline} />
                  )}
                </span>
                <div className="olano-item-banner-text">
                  <strong>{banner.title}</strong>
                  <span>{banner.body}</span>
                </div>
                {promotion.phase === "promoted" && (
                  <IonButton size="small" fill="solid" className="olano-item-banner-action" onClick={openSuccessor}>
                    {promotion.id ? `Open the ${KIND_LABEL[promotion.to].toLowerCase()}` : "Back to the list"}
                  </IonButton>
                )}
              </div>
            )}

            {detail.data?.progress && detail.data.progress.total > 0 && (
              <div className="olano-progress">
                <IonProgressBar value={detail.data.progress.pct / 100} />
                <span>
                  {detail.data.progress.done} of {detail.data.progress.total} done
                </span>
              </div>
            )}

            {item.rating && typeof item.rating.score === "number" && (
              <div className="olano-rating">
                <div className="olano-rating-score">
                  <strong>{item.rating.score}</strong>
                  <span>/10</span>
                </div>
                <div>
                  {item.rating.recommendation && <div className="olano-rating-rec">{item.rating.recommendation}</div>}
                  {item.rating.rationale && <p>{item.rating.rationale}</p>}
                  {item.rating.next_step && <p className="olano-muted">Next: {item.rating.next_step}</p>}
                </div>
              </div>
            )}

            <div className="olano-item-actions">
              {active && next && (
                <ActionButton
                  expand="block"
                  icon={arrowForwardCircleOutline}
                  busy={pending === "promote" || moving}
                  busyLabel={pending === "promote" ? `Making it ${withArticle(next)}…` : `Becoming ${withArticle(next)}…`}
                  disabled={pending === "discard"}
                  onClick={() => void promote()}
                >
                  {promotion?.phase === "failed" ? `Try again: make it ${withArticle(next)}` : `Make it ${withArticle(next)}`}
                </ActionButton>
              )}
              <IonButton
                expand="block"
                fill="outline"
                disabled={pending !== null}
                onClick={() => {
                  // The framing goes in the message box of a first conversation
                  // about this item; one already under way reopens as it is.
                  if (item) startDraft(agent, itemDraft(item), { onlyIfNew: true });
                  router.push(`/chats/${encodeURIComponent(agent)}/${encodeURIComponent(`cortex:${id}`)}`);
                }}
              >
                <IonIcon slot="start" icon={chatbubbleEllipsesOutline} />
                Talk it over
              </IonButton>
              {active && (
                <ActionButton
                  expand="block"
                  fill="outline"
                  color="danger"
                  className="olano-discard"
                  icon={trashOutline}
                  busy={pending === "discard"}
                  busyLabel="Discarding…"
                  disabled={pending === "promote" || moving}
                  onClick={() => setConfirming(true)}
                >
                  Discard
                </ActionButton>
              )}
            </div>

            {detail.data?.detail && (
              <div className="olano-item-detail">
                <Markdown text={detail.data.detail} />
              </div>
            )}
          </article>
        )}

        <IonAlert
          isOpen={confirming}
          onDidDismiss={() => setConfirming(false)}
          header={item ? `Discard this ${KIND_LABEL[item.kind].toLowerCase()}?` : "Discard this item?"}
          message="It leaves the list here and in the dashboard. The agent keeps its notes."
          buttons={confirmButtons}
        />
      </IonContent>
    </IonPage>
  );
}
