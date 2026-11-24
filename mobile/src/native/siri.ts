/**
 * Siri, the Action button, the widgets and the launcher: the phone's own
 * ways of reaching an agent from outside the app.
 *
 * iOS (ios/App/App/AppIntents.swift, ios/App/OlanoWidgets): "Ask an Agent"
 * runs a turn and Siri speaks the reply, with no part for the screens to
 * play; "Talk to an Agent" opens the app in Hands-Free Mode with the agent;
 * the widgets and the Control Center controls open the app through its own
 * links (`olano://talk`, `olano://open`, src/siri/deepLinks.ts). All of them
 * need to know the agents, so the app publishes its roster through
 * `OlanoShell.setAgents` whenever the list loads or the default agent
 * changes (`publishRoster`, the default first, src/state/agentPrefs.ts), and
 * hands the widgets the session right after (src/native/widgets.ts); on
 * Android the same roster call makes the launcher's "Talk to ..." shortcuts
 * (a long press on the app icon).
 *
 * A "talk" request reaches the screens as the `talk` event while the app is
 * running, through `OlanoShell.takeTalk()` at launch and on every resume (a
 * cold start, a launcher shortcut), or as the `olano://talk` link. It is held
 * in `talkRequest`; App.tsx opens a new conversation with the agent, and the
 * conversation starts Hands-Free Mode the moment it is on screen
 * (`takeTalkFor`). An `olano://open` link becomes a route for the app's router
 * (`pendingRoute`, the same hand-off a tapped notification uses), or, naming
 * only an agent, an `agentRequest` App.tsx resolves to the agent's last
 * conversation the way the Chats list does.
 *
 * On iOS every link comes through `OlanoShell.takeDeepLink()`: asked at
 * launch, on every resume and whenever the `deeplink` event says one is
 * waiting (OlanoShellPlugin.swift keeps the newest in a one-shot slot).
 * Not Capacitor's `appUrlOpen` + `getLaunchUrl()`: the launch link's event
 * reaches only the first listener registered for it (the share handler),
 * and `getLaunchUrl()` keeps answering the same link for the life of the
 * process, so a page loaded again on return to the app (a blank web view)
 * opened whatever was tapped last. In a desktop browser
 * `window.__olanoTalk(agent)` and `window.__olanoDeepLink(url)` stand in for
 * the phone (the end-to-end tests use them).
 */

import { App as CapApp } from "@capacitor/app";
import { registerPlugin, type PluginListenerHandle } from "@capacitor/core";

import { deepLinkAction } from "../siri/deepLinks.ts";
import { rosterKey, rosterOf, talkAgentOf, type RosterEntry } from "../siri/roster.ts";
import { defaultAgent } from "../state/agentPrefs.ts";
import { createStore } from "../state/store.ts";
import { pendingRoute } from "./push.ts";
import { isNative, platform } from "./shell.ts";
import { syncWidgets } from "./widgets.ts";

interface SiriPlugin {
  setAgents(options: { agents: RosterEntry[]; default: string }): Promise<void>;
  takeTalk(): Promise<{ agent: string | null }>;
  /** iOS: the app's own link waiting to be opened, read once. */
  takeDeepLink(): Promise<{ url: string | null }>;
  addListener(event: "talk", listener: (data: { agent?: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: "deeplink", listener: () => void): Promise<PluginListenerHandle>;
}

const OlanoShell = registerPlugin<SiriPlugin>("OlanoShell");

export interface TalkRequest {
  agent: string;
  /** When it arrived, so the same agent asked for twice is two requests. */
  at: number;
}

/** A "Talk to an Agent" waiting for its conversation to open. */
export const talkRequest = createStore<TalkRequest | null>(null);

export interface AgentRequest {
  agent: string;
  at: number;
}

/** An `olano://open?agent=` link naming only the agent: its last conversation opens (App.tsx). */
export const agentRequest = createStore<AgentRequest | null>(null);

let published = "";
let lastAgents: { name: string; display_name?: string }[] = [];

/** The agents the phone may name, published once per change, the default agent first. */
export function publishRoster(agents: { name: string; display_name?: string }[]): void {
  lastAgents = agents;
  if (!isNative()) return;
  const preferred = defaultAgent.get();
  const roster = rosterOf(agents, preferred);
  const key = rosterKey(roster);
  if (key === published) return;
  published = key;
  const chosen = roster.some((entry) => entry.name === preferred) ? preferred : "";
  OlanoShell.setAgents({ agents: roster, default: chosen })
    .then(() => syncWidgets())
    .catch(() => {
      // An older build without the method, or a launcher that refused: try
      // again when the list next loads.
      published = "";
    });
}

// A new default agent reorders the roster: publish it again.
defaultAgent.subscribe(() => publishRoster(lastAgents));

export function requestTalk(raw: unknown): void {
  const agent = talkAgentOf(raw);
  if (agent) talkRequest.set({ agent, at: Date.now() });
}

/** Drops a request that cannot be met (no such agent). */
export function dropTalk(): void {
  talkRequest.set(null);
}

/** Called by a conversation once it is on screen: was it opened to talk with `agent`? Once. */
export function takeTalkFor(agent: string): boolean {
  const request = talkRequest.get();
  if (!request || request.agent !== agent) return false;
  talkRequest.set(null);
  return true;
}

/** One of the app's own links: a talk request, an agent, or a route. Returns whether it was one. */
export function openDeepLink(url: string): boolean {
  const action = deepLinkAction(url);
  if (!action) return false;
  if (action.kind === "talk") requestTalk(action.agent);
  else if (action.kind === "agent") agentRequest.set({ agent: action.agent, at: Date.now() });
  else pendingRoute.set(action.route);
  return true;
}

/** Called by App.tsx once it has opened (or refused) the agent an `agentRequest` named. */
export function takeAgentRequest(): void {
  agentRequest.set(null);
}

/** Wires the platform's way in; returns the undo. */
export function installTalkIn(): () => void {
  if (!isNative()) {
    const hook = window as unknown as { __olanoTalk?: (agent: string) => void; __olanoDeepLink?: (url: string) => boolean };
    hook.__olanoTalk = requestTalk;
    hook.__olanoDeepLink = openDeepLink;
    return () => {
      delete hook.__olanoTalk;
      delete hook.__olanoDeepLink;
    };
  }
  const cleanups: (() => void)[] = [];
  const take = async () => {
    try {
      requestTalk((await OlanoShell.takeTalk()).agent);
    } catch {
      // An older build without the method.
    }
  };
  const heard = OlanoShell.addListener("talk", (data) => requestTalk(data.agent));
  cleanups.push(() => void heard.then((h) => h.remove()).catch(() => undefined));
  const link = platform() === "ios";
  const takeLink = async () => {
    try {
      const { url } = await OlanoShell.takeDeepLink();
      if (url) openDeepLink(url);
    } catch {
      // An older build without the method.
    }
  };
  if (link) {
    // A link that reaches the app while the page runs: the event only says
    // one is waiting, the slot holds it until this read, so a page loaded
    // again mid-arrival still finds it at launch. Listening before the first
    // read, so a launch link handed over between the two is not left waiting.
    const waiting = OlanoShell.addListener("deeplink", () => void takeLink());
    cleanups.push(() => void waiting.then((h) => h.remove()).catch(() => undefined));
  }
  void take();
  if (link) void takeLink();
  const resumed = CapApp.addListener("resume", () => {
    void take();
    if (link) void takeLink();
  });
  cleanups.push(() => void resumed.then((h) => h.remove()));
  return () => {
    for (const cleanup of cleanups) cleanup();
  };
}
