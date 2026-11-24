/**
 * Make or change a scheduled task: which agent, what it should do, and when.
 * "When" is the dashboard's schedule builder in small: a repeat choice and the
 * fields that choice needs, with the result read back as a sentence. A
 * schedule the builder cannot show (a hand-written cron) stays editable as
 * text, so opening a task never changes it.
 */

import {
  IonButton,
  IonButtons,
  IonChip,
  IonContent,
  IonHeader,
  IonInput,
  IonItem,
  IonLabel,
  IonList,
  IonModal,
  IonNote,
  IonSelect,
  IonSelectOption,
  IonSpinner,
  IonTextarea,
  IonTitle,
  IonToolbar,
} from "@ionic/react";
import { useEffect, useMemo, useState } from "react";

import { haptic } from "../../native/haptics.ts";
import { messageOf } from "../../native/shell.ts";
import type { Agent } from "../../net/types.ts";
import { describe, nameFromPrompt } from "../../scheduled/schedule.ts";
import {
  defaultRecurrence,
  guessTimeZone,
  isCadenceTooFrequent,
  isRecurrenceValid,
  scheduleToSpec,
  specToSchedule,
  weekdayName,
  type Freq,
  type Recurrence,
  type ScheduledTask,
} from "../../shared/dashboard.ts";
import { invalidate } from "../../state/resources.ts";
import { currentApi } from "../../state/session.ts";
import { displayName } from "../../ui/Avatar.tsx";

const REPEATS: { value: Freq; label: string }[] = [
  { value: "daily", label: "Every day" },
  { value: "weekly", label: "Every week" },
  { value: "monthly", label: "Every month" },
  { value: "hourly", label: "Every few hours" },
  { value: "minutes", label: "Every few minutes" },
  { value: "once", label: "Once" },
  { value: "advanced", label: "Custom (cron)" },
];

const pad = (n: number) => String(n).padStart(2, "0");

function timeOf(r: Recurrence): string {
  return `${pad(r.hour)}:${pad(r.minute)}`;
}

function withTime(r: Recurrence, value: string): Recurrence {
  const m = /^(\d{1,2}):(\d{2})/.exec(value);
  return m ? { ...r, hour: Number(m[1]), minute: Number(m[2]) } : r;
}

function num(value: string | number | null | undefined, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) && String(value).trim() !== "" ? n : fallback;
}

