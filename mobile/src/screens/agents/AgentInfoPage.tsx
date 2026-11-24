/**
 * An agent's page: who it is, whether it is running and, when it is not, why
 * (the error it failed to start with, or what holds it back). Admins get
 * Start, Stop and Restart; everyone gets the way to its conversations, files
 * and scheduled tasks.
 */

import {
  IonBackButton,
  IonButton,
  IonButtons,
  IonContent,
  IonHeader,
  IonIcon,
  IonItem,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonRefresher,
  IonRefresherContent,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonToast,
} from "@ionic/react";
import { alarmOutline, chatbubblesOutline, folderOpenOutline } from "ionicons/icons";
import { useState } from "react";

import { agentHealth } from "../../agents/health.ts";
import { modelLabel } from "../../chat/models.ts";
import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { useRouteParams } from "../../routeParams.ts";
import { invalidate } from "../../state/resources.ts";
import { currentApi, session } from "../../state/session.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { ErrorNote } from "../../ui/Empty.tsx";
import { useAgents } from "../chats/AgentsPage.tsx";
import { useAppLayout } from "../../state/layoutStore.ts";

type Action = "start" | "stop" | "restart";

const DONE: Record<Action, string> = {
  start: "is running",
  stop: "is stopped. It stays stopped until it is started again",
  restart: "restarted",
};

export function AgentInfoPage() {
  const showFiles = useAppLayout().settingsTab.files;
  const params = useRouteParams();
  const name = params.agent ?? "";
  const [agents, reload] = useAgents();
  const agent = agents.data?.find((a) => a.name === name);
  const me = useStore(session).me;
  const admin = !!me?.user?.is_admin || me?.multi_user === false;
  const [busy, setBusy] = useState<Action | null>(null);
  const [sheet] = useIonActionSheet();
  const [toast] = useIonToast();
  const base = `/chats/${encodeURIComponent(name)}`;

  async function run(action: Action) {
    setBusy(action);
    try {
      const api = currentApi();
      if (action === "start") await api.startAgent(name);
      else if (action === "stop") await api.stopAgent(name);
      else await api.restartAgent(name);
      void haptic("success");
      void toast({ message: `${agent ? displayName(agent) : name} ${DONE[action]}.`, duration: 2500 });
    } catch (error) {
      void toast({ message: messageOf(error, `The agent could not ${action}.`), duration: 5000, color: "danger" });
    } finally {
      setBusy(null);
      invalidate("agents");
    }
  }

  function confirmStop() {
    void sheet({
      header: `Stop ${agent ? displayName(agent) : name}?`,
      subHeader: "It stops answering messages, channels and scheduled tasks until someone starts it again.",
      buttons: [
        { text: "Stop", role: "destructive", handler: () => void run("stop") },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  const health = agent ? agentHealth(agent) : null;

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref={base} text="" />
          </IonButtons>
          <IonTitle>Agent</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>
        {agents.error && !agents.data && <ErrorNote message={agents.error} onRetry={() => void reload()} />}
        {!agents.data && !agents.error && (
          <div className="olano-empty">
            <IonSpinner name="crescent" />
          </div>
        )}
        {agents.data && !agent && <ErrorNote message="This agent is not on the deployment any more." />}

        {agent && health && (
          <>
            <div className="olano-agent-hero">
              <Avatar agent={agent} size={72} />
              <h2>{displayName(agent)}</h2>
              {(agent.title || agent.description) && <p>{agent.title || agent.description}</p>}
            </div>

            <IonList inset>
              <IonItem lines={health.detail ? "inset" : "none"}>
                <span slot="start" className={`olano-health-dot ${health.state}`} aria-hidden />
                <IonLabel>Status</IonLabel>
                <IonNote slot="end" className="olano-health-label">
                  {health.label}
                </IonNote>
              </IonItem>
              {health.detail && (
                <IonItem lines="none">
                  <IonLabel className="ion-text-wrap olano-health-detail">
                    <p>{health.detail}</p>
                  </IonLabel>
                </IonItem>
              )}
            </IonList>

            {admin && (health.canStart || health.canStop || health.canRestart) && (
              <div className="olano-agent-controls">
                {health.canStart && (
                  <IonButton expand="block" disabled={!!busy} onClick={() => void run("start")}>
                    {busy === "start" ? <IonSpinner name="crescent" /> : "Start"}
                  </IonButton>
                )}
                {health.canRestart && (
                  <IonButton expand="block" fill="outline" disabled={!!busy} onClick={() => void run("restart")}>
                    {busy === "restart" ? <IonSpinner name="crescent" /> : "Restart"}
                  </IonButton>
                )}
                {health.canStop && (
                  <IonButton expand="block" fill="clear" color="danger" disabled={!!busy} onClick={confirmStop}>
                    {busy === "stop" ? <IonSpinner name="crescent" /> : "Stop"}
                  </IonButton>
                )}
              </div>
            )}
            {!admin && health.state !== "running" && (
              <IonNote className="olano-list-note">Ask an admin of this deployment to start it.</IonNote>
            )}

            <IonList inset>
              {agent.model && (
                <IonItem>
                  <IonLabel>Model</IonLabel>
                  <IonNote slot="end">{modelLabel(agent.model)}</IonNote>
                </IonItem>
              )}
              <IonItem button detail routerLink={base}>
                <IonIcon slot="start" icon={chatbubblesOutline} color="primary" />
                <IonLabel>Conversations</IonLabel>
              </IonItem>
              {showFiles && (
                <IonItem button detail routerLink={`${base}/_files`}>
                  <IonIcon slot="start" icon={folderOpenOutline} color="primary" />
                  <IonLabel>Files</IonLabel>
                </IonItem>
              )}
              <IonItem button detail routerLink={`${base}/_scheduled`} lines="none">
                <IonIcon slot="start" icon={alarmOutline} color="primary" />
                <IonLabel>Scheduled Tasks</IonLabel>
              </IonItem>
            </IonList>
          </>
        )}
      </IonContent>
    </IonPage>
  );
}
