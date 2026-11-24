// Handing a conversation that STARTED somewhere else to the chat view.
//
// Cortex used to grow its own chat: a small inline transcript under a Pulse
// card, and a Chat tab inside the item drawer. Both streamed from
// /api/chat/stream on a deterministic thread id — so they were writing real
// threads all along, in a 280px-tall box with none of the chat view's
// attachments, model picker, history, approvals or error remediation, and with
// their own bugs to keep (a client-side "already seeded" flag, a transcript
// that lived only in module state).
//
// The thread id is the whole handoff: Cortex keeps minting `pulse:<id>` /
// `cortex:<id>`, and the chat view opens that thread. Continuity is free, for
// conversations started before this change too.
//
// Two halves, deliberately:
//   * the DURABLE half is the URL, `/chat/<agent>?thread=<id>`, so a refresh, a
//     copied link or a back-button lands on the same conversation;
//   * the EPHEMERAL half is the draft staged here: the framing Cortex wants in
//     front of the agent ("The user wants to explore more about this idea …",
//     with the full workspace path to its artifacts), placed in the composer
//     of a thread that has no history yet. It is module state (never the
//     URL): a draft is a one-time intent, and putting a paragraph of prompt in
//     a link would re-draft it on every visit.
//
// The draft is NEVER sent on the user's behalf. It ends in "User: " and waits
// for the person to say what they actually want; an existing conversation is
// simply reopened with its history and an untouched composer. The earlier
// design auto-sent an opening question, which asked the agent for a take
// nobody had requested and — worse — did nothing at all in the simple shell,
// so the button read as broken.

export interface ChatHandoff {
  /** The agent the conversation belongs to — a Pulse card can name its own. */
  agent: string;
  /** Deterministic thread id, e.g. `pulse:<item id>` or `cortex:<item id>`. */
  threadId: string;
  /**
   * Text to preload into the composer — only when the thread has no history
   * yet and nothing is already typed there. Never sent automatically.
   */
  draft?: string;
  /**
   * The runtime is already writing this thread's first rows on the owner's
   * behalf — the onboarding greeting (`runtime/onboarding_greeting.py`), which
   * boots Agent Father if it has to and can take a minute on a cold box. The
   * chat shows a "waking up" state instead of an empty composer until the
   * first row lands; without it a new owner sat on a blank thread with no
   * sign anything was happening.
   */
  awaiting?: "greeting";
}

let pending: ChatHandoff | null = null;

/** Stage the intent, then navigate to `/chat/<agent>?thread=<threadId>`. */
export function stageChatHandoff(handoff: ChatHandoff): void {
  pending = handoff;
}

/**
 * Claim the staged handoff, if any. Consuming it here (rather than after it has
 * been acted on) is what keeps a draft one-shot: a remount, a second tab of the
 * same view, or an agent switch mid-flight can never re-draft it.
 */
export function takeChatHandoff(): ChatHandoff | null {
  const h = pending;
  pending = null;
  return h;
}

/**
 * Whether a just-opened thread should receive the hand-off draft: it must be a
 * genuinely new conversation (reopening an existing Cortex discussion continues
 * it — the history is the context) and the composer must be empty (a draft the
 * person is mid-way through typing is never overwritten).
 */
export function shouldPreloadDraft(session: { messages: unknown[]; input: string } | undefined): boolean {
  return !!session && session.messages.length === 0 && !session.input.trim();
}

/**
 * Park the caret at the end of a composer that was just preloaded, so the
 * person types straight after "User: ". Deferred a frame: the value lands on
 * the next render, and selecting before that pins the caret at 0.
 */
export function focusComposerEnd(ta: HTMLTextAreaElement | null | undefined): void {
  if (!ta) return;
  requestAnimationFrame(() => {
    ta.focus();
    const n = ta.value.length;
    ta.setSelectionRange(n, n);
  });
}
