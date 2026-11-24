// The channel-hue ramp — the ONE source of decorative colour in the dashboard.
//
// Thirteen slots, each a `--hue-*` token that every theme defines for its own
// ground: bright on a dark page, deepened on a pale one (see styles/tokens.css).
// Anything that needs "a colour per arbitrary name" — platform badges, issue
// labels — takes a slot from here instead of computing one.
//
// **Never compute a colour from a hash in JS.** `hsl(<hash> 75% 62%)` is the
// shape this exists to prevent: a fixed lightness is theme-blind AND hue-blind,
// so the same declaration that reads on a dark page falls to 1.2:1 on a pale one
// (measured on the issue-label chips: 1.16–2.39:1 across the labels in use, i.e.
// invisible), and yellow-green lands far brighter than blue at identical inputs.
// A ramp slot cannot fail that way: the theme already chose a value that works
// on its own ground, and a theme added later gets the right answer for free.
export const HUE_SLOTS = [
  "azure",
  "sky",
  "indigo",
  "violet",
  "magenta",
  "red",
  "amber",
  "lime",
  "grass",
  "jade",
  "teal",
  "slate",
  "sand",
] as const;

export type HueSlot = (typeof HUE_SLOTS)[number];

// Stable name → slot. Same name, same slot, every render, browser and theme —
// we store no colours, so the name is the only thing to key on. FNV-ish 31x
// accumulate, kept inside a prime modulus so long names can't collapse together.
export function hueSlotFor(name: string): HueSlot {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 100003;
  return HUE_SLOTS[h % HUE_SLOTS.length];
}

// The CSS variable for that slot, ready to drop into a style prop.
export function hueVarFor(name: string): string {
  return `var(--hue-${hueSlotFor(name)})`;
}
