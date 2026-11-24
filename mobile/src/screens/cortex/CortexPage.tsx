/**
 * Cortex: what the agents have been thinking about on their own. At the top,
 * a card for the Pulse findings they want you to see (PulsePage); under it the
 * pipeline as a strip of its four stages in flow order (Brainstorms, Ideas,
 * Prototypes, Projects), each with its count. Tap a stage to list it, tap it
 * again to list everything; the agent chips narrow it to one agent.
 *
 * Triage happens on the list itself. Swipe a row right to approve it (move it
 * to the next stage: the option reads "Approve" as the row slides, a full
 * swipe does it), left to discard it. Select puts a checkbox on every row for
 * discarding several at once. Every action takes effect on the list at once
 * (a discarded row goes, an approved one is listed under the stage it is
 * heading for) and the deployment is told after. Rows this phone has not
 * opened yet are tinted.
 */

import {
  IonAlert,
  IonButton,
  IonButtons,
  IonCheckbox,
  IonChip,
  IonContent,
  IonFooter,
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
  IonSkeletonText,
  IonTitle,
  IonToast,
  IonToolbar,
} from "@ionic/react";
import { checkmark, chevronForward, sparkles, trash } from "ionicons/icons";
import { Fragment, useMemo, useRef, useState } from "react";

import {
  isMoving,
  itemKey,
  itemsFor,
  KIND_LABEL,
  NEXT,
  shownKind,
  STAGES,
  stageCounts,
  toggleStage,
  withArticle,
  withMoving,
  withoutItems,
  type StageFilter,
} from "../../cortex/stages.ts";
import { useSeen } from "../../cortex/seen.ts";
import type { CortexItem } from "../../net/types.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { useHideTabBar } from "../../state/chrome.ts";
import { invalidate, patchResource, useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Avatar } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { listTime } from "../../ui/time.ts";
import { brain } from "../../ui/icons.ts";
import { useAgents } from "../chats/AgentsPage.tsx";
import { usePulseUpdates } from "./usePulse.ts";

export { KIND_LABEL } from "../../cortex/stages.ts";

interface AllItems {
  items: CortexItem[];
}

interface Notice {
  message: string;
  duration: number;
  color?: "danger";
  /** Changes per notice, so a second one remounts the toast instead of editing the first. */
  nonce: number;
}

let noticeCount = 0;

export const CORTEX_ALL_KEY = "cortex:all";

export function useCortexItems() {
  return useResource<AllItems>(CORTEX_ALL_KEY, () => currentApi().get<AllItems>("/api/cortex/items?all_agents=true"), 30000);
}

/** Takes the items out of the list before the deployment has answered. */
export function dropFromList(keys: ReadonlySet<string>): void {
  patchResource<AllItems>(CORTEX_ALL_KEY, (prev) => ({ ...prev, items: withoutItems(prev?.items ?? [], keys) }));
}

/** Lists the item under the stage its promotion is heading for, before the job is known. */
export function showMoving(key: string, to: CortexItem["kind"]): void {
  patchResource<AllItems>(CORTEX_ALL_KEY, (prev) => ({ ...prev, items: withMoving(prev?.items ?? [], key, to) }));
}

interface RowProps {
  item: CortexItem;
  unread: boolean;
  selecting: boolean;
  selected: boolean;
  onToggle: () => void;
  onApprove: () => void;
  onDiscard: () => void;
}

