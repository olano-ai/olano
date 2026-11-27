// Theme selection, persisted to localStorage and applied to <html data-theme>.
// Default is dark (the Aurora identity); an inline script in index.html applies
// it before first paint to avoid a flash.

const KEY = "lc-theme";
// dark / light / noir are the three originals.
//
// Dark grounds: `dark` (warm charcoal, Olano orange), `midnight` (deep navy,
// azure), `mocha` (espresso, copper), `noir` (pure black, green), `volt`
// (near-black, electric chartreuse; the one accent pale enough to need dark
// type on it, see --accent-fg in styles/tokens.css).
// Pale grounds: `light` (neutral off-white, orange), `bw` (ink on paper — a
// white page with a black accent and no colour anywhere but the states),
// `harbor` (cool slate, deep teal), `sepia` (warm parchment, umber — nothing in
// it is pure white), `sakura` (blush, deep rose), `pastel` (sage-green page,
// emerald accent), `singapore` (the flag: white page, flag red).
// `contrast` is the accessibility theme, not a taste one: black on white, real
// hairlines, every state colour past 7:1.
//
// Cortex keeps its violet in every theme — deepened on pale grounds, pushed
// toward magenta in `midnight` (whose accent is blue) and toward blue in
// `sakura` (whose accent is rose), so it never merges with the accent.
//
// Each is a full token block in styles/tokens.css; the pale ones also join the
// `light` selectors in globals.css / simple.css that tune what a pale page
// needs (the Cortex glow, the simple shell's frame).
export type Theme =
  | "dark"
  | "midnight"
  | "mocha"
  | "noir"
  | "volt"
  | "light"
  | "bw"
  | "harbor"
  | "sepia"
  | "sakura"
  | "pastel"
  | "singapore"
  | "contrast";

// Canonical, ordered list of themes — drives the picker lists and the cycle
// order of the sidebar toggle.
//
// **The two house themes lead**: `dark` and `light` are the product's own
// identity and between them are what nearly everyone picks, so they are the
// first two rows of the dropdown rather than being separated by three dark
// variants (which put Olano Light fifth, below the fold of a short list). The
// eleven that follow keep the dark-then-pale gradient, so the sidebar's cycle
// toggle still walks from dark to pale rather than flashing the screen between
// every step — the one step that now inverts is its second, which is the cost
// of putting the pair a person actually wants at the top of the list they
// actually read.
export const THEMES: Theme[] = [
  "dark",
  "light",
  "midnight",
  "mocha",
  "noir",
  "volt",
  "bw",
  "harbor",
  "sepia",
  "sakura",
  "pastel",
  "singapore",
  "contrast",
];

// The themes drawn on a pale page. Anything that styles "light" by data-theme
// should read this list rather than the literal, so a new pale theme gets the
// same treatment by being added here and to the CSS selectors it names.
export const LIGHT_THEMES: readonly Theme[] = [
  "light",
  "bw",
  "harbor",
  "sepia",
  "sakura",
  "pastel",
  "singapore",
  "contrast",
];

// One source of truth for "which theme is active". Every surface that shows
// the current theme (the sidebar toggle's icon, the account menu's select, the
// Config picker) reads it through `useTheme()` / `subscribeTheme`, and every
// writer goes through `setTheme`, which notifies them all. Each surface used to
// keep its own `useState(getTheme())`, so a change made in one left the others
// showing the theme that was active when they mounted.

const listeners = new Set<() => void>();

function readStored(): Theme {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch {
    stored = null;
  }
  return THEMES.includes(stored as Theme) ? (stored as Theme) : "dark";
}

let current: Theme | null = null;

export function getTheme(): Theme {
  if (current === null) current = readStored();
  return current;
}

function emit(): void {
  for (const fn of [...listeners]) fn();
}

export function setTheme(theme: Theme): void {
  current = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Private mode or blocked storage: the choice still applies to this page.
  }
  if (typeof document !== "undefined") document.documentElement.dataset.theme = theme;
  emit();
}

// Subscribe to theme changes (the shape `useSyncExternalStore` expects). A
// change made in another tab arrives as a `storage` event and is applied here
// too, so two open tabs never disagree about their own toggle icon.
export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== KEY) return;
    const next = readStored();
    if (next === current) return;
    current = next;
    if (typeof document !== "undefined") document.documentElement.dataset.theme = next;
    emit();
  });
}

// Advance to the next theme in THEMES, wrapping around. Used by the sidebar
// quick-toggle button; the picker lists call setTheme directly.
export function cycleTheme(): Theme {
  const next = THEMES[(THEMES.indexOf(getTheme()) + 1) % THEMES.length];
  setTheme(next);
  return next;
}
