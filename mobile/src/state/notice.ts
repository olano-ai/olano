/**
 * A short message for the person from outside any screen (an approval
 * answered from a notification, a file shared in from another app): the app
 * shows it as a toast over whatever is on screen.
 */

import { createStore } from "./store.ts";

export const notice = createStore<{ text: string; at: number } | null>(null);

export function say(text: string): void {
  notice.set({ text, at: Date.now() });
}
