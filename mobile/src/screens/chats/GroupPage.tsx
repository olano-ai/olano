/**
 * A group chat: several agents in one conversation. A message goes to the
 * agents it names with @ (the first member when it names nobody), and each
 * answers in turn, its name and face above its reply. Typing @ suggests the
 * members; the line above the message box says who will answer.
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
  IonSpinner,
  IonTextarea,
  IonTitle,
  IonToolbar,
  useIonViewDidEnter,
} from "@ionic/react";
import { arrowUp, chevronDown, informationCircleOutline, stop } from "ionicons/icons";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { groupConversationFor } from "../../chat/groupConversation.ts";
import { addresseeLabel, addressees, authorLabel, completeMention, groupTitle, mentionQuery } from "../../chat/groups.ts";
import { groupItems, startsRun } from "../../chat/groupView.ts";
import { haptic } from "../../native/haptics.ts";
import type { Agent } from "../../net/types.ts";
import { useRouteParams } from "../../routeParams.ts";
import { invalidate } from "../../state/resources.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { GroupAvatar } from "../../ui/GroupAvatar.tsx";
import { dayLabel, sameDay } from "../../ui/time.ts";
import { useBottomAnchor } from "../../ui/useBottomAnchor.ts";
import { enterSends } from "../../native/softKeyboard.ts";
import { groupPath, useAgents } from "./AgentsPage.tsx";
import { MessageView } from "./MessageView.tsx";

const JUMP_AFTER_PX = 400;

function face(name: string, agents: readonly Agent[]): Agent {
  return agents.find((a) => a.name === name) ?? ({ name, running: false } as Agent);
}

export function GroupPage() {
  const params = useRouteParams();
  const id = Number(params.id);
  const conversation = useMemo(() => groupConversationFor(id), [id]);
  const state = useStore(conversation.store);
  const [agents] = useAgents();
  const roster = agents.data ?? [];
  const content = useRef<HTMLIonContentElement>(null);
  const input = useRef<HTMLIonTextareaElement>(null);
  const [draft, setDraft] = useState("");
  const stick = useRef(true);
  const [away, setAway] = useState(false);
  const busyBefore = useRef(state.busy);

  useEffect(() => {
    void conversation.load();
  }, [conversation]);

  useEffect(() => {
    if (busyBefore.current && !state.busy) {
      invalidate("groups");
      invalidate(`group-preview:${id}`);
      void haptic("light");
    }
    busyBefore.current = state.busy;
  }, [state.busy, id]);

  const group = state.group;
  const members = group?.members ?? [];
  const items = useMemo(() => groupItems(state.messages, state.pending, state.live, state.steps), [state.messages, state.pending, state.live, state.steps]);
  // Following the newest message, above the keyboard and the composer too.
  const list = useRef<HTMLDivElement>(null);
  const scrollSettled = useBottomAnchor(content, list, stick);
  useIonViewDidEnter(() => {
    void content.current?.scrollToBottom(0);
  });

  const query = mentionQuery(draft);
  const suggestions = useMemo(() => {
    if (query === null) return [];
    const q = query.toLowerCase();
    return members.filter((m) => {
      const a = face(m, roster);
      return m.toLowerCase().startsWith(q) || displayName(a).toLowerCase().startsWith(q);
    });
  }, [query, members, roster]);
  const answering = draft.trim() ? addressees(draft, members) : [];

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || state.busy) return;
    setDraft("");
    stick.current = true;
    void haptic("light");
    const ok = await conversation.send(text);
    if (!ok) setDraft((current) => current || text);
  }, [draft, state.busy, conversation]);

  function mention(name: string) {
    setDraft((current) => completeMention(current, name));
    void haptic("light");
    window.setTimeout(() => void input.current?.setFocus(), 30);
  }

  const title = group ? groupTitle(group, roster) : "Group chat";
  const cascade = state.cascade;

  return (
    <IonPage className="olano-conversation olano-group-chat">
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/chats" text="" />
          </IonButtons>
          <IonTitle>
            <div className="olano-chat-title">
              {group && <GroupAvatar members={group.members} agents={roster} size={30} />}
              <div className="olano-chat-title-text">
                <span>{title}</span>
                <small className="olano-chat-subtitle">
                  {members.length ? members.map((m) => displayName(face(m, roster))).join(", ") : " "}
                </small>
              </div>
            </div>
          </IonTitle>
          <IonButtons slot="end">
            <IonButton routerLink={`${groupPath(id)}/info`} aria-label="Group info">
              <IonIcon slot="icon-only" icon={informationCircleOutline} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>

      <IonContent
        ref={content}
        fullscreen
        scrollEvents
        className="olano-chat-content"
        onIonScroll={async (e) => {
          const el = await content.current?.getScrollElement();
          if (!el) return;
          // A scroll the browser made while resizing is not the person
          // moving: leave the follow flag to useBottomAnchor's answer.
          if (!scrollSettled(el)) return;
          const fromBottom = el.scrollHeight - (e.detail.scrollTop + el.clientHeight);
          stick.current = fromBottom < 80;
          setAway(fromBottom > JUMP_AFTER_PX);
        }}
      >
        <div className="olano-messages" ref={list}>
          {state.loading && (
            <div className="olano-center">
              <IonSpinner name="crescent" />
            </div>
          )}
          {state.loadError && <ErrorNote message={state.loadError} onRetry={() => void conversation.load()} />}
          {!state.loading && !state.loadError && items.length === 0 && group && (
            <Empty
              title={`Say hello to ${title}`}
              body={`${displayName(face(members[0], roster))} answers messages that don't name anyone. Write @ and a name to ask someone else, or several at once.`}
            >
              <div className="olano-empty-avatar">
                <GroupAvatar members={members} agents={roster} size={64} />
              </div>
            </Empty>
          )}
          {items.map((item, i) => {
            const previous = items[i - 1];
            const newDay = item.message.ts !== undefined && (previous?.message.ts === undefined || !sameDay(previous.message.ts, item.message.ts));
            const run = newDay || startsRun(item, previous);
            const agent = item.kind === "agent" ? face(item.author, roster) : null;
            return (
              <Fragment key={item.message.key}>
                {newDay && (
                  <div className="olano-day" role="separator">
                    <span>{dayLabel(item.message.ts)}</span>
                  </div>
                )}
                {run && item.kind !== "system" && !item.mine && (
                  <div className="olano-author">
                    {agent ? <Avatar agent={agent} size={22} /> : <span className="olano-author-human" aria-hidden="true">{authorLabel({ author: item.author, author_kind: "human" }, roster).slice(0, 1).toUpperCase()}</span>}
                    <span className="olano-author-name">{agent ? displayName(agent) : authorLabel({ author: item.author, author_kind: "human" }, roster)}</span>
                  </div>
                )}
                {item.mine && item.to.length > 0 && <div className="olano-to">{addresseeLabel(item.to.join(","), roster)}</div>}
                <MessageView
                  message={item.message}
                  readAs={item.mine ? undefined : item.author}
                  previous={run ? undefined : previous?.message}
                  onAnswer={() => undefined}
                  onEdit={item.mine ? (text) => setDraft(text) : undefined}
                  showActions={!state.busy && i === items.length - 1}
                />
              </Fragment>
            );
          })}
          {state.awaiting && (
            <div className="olano-group-typing" aria-label={`${displayName(face(state.awaiting, roster))} is writing`}>
              <Avatar agent={face(state.awaiting, roster)} size={22} />
              <span>{displayName(face(state.awaiting, roster))} is writing</span>
              <span className="olano-typing inline">
                <span />
                <span />
                <span />
              </span>
            </div>
          )}
        </div>
      </IonContent>

      <IonFooter className="olano-composer">
        {away && (
          <button
            type="button"
            className="olano-jump"
            aria-label="Jump to the latest message"
            onClick={() => {
              stick.current = true;
              setAway(false);
              void content.current?.scrollToBottom(300);
            }}
          >
            <IonIcon icon={chevronDown} />
          </button>
        )}
        {state.sendError && <div className="olano-send-error">{state.sendError}</div>}
        {cascade?.running && (
          <div className="olano-cascade" role="status">
            <IonSpinner name="dots" />
            <span>
              {cascade.stopping
                ? "Stopping after this reply…"
                : `The agents are answering each other${cascade.max_hops ? ` (up to ${cascade.max_hops} replies)` : ""}`}
            </span>
            {!cascade.stopping && (
              <button type="button" onClick={() => void conversation.stopCascade()} aria-label="Stop the agents answering each other">
                <IonIcon icon={stop} />
                Stop
              </button>
            )}
          </div>
        )}
        {suggestions.length > 0 && (
          <div className="olano-mentions" role="listbox" aria-label="Members">
            {suggestions.map((m) => (
              <button key={m} type="button" role="option" aria-selected="false" className="olano-mention" onClick={() => mention(m)}>
                <Avatar agent={face(m, roster)} size={22} />
                <span>{displayName(face(m, roster))}</span>
              </button>
            ))}
          </div>
        )}
        {answering.length > 0 && suggestions.length === 0 && (
          <div className="olano-answering">{addresseeLabel(answering.join(","), roster).replace(/^To /, "Answering: ")}</div>
        )}
        <IonToolbar>
          <div className="olano-composer-card">
            <div className="olano-composer-field">
              <IonTextarea
                ref={input}
                className="olano-composer-input"
                placeholder={group ? `Message ${title}` : "Message"}
                autoGrow
                rows={1}
                value={draft}
                autocapitalize="sentences"
                enterkeyhint="enter"
                onIonInput={(e) => setDraft(String(e.detail.value ?? ""))}
                onKeyDown={(e) => {
                  if (enterSends({ key: e.key, shiftKey: e.shiftKey, isComposing: e.nativeEvent.isComposing })) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
            </div>
            <div className="olano-composer-row">
              <span className="olano-composer-gap" />
              <IonButton
                shape="round"
                className="olano-send"
                aria-label={state.busy ? "An agent is answering" : "Send"}
                disabled={state.busy || !draft.trim()}
                onClick={() => void send()}
              >
                {state.busy ? <IonSpinner name="crescent" /> : <IonIcon slot="icon-only" icon={arrowUp} />}
              </IonButton>
            </div>
          </div>
        </IonToolbar>
      </IonFooter>
    </IonPage>
  );
}
