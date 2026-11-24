/**
 * The app frame: the connect and sign-in screens before a session, then the
 * four tabs (Chats, Cortex, Inbox, Settings), each with its own page stack,
 * back button and iOS swipe back.
 */

import {
  IonApp,
  IonBadge,
  IonIcon,
  IonLabel,
  IonRouterOutlet,
  IonSpinner,
  IonTabBar,
  IonTabButton,
  IonTabs,
  useIonActionSheet,
  useIonRouter,
  useIonToast,
} from "@ionic/react";
import { IonReactRouter } from "@ionic/react-router";
import { chatbubbles, chatbubblesOutline, fileTray, fileTrayOutline, settings, settingsOutline } from "ionicons/icons";
import { useEffect, useState } from "react";
import { Navigate, Route, useLocation } from "react-router-dom";

import { useInboxCount } from "./screens/inbox/useInbox.ts";
import { AgentInfoPage } from "./screens/agents/AgentInfoPage.tsx";
import { AgentsPage, useAgents } from "./screens/chats/AgentsPage.tsx";
import { ConversationPage } from "./screens/chats/ConversationPage.tsx";
import { GroupInfoPage } from "./screens/chats/GroupInfoPage.tsx";
import { GroupPage } from "./screens/chats/GroupPage.tsx";
import { ThreadsPage } from "./screens/chats/ThreadsPage.tsx";
import { CortexItemPage } from "./screens/cortex/CortexItemPage.tsx";
import { CortexPage } from "./screens/cortex/CortexPage.tsx";
import { PulsePage } from "./screens/cortex/PulsePage.tsx";
import { InboxPage } from "./screens/inbox/InboxPage.tsx";
import { IssuePage } from "./screens/inbox/IssuePage.tsx";
import { ScheduledPage, ScheduledTaskPage } from "./screens/scheduled/ScheduledPage.tsx";
import { FilesHomePage, FilesPage } from "./screens/files/FilesPage.tsx";
import { SettingsPage } from "./screens/settings/SettingsPage.tsx";
import { UsagePage } from "./screens/settings/UsagePage.tsx";
import { ConnectScreen } from "./screens/start/ConnectScreen.tsx";
import { OfflineScreen, SetupScreen } from "./screens/start/StatusScreens.tsx";
import { SignInScreen } from "./screens/start/SignInScreen.tsx";
import { installAppLock } from "./native/appLock.ts";
import { installLifecycle } from "./native/lifecycle.ts";
import { pendingRoute } from "./native/push.ts";
import { claimShare, discardShare, incomingShare, installShareIn } from "./native/shareIn.ts";
import { agentRequest, dropTalk, installTalkIn, takeAgentRequest, talkRequest } from "./native/siri.ts";
import { openAgentPath } from "./chat/openAgent.ts";
import { displayName } from "./ui/Avatar.tsx";
import { notice } from "./state/notice.ts";
import { currentApi, session, startSession } from "./state/session.ts";
import { FileViewer } from "./ui/FileViewer.tsx";
import { LockScreen } from "./ui/LockScreen.tsx";
import { brain, brainBold } from "./ui/icons.ts";
import { PhotoViewer } from "./ui/PhotoViewer.tsx";
import { useTabBarHidden } from "./state/chrome.ts";
import { redirectFor, showsInbox } from "./state/appLayout.ts";
import { useAppLayout } from "./state/layoutStore.ts";
import { useStore } from "./state/store.ts";
import { loadAppConfig } from "./state/appConfig.ts";

export function App() {
  const { phase } = useStore(session);

  useEffect(() => {
    void startSession();
    // The demo entry's switch (src/appConfig.ts); the screens draw from the
    // kept copy meanwhile, so nothing waits on it.
    void loadAppConfig();
    const undoLifecycle = installLifecycle();
    const undoShare = installShareIn();
    const undoTalk = installTalkIn();
    const undoLock = installAppLock();
    return () => {
      undoLifecycle();
      undoShare();
      undoTalk();
      undoLock();
    };
  }, []);

  return (
    <IonApp>
      {phase === "starting" && (
        <div className="olano-splash">
          <IonSpinner name="crescent" />
        </div>
      )}
      {phase === "connect" && <ConnectScreen />}
      {phase === "signin" && <SignInScreen />}
      {phase === "setup" && <SetupScreen />}
      {phase === "offline" && <OfflineScreen />}
      {phase === "ready" && (
        <IonReactRouter>
          <Tabs />
        </IonReactRouter>
      )}
      {phase === "ready" && <PhotoViewer />}
      {phase === "ready" && <FileViewer />}
      <LockScreen active={phase === "ready"} />
    </IonApp>
  );
}

