/**
 * Scheduled tasks: what the agents do on their own on a timetable (a morning
 * brief, a weekly report), with a switch to pause each one, Run now, its last
 * runs, and a sheet to add or change one.
 *
 * Two ways in, one screen: Settings lists every agent's tasks
 * (/settings/scheduled), a conversation's menu lists that agent's
 * (/chats/<agent>/_scheduled). A task opens below whichever list it was in.
 */

import {
  IonBackButton,
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
  IonSkeletonText,
  IonSpinner,
  IonTitle,
  IonToggle,
  IonToolbar,
  useIonActionSheet,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import { add, alarmOutline, alertCircle, checkmarkCircle, createOutline, playOutline, trash } from "ionicons/icons";
import { useMemo, useState } from "react";

import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import { ApiError } from "../../net/api.ts";
import { useRouteParams } from "../../routeParams.ts";
import { byAgent, isFinished, nextText, runFailed, runTime, whenText } from "../../scheduled/schedule.ts";
import type { ScheduledTask, TaskRun } from "../../shared/dashboard.ts";
import { invalidate, patchResource, useResource } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { Markdown } from "../../ui/Markdown.tsx";
import { ago, upcomingTime } from "../../ui/time.ts";
import { useAgents } from "../chats/AgentsPage.tsx";
import { TaskEditor } from "./TaskEditor.tsx";
import { goBackOr } from "../../ui/navigation.ts";

const KEY = "scheduled";

export function useScheduledTasks() {
  return useResource<ScheduledTask[]>(KEY, () => currentApi().scheduledTasks(), 30000);
}

/** Where this screen was opened from, and so where a task opens and Back goes. */
function useBase(): { base: string; back: string; agent: string | null } {
  // This page's own route params (only the agent's route has `:agent`), not
  // the current URL, which belongs to whatever tab is on screen.
  const params = useRouteParams();
  if (params.agent) {
    const agentPath = `/chats/${encodeURIComponent(params.agent)}`;
    return { base: `${agentPath}/_scheduled`, back: agentPath, agent: params.agent };
  }
  return { base: "/settings/scheduled", back: "/settings", agent: null };
}

function setEnabled(task: ScheduledTask, enabled: boolean): Promise<void> {
  patchResource<ScheduledTask[]>(KEY, (list) => (list ?? []).map((t) => (t.id === task.id ? { ...t, enabled } : t)));
  return currentApi()
    .updateTask(task.id, { enabled })
    .then(() => {
      void haptic("light");
      invalidate(KEY);
    });
}

function runNowError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return messageOf(error, "The deployment's scheduler is not running, so the task cannot run now.");
  }
  return messageOf(error, "The task could not be started.");
}

function TaskRow({ task, base, onError }: { task: ScheduledTask; base: string; onError: (message: string) => void }) {
  const [toast] = useIonToast();
  async function runNow() {
    try {
      await currentApi().runTask(task.id);
      void haptic("success");
      void toast({ message: `${task.name || "The task"} is running. Its reply lands in the agent's conversations.`, duration: 3000 });
      invalidate(KEY);
    } catch (e) {
      onError(runNowError(e));
    }
  }
  return (
    <IonItemSliding>
      <IonItem button detail={false} routerLink={`${base}/${encodeURIComponent(task.id)}`} className={task.enabled ? undefined : "olano-read"}>
        <IonLabel>
          <h3>{task.name || "Untitled task"}</h3>
          <p>{whenText(task)}</p>
          <p className="olano-muted">{nextText(task, (iso) => upcomingTime(iso))}</p>
        </IonLabel>
        <IonToggle
          slot="end"
          checked={task.enabled}
          aria-label={task.enabled ? `Pause ${task.name}` : `Resume ${task.name}`}
          onClick={(e) => e.stopPropagation()}
          onIonChange={(e) => void setEnabled(task, e.detail.checked).catch((err) => onError(messageOf(err)))}
        />
      </IonItem>
      <IonItemOptions side="end">
        <IonItemOption color="primary" onClick={() => void runNow()}>
          <IonIcon slot="top" icon={playOutline} />
          Run now
        </IonItemOption>
      </IonItemOptions>
    </IonItemSliding>
  );
}

