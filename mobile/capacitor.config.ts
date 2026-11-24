/// <reference types="@capacitor/keyboard" />
/// <reference types="@capacitor/push-notifications" />
/// <reference types="@capacitor/splash-screen" />

import { readFileSync } from "node:fs";
import { join } from "node:path";

import type { CapacitorConfig } from "@capacitor/cli";
import { KeyboardResize } from "@capacitor/keyboard";

// The Capacitor CLI evaluates this file from the project root (mobile/).
const { version } = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf8")) as { version: string };

/**
 * The app's screens are bundled (webDir `www`, built by Vite from src/) and
 * always load from the app itself; there is no `server.url`. The deployment is
 * only ever a server the screens call through the OlanoHttp plugin
 * (OlanoHttpPlugin.swift / .java), which is why nothing here names it.
 */
const config: CapacitorConfig = {
  appId: "ai.olano.app",
  appName: "Olano",
  webDir: "www",
  // Shown behind the web view before the first paint.
  backgroundColor: "#000000",
  // OlanoHttp sends its own user agent; this one is for anything the web view fetches itself.
  appendUserAgent: `OlanoApp/${version}`,
  ios: {
    // The web view fills the screen and pages pad themselves with
    // env(safe-area-inset-*) under viewport-fit=cover. "automatic" would add
    // UIKit's own insets on top and double the padding.
    contentInset: "never",
  },
  plugins: {
    // Both stay off (their default) on purpose. The app talks to the deployment
    // through OlanoHttp, which streams; CapacitorHttp buffers whole responses.
    // CapacitorCookies would install a java.net.CookieHandler on Android, and
    // HttpURLConnection would then send the session cookie a second time next to
    // the one OlanoHttp adds itself.
    CapacitorHttp: { enabled: false },
    CapacitorCookies: { enabled: false },
    Keyboard: {
      // The web view shrinks above the keyboard, so a conversation's composer
      // (an Ionic footer) rides on top of it and the messages stay scrollable.
      resize: KeyboardResize.Native,
      resizeOnFullScreen: true,
    },
    SystemBars: {
      // Android: on WebView 140+ a page with viewport-fit=cover is drawn edge to
      // edge and env(safe-area-inset-*) reports the real insets; on older
      // WebViews the web view is padded clear of the bars instead and the
      // insets read 0. Either way Ionic's safe-area padding is right.
      // "css" would also inject --safe-area-inset-* variables, which nothing reads.
      insetsHandling: "native",
      initialViewportFitValueHint: "cover",
      // Light icons for the dark launch; the app matches the phone's scheme once it runs.
      style: "DARK",
    },
    SplashScreen: {
      // Hidden after this long, or sooner when the app calls SplashScreen.hide()
      // on its first render (native/lifecycle.ts).
      launchShowDuration: 500,
      launchAutoHide: true,
      launchFadeOutDuration: 200,
      backgroundColor: "#000000",
      showSpinner: false,
    },
    PushNotifications: {
      // Nothing while the app is open: it shows live updates itself.
      // Banners appear only when the app is in the background or closed.
      presentationOptions: [],
    },
  },
};

export default config;
