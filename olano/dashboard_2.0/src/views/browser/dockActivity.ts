// When the chat's browser dock, once hidden, comes back.
//
// "Hide" promises "until the agent browses again", but a browser session is
// kept open across turns (it closes only when idle or when the agent closes
// it), so keying the dismissal on the session alone kept the dock hidden for
// every later question: the agent was visibly using a browser nobody could
// see. The server sends no event per action (a navigation in the same tab
// emits nothing), so the signal is the conversation's own tool calls: a
// dismissal records how many browsing calls the thread showed, and one more
// brings the dock back. Pure, so node's --test can import it.

// Browser tools that do not drive the page: closing ends the session (the dock
// goes with it) and the auto-approval pair only reads or edits settings.
const NOT_BROWSING: ReadonlySet<string> = new Set([
  "browser_close",
  "browser_get_auto_approvals",
  "browser_set_auto_approvals",
]);

export function isBrowsingCall(name: string): boolean {
  return name.startsWith("browser_") && !NOT_BROWSING.has(name);
}

/** How many browsing calls the conversation shows. A count, not the last call
 *  id: a settled turn is refetched from the transcript, and a count survives
 *  that while an id the live stream and the stored row spell differently
 *  would bring a hidden dock back with nothing new happening. */
export function countBrowsingCalls(messages: readonly { tools?: readonly { name: string }[] }[]): number {
  let n = 0;
  for (const m of messages) for (const tc of m.tools || []) if (isBrowsingCall(tc.name)) n += 1;
  return n;
}

export interface DockDismissal {
  /** The session that was hidden (`opened_at`); a new session always shows. */
  openedAt: number;
  /** Browsing calls the conversation showed when it was hidden. */
  calls: number;
}

export function dockHidden(d: DockDismissal | null, openedAt: number | undefined, calls: number): boolean {
  return !!d && d.openedAt === openedAt && calls <= d.calls;
}

/** Follow the count down when it drops (another thread or a new chat opened):
 *  otherwise a fresh conversation would have to out-browse the old one before
 *  the dock came back. Returns the same object when nothing changes. */
export function rebaseDismissal(d: DockDismissal | null, calls: number): DockDismissal | null {
  return d && calls < d.calls ? { ...d, calls } : d;
}
