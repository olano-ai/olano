/**
 * This phone's choice of a default agent (Settings, Siri and widgets): the
 * one Siri's "talk to Olano" means, the one a Control Center control, the
 * Action button or a widget reaches when none was picked for it, and the
 * first of the launcher's shortcuts on Android. Kept on the device like the
 * theme; "" means the first agent in the list.
 */

import { createStore, useStore } from "./store.ts";

const KEY = "olano.defaultAgent";

function read(): string {
  try {
    return (localStorage.getItem(KEY) ?? "").trim();
  } catch {
    return "";
  }
}

export const defaultAgent = createStore<string>(read());

export function setDefaultAgent(name: string): void {
  defaultAgent.set(name.trim());
  try {
    if (defaultAgent.get()) localStorage.setItem(KEY, defaultAgent.get());
    else localStorage.removeItem(KEY);
  } catch {
    // Not remembered past this session; it still applies now.
  }
}

export function useDefaultAgent(): string {
  return useStore(defaultAgent);
}
