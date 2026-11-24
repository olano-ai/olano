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

export function getTheme(): Theme {
  const stored = localStorage.getItem(KEY);
  return THEMES.includes(stored as Theme) ? (stored as Theme) : "dark";
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(KEY, theme);
  document.documentElement.dataset.theme = theme;
}

// Advance to the next theme in THEMES, wrapping around. Used by the sidebar
// quick-toggle button; the picker lists call setTheme directly.
export function cycleTheme(): Theme {
  const next = THEMES[(THEMES.indexOf(getTheme()) + 1) % THEMES.length];
  setTheme(next);
  return next;
}
