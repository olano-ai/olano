// Shared "5 mins ago" rendering, plus the one clock that keeps it honest.
//
// Every other relative stamp in this dashboard reads `Date.now()` at render
// time. That is fine for a list that refetches while you look at it and wrong
// for one that doesn't: the notifications store re-renders only when a
// `notifications_changed` frame arrives, so a row painted "just now" would
// still read "just now" an hour later — precisely the stamp a reader would be
// wrong to trust. `useRelativeNow()` closes that by advancing a shared clock:
// ONE app-wide interval, ref-counted, however many rows subscribe.
//
// The strings live in the shared dictionary (`core/i18n.tsx`, `time.ago.*`), so
// a caller gets all ten languages without copying phrases into its own dict.

import { useSyncExternalStore } from "react";
import { subscribeResume } from "./lifecycle";

/** A translator as returned by `useT()` — passed in so a helper that is not a
 *  component can localise without calling hooks. */
type Translate = (key: string, vars?: Record<string, string>) => string;

// A minute is the finest unit rendered above "just now", so ticking faster than
// this only buys re-renders. Snapshots are floored to it (see `getSnapshot`):
// that is what keeps the value stable across the calls React makes within one
// render — a bare `Date.now()` there trips the "getSnapshot should be cached"
// loop guard.
const TICK_MS = 30_000;

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
// Past this, "N days ago" stops being something a reader can picture and a
// plain date is the more useful answer.
const MAX_RELATIVE_DAYS = 30;

const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let unsubResume: (() => void) | null = null;

function emit(): void {
  for (const l of listeners) l();
}

function getSnapshot(): number {
  return Math.floor(Date.now() / TICK_MS) * TICK_MS;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    timer = setInterval(emit, TICK_MS);
    // A suspended tab freezes its timers, so the first frame after a phone
    // unlock would otherwise repaint the stamp the tab fell asleep with.
    unsubResume = subscribeResume(emit);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size > 0) return;
    if (timer !== null) clearInterval(timer);
    timer = null;
    unsubResume?.();
    unsubResume = null;
  };
}

/**
 * Wall-clock ms that advances on its own, so a stamp rendered from it ages
 * while the surface showing it sits open. Floored to the tick, so every row on
 * screen is measured against the same instant.
 */
export function useRelativeNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/**
 * "just now" → "5 mins ago" → "2 hours ago" → "3 days ago", then a plain date.
 *
 * `now` is threaded in (from `useRelativeNow()`) rather than read here, so no
 * helper owns a timer and a whole list agrees on the present. Returns "" for a
 * missing or unparseable stamp — the caller renders nothing rather than the
 * words "Invalid Date".
 */
export function formatRelativeTime(
  iso: string | null | undefined,
  t: Translate,
  now: number,
): string {
  const ts = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(ts)) return "";
  const sec = Math.floor((now - ts) / 1000);
  // A stamp from a box whose clock runs ahead of the browser's reads "just
  // now", never "-2 mins ago".
  if (sec < MINUTE) return t("time.ago.now");
  const mins = Math.floor(sec / MINUTE);
  if (mins < 60) return t(mins === 1 ? "time.ago.minute" : "time.ago.minutes", { n: String(mins) });
  const hours = Math.floor(sec / HOUR);
  if (hours < 24) return t(hours === 1 ? "time.ago.hour" : "time.ago.hours", { n: String(hours) });
  const days = Math.floor(sec / DAY);
  if (days < MAX_RELATIVE_DAYS) {
    return t(days === 1 ? "time.ago.day" : "time.ago.days", { n: String(days) });
  }
  return new Date(ts).toLocaleDateString();
}

/**
 * The exact stamp behind a relative one — never abbreviated, never relative.
 * Belongs on a `title`, where a reader who needs the minute can find it. "" for
 * a missing or unparseable stamp, so a caller can drop the tooltip entirely.
 */
export function absoluteTimestamp(iso: string | null | undefined): string {
  const ts = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(ts)) return "";
  return new Date(ts).toLocaleString();
}
