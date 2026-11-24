/**
 * Settings: who you are signed in as and where, the app's appearance, voice
 * (where voice notes become words, when replies are read aloud),
 * notifications for this phone, and the ways out (sign out, switch
 * deployment).
 */

import {
  IonContent,
  IonHeader,
  IonIcon,
  IonItem,
  IonLabel,
  IonList,
  IonNote,
  IonPage,
  IonRadio,
  IonRadioGroup,
  IonSegment,
  IonSegmentButton,
  IonSelect,
  IonSelectOption,
  IonTitle,
  IonToggle,
  IonToolbar,
  useIonActionSheet,
  useIonToast,
} from "@ionic/react";
import {
  alarmOutline,
  chatbubbleEllipsesOutline,
  folderOpenOutline,
  lockClosedOutline,
  logOutOutline,
  micOutline,
  notificationsOutline,
  sparklesOutline,
  swapHorizontalOutline,
  walletOutline,
} from "ionicons/icons";
import { useEffect, useState } from "react";

import { ApiError } from "../../net/api.ts";
import type { PushStatus } from "../../net/types.ts";
import {
  disablePush,
  enablePush,
  permission,
  PUSH_KINDS,
  pushSupported,
  savedDevice,
  setPushEvents,
  type EnableResult,
  type PushKind,
} from "../../native/push.ts";
import { lockPrefs, lockStatus, setLockPrefs, unlock, type LockStatus } from "../../native/appLock.ts";
import { appInfo, platform, type AppInfo } from "../../native/shell.ts";
import { LOCK_AFTER_CHOICES, lockName } from "../../lock/lock.ts";
import { speechEngine, type DictationStatus, type RecognitionStatus } from "../../native/speech.ts";
import { dictationStatus, forgetDictationStatus } from "../../native/dictation.ts";
import { describePreference, setThemePreference, theme, THEME_CHOICES, type ThemePreference } from "../../native/theme.ts";
import { setDefaultAgent, useDefaultAgent } from "../../state/agentPrefs.ts";
import { useResource } from "../../state/resources.ts";
import { displayName } from "../../ui/Avatar.tsx";
import { useAgents } from "../chats/AgentsPage.tsx";
import { currentApi, hostOf, isDemo, session, signOut, switchDeployment } from "../../state/session.ts";
import { needsUpdate } from "../../appConfig.ts";
import { appConfig } from "../../state/appConfig.ts";
import { useStore } from "../../state/store.ts";
import { setVoicePrefs, useVoicePrefs, type ReadAloud } from "../../state/voicePrefs.ts";
import { transcribeOn } from "../../state/appLayout.ts";
import { useAppLayout } from "../../state/layoutStore.ts";
import { useCreditsDisplay } from "./UsagePage.tsx";

const READ_ALOUD_CHOICES: { value: ReadAloud; label: string; note: string }[] = [
  { value: "voice", label: "After my voice notes", note: "A reply to a voice note is read aloud as it lands, like a conversation." },
  { value: "always", label: "Always", note: "Every reply is read aloud as it lands." },
  { value: "never", label: "Never", note: "Replies are not read aloud by themselves." },
];

function languageName(tag: string): string {
  try {
    return new Intl.DisplayNames([navigator.language || "en"], { type: "language" }).of(tag) || tag;
  } catch {
    return tag;
  }
}

/** What "Transcribe on this phone" does here, given what the phone can do. */
function transcribeNote(status: RecognitionStatus, on: boolean): string {
  if (status.permission === "denied" || status.permission === "restricted") {
    return "Speech recognition is turned off for Olano in your phone's Settings, so voice notes go to the deployment to be transcribed.";
  }
  if (!on) return "Voice notes go to the deployment as recordings, and its speech-to-text turns them into words.";
  const where = status.onDevice ? "on this phone" : "by Apple's speech recognition";
  const language = status.language ? ` It listens for ${languageName(status.language)}; for another language, turn this off.` : "";
  return `Your words are worked out ${where} and sent as text, so the agent starts at once and the recording stays here.${language}`;
}

