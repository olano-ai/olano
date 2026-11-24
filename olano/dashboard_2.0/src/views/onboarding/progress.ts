// Wizard position, persisted to localStorage.
//
// Onboarding is the one flow a first-run instance is most likely to be
// interrupted during: a brand-new box is still finishing its first boot
// (image pulls, quick-setup, agent build, catalog sync) while the dashboard is
// already being served, so the runtime can stop answering mid-wizard. Whatever
// ends that interruption — the page reconnecting, a reload, a real restart —
// used to drop the admin back on step 1 of 9 with every step they had already
// completed forgotten, because the whole wizard lived in useState.
//
// Only the *position* is stored (which step, how far they got, what each step
// resolved to). The work itself — password set, vault rekeyed, agent created —
// is server-side already; this just stops us asking about it twice. Cleared
// when the wizard completes, so a later visit is never resumed into a finished
// tour.

import type { Outcome, StepId } from "./types";

const KEY = "lc.onboardingProgress";

export interface Progress {
  idx: number;
  maxIdx: number;
  outcomes: Partial<Record<StepId, Outcome>>;
}

const EMPTY: Progress = { idx: 0, maxIdx: 0, outcomes: {} };

/**
 * The saved position, or a fresh one. `steps` is the wizard's current step
 * count: a stored index past the end (an ORDER that lost a step since the
 * write) clamps rather than rendering an undefined panel.
 */
export function loadProgress(steps: number): Progress {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(KEY);
  } catch {
    return { ...EMPTY };
  }
  if (!raw) return { ...EMPTY };
  try {
    const saved = JSON.parse(raw) as Partial<Progress>;
    const last = Math.max(0, steps - 1);
    const idx = Math.min(Math.max(0, Number(saved.idx) || 0), last);
    const maxIdx = Math.min(Math.max(idx, Number(saved.maxIdx) || 0), last);
    return {
      idx,
      maxIdx,
      // Outcomes keyed by a retired step id (an older bundle's "password",
      // "vault", …) ride along harmlessly: nothing reads them, and they never
      // count as the profile step being done.
      outcomes:
        saved.outcomes && typeof saved.outcomes === "object" ? saved.outcomes : {},
    };
  } catch {
    return { ...EMPTY };
  }
}

export function saveProgress(progress: Progress): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(progress));
  } catch {
    // storage disabled / quota — the wizard still works, just not resumable.
  }
}

export function clearProgress(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to clean up */
  }
}
