// The composer: type, attach, dictate, send.
//
// Presentation plus input behaviour only — the send POLICY (send now, queue
// behind a running reply, or run a slash command) lives in SimpleChatView,
// which owns the conversation. This component decides what a keystroke means
// and hands the decision up.
//
// Two things it does NOT have, on purpose: the "!" shell lane (running commands
// on the box is an operator power, and the capability that gates it is not one
// a consumer shell should be teaching) and any model / effort / verbosity
// control. What is left is the messenger composer everyone already knows: a
// pill with a round "+" on the left and a round mic on the right, the mic
// giving way to send the moment there is something to send.
//
// One composer, two conversations. A group chat needs two extra affordances —
// pick which member answers, and `@name` completion — and they arrive as
// optional props rather than as a forked component, because everything else
// (autogrow, dictation, queueing, history recall, paste, slash) is identical
// and a second copy of it would drift within a release.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AtSign, Clock, FastForward, Plus, Slash, Square } from "lucide-react";
import { useI18n, useT } from "../../core/i18n";
import { useMediaQuery } from "../../core/useMediaQuery";
import { useCapability } from "../../core/capabilities";
import { MicIcon, SendIcon } from "../../ui/icons";
import { useToast } from "../../ui/toast";
import { StagedAttachments } from "../chat/AttachmentViews";
import type { Attachment } from "../chat/attachments";
import type { QueuedMsg } from "../chat/sessionStore";
import { QueuedChip } from "../chat/QueuedChip";
import type { SlashCommand } from "../chat/slash";
import { TAP_THRESHOLD_MS, useVoiceInput, type VoiceErrorCode } from "../chat/voice";
import { STR } from "./strings";

// The box grows with its content up to this many rows, then scrolls — there is
// no pure-CSS equivalent, so the height is driven off scrollHeight.
const MAX_ROWS = 8;

function autosize(el: HTMLTextAreaElement) {
  const cs = getComputedStyle(el);
  const line = parseFloat(cs.lineHeight) || 20;
  const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  const max = line * MAX_ROWS + padY;
  el.style.height = "auto"; // reset first so the box can shrink as text is deleted
  const next = el.scrollHeight;
  el.style.height = `${Math.min(next, max)}px`;
  el.style.overflowY = next > max ? "auto" : "hidden";
}