function ItemRow({ item, unread, selecting, selected, onToggle, onApprove, onDiscard }: RowProps) {
  const [agents] = useAgents();
  const agent = agents.data?.find((a) => a.name === item.agent);
  const score = item.rating?.score;
  const moving = isMoving(item);
  const next = NEXT[item.kind];
  const working = !moving && item.job && (item.job.status === "running" || item.job.status === "queued");
  const classes = ["olano-cortex-row", unread ? "olano-cortex-unread" : "", moving ? "olano-moving" : ""].filter(Boolean).join(" ");
  const row = (
    <IonItem
      button
      detail={!selecting}
      className={classes}
      // An item with no agent has no page (`/cortex//id` matches no route and fell to the catch-all redirect).
      routerLink={selecting || !item.agent ? undefined : `/cortex/${encodeURIComponent(item.agent)}/${encodeURIComponent(item.id)}`}
      onClick={selecting ? onToggle : undefined}
      aria-selected={selecting ? selected : undefined}
    >
      {selecting ? (
        <IonCheckbox slot="start" className="olano-select-box" checked={selected} aria-label={`Select ${item.title}`} />
      ) : (
        <div slot="start" className="olano-row-avatar">
          {agent ? <Avatar agent={agent} size={36} /> : <span className="olano-kind-dot" />}
        </div>
      )}
      <IonLabel>
        <div className="olano-row-top">
          <h3 className="olano-thread-title">{item.title}</h3>
        </div>
        <p className="olano-cortex-meta">
          <span className={`olano-kind ${item.kind}`}>{KIND_LABEL[item.kind] ?? item.kind}</span>
          {typeof score === "number" && <span className="olano-score">★ {score}/10</span>}
          {moving && <span className="olano-working">Becoming {withArticle(shownKind(item))}…</span>}
          {working && <span className="olano-working">Working…</span>}
        </p>
        {(item.summary || item.preview) && <p className="olano-row-sub">{item.summary || item.preview}</p>}
      </IonLabel>
      <IonNote slot="end" className="olano-row-time">
        {listTime(item.updated_at || item.created_at)}
      </IonNote>
    </IonItem>
  );
  return (
    <IonItemSliding disabled={selecting || moving}>
      {next && (
        <IonItemOptions side="start" onIonSwipe={onApprove}>
          <IonItemOption color="success" expandable onClick={onApprove}>
            <IonIcon slot="start" icon={checkmark} aria-hidden="true" />
            Approve
          </IonItemOption>
        </IonItemOptions>
      )}
      {row}
      <IonItemOptions side="end" onIonSwipe={onDiscard}>
        <IonItemOption color="danger" expandable onClick={onDiscard}>
          <IonIcon slot="start" icon={trash} aria-hidden="true" />
          Discard
        </IonItemOption>
      </IonItemOptions>
    </IonItemSliding>
  );
}