export function ScheduledPage() {
  const { base, back, agent } = useBase();
  const [tasks, reload] = useScheduledTasks();
  const [agents] = useAgents();
  const [editing, setEditing] = useState(false);
  const [toast] = useIonToast();
  const router = useIonRouter();
  const agentInfo = agents.data?.find((a) => a.name === agent);

  const groups = useMemo(() => {
    const list = (tasks.data ?? []).filter((t) => !agent || t.agent_name === agent);
    return byAgent(list);
  }, [tasks.data, agent]);
  const count = groups.reduce((n, g) => n + g.tasks.length, 0);
  const problem = (message: string) => void toast({ message, duration: 4000, color: "danger" });
  const title = agentInfo ? `${displayName(agentInfo)}'s Tasks` : "Scheduled Tasks";

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref={back} text="" />
          </IonButtons>
          <IonTitle>{title}</IonTitle>
          <IonButtons slot="end">
            <IonButton aria-label="New scheduled task" onClick={() => setEditing(true)} disabled={!agents.data?.length}>
              <IonIcon slot="icon-only" icon={add} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void reload().finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {tasks.error && !tasks.data && <ErrorNote message={tasks.error} onRetry={() => void reload()} />}

        {!tasks.data && !tasks.error && (
          <IonList inset>
            {[0, 1, 2].map((i) => (
              <IonItem key={i}>
                <IonLabel>
                  <IonSkeletonText animated style={{ width: "55%" }} />
                  <IonSkeletonText animated style={{ width: "40%" }} />
                </IonLabel>
              </IonItem>
            ))}
          </IonList>
        )}

        {tasks.data && count === 0 && (
          <Empty
            icon={alarmOutline}
            title="No scheduled tasks"
            body={`Ask ${agentInfo ? displayName(agentInfo) : "an agent"} to do something on a schedule, like "every weekday at 8, send me what is on my calendar", or add one here.`}
          >
            <IonButton fill="outline" size="small" onClick={() => setEditing(true)} disabled={!agents.data?.length}>
              New Task
            </IonButton>
          </Empty>
        )}

        {groups.map((group) => {
          const owner = agents.data?.find((a) => a.name === group.agent);
          return (
            <div key={group.agent || "_none"}>
              {!agent && (
                <div className="olano-section olano-task-group">
                  {owner && <Avatar agent={owner} size={18} />}
                  {owner ? displayName(owner) : group.agent || "No agent"}
                </div>
              )}
              <IonList inset>
                {group.tasks.map((task) => (
                  <TaskRow key={task.id} task={task} base={base} onError={problem} />
                ))}
              </IonList>
            </div>
          );
        })}
        {count > 0 && <IonNote className="olano-list-note">Swipe a task to run it now. The switch pauses it.</IonNote>}
      </IonContent>
      <TaskEditor
        open={editing}
        onClose={() => setEditing(false)}
        agents={agents.data ?? []}
        agent={agent ?? undefined}
        onSaved={(id) => id && router.push(`${base}/${encodeURIComponent(id)}`)}
      />
    </IonPage>
  );
}

function RunRow({ run }: { run: TaskRun }) {
  const [open, setOpen] = useState(false);
  const failed = runFailed(run);
  const text = (failed ? run.error : run.result) || "";
  return (
    <IonItem button={!!text} detail={false} onClick={() => text && setOpen((v) => !v)}>
      <IonIcon slot="start" icon={failed ? alertCircle : checkmarkCircle} color={failed ? "danger" : "success"} />
      <IonLabel className="ion-text-wrap">
        <h3>{failed ? "Failed" : "Ran"}</h3>
        <p className="olano-muted">{ago(runTime(run))}</p>
        {text && !open && <p className="olano-run-preview">{text}</p>}
        {text && open && (
          <div className="olano-run-full">
            <Markdown text={text} />
          </div>
        )}
      </IonLabel>
    </IonItem>
  );
}

