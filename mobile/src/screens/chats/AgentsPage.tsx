/**
 * Chats: every agent this account can talk to, like a contact list with the
 * latest conversation under each name, arranged the way the dashboard's
 * sidebar is: Pinned first, then the person's own folders, then All agents,
 * then the group chats. Each section folds shut.
 *
 * Swipe an agent right to pin it, left to move it to a folder; press and
 * hold for everything (pin, move, reorder, a new folder). The arrangement is
 * the person's own and follows them across phones (state/arrangement.ts).
 */

import {
  IonButton,
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
  IonRefresher,
  IonRefresherContent,
  IonSearchbar,
  IonSkeletonText,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonAlert,
  useIonToast,
  useIonRouter,
} from "@ionic/react";
import { add, chevronDown, chevronForward, ellipsisHorizontal, folderOutline, peopleOutline, pin } from "ionicons/icons";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { chatSections, folderNameProblem, moveTargets, onlyAllAgents, type ChatSection } from "../../chat/arrangement.ts";
import { publishRoster } from "../../native/siri.ts";
import { groupSubtitle, groupTitle } from "../../chat/groups.ts";
import { threadTitle } from "../../chat/model.ts";
import { openAgentPath } from "../../chat/openAgent.ts";
import { conversationPath } from "../../chat/threadPick.ts";
import { haptic } from "../../native/haptics.ts";
import type { Agent, GroupMessage, GroupSummary, ThreadPage } from "../../net/types.ts";
import {
  createFolder,
  deleteFolder,
  FOLDER_NAME_MAX,
  homeOf,
  membersOf,
  moveTo,
  nudge,
  renameFolder,
  toggleFold,
  togglePin,
  type Arrangement,
  type Folder,
} from "../../shared/dashboard.ts";
import { arrange, useArrangement } from "../../state/arrangement.ts";
import { useGroupsEnabled } from "../../state/capabilities.ts";
import { useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { ErrorNote, Empty } from "../../ui/Empty.tsx";
import { GroupAvatar } from "../../ui/GroupAvatar.tsx";
import { listTime } from "../../ui/time.ts";
import { useLongPress } from "../../ui/useLongPress.ts";
import { NewGroupSheet } from "./NewGroupSheet.tsx";
import { agentHealth } from "../../agents/health.ts";

export function useAgents() {
  const resource = useResource<Agent[]>("agents", () => currentApi().agents(), 60000);
  const list = resource[0].data;
  // Siri and the launcher learn the agents' names from whatever loads the list.
  useEffect(() => {
    if (list) publishRoster(list);
  }, [list]);
  return resource;
}

export function useGroups(enabled: boolean | undefined) {
  return useResource<GroupSummary[]>(enabled ? "groups" : null, () => currentApi().groups(), 30000);
}

export function isOnline(agent: Agent): boolean {
  return agent.running && agent.paused !== true;
}

export const groupPath = (id: number) => `/chats/_groups/${id}`;

function AgentRow({
  agent,
  pinned,
  onPin,
  onMove,
  onMenu,
}: {
  agent: Agent;
  pinned: boolean;
  onPin: () => void;
  onMove: () => void;
  onMenu: () => void;
}) {
  // The row shows, and opens, the last conversation you had with the agent;
  // every other thread is in the list its menu offers. It asks the deployment
  // for exactly that one thread (chat/threadPages.ts): listing an agent's
  // whole history to pick one row is what kept a long-lived agent from
  // opening at all.
  const [latestPage] = useResource<ThreadPage>(
    `threads:${agent.name}:latest`,
    () => currentApi().threadPage(agent.name, { limit: 1, kind: "conversation" }),
    60000,
  );
  const health = agentHealth(agent);
  const sliding = useRef<HTMLIonItemSlidingElement>(null);
  const press = useLongPress(onMenu);
  const router = useIonRouter();
  const latest = latestPage.data ? (latestPage.data.threads[0] ?? null) : undefined;
  const subtitle = latest ? threadTitle(latest) : agent.title || agent.description || "";
  // Normally the answer is already here (kept from the last launch, then
  // refreshed). Without it the tap waits for it, visibly, and an answer that
  // cannot be had leads to the agent's conversations, which say why and
  // offer Retry: opening a blank new chat instead read as "every conversation
  // with this agent is gone".
  const [opening, setOpening] = useState(false);
  const open = async () => {
    if (opening) return;
    if (latest !== undefined) {
      router.push(conversationPath(agent.name, latest));
      return;
    }
    setOpening(true);
    try {
      router.push(await openAgentPath(currentApi(), agent.name));
    } finally {
      setOpening(false);
    }
  };
  const close = (then: () => void) => () => {
    void sliding.current?.close();
    then();
  };
  return (
    <IonItemSliding ref={sliding} className="olano-agent-row">
      <IonItem
        button
        detail={false}
        onClick={() => void open()}
        className="olano-row"
        lines="full"
        {...press}
      >
        <div slot="start" className="olano-row-avatar">
          <Avatar agent={agent} size={50} online={isOnline(agent)} />
        </div>
        <IonLabel>
          <div className="olano-row-top">
            <h2>
              {displayName(agent)}
              {pinned && <IonIcon icon={pin} className="olano-row-pin" aria-label="Pinned" />}
            </h2>
            {latest?.updated_at && <IonNote className="olano-row-time">{listTime(latest.updated_at)}</IonNote>}
          </div>
          <p className="olano-row-sub">
            {/* A stopped agent says so before its last message, so nobody opens it expecting a reply. */}
            {health.state !== "running" && health.state !== "starting" && (
              <span className={`olano-row-state olano-row-state-${health.state}`}>{health.label}</span>
            )}
            {subtitle || (isOnline(agent) ? "Ready" : health.state === "starting" ? "Starting" : "")}
          </p>
        </IonLabel>
        {opening && <IonSpinner slot="end" name="crescent" aria-label="Opening" />}
      </IonItem>
      <IonItemOptions side="start">
        <IonItemOption color="warning" expandable onClick={close(onPin)}>
          <IonIcon slot="top" icon={pin} />
          {pinned ? "Unpin" : "Pin"}
        </IonItemOption>
      </IonItemOptions>
      <IonItemOptions side="end">
        <IonItemOption color="primary" onClick={close(onMove)}>
          <IonIcon slot="top" icon={folderOutline} />
          Move
        </IonItemOption>
      </IonItemOptions>
    </IonItemSliding>
  );
}

function GroupRow({ group, agents }: { group: GroupSummary; agents: Agent[] }) {
  const [last] = useResource<GroupMessage | null>(
    `group-preview:${group.id}`,
    () => currentApi().groupMessages(group.id, 1).then((r) => r.messages?.[0] ?? null),
    60000,
  );
  const when = last.data?.ts || group.updated_at;
  return (
    <IonItem button detail={false} routerLink={groupPath(group.id)} className="olano-row" lines="full">
      <div slot="start" className="olano-row-avatar">
        <GroupAvatar members={group.members} agents={agents} size={50} />
      </div>
      <IonLabel>
        <div className="olano-row-top">
          <h2>{groupTitle(group, agents)}</h2>
          {when && <IonNote className="olano-row-time">{listTime(when)}</IonNote>}
        </div>
        <p className="olano-row-sub">{groupSubtitle(group, last.data, agents)}</p>
      </IonLabel>
    </IonItem>
  );
}

function SectionHead({
  title,
  count,
  folded,
  onToggle,
  onMenu,
}: {
  title: string;
  count: number;
  folded: boolean;
  onToggle: () => void;
  onMenu?: () => void;
}) {
  return (
    <div className="olano-chat-section">
      <button type="button" className="olano-chat-section-toggle" aria-expanded={!folded} onClick={onToggle}>
        <IonIcon icon={folded ? chevronForward : chevronDown} aria-hidden="true" />
        <span className="olano-chat-section-title">{title}</span>
        <span className="olano-chat-section-count">{count}</span>
      </button>
      {onMenu && (
        <button type="button" className="olano-chat-section-menu" aria-label={`${title} folder options`} onClick={onMenu}>
          <IonIcon icon={ellipsisHorizontal} />
        </button>
      )}
    </div>
  );
}

function matcher(query: string, agents: Agent[]): ((name: string) => boolean) | undefined {
  const q = query.trim().toLowerCase();
  if (!q) return undefined;
  const byName = new Map(agents.map((a) => [a.name, a]));
  return (name) => {
    const a = byName.get(name);
    return [name, a?.display_name, a?.title, a?.description].some((field) => field?.toLowerCase().includes(q));
  };
}

export function AgentsPage() {
  const [agents, reload] = useAgents();
  const groupsEnabled = useGroupsEnabled();
  const [groups, reloadGroups] = useGroups(groupsEnabled);
  const arrangement = useArrangement();
  const [query, setQuery] = useState("");
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [presentSheet] = useIonActionSheet();
  const [alert] = useIonAlert();
  const [toast] = useIonToast();
  // A choice that opens another sheet waits for this one to close: Ionic's
  // hook shows one sheet at a time, and counts this one as open until just
  // after its close callback returns (hence the zero timeout).
  const next = useRef<(() => void) | null>(null);
  const sheet = (options: Parameters<typeof presentSheet>[0] & object) =>
    presentSheet({
      ...options,
      onDidDismiss: () => {
        const then = next.current;
        next.current = null;
        if (then) window.setTimeout(then, 0);
      },
    });
  const thenOpen = (open: () => void) => () => {
    next.current = open;
  };

  const list = agents.data ?? [];
  const roster = useMemo(() => list.map((a) => a.name), [list]);
  const byName = useMemo(() => new Map(list.map((a) => [a.name, a])), [list]);
  const matches = useMemo(() => matcher(query, list), [query, list]);
  const sections = useMemo(() => chatSections(arrangement, roster, matches), [arrangement, roster, matches]);
  const visibleGroups = useMemo(() => {
    const all = groups.data ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (g) => groupTitle(g, list).toLowerCase().includes(q) || g.members.some((m) => matches?.(m)),
    );
  }, [groups.data, query, list, matches]);
  const showGroups = groupsEnabled === true && (!query || visibleGroups.length > 0);
  const groupsFolded = !query && arrangement.folded.includes("chats");

  const problem = (message: string) => void toast({ message, duration: 2500, position: "top", color: "dark" });

  function askFolderName(header: string, value: string, done: (name: string) => string | null) {
    void alert({
      header,
      inputs: [{ name: "name", value, placeholder: "Folder name", attributes: { maxlength: FOLDER_NAME_MAX, autocapitalize: "words" } }],
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Save",
          handler: (values: { name: string }) => {
            const refused = done(String(values?.name ?? ""));
            if (refused) problem(refused);
          },
        },
      ],
    });
  }

  /** A new folder, optionally with an agent moved into it straight away. */
  function newFolder(agent?: string) {
    askFolderName("New folder", "", (raw) => {
      const made = createFolder(arrangement, raw);
      if ("error" in made) return folderNameProblem(made.error);
      arrange((current) => {
        const again = createFolder(current, raw);
        if ("error" in again) return current;
        return agent ? moveTo(again.next, agent, { group: again.group.id }) : again.next;
      });
      void haptic("light");
      return null;
    });
  }

  function folderMenu(folder: Folder) {
    void sheet({
      header: folder.name,
      buttons: [
        {
          text: "Rename",
          handler: () =>
            askFolderName("Rename folder", folder.name, (raw) => {
              const renamed = renameFolder(arrangement, folder.id, raw);
              if ("error" in renamed) return folderNameProblem(renamed.error);
              arrange((current) => {
                const again = renameFolder(current, folder.id, raw);
                return "error" in again ? current : again.next;
              });
              return null;
            }),
        },
        { text: "Delete Folder", role: "destructive", handler: () => removeFolder(folder) },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  /** Deleting a folder puts its agents back under All agents; nothing else changes. */
  function removeFolder(folder: Folder) {
    const inside = membersOf(arrangement, { group: folder.id }, roster);
    const drop = (current: Arrangement) => {
      let next = current;
      for (const name of membersOf(current, { group: folder.id }, roster)) next = moveTo(next, name, "all");
      const deleted = deleteFolder(next, folder.id, roster);
      return "error" in deleted ? current : deleted.next;
    };
    if (!inside.length) {
      arrange(drop);
      return;
    }
    void alert({
      header: `Delete "${folder.name}"?`,
      message: `${inside.length === 1 ? "The agent in it goes" : `The ${inside.length} agents in it go`} back to All agents.`,
      buttons: [
        { text: "Cancel", role: "cancel" },
        { text: "Delete", role: "destructive", handler: () => arrange(drop) },
      ],
    });
  }

  function moveSheet(agent: Agent) {
    const targets = moveTargets(arrangement, agent.name);
    void sheet({
      header: `Move ${displayName(agent)} to`,
      buttons: [
        ...targets.map((target) => ({
          text: target.current ? `${target.label} ✓` : target.label,
          handler: () => {
            if (target.current) return;
            arrange((current) => moveTo(current, agent.name, target.home));
            void haptic("light");
          },
        })),
        { text: "New Folder…", handler: () => newFolder(agent.name) },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  function agentMenu(agent: Agent, section: ChatSection) {
    const pinned = homeOf(arrangement, agent.name) === "pinned";
    const at = section.agents.indexOf(agent.name);
    const buttons: { text: string; role?: string; handler?: () => void }[] = [
      { text: pinned ? "Unpin" : "Pin", handler: () => pinToggle(agent) },
      { text: "Move to…", handler: thenOpen(() => moveSheet(agent)) },
    ];
    // Reordering inside a section; hidden while searching, where the list is filtered.
    if (!query && at > 0) buttons.push({ text: "Move Up", handler: () => arrange((c) => nudge(c, agent.name, -1, roster)) });
    if (!query && at >= 0 && at < section.agents.length - 1)
      buttons.push({ text: "Move Down", handler: () => arrange((c) => nudge(c, agent.name, 1, roster)) });
    buttons.push({ text: "Cancel", role: "cancel" });
    void haptic("medium");
    void sheet({ header: displayName(agent), buttons });
  }

  function pinToggle(agent: Agent) {
    arrange((current) => togglePin(current, agent.name));
    void haptic("light");
  }

  function addMenu() {
    void haptic("light");
    const buttons: { text: string; role?: string; icon?: string; handler?: () => void }[] = [];
    if (groupsEnabled) buttons.push({ text: "New Group Chat", icon: peopleOutline, handler: () => setCreatingGroup(true) });
    buttons.push({ text: "New Folder", icon: folderOutline, handler: () => newFolder() });
    buttons.push({ text: "Cancel", role: "cancel" });
    void sheet({ buttons });
  }

  function renderSection(section: ChatSection, heading: boolean): ReactNode {
    const folder = section.kind === "folder" ? arrangement.groups.find((g) => g.id === section.id) : undefined;
    return (
      <section key={section.id} className="olano-chat-group" data-section={section.kind}>
        {heading && (
          <SectionHead
            title={section.title}
            count={section.agents.length}
            folded={section.folded}
            onToggle={() => arrange((current) => toggleFold(current, section.id))}
            onMenu={folder ? () => folderMenu(folder) : undefined}
          />
        )}
        {!section.folded && section.agents.length > 0 && (
          <IonList>
            {section.agents.map((name) => {
              const agent = byName.get(name);
              if (!agent) return null;
              return (
                <AgentRow
                  key={name}
                  agent={agent}
                  pinned={section.kind === "pinned"}
                  onPin={() => pinToggle(agent)}
                  onMove={() => moveSheet(agent)}
                  onMenu={() => agentMenu(agent, section)}
                />
              );
            })}
          </IonList>
        )}
        {!section.folded && section.kind === "folder" && section.agents.length === 0 && (
          <p className="olano-chat-section-empty">Empty. Swipe an agent to the left, or press and hold it, to move it here.</p>
        )}
      </section>
    );
  }

  const nothing = agents.data && sections.length === 0 && !(showGroups && visibleGroups.length);

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonTitle>Chats</IonTitle>
          <IonButtons slot="end">
            <IonButton aria-label="New folder or group chat" onClick={addMenu}>
              <IonIcon slot="icon-only" icon={add} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen>
        <IonRefresher
          slot="fixed"
          onIonRefresh={(e) => void Promise.all([reload(), reloadGroups()]).finally(() => e.detail.complete())}
        >
          <IonRefresherContent />
        </IonRefresher>
        <IonHeader collapse="condense">
          <IonToolbar>
            <IonTitle size="large">Chats</IonTitle>
          </IonToolbar>
          <IonToolbar>
            <IonSearchbar placeholder="Search" value={query} onIonInput={(e) => setQuery(String(e.detail.value ?? ""))} />
          </IonToolbar>
        </IonHeader>

        {agents.error && !agents.data && <ErrorNote message={agents.error} onRetry={() => void reload()} />}

        {!agents.data && !agents.error && (
          <IonList>
            {[0, 1, 2, 3].map((i) => (
              <IonItem key={i} lines="full" className="olano-row">
                <div slot="start" className="olano-row-avatar">
                  <IonSkeletonText animated style={{ width: 50, height: 50, borderRadius: 25 }} />
                </div>
                <IonLabel>
                  <IonSkeletonText animated style={{ width: "40%" }} />
                  <IonSkeletonText animated style={{ width: "70%" }} />
                </IonLabel>
              </IonItem>
            ))}
          </IonList>
        )}

        {nothing && (
          <Empty
            title={query ? "No matches" : "No agents yet"}
            body={query ? `Nothing is called "${query}".` : "Agents your account can use appear here."}
          />
        )}

        {sections.map((section) => renderSection(section, !onlyAllAgents(sections) || showGroups))}

        {showGroups && (
          <section className="olano-chat-group" data-section="groups">
            <SectionHead
              title="Group chats"
              count={visibleGroups.length}
              folded={groupsFolded}
              onToggle={() => arrange((current) => toggleFold(current, "chats"))}
            />
            {!groupsFolded && visibleGroups.length > 0 && (
              <IonList>
                {visibleGroups.map((group) => (
                  <GroupRow key={group.id} group={group} agents={list} />
                ))}
              </IonList>
            )}
            {!groupsFolded && !query && groups.data && visibleGroups.length === 0 && (
              <button type="button" className="olano-chat-section-cta" onClick={() => setCreatingGroup(true)}>
                <IonIcon icon={peopleOutline} aria-hidden="true" />
                Start a group chat with several agents
              </button>
            )}
          </section>
        )}
      </IonContent>
      <NewGroupSheet open={creatingGroup} onClose={() => setCreatingGroup(false)} agents={list} />
    </IonPage>
  );
}