export function CortexPage() {
  const [data, reload] = useCortexItems();
  const [pulse, reloadPulse] = usePulseUpdates();
  const [agents] = useAgents();
  const [stage, setStage] = useState<StageFilter>("all");
  const [agentFilter, setAgentFilter] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [confirming, setConfirming] = useState(false);
  // Declarative, keyed per notice: a toast presented through the hook while
  // the previous one is still on screen never shows, and triage is quick.
  const [notice, setNotice] = useState<Notice | null>(null);
  const seen = useSeen();
  const listRef = useRef<HTMLIonListElement>(null);
  // The discard bar takes the tab bar's place while rows are being picked.
  useHideTabBar(selecting);

  const all = data.data?.items ?? [];
  const counts = useMemo(() => stageCounts(all, agentFilter), [all, agentFilter]);
  const items = useMemo(() => itemsFor(all, stage, agentFilter), [all, stage, agentFilter]);
  const agentsWithItems = useMemo(() => {
    const names = new Set(all.map((i) => i.agent).filter(Boolean));
    return (agents.data ?? []).filter((a) => names.has(a.name));
  }, [all, agents.data]);
  const updates = pulse.data?.length ?? 0;
  const stageLabel = STAGES.find((s) => s.kind === stage)?.label;
  // A selection only ever names rows that are on screen.
  const chosen = useMemo(() => items.filter((i) => selected.has(itemKey(i))), [items, selected]);
  const allChosen = items.length > 0 && chosen.length === items.length;

  function toast(next: Omit<Notice, "nonce">) {
    setNotice({ ...next, nonce: ++noticeCount });
  }

  function stopSelecting() {
    setSelecting(false);
    setSelected(new Set());
  }

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function closeRows() {
    void listRef.current?.closeSlidingItems();
  }

  async function discard(item: CortexItem) {
    closeRows();
    dropFromList(new Set([itemKey(item)]));
    void haptic("success");
    try {
      await currentApi().discardItem(item.agent ?? "", item.id);
      toast({ message: "Discarded", duration: 1500 });
    } catch (e) {
      toast({ message: messageOf(e), duration: 3000, color: "danger" });
      void reload();
    }
  }

  async function approve(item: CortexItem) {
    const to = NEXT[item.kind];
    if (!to || isMoving(item)) return;
    closeRows();
    showMoving(itemKey(item), to);
    void haptic("success");
    try {
      await currentApi().promoteItem(item.agent ?? "", item.id);
      toast({ message: `Making it ${withArticle(to)}`, duration: 2000 });
      invalidate("cortex");
    } catch (e) {
      toast({ message: messageOf(e), duration: 3000, color: "danger" });
      void reload();
    }
  }

  async function discardChosen() {
    const targets = chosen;
    setConfirming(false);
    stopSelecting();
    if (targets.length === 0) return;
    dropFromList(new Set(targets.map(itemKey)));
    void haptic("success");
    let failed = 0;
    for (const item of targets) {
      try {
        await currentApi().discardItem(item.agent ?? "", item.id);
      } catch {
        failed += 1;
      }
    }
    if (failed) {
      toast({ message: `${failed} of ${targets.length} could not be discarded`, duration: 3000, color: "danger" });
      void reload();
    } else {
      toast({ message: targets.length === 1 ? "Discarded" : `Discarded ${targets.length} items`, duration: 1500 });
    }
  }

  const confirmButtons = useMemo(
    () => [
      { text: "Cancel", role: "cancel" },
      { text: "Discard", role: "destructive", handler: () => void discardChosen() },
    ],
    // The handler reads the selection through `chosen` at the time it runs.
    [chosen],
  );

  const title = selecting ? (chosen.length ? `${chosen.length} selected` : "Select items") : "Cortex";

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          {selecting && (
            <IonButtons slot="start">
              <IonButton
                onClick={() => setSelected(allChosen ? new Set() : new Set(items.map(itemKey)))}
                disabled={items.length === 0}
              >
                {allChosen ? "Deselect all" : "Select all"}
              </IonButton>
            </IonButtons>
          )}
          <IonTitle>{title}</IonTitle>
          <IonButtons slot="end">
            {selecting ? (
              <IonButton strong onClick={stopSelecting}>
                Done
              </IonButton>
            ) : (
              items.length > 0 && (
                <IonButton className="olano-cortex-select" onClick={() => setSelecting(true)}>
                  Select
                </IonButton>
              )
            )}
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void Promise.all([reload(), reloadPulse()]).finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>
        <IonHeader collapse="condense">
          <IonToolbar>
            <IonTitle size="large">{title}</IonTitle>
          </IonToolbar>
        </IonHeader>

        {!selecting && (
          <IonItem button detail={false} routerLink="/cortex/pulse" lines="none" className="olano-pulse-card">
            <IonIcon slot="start" icon={sparkles} aria-hidden="true" />
            <IonLabel>
              <h2>{updates ? `${updates} ${updates === 1 ? "finding" : "findings"} from your agents` : "Pulse"}</h2>
              <p>{updates ? "News, research and ideas they want you to see" : "Nothing new found yet"}</p>
            </IonLabel>
            <IonIcon slot="end" icon={chevronForward} aria-hidden="true" className="olano-pulse-chevron" />
          </IonItem>
        )}

        <div className="olano-filters">
          <div className="olano-stages" role="group" aria-label="Stages">
            {STAGES.map((s, i) => (
              <Fragment key={s.kind}>
                {i > 0 && <IonIcon icon={chevronForward} className="olano-stage-arrow" aria-hidden="true" />}
                <button
                  type="button"
                  className={stage === s.kind ? "olano-stage on" : "olano-stage"}
                  aria-pressed={stage === s.kind}
                  onClick={() => setStage((current) => toggleStage(current, s.kind))}
                >
                  {s.label} <span className="olano-stage-count">{counts[s.kind]}</span>
                </button>
              </Fragment>
            ))}
          </div>
          {stage !== "all" && (
            <p className="olano-stage-note">
              Showing {stageLabel}.{" "}
              <button type="button" onClick={() => setStage("all")}>
                Show all
              </button>
            </p>
          )}
          {agentsWithItems.length > 1 && (
            <div className="olano-chips">
              <IonChip outline={agentFilter !== null} color={agentFilter === null ? "primary" : undefined} onClick={() => setAgentFilter(null)}>
                Everyone
              </IonChip>
              {agentsWithItems.map((a) => (
                <IonChip
                  key={a.name}
                  outline={agentFilter !== a.name}
                  color={agentFilter === a.name ? "primary" : undefined}
                  onClick={() => setAgentFilter(agentFilter === a.name ? null : a.name)}
                >
                  <Avatar agent={a} size={20} />
                  <IonLabel>{a.display_name || a.name}</IonLabel>
                </IonChip>
              ))}
            </div>
          )}
          {selecting && <p className="olano-stage-note">Tap rows to pick them, then discard them below.</p>}
          {!selecting && items.length > 0 && <p className="olano-stage-note olano-swipe-hint">Swipe a row right to approve it, left to discard it.</p>}
        </div>

        {data.error && !data.data && <ErrorNote message={data.error} onRetry={() => void reload()} />}

        {!data.data && !data.error && (
          <IonList inset>
            {[0, 1, 2, 3].map((i) => (
              <IonItem key={i}>
                <IonLabel>
                  <IonSkeletonText animated style={{ width: "60%" }} />
                  <IonSkeletonText animated style={{ width: "35%" }} />
                </IonLabel>
              </IonItem>
            ))}
          </IonList>
        )}

        {data.data && items.length === 0 && (
          <Empty
            icon={brain}
            title={stage === "all" ? "Nothing here yet" : `No ${stageLabel?.toLowerCase()} yet`}
            body="When agents think on their own, their brainstorms, ideas, prototypes and projects show up here."
          />
        )}

        {items.length > 0 && (
          <IonList inset ref={listRef} className="olano-cortex-list">
            {items.map((item) => {
              const key = itemKey(item);
              return (
                <ItemRow
                  key={key}
                  item={item}
                  unread={!seen.has(key)}
                  selecting={selecting}
                  selected={selected.has(key)}
                  onToggle={() => toggle(key)}
                  onApprove={() => void approve(item)}
                  onDiscard={() => void discard(item)}
                />
              );
            })}
          </IonList>
        )}

        <IonAlert
          isOpen={confirming}
          onDidDismiss={() => setConfirming(false)}
          header={chosen.length === 1 ? "Discard this item?" : `Discard ${chosen.length} items?`}
          message="They leave every list here and in the dashboard. The agent keeps its notes."
          buttons={confirmButtons}
        />
        {notice && (
          <IonToast
            key={notice.nonce}
            isOpen
            message={notice.message}
            duration={notice.duration}
            color={notice.color}
            position="bottom"
            onDidDismiss={() => setNotice((current) => (current?.nonce === notice.nonce ? null : current))}
          />
        )}
      </IonContent>
      {selecting && (
        <IonFooter className="olano-select-bar">
          <IonToolbar>
            <IonButton expand="block" color="danger" disabled={chosen.length === 0} onClick={() => setConfirming(true)}>
              <IonIcon slot="start" icon={trash} aria-hidden="true" />
              {chosen.length ? `Discard ${chosen.length}` : "Discard"}
            </IonButton>
          </IonToolbar>
        </IonFooter>
      )}
    </IonPage>
  );
}
