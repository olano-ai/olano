/**
 * The remote app config (src/appConfig.ts) as a store the screens read.
 *
 * It starts as the copy kept on the phone (or the bundled one) so the connect
 * screen draws at once, and `loadAppConfig()` refreshes it in the background
 * at launch. On a phone the request goes through OlanoHttp, whose one-origin
 * rule has a fixed exception for exactly this URL (GET, no cookies); in a
 * browser it is a plain credential-less fetch(), which the cloud route allows
 * with `Access-Control-Allow-Origin: *`.
 */

import { APP_CONFIG_URL, currentAppConfig, demoOrigin, refreshAppConfig, type AppConfig } from "../appConfig.ts";
import { getTransport } from "../net/transport.ts";
import { isNative } from "../native/shell.ts";
import { createStore } from "./store.ts";

export const appConfig = createStore<AppConfig>(currentAppConfig());

async function fetchText(url: string, timeoutMs: number): Promise<{ status: number; body: string }> {
  if (isNative()) {
    const response = await getTransport().request(url, { headers: { Accept: "application/json" }, timeoutMs });
    return { status: response.status, body: response.body };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      credentials: "omit",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    return { status: response.status, body: await response.text() };
  } finally {
    clearTimeout(timer);
  }
}

let inflight: Promise<void> | null = null;

/** Refreshes the config in the background; settles once, never rejects. */
export function loadAppConfig(force = false): Promise<void> {
  inflight ??= refreshAppConfig({ fetchText, force })
    .then((config) => appConfig.set(config))
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** True when `origin` is the demo deployment the config names (whether or not the entry is shown). */
export function isDemoOrigin(origin: string | null): boolean {
  if (!origin) return false;
  return demoOrigin(appConfig.get().demo.url) === origin;
}

export { APP_CONFIG_URL };