function Tabs() {
  const location = useLocation();
  const router = useIonRouter();
  const layout = useAppLayout();
  const inbox = useInboxCount(layout);
  const route = useStore(pendingRoute);
  const message = useStore(notice);
  const [toast] = useIonToast();

  useEffect(() => {
    if (!message) return;
    void toast({ message: message.text, duration: 3500, position: "top", color: "dark" });
  }, [message, toast]);

  useSharePicker();
  useTalkOpener();
  useAgentOpener();

  // A tapped notification, or one of the app's own links (a widget, a
  // control): open what it is about (unless the admin hid it).
  const settled = useRouterSettled();
  const here = location.pathname + location.search;
  useEffect(() => {
    if (!route || !settled) return;
    pendingRoute.set(null);
    const to = redirectFor(route, layout) ?? route;
    // Already there (the widget of the conversation on screen): nothing to
    // do. A link with a query readies the composer per arrival, so it is
    // pushed again.
    if (to === here && !to.includes("?")) return;
    // Forward, not "root": a root navigation unmounts every tab's pages and
    // erases history, and the page it lands on can keep a back target that no
    // longer exists, so the next back showed a blank screen.
    router.push(to, "forward", "push");
  }, [route, router, layout, settled, here]);

  // On a screen the admin has since hidden (the layout is reread every time
  // the app comes back): move to where it would have been reachable from.
  const hiddenHere = redirectFor(location.pathname, layout);
  useEffect(() => {
    // A tab's own root may be a root navigation; anything deeper steps back to it.
    if (hiddenHere) router.push(hiddenHere, hiddenHere === "/chats" || hiddenHere === "/settings" ? "root" : "back", "replace");
  }, [hiddenHere, router]);
  // A conversation gets the whole screen, like Messages: the composer sits
  // where the tab bar would be.
  const inConversation =
    /^\/chats\/[^/]+\/[^/]+/.test(location.pathname) &&
    !/^\/chats\/_groups\/[^/]+\/info$/.test(location.pathname) &&
    !/^\/chats\/[^/]+\/_(scheduled|files|info)(\/|$)/.test(location.pathname);
  // So does an issue: its comment box sits where the tab bar would be. A
  // screen in a mode of its own (Cortex selecting rows) asks the same way.
  const held = useTabBarHidden();
  const fullScreen = held || inConversation || /^\/inbox\/issues\//.test(location.pathname);
  // The selected tab's icon is filled, the others outlined, as on iOS.
  const tab = location.pathname.split("/")[1] || "chats";
  const icon = (name: string, filled: string, outline: string) => (tab === name ? filled : outline);

  return (
    <IonTabs>
      <IonRouterOutlet>
        <Route path="/chats" element={<AgentsPage />} />
        {/* Agent names start with a letter or digit, so "_groups" is never one. */}
        <Route path="/chats/_groups/:id" element={<GroupPage />} />
        <Route path="/chats/_groups/:id/info" element={<GroupInfoPage />} />
        <Route path="/chats/:agent" element={<ThreadsPage />} />
        {/* Thread ids never start with "_", so "_scheduled", "_files" and "_info" are never one. */}
        <Route path="/chats/:agent/_scheduled" element={<ScheduledPage />} />
        <Route path="/chats/:agent/_scheduled/:task" element={<ScheduledTaskPage />} />
        <Route path="/chats/:agent/_info" element={<AgentInfoPage />} />
        <Route path="/chats/:agent/_files" element={<FilesPage />} />
        <Route path="/chats/:agent/_files/:folder" element={<FilesPage />} />
        <Route path="/chats/:agent/:thread" element={<ConversationPage />} />
        <Route path="/cortex" element={<CortexPage />} />
        <Route path="/cortex/pulse" element={<PulsePage />} />
        <Route path="/cortex/:agent/:item" element={<CortexItemPage />} />
        <Route path="/inbox" element={<InboxPage />} />
        <Route path="/inbox/issues/:issue" element={<IssuePage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/settings/scheduled" element={<ScheduledPage />} />
        <Route path="/settings/scheduled/:task" element={<ScheduledTaskPage />} />
        <Route path="/settings/files" element={<FilesHomePage />} />
        <Route path="/settings/files/:agent" element={<FilesPage under="settings" />} />
        <Route path="/settings/files/:agent/:folder" element={<FilesPage under="settings" />} />
        <Route path="/settings/usage" element={<UsagePage />} />
        <Route path="/" element={<Navigate to="/chats" replace />} />
        <Route path="*" element={<Navigate to="/chats" replace />} />
      </IonRouterOutlet>
      <IonTabBar slot="bottom" className={fullScreen ? "olano-tabbar-hidden" : undefined}>
        <IonTabButton tab="chats" href="/chats">
          <IonIcon ios={icon("chats", chatbubbles, chatbubblesOutline)} md={chatbubbles} />
          <IonLabel>Chats</IonLabel>
        </IonTabButton>
        {layout.cortexTab && (
          <IonTabButton tab="cortex" href="/cortex" className="olano-cortex-tab">
            <IonIcon ios={icon("cortex", brainBold, brain)} md={brainBold} />
            <IonLabel>Cortex</IonLabel>
          </IonTabButton>
        )}
        {showsInbox(layout) && (
          <IonTabButton tab="inbox" href="/inbox">
            <IonIcon ios={icon("inbox", fileTray, fileTrayOutline)} md={fileTray} />
            <IonLabel>Inbox</IonLabel>
            {inbox > 0 && <IonBadge color="primary">{inbox > 99 ? "99+" : inbox}</IonBadge>}
          </IonTabButton>
        )}
        <IonTabButton tab="settings" href="/settings">
          <IonIcon ios={icon("settings", settings, settingsOutline)} md={settings} />
          <IonLabel>Settings</IonLabel>
        </IonTabButton>
      </IonTabBar>
    </IonTabs>
  );
}

/**
 * How long after the router leaves `/` a navigation on arrival waits. Ionic
 * keeps the redirect's `<Navigate>` mounted for 100 ms after it fires
 * (`NAVIGATE_REDIRECT_DELAY_MS` in @ionic/react-router), and react-router
 * runs a mounted `<Navigate>` again on every location change, so a push
 * inside that window was replaced straight back to Chats.
 */
const REDIRECT_SETTLE_MS = 350;

/**
 * Whether a navigation on arrival (a link the app was launched with, a talk
 * request answered from the agent list kept on the phone) may be made. The
 * tabs mount at `/`, and the route there redirects to Chats in an effect of
 * its own: a push made before that redirect, or within its window above, lost
 * to the redirect's replace, and the app opened on Chats instead of the link.
 */
function useRouterSettled(): boolean {
  const atRoot = useLocation().pathname === "/";
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    if (atRoot || settled) return;
    const timer = window.setTimeout(() => setSettled(true), REDIRECT_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [atRoot, settled]);
  return settled;
}

/**
 * "Talk to an Agent" (Siri, the Action button, a launcher shortcut): open a
 * new conversation with the agent; the conversation starts Hands-Free Mode
 * once it is on screen (ConversationPage, `takeTalkFor`).
 */
function useTalkOpener() {
  const talk = useStore(talkRequest);
  const [agents] = useAgents();
  const router = useIonRouter();
  const [toast] = useIonToast();
  const settled = useRouterSettled();
  const list = agents.data;

  useEffect(() => {
    if (!talk || !list || !settled) return;
    const agent = list.find((a) => a.name === talk.agent);
    if (!agent) {
      dropTalk();
      void toast({ message: `There is no agent called ${talk.agent} on this deployment.`, duration: 4000, position: "top", color: "dark" });
      return;
    }
    router.push(`/chats/${encodeURIComponent(agent.name)}/new`, "forward", "push");
  }, [talk, list, router, toast, settled]);
}

/**
 * A link naming only an agent (the Agent and Team widgets, a control with
 * no conversation to name yet): open it the way a tap in Chats does, on the
 * last conversation the person had with it, or a new one.
 */
function useAgentOpener() {
  const request = useStore(agentRequest);
  const [agents] = useAgents();
  const router = useIonRouter();
  const [toast] = useIonToast();
  const settled = useRouterSettled();
  const list = agents.data;

  useEffect(() => {
    if (!request || !list || !settled) return;
    const agent = list.find((a) => a.name === request.agent);
    if (!agent) {
      takeAgentRequest();
      void toast({ message: `There is no agent called ${request.agent} on this deployment.`, duration: 4000, position: "top", color: "dark" });
      return;
    }
    // The request is taken once its conversation is known: taking it first
    // would re-run this effect with none and cancel the read below. A newer
    // request arriving meanwhile cancels this one (the newest wins).
    let current = true;
    void openAgentPath(currentApi(), agent.name).then((path) => {
      if (!current) return;
      takeAgentRequest();
      router.push(path, "forward", "push");
    });
    return () => {
      current = false;
    };
  }, [request, list, router, toast, settled]);
}

/**
 * Something shared into Olano from another app: ask which agent gets it, then
 * open a new conversation with it in the composer (it is not sent until the
 * person sends it).
 */
function useSharePicker() {
  const share = useStore(incomingShare);
  const [agents] = useAgents();
  const router = useIonRouter();
  const [present, dismiss] = useIonActionSheet();
  const asking = share !== null && share.agent === null;
  const list = agents.data;

  useEffect(() => {
    if (!asking || !list) return;
    const open = (name: string) => {
      claimShare(name);
      router.push(`/chats/${encodeURIComponent(name)}/new`, "forward", "push");
    };
    if (list.length === 1) {
      open(list[0].name);
      return;
    }
    const item = incomingShare.get();
    const count = item?.files.length ?? 0;
    const what = count === 1 ? item!.files[0].name : count > 1 ? `${count} files` : "This";
    void present({
      header: "Send to which agent?",
      subHeader: `${what} opens in a new conversation. Nothing is sent until you send it.`,
      buttons: [
        ...list.map((agent) => ({ text: displayName(agent), handler: () => open(agent.name) })),
        { text: "Cancel", role: "cancel", handler: () => discardShare() },
      ],
      onDidDismiss: (event) => {
        if (event.detail.role === "backdrop") discardShare();
      },
    });
    return () => void dismiss();
  }, [asking, list, present, dismiss, router]);
}
