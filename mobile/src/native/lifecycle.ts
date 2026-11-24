/**
 * Foreground and background.
 *
 * Going to the background closes every stream the app holds (the chat's and
 * the event stream). A turn keeps running on the deployment, and with nobody
 * watching it the deployment sends a push notification when the reply lands.
 * Coming back rejoins each turn from the last frame the app saw and catches
 * the lists up.
 */

import { App as CapApp } from "@capacitor/app";
import { Keyboard } from "@capacitor/keyboard";
import { SplashScreen } from "@capacitor/splash-screen";
import { Capacitor, SystemBars, SystemBarsStyle } from "@capacitor/core";

import { allConversations, closeAllConversations } from "../chat/conversation.ts";
import { allGroupConversations, closeAllGroupConversations } from "../chat/groupConversation.ts";
import { closeAllLiveBrowsers, pauseLiveBrowsers, resumeLiveBrowsers } from "../browser/live.ts";
import { invalidate, resetResources } from "../state/resources.ts";
import { resetArrangement } from "../state/arrangement.ts";
import { clearMediaCache } from "../ui/mediaCache.ts";
import { theme } from "./theme.ts";
import { stopVoice } from "../ui/audioPlayer.ts";
import { resetReadAloud, stopReading } from "../ui/readAloud.ts";
import { openLink } from "../ui/Markdown.tsx";
import { startLive, stopLive } from "../state/live.ts";
import { refreshLayout, resetLayout } from "../state/layoutStore.ts";
import { session, type Phase } from "../state/session.ts";
import { enablePush, installPushTaps, permission } from "./push.ts";
import { isNative } from "./shell.ts";
import { SOFT_KEYBOARD_CLASS, isSoftKeyboard } from "./softKeyboard.ts";
import { syncWidgets } from "./widgets.ts";

let foreground = true;

function toBackground(): void {
  if (!foreground) return;
  foreground = false;
  stopLive();
  stopVoice();
  stopReading();
  for (const conversation of allConversations()) conversation.park();
  for (const group of allGroupConversations()) group.park();
  pauseLiveBrowsers();
}

function toForeground(): void {
  if (foreground) return;
  foreground = true;
  if (session.get().phase !== "ready") return;
  startLive();
  // The admin may have changed what this app shows since it was last open.
  void refreshLayout();
  for (const conversation of allConversations()) conversation.resume();
  for (const group of allGroupConversations()) group.resume();
  resumeLiveBrowsers();
  invalidate("");
  // A widget added while the app was away draws once it has the session.
  syncWidgets();
}

let overDark = false;

function applySystemBars(): void {
  if (!Capacitor.isNativePlatform()) return;
  // Dark text on the light theme; light text on the dark theme and on screens drawn on black.
  const style = !overDark && !theme.get().dark ? SystemBarsStyle.Light : SystemBarsStyle.Dark;
  SystemBars.setStyle({ style }).catch(() => undefined);
}

/** A screen drawn on black (the photo viewer) wants light status bar text, whatever the theme. */
export function setDarkOverlay(on: boolean): void {
  overDark = on;
  applySystemBars();
}

/** Wires the app lifecycle once; returns the undo for React's effect. */
export function installLifecycle(): () => void {
  const cleanups: (() => void)[] = [];

  applySystemBars();
  cleanups.push(theme.subscribe(applySystemBars));
  if (Capacitor.isPluginAvailable("SplashScreen")) void SplashScreen.hide().catch(() => undefined);

  // A web link tapped anywhere in the app opens in the in-app browser: the
  // app's own web view never navigates away from the app. Screens that open
  // their links themselves have already handled the click.
  const onLinkClick = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0) return;
    const anchor = (event.target as Element | null)?.closest?.("a[href]");
    if (!(anchor instanceof HTMLAnchorElement)) return;
    const href = anchor.getAttribute("href") || "";
    if (!/^(https?:|mailto:|tel:)/i.test(href)) return;
    event.preventDefault();
    void openLink(href);
  };
  document.addEventListener("click", onLinkClick);
  cleanups.push(() => document.removeEventListener("click", onLinkClick));

  const onVisibility = () => (document.hidden ? toBackground() : toForeground());
  document.addEventListener("visibilitychange", onVisibility);
  cleanups.push(() => document.removeEventListener("visibilitychange", onVisibility));

  if (isNative()) {
    const pause = CapApp.addListener("pause", toBackground);
    const resume = CapApp.addListener("resume", toForeground);
    cleanups.push(() => void pause.then((h) => h.remove()));
    cleanups.push(() => void resume.then((h) => h.remove()));
    installPushTaps();
  }
  if (isNative() && Capacitor.isPluginAvailable("Keyboard")) {
    // While the keyboard is up it covers the home indicator, so the composer
    // need not keep clear of it (`.olano-keyboard-open` in app.css). Whether it
    // is the phone's own keyboard decides what return does (softKeyboard.ts).
    const root = document.documentElement;
    const shown = Keyboard.addListener("keyboardWillShow", (info) => {
      root.classList.add("olano-keyboard-open");
      root.classList.toggle(SOFT_KEYBOARD_CLASS, isSoftKeyboard(info?.keyboardHeight));
    });
    const hidden = Keyboard.addListener("keyboardWillHide", () => {
      root.classList.remove("olano-keyboard-open", SOFT_KEYBOARD_CLASS);
    });
    cleanups.push(() => void shown.then((h) => h.remove()));
    cleanups.push(() => void hidden.then((h) => h.remove()));
  }

  // Signed in: open the event stream and refresh push silently. Signed out:
  // close everything that belonged to the old session.
  let previous: Phase = session.get().phase;
  const unsubscribe = session.subscribe(() => {
    const phase = session.get().phase;
    if (phase === previous) return;
    const wasReady = previous === "ready";
    previous = phase;
    if (phase === "ready") {
      startLive();
      void refreshLayout();
      // Back in the foreground before the session was ready (a resume that
      // re-checked the sign-in): `toForeground` returned early, so the
      // conversations parked on the way out are rejoined here instead.
      if (foreground) {
        for (const conversation of allConversations()) conversation.resume();
        for (const group of allGroupConversations()) group.resume();
      }
      void permission().then((state) => {
        if (state === "granted") void enablePush({ prompt: false });
      });
    } else if (wasReady) {
      stopLive();
      closeAllConversations();
      closeAllGroupConversations();
      closeAllLiveBrowsers();
      resetResources();
      resetArrangement();
      resetLayout();
      clearMediaCache();
      resetReadAloud();
    }
  });
  cleanups.push(unsubscribe);

  return () => {
    for (const cleanup of cleanups) cleanup();
  };
}
