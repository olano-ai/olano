/**
 * A group chat's details: its name, its members (add one, remove one) and
 * deleting it. The first member answers messages that name nobody.
 */

import {
  IonBackButton,
  IonButtons,
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
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonAlert,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import { pencil, personAddOutline, trash, trashOutline } from "ionicons/icons";
import { useEffect, useMemo } from "react";

import { groupConversationFor } from "../../chat/groupConversation.ts";
import { GROUP_MAX_MEMBERS, GROUP_MIN_MEMBERS, groupTitle } from "../../chat/groups.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import type { Agent, GroupSummary } from "../../net/types.ts";
import { useRouteParams } from "../../routeParams.ts";
import { invalidate } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { GroupAvatar } from "../../ui/GroupAvatar.tsx";
import { groupPath, useAgents } from "./AgentsPage.tsx";

export function GroupInfoPage() {
  const params = useRouteParams();
  const id = Number(params.id);
  const conversation = useMemo(() => groupConversationFor(id), [id]);
  const state = useStore(conversation.store);
  const [agents] = useAgents();
  const roster = agents.data ?? [];
  const [alert] = useIonAlert();
  const [sheet] = useIonActionSheet();
  const [toast] = useIonToast();
  const router = useIonRouter();
  const group = state.group;
  const members = group?.members ?? [];

  useEffect(() => {
    if (!state.group) void conversation.load();
  }, [conversation, state.group]);

  const problem = (error: unknown, fallback: string) =>
    void toast({ message: messageOf(error, fallback), duration: 3000, position: "top", color: "dark" });

  function applied(next: GroupSummary) {
    conversation.setGroup(next);
    invalidate("groups");
    void haptic("light");
  }

  function rename() {
    if (!group) return;
    void alert({
      header: "Rename group",
      inputs: [{ name: "name", value: group.name, attributes: { maxlength: 80, autocapitalize: "words" } }],
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Save",
          handler: (values: { name: string }) => {
            const name = String(values?.name ?? "").trim();
            if (!name || name === group.name) return;
            currentApi()
              .renameGroup(id, name)
              .then(applied)
              .catch((e) => problem(e, "The group could not be renamed."));
          },
        },
      ],
    });
  }

  function addMember() {
    const candidates = roster.filter((a) => !members.includes(a.name));
    if (!candidates.length) {
      void toast({ message: "Every agent you can use is already in this group.", duration: 2500, position: "top", color: "dark" });
      return;
    }
    void sheet({
      header: "Add to the group",
      buttons: [
        ...candidates.map((agent) => ({
          text: displayName(agent),
          handler: () => {
            currentApi()
              .addGroupMember(id, agent.name)
              .then(applied)
              .catch((e) => problem(e, "The agent could not be added."));
          },
        })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  function removeMember(agent: Agent) {
    void alert({
      header: `Remove ${displayName(agent)}?`,
      message: "It stops seeing this group's new messages. What it already said stays in the conversation.",
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Remove",
          role: "destructive",
          handler: () => {
            currentApi()
              .removeGroupMember(id, agent.name)
              .then(applied)
              .catch((e) => problem(e, "The agent could not be removed."));
          },
        },
      ],
    });
  }

  function deleteGroup() {
    void alert({
      header: "Delete this group chat?",
      message: "The conversation is deleted for everyone who can see it. The agents keep their own memories.",
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Delete",
          role: "destructive",
          handler: () => {
            currentApi()
              .deleteGroup(id)
              .then(() => {
                invalidate("groups");
                router.push("/chats", "back", "pop");
              })
              .catch((e) => problem(e, "The group could not be deleted."));
          },
        },
      ],
    });
  }

  const canRemove = members.length > GROUP_MIN_MEMBERS;
  const canAdd = members.length < GROUP_MAX_MEMBERS;
  const face = (name: string): Agent => roster.find((a) => a.name === name) ?? ({ name, running: false } as Agent);

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref={groupPath(id)} text="" />
          </IonButtons>
          <IonTitle>Group info</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        {group && (
          <div className="olano-agent-hero">
            <GroupAvatar members={members} agents={roster} size={72} />
            <h2>{groupTitle(group, roster)}</h2>
            <p>
              {members.length} agents · {group.message_count} messages
            </p>
          </div>
        )}

        <IonList inset>
          <IonItem button detail onClick={rename} disabled={!group}>
            <IonIcon slot="start" icon={pencil} color="medium" />
            <IonLabel>Name</IonLabel>
            <IonNote slot="end">{group?.name}</IonNote>
          </IonItem>
        </IonList>

        <div className="olano-section">Members</div>
        <IonList inset>
          {members.map((name, i) => {
            const agent = face(name);
            return (
              <IonItemSliding key={name} disabled={!canRemove}>
                <IonItem>
                  <div slot="start" className="olano-pick-avatar">
                    <Avatar agent={agent} size={36} />
                  </div>
                  <IonLabel>
                    <h3>{displayName(agent)}</h3>
                    <p>{i === 0 ? "Answers messages that don't name anyone" : agent.title || `@${name}`}</p>
                  </IonLabel>
                </IonItem>
                <IonItemOptions side="end">
                  <IonItemOption color="danger" onClick={() => removeMember(agent)}>
                    <IonIcon slot="icon-only" icon={trash} aria-label={`Remove ${displayName(agent)}`} />
                  </IonItemOption>
                </IonItemOptions>
              </IonItemSliding>
            );
          })}
          {canAdd && (
            <IonItem button detail={false} onClick={addMember}>
              <IonIcon slot="start" icon={personAddOutline} color="primary" />
              <IonLabel color="primary">Add an agent</IonLabel>
            </IonItem>
          )}
        </IonList>
        <div className="olano-section-note">
          {`A group holds ${GROUP_MIN_MEMBERS} to ${GROUP_MAX_MEMBERS} agents.${canRemove ? " Swipe a member to the left to remove it." : ""}`}
        </div>

        <IonList inset>
          <IonItem button detail={false} onClick={deleteGroup} disabled={!group}>
            <IonIcon slot="start" icon={trashOutline} color="danger" />
            <IonLabel color="danger">Delete group chat</IonLabel>
          </IonItem>
        </IonList>
      </IonContent>
    </IonPage>
  );
}
