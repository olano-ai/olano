/**
 * A new group chat: a name and two to six agents. The first agent picked
 * answers a message that names nobody; anyone can be addressed with @name.
 */

import {
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonInput,
  IonItem,
  IonLabel,
  IonList,
  IonModal,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonRouter,
} from "@ionic/react";
import { useRef, useState } from "react";

import { defaultGroupName, GROUP_MAX_MEMBERS, GROUP_MIN_MEMBERS } from "../../chat/groups.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import type { Agent } from "../../net/types.ts";
import { invalidate } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";

export function NewGroupSheet({ open, onClose, agents }: { open: boolean; onClose: () => void; agents: Agent[] }) {
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useIonRouter();
  const openAfter = useRef<string | null>(null);
  const full = members.length >= GROUP_MAX_MEMBERS;
  const ready = members.length >= GROUP_MIN_MEMBERS && !busy;
  const placeholder = members.length ? defaultGroupName(members, agents) : "Group name";

  function toggle(agent: string) {
    void haptic("light");
    setError(null);
    setMembers((current) =>
      current.includes(agent) ? current.filter((m) => m !== agent) : current.length >= GROUP_MAX_MEMBERS ? current : [...current, agent],
    );
  }

  function reset() {
    setName("");
    setMembers([]);
    setError(null);
    setBusy(false);
  }

  async function create() {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const group = await currentApi().createGroup(name.trim() || defaultGroupName(members, agents), members);
      invalidate("groups");
      // Opened once the sheet has gone (onDidDismiss): a page transition
      // under a closing modal can leave the page underneath blank.
      openAfter.current = `/chats/_groups/${group.id}`;
      onClose();
    } catch (e) {
      setBusy(false);
      setError(messageOf(e, "The group could not be made."));
    }
  }

  return (
    <IonModal
      isOpen={open}
      onDidDismiss={() => {
        onClose();
        reset();
        const next = openAfter.current;
        openAfter.current = null;
        if (next) window.setTimeout(() => router.push(next), 0);
      }}
      className="olano-new-group"
    >
      <IonHeader>
        <IonToolbar>
          <IonButtons slot="start">
            <IonButton onClick={onClose}>Cancel</IonButton>
          </IonButtons>
          <IonTitle>New Group</IonTitle>
          <IonButtons slot="end">
            <IonButton strong disabled={!ready} onClick={() => void create()}>
              {busy ? <IonSpinner name="crescent" /> : "Create"}
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent className="olano-grouped">
        <IonList inset>
          <IonItem lines="none">
            <IonInput
              aria-label="Group name"
              placeholder={placeholder}
              value={name}
              maxlength={80}
              autocapitalize="words"
              onIonInput={(e) => setName(String(e.detail.value ?? ""))}
            />
          </IonItem>
        </IonList>
        <div className="olano-section">
          Agents · {members.length} of {GROUP_MAX_MEMBERS}
        </div>
        <IonList inset>
          {agents.map((agent) => {
            const at = members.indexOf(agent.name);
            const picked = at >= 0;
            return (
              <IonItem
                key={agent.name}
                button
                detail={false}
                disabled={!picked && full}
                onClick={() => toggle(agent.name)}
                aria-pressed={picked}
                className="olano-pick-row"
              >
                <div slot="start" className="olano-pick-avatar">
                  <Avatar agent={agent} size={36} />
                </div>
                <IonLabel>
                  <h3>{displayName(agent)}</h3>
                  {agent.title && <p>{agent.title}</p>}
                </IonLabel>
                <span slot="end" className={picked ? "olano-pick-mark on" : "olano-pick-mark"} aria-hidden="true">
                  {picked ? at + 1 : ""}
                </span>
              </IonItem>
            );
          })}
        </IonList>
        <div className="olano-section-note olano-new-group-note">
          {error ? (
            <span className="olano-error-text">{error}</span>
          ) : members.length < GROUP_MIN_MEMBERS ? (
            `Pick at least ${GROUP_MIN_MEMBERS} agents. The first one you pick answers messages that don't name anyone; write @name to ask someone else.`
          ) : (
            `${displayName(agents.find((a) => a.name === members[0]) ?? { name: members[0] })} answers messages that don't name anyone; write @name to ask someone else.`
          )}
        </div>
      </IonContent>
    </IonModal>
  );
}
