/**
 * One message. A person's message is a bubble on the right; the agent's is
 * on the left, with what it did on the way as a collapsed "N steps" line
 * (the running step shows by itself while it runs), its plan as a checklist,
 * and anything that needs an answer as a card.
 */

import { IonButton, IonIcon, IonSpinner, useIonActionSheet, useIonToast } from "@ionic/react";
import {
  albumsOutline,
  calendarOutline,
  checkmark,
  checkmarkCircle,
  chevronDown,
  chevronForward,
  documentTextOutline,
  ellipseOutline,
  globeOutline,
  imageOutline,
  keyOutline,
  mailOutline,
  peopleOutline,
  pencilOutline,
  searchOutline,
  sparklesOutline,
  terminalOutline,
  timeOutline,
  warningOutline,
  constructOutline,
  copyOutline,
  createOutline,
  refreshOutline,
  shareOutline,
  playOutline,
  stopCircleOutline,
  volumeHighOutline,
} from "ionicons/icons";
import { memo, useMemo, useState } from "react";

import { filesOf, photosOf, voicesOf, type ApprovalAction, type ChatMessage, type Todo } from "../../chat/model.ts";
import { describeStep, type StepIcon } from "../../chat/tools.ts";
import type { Agent } from "../../net/types.ts";
import { extractConnectLinks, fmtSize, type MediaRef, type ToolCall } from "../../shared/dashboard.ts";
import { ConnectCard, ConnectResultCard } from "../../ui/ConnectCards.tsx";
import { LinkText } from "../../ui/LinkText.tsx";
import { Markdown, openLink } from "../../ui/Markdown.tsx";
import { openFileViewer } from "../../ui/FileViewer.tsx";
import { PhotoGrid } from "../../ui/PhotoGrid.tsx";
import { haptic } from "../../native/haptics.ts";
import { canShare, copyText, shareText } from "../../ui/messageActions.ts";
import { readAloud, reader, stopReading } from "../../ui/readAloud.ts";
import { clockTime } from "../../ui/time.ts";
import { useLongPress } from "../../ui/useLongPress.ts";
import { useStore } from "../../state/store.ts";
import { VoiceNote, VoiceTranscript } from "../../ui/VoiceNote.tsx";

const ICONS: Record<StepIcon, string> = {
  search: searchOutline,
  web: globeOutline,
  file: documentTextOutline,
  edit: pencilOutline,
  terminal: terminalOutline,
  agent: peopleOutline,
  memory: albumsOutline,
  mail: mailOutline,
  calendar: calendarOutline,
  image: imageOutline,
  link: keyOutline,
  tool: constructOutline,
};

function Step({ call }: { call: ToolCall }) {
  const step = describeStep(call.name, call.args, call.hasResult);
  return (
    <div className={call.hasResult ? "olano-step" : "olano-step running"}>
      {call.hasResult ? <IonIcon icon={ICONS[step.icon]} /> : <IonSpinner name="dots" />}
      <span className="olano-step-label">{step.label}</span>
      {step.detail && <span className="olano-step-detail">{step.detail}</span>}
    </div>
  );
}

function Steps({ tools, streaming }: { tools: ToolCall[]; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  const named = tools.filter((t) => t.name);
  if (!named.length) return null;
  const running = named.filter((t) => !t.hasResult);
  const done = named.length - running.length;
  return (
    <div className="olano-steps">
      {done > 0 && (
        <button type="button" className="olano-steps-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
          <IonIcon icon={open ? chevronDown : chevronForward} />
          {done === 1 ? "1 step" : `${done} steps`}
        </button>
      )}
      {open && named.filter((t) => t.hasResult).map((t) => <Step key={t.id} call={t} />)}
      {streaming && running.map((t) => <Step key={t.id} call={t} />)}
    </div>
  );
}

function Checklist({ todos }: { todos: Todo[] }) {
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <div className="olano-todos">
      <div className="olano-todos-head">
        Plan · {done} of {todos.length}
      </div>
      {todos.map((todo, i) => (
        <div key={i} className={`olano-todo ${todo.status}`}>
          <IonIcon
            icon={todo.status === "completed" ? checkmarkCircle : todo.status === "in_progress" ? timeOutline : ellipseOutline}
          />
          <span>{todo.content}</span>
        </div>
      ))}
    </div>
  );
}

function actionLabel(action: ApprovalAction): string {
  if (action.description) return action.description;
  return describeStep(action.name, typeof action.args === "string" ? action.args : JSON.stringify(action.args ?? {}), false).label;
}

