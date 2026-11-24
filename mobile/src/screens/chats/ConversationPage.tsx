/**
 * A conversation with an agent: message bubbles, the agent's steps as one
 * quiet line each, approvals as a card to answer, and a composer that turns
 * into a stop button while the agent works. Full screen (the tab bar hides),
 * with the platform's back button and swipe.
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
  useIonActionSheet,
  useIonAlert,
  useIonRouter,
  useIonToast,
  useIonViewDidEnter,
  useIonViewDidLeave,
} from "@ionic/react";
import {
  add,
  arrowUp,
  cameraOutline,
  chevronDown,
  close,
  createOutline,
  documentOutline,
  ellipsisHorizontal,
  flashOutline,
  globeOutline,
  imagesOutline,
  stop,
  timeOutline,
} from "ionicons/icons";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";

import { liveBrowserFor } from "../../browser/live.ts";
import { dismissKey, type BrowserStatus } from "../../browser/model.ts";
import { conversationFor, newConversation, type Conversation } from "../../chat/conversation.ts";
import { PREFETCH_SCREENS } from "../../chat/pages.ts";
import { ApiError } from "../../net/api.ts";
import { levelLabel, modelChoices, modelLabel, modelUsable, reasoningLevels, type ProvidersResponse } from "../../chat/models.ts";
import { MAX_PHOTOS, photoName, takeRoom, type MessagePhoto, type StagedPhoto } from "../../chat/photos.ts";
import { imagesFromClipboard, PhotoError, pickPhotos, preparePhoto, type PhotoSource } from "../../native/photos.ts";
import { canRecordVoice } from "../../native/recorder.ts";
import type { PreparedVoice } from "../../chat/voice.ts";
import { useGroupsEnabled } from "../../state/capabilities.ts";
import { invalidate, useResource } from "../../state/resources.ts";
import { currentApi, session } from "../../state/session.ts";
import { agentHealth, unavailableNotice } from "../../agents/health.ts";
import { invalidate as invalidateResource } from "../../state/resources.ts";
import { messageOf } from "../../native/shell.ts";
import { useStore } from "../../state/store.ts";
import { Avatar, displayName } from "../../ui/Avatar.tsx";
import { Empty, ErrorNote } from "../../ui/Empty.tsx";
import { haptic } from "../../native/haptics.ts";
import { openPhotoViewer } from "../../ui/PhotoViewer.tsx";
import { dayLabel, sameDay } from "../../ui/time.ts";
import { useBottomAnchor } from "../../ui/useBottomAnchor.ts";
import { isPageFor, useRouteParams } from "../../routeParams.ts";
import { groupPath, useAgents } from "./AgentsPage.tsx";
import { HandsFreePanel, useHandsFree } from "./HandsFree.tsx";
import { MessageView } from "./MessageView.tsx";
import { ModelSheet } from "./ModelSheet.tsx";
import { MicButton, RecordingStrip, useVoiceCapture } from "./VoiceCapture.tsx";
import { DictationControls, DictationField, useDictation, useDictationOffered } from "./Dictation.tsx";
import { withDictation } from "../../chat/dictation.ts";
import { MODES, modeLabel } from "../../chat/modes.ts";
import { fmtSize, ingestFile, MAX_ATTACHMENT_BYTES, type FileAttachment } from "../../shared/dashboard.ts";
import { incomingShare, takeSharedFor } from "../../native/shareIn.ts";
import { takeTalkFor, talkRequest } from "../../native/siri.ts";
import { composeModeOf } from "../../siri/deepLinks.ts";
import { syncWidgets } from "../../native/widgets.ts";
import { BrowserPip } from "./browser/BrowserPip.tsx";
import { BrowserView, HANDED_BACK } from "./browser/BrowserView.tsx";
import { useAppLayout } from "../../state/layoutStore.ts";
import { conversationMenu, type MenuAction } from "../../chat/menu.ts";
import { MenuSheet } from "../../ui/MenuSheet.tsx";
import { goTo } from "../../ui/navigation.ts";
import { enterSends } from "../../native/softKeyboard.ts";

const MIC_AVAILABLE = canRecordVoice();

/** Ways to start a conversation, offered while it is empty. */
const STARTERS = ["What can you help me with?", "What have you been working on?", "What should I know about today?"];

/** How far from the newest message the jump-down button appears. */
const JUMP_AFTER_PX = 400;

function useConversation(agent: string, thread: string): Conversation {
  // "new" is a fresh chat; it becomes a real thread with its first reply.
  const [conversation] = useState(() => (thread === "new" ? newConversation(agent) : conversationFor(agent, thread)));
  return conversation;
}

/**
 * Photos waiting in the composer. Each is shown at once from the phone's own
 * copy while it is prepared for sending (native/photos.ts), one at a time.
 */