/** The group composer's "who answers next" control. Omitted in a 1:1 chat. */
export function SimpleComposer({
  botName,
  placeholder,
  canAttach = true,
  hint,
  input,
  staged,
  queued,
  streaming,
  commands,
  mentions,
  history,
  textareaRef,
  onInput,
  onAddFiles,
  onRemoveStaged,
  onSteerQueued,
  onEditQueued,
  onCancelQueued,
  onClearQueue,
  onSubmit,
  onSteer,
  onStop,
  onSlash,
}: {
  botName: string;
  /** Overrides the "Message {bot}…" placeholder (a group names the group). */
  placeholder?: string;
  /**
   * Whether files can ride along with a message. False in a group chat, whose
   * endpoint takes text only — a paperclip that silently drops the file is
   * worse than no paperclip.
   */
  canAttach?: boolean;
  /** Overrides the one-line explanation under the box. */
  hint?: string;
  input: string;
  staged: Attachment[];
  queued: QueuedMsg[];
  streaming: boolean;
  /** The user-facing slash commands this shell exposes. */
  commands: SlashCommand[];
  /** Names `@` completes against — the group's members. Empty in a 1:1 chat. */
  mentions?: string[];
  /** Group only: which member takes the next turn. */
  /** The reader's own past messages, oldest → newest, for ArrowUp recall. */
  history: string[];
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  onInput: (value: string) => void;
  onAddFiles: (files: FileList | File[]) => void;
  onRemoveStaged: (id: string) => void;
  // The three actions every waiting chip offers — see chat/QueuedChip.tsx.
  onSteerQueued: (id: number) => void;
  onEditQueued: (id: number) => void;
  onCancelQueued: (id: number) => void;
  onClearQueue: () => void;
  onSubmit: () => void;
  onSteer: () => void;
  onStop: () => void;
  onSlash: (head: string, arg: string) => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const { lang } = useI18n();
  const fileRef = useRef<HTMLInputElement>(null);
  const mirrorRef = useRef<HTMLDivElement>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const [interim, setInterim] = useState("");

  // Shell-style recall: ArrowUp walks back through sent messages, ArrowDown
  // forward, restoring the live draft once you pass the newest.
  const [histIdx, setHistIdx] = useState(-1);
  const histDraftRef = useRef("");

  // Touch keyboards insert a newline on Return (like every phone messenger) and
  // only the send button submits; desktop keeps Enter-to-send. Read reactively,
  // so plugging in a keyboard (or a device that reports late) is picked up.
  const coarsePointer = useMediaQuery("(pointer: coarse)");

  // ── dictation ────────────────────────────────────────────────────────────
  // The draft lives in the conversation store, so the callbacks read it through
  // a ref rather than a closure that goes stale between utterances.
  const inputRef = useRef(input);
  inputRef.current = input;
  const sttEnabled = useCapability("stt_enabled");
  const voiceErrorMsg = useCallback(
    (code: VoiceErrorCode): string =>
      code === "denied"
        ? t("voice.denied")
        : code === "no-speech"
          ? t("voice.noSpeech")
          : code === "network"
            ? t("voice.network")
            : code === "stt-off"
              ? t("voice.sttOff")
              : t("voice.error"),
    [t],
  );
  const onCommit = useCallback(
    (text: string) => {
      const prev = inputRef.current;
      // Dictation always appends; typing still edits anywhere. One rule beats
      // fighting the caret mid-utterance.
      onInput(prev ? `${prev.replace(/\s+$/, "")} ${text}` : text);
    },
    [onInput],
  );
  const onVoiceError = useCallback(
    (code: VoiceErrorCode) => toast.push(voiceErrorMsg(code), code === "no-speech" ? "info" : "error"),
    [toast, voiceErrorMsg],
  );
  const voice = useVoiceInput({ sttEnabled, lang, onInterim: setInterim, onCommit, onError: onVoiceError });
  const voiceUsable = voice.support !== "none";
  const recording = voice.recording;
  // Press-and-hold vs tap-to-toggle is decided on release.
  const holdStartRef = useRef(0);
  const pressStartedRef = useRef(false);

  // Leaving a hot mic unindicated is never acceptable: if the button goes away
  // (an unsupported browser mid-session), stop listening with it.
  useEffect(() => {
    if (!voiceUsable && voice.recording) voice.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceUsable, voice.recording]);

  // ── autogrow ─────────────────────────────────────────────────────────────
  useLayoutEffect(() => {
    const ta = textareaRef.current;
    if (ta) autosize(ta);
    // The dimmed interim tail is part of the textarea's value while dictating,
    // so the mirror has to scroll with it or the two drift apart past MAX_ROWS.
    if (ta && mirrorRef.current) mirrorRef.current.scrollTop = ta.scrollTop;
  }, [input, interim, textareaRef]);

  useEffect(() => {
    const onResize = () => {
      const ta = textareaRef.current;
      if (ta) autosize(ta);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [textareaRef]);

  // ── slash autocomplete ───────────────────────────────────────────────────
  const slashQuery =
    input.startsWith("/") && !input.includes(" ") && !input.includes("\n") ? input.slice(1).toLowerCase() : null;
  const matches = slashQuery !== null ? commands.filter((c) => c.name.toLowerCase().startsWith(slashQuery)) : [];
  const slashOpen = matches.length > 0;

  // ── @mention autocomplete (group chats) ──────────────────────────────────
  // Matched at the END of the draft rather than at the caret: a mention is a
  // thing you type forward, and reading the caret would mean tracking selection
  // state on every keystroke for a case nobody hits (going back to edit a
  // half-written @name mid-paragraph). Anchored to a word start so an email
  // address never opens the menu.
  const mentionQuery = (() => {
    if (!mentions?.length || slashOpen) return null;
    const m = /(?:^|\s)@([A-Za-z0-9_.-]*)$/.exec(input);
    return m ? m[1].toLowerCase() : null;
  })();
  const mentionMatches =
    mentionQuery === null
      ? []
      : (mentions ?? []).filter((n) => n.toLowerCase().startsWith(mentionQuery));
  const mentionOpen = mentionMatches.length > 0;

  function chooseMention(i: number) {
    const name = mentionMatches[i];
    if (!name) return;
    // Replace the partial token, keeping everything before it, and leave a
    // trailing space so the next word is not glued to the name.
    onInput(input.replace(/@[A-Za-z0-9_.-]*$/, `@${name} `));
    textareaRef.current?.focus();
  }

  function chooseSlash(i: number) {
    const cmd = matches[i];
    if (!cmd) return;
    // A command that takes an argument fills the box and waits; the rest run.
    if (cmd.args) {
      onInput(`/${cmd.name} `);
      textareaRef.current?.focus();
    } else {
      onSlash(cmd.name, "");
      onInput("");
    }
  }

  function applyRecall(text: string) {
    onInput(text);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (ta) ta.selectionStart = ta.selectionEnd = ta.value.length;
    });
  }

  // ArrowUp/ArrowDown recall previous messages, but only when the caret sits on
  // the first / last line, so multi-line editing still moves the caret normally.
  function recallHistory(e: React.KeyboardEvent): boolean {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return false;
    if (e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return false;
    if (history.length === 0) return false;
    const ta = textareaRef.current;
    const caret = ta ? ta.selectionStart : input.length;
    if (ta && ta.selectionStart !== ta.selectionEnd) return false; // a selection — leave it alone
    const onFirstLine = input.slice(0, caret).indexOf("\n") === -1;
    const onLastLine = input.slice(caret).indexOf("\n") === -1;
    const idx = histIdx >= history.length ? -1 : histIdx;

    if (e.key === "ArrowUp") {
      if (!onFirstLine) return false;
      if (idx === -1) {
        histDraftRef.current = input; // stash the in-progress draft
        const next = history.length - 1;
        setHistIdx(next);
        applyRecall(history[next]);
      } else if (idx > 0) {
        setHistIdx(idx - 1);
        applyRecall(history[idx - 1]);
      }
      // idx === 0 → already the oldest; swallow the key to hold position.
      e.preventDefault();
      return true;
    }

    if (idx === -1 || !onLastLine) return false;
    if (idx < history.length - 1) {
      setHistIdx(idx + 1);
      applyRecall(history[idx + 1]);
    } else {
      setHistIdx(-1);
      applyRecall(histDraftRef.current);
    }
    e.preventDefault();
    return true;
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (slashOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, matches.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        chooseSlash(Math.min(slashIndex, matches.length - 1));
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onInput("");
        return;
      }
    }
    if (mentionOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, mentionMatches.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        chooseMention(Math.min(slashIndex, mentionMatches.length - 1));
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Dismiss the menu without eating the draft: a space ends the token.
        onInput(`${input} `);
        return;
      }
      // Enter is deliberately NOT bound here. In a group the mention is part of
      // a sentence you are still writing, so Enter must keep meaning "send" —
      // Tab completes.
    }
    if (recallHistory(e)) return;
    // ⌘/Ctrl+Enter while a reply streams = steer: the bot reads this at its
    // next step, without stopping.
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && streaming) {
      e.preventDefault();
      onSteer();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      if (coarsePointer) return; // Return = newline on touch; the button submits
      e.preventDefault();
      setHistIdx(-1);
      if (recording) voice.stop(); // sending settles dictation and keeps the words
      onSubmit();
    }
  }

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.items || [])
      .filter((it) => it.kind === "file")
      .map((it) => it.getAsFile())
      .filter((f): f is File => !!f);
    if (files.length) {
      e.preventDefault();
      onAddFiles(files);
    }
  }

  function send() {
    setHistIdx(-1);
    if (recording) voice.stop();
    onSubmit();
  }

  const canSend = !!input.trim() || staged.length > 0;
  const boxPlaceholder = placeholder ?? t("chat.placeholder", { agent: botName });
  // The mic owns the right of the pill until there is something to send — the
  // messenger swap. With no usable mic the send button simply lives there and
  // greys out, so the pill never ends in an empty gap.
  const showMic = voiceUsable && (!canSend || recording);
  const showSend = !showMic || canSend;

  return (
    <div className="composer simple-composer" data-simple-composer>
      {mentionOpen && (
        <div className="slash-menu simple-mention-menu" role="listbox" aria-label={t("groups.mention")}>
          {mentionMatches.map((name, i) => {
            const active = i === Math.min(slashIndex, mentionMatches.length - 1);
            return (
              <div
                key={name}
                role="option"
                aria-selected={active}
                className={`slash-item ${active ? "active" : ""}`}
                onMouseEnter={() => setSlashIndex(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  chooseMention(i);
                }}
              >
                <span className="slash-icon">
                  <AtSign size={13} />
                </span>
                <span className="slash-name">{name}</span>
                {i === 0 && !mentionQuery && <span className="slash-desc">{t("groups.answersFirst")}</span>}
              </div>
            );
          })}
        </div>
      )}

      {slashOpen && (
        <div className="slash-menu">
          {matches.map((c, i) => {
            const active = i === Math.min(slashIndex, matches.length - 1);
            const desc = c.descKey ? t(c.descKey) : c.desc || "";
            return (
              <div
                key={c.name}
                className={`slash-item ${active ? "active" : ""}`}
                onMouseEnter={() => setSlashIndex(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  chooseSlash(i);
                }}
              >
                <span className="slash-icon">
                  <Slash size={13} />
                </span>
                <span className="slash-name">
                  /{c.name}
                  {c.args && <span className="slash-args"> {c.args}</span>}
                </span>
                {desc && <span className="slash-desc">{desc}</span>}
              </div>
            );
          })}
        </div>
      )}

      <StagedAttachments items={staged} onRemove={onRemoveStaged} />

      {queued.length > 0 && (
        <div className="queue-chip">
          <Clock size={13} />
          <span>{t("queue.label", { n: String(queued.length) })}</span>
          <div className="queue-items">
            {queued.map((q) => (
              <QueuedChip
                key={q.id}
                q={q}
                streaming={streaming}
                t={t}
                onSteer={onSteerQueued}
                onEdit={onEditQueued}
                onCancel={onCancelQueued}
              />
            ))}
          </div>
          <button type="button" className="btn ghost sm" onClick={onClearQueue}>
            {t("queue.clear")}
          </button>
        </div>
      )}

      {voiceUsable && (recording || voice.state === "transcribing") && (
        <div className="dictation-bar">
          <span className="dot" />
          <span>{recording ? t("voice.listening") : t("voice.transcribing")}</span>
          {recording && <span className="hint">{t("voice.listeningHint")}</span>}
        </div>
      )}

      <div className={`composer-box simple-pill${recording ? " recording" : ""}`}>
        {canAttach && (
          <>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files?.length) onAddFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              className="composer-attach simple-round"
              onClick={() => fileRef.current?.click()}
              aria-label={t("att.attach")}
              title={t("att.attachHelp")}
            >
              <Plus size={18} strokeWidth={2} />
            </button>
          </>
        )}

        <div className="composer-ta-wrap">
          {interim && (
            // Draws what the box holds with the not-yet-final words dimmed; the
            // textarea's own text goes transparent (see .dictating) so this is
            // what the reader sees. Identical typography, so the wrap matches.
            <div className="dictation-mirror" aria-hidden="true" ref={mirrorRef}>
              <span>{input}</span>
              <span className="interim">
                {input ? " " : ""}
                {interim}
              </span>
            </div>
          )}
          <textarea
            ref={textareaRef}
            className={interim ? "dictating" : undefined}
            rows={1}
            placeholder={boxPlaceholder}
            aria-label={boxPlaceholder}
            // The keyboard rules that used to print under the box. On the
            // tooltip they are there when somebody wonders and invisible when
            // nobody does — which is the right trade for something every
            // message box already behaves this way.
            title={coarsePointer ? t("compose.hintTouch") : t("compose.hint")}
            value={interim ? `${input}${input ? " " : ""}${interim}` : input}
            onChange={(e) => {
              const v = e.target.value;
              // While dictating, the interim tail is display-only — strip it
              // back off so typed edits land in the draft alone.
              const tail = interim ? `${input ? " " : ""}${interim}` : "";
              onInput(tail && v.endsWith(tail) ? v.slice(0, v.length - tail.length) : v);
              setSlashIndex(0);
              setHistIdx(-1);
            }}
            onPaste={onPaste}
            onKeyDown={onKeyDown}
          />
        </div>

        {showMic && (
          <button
            type="button"
            className={`composer-mic simple-round${recording ? " rec" : ""}`}
            disabled={voice.state === "transcribing"}
            aria-label={recording ? t("voice.stop") : t("voice.hold")}
            aria-pressed={recording}
            title={recording ? t("voice.stop") : t("voice.holdHelp")}
            onContextMenu={(e) => e.preventDefault()}
            onPointerDown={(e) => {
              // Keeps the caret in the composer and stops iOS turning the hold
              // into a selection or a callout.
              e.preventDefault();
              if (voice.state === "transcribing") return;
              try {
                // Keeps events coming if the finger slides off mid-hold. A
                // nicety — never let it take the gesture down with it.
                e.currentTarget.setPointerCapture(e.pointerId);
              } catch {
                /* capture unavailable; the hold still works */
              }
              if (recording) {
                // Pressing a toggled-on mic ends it; the release does nothing.
                pressStartedRef.current = false;
                voice.stop();
                return;
              }
              pressStartedRef.current = true;
              holdStartRef.current = Date.now();
              voice.start("hold");
            }}
            onPointerUp={() => {
              if (!pressStartedRef.current) return;
              pressStartedRef.current = false;
              // A quick tap means "keep listening"; a real hold is walkie-talkie.
              if (Date.now() - holdStartRef.current < TAP_THRESHOLD_MS) voice.setMode("toggle");
              else voice.stop();
            }}
            onPointerCancel={() => {
              pressStartedRef.current = false;
              voice.cancel();
            }}
          >
            <MicIcon size={17} />
          </button>
        )}

        {streaming ? (
          <>
            {/* Queue + Steer while a reply streams. A touch Return is a
                newline and the Send button is hidden here, so both are
                explicit buttons; the tooltips say what each does. */}
            {canSend && (
              <>
                <button
                  type="button"
                  className="icon-btn simple-round"
                  onClick={onSubmit}
                  aria-label={t("compose.queue")}
                  title={t("compose.queueHelp")}
                >
                  <SendIcon size={17} />
                </button>
                <button
                  type="button"
                  className="icon-btn simple-round"
                  onClick={onSteer}
                  aria-label={t("compose.steer")}
                  title={t("compose.steerHelp")}
                >
                  <FastForward size={17} />
                </button>
              </>
            )}
            <button
              type="button"
              className="icon-btn danger simple-round"
              onClick={onStop}
              aria-label={t("compose.stop")}
              title={t("compose.stopHelp")}
            >
              <Square size={15} fill="currentColor" />
            </button>
          </>
        ) : (
          showSend && (
            <button
              type="button"
              className="icon-btn simple-round"
              disabled={!canSend}
              onClick={send}
              aria-label={t("compose.send")}
              title={t("compose.sendHelp")}
            >
              <SendIcon size={17} />
            </button>
          )
        )}
      </div>

      {/* Only when a caller has something worth saying — a group's "@ picks who
          answers", say. There is deliberately no default line: "Enter sends,
          Shift+Enter is a newline" is how every message box on earth already
          works, and printing it under this one just puts grey text between the
          reader and their conversation. It lives on the box's own tooltip
          instead (compose.placeholderHelp). */}
      {hint && <p className="simple-composer-hint">{hint}</p>}
    </div>
  );
}