function ApprovalCard({ message, onAnswer }: { message: ChatMessage; onAnswer: (approve: boolean) => void }) {
  const approval = message.approval;
  if (!approval) return null;
  const risky = approval.actions.some((a) => a.risk === "high");
  return (
    <div className={`olano-approval ${approval.state}`}>
      <div className="olano-approval-head">
        <IonIcon icon={risky ? warningOutline : sparklesOutline} color={risky ? "warning" : "primary"} />
        <span>{approval.state === "pending" || approval.state === "sending" ? "Needs your OK" : approval.state === "approved" ? "Approved" : "Declined"}</span>
      </div>
      {approval.actions.map((action, i) => (
        <div key={i} className="olano-approval-action">
          <strong>{actionLabel(action)}</strong>
          <code>{action.name}</code>
        </div>
      ))}
      {(approval.state === "pending" || approval.state === "sending") && (
        <div className="olano-approval-buttons">
          <IonButton fill="outline" color="medium" disabled={approval.state === "sending"} onClick={() => onAnswer(false)}>
            Decline
          </IonButton>
          <IonButton disabled={approval.state === "sending"} onClick={() => onAnswer(true)}>
            {approval.state === "sending" ? <IonSpinner name="crescent" /> : "Approve"}
          </IonButton>
        </div>
      )}
    </div>
  );
}

/** A message's files other than photos: a card each, opened in the app's file viewer. */
function Files({ items }: { items: MediaRef[] }) {
  if (!items.length) return null;
  return (
    <div className="olano-media">
      {items.map((item) => (
        <button
          key={item.url}
          type="button"
          className="olano-file"
          aria-label={`Open ${item.name}`}
          onClick={() => openFileViewer(item)}
        >
          <IonIcon icon={documentTextOutline} />
          <span>{item.name}</span>
        </button>
      ))}
    </div>
  );
}

/** Files a message carried that have no copy to open, by name (see ChatMessage.sentFiles). */
function SentFiles({ message }: { message: ChatMessage }) {
  const opened = new Set(filesOf(message).map((item) => item.name));
  const items = (message.sentFiles ?? []).filter((file) => !opened.has(file.name));
  if (!items.length) return null;
  return (
    <div className="olano-media">
      {items.map((file, i) => (
        <div key={`${file.name}-${i}`} className="olano-file sent">
          <IonIcon icon={documentTextOutline} />
          <span>{file.name}</span>
          {file.size ? <small>{fmtSize(file.size)}</small> : null}
        </div>
      ))}
    </div>
  );
}

/**
 * Long press a message: copy or share its words or a voice note's
 * transcript, or edit what you sent. Pressed on a link, the link's own
 * choices: open it, copy it, share it.
 */
/** What a person can report a reply for (the deployment's admins read the reason). */
export const REPORT_REASONS = ["Offensive or harmful", "Inaccurate or misleading", "Something else"];

function useMessageMenu(message: ChatMessage, onEdit?: (text: string) => void, onReport?: (reason: string) => Promise<unknown>) {
  const [present] = useIonActionSheet();
  const [toast] = useIonToast();
  return useLongPress((target) => {
    const copy = (value: string, what: string) =>
      void copyText(value).then(() => toast({ message: `${what} copied`, duration: 1400, position: "top", color: "dark" }));
    const link = target?.closest?.("a[href]")?.getAttribute("href");
    if (link && /^https?:/i.test(link)) {
      void haptic("medium");
      const linkButtons: { text: string; role?: string; handler?: () => void }[] = [
        { text: "Open Link", handler: () => void openLink(link) },
        { text: "Copy Link", handler: () => copy(link, "Link") },
      ];
      if (canShare()) linkButtons.push({ text: "Share Link", handler: () => void shareText(link) });
      void present({ header: link.length > 80 ? `${link.slice(0, 79)}…` : link, buttons: [...linkButtons, { text: "Cancel", role: "cancel" }] });
      return;
    }
    const text = message.body.trim();
    const transcript = (message.transcripts ?? []).join("\n\n").trim();
    const buttons: { text: string; role?: string; handler?: () => void; data?: { action: string } }[] = [];
    if (text) buttons.push({ text: "Copy", handler: () => copy(text, "Message") });
    if (transcript) buttons.push({ text: "Copy Transcript", handler: () => copy(transcript, "Transcript") });
    if (text && onEdit && message.role === "user") buttons.push({ text: "Edit", handler: () => onEdit(text) });
    if ((text || transcript) && canShare()) buttons.push({ text: "Share", handler: () => void shareText(text || transcript) });
    // Report opens its own sheet once this one has gone (Ionic shows one at a time).
    const report =
      onReport && message.role === "assistant"
        ? () =>
            void present({
              header: "Report this reply",
              subHeader: "The deployment's administrators will see it, with the reason you pick.",
              buttons: [
                ...REPORT_REASONS.map((reason) => ({
                  text: reason,
                  handler: () => {
                    void onReport(reason).then(
                      () => toast({ message: "Reported. Thank you.", duration: 1800, position: "top", color: "dark" }),
                      () => toast({ message: "Couldn't send the report. Try again.", duration: 2200, position: "top", color: "danger" }),
                    );
                  },
                })),
                { text: "Cancel", role: "cancel" },
              ],
            })
        : null;
    if (report) buttons.push({ text: "Report", role: "destructive", data: { action: "report" } });
    if (!buttons.length) return;
    void haptic("medium");
    const when = clockTime(message.ts);
    void present({
      header: when ? `${message.role === "user" ? "Sent" : "Replied"} at ${when}` : undefined,
      buttons: [...buttons, { text: "Cancel", role: "cancel" }],
      onDidDismiss: (event) => {
        // After the hook has let go of this sheet: a present() from inside
        // its own dismissal is ignored.
        if ((event.detail.data as { action?: string } | undefined)?.action === "report" && report) setTimeout(report, 0);
      },
    });
  });
}