function useStagedPhotos(onProblem: (message: string) => void) {
  const [photos, setPhotos] = useState<StagedPhoto[]>([]);
  const latest = useRef(photos);
  latest.current = photos;
  const counter = useRef(0);

  const update = (id: string, change: (p: StagedPhoto) => StagedPhoto | null) =>
    setPhotos((list) => list.flatMap((p) => (p.id === id ? (change(p) ?? []) : [p])));

  const addBlobs = useCallback(
    (blobs: Blob[]) => {
      const { take, dropped } = takeRoom(latest.current.length, blobs.length);
      if (dropped) onProblem(`A message can carry up to ${MAX_PHOTOS} photos.`);
      const now = new Date();
      for (const blob of blobs.slice(0, take)) {
        const id = `p${++counter.current}`;
        const staged: StagedPhoto = { id, preview: URL.createObjectURL(blob), status: "preparing" };
        setPhotos((list) => [...list, staged]);
        void preparePhoto(blob, photoName(now, counter.current - 1))
          .then((prepared) =>
            update(id, (p) => {
              URL.revokeObjectURL(p.preview);
              return { ...p, status: "ready", prepared, preview: prepared.url };
            }),
          )
          .catch((error) => {
            update(id, (p) => {
              URL.revokeObjectURL(p.preview);
              return null;
            });
            onProblem(error instanceof PhotoError ? error.message : "That photo could not be read.");
          });
      }
      if (take) void haptic("light");
    },
    [onProblem],
  );

  const remove = (id: string) =>
    update(id, (p) => {
      URL.revokeObjectURL(p.preview);
      return null;
    });

  // Photos never sent are let go with the screen.
  useEffect(
    () => () => {
      for (const p of latest.current) URL.revokeObjectURL(p.preview);
    },
    [],
  );

  return { photos, setPhotos, addBlobs, remove };
}

