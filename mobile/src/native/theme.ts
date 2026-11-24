/**
 * Light, dark, Volt, or the phone's own setting.
 *
 * Ionic's dark palette is switched by a class (`ion-palette-dark` on <html>,
 * palettes/dark.class.css) rather than a media query, so the app can follow
 * the phone or override it. The choice is kept on this device and applied
 * before the first paint (main.tsx), so the app never flashes the other theme.
 * Everything that has to match it listens here: the status bar
 * (native/lifecycle.ts) and the keyboard's appearance on iOS.
 *
 * Volt is the dashboard's black-and-chartreuse theme. On the phone it is the
 * dark palette (so every `.ion-palette-dark` rule, the status bar and the
 * keyboard behave as in Dark) plus a second class, `olano-theme-volt`, under
 * which theme/variables.css repaints the brand colour, the bubbles and the
 * surfaces. A theme that follows the phone has no Volt: the phone only knows
 * light and dark, so Volt is always an explicit choice.
 */

import { Capacitor } from "@capacitor/core";
import { Keyboard, KeyboardStyle } from "@capacitor/keyboard";

import { createStore } from "../state/store.ts";

export type ThemePreference = "system" | "light" | "dark" | "volt";

/** What is actually drawn: the two platform palettes, or Volt (dark + its own colours). */
export type Palette = "light" | "dark" | "volt";

export const THEME_CHOICES: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "volt", label: "Volt" },
];

/** The class the Volt colours hang off (theme/variables.css, theme/app.css). */
export const VOLT_CLASS = "olano-theme-volt";

const KEY = "olano.theme";

export interface ThemeState {
  preference: ThemePreference;
  /** What is on screen now: true for Dark and for Volt. */
  dark: boolean;
  palette: Palette;
}

function isPreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark" || value === "volt";
}

function readPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(KEY);
    return isPreference(value) ? value : "system";
  } catch {
    return "system";
  }
}

/** Which palette a preference draws, given the phone's own setting. */
export function resolvePalette(preference: ThemePreference, systemDark: boolean): Palette {
  if (preference === "volt") return "volt";
  if (preference === "dark") return "dark";
  if (preference === "light") return "light";
  return systemDark ? "dark" : "light";
}

/** Whether a preference shows a dark theme (Dark or Volt), given the phone's own setting. */
export function resolveDark(preference: ThemePreference, systemDark: boolean): boolean {
  return resolvePalette(preference, systemDark) !== "light";
}

/** The footnote under the Appearance control, in the words the user guide uses. */
export function describePreference(preference: ThemePreference): string {
  switch (preference) {
    case "system":
      return "Follows your phone's light or dark setting.";
    case "volt":
      return "Always black with a bright chartreuse accent, whatever your phone is set to.";
    default:
      return `Always ${preference}, whatever your phone is set to.`;
  }
}

const systemQuery = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;

export const theme = createStore<ThemeState>({ preference: "system", dark: false, palette: "light" });

function apply(): void {
  const preference = theme.get().preference;
  const palette = resolvePalette(preference, !!systemQuery?.matches);
  const dark = palette !== "light";
  const root = document.documentElement;
  root.classList.toggle("ion-palette-dark", dark);
  root.classList.toggle(VOLT_CLASS, palette === "volt");
  // Form controls, scrollbars and the browser's own surfaces follow too.
  root.style.colorScheme = dark ? "dark" : "light";
  for (const meta of Array.from(document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]'))) {
    meta.content = dark ? "#000000" : "#f2f2f7";
    meta.removeAttribute("media");
  }
  if (Capacitor.getPlatform() === "ios" && Capacitor.isPluginAvailable("Keyboard")) {
    Keyboard.setStyle({ style: dark ? KeyboardStyle.Dark : KeyboardStyle.Light }).catch(() => undefined);
  }
  theme.set({ preference, dark, palette });
}

/** Applies the saved choice and follows the phone's setting from now on. Call once, before rendering. */
export function initTheme(): void {
  theme.set((s) => ({ ...s, preference: readPreference() }));
  apply();
  systemQuery?.addEventListener("change", apply);
}

export function setThemePreference(preference: ThemePreference): void {
  try {
    localStorage.setItem(KEY, preference);
  } catch {
    // Private storage off: the choice holds until the app restarts.
  }
  theme.set((s) => ({ ...s, preference }));
  apply();
}