export function TaskEditor({
  open,
  onClose,
  task,
  agents,
  agent,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  /** The task to change; a new one when absent. */
  task?: ScheduledTask | null;
  agents: Agent[];
  /** The agent a new task starts with. */
  agent?: string;
  onSaved?: (id: string) => void;
}) {
  const [agentName, setAgentName] = useState("");
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [recurrence, setRecurrence] = useState<Recurrence>(defaultRecurrence);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timezone = task?.timezone || guessTimeZone();

  // Fresh values each time the sheet opens.
  useEffect(() => {
    if (!open) return;
    setAgentName(task?.agent_name || agent || agents[0]?.name || "");
    setName(task?.name || "");
    setPrompt(task?.prompt || "");
    setRecurrence(task ? scheduleToSpec(task.schedule, task.run_at) : defaultRecurrence());
    setBusy(false);
    setError(null);
  }, [open, task, agent, agents]);

  const summary = useMemo(() => describe(recurrence), [recurrence]);
  const valid = isRecurrenceValid(recurrence) && !!prompt.trim() && (!!task || !!agentName);
  const set = (patch: Partial<Recurrence>) => setRecurrence((r) => ({ ...r, ...patch }));

  async function save() {
    if (!valid || busy) return;
    setBusy(true);
    setError(null);
    const when = specToSchedule(recurrence);
    const body = {
      name: name.trim() || nameFromPrompt(prompt),
      prompt: prompt.trim(),
      schedule: when.schedule,
      run_at: when.run_at,
      timezone,
    };
    try {
      let id = task?.id ?? "";
      if (task) await currentApi().updateTask(task.id, body);
      else id = (await currentApi().createTask({ ...body, agent_name: agentName })).id;
      void haptic("success");
      invalidate("scheduled");
      onClose();
      onSaved?.(id);
    } catch (e) {
      setBusy(false);
      setError(messageOf(e, "The task could not be saved."));
    }
  }

  const r = recurrence;
  return (
    <IonModal isOpen={open} onDidDismiss={onClose} className="olano-task-editor">
      <IonHeader>
        <IonToolbar>
          <IonButtons slot="start">
            <IonButton onClick={onClose}>Cancel</IonButton>
          </IonButtons>
          <IonTitle>{task ? "Edit Task" : "New Task"}</IonTitle>
          <IonButtons slot="end">
            <IonButton strong disabled={!valid || busy} onClick={() => void save()}>
              {busy ? <IonSpinner name="crescent" /> : task ? "Save" : "Add"}
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>
      <IonContent className="olano-grouped">
        <IonList inset>
          {!task && agents.length > 1 && (
            <IonItem>
              <IonSelect
                label="Agent"
                value={agentName}
                interface="action-sheet"
                onIonChange={(e) => setAgentName(String(e.detail.value ?? ""))}
              >
                {agents.map((a) => (
                  <IonSelectOption key={a.name} value={a.name}>
                    {displayName(a)}
                  </IonSelectOption>
                ))}
              </IonSelect>
            </IonItem>
          )}
          <IonItem>
            <IonTextarea
              label="What should it do?"
              labelPlacement="stacked"
              placeholder="Check my inbox and tell me what needs an answer today."
              autoGrow
              rows={3}
              value={prompt}
              autocapitalize="sentences"
              onIonInput={(e) => setPrompt(String(e.detail.value ?? ""))}
            />
          </IonItem>
          <IonItem lines="none">
            <IonInput
              label="Name"
              labelPlacement="stacked"
              placeholder={prompt.trim() ? nameFromPrompt(prompt) : "Morning brief"}
              value={name}
              autocapitalize="sentences"
              onIonInput={(e) => setName(String(e.detail.value ?? ""))}
            />
          </IonItem>
        </IonList>
        <IonNote className="olano-list-note">
          The agent gets these words as a message each time the task runs, in a conversation of its own.
        </IonNote>

        <div className="olano-section">When</div>
        <IonList inset>
          <IonItem>
            <IonSelect
              label="Repeat"
              value={r.freq}
              interface="action-sheet"
              onIonChange={(e) => set({ freq: (e.detail.value as Freq) ?? "daily" })}
            >
              {REPEATS.filter((o) => o.value !== "advanced" || r.freq === "advanced" || !!task).map((o) => (
                <IonSelectOption key={o.value} value={o.value}>
                  {o.label}
                </IonSelectOption>
              ))}
            </IonSelect>
          </IonItem>

          {r.freq === "minutes" && (
            <IonItem>
              <IonInput
                label="Every how many minutes"
                type="number"
                inputmode="numeric"
                min={1}
                max={59}
                value={r.everyMinutes}
                onIonInput={(e) => set({ everyMinutes: num(e.detail.value, 15) })}
              />
            </IonItem>
          )}

          {r.freq === "hourly" && (
            <>
              <IonItem>
                <IonInput
                  label="Every how many hours"
                  type="number"
                  inputmode="numeric"
                  min={1}
                  max={23}
                  value={r.everyHours}
                  onIonInput={(e) => set({ everyHours: num(e.detail.value, 1) })}
                />
              </IonItem>
              <IonItem>
                <IonInput
                  label="Minutes past the hour"
                  type="number"
                  inputmode="numeric"
                  min={0}
                  max={59}
                  value={r.atMinute}
                  onIonInput={(e) => set({ atMinute: num(e.detail.value, 0) })}
                />
              </IonItem>
            </>
          )}

          {r.freq === "weekly" && (
            <IonItem>
              <div className="olano-weekdays" role="group" aria-label="On these days">
                {[0, 1, 2, 3, 4, 5, 6].map((d) => {
                  const on = r.weekdays.includes(d);
                  return (
                    <IonChip
                      key={d}
                      outline={!on}
                      color={on ? "primary" : undefined}
                      aria-pressed={on}
                      onClick={() => set({ weekdays: on ? r.weekdays.filter((w) => w !== d) : [...r.weekdays, d] })}
                    >
                      {weekdayName(d)}
                    </IonChip>
                  );
                })}
              </div>
            </IonItem>
          )}

          {r.freq === "monthly" && (
            <IonItem>
              <IonInput
                label="Day of the month"
                type="number"
                inputmode="numeric"
                min={1}
                max={31}
                value={r.dayOfMonth}
                onIonInput={(e) => set({ dayOfMonth: num(e.detail.value, 1) })}
              />
            </IonItem>
          )}

          {(r.freq === "daily" || r.freq === "weekly" || r.freq === "monthly") && (
            <IonItem>
              <IonInput label="At" type="time" value={timeOf(r)} onIonInput={(e) => setRecurrence((cur) => withTime(cur, String(e.detail.value ?? "")))} />
            </IonItem>
          )}

          {r.freq === "once" && (
            <>
              <IonItem>
                <IonInput label="On" type="date" value={r.date} onIonInput={(e) => set({ date: String(e.detail.value ?? "") })} />
              </IonItem>
              <IonItem>
                <IonInput label="At" type="time" value={r.time} onIonInput={(e) => set({ time: String(e.detail.value ?? "") })} />
              </IonItem>
            </>
          )}

          {r.freq === "advanced" && (
            <IonItem>
              <IonInput
                label="Cron"
                labelPlacement="stacked"
                placeholder="0 9 * * 0-4"
                value={r.cron}
                autocapitalize="off"
                spellcheck={false}
                onIonInput={(e) => set({ cron: String(e.detail.value ?? "") })}
              />
            </IonItem>
          )}

          <IonItem lines="none" className="olano-task-summary">
            <IonLabel className="ion-text-wrap">
              <strong>{summary}</strong>
              <p>Times are in {timezone.replace(/_/g, " ")}.</p>
            </IonLabel>
          </IonItem>
        </IonList>
        {isCadenceTooFrequent(r) && (
          <IonNote className="olano-list-note warn">
            Each run is a full agent turn, which takes a while and uses credits. If one is still going when the next is due, that
            run is skipped. Choose a longer interval unless the task is quick.
          </IonNote>
        )}
        {r.freq === "advanced" && (
          <IonNote className="olano-list-note">
            Five fields: minute, hour, day of the month, month, day of the week (0 is Monday).
          </IonNote>
        )}
        {error && <IonNote className="olano-list-note error">{error}</IonNote>}
      </IonContent>
    </IonModal>
  );
}
