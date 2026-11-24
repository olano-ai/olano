/**
 * Going back safely. The app has one IonRouterOutlet for all four tabs, and
 * Ionic's `goBack()` with nothing to go back to does nothing while the page
 * animation has already started, which is one way to land on a blank (black,
 * in the dark theme) screen. These go back only when this page was pushed by
 * another, and otherwise move to a parent that always exists.
 */

import type { UseIonRouterResult } from "@ionic/react";

/** Back to the page that pushed this one, or to `fallback` as a backwards step. */
export function goBackOr(router: UseIonRouterResult, fallback: string): void {
  if (router.routeInfo?.pushedByRoute) router.goBack();
  else router.push(fallback, "back", "pop");
}

/**
 * To `path`, going back when it is the page right under this one: pushing it
 * forward would put the same page in history twice, and a swipe back from the
 * second copy reveals a page Ionic is about to remove.
 */
export function goTo(router: UseIonRouterResult, path: string): void {
  if (router.routeInfo?.pushedByRoute === path) router.goBack();
  else router.push(path);
}