/**
 * Hear a reply: in the deployment's voice when it has text-to-speech set up,
 * in the phone's own voice otherwise (ui/readAloud.ts). Tapped again while it
 * reads, it stops.
 */
function ListenButton({ agent, message }: { agent: string; message: ChatMessage }) {
  const reading = useStore(reader);
  const [toast] = useIonToast();
  const active = reading.key === message.key && reading.phase !== "idle";
  return (
    <button
      type="button"
      className="olano-action"
      aria-label={active ? "Stop reading" : "Read reply aloud"}
      onClick={() => {
        if (active) {
          stopReading();
          return;
        }
        void haptic("light");
        void readAloud(agent, message.key, message.body).then((outcome) => {
          if (outcome === "read") return;
          void toast({
            message:
              outcome === "nothing"
                ? "There are no words in this reply to read aloud."
                : "There is no voice to read this with: this phone has none, and text to speech is off on the deployment.",
            duration: 3500,
            position: "top",
            color: "dark",
          });
        });
      }}
    >
      {active && reading.phase === "loading" ? <IonSpinner name="crescent" /> : <IonIcon icon={active ? stopCircleOutline : volumeHighOutline} />}
    </button>
  );
}

/**
 * The buttons under a message, as AI chat apps have them: copy, hear and
 * share a finished reply (and try again when it failed); copy or edit what
 * you sent. Editing sends the corrected words as a new message, as the
 * dashboard does: the conversation's history is never rewritten.
 */