/** What "Dictate with the microphone" does here, given what the phone can do. */
function dictationNote(status: DictationStatus, on: boolean): string {
  if (status.permission === "denied" || status.permission === "restricted") {
    return "The microphone or speech recognition is turned off for Olano in your phone's Settings, so a tap on the microphone records a voice note instead.";
  }
  if (!on) return "A tap on the microphone records a voice note, as does holding it, and Hands-Free Mode sends voice notes.";
  const language = status.language ? ` It listens for ${languageName(status.language)}, the language your phone is set to.` : "";
  return `Tap the microphone and your words appear in the message box as you speak, worked out on this phone: no recording leaves it, and what you send is the text, which you can edit first. Hold the microphone to record a voice note instead. Hands-Free Mode listens the same way and sends your words when you pause.${language}`;
}

/** What the default agent is for, on this phone. */
function defaultAgentNote(): string {
  switch (platform()) {
    case "ios":
      return "Siri's \"talk to Olano\", the Action button, Control Center and a widget with no agent picked all reach this agent. Each widget can still be set to another one (touch and hold it, then Edit Widget).";
    case "android":
      return "The first \"Talk to ...\" shortcut on a long press of the app's icon is this agent.";
    default:
      return "On a phone, Siri, the widgets and the launcher's shortcuts reach this agent when no other was picked.";
  }
}

function pushProblem(result: Exclude<EnableResult, { ok: true }>): string {
  switch (result.reason) {
    case "denied":
      return "Notifications are turned off for Olano in your phone's Settings.";
    case "unsupported":
      return "This build of the app cannot receive notifications.";
    case "old_runtime":
      return "This deployment needs a newer version of Olano to send notifications.";
    case "token":
      return "Your phone did not hand out a notification token. Try again in a moment.";
    case "prompt":
      return "Allow notifications when your phone asks.";
    default:
      return result.detail || "The deployment did not accept this phone. Try again.";
  }
}

function usePushStatus() {
  return useResource<PushStatus | null>(
    "push:status",
    () =>
      currentApi()
        .pushStatus()
        .catch((error) => {
          if (error instanceof ApiError && error.status === 404) return null;
          throw error;
        }),
    60000,
  );
}

