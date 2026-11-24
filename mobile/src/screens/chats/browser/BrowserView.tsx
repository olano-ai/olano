/**
 * The agent's browser, full screen: watch it work, or take control to do
 * something only a person should (sign in, solve a check, approve on a
 * site), then hand it back.
 *
 * While the agent has control the person watches; "Take control" pauses the
 * agent's browsing (its browser tools refuse until it is handed back) and
 * the picture takes taps, swipes and typing. The page's own dialogs, file
 * pickers and drop-down lists come to the person while they have control.
 * Handing back after the agent asked for help also tells the agent, in the
 * conversation, that the person is done.
 */

import {
  IonButton,
  IonButtons,
  IonContent,
  IonFooter,
  IonHeader,
  IonIcon,
  IonAlert,
  IonInput,
  IonModal,
  IonToast,
  IonSpinner,
  IonTitle,
  IonToolbar,
  useIonActionSheet,
  useIonAlert,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import {
  arrowBack,
  arrowForward,
  backspaceOutline,
  cameraOutline,
  handLeftOutline,
  keypadOutline,
  layersOutline,
  radioButtonOn,
  refresh,
  returnDownBackOutline,
  send,
  stopCircle,
} from "ionicons/icons";
import { useEffect, useMemo, useRef, useState } from "react";

import type { LiveBrowser, LiveView } from "../../../browser/live.ts";
import {
  addressFor,
  pickerInputType,
  pickerTitle,
  recordingClock,
  savedFileLine,
  siteOf,
  statusLine,
  type LivePicker,
  type SavedBrowserFile,
} from "../../../browser/model.ts";
import { haptic } from "../../../native/haptics.ts";
import { messageOf } from "../../../native/shell.ts";
import { currentApi } from "../../../state/session.ts";
import { useStore } from "../../../state/store.ts";
import { copyText } from "../../../ui/messageActions.ts";
import { LivePicture } from "./LivePicture.tsx";
import { useAppLayout } from "../../../state/layoutStore.ts";

/** The message a person's hand-back sends when the agent had asked for them (the dashboard's words). */
export const HANDED_BACK = "Done: I've handed the browser back to you.";

const MAX_FILES_BYTES = 10 * 1024 * 1024;

export function BrowserView({
  open,
  onClose,
  browser,
  attention,
  approvalPending,
  onHandedBack,
}: {
  open: boolean;
  onClose: () => void;
  browser: LiveBrowser;
  /** Why the agent asked for a person, if it did (from the session status). */
  attention: string | null;
  /** The agent waits for an approval in the conversation. */
  approvalPending: boolean;
  /** Called when the person hands back after the agent asked for them. */
  onHandedBack: () => void;
}) {
  const view = useStore(browser.store);
  const [typing, setTyping] = useState(false);
  const [text, setText] = useState("");
  const [address, setAddress] = useState<string | null>(null);
  const askedWhenTaken = useRef(false);
  const files = useRef<HTMLInputElement>(null);
  const [alert] = useIonAlert();
  const [sheet] = useIonActionSheet();
  const [toast] = useIonToast();
  const shownDialog = useRef<string | null>(null);
  const router = useIonRouter();
  const openAfter = useRef<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    browser.acquire();
    return () => browser.release();
  }, [open, browser]);

  const state = view.state;
  const inControl = state?.controller === "user";
  const asking = state?.attention ?? attention;
  const line = statusLine(state?.controller, asking, approvalPending);
  const tabs = state?.tabs ?? [];

  useEffect(() => {
    if (!inControl) setTyping(false);
  }, [inControl]);

  // The page's own messages: shown once each, while the person has control.
  usePageRequests(browser, view, inControl, shownDialog, alert, sheet, files);

  useEffect(() => {
    if (!view.notice) return;
    void toast({ message: view.notice, duration: 2500, position: "top", color: "dark" });
    browser.clearNotice();
  }, [view.notice, browser, toast]);

  useEffect(() => {
    if (view.clipboard === null) return;
    void copyText(view.clipboard).then(() => toast({ message: "Copied from the page", duration: 1400, position: "top", color: "dark" }));
    browser.clearClipboard();
  }, [view.clipboard, browser, toast]);

  function take() {
    askedWhenTaken.current = !!asking;
    browser.takeControl();
    void haptic("medium");
  }

  function handBack() {
    browser.handBack();
    void haptic("light");
    if (askedWhenTaken.current) {
      askedWhenTaken.current = false;
      onHandedBack();
    }
  }

  function sendText() {
    if (!text) return;
    browser.send({ t: "text", text });
    setText("");
  }

  function key(name: string) {
    browser.send({ t: "keydown", key: name });
    browser.send({ t: "keyup", key: name });
  }

  function navigate() {
    const url = addressFor(address ?? "");
    setAddress(null);
    if (url) browser.send({ t: "navigate", url });
  }

  const recording = state?.recording ?? null;

  // A file the browser saved: a toast says where it went and offers the
  // Files screen. Declared, not presented from the hook: an overlay presented
  // from a hook in this view is gone a moment later (the picker was too).
  // Each save is its own toast (keyed by `n`): one toast reused across saves
  // kept the first one's timer, so a picture saved right after a recording
  // was told for a moment, or not at all when it landed while that toast was
  // leaving (its dismissal then cleared the new file).
  const [lastSaved, setLastSaved] = useState<{ file: SavedBrowserFile; n: number } | null>(null);
  const savedCount = useRef(0);
  // The toast sits under the header, never on it: on top it covered Done and
  // the picture and record buttons for its whole six seconds.
  const header = useRef<HTMLIonHeaderElement>(null);
  const showFiles = useAppLayout().settingsTab.files;
  const savedButtons = useMemo(() => {
    const file = lastSaved?.file;
    if (!file || !showFiles) return [];
    const folder = file.relative.includes("/") ? file.relative.slice(0, file.relative.lastIndexOf("/")) : "";
    return [
      {
        text: "Open",
        handler: () => {
          // Opened once the browser view has gone, never under it closing.
          openAfter.current = `/chats/${encodeURIComponent(browser.agent)}/_files${folder ? `/${encodeURIComponent(folder)}` : ""}`;
          onClose();
        },
      },
    ];
  }, [lastSaved, showFiles, browser, onClose, router]);
  function saved(file: SavedBrowserFile) {
    void haptic("light");
    savedCount.current += 1;
    setLastSaved({ file, n: savedCount.current });
  }

  /** Record the browser to a video, or stop and save it. */
  async function toggleRecording() {
    if (saving) return;
    setSaving(true);
    try {
      const result = await currentApi().browserRecord(browser.agent, recording ? "stop" : "start");
      if (result.file) saved(result.file);
      else if (result.recording) void haptic("medium");
    } catch (error) {
      void toast({ message: messageOf(error, "The browser could not be recorded."), duration: 3500, position: "top", color: "dark" });
    } finally {
      setSaving(false);
    }
  }

  /** Save the page as a picture. */
  async function takePicture() {
    if (saving) return;
    setSaving(true);
    try {
      saved((await currentApi().browserPicture(browser.agent)).file);
    } catch (error) {
      void toast({ message: messageOf(error, "The picture could not be saved."), duration: 3500, position: "top", color: "dark" });
    } finally {
      setSaving(false);
    }
  }

  function chooseTab() {
    void sheet({
      header: "Tabs",
      buttons: [
        ...tabs.map((tab) => ({ text: `${tab.active ? "✓ " : ""}${tab.title || siteOf(tab.url) || "New tab"}`, handler: () => browser.send({ t: "tab", id: tab.id }) })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }

  return (
    <IonModal
      isOpen={open}
      onDidDismiss={() => {
        onClose();
        const next = openAfter.current;
        openAfter.current = null;
        if (next) window.setTimeout(() => router.push(next, "forward", "push"), 0);
      }}
      className="olano-browser-view"
    >
      <IonHeader ref={header}>
        <IonToolbar>
          <IonButtons slot="start">
            <IonButton onClick={onClose}>Done</IonButton>
          </IonButtons>
          <IonTitle>
            <span className="olano-browser-title">
              {siteOf(state?.url) || "Browser"}
              {state?.loading && <IonSpinner name="crescent" />}
            </span>
          </IonTitle>
          <IonButtons slot="end">
            {view.phase === "open" && (
              <>
                <IonButton onClick={() => void takePicture()} disabled={saving} aria-label="Save a picture of the page">
                  <IonIcon slot="icon-only" icon={cameraOutline} />
                </IonButton>
                <IonButton
                  onClick={() => void toggleRecording()}
                  disabled={saving}
                  color={recording ? "danger" : undefined}
                  className={recording ? "olano-browser-recording" : undefined}
                  aria-label={recording ? "Stop recording" : "Record the browser"}
                >
                  <IonIcon slot="icon-only" icon={recording ? stopCircle : radioButtonOn} />
                </IonButton>
              </>
            )}
            {tabs.length > 1 && (
              <IonButton onClick={chooseTab} aria-label={`${tabs.length} tabs`}>
                <IonIcon slot="start" icon={layersOutline} />
                {tabs.length}
              </IonButton>
            )}
          </IonButtons>
        </IonToolbar>
        <div className="olano-browser-address">
          {inControl && address !== null ? (
            <IonInput
              aria-label="Address"
              value={address}
              inputmode="url"
              enterkeyhint="go"
              autocapitalize="off"
              spellcheck={false}
              onIonInput={(e) => setAddress(String(e.detail.value ?? ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") navigate();
              }}
              onIonBlur={() => setAddress(null)}
            />
          ) : (
            <button type="button" disabled={!inControl} onClick={() => setAddress(state?.url ?? "")} aria-label="Address">
              {state?.url || " "}
            </button>
          )}
        </div>
      </IonHeader>
      <IonContent className="olano-browser-content" scrollY={false}>
        <div className="olano-browser-stage">
          {asking && !inControl && (
            <div className="olano-browser-banner attention">
              <IonIcon icon={handLeftOutline} aria-hidden="true" />
              <span>The agent asked for you: {asking}</span>
            </div>
          )}
          {approvalPending && (
            <div className="olano-browser-banner">
              <span>The agent is waiting for your OK in the chat.</span>
              <IonButton size="small" fill="clear" onClick={onClose}>
                Review
              </IonButton>
            </div>
          )}
          <LivePicture browser={browser} view={view} interactive={inControl} />
          {!view.frame && (
            <div className="olano-browser-empty">
              {view.phase === "closed" ? (
                <p>The agent's browser is closed.</p>
              ) : view.phase === "failed" ? (
                <>
                  <p>The browser could not be reached.</p>
                  <IonButton fill="outline" onClick={() => browser.retry()}>
                    Try again
                  </IonButton>
                </>
              ) : (
                <IonSpinner name="crescent" />
              )}
            </div>
          )}
        </div>
      </IonContent>
      <IonFooter className="olano-browser-footer">
        <div className={`olano-browser-status ${line.tone}`} role="status">
          <span className="dot" aria-hidden="true" />
          {line.text}
          {recording && <RecordingClock since={recording.since} />}
        </div>
        {inControl && typing && (
          <div className="olano-browser-keys">
            <IonInput
              aria-label="Type into the page"
              placeholder="Type into the page"
              value={text}
              autocapitalize="off"
              enterkeyhint="send"
              onIonInput={(e) => setText(String(e.detail.value ?? ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") sendText();
              }}
            />
            <button type="button" aria-label="Send the text" onClick={sendText}>
              <IonIcon icon={send} />
            </button>
            <button type="button" aria-label="Backspace" onClick={() => key("Backspace")}>
              <IonIcon icon={backspaceOutline} />
            </button>
            <button type="button" aria-label="Tab" onClick={() => key("Tab")}>
              Tab
            </button>
            <button type="button" aria-label="Enter" onClick={() => key("Enter")}>
              <IonIcon icon={returnDownBackOutline} />
            </button>
          </div>
        )}
        {inControl ? (
          <div className="olano-browser-controls">
            <button type="button" aria-label="Back" disabled={state?.can_back === false} onClick={() => browser.send({ t: "back" })}>
              <IonIcon icon={arrowBack} />
            </button>
            <button type="button" aria-label="Forward" disabled={state?.can_forward === false} onClick={() => browser.send({ t: "forward" })}>
              <IonIcon icon={arrowForward} />
            </button>
            <button type="button" aria-label="Reload" onClick={() => browser.send({ t: "reload" })}>
              <IonIcon icon={refresh} />
            </button>
            <button type="button" aria-label="Keyboard" aria-pressed={typing} className={typing ? "on" : undefined} onClick={() => setTyping(!typing)}>
              <IonIcon icon={keypadOutline} />
            </button>
            <IonButton className="olano-browser-handback" onClick={handBack}>
              Hand back
            </IonButton>
          </div>
        ) : (
          <div className="olano-browser-take">
            <IonButton expand="block" disabled={view.phase !== "open"} onClick={take}>
              <IonIcon slot="start" icon={handLeftOutline} />
              Take control
            </IonButton>
            <p>You're watching. Taking control pauses the agent's browsing until you hand it back.</p>
          </div>
        )}
      </IonFooter>
      <PickerAlert browser={browser} picker={view.picker} />
      <IonToast
        key={lastSaved?.n ?? 0}
        isOpen={!!lastSaved}
        message={lastSaved ? savedFileLine(lastSaved.file) : ""}
        duration={6000}
        position="top"
        positionAnchor={header.current ?? undefined}
        color="dark"
        buttons={savedButtons}
        onDidDismiss={() => {
          const shown = lastSaved?.n;
          setLastSaved((current) => (current && current.n === shown ? null : current));
        }}
      />
      <input
        ref={files}
        type="file"
        hidden
        onChange={(e) => {
          const chooser = view.state?.chooser;
          const list = Array.from(e.currentTarget.files ?? []);
          e.currentTarget.value = "";
          if (chooser) void sendFiles(browser, chooser.id, list, toast);
        }}
      />
    </IonModal>
  );
}

/** Dialogs, file pickers and drop-down lists the page opens while the person has control. */
function usePageRequests(
  browser: LiveBrowser,
  view: LiveView,
  inControl: boolean,
  shownDialog: React.RefObject<string | null>,
  alert: ReturnType<typeof useIonAlert>[0],
  sheet: ReturnType<typeof useIonActionSheet>[0],
  files: React.RefObject<HTMLInputElement | null>,
) {
  const dialog = view.state?.dialog;
  const chooser = view.state?.chooser;
  const shownChooser = useRef<string | null>(null);

  useEffect(() => {
    if (!inControl || !dialog || shownDialog.current === dialog.id) return;
    shownDialog.current = dialog.id;
    const prompt = dialog.kind === "prompt";
    const answer = (accept: boolean, text?: string) => browser.send({ t: "dialog", id: dialog.id, accept, ...(text !== undefined ? { text } : {}) });
    void alert({
      header: dialog.kind === "beforeunload" ? "Leave this page?" : "The page says",
      message: dialog.message,
      // Only a prompt has a field (an explicit undefined breaks Ionic's alert).
      ...(prompt ? { inputs: [{ name: "text", value: dialog.default ?? "" }] } : {}),
      backdropDismiss: false,
      buttons:
        dialog.kind === "alert"
          ? [{ text: "OK", handler: () => answer(true) }]
          : [
              { text: "Cancel", role: "cancel", handler: () => answer(false) },
              { text: "OK", handler: (values: { text?: string }) => answer(true, prompt ? String(values?.text ?? "") : undefined) },
            ],
    });
  }, [inControl, dialog, browser, alert, shownDialog]);

  useEffect(() => {
    if (!inControl || !chooser || shownChooser.current === chooser.id) return;
    shownChooser.current = chooser.id;
    const input = files.current;
    if (!input) return;
    input.multiple = chooser.multiple;
    input.accept = chooser.accept || "";
    input.click();
  }, [inControl, chooser, files]);

  const select = view.select;
  useEffect(() => {
    if (!select) return;
    browser.clearSelect();
    void sheet({
      header: select.label || "Choose",
      buttons: [
        ...select.options
          .map((option, index) => ({ option, index }))
          .filter(({ option }) => !option.disabled)
          .map(({ option, index }) => ({
            text: `${index === select.selected ? "✓ " : ""}${option.label}`,
            handler: () => browser.send({ t: "select", index }),
          })),
        { text: "Cancel", role: "cancel" },
      ],
    });
  }, [select, browser, sheet]);
}

async function sendFiles(
  browser: LiveBrowser,
  id: string,
  list: File[],
  toast: ReturnType<typeof useIonToast>[0],
): Promise<void> {
  const total = list.reduce((sum, file) => sum + file.size, 0);
  if (total > MAX_FILES_BYTES) {
    void toast({ message: "Files for the page can be 10 MB at most.", duration: 3000, position: "top", color: "dark" });
    browser.send({ t: "files", id, files: [] });
    return;
  }
  const encoded = await Promise.all(
    list.slice(0, 20).map(async (file) => {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      return { name: file.name, mime: file.type || "application/octet-stream", data: btoa(binary) };
    }),
  );
  browser.send({ t: "files", id, files: encoded });
}

/**
 * A date, time or colour field a tap landed on: the phone's own picker, since
 * the browser draws its own outside the picture. Declared rather than
 * presented from a hook: it stays until answered, whatever other overlay is
 * on its way out.
 */
function PickerAlert({ browser, picker }: { browser: LiveBrowser; picker: LivePicker | null }) {
  const colour = picker?.kind === "color";
  // The same arrays for as long as the picker is up: a new `inputs` array on
  // every render (a frame arrives several times a second) makes Ionic rebuild
  // the field with the page's value and throw away what was typed.
  const inputs = useMemo(() => {
    if (!picker) return [];
    // Built without undefined fields: an explicit undefined breaks Ionic's alert.
    const input: Record<string, unknown> = { name: "value", type: pickerInputType(picker.kind), value: picker.value };
    if (picker.kind === "color") input.placeholder = "#rrggbb";
    if (picker.min) input.min = picker.min;
    if (picker.max) input.max = picker.max;
    return [input];
  }, [picker]);
  const buttons = useMemo(
    () => [
      { text: "Cancel", role: "cancel" },
      {
        text: "Set",
        handler: (values: { value?: string }) => {
          browser.send({ t: "setvalue", value: String(values?.value ?? "").trim() });
        },
      },
    ],
    [browser],
  );
  return (
    <IonAlert
      isOpen={!!picker}
      header={picker ? pickerTitle(picker) : ""}
      {...(colour ? { message: "A colour as #rrggbb, for example #ff6a3d." } : {})}
      inputs={inputs}
      buttons={buttons}
      onDidDismiss={() => browser.clearPicker()}
    />
  );
}

/** "REC 0:42", counting while a recording runs. */
function RecordingClock({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span className="olano-browser-rec" aria-label="Recording">
      REC {recordingClock(now - since)}
    </span>
  );
}