export function ScheduledTaskPage() {
  const { base } = useBase();
  const params = useRouteParams();
  const id = params.task ?? "";
  const [tasks, reload] = useScheduledTasks();
  const [runs, reloadRuns] = useResource<TaskRun[]>(id ? `${KEY}:runs:${id}` : null, () => currentApi().taskHistory(id), 15000);
  const [agents] = useAgents();
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [toast] = useIonToast();
  const [sheet] = useIonActionSheet();
  const router = useIonRouter();
  const task = tasks.data?.find((t) => t.id === id);
  const owner = agents.data?.find((a) => a.name === task?.agent_name);
  const problem = (message: string) => void toast({ message, duration: 4000, color: "danger" });

  async function runNow() {
    if (!task) return;
    setRunning(true);
    try {
      await currentApi().runTask(task.id);
      void haptic("success");
      void toast({ message: "Running now. The reply lands in the agent's conversations.", duration: 3000 });
      invalidate(KEY);
      window.setTimeout(() => void reloadRuns(), 4000);
    } catch (e) {
      problem(runNowError(e));
    } finally {
      setRunning(false);
    }
  }

  function remove() {
    if (!task) return;
    void sheet({
      header: `Delete "${task.name || "this task"}"?`,
      subHeader: "It stops running. Its past runs stay in the agent's conversations.",
      buttons: [
        {
          text: "Delete Task",
          role: "destructive",
          handler: () => {
            void currentApi()
              .deleteTask(task.id)
              .then(() => {
                patchResource<ScheduledTask[]>(KEY, (list) => (list ?? []).filter((t) => t.id !== task.id));
                void haptic("success");
                goBackOr(router, base);
              })
              .catch((e) => problem(messageOf(e, "The task could not be deleted.")));
          },
        },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref={base} text="" />
          </IonButtons>
          <IonTitle>{task?.name || "Task"}</IonTitle>
          <IonButtons slot="end">
            <IonButton aria-label="Edit task" disabled={!task} onClick={() => setEditing(true)}>
              <IonIcon slot="icon-only" icon={createOutline} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonRefresher slot="fixed" onIonRefresh={(e) => void Promise.all([reload(), reloadRuns()]).finally(() => e.detail.complete())}>
          <IonRefresherContent />
        </IonRefresher>

        {tasks.error && !tasks.data && <ErrorNote message={tasks.error} onRetry={() => void reload()} />}
        {tasks.data && !task && <Empty icon={alarmOutline} title="This task is gone" body="It was deleted, or it belongs to an agent you cannot use." />}
        {!tasks.data && !tasks.error && (
          <div className="olano-empty">
            <IonSpinner name="crescent" />
          </div>
        )}

        {task && (
          <>
            <IonList inset>
              <IonItem>
                <IonLabel className="ion-text-wrap">
                  <h3>{whenText(task)}</h3>
                  <p>{nextText(task, (iso) => upcomingTime(iso))}</p>
                  <p className="olano-muted">
                    {owner ? `Runs as ${displayName(owner)}` : task.agent_name ? `Runs as ${task.agent_name}` : "No agent"}
                    {` · ${(task.timezone || "UTC").replace(/_/g, " ")}`}
                    {task.run_count ? ` · ran ${task.run_count} ${task.run_count === 1 ? "time" : "times"}` : ""}
                  </p>
                </IonLabel>
              </IonItem>
              <IonItem lines="none">
                <IonToggle
                  checked={task.enabled}
                  disabled={isFinished(task)}
                  onIonChange={(e) => void setEnabled(task, e.detail.checked).catch((err) => problem(messageOf(err)))}
                >
                  {task.enabled ? "On" : "Paused"}
                </IonToggle>
              </IonItem>
            </IonList>

            <div className="olano-task-actions">
              <IonButton expand="block" disabled={running} onClick={() => void runNow()}>
                {running ? <IonSpinner name="crescent" /> : <IonIcon slot="start" icon={playOutline} />}
                Run Now
              </IonButton>
            </div>

            <div className="olano-section">Instructions</div>
            <IonList inset>
              <IonItem lines="none">
                <IonLabel className="ion-text-wrap olano-task-prompt">{task.prompt}</IonLabel>
              </IonItem>
            </IonList>

            <div className="olano-section">Recent runs</div>
            <IonList inset>
              {runs.error && !runs.data && (
                <IonItem lines="none">
                  <IonLabel className="ion-text-wrap olano-muted">{runs.error}</IonLabel>
                </IonItem>
              )}
              {runs.data && runs.data.length === 0 && (
                <IonItem lines="none">
                  <IonLabel className="ion-text-wrap olano-muted">It has not run yet.</IonLabel>
                </IonItem>
              )}
              {runs.data?.map((run, i) => <RunRow key={run.id ?? `${runTime(run)}-${i}`} run={run} />)}
            </IonList>

            <IonList inset>
              <IonItem button detail={false} onClick={remove}>
                <IonIcon slot="start" icon={trash} color="danger" />
                <IonLabel color="danger">Delete Task</IonLabel>
              </IonItem>
            </IonList>
          </>
        )}
      </IonContent>
      <TaskEditor open={editing} onClose={() => setEditing(false)} task={task} agents={agents.data ?? []} />
    </IonPage>
  );
}