export function SettingsPage() {
  const { me, origin } = useStore(session);
  const config = useStore(appConfig);
  const appearance = useStore(theme);
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [status] = usePushStatus();
  const [enabled, setEnabled] = useState(() => !!savedDevice());
  const [events, setEvents] = useState<PushKind[]>(() => savedDevice()?.events ?? PUSH_KINDS.map((k) => k.kind));
  const [busy, setBusy] = useState(false);
  const [toast] = useIonToast();
  const [sheet] = useIonActionSheet();
  const voice = useVoicePrefs();
  const layout = useAppLayout();
  const onPhone = transcribeOn(voice, layout) === "phone";
  const showFiles = layout.settingsTab.files;
  const [recognition, setRecognition] = useState<RecognitionStatus | null>(null);
  const [dictation, setDictation] = useState<DictationStatus | null>(null);
  const [credits] = useCreditsDisplay();
  const showCredits = layout.settingsTab.creditsUsage && !!credits.data?.credits_enabled;
  const lock = useStore(lockPrefs);
  const [lockable, setLockable] = useState<LockStatus | null>(null);
  const [agents] = useAgents();
  const chosenAgent = useDefaultAgent();
  // A chosen agent the deployment no longer has reads as "the first".
  const defaultValue = agents.data?.some((a) => a.name === chosenAgent) ? chosenAgent : "";
  const firstAgent = agents.data?.[0];

  useEffect(() => {
    void speechEngine()
      .recognitionStatus()
      .then(setRecognition)
      .catch(() => setRecognition(null));
    // Asked afresh: the permission may have changed in the phone's Settings.
    forgetDictationStatus();
    void dictationStatus().then(setDictation);
  }, []);

  useEffect(() => {
    void lockStatus().then(setLockable);
  }, []);

  useEffect(() => {
    void appInfo().then(setInfo);
    void permission().then((state) => {
      if (state !== "granted") setEnabled(false);
    });
  }, []);

  const serverOff = status.data === null || status.data?.reason === "not_managed" || status.data?.enabled === false;
  const pushNote =
    !pushSupported()
      ? "Available in the app on a phone."
      : status.data === null
        ? "Needs a newer version of Olano on the deployment."
        : status.data?.reason === "not_managed"
          ? "Only deployments managed by Olano Cloud can send notifications."
          : status.data?.enabled === false
            ? "Turned off for this deployment by an admin."
            : "";

  async function toggle(on: boolean) {
    setBusy(true);
    try {
      if (on) {
        const result = await enablePush({ prompt: true, events });
        if (result.ok) {
          setEnabled(true);
          setEvents(result.events);
        } else {
          setEnabled(false);
          void toast({ message: pushProblem(result), duration: 3500 });
        }
      } else {
        await disablePush();
        setEnabled(false);
      }
    } finally {
      setBusy(false);
    }
  }

  async function toggleKind(kind: PushKind, on: boolean) {
    const next = on ? [...new Set([...events, kind])] : events.filter((k) => k !== kind);
    setEvents(next);
    if (!(await setPushEvents(next))) void toast({ message: "Could not save that. Try again.", duration: 2500 });
  }

  /** Turning the lock on proves the phone can unlock it first, so nobody locks themselves out. */
  async function toggleLock(on: boolean) {
    if (!on) {
      setLockPrefs({ enabled: false });
      return;
    }
    const result = await unlock("Turn on the app lock");
    if (result === "ok") setLockPrefs({ enabled: true });
    else {
      setLockPrefs({ enabled: false });
      if (result !== "cancelled") void toast({ message: "The phone could not confirm it is you. The lock stays off.", duration: 3000 });
    }
  }

  function confirm(action: "signout" | "switch") {
    void sheet({
      header: action === "signout" ? "Sign out of this deployment?" : "Use a different deployment?",
      buttons: [
        {
          text: action === "signout" ? "Sign out" : "Switch deployment",
          role: "destructive",
          handler: () => {
            void disablePush().then(() => (action === "signout" ? signOut() : switchDeployment()));
          },
        },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  const user = me?.user;
  const kinds = PUSH_KINDS.filter((k) => k.kind !== "alerts" || user?.is_admin);

  return (
    <IonPage>
      <IonHeader translucent>
        <IonToolbar>
          <IonTitle>Settings</IonTitle>
        </IonToolbar>
      </IonHeader>
      <IonContent fullscreen className="olano-grouped">
        <IonHeader collapse="condense">
          <IonToolbar>
            <IonTitle size="large">Settings</IonTitle>
          </IonToolbar>
        </IonHeader>

        <IonList inset>
          <IonItem lines="none" className="olano-account">
            <div slot="start" className="olano-account-avatar">
              {(user?.name || user?.email || "?").slice(0, 1).toUpperCase()}
            </div>
            <IonLabel>
              <h2>{user?.name || "Owner"}</h2>
              <p>{user?.email && !user.email.endsWith("@localhost") ? user.email : me?.multi_user === false ? "Single-user deployment" : ""}</p>
              <p>{user?.is_admin ? "Admin" : "Member"} · {origin ? hostOf(origin) : ""}</p>
            </IonLabel>
          </IonItem>
        </IonList>
        {isDemo(origin) && (
          <div className="olano-section-note" role="note">
            You are in the Olano demo. Your demo account and its conversations are deleted after a day. Switch deployment, below, to
            connect to your own Olano.
          </div>
        )}

        <IonList inset>
          <IonItem button detail routerLink="/settings/scheduled" lines={showFiles || showCredits ? undefined : "none"}>
            <IonIcon slot="start" icon={alarmOutline} color="primary" />
            <IonLabel>Scheduled Tasks</IonLabel>
          </IonItem>
          {showFiles && (
            <IonItem button detail routerLink="/settings/files" lines={showCredits ? undefined : "none"}>
              <IonIcon slot="start" icon={folderOpenOutline} color="primary" />
              <IonLabel>Files</IonLabel>
            </IonItem>
          )}
          {showCredits && (
            <IonItem button detail routerLink="/settings/usage" lines="none">
              <IonIcon slot="start" icon={walletOutline} color="primary" />
              <IonLabel>Credits and Usage</IonLabel>
            </IonItem>
          )}
        </IonList>

        <div className="olano-section">Appearance</div>
        <IonList inset>
          <IonItem lines="none" className="olano-appearance">
            <IonSegment
              aria-label="Theme"
              value={appearance.preference}
              onIonChange={(e) => setThemePreference((e.detail.value as ThemePreference) ?? "system")}
            >
              {THEME_CHOICES.map((choice) => (
                <IonSegmentButton key={choice.value} value={choice.value}>
                  <IonLabel>{choice.label}</IonLabel>
                </IonSegmentButton>
              ))}
            </IonSegment>
          </IonItem>
        </IonList>
        <div className="olano-section-note">
          {describePreference(appearance.preference)}
        </div>

        {layout.settingsTab.transcribeOnPhone && recognition && (recognition.available || recognition.permission !== "prompt") && (
          <>
            <div className="olano-section">Voice notes</div>
            <IonList inset>
              <IonItem lines="none">
                <IonIcon slot="start" icon={micOutline} color="primary" />
                <IonToggle
                  checked={onPhone}
                  onIonChange={(e) => setVoicePrefs({ transcribeOn: e.detail.checked ? "phone" : "deployment", transcribeChosen: true })}
                >
                  Transcribe on this phone
                </IonToggle>
              </IonItem>
            </IonList>
            <div className="olano-section-note">{transcribeNote(recognition, onPhone)}</div>
          </>
        )}

        {dictation && (dictation.available || dictation.permission === "denied" || dictation.permission === "restricted") && (
          <>
            <div className="olano-section">Dictation</div>
            <IonList inset>
              <IonItem lines="none">
                <IonIcon slot="start" icon={chatbubbleEllipsesOutline} color="primary" />
                <IonToggle checked={voice.dictation} onIonChange={(e) => setVoicePrefs({ dictation: e.detail.checked })}>
                  Dictate with the microphone
                </IonToggle>
              </IonItem>
            </IonList>
            <div className="olano-section-note">{dictationNote(dictation, voice.dictation)}</div>
          </>
        )}

        <div className="olano-section">Read replies aloud</div>
        <IonList inset>
          <IonRadioGroup
            value={voice.readAloud}
            onIonChange={(e) => setVoicePrefs({ readAloud: (e.detail.value as ReadAloud) ?? "voice" })}
          >
            {READ_ALOUD_CHOICES.map((choice, i) => (
              <IonItem key={choice.value} lines={i === READ_ALOUD_CHOICES.length - 1 ? "none" : undefined}>
                <IonRadio value={choice.value} justify="space-between">
                  {choice.label}
                </IonRadio>
              </IonItem>
            ))}
          </IonRadioGroup>
        </IonList>
        <div className="olano-section-note">
          {READ_ALOUD_CHOICES.find((c) => c.value === voice.readAloud)?.note} The speaker under a reply reads it at any time, in
          the deployment's voice when it has text to speech turned on and in this phone's own voice otherwise.
        </div>

        <div className="olano-section">Siri and widgets</div>
        <IonList inset>
          <IonItem lines="none" className="olano-default-agent">
            <IonIcon slot="start" icon={sparklesOutline} color="primary" />
            <IonSelect
              label="Default agent"
              interface="action-sheet"
              value={defaultValue}
              disabled={!agents.data?.length}
              placeholder={agents.data ? "No agents" : "Loading…"}
              onIonChange={(e) => setDefaultAgent(String(e.detail.value ?? ""))}
            >
              <IonSelectOption value="">{firstAgent ? `First in the list (${displayName(firstAgent)})` : "First in the list"}</IonSelectOption>
              {(agents.data ?? []).map((agent) => (
                <IonSelectOption key={agent.name} value={agent.name}>
                  {displayName(agent)}
                </IonSelectOption>
              ))}
            </IonSelect>
          </IonItem>
        </IonList>
        <div className="olano-section-note">{defaultAgentNote()}</div>

        <div className="olano-section">Privacy</div>
        <IonList inset>
          <IonItem lines={lock.enabled ? undefined : "none"}>
            <IonIcon slot="start" icon={lockClosedOutline} color="primary" />
            <IonToggle
              checked={lock.enabled}
              disabled={!lockable?.available}
              onIonChange={(e) => void toggleLock(e.detail.checked)}
            >
              <IonLabel>
                Lock with {lockable?.available ? lockName(lockable.kind, platform()) : "Face ID"}
                {!lockable?.available && (
                  <p className="olano-muted">
                    {platform() === "web" ? "Available in the app on a phone." : "Set a passcode in your phone's Settings first."}
                  </p>
                )}
              </IonLabel>
            </IonToggle>
          </IonItem>
          {lock.enabled && (
            <IonItem lines="none">
              <IonSelect
                label="Ask again"
                interface="action-sheet"
                value={lock.afterSeconds}
                onIonChange={(e) => setLockPrefs({ afterSeconds: Number(e.detail.value) || 0 })}
              >
                {LOCK_AFTER_CHOICES.map((c) => (
                  <IonSelectOption key={c.seconds} value={c.seconds}>
                    {c.label}
                  </IonSelectOption>
                ))}
              </IonSelect>
            </IonItem>
          )}
        </IonList>
        <div className="olano-section-note">
          {lock.enabled
            ? "Olano asks for it when it opens, and when you come back after the time above. The phone's passcode always works too."
            : "Your agents can act on your accounts. With this on, someone holding your unlocked phone can't open Olano."}
        </div>

        <div className="olano-section">Notifications</div>
        <IonList inset>
          <IonItem>
            <IonIcon slot="start" icon={notificationsOutline} color="primary" />
            <IonToggle
              checked={enabled}
              disabled={busy || !pushSupported() || serverOff}
              onIonChange={(e) => void toggle(e.detail.checked)}
            >
              <IonLabel>
                Push notifications
                {pushNote && <p className="olano-muted">{pushNote}</p>}
              </IonLabel>
            </IonToggle>
          </IonItem>
          {enabled &&
            kinds.map((k) => (
              <IonItem key={k.kind}>
                <IonToggle checked={events.includes(k.kind)} onIonChange={(e) => void toggleKind(k.kind, e.detail.checked)}>
                  <IonLabel>
                    {k.label}
                    <p className="olano-muted">{k.help}</p>
                  </IonLabel>
                </IonToggle>
              </IonItem>
            ))}
        </IonList>

        <IonList inset>
          <IonItem button detail onClick={() => confirm("switch")}>
            <IonIcon slot="start" icon={swapHorizontalOutline} color="medium" />
            <IonLabel>Switch deployment</IonLabel>
          </IonItem>
          {me?.multi_user !== false && (
            <IonItem button detail={false} onClick={() => confirm("signout")}>
              <IonIcon slot="start" icon={logOutOutline} color="danger" />
              <IonLabel color="danger">Sign out</IonLabel>
            </IonItem>
          )}
        </IonList>

        <IonNote className="olano-version">
          Olano {info ? `${info.version}${info.build ? ` (${info.build})` : ""}` : ""}
        </IonNote>
        {info && needsUpdate(info.version, config) && (
          <IonNote className="olano-version" role="status">
            A newer version is available. Update Olano from the App Store or Google Play.
          </IonNote>
        )}
      </IonContent>
    </IonPage>
  );
}
