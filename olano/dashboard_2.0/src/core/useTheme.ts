import { useSyncExternalStore } from "react";
import { getTheme, subscribeTheme, type Theme } from "./theme";

// The active theme, re-rendering whenever any surface changes it.
export function useTheme(): Theme {
  return useSyncExternalStore(subscribeTheme, getTheme, getTheme);
}
