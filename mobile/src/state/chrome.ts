/**
 * What a screen asks of the app's chrome. A conversation and an issue take
 * the whole screen by route (App.tsx knows their paths); a screen in a mode
 * of its own, such as the Cortex list while rows are being selected, asks
 * here, so its action bar can sit where the tab bar was.
 */

import { useEffect } from "react";

import { createStore, useStore } from "./store.ts";

/** How many screens currently want the tab bar out of the way. */
const tabBarHolds = createStore(0);

export function useTabBarHidden(): boolean {
  return useStore(tabBarHolds) > 0;
}

/** Hides the tab bar while `active`, and gives it back on unmount. */
export function useHideTabBar(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    tabBarHolds.set((n) => n + 1);
    return () => tabBarHolds.set((n) => Math.max(0, n - 1));
  }, [active]);
}
