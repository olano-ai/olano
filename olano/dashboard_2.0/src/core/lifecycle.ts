// Shared tab-lifecycle "resume" signal.
//
// Mobile browsers aggressively suspend background tabs (lock screen, app
// switch): JS freezes, sockets die, and every live surface silently misses the
// frames pushed while the tab was away. The general `/api/stream` channel has
// no replay, so recovery is refetch-on-resume — and every consumer needs the
// SAME notion of "the tab just came back". This module owns that signal.
//
// It coalesces the three browser events that fire on a comeback
// (`visibilitychange`→visible, `pageshow` from the BFCache, `online`) into one
// debounced ResumeEvent, enriched with how long the tab was hidden and whether
// the shared stream connection was lost along the way. Consumers (chat
// sessionStore, activity/scheduler/approvals stores, views) subscribe and
// decide what to refetch via the shared `shouldRefetch` policy, so a quick
// tab-flick doesn't trigger a refetch storm but a real suspension does.
//
// Listener pattern mirrors useServerHealth/VaultMonitor (the existing
// visibility-aware code); stream state is observed via `subscribeStreamState`,
// which deliberately does NOT hold the connection open (see agentStream.ts).

import { getStreamState, subscribeStreamState } from "./agentStream";

export interface ResumeEvent {
  /** Which browser event (first of the coalesced burst) woke the tab. */
  reason: "visible" | "pageshow" | "online";
  /** How long the document reported hidden, 0 if it never did. */
  hiddenMs: number;
  /** The shared /api/stream connection dropped while away (or is still down). */
  streamWasDown: boolean;
}

type ResumeHandler = (e: ResumeEvent) => void;

// Coalesce window: visible/pageshow/online typically fire together on unlock.
const DEBOUNCE_MS = 300;
// shouldRefetch: a hide shorter than this with a healthy stream missed nothing.
const MIN_HIDDEN_MS = 5000;

const listeners = new Set<ResumeHandler>();
let installed = false;
let hiddenAt: number | null = null;
// Latched when the stream leaves "open" while the tab is hidden — the frozen
// tab's own onerror may only run *after* our resume handlers, so the fire-time
// getStreamState() check below is the belt to this suspender.
let streamDropLatch = false;
let debounce: ReturnType<typeof setTimeout> | null = null;
let pendingReason: ResumeEvent["reason"] | null = null;
let lastResume = 0;

function fire(): void {
  debounce = null;
  const reason = pendingReason ?? "visible";
  pendingReason = null;
  const event: ResumeEvent = {
    reason,
    hiddenMs: hiddenAt !== null ? Date.now() - hiddenAt : 0,
    streamWasDown: streamDropLatch || getStreamState() !== "open",
  };
  hiddenAt = null;
  streamDropLatch = false;
  lastResume = Date.now();
  for (const l of listeners) l(event);
}

function trigger(reason: ResumeEvent["reason"]): void {
  if (debounce) return; // first reason of the burst wins
  pendingReason = reason;
  debounce = setTimeout(fire, DEBOUNCE_MS);
}

function install(): void {
  if (installed) return;
  installed = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") hiddenAt ??= Date.now();
    else trigger("visible");
  });
  // BFCache restore: the page comes back with all its state but stale data.
  window.addEventListener("pageshow", (e) => {
    if ((e as PageTransitionEvent).persisted) trigger("pageshow");
  });
  window.addEventListener("online", () => trigger("online"));
  subscribeStreamState((state) => {
    if (hiddenAt !== null && state !== "open") streamDropLatch = true;
  });
}

/**
 * Subscribe to tab-resume events. Returns an unsubscribe function. Module-scope
 * stores subscribe once and never unsubscribe; components should unsubscribe in
 * their effect cleanup.
 */
export function subscribeResume(handler: ResumeHandler): () => void {
  install();
  listeners.add(handler);
  return () => {
    listeners.delete(handler);
  };
}

/** Timestamp of the last fired resume, 0 if none — for consumers that mount late. */
export function lastResumeAt(): number {
  return lastResume;
}

/**
 * The one shared refetch policy: act only when the suspension could actually
 * have lost data — the stream connection dropped, or the tab was hidden long
 * enough for the OS to have frozen it. Keeps quick tab-flicks free.
 */
export function shouldRefetch(e: ResumeEvent): boolean {
  return e.streamWasDown || e.hiddenMs >= MIN_HIDDEN_MS;
}
