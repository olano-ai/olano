// Thumbs up / down under a finished reply, in the dashboard chat and the
// simple shell. A vote is recorded the moment a thumb is pressed
// (POST /api/chat/feedback); thumbs down then offers an optional comment,
// which re-sends the same vote with it. Pressing the lit thumb again takes the
// vote back. The server owns the record (olano/core/feedback.py) and decides
// what, if anything, reaches Langfuse; this only shows the person their own
// vote. Renders nothing for a reply without a turn id (recorded before turns
// were stamped) or outside a saved thread.

import { useCallback, useRef, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";

import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { AnchoredPopover } from "../../ui/AnchoredPopover";
import { useToast } from "../../ui/toast";
import { STR } from "./feedbackStrings";
import { chatStore, type ReplyVote } from "./sessionStore";

const COMMENT_MAX = 2000;

export function ReplyFeedback({
  agent,
  threadId,
  msgId,
  turnId,
  vote,
}: {
  agent: string;
  threadId: string | null | undefined;
  msgId: number;
  turnId: string | undefined;
  vote: ReplyVote | undefined;
}) {
  const t = useT(STR);
  const toast = useToast();
  const downRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // The request in flight: votes are sent strictly one after another, so the
  // comment that follows a thumbs down can never land before (and then under)
  // the vote it explains. The last row the server records is the vote.
  const chain = useRef<Promise<void>>(Promise.resolve());
  const close = useCallback(() => setOpen(false), []);
  if (!threadId || !turnId) return null;

  function send(value: 1 | -1 | 0, comment = ""): void {
    chain.current = chain.current.then(() => post(value, comment));
  }

  async function post(value: 1 | -1 | 0, comment: string): Promise<void> {
    setBusy(true);
    try {
      const res = await api.post<{ value: number; comment: string }>("/api/chat/feedback", {
        agent,
        thread_id: threadId,
        turn_id: turnId,
        value,
        comment,
      });
      const next: ReplyVote | null =
        res.value === 1 || res.value === -1 ? { value: res.value, comment: res.comment || "" } : null;
      chatStore.setFeedback(agent, threadId as string, msgId, next);
    } catch {
      toast.push(t("fb.failed"), "error");
    } finally {
      setBusy(false);
    }
  }

  function up() {
    setOpen(false);
    send(vote?.value === 1 ? 0 : 1);
  }

  function down() {
    if (vote?.value === -1) {
      setOpen(false);
      send(0);
      return;
    }
    setDraft("");
    setOpen(true);
    send(-1);
  }

  function submit() {
    setOpen(false);
    if (draft.trim()) send(-1, draft.trim());
  }

  const isUp = vote?.value === 1;
  const isDown = vote?.value === -1;
  return (
    <>
      <button
        type="button"
        className={`msg-edit-btn msg-vote msg-vote-up${isUp ? " voted" : ""}`}
        data-testid="reply-vote-up"
        aria-pressed={isUp}
        aria-label={t("fb.up")}
        title={isUp ? t("fb.upTaken") : t("fb.up")}
        disabled={busy}
        onClick={up}
      >
        <ThumbsUp size={13} />
      </button>
      <span ref={downRef} style={{ display: "inline-flex" }}>
        <button
          type="button"
          className={`msg-edit-btn msg-vote msg-vote-down${isDown ? " voted" : ""}`}
          data-testid="reply-vote-down"
          aria-pressed={isDown}
          aria-label={t("fb.down")}
          title={isDown ? t("fb.downTaken") : t("fb.down")}
          disabled={busy}
          onClick={down}
        >
          <ThumbsDown size={13} />
        </button>
      </span>
      <AnchoredPopover
        anchorRef={downRef}
        open={open}
        onClose={close}
        minWidth={320}
        className="fb-pop"
        role="dialog"
        ariaLabel={t("fb.prompt")}
        testId="reply-feedback-form"
      >
        <label className="fb-pop-label" htmlFor={`fb-${msgId}`}>
          {t("fb.prompt")}
        </label>
        <textarea
          id={`fb-${msgId}`}
          className="input fb-pop-text"
          autoFocus
          rows={3}
          maxLength={COMMENT_MAX}
          placeholder={t("fb.placeholder")}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          }}
        />
        <p className="fb-pop-help">{t("fb.help")}</p>
        <div className="fb-pop-actions">
          <button type="button" className="btn ghost" onClick={close}>
            {t("fb.skip")}
          </button>
          <button type="button" className="btn primary" disabled={!draft.trim()} onClick={submit}>
            {t("fb.send")}
          </button>
        </div>
      </AnchoredPopover>
    </>
  );
}