function MessageActions({
  message,
  readAs,
  onEdit,
  onRetry,
}: {
  message: ChatMessage;
  /** The agent whose reply this is, for reading it aloud. */
  readAs?: string;
  onEdit?: (text: string) => void;
  onRetry?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const text = message.body.trim() || (message.transcripts ?? []).join("\n\n").trim();
  const mine = message.role === "user";
  if (!text && !onRetry) return null;
  const copy = () => {
    void copyText(text).then(() => {
      void haptic("light");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  const who = mine ? "message" : "reply";
  return (
    <div className={mine ? "olano-actions mine" : "olano-actions"}>
      {text && (
        <button type="button" className="olano-action" aria-label={copied ? "Copied" : `Copy ${who}`} onClick={copy}>
          <IonIcon icon={copied ? checkmark : copyOutline} />
        </button>
      )}
      {text && mine && onEdit && (
        <button type="button" className="olano-action" aria-label="Edit message" onClick={() => onEdit(message.body.trim() || text)}>
          <IonIcon icon={createOutline} />
        </button>
      )}
      {message.body.trim() && !mine && readAs && <ListenButton agent={readAs} message={message} />}
      {text && !mine && canShare() && (
        <button type="button" className="olano-action" aria-label="Share reply" onClick={() => void shareText(text)}>
          <IonIcon icon={shareOutline} />
        </button>
      )}
      {onRetry && (
        <button type="button" className="olano-action olano-action-labelled" onClick={onRetry}>
          <IonIcon icon={refreshOutline} aria-hidden="true" />
          Try again
        </button>
      )}
    </div>
  );
}

function channelNote(message: ChatMessage): string | null {
  const channel = (message.channel || "").toLowerCase();
  if (!channel || channel === "dashboard" || channel === "api" || channel === "app" || channel === "mobile") return null;
  const name = channel[0].toUpperCase() + channel.slice(1);
  return message.sender ? `${message.sender} · ${name}` : `via ${name}`;
}

export const MessageView = memo(function MessageView({
  message,
  previous,
  onAnswer,
  onEdit,
  onRetry,
  onApprovePlan,
  onReport,
  readAs,
  showActions = true,
}: {
  message: ChatMessage;
  agent?: Agent;
  /** The agent whose replies these are: a reply gets a button to hear it. */
  readAs?: string;
  /** "Approve & run" under a plan drafted in Plan mode. */
  onApprovePlan?: () => void;
  previous?: ChatMessage;
  onAnswer: (approve: boolean) => void;
  /** Puts the words of a message the person sent back in the message box. */
  onEdit?: (text: string) => void;
  /** Sends the message before a failed reply again. */
  onRetry?: () => void;
  /** Reports a reply to the deployment's administrators, with a reason; in the message menu. */
  onReport?: (reason: string) => Promise<unknown>;
  /** The person's own messages show their buttons on the latest one only. */
  showActions?: boolean;
}) {
  const grouped = previous?.role === message.role;
  const menu = useMessageMenu(message, onEdit, onReport);
  const links = useMemo(() => (message.role === "assistant" ? extractConnectLinks(message.tools) : []), [message.role, message.tools]);
  if (message.role === "system") {
    if (message.connectResult) return <ConnectResultCard result={message.connectResult} />;
    return (
      <div className="olano-system">
        <LinkText text={message.body} />
      </div>
    );
  }
  if (message.role === "user") {
    const note = channelNote(message);
    const voices = voicesOf(message);
    return (
      <div className={grouped ? "olano-msg user grouped" : "olano-msg user"} {...menu}>
        {note && <div className="olano-msg-note">{note}</div>}
        {message.steered && <div className="olano-msg-note steered">Added while it worked</div>}
        <PhotoGrid photos={photosOf(message)} sending={message.sending} />
        {!voices.length && (message.transcripts?.length || message.untranscribed)
          ? (message.transcripts?.length ? message.transcripts : [undefined]).map((transcript, i) => (
              <VoiceTranscript key={i} transcript={transcript} untranscribed={message.untranscribed} />
            ))
          : null}
        {voices.map((voice, i) => (
          <VoiceNote
            key={voice.key}
            voice={voice}
            mine
            sending={message.sending}
            transcript={message.transcripts?.[i]}
            untranscribed={message.untranscribed && !message.transcripts?.[i]}
            transcribing={message.transcribing}
          />
        ))}
        <Files items={filesOf(message)} />
        <SentFiles message={message} />
        {message.body && (
          <div className="olano-bubble user">
            <LinkText text={message.body} />
          </div>
        )}
        {showActions && !message.sending && <MessageActions message={message} onEdit={onEdit} />}
      </div>
    );
  }
  return (
    <div className={grouped ? "olano-msg agent grouped" : "olano-msg agent"} {...menu}>
      <Steps tools={message.tools} streaming={message.streaming} />
      {message.todos?.length ? <Checklist todos={message.todos} /> : null}
      {message.body && (
        <div className="olano-bubble agent">
          <Markdown text={message.body} />
          {message.streaming && <span className="olano-caret" />}
        </div>
      )}
      <PhotoGrid photos={photosOf(message)} />
      {voicesOf(message).map((voice) => (
        <VoiceNote key={voice.key} voice={voice} mine={false} />
      ))}
      <Files items={filesOf(message)} />
      {links.map((link) => (
        <ConnectCard key={link.href} link={link} />
      ))}
      <ApprovalCard message={message} onAnswer={onAnswer} />
      {message.planProposal && onApprovePlan && !message.streaming && (
        <div className="olano-plan-actions">
          <IonButton size="small" onClick={onApprovePlan}>
            <IonIcon slot="start" icon={playOutline} />
            Approve &amp; run
          </IonButton>
          <span>Or reply to change the plan.</span>
        </div>
      )}
      {message.error && (
        <div className="olano-msg-error">
          <IonIcon icon={warningOutline} />
          <span>{message.error}</span>
        </div>
      )}
      {message.note && <div className="olano-msg-note">{message.note}</div>}
      {!message.streaming && (message.body || onRetry) && <MessageActions message={message} readAs={readAs} onRetry={onRetry} />}
    </div>
  );
});