export function ConversationPage() {
  const layout = useAppLayout();
  const params = useRouteParams();
  const agentName = params.agent ?? "";
  const thread = params.thread ?? "new";
  const conversation = useConversation(agentName, thread);
  const state = useStore(conversation.store);
  const [agents] = useAgents();
  const agent = agents.data?.find((a) => a.name === agentName);
  const groupsEnabled = useGroupsEnabled();
  const content = useRef<HTMLIonContentElement>(null);
  const [draft, setDraft] = useState("");
  // Set while the box holds the words of a sent message being corrected.
  const [editing, setEditing] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  // The deployment's models: fetched once and kept for a while, all
  // conversations share them.
  const [catalog, reloadCatalog] = useResource<ProvidersResponse>("catalog:providers", () => currentApi().providers(), 10 * 60 * 1000);
  const choices = useMemo(() => modelChoices(catalog.data), [catalog.data]);
  const input = useRef<HTMLIonTextareaElement>(null);
  const stick = useRef(true);
  const [away, setAway] = useState(false);
  const [unseen, setUnseen] = useState(false);
  const busyBefore = useRef(state.busy);
  const [presentSheet] = useIonActionSheet();
  const [presentAlert] = useIonAlert();
  const [presentToast] = useIonToast();
  const me = useStore(session).me;
  const admin = !!me?.user?.is_admin || me?.multi_user === false;
  const [starting, setStarting] = useState(false);
  const router = useIonRouter();
  const problem = useCallback(
    (message: string) => void presentToast({ message, duration: 3500, position: "top", color: "dark" }),
    [presentToast],
  );
  const staged = useStagedPhotos(problem);

  // The agent's browser, when the deployment runs it live: a floating window
  // over the conversation and a full view to watch it or take control.
  const browserKey = `browser:${agentName}`;
  const [browserStatus] = useResource<BrowserStatus | null>(
    browserKey,
    () =>
      currentApi()
        .browserStatus(agentName)
        .catch((error) => {
          if (error instanceof ApiError && (error.status === 404 || error.status === 403)) return null;
          throw error;
        }),
    30000,
  );
  const liveSession = browserStatus.data?.live_view ? browserStatus.data.session : null;
  const browser = useMemo(() => liveBrowserFor(agentName), [agentName]);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [hiddenFor, setHiddenFor] = useState<string | null>(null);
  const showPip = !!liveSession && !browserOpen && dismissKey(liveSession) !== hiddenFor;
  // Documents waiting in the composer (PDF, Word, Excel, text...), read on the phone.
  const [files, setFiles] = useState<FileAttachment[]>([]);
  const filePicker = useRef<HTMLInputElement>(null);
  const addFiles = useCallback(
    async (list: File[]) => {
      for (const file of list) {
        const result = await ingestFile(file);
        if (result.ok) setFiles((current) => [...current, result.attachment]);
        else
          problem(
            result.reason === "too_large"
              ? `${file.name} is larger than ${fmtSize(MAX_ATTACHMENT_BYTES)}, the most a message can carry.`
              : `${file.name} could not be read.`,
          );
      }
      if (list.length) void haptic("light");
    },
    [problem],
  );
  // A file shared into Olano from another app, for this agent.
  const share = useStore(incomingShare);
  useEffect(() => {
    // A draft meant only for a new conversation waits until this one has
    // loaded, then goes only into an empty one.
    if (share?.onlyIfNew && share.agent === agentName && state.loading) return;
    const shared = takeSharedFor(agentName);
    if (!shared) return;
    if (shared.onlyIfNew && state.messages.length > 0) return;
    if (shared.text) setDraft((current) => current || shared.text);
    if (shared.files.length) setFiles((current) => [...current, ...shared.files]);
  }, [agentName, share, state.loading, state.messages.length]);
  const preparing = staged.photos.some((p) => p.status === "preparing");
  const ready = staged.photos.filter((p) => p.status === "ready" && p.prepared);

  const sendVoice = useCallback(
    async function sendOne(voice: PreparedVoice) {
      stick.current = true;
      const ok = await conversation.send("", [], voice);
      if (!ok) {
        void presentToast({
          message: "The voice note could not be sent.",
          duration: 6000,
          position: "top",
          color: "dark",
          buttons: [{ text: "Retry", handler: () => void sendOne(voice) }],
        });
      }
    },
    [conversation, presentToast],
  );
  // Live dictation: a tap on the microphone puts the words in the box as they
  // are said; whatever was heard when it stops on its own stays there.
  const keepDictated = useCallback((words: string) => setDraft((current) => withDictation(current, words)), []);
  const dictation = useDictation(problem, keepDictated);
  const dictating = dictation.state !== "idle";
  const dictationOffered = useDictationOffered();
  const capture = useVoiceCapture(sendVoice, problem, dictationOffered ? dictation.start : undefined);
  const handsFree = useHandsFree(conversation, problem, dictationOffered);
  const handsFreeOn = handsFree.phase !== "off";
  const recording = capture.state !== "idle";
  const canSend = !!draft.trim() || staged.photos.length > 0 || files.length > 0;

  useEffect(() => {
    void conversation.load();
  }, [conversation]);

  // A model this conversation last ran on that the deployment no longer
  // offers: continue on the agent's own rather than fail every message.
  useEffect(() => {
    if (!catalog.data || !agent || !state.model) return;
    if (modelUsable(state.model, agent.model, choices)) return;
    const gone = modelLabel(state.model);
    conversation.setModel("", "");
    problem(`${gone} isn't available on this deployment any more, so this conversation continues on ${modelLabel(agent.model ?? "") || "the agent's model"}.`);
  }, [catalog.data, agent, state.model, choices, conversation, problem]);

  // A browser step in the reply: the agent's browser may have just opened.
  const lastBrowserStep = [...(state.messages[state.messages.length - 1]?.tools ?? [])].reverse().find((t) => t.name.startsWith("browser_"))?.id;
  useEffect(() => {
    if (lastBrowserStep) invalidate(browserKey);
  }, [lastBrowserStep, browserKey]);

  // A finished turn changes the conversation list's order and preview.
  useEffect(() => {
    if (busyBefore.current && !state.busy) {
      invalidate(`threads:${agentName}`);
      void haptic("light");
      // The agent's latest reply is what its widget shows.
      syncWidgets();
    }
    busyBefore.current = state.busy;
  }, [state.busy, agentName]);

  // Follow the reply as it grows, unless the person scrolled up to read.
  // Keyed on the last message, not the count: earlier messages arriving on
  // top (`loadOlder`) are not something new at the bottom.
  // The following itself is `useBottomAnchor`, which also keeps the newest
  // message above the keyboard and a growing composer, and follows a reply's
  // buttons appearing once it finishes; here only the dot on the jump button.
  const last = state.messages[state.messages.length - 1];
  const growth = `${last?.key ?? ""}:${last?.body.length ?? 0}:${last?.tools.length ?? 0}:${last?.approval?.state ?? ""}`;
  useEffect(() => {
    if (!stick.current && state.messages.length) setUnseen(true);
  }, [growth]);
  const list = useRef<HTMLDivElement>(null);
  const scrollSettled = useBottomAnchor(content, list, stick);

  // Earlier messages load before the person reaches the top: once they are
  // within PREFETCH_SCREENS screens of it (the scroll handler below), or
  // straight away when what is held does not fill that much.
  const scroller = useRef<HTMLElement | null>(null);
  // Content too short to scroll sends no scroll events, so the check below
  // must also run once the scroll element is known.
  const [scrollerReady, setScrollerReady] = useState(false);
  useEffect(() => {
    void content.current?.getScrollElement().then((el) => {
      scroller.current = el;
      setScrollerReady(true);
    });
  }, []);
  const nearTop = (el: HTMLElement) => el.scrollTop < el.clientHeight * PREFETCH_SCREENS;
  useEffect(() => {
    const el = scroller.current;
    if (!state.hasOlder || state.loading || !el || !el.clientHeight) return;
    if (el.scrollHeight - el.clientHeight < el.clientHeight * PREFETCH_SCREENS) void conversation.loadOlder();
  }, [state.hasOlder, state.loading, state.messages.length, conversation, scrollerReady]);

  // Messages put on top must not move what the person is reading. Measured
  // from the bottom, which an insertion above leaves alone: the distance is
  // read while the old list is still on screen (this render) and restored
  // once the new one is (the layout effect), before anything is painted.
  const firstKey = state.messages[0]?.key;
  const shownFirst = useRef(firstKey);
  const keepFromBottom = useRef<number | null>(null);
  if (firstKey !== shownFirst.current) {
    const el = scroller.current;
    const before = shownFirst.current;
    if (el && before && state.messages.some((m) => m.key === before)) keepFromBottom.current = el.scrollHeight - el.scrollTop;
    shownFirst.current = firstKey;
  }
  useLayoutEffect(() => {
    const el = scroller.current;
    const fromBottom = keepFromBottom.current;
    keepFromBottom.current = null;
    if (el && fromBottom !== null) el.scrollTop = el.scrollHeight - fromBottom;
  }, [firstKey]);

  function jumpToLatest() {
    stick.current = true;
    setAway(false);
    setUnseen(false);
    void content.current?.scrollToBottom(300);
  }

  // On screen (a page that slid away, or another tab's, stays mounted).
  const [entered, setEntered] = useState(false);
  useIonViewDidEnter(() => {
    setEntered(true);
    void content.current?.scrollToBottom(0);
  });
  useIonViewDidLeave(() => setEntered(false));
  // The page the address names: an earlier conversation of the same agent
  // left on the stack can keep `entered` after a push to another of its
  // threads (the leave event does not always reach it), and a request for the
  // agent then landed on that hidden page, hands-free listening where nobody
  // was looking. The location is the app's, the params this page's own.
  const location = useLocation();
  const onScreen = entered && isPageFor(location.pathname, agentName, thread);

  // Opened by "Talk to an Agent" (Siri, the Action button, a launcher
  // shortcut): start Hands-Free Mode once the conversation is on screen and
  // loaded, so the microphone opens on the page the person is looking at.
  const talk = useStore(talkRequest);
  const startHandsFree = handsFree.start;
  useEffect(() => {
    if (!talk || talk.agent !== agentName || !onScreen || state.loading) return;
    if (takeTalkFor(agentName)) startHandsFree();
  }, [talk, agentName, onScreen, state.loading, startHandsFree]);

  // Opened by the Ask widget's buttons (`olano://open?...&compose=`): the
  // composer is readied once the conversation is on screen, once per arrival.
  const composeMode = composeModeOf(location.search);
  const composed = useRef<string | null>(null);
  useEffect(() => {
    if (!composeMode || !onScreen || state.loading || composed.current === location.key) return;
    composed.current = location.key;
    if (composeMode === "text") window.setTimeout(() => void input.current?.setFocus(), 150);
    else void pick(composeMode === "camera" ? "camera" : "library");
  }, [composeMode, onScreen, state.loading, location.key]);

  /** Edit: the message's words back in the box, to correct and send again. */
  const edit = useCallback((text: string) => {
    setDraft(text);
    setEditing(true);
    void haptic("light");
    window.setTimeout(() => void input.current?.setFocus(), 50);
  }, []);

  function stopEditing() {
    setEditing(false);
    setDraft("");
  }

  /** While the agent works: hand the words to it now (steer), or keep them for after (queue). */
  function addWhileWorking(kind: "steer" | "queue", words?: string) {
    const text = (words ?? draft).trim();
    if (!text) return;
    setEditing(false);
    setDraft("");
    stick.current = true;
    void haptic("light");
    void conversation.addWhileWorking(text, kind);
  }

  /** Sends the box (or `words`, the box's text from a dictation that just stopped). */
  async function send(words?: string) {
    const text = (words ?? draft).trim();
    if (state.busy && text && !ready.length && !files.length) {
      addWhileWorking("steer", text);
      return;
    }
    if ((!text && !ready.length && !files.length) || preparing || state.busy) return;
    const sending = staged.photos;
    const sendingFiles = files;
    setEditing(false);
    setDraft("");
    staged.setPhotos([]);
    setFiles([]);
    stick.current = true;
    void haptic("light");
    const ok = await conversation.send(
      text,
      ready.map((p) => p.prepared!),
      undefined,
      undefined,
      sendingFiles,
    );
    if (!ok) {
      // Not sent: the words, photos and files go back where they were.
      setDraft((current) => current || text);
      staged.setPhotos((current) => (current.length ? current : sending));
      setFiles((current) => (current.length ? current : sendingFiles));
    }
  }

  /** Dictation's stop: the words stay in the box to edit. */
  async function stopDictating() {
    const words = await dictation.finish();
    if (words !== null) keepDictated(words);
  }

  /** Dictation's send: the words, after anything typed before, go as the message. */
  async function sendDictated() {
    const words = await dictation.finish();
    if (words === null) return;
    const message = withDictation(draft, words);
    // In the box first: if the message cannot go yet (photos still being
    // prepared), the words wait there rather than vanish.
    setDraft(message);
    if (message.trim() || ready.length || files.length) await send(message);
  }

  async function pick(source: PhotoSource) {
    const room = MAX_PHOTOS - staged.photos.length;
    if (room <= 0) {
      problem(`A message can carry up to ${MAX_PHOTOS} photos.`);
      return;
    }
    try {
      staged.addBlobs(await pickPhotos(source, room));
    } catch (error) {
      problem(error instanceof PhotoError ? error.message : "The photos could not be opened.");
    }
  }

  /** How the agent goes about requests here: auto, plan first, or ask before each step. */
  function chooseMode() {
    void presentSheet({
      header: "How should the agent work?",
      buttons: [
        ...MODES.map((m) => ({
          text: `${m.label}${state.mode === m.mode ? " ✓" : ""}`,
          handler: () => {
            conversation.setMode(m.mode);
            void haptic("light");
            void presentToast({ message: `${m.label}: ${m.help}`, duration: 2500, position: "top", color: "dark" });
          },
        })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  async function compact() {
    const threadId = state.threadId;
    if (!threadId) return;
    void presentToast({ message: "Compacting the conversation…", duration: 1500, position: "top", color: "dark" });
    try {
      const result = await currentApi().compactThread(agentName, threadId);
      await conversation.load(true);
      void presentToast({
        message: result.compacted ? "Done. Earlier messages are now a summary the agent keeps." : "Nothing to compact yet.",
        duration: 2500,
        position: "top",
        color: "dark",
      });
    } catch (error) {
      problem(messageOf(error, "The conversation could not be compacted."));
    }
  }

  function rename() {
    const threadId = state.threadId;
    if (!threadId) return;
    void presentAlert({
      header: "Rename conversation",
      inputs: [{ name: "name", type: "text", placeholder: "Name", attributes: { maxlength: 80 } }],
      buttons: [
        { text: "Cancel", role: "cancel" },
        {
          text: "Save",
          handler: (values: { name?: string }) => {
            const name = (values?.name ?? "").trim();
            if (!name) return;
            void currentApi()
              .renameThread(agentName, threadId, name)
              .then(() => {
                invalidate(`threads:${agentName}`);
                void presentToast({ message: `Renamed to "${name}"`, duration: 1800, position: "top", color: "dark" });
              })
              .catch((error) => problem(messageOf(error, "The conversation could not be renamed.")));
          },
        },
      ],
    });
  }

  function openInfo() {
    void haptic("light");
    router.push(`/chats/${encodeURIComponent(agentName)}/_info`);
  }

  /**
   * A second agent joins: the conversation becomes a group (this agent leads
   * it) with everything said so far copied in, and the group opens.
   */
  function addAgent() {
    const threadId = state.threadId;
    const others = (agents.data ?? []).filter((a) => a.name !== agentName);
    if (!threadId || !others.length) return;
    void presentSheet({
      header: "Add an agent to this chat",
      subHeader: "It becomes a group chat with the conversation so far, so nobody has to repeat it.",
      buttons: [
        ...others.map((other) => ({
          text: displayName(other),
          handler: () => {
            void currentApi()
              .groupFromThread(agentName, threadId, [other.name])
              .then((group) => {
                void haptic("success");
                invalidate("groups");
                router.push(groupPath(group.id), "forward", "push");
              })
              .catch((error) => problem(messageOf(error, "The group could not be made.")));
          },
        })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  /** The conversation's menu (chat/menu.ts): this conversation, the agent, then the rest. */
  const [menuOpen, setMenuOpen] = useState(false);
  function openMenu() {
    void haptic("light");
    setMenuOpen(true);
  }
  const menuSections = conversationMenu({
    hasThread: !!state.threadId,
    modeLabel: modeLabel(state.mode),
    canAddAgent: !!groupsEnabled && (agents.data ?? []).some((a) => a.name !== agentName),
    handsFreeOn,
    showFiles: layout.settingsTab.files,
  });
  function onMenuAction(action: MenuAction) {
    const base = `/chats/${encodeURIComponent(agentName)}`;
    switch (action) {
      case "new":
        return router.push(`${base}/new`, "forward", "replace");
      case "mode":
        return chooseMode();
      case "rename":
        return rename();
      case "compact":
        return void compact();
      case "conversations":
        return goTo(router, base);
      case "files":
        return router.push(`${base}/_files`);
      case "scheduled":
        return router.push(`${base}/_scheduled`);
      case "info":
        return openInfo();
      case "handsFree":
        // Inside the tap: the phone only opens the microphone for a gesture.
        return handsFree.start();
      case "addAgent":
        return addAgent();
    }
  }

  function attach() {
    void haptic("light");
    void presentSheet({
      header: staged.photos.length ? `${staged.photos.length} of ${MAX_PHOTOS} photos` : undefined,
      buttons: [
        { text: "Take Photo", icon: cameraOutline, handler: () => void pick("camera") },
        { text: "Choose Photos", icon: imagesOutline, handler: () => void pick("library") },
        { text: "Choose Files", icon: documentOutline, handler: () => filePicker.current?.click() },
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  const trayPhotos: MessagePhoto[] = staged.photos.map((p) => ({ key: p.id, name: "Photo", src: p.preview }));

  const title = agent ? displayName(agent) : agentName;
  // An agent that will not answer (stopped, failed to start, held back) gets
  // a banner where the composer was, instead of a "Say hello" screen that
  // takes a message and then reports the agent missing. The agent list
  // decides; a refusal from the deployment covers a list that is behind.
  const health = agent ? agentHealth(agent) : null;
  const notice = (health && unavailableNotice(title, health, admin)) ?? state.unavailable;
  // A fresh list that says it runs again (started here, in the dashboard or
  // on another phone) ends the refusal the last send got.
  const runsNow = health?.state === "running";
  useEffect(() => {
    if (runsNow) conversation.clearUnavailable();
  }, [runsNow, agents.data, conversation]);

  async function startHere() {
    setStarting(true);
    try {
      await currentApi().startAgent(agentName);
      void haptic("success");
      conversation.clearUnavailable();
      void presentToast({ message: `${title} is starting.`, duration: 2000, position: "top", color: "dark" });
    } catch (error) {
      void presentToast({ message: messageOf(error, `${title} could not be started.`), duration: 5000, position: "top", color: "danger" });
    } finally {
      setStarting(false);
      invalidateResource("agents");
    }
  }
  const agentModel = agent?.model ?? "";
  const shownModel = state.model || agentModel;
  const levels = reasoningLevels(catalog.data, shownModel);
  const shownLevel = state.reasoningEffort || levels?.defaultLevel || "";
  const modelLine = shownModel
    ? `${modelLabel(shownModel)}${levels && shownLevel ? ` · ${levelLabel(shownLevel)}` : ""}`
    : "Choose a model";
  const showTyping = state.busy && last?.role === "assistant" && !last.body && !last.tools.some((t) => !t.hasResult) && !last.approval;
  const browserApproval = last?.approval?.state === "pending" && last.approval.actions.some((a) => a.name.startsWith("browser_"));

  const messages = state.messages;
  // The person's buttons go on their latest message; Try again goes on a
  // failed last reply when the message before it was words alone.
  let lastMine = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user") {
      lastMine = i;
      break;
    }
  }
  const failed = last?.role === "assistant" && !!last.error && !state.busy ? last : null;
  const failedAfter = failed && lastMine >= 0 ? messages[lastMine] : null;
  const retryable =
    failedAfter && failedAfter.body.trim() && !failedAfter.photos?.length && !failedAfter.voice?.length && !failedAfter.media?.length
      ? failedAfter.body.trim()
      : "";
  const retry = useCallback(() => {
    if (!retryable) return;
    stick.current = true;
    void haptic("light");
    void conversation.send(retryable);
  }, [conversation, retryable]);

  return (
    <IonPage
      className="olano-conversation"
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
      }}
      onDrop={(e) => {
        const dropped = Array.from(e.dataTransfer?.files ?? []);
        if (!dropped.length) return;
        e.preventDefault();
        const images = dropped.filter((f) => f.type.startsWith("image/"));
        if (images.length) staged.addBlobs(images);
        void addFiles(dropped.filter((f) => !f.type.startsWith("image/")));
      }}
    >
      <IonHeader translucent>
        <IonToolbar>
          <IonButtons slot="start">
            <IonBackButton defaultHref="/chats" text="" />
          </IonButtons>
          <IonTitle>
            <div className="olano-chat-title">
              {agent && (
                <button type="button" className="olano-chat-title-avatar" aria-label={`About ${title}`} onClick={openInfo}>
                  <Avatar agent={agent} size={28} />
                </button>
              )}
              <div className="olano-chat-title-text">
                <span onClick={openInfo}>{title}</span>
              </div>
            </div>
          </IonTitle>
          <IonButtons slot="end">
            {liveSession && (
              <IonButton
                aria-label="The agent's browser"
                className={liveSession.attention ? "olano-browser-button attention" : "olano-browser-button"}
                onClick={() => setBrowserOpen(true)}
              >
                <IonIcon slot="icon-only" icon={globeOutline} />
              </IonButton>
            )}
            <IonButton aria-label="Conversation menu" onClick={openMenu}>
              <IonIcon slot="icon-only" icon={ellipsisHorizontal} />
            </IonButton>
          </IonButtons>
        </IonToolbar>
      </IonHeader>

      {showPip && (
        <BrowserPip
          browser={browser}
          attention={liveSession?.attention ?? null}
          approvalPending={browserApproval}
          onOpen={() => setBrowserOpen(true)}
          onHide={() => setHiddenFor(dismissKey(liveSession))}
        />
      )}

      <IonContent
        ref={content}
        fullscreen
        scrollEvents
        className="olano-chat-content"
        onIonScroll={async (e) => {
          const el = await content.current?.getScrollElement();
          if (!el) return;
          scroller.current = el;
          if (state.hasOlder && nearTop(el)) void conversation.loadOlder();
          // A scroll the browser made while resizing is not the person
          // moving: leave the follow flag to useBottomAnchor's answer.
          if (!scrollSettled(el)) return;
          const fromBottom = el.scrollHeight - (e.detail.scrollTop + el.clientHeight);
          stick.current = fromBottom < 80;
          setAway(fromBottom > JUMP_AFTER_PX);
          if (stick.current) setUnseen(false);
        }}
      >
        <div className="olano-messages" ref={list}>
          {state.loading && (
            <div className="olano-center">
              <IonSpinner name="crescent" />
            </div>
          )}
          {state.loadError && <ErrorNote message={state.loadError} onRetry={() => void conversation.load(true)} />}
          {state.hasOlder && (
            <div className="olano-older" aria-live="polite">
              {state.olderError ? (
                <button type="button" className="olano-older-retry" onClick={() => void conversation.loadOlder(true)}>
                  Couldn't load earlier messages. Tap to try again.
                </button>
              ) : (
                // Kept the same height when idle, so the page above landing
                // (which starts two screens before here) never shifts the list.
                state.loadingOlder && <IonSpinner name="dots" aria-label="Loading earlier messages" />
              )}
            </div>
          )}
          {!state.hasOlder && state.reachedStart && messages.length > 0 && (
            <div className="olano-older olano-older-start" role="note">
              Start of conversation
            </div>
          )}
          {!state.loading && !state.loadError && messages.length === 0 && agent && notice && (
            <Empty title={notice.title} body={notice.body}>
              <div className="olano-empty-avatar">
                <Avatar agent={agent} size={64} />
              </div>
            </Empty>
          )}
          {!state.loading && !state.loadError && messages.length === 0 && agent && !notice && (
            <Empty title={`Say hello to ${title}`} body={agent.title || agent.description || "Ask anything."}>
              <div className="olano-empty-avatar">
                <Avatar agent={agent} size={64} />
              </div>
              <div className="olano-starters" role="list" aria-label="Suggestions">
                {STARTERS.map((starter) => (
                  <button
                    key={starter}
                    type="button"
                    role="listitem"
                    className="olano-starter"
                    onClick={() => {
                      stick.current = true;
                      void haptic("light");
                      void conversation.send(starter);
                    }}
                  >
                    {starter}
                  </button>
                ))}
              </div>
            </Empty>
          )}
          {messages.map((message, i) => {
            const previous = messages[i - 1];
            const newDay = message.ts !== undefined && (previous?.ts === undefined || !sameDay(previous.ts, message.ts));
            return (
              <Fragment key={message.key}>
                {newDay && (
                  <div className="olano-day" role="separator">
                    <span>{dayLabel(message.ts)}</span>
                  </div>
                )}
                <MessageView
                  message={message}
                  agent={agent}
                  readAs={agentName}
                  previous={newDay ? undefined : previous}
                  onAnswer={(approve) => void conversation.answerApproval(approve)}
                  onEdit={edit}
                  onRetry={message === failed && retryable ? retry : undefined}
                  onReport={
                    message.role === "assistant" && message.turnId && state.threadId && !message.streaming
                      ? (reason) => currentApi().reportReply(agentName, state.threadId as string, message.turnId as string, reason)
                      : undefined
                  }
                  onApprovePlan={
                    i === messages.length - 1 && !state.busy
                      ? () => {
                          stick.current = true;
                          void haptic("medium");
                          void conversation.approvePlan();
                        }
                      : undefined
                  }
                  showActions={i === lastMine && !state.busy}
                />
              </Fragment>
            );
          })}
          {showTyping && (
            <div className="olano-typing" aria-label={`${title} is working`}>
              <span />
              <span />
              <span />
            </div>
          )}
        </div>
      </IonContent>

      <IonFooter className="olano-composer">
        {away && (
          <button type="button" className="olano-jump" aria-label="Jump to the latest message" onClick={jumpToLatest}>
            <IonIcon icon={chevronDown} />
            {unseen && <span className="olano-jump-dot" />}
          </button>
        )}
        {notice ? (
          <div className="olano-unavailable" role="status">
            <div className="olano-unavailable-text">
              <strong>{notice.title}</strong>
              {/* The empty screen already says it all; under history, say why here. */}
              {messages.length > 0 && <span>{notice.body}</span>}
            </div>
            {notice.canStart && (
              <IonButton size="small" disabled={starting} onClick={() => void startHere()}>
                {starting ? "Starting…" : "Start"}
              </IonButton>
            )}
          </div>
        ) : (
          <>
        {state.sendError && <div className="olano-send-error">{state.sendError}</div>}
        {state.mode !== "auto" && !recording && (
          <button type="button" className="olano-mode-bar" onClick={chooseMode}>
            <strong>{modeLabel(state.mode)}</strong>
            <span>{MODES.find((m) => m.mode === state.mode)?.help}</span>
          </button>
        )}
        {state.queue.length > 0 && (
          <div className="olano-queue" role="list" aria-label="Waiting to be read">
            {state.queue.map((item) => (
              <div key={item.id} className="olano-queue-item" role="listitem">
                <IonIcon icon={item.kind === "steer" ? flashOutline : timeOutline} aria-hidden="true" />
                <div className="olano-queue-text">
                  <span>{item.text}</span>
                  <small>{item.kind === "steer" ? "The agent reads this at its next step" : "Sends when this reply is done"}</small>
                </div>
                <button
                  type="button"
                  aria-label="Edit waiting message"
                  onClick={() => {
                    const text = conversation.takeBack(item.id);
                    if (text) setDraft((current) => (current ? `${current} ${text}` : text));
                  }}
                >
                  <IonIcon icon={createOutline} />
                </button>
                <button type="button" aria-label="Cancel waiting message" onClick={() => void conversation.takeBack(item.id)}>
                  <IonIcon icon={close} />
                </button>
              </div>
            ))}
          </div>
        )}
        {state.busy && draft.trim() && !recording && (
          <div className="olano-working-bar">
            <span>{title} is working. Send adds this to what it's doing.</span>
            <button type="button" onClick={() => addWhileWorking("queue")}>
              Send after
            </button>
            <button type="button" className="stop" onClick={() => void conversation.stop()}>
              Stop
            </button>
          </div>
        )}
        {editing && !recording && (
          <div className="olano-editing">
            <IonIcon icon={createOutline} aria-hidden="true" />
            <span>Editing your message. It sends as a new message.</span>
            <button type="button" aria-label="Cancel editing" onClick={stopEditing}>
              <IonIcon icon={close} />
            </button>
          </div>
        )}
        {handsFreeOn && <HandsFreePanel handsFree={handsFree} agent={title} />}
        <IonToolbar className={handsFreeOn ? "olano-composer-away" : undefined}>
          <div className={recording ? "olano-composer-card recording" : dictating ? "olano-composer-card dictating" : "olano-composer-card"}>
            {!recording && (
              <>
                <input
                  ref={filePicker}
                  type="file"
                  multiple
                  hidden
                  data-testid="file-picker"
                  onChange={(e) => {
                    const chosen = Array.from(e.currentTarget.files ?? []);
                    e.currentTarget.value = "";
                    void addFiles(chosen);
                  }}
                />
                <div
                  className={staged.photos.length || files.length ? "olano-composer-field with-photos" : "olano-composer-field"}
                  onPaste={(e) => {
                    const images = imagesFromClipboard(e.clipboardData);
                    if (!images.length) return;
                    e.preventDefault();
                    staged.addBlobs(images);
                  }}
                >
                  {staged.photos.length > 0 && (
                    <div className="olano-tray" role="list" aria-label="Photos to send">
                      {staged.photos.map((photo, i) => (
                        <div key={photo.id} className={`olano-tray-item ${photo.status}`} role="listitem">
                          <button
                            type="button"
                            className="olano-tray-thumb"
                            aria-label={`Photo ${i + 1} of ${staged.photos.length}`}
                            onClick={(e) => openPhotoViewer(trayPhotos, i, e.currentTarget)}
                          >
                            <img src={photo.preview} alt="" draggable={false} />
                            {photo.status === "preparing" && <IonSpinner name="crescent" />}
                          </button>
                          <button
                            type="button"
                            className="olano-tray-remove"
                            aria-label={`Remove photo ${i + 1}`}
                            onClick={() => {
                              void haptic("light");
                              staged.remove(photo.id);
                            }}
                          >
                            <IonIcon icon={close} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  {files.length > 0 && (
                    <div className="olano-tray-files" role="list" aria-label="Files to send">
                      {files.map((file) => (
                        <div key={file.id} className="olano-tray-file" role="listitem">
                          <IonIcon icon={documentOutline} aria-hidden="true" />
                          <span className="name">{file.name}</span>
                          <span className="size">{fmtSize(file.size)}</span>
                          <button
                            type="button"
                            aria-label={`Remove ${file.name}`}
                            onClick={() => setFiles((current) => current.filter((f) => f.id !== file.id))}
                          >
                            <IonIcon icon={close} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  {dictating ? (
                    <DictationField draft={draft} dictation={dictation} />
                  ) : (
                    <IonTextarea
                      ref={input}
                      className="olano-composer-input"
                      placeholder={staged.photos.length || files.length ? "Add a message or send" : `Message ${title}`}
                      autoGrow
                      rows={1}
                      value={draft}
                      autocapitalize="sentences"
                      enterkeyhint="enter"
                      onIonInput={(e) => setDraft(String(e.detail.value ?? ""))}
                      onKeyDown={(e) => {
                        // The phone's return key breaks the line and the send button sends; a
                        // hardware keyboard (iPad, Android tablet): Enter sends, Shift+Enter breaks the line.
                        if (enterSends({ key: e.key, shiftKey: e.shiftKey, isComposing: e.nativeEvent.isComposing })) {
                          e.preventDefault();
                          void send();
                        }
                      }}
                    />
                  )}
                </div>
              </>
            )}
            {dictating ? (
              <DictationControls dictation={dictation} onStop={() => void stopDictating()} onSend={() => void sendDictated()} />
            ) : (
              <div className="olano-composer-row">
                {recording ? (
                  <RecordingStrip capture={capture} />
                ) : (
                  <>
                    <button type="button" className="olano-attach" aria-label="Add photos or files" onClick={attach}>
                      <IonIcon icon={add} />
                    </button>
                    <button
                      type="button"
                      className={state.model || state.reasoningEffort ? "olano-model-chip picked" : "olano-model-chip"}
                      aria-label={`Model: ${modelLine}. Change it`}
                      onClick={() => {
                        void haptic("light");
                        setModelsOpen(true);
                      }}
                    >
                      <span className="name">{shownModel ? modelLabel(shownModel) : "Choose a model"}</span>
                      {shownModel && levels && shownLevel && <span className="level">{levelLabel(shownLevel)}</span>}
                    </button>
                    <span className="olano-composer-gap" />
                  </>
                )}
                {/* Where a tap dictates, the microphone stays next to the words: what is said is added to them. */}
                {(recording || (!state.busy && (dictationOffered || (!canSend && MIC_AVAILABLE)))) && <MicButton capture={capture} />}
                {recording ? null : state.busy && !draft.trim() ? (
                  <IonButton shape="round" color="medium" className="olano-send" aria-label="Stop" onClick={() => void conversation.stop()}>
                    <IonIcon slot="icon-only" icon={stop} />
                  </IonButton>
                ) : (
                  <IonButton
                    shape="round"
                    className="olano-send"
                    aria-label={state.busy ? "Add to what the agent is doing" : "Send"}
                    disabled={preparing || (!draft.trim() && !ready.length && !files.length)}
                    onClick={() => void send()}
                  >
                    <IonIcon slot="icon-only" icon={arrowUp} />
                  </IonButton>
                )}
              </div>
            )}
          </div>
        </IonToolbar>
          </>
        )}
      </IonFooter>
      <BrowserView
        open={browserOpen}
        onClose={() => setBrowserOpen(false)}
        browser={browser}
        attention={liveSession?.attention ?? null}
        approvalPending={browserApproval}
        onHandedBack={() => {
          stick.current = true;
          void conversation.say(HANDED_BACK);
        }}
      />
      <MenuSheet
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        sections={menuSections}
        onAction={onMenuAction}
        header={
          <>
            {agent && <Avatar agent={agent} size={44} />}
            <div>
              <h2>{title}</h2>
              <p>{modelLine}</p>
            </div>
          </>
        }
      />
      <ModelSheet
        open={modelsOpen}
        onClose={() => setModelsOpen(false)}
        catalog={catalog.data}
        catalogError={catalog.error}
        onRetry={() => void reloadCatalog()}
        choices={choices}
        agentModel={agentModel}
        model={state.model}
        level={state.reasoningEffort}
        onPick={(model, level) => conversation.setModel(model, level)}
      />
    </IonPage>
  );
}
