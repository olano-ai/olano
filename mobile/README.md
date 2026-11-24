# Olano mobile app

The iOS and Android app for Olano: native-looking screens built with
[Ionic](https://ionicframework.com) 9 and React, in a
[Capacitor](https://capacitorjs.com) 8 shell.

Every Olano customer runs their own deployment (a "box") at its own HTTPS
address, usually `https://<name>.olano.run`, with its API at `/api/*`. The app
bundles its own screens and talks to that API; it never loads the deployment's
web pages. Four tabs:

- **Chats**: the agents (Pinned, the person's folders, All agents) and group
  chats, each agent's conversations, and a conversation with streamed replies,
  the agent's steps, plans, images and in-line approvals.
- **Cortex**: every agent's brainstorms, ideas, prototypes and projects, with
  Cortex's ratings; promote, discard, or talk an item over with its agent.
  Triage on the list: swipe right to approve, left to discard, Select for
  discarding several; rows not yet opened on this phone are tinted
  (`src/cortex/stages.ts` lists an item under the stage its promotion is
  heading for while it runs, `src/cortex/seen.ts` keeps the phone's read
  memory).
- **Inbox**: approvals waiting on the person, Pulse updates, and the
  deployment's alerts.
- **Settings**: account, push notifications per kind, appearance (System,
  Light, Dark), switch deployment, sign out.

Ionic draws every component in the platform's own style (iOS on an iPhone,
Material on Android), light or dark following the phone unless Settings picks
one (`src/native/theme.ts`: the class form of Ionic's dark palette,
`ion-palette-dark` on `<html>`, set before the first render). Navigation
is a stack per tab with a back button, the iOS swipe back and Android's back
button; lists pull to refresh.

The app needs only the API a deployment already has, so it works with
deployments that predate it. Push notifications need the push backend
(`olano/push/`) on the deployment.

## How it works

```
the app's screens (www/, capacitor://localhost or https://localhost)
  |
  |  OlanoHttp.request / OlanoHttp.stream        (native: URLSession, HttpURLConnection)
  v
https://<deployment>/api/...                      session cookie kept by native code
```

The screens cannot call the deployment with `fetch()`: they run on the app's
own origin, the runtime answers credentialed cross-origin requests without CORS
headers, and its session cookie is first-party to the deployment. Capacitor's
own `CapacitorHttp` would get around both but buffers whole responses, and the
chat is server-sent events. So the app has its own plugin, **OlanoHttp**,
which streams.

In a desktop browser (`npm run dev`, the end-to-end test) the same screens use
`fetch()` on their own origin and Vite proxies `/api` to a runtime
(`src/net/transport.ts` picks the transport).

A turn runs on the deployment, not in the app (`olano/core/runs.py`): the
app's stream is only a view of it. Leaving a conversation keeps its stream; the
app going to the background closes all of them on purpose, and coming back
rejoins each turn from the last frame it saw (`GET /api/chat/attach?cursor=N`).
That is also what lets the deployment send a push notification when the reply
lands: it notifies only when nobody watches.

Every turn the app sends carries `client: "mobile"` on `POST /api/chat/stream`.
The deployment stamps such a conversation's channel `mobile`, so it reads
MOBILE in the app and the dashboard and gets its own filter chip
(`core/thread_labels.channel_for_request`); the turn's request context keeps
channel `api`, so it is trusted exactly like the dashboard. A runtime that
predates the field ignores it and labels the conversation DASHBOARD.

Tapping an agent opens the last conversation a person had with it
(`src/chat/threadPick.ts`, the dashboard's own rule plus Cortex threads left
out); the whole list, filtered by source with the dashboard's buckets
(`src/chat/threadFilters.ts`), is the conversation menu's All Conversations.

Photos travel the way the dashboard sends them: `attachments` on
`POST /api/chat/stream`, each `{kind: "image", name, mime, data}` in base64
(`olano/runtime/chat_attachments.py`), so they need nothing new on the
deployment either. The phone prepares each one first (`src/native/photos.ts`):
drawn upright onto a canvas at most 2048 px on its longest side and saved as
JPEG, which drops the location and other metadata, keeps a ten-photo message
small enough for a phone connection, and stays under the runtime's 10 MB
per-file cap. One at a time, because ten full-size originals decoded at once
outgrow an older phone's web view. The runtime saves each photo under the
agent's `uploads/` and writes a note per photo into the message for the agent;
a reloaded message gets that text back with the photos as media, and
`stripAttachmentNotes` (`src/chat/photos.ts`) leaves the person's own words.

Voice notes ride the same contract as `{kind: "audio", ...}`, as WAV: 16-bit
mono PCM at 16 kHz (`voice-<time>.wav`), which every speech-to-text service
reads and every phone plays. The web view records them through Web Audio
(`src/native/recorder.ts`: getUserMedia into a script processor, brought down
to 16 kHz as the samples arrive, written out with `wavHeader`/`wavBody` from
`src/chat/voice.ts`); Capacitor hands the page the microphone once the phone
has granted it, so no plugin is involved. The microphone and the audio context
are shared and kept warm (`src/native/microphone.ts`, over the pure
`src/chat/keepWarm.ts`): on an iPhone `getUserMedia` starts the web view's
capture process and switches the audio session, several hundred milliseconds
each press, and a recorder that opened it per press lost the first words of
every note. The stream is opened once, handed back after a note and closed
`KEEP_WARM_MS` (20 s) later, or at once when the app goes to the background;
`VoiceRecorder.start()` resolves on the first samples (the strip's dot is grey
until then, `.olano-recording.starting`, and a light haptic marks the moment),
the timer counts from them, and `stop()` waits up to 150 ms for the buffer
still in flight so the last syllable is kept. Not MediaRecorder: on an iPhone it
records fragmented MP4, which speech-to-text services refuse, and the first
iPhone voice notes arrived "not transcribed". Five minutes of WAV is 9.6 MB,
inside the runtime's 10 MB per-file cap, which is why that is the longest
note. For an administrator the runtime saves the recording and adds
`[Voice message transcript] ...` (or a note saying speech-to-text is off or
failed); for a member it transcribes from a temp file and saves nothing
(`transcribe_chat_audio`). The app reads the transcript back from the thread
once the turn starts and shows it under the voice note; `splitVoiceNotes`
(`src/chat/voice.ts`) must keep matching those two note shapes.

An iPhone transcribes a voice note itself before sending (OlanoSpeech above,
`wordsOnPhone` in `src/chat/conversation.ts`, unless Settings turns it off):
the message is then `voiceTranscriptMessage(words)`, the same
`[Voice message transcript] ...` line the runtime writes, with no attachment.
The agent starts at once, nothing depends on the deployment's speech-to-text,
the recording never leaves the phone, and a reloaded conversation shows the
words as a voice note, as it does for a member's. When the phone cannot (no
permission, no recognizer, no words heard), the recording goes as above. On
reload, a
saved recording comes back as media (`audio` for `.wav`; recordings from
before WAV were `.m4a`, or `.webm`, which `media_refs` lists as `video`, so
`isVoiceMedia` also takes a `voice-*.webm` video).
Playback goes through one shared audio element (`src/ui/audioPlayer.ts`), so
starting a voice note pauses the last.

Replies are read aloud by `src/ui/readAloud.ts`: the speaker under a reply,
and by itself after a voice note (Settings, `src/state/voicePrefs.ts`, kept on
the phone). It first asks the deployment, `POST /api/speech/speak` (text to
speech set up in Config, Speech; mp3 back, nothing written into the
workspace), and plays that through the voice notes' player; a 409 (no voice
there) or a 404 (a runtime from before) is remembered for five minutes and the
phone's own voice reads instead. `src/chat/speakable.ts` turns the Markdown
into what a person would say (no code blocks, links as their text, addresses
as their site). `/api/status` also reports `tts_enabled`.

A conversation's model and reasoning level are its own, as in the dashboard's
chat header: `src/chat/models.ts` builds the choices from `GET
/api/catalog/providers` with the dashboard's rules (usable providers only, the
deployment's defaults, then the Olano tiers cheapest first, then the rest), the
pair rides on every `POST /api/chat/stream` as `model` / `reasoning_effort`,
and a reopened thread restores it from the thread payload (the runner stamps
each reply with the model it ran on). The dashboard's own version of those
rules (`views/agents/catalog.ts`) keeps React state and an API client, so it
is mirrored here rather than imported.

The Chats list is arranged by the dashboard sidebar's own rules
(`views/nav/botArrangement.ts`, imported, its "groups" called folders here
because a group is a group chat): one home per agent (Pinned, a folder, or All
agents), pin order, one hand ranking across sections, folded sections.
`src/chat/arrangement.ts` turns an arrangement into sections. The arrangement
is the person's, stored in their preferences (`GET/PUT /api/auth/me/prefs`,
key `mobile_chat_arrangement`) with a copy on the phone for the first paint
(`src/state/arrangement.ts`); it is deliberately not the dashboard sidebar's,
which stays per browser.

Group chats use `/api/groups`: the list, a group's transcript, and a message
streamed through `POST /api/groups/{id}/messages/stream`, whose frames name
their author (`speaker` opens each agent's turn); the dashboard's
`views/groups/liveTurn.ts` grows the reply being written. The turn runs on the
deployment, so a dropped stream rejoins through `GET
.../messages/attach?cursor=N`. Agents answering each other afterwards arrive as
`group_updated` events on `/api/stream`, and the transcript is read again
(`src/chat/groupConversation.ts`). Group chats show when `GET /api/status`
reports `capabilities.groups_enabled`.

The agent's browser, when the deployment runs it live (`GET
/api/agents/{name}/browser` → `live_view`), is a WebSocket to
`/api/agents/{name}/browser/live`: JPEG frames as binary messages, the page's
state, the agent's pointer and the page's dialogs as JSON, and the person's
input back as `{t: ...}` messages with coordinates as fractions of the picture,
the protocol of the dashboard's `views/browser/LiveScreen.tsx` and
`liveInput.ts`. `src/browser/live.ts` holds one socket per agent, shared by the
floating window and the full view and closed in the background;
`src/browser/model.ts` is the pure half (fitting, touch to fraction, tap
counting, status wording). **The viewer paces the stream**: on open the
socket says `{t: "view", ack: true, w, h}` (its picture in device pixels,
`src/browser/viewSize.ts`, told again when it changes by more than a tenth
or the person zooms) and answers every frame with `{t: "shown"}` once the
picture has drawn it; the deployment sends the next frame only then (or after
1.5 s), no larger than the picture, and steps a slow viewer down to smaller,
rougher frames until it keeps up. A frame crosses the native bridge as a
base64 string the web view evaluates on the thread the taps need, so this is
what keeps taps working on a page that never stops redrawing, where the
earlier 12-frames-a-second cap alone still buried them. The deployment also
drops taps queued behind one the page did not take in 8 s (one message,
"not taking taps right now"), and refreshes the picture when nothing came
back after a tap, so the view can neither hang nor show the past.
What the browser draws outside the page comes as JSON the app answers with
its own UI: a `select` list (an action sheet), a `picker` for a date, time,
month, week or colour field (an alert with an input of that type, `setvalue`
back; a colour is typed as `#rrggbb`), the page's dialogs and file requests
(`state.dialog` / `state.chooser`), and a `notice` when a page asked for a
location, a camera, a microphone or leave to notify, which the agent's
browser refuses and the app explains as a toast.
Recordings and pictures go through REST, not the socket: `POST
/api/agents/{name}/browser/recording` with `{action: "start" | "stop"}`
(the state frame then carries `recording: {since, seconds, name}`, which
every viewer shows as the REC clock; stop answers with the saved `file`) and
`POST .../browser/picture` (the page as a PNG). Both land in the agent's
workspace (`recordings/`, `screenshots/`), and the toast's Open goes to the
Files screen for that folder. The deployment side is
`olano/browser/recording.py`: the live view's screencast fed to ffmpeg.
`browser_session` events on `/api/stream` refresh the status. Handing back after the agent asked for help sends the dashboard's
"Done: I've handed the browser back to you.", as a steer
(`POST /api/chat/steer`) when a turn is running.

Replies are rendered by the dashboard's `renderProse`, which formats text that
is already escaped: `src/chat/links.ts` (`proseHtml`) escapes first, as the
dashboard does, after turning the addresses the renderer leaves as text (a URL
in code formatting, a `www.` address) into Markdown links and percent-encoding
any `"` in a URL. The person's own messages are drawn by React with their
addresses as links (`src/ui/LinkText.tsx`). Every web link opens in the in-app
browser: a click on any `http(s)` anchor the screen did not handle is caught
app-wide (`src/native/lifecycle.ts`), so the app's own web view never
navigates away.

## Remote config and the demo

The connect screen offers **Try the demo** under the address field: it checks
the Olano-run demo deployment (`https://demo.olano.run` unless the config says
otherwise) like any address, saves it, and asks it for a demo session,
`POST /api/auth/demo` (`olano/runtime/demo_access.py`): no password is typed,
shipped or served. The demo box creates a member account per visitor holding
its demo agents, so visitors never read each other's conversations. Settings
says when the app is in the demo, and an expired demo session is renewed from
the sign-in screen (**Enter the demo again**). `src/state/session.ts`
(`enterDemo`, `renewDemo`, `isDemo`) is the flow.

Whether the entry shows comes from a small public JSON document, `GET
https://cloud.olano.ai/api/v1/mobile/config` (the cloud repository's
`apps/web/lib/mobile-config.ts`), read by `src/appConfig.ts` (pure, unit
tested) through `src/state/appConfig.ts`:

```json
{ "version": 1,
  "demo": { "enabled": true, "url": "https://demo.olano.run",
            "label": "Try the demo", "accountHint": "..." },
  "minSupportedVersion": null }
```

The screens draw from the copy kept on the phone (`olano.appConfig` in local
storage), or the bundled one before the first fetch; the app refetches at
launch at most every ten minutes, with a 4 s timeout, and a failed, slow or
malformed answer changes nothing. The demo address must be an https origin
under `olano.run` or `olano.ai`; anything the app does not accept turns the
entry off. `minSupportedVersion` makes an older app show an "update available"
note and blocks nothing. **The document can only hide or move the demo entry**:
it carries no feature switches, because App Review forbids features that
appear only after review (guideline 2.3.1). Keep it that way; `store/DEMO.md`
is the procedure around it. In a browser the request is a credential-less
`fetch()`, which the build's CSP allows for that one URL.

## Native plugins

### OlanoHttp

Part of this app (`OlanoHttpPlugin.swift`, `OlanoHttpPlugin.java`). Every
method returns a promise.

| Method | Result |
|---|---|
| `request({ url, method?, headers?, body?, responseType?, timeout? })` | `{ status, headers, body, url }`. Header names are lower case; `body` is text, or base64 when `responseType` is `"base64"` (images). |
| `stream({ id, url, method?, headers?, body? })` | `{ status, headers }` once the response starts; then `chunk` events `{ id, data }` (base64, so a character split across two reads survives) and one `end` event `{ id, error?, cancelled? }`. |
| `cancel({ id })` | Ends a stream; its `end` event still follows. |
| `clearCookies()` | Forgets the saved deployment's cookies (sign out, switch deployment). |
| `socketOpen({ id, url, headers? })` | Opens a WebSocket to an `/api/` path of the deployment (`url` is its https:// address, upgraded to wss://; the session cookie goes with the handshake). Then `socketOpen` `{ id }`, `socketMessage` `{ id, binary, data }` (text, or base64 when `binary`) and one `socketClose` `{ id, code, reason, error? }`. iOS: `URLSessionWebSocketTask`, the cookie added by hand (a cookie stored for https:// is not matched to wss://). Android: OkHttp 4.12. Binary messages reach the page at most once every 80 ms, the newest one when the interval has passed: they are the live browser view's frames, each a base64 string of a few hundred kilobytes the web view evaluates on the thread the person's taps need, and a page that never stops changing (a feed) sent them faster than it could unpack them, leaving no time for taps. Text messages go at once. |
| `socketSend({ id, text })` | Sends a text message. |
| `socketClose({ id })` | Closes the socket; its `socketClose` event still follows. |

Rules both platforms keep:

- **One origin.** Only URLs on the deployment saved by OlanoShell, with two
  exceptions: `GET <any https origin>/api/status`, which the connect screen uses
  to check an address before saving it, and `GET` of exactly
  `https://cloud.olano.ai/api/v1/mobile/config`, the app's remote config (see
  *Remote config and the demo*), sent and answered without cookies. Sockets
  only to the deployment's `/api/` paths.
- **https only**, plus `http` in debug builds (a box on the local network).
- **No redirects followed.** A redirect could lead the session cookie's
  requests off the deployment; it is returned to the screens as it is.
- **Cookies**: iOS keeps them in `HTTPCookieStorage.shared`, Android in the
  WebView's `CookieManager` (persistent, and never backed up). The session
  cookie lasts 14 days, like the dashboard's.

### OlanoShell

Which deployment the app talks to (`OlanoShellPlugin.swift`,
`OlanoShellPlugin.java`).

| Method | Result |
|---|---|
| `getServer()` | `{ url: string \| null }`: the saved deployment origin, or `null`. |
| `setServer({ url })` | Validates `url` (an absolute URL with a host; `https`, or `http` in debug builds only), reduces it to its origin (`scheme://host[:port]`), saves it and resolves `{ url }`. Rejects with a readable message (code `INVALID_URL`) otherwise. |
| `clearServer()` | Forgets the saved deployment. |
| `getAppInfo()` | `{ version, build, platform }`, all strings: `"1.0.0"`, `"1"`, `"ios"` or `"android"`. |
| `takeShared()` | What another app shared into Olano: `{ text, files: [{ name, mime, data, size? }] }`, `data` in base64, empty with `size` set for a file over 10 MB. Android: the activity's intent (`ACTION_SEND` / `ACTION_SEND_MULTIPLE`, declared in `AndroidManifest.xml`), read once and cleared, so a resume does not share it again; `MainActivity.onNewIntent` keeps a share that arrives while the app is open. iOS: what the share extension kept in the App Group (below), read and deleted. |
| `lockStatus()` | `{ available, kind }`: whether the phone has a passcode, and how it unlocks (`"face"`, `"fingerprint"`, `"optic"`, `"biometric"`, `"passcode"`, or `"none"`). |
| `unlock({ reason })` | Asks the phone to confirm it is its owner: Face ID / Touch ID with the passcode as the fallback (`LAContext`, policy `.deviceOwnerAuthentication`) on iOS, `BiometricPrompt` with `BIOMETRIC_WEAK \| DEVICE_CREDENTIAL` on Android. Resolves `{ ok: true }` or rejects with code `cancelled`, `unavailable` (no passcode) or `failed`. The app lock, `src/native/appLock.ts`; `NSFaceIDUsageDescription` is in `Info.plist`. |
| `setAgents({ agents: [{ name, display }], default })` | The roster the phone may name, the default agent first (`src/state/agentPrefs.ts`, Settings, Siri and widgets), published by `src/native/siri.ts` whenever the agent list loads or the default changes (once per change). iOS keeps both (`SharedStore`: App Group defaults, or the shared keychain) for the Siri actions, the controls and the widgets, which fall back to the default when none was picked for them, and refreshes the phrases Siri listens for; Android turns the roster into the launcher's "Talk to ..." dynamic shortcuts (the first four) and ignores `default`, the order carrying it. |
| `takeTalk()` | `{ agent: string \| null }`: the agent a "Talk to an Agent" request named (Siri, the Action button, a launcher shortcut), read once and cleared. iOS also raises the `talk` event (`{ agent }`) when the request arrives while the page is listening. |
| `takeDeepLink()` | iOS: `{ url: string \| null }`, one of the app's own links (`olano://open`, `olano://talk`) waiting to be opened, read once and cleared; the newest wins when two arrive before the page asks. The `deeplink` event (no data) says one is waiting while the page runs; the page then calls this. See *Widgets and controls* for why this is not Capacitor's `appUrlOpen`. |
| `syncWidgets({ admin? })` | iOS: hands the widget extension the session (the deployment's address through `SharedStore`, the cookies `HTTPCookieStorage.shared` holds for it into the keychain: under the App Group's access group when the build has one, in the app's own group otherwise, which the extension's `keychain-access-groups` lists) and redraws every widget. `src/native/widgets.ts` calls it, coalesced, on sign-in, on coming to the front, roster publish, the end of a turn and sign-out. Resolves `{ shared }`, false before sign-in. |

Where it is stored: iOS `UserDefaults.standard`, key `olano.serverURL`; Android
SharedPreferences file `olano_shell`, key `server_url`.

**Sharing into the app** (`src/native/shareIn.ts`). iOS: the share extension
(`ios/App/ShareExtension`, target `ShareExtension`, bundle id
`ai.olano.app.share`) appears in the share sheet for a web link, photos,
files and text. It copies what was shared into the App Group
`group.ai.olano.app`, folder `Shared/<uuid>/` with a `manifest.json`
(`{ text, files: [{ name, mime, file }] }`), then opens `olano://share`
(`CFBundleURLTypes` in the app's `Info.plist`) through the responder chain.
The app asks `takeShared()` on that URL, at launch and on every resume, so a
share still arrives when the extension could not open the app. The app also
declares `CFBundleDocumentTypes` for any file with
`LSSupportsOpeningDocumentsInPlace` off ("Open in" from Files): iOS copies the
file into `Documents/Inbox` and hands its `file://` URL to `appUrlOpen`; the app
reads it with `@capacitor/filesystem` and deletes the copy. Android asks
`takeShared()` at launch and on every resume. Either way the files go through the composer's own
`ingestFile`, App.tsx asks which agent gets them, and the new conversation
takes them into its composer unsent. In a desktop browser
`window.__olanoShareIn(text, files)` stands in for another app (the end-to-end
tests use it).

**Siri, Shortcuts and the Action button** (`ios/App/App/AppIntents.swift`,
`src/native/siri.ts`). Two App Intents, each with an agent parameter resolved
from the published roster (`AgentRoster`, `UserDefaults`, so no network):

- **Ask an Agent** (`AskAgentIntent`): runs in the app's process without
  opening it. It POSTs `/api/chat/stream` with the session cookie (as the
  notification buttons do), gathers the `text` frames until `done`, and hands
  the reply to Siri as dialog (a port of `src/chat/speakable.ts` strips the
  Markdown) and as a value the Shortcuts app can pass on. After
  `SiriTurn.patience` (20 s) the stream is cancelled, which leaves the run with
  no viewer so its reply pushes, and Siri says the agent is still working. An
  ask within 30 minutes of the last continues that thread (`SiriThreads`);
  otherwise the deployment mints a new one. Phrases: "Ask \(agent) in Olano",
  "Olano ask \(agent)", "Ask an agent in Olano", "Ask Olano" (and "on" /
  "with" spellings). A phrase can carry an entity parameter only, never the
  `String` message, so the question is always Siri's follow-up; words after
  the phrase in one breath make Siri fall back to its own Messages request.
- **Talk to an Agent** (`TalkToAgentIntent`, `openAppWhenRun`): keeps the
  agent in `TalkRequest` and posts a notification. `OlanoShellPlugin` forwards
  it as the `talk` event when the page listens, else the page reads
  `takeTalk()` at launch and on resume. App.tsx opens `/chats/<agent>/new`
  and the conversation starts Hands-Free Mode once it is on screen
  (`useIonViewDidEnter` **and** the address naming this page's own agent and
  thread, `routeParams.isPageFor`: an earlier conversation of the same agent
  left on the stack can keep its entered state after a push to another of
  its threads, and took the request while hidden).
  Phrases: "Talk to \(agent) in Olano", "Talk to an agent in Olano".

`OlanoShortcuts` publishes both (`AppShortcutsProvider`, iOS 16.4 for the
titled initializer; the intents themselves are iOS 16). Every phrase carries
`\(.applicationName)`, which Apple requires; `setAgents` calls
`updateAppShortcutParameters()` so a renamed or new agent is heard. Android has
no Siri: `setAgents` makes `ShortcutManagerCompat` dynamic shortcuts whose
intent carries `ai.olano.app.TALK` + `agent`, which `takeTalk()` reads off the
activity's intent (`MainActivity.onNewIntent` keeps it when the app is open).
In a desktop browser `window.__olanoTalk(agent)` stands in (the end-to-end
test).

**Widgets and controls** (`ios/App/OlanoWidgets`, target `OlanoWidgets`,
bundle id `ai.olano.app.widgets`, iOS 18; `ios/App/App/Shared` is compiled
into both the app and the extension). Seven widgets and two Control Center
controls, all SwiftUI over one `WidgetSnapshot` (`WidgetModel.swift`) that
`SnapshotLoader` fetches from the deployment with the shared session
(`Deployment` in `Shared/OlanoShared.swift`: agents, each one's latest thread
and last assistant row, HITL approvals, Pulse, scheduled tasks, `/api/auth/me`
for admin) and keeps in the App Group's defaults, so a redraw without network
or session draws the last state; avatars are downsized to 192 px once, when
fetched. Timelines ask for a redraw every 15 minutes (sooner while an approval
or a quick ask is pending, and right after the next scheduled run), and the
app's `syncWidgets` redraws them on every change it knows of.

- **Ask** (`AskWidget`, configured by `AgentConfigIntent`): the launcher.
  Small is one pill whose `widgetURL` is `olano://open?agent=&thread=new&compose=text`
  (a small widget has a single tap target); medium adds `Link`s for
  `compose=text|camera|photos` and `olano://talk`. It draws from the roster
  alone (`DefaultAgent.resolve` when none is configured), so it works before
  a session is handed over, and takes the picture and status from the
  snapshot when there is one.
- **Agent** (`AgentCardWidget`, configured by `AgentConfigIntent`): small,
  medium, circular, rectangular, inline. Tap opens `olano://open?agent=&thread=`,
  the thread being the last conversation the person had with the agent
  (`Deployment.conversations(of:)`, `GET …/threads?limit=1&kind=conversation`,
  the read the app's own Chats row makes; a runtime from before paging
  answers with every thread and the same rule is applied in Swift), and no
  thread when there is none, which the app opens as a new conversation. It
  used to be the agent's newest thread of any kind, so a tap landed on the
  last scheduled run instead of the person's conversation. The reply shown
  is that conversation's last. Talk is a `Link` to `olano://talk`;
  Start/Stop is `ToggleAgentIntent` (admins, `POST /api/agents/{name}/start|stop`).
- **Quick asks** (`QuickAsksWidget`, `QuickAsksConfigIntent` with three
  `String` parameters): each button is `QuickAskIntent`, which runs `SiriTurn`
  in the extension's process, keeps `QuickAskRecord` in the App Group so the
  widget shows "Working on it" and then the reply, and lets the turn go after
  20 s like Siri does (the agent's latest reply then fills the record).
- **Approvals** (`ApprovalsWidget`): `ResolveApprovalIntent` posts
  `/api/hitl/resolve`; a `risk: high` item gets no buttons. Pulse approvals are
  counted, not answered, from the widget.
- **Team**, **Pulse**, **Next run** (`TeamWidget.swift`, `CortexWidgets.swift`):
  `StaticConfiguration`s over the same snapshot; Next run uses
  `Text(date, style: .relative)` so the countdown moves without redraws.
- **Controls** (`Controls.swift`, `AppIntentControlConfiguration` over
  `AgentControlConfigIntent`): `OpenTalkIntent` / `OpenAskIntent` return
  `OpenURLIntent(olano://...)`, the one route that works whichever process
  runs them; they also bind to the Action button on iOS 18.

The deep links are read by `src/native/siri.ts` through the pure
`src/siri/deepLinks.ts`: `olano://talk?agent=` is a talk request,
`olano://open` with `agent` + `thread` (+ `compose`) or `tab` becomes a
`pendingRoute`, exactly as a tapped notification does, and `olano://open`
naming only an agent is an `agentRequest` App.tsx resolves the way a tap in
Chats does (`src/chat/openAgent.ts`: the last conversation the person had,
else a new one); `ConversationPage` reads `?compose=` off its location once
per arrival (`composeModeOf`) and focuses the box or opens the camera or the
picker. `window.__olanoDeepLink(url)` stands in on the web.

On iOS the links reach the page through `OlanoShell.takeDeepLink()` (at
launch, on every resume, and when the `deeplink` event says one is waiting),
fed by `OlanoShellPlugin`'s observer of Capacitor's `capacitorOpenURL`
notification, which is posted for a link that opens the running app and once
for the link the app was launched with (after the bridge's view has
appeared, so plugins are in place). Two things made Capacitor's own route
(`App.addListener("appUrlOpen")` + `App.getLaunchUrl()`) open the app on
the wrong screen. A launch link's event is retained until the first
`appUrlOpen` listener registers and is handed to that listener only; the
share handler (`src/native/shareIn.ts`, for `olano://share` and `file://`)
registers first and ignores it, so the deep-link listener never saw a cold
start's link. And `getLaunchUrl()` answers the last link for the life of the
process, so a page loaded again on return (`OlanoBridgeViewController.reloadIfBlank`)
opened the conversation of a widget tapped an hour earlier. The slot holds
one link, read once, and the event carries no link itself: a page being
loaded again while one arrives still finds it at launch, and a running page
never sees it twice. The two openers in App.tsx also wait until the router
has left `/`: the tabs mount there and redirect to Chats in an effect of
their own, and a push made in the same pass (a launch link; a talk request
answered from the agent list kept on the phone) lost to that redirect's
replace, which is how a widget tap opened the app on Chats.
`tests/e2e/test_mobile_app.py::test_a_link_the_app_was_opened_with` covers
the race.

**The hand-off works without an App Group.** `SharedStore`
(`Shared/OlanoShared.swift`) writes the address, the admin flag, the roster
and the default agent to the App Group's defaults when the build has one and
to the keychain otherwise, one generic-password item per key, with no access
group named, so they land in the app's default group (`<prefix>ai.olano.app`,
the app's own identifier); the cookie item does the same. The extension's
entitlements, Debug and Release, list that group first in
`keychain-access-groups`, so it reads them, and Keychain Sharing is a
capability every provisioning profile already carries (`<prefix>.*`), which a
personal team is granted where App Groups are refused. This is why a build
installed from Xcode on a free Apple ID draws its widgets; before it, the
extension read its own empty defaults and showed "open the app" for good.
What only one process needs (the snapshot cache, a quick ask's record, the
talk request) stays in `AppGroup.defaults`, which without a group is that
process's own. No Android widgets yet.

### OlanoSpeech

Speech on the phone itself (`OlanoSpeechPlugin.swift`, `OlanoSpeechPlugin.java`,
reached through `src/native/speech.ts`).

| Method | Result |
|---|---|
| `recognitionStatus({ language? })` | `{ available, onDevice, permission, language }`: whether the phone can transcribe a recording, on the device itself or through Apple, the speech recognition permission (`granted`, `denied`, `restricted`, `prompt`) and the language it listens for. |
| `transcribe({ data, durationMs?, language? })` | `data` is the recording as base64 WAV. Asks for the permission the first time. `{ text, onDevice, language }`, `text` empty when nothing was said; rejects with code `denied`, `unavailable`, `timeout` or `failed`. |
| `speak({ text, language? })` | Reads `text` aloud and resolves `{ finished }` when it ends (`false` when stopped). |
| `stopSpeaking()` | Stops reading. |

- **iOS** transcribes the WAV the screens recorded with `SFSpeechURLRecognitionRequest`,
  on the device when the recognizer supports it (`requiresOnDeviceRecognition`),
  otherwise through Apple. The recognizer is the first of the phone's preferred
  languages it supports. Reading aloud is `AVSpeechSynthesizer`, with the best
  installed voice for the language `NLLanguageRecognizer` detects in the text
  (the person's own regional variant first), in the `.playback` category so the
  silent switch does not mute it.
- **Android** reads aloud with `TextToSpeech` (the manifest's `<queries>` entry
  for `TTS_SERVICE` lets it find the engine on Android 11 and later). Its
  `SpeechRecognizer` listens to the microphone itself and cannot be handed a
  recording, so `recognitionStatus` reports unavailable and voice notes go to
  the deployment.
- **A browser** reads aloud with `speechSynthesis` where it has it, and cannot
  transcribe. The end-to-end tests stand in for a phone by setting
  `window.__olanoFakeSpeech` (the same four methods); nothing on a phone reads it.

### Other plugins

| Plugin | Package | Used for |
|---|---|---|
| `PushNotifications` | `@capacitor/push-notifications` 8.1.3 | Permission, registration token, tapped notifications. |
| `App` | `@capacitor/app` 8.1.2 | Foreground and background (closing and rejoining streams). |
| `Browser` | `@capacitor/browser` 8.0.5 | Links in replies, in an in-app browser. |
| `Camera` | `@capacitor/camera` 8.2.5 | Take a photo, or choose up to ten from the library (`takePhoto`, `chooseFromGallery`). |
| `Haptics` | `@capacitor/haptics` 8.0.2 | A tap when a message is sent, a reply finishes, an approval is answered. |
| `Keyboard` | `@capacitor/keyboard` 8.0.6 | The web view shrinks above the keyboard so the composer rides on it; the iOS keyboard follows the app's theme. |
| `Share` | `@capacitor/share` 8.0.2 | Share a message, or hand a reply's file to another app. |
| `Filesystem` | `@capacitor/filesystem` 8.1.4 | Write a reply's file to the app's cache, which the share sheet needs (`src/native/files.ts`). |
| `SplashScreen` | `@capacitor/splash-screen` 8.0.2 | Hide the launch splash once the app has rendered. |
| `SystemBars` | built into `@capacitor/core` 8 | Status bar icons matching the light or dark scheme. |

## Layout

```
mobile/
  package.json            versions, scripts; "version" is the app version
  capacitor.config.ts     bundled config (no server.url: the screens are local)
  vite.config.ts          builds src/ into www/; `npm run dev` proxies /api
  src/
    index.html, main.tsx, App.tsx    the frame: start screens, then the tabs
    theme/                Olano's accent on Ionic's variables, chat bubbles
    net/                  transport (native or fetch), API client, SSE parser, types
    native/               OlanoShell, push, haptics, photos (pick, prepare), voice recorder, speech on the phone, theme, foreground and background, Siri and the launcher (siri.ts), the widgets' session (widgets.ts)
    siri/                 the roster the phone may name, the app's own links (pure)
    state/                session (connect, sign in), cached lists, live events
    chat/                 a conversation: transcript folding, live turn, steps, photos, voice notes
    screens/              start/, chats/, cortex/, inbox/, settings/
    ui/                   avatars, Markdown, photo grid and viewer, voice notes and player, media cache, empty states, times
    shared/dashboard.ts   the dashboard modules the app imports, not copies
    address.ts, probe.ts, recents.ts   the connect screen's address handling
    appConfig.ts          the remote config: the demo entry's switch (pure)
    *.test.ts             node --test unit tests
  store/                  App Store and Google Play submission kit: listing, review notes, privacy answers, the demo
  scripts/
    check-versions.mjs    package.json, Android and iOS versions agree
    check-store-readiness.mjs  privacy manifests, purpose strings, encryption flag, Android permissions
    make_icons.py         draws every icon and splash image (and the dashboard's)
  assets/                 icon.svg, the app icon's master artwork (written by make_icons.py)
  android/                Android Studio project (Capacitor template + the shell)
    app/src/main/java/ai/olano/app/
      MainActivity.java       plugin registration, notification channels
      OlanoHttpPlugin.java    HTTP and streams to the deployment
      OlanoShellPlugin.java   the saved deployment
      ServerStore.java        SharedPreferences
      ServerUrl.java          address validation (unit tested)
      NotificationChannels.java
      OlanoMessagingService.java      draws the pushes that carry buttons
      NotificationActionReceiver.java answers Approve, Decline and Reply
    app/src/debug/AndroidManifest.xml   cleartext http for debug builds only
  ios/                    Xcode project (Capacitor SPM template + the shell)
    App/App/
      OlanoBridgeViewController.swift   registers the app's plugins
      OlanoHttpPlugin.swift             HTTP and streams to the deployment
      OlanoShellPlugin.swift            the saved deployment, ServerURL
      NotificationActions.swift         Approve, Decline and Reply from a notification
      AppIntents.swift                  Siri, Shortcuts and the Action button: Ask an Agent, Talk to an Agent
      Shared/                           compiled into the app and the widgets: the shared session, roster, deployment client, Siri turn, agent entity
      AppDelegate.swift                 forwards APNs registration, notification buttons
      App.entitlements                  aps-environment, the App Group
    App/ShareExtension/
      ShareViewController.swift         "Olano" in the share sheet
      Info.plist, ShareExtension.entitlements
    App/OlanoWidgets/                   the Home Screen and Lock Screen widgets, the Control Center controls (iOS 18)
      OlanoWidgetsBundle.swift          the bundle: Agent, Quick asks, Approvals, Team, Pulse, Next run, two controls
      WidgetModel.swift                 the snapshot the widgets draw, its loader and cache
      WidgetIntents.swift               configuration intents and the buttons' intents
      Theme.swift                       colours, avatars, status, the card background
      Info.plist, OlanoWidgets.entitlements
```

`src/shared/dashboard.ts` imports a few pure modules from the dashboard's source
tree (`olano/dashboard_2.0/src`): the XSS-safe Markdown renderer, the turn
helpers and the stream frame types. They are imported rather than copied so the
app and the dashboard render a reply the same way and share hardening fixes.

Generated and not committed (see the `.gitignore` files): `node_modules/`,
`www/`, the web assets and config `npx cap sync` copies into each native project,
`android/capacitor-cordova-android-plugins/`, `ios/capacitor-cordova-ios-plugins/`,
Gradle and Xcode build output, and every push or signing credential.

## Developing

### Prerequisites

- Node.js 22.12 or newer.
- Android: Android Studio (or JDK 21 and the Android SDK with platform 36).
- iOS: a Mac with Xcode 26 (what Capacitor 8.5 is tested against). The project
  uses Swift Package Manager; no CocoaPods.
- Only to redraw icons: Python 3 with Pillow (`pip install pillow`).

### Build and sync

```bash
cd mobile
npm install
npm test            # unit tests + version check
npm run build       # typecheck, then the app into www/
npx cap sync        # copy www/ and the config into android/ and ios/
```

`npm run sync` does the last two in one go. Run a sync after changing anything
in `src/`, `capacitor.config.ts` or the plugin list.

The `Makefile` in this folder chains these for the everyday cases, installing
the npm tree first when it is missing or stale (`make help` lists everything):

```bash
make ios              # build, sync into the Xcode project and open it
make android          # build, sync into the Android Studio project and open it
make ios-build        # build and compile for the iOS Simulator, no IDE (what CI does)
make android-build    # build and compile the debug APK, no IDE (what CI does)
make ios-run          # build and run on a simulator or a plugged-in iPhone
make android-run      # build and run on an emulator or a plugged-in phone
make apk              # android-build, then install the APK over adb
```

Pull first: the native projects are committed, so a stale checkout opens a
stale project.

### In a desktop browser

`npm run dev` serves the app at http://localhost:5199 and proxies `/api` to a
runtime, `http://127.0.0.1:8765` unless `OLANO_DEV_RUNTIME` says otherwise.
Enter `http://localhost:5199` on the connect screen. Use the browser's phone
emulation with an iPhone or Android user agent: Ionic picks its iOS or Material
style from it.

`npm run preview` serves the built app (`www/`, what ships in the native
projects) the same way. `tests/e2e/test_mobile_app.py` builds the app and runs
it under `vite preview` against a real runtime (agents with a scripted model),
in an iPhone-sized Chromium that also reports an iPhone's notch and
home-indicator insets (`--safe-area-inset-*`, which Ionic reads before
`env()`): a desktop browser reports none, and the built app and the dev server
differ in the order Ionic's styles land in. Both differences once hid layout
bugs that only showed on a phone. It covers connect, chat with a streamed
reply, threads whose ids carry `:` and `@` and their source badges, the
message buttons (copy, edit, try again), Cortex, Inbox, Settings, sign in and
out, photos, voice notes (as an administrator and as a member, on Chromium's
fake microphone, checking the deployment receives a readable 16 kHz WAV), the
theme setting, dark mode, and the background-then-push path.
`OLANO_MOBILE_SHOTS=<dir>` keeps a screenshot of every screen.

### Run on Android

```bash
npx cap run android      # pick a device or emulator
npx cap open android     # or open the project in Android Studio
```

Without `android/app/google-services.json` the app builds and runs, but the push
plugin is left out (`MainActivity.load()`), so
`Capacitor.isPluginAvailable("PushNotifications")` is false. That is on purpose:
the plugin's `register()` throws on a background thread when Firebase is not
configured, which would end the app.

### Run on iOS

```bash
npx cap open ios
```

In Xcode, pick your team under *Signing & Capabilities* for the `App` target,
then run on a simulator or a device. Push notifications need a real device and a
team whose App ID `ai.olano.app` has the Push Notifications capability.

### Trying it on an iPhone before the App Store

Three routes, from least to most setup:

| Route | Needs | Push notifications | Lasts |
|---|---|---|---|
| Xcode on your Mac, phone on a cable | A Mac with Xcode 26 and any Apple ID ("Personal Team") | No: a free team cannot use the Push Notifications capability, so remove it under *Signing & Capabilities* first | 7 days, then run it again from Xcode |
| **TestFlight, internal testers** | Apple Developer Program membership, the app record in App Store Connect, the four secrets in `.github/workflows/mobile-testflight.yml` | Yes (production APNs, through the Olano Cloud relay) | 90 days per build |
| TestFlight, external testers | The same, plus Apple's beta review of the first build (usually about a day) | Yes | 90 days per build |

TestFlight is the one to use. Nobody needs a Mac: run **Mobile TestFlight**
from the repository's Actions tab, wait for Apple to process the build (usually
10 to 30 minutes), and testers install it with Apple's TestFlight app. Internal
testers are people on the App Store Connect team (up to 100) and need no review;
external testers (up to 10,000, by email or a public link) wait for the beta
review. The workflow's header lists the secrets and the one manual step (creating
the app record).

#### From Xcode with a free Apple ID

1. On the Mac: Node.js 22.12 or newer and Xcode (see *Prerequisites*). Then, in
   `mobile/`: `npm install`, `npm run sync:ios`, `npx cap open ios`.
2. In Xcode, select the `App` target, *Signing & Capabilities*:
   - *Team*: your Apple ID's "(Personal Team)". Add the Apple ID under
     *Xcode, Settings, Accounts* if it is not listed.
   - Remove the *Push Notifications* capability (the x on its row); a personal
     team cannot sign it and the build stops with a provisioning error. The
     Debug configuration already leaves it and the App Group out
     (`App.debug.entitlements`) and keeps *Keychain Sharing*, which a personal
     team may use and which the widgets read the session through.
   - Change *Bundle Identifier* to one of your own, for example
     `ai.olano.app.<yourname>`. A team claims the identifiers it signs, and
     `ai.olano.app` has to stay free for the company's developer account.

   These are local edits for trying the app; do not commit them.
3. Connect the iPhone with a cable, unlock it and tap *Trust*. The first time,
   Xcode may download support for the phone's iOS version, and iOS asks for
   *Developer Mode* (*Settings, Privacy & Security, Developer Mode*, then a
   restart).
4. Choose the phone as the run destination and press Run. If the phone refuses
   to open the app ("Untrusted Developer"), trust your Apple ID under
   *Settings, General, VPN & Device Management*, then open it again.
5. Enter the deployment's address on the connect screen.

The app needs nothing new on the deployment: chat, Cortex and the inbox work
with any recent Olano. Push notifications need a deployment that has the push
backend and a build signed for push (TestFlight or the stores).

Every push that touches `mobile/` also compiles the iOS app for the simulator on
a macOS runner (`.github/workflows/mobile.yml`), so the Swift is checked even
when nobody on the team has a Mac, and builds an Android debug APK you can
download from the run's *Artifacts* and install directly on an Android phone.

### Point a debug build at a local box

Debug builds accept plain `http://` addresses so you can use a box on your
network; release builds refuse them (Android `src/debug/AndroidManifest.xml`
allows cleartext only in debug, iOS allows only local-network hosts through
`NSAllowsLocalNetworking`, and both native plugins refuse `http` in release).

1. Start a runtime and the dashboard server on your computer, for example
   `olano dashboard` (dashboard on port 3200, `/api` proxied to the runtime).
2. In the app's connect screen enter:
   - Android emulator: `http://10.0.2.2:3200`
   - iOS simulator: `http://localhost:3200`
   - a phone on the same network: `http://<your computer's LAN address>:3200`

To debug the web content: `chrome://inspect` for Android, Safari's *Develop* menu
for iOS. Both are enabled in debug builds only.

### Icons and splash

`python3 scripts/make_icons.py` (or `npm run icons`) redraws every launcher
icon, the Android adaptive and themed icon, and the launch splash images on both
platforms: the mesh mark from the olano.ai wordmark (`src/assets/logo.svg`) in
brand orange on white, centred with the same clear space on every side. It
first checks its node and edge lists against `logo.svg`, then writes the master
artwork to `assets/icon.svg` and every raster from the same geometry. It also
writes the web dashboard's favicons and install icons
(`olano/dashboard_2.0/public/`); the favicons use a simplified style (heavy outer
octagon, light inner mesh) because the full mesh blurs at 16 px. The status bar
notification icon is the vector
`android/app/src/main/res/drawable/ic_stat_olano.xml`.

## Edge to edge and safe areas

The app is laid out with `viewport-fit=cover`, and Ionic pads its headers, tab
bar and footers with `env(safe-area-inset-*)`:

- **Android.** The app targets SDK 36, where Android draws every app edge to
  edge with no opt-out. Capacitor 8 handles insets in its built-in `SystemBars`
  plugin, configured with `insetsHandling: "native"`: on Android System WebView
  140 and newer `env(safe-area-inset-*)` reports the real insets; on older
  WebViews the web view is padded clear of the bars and the insets read 0, which
  still lays out correctly.
- **iOS.** `ios.contentInset: "never"` keeps UIKit from adding its own scroll
  insets on top of Ionic's padding. The web view itself never bounces
  (`OlanoBridgeViewController`); pull to refresh is the page's own.

The keyboard shrinks the web view (`Keyboard.resize: "native"`), so a
conversation's composer stays on top of it.

## Push notifications

End to end:

1. **The app** (`src/native/push.ts`, from *Settings, Push notifications*) asks
   for permission (`PushNotifications.requestPermissions()`), calls
   `PushNotifications.register()` and receives the token in the `registration`
   event. On Android that is an FCM registration token; on iOS it is the APNs
   device token (hex), because the iOS plugin registers with APNs directly. Every
   launch re-registers silently once permission was given.
2. **The app registers the token with the deployment** (`/api/push/*`,
   `olano/runtime/routes/push.py`): token, platform (`ios` or `android`), a
   device name, the app version and the kinds of notification wanted. The box
   keeps one list per signed-in person.
3. **When something happens** (a reply finished while nobody watched, an
   approval is waiting, a Cortex finding, a deployment alert), the box sends the
   notification to the Olano Cloud relay, `POST /api/v1/push`, authenticated with
   its deployment token (`olano/push/`).
4. **The relay** signs and sends it through APNs for iOS and FCM for Android. The
   Apple and Google credentials live only in the cloud, never on a box or in the
   app. It stores nothing. Its contract is `docs/push-notifications.md` in the
   private `olano-ai/cloud` repository.
5. **On the phone**, while the app is in the background the system shows the
   notification; on Android it is filed under the channel the relay names
   (`replies`, `approvals`, `cortex`, `alerts`; created by
   `NotificationChannels.java` at every launch). While the app is open nothing is
   shown (`presentationOptions: []`), because the app shows live updates
   itself. A tap opens the app on the conversation or the Inbox
   (`pushNotificationActionPerformed`, `routeFor` in `src/native/push.ts`).
6. **Buttons on the notification, answered without opening the app.** The box
   sends an approval with `category: "approval"` (never for a high-risk
   action) and a finished reply with `category: "reply"` (not a failed one),
   plus what answering needs in `data` (`thread_id` and `count` for an
   approval in a conversation, `item_id` for an inbox one, `agent` and
   `thread_id` for a reply).
   - **iOS.** The relay passes the category as `aps.category`.
     `NotificationActions.swift` registers the buttons at launch (`approve`,
     `decline`, and `reply`, a text field), all requiring an unlocked phone
     and none opening the app, and sits in front of Capacitor's notification
     router as the `UNUserNotificationCenter` delegate (installed in
     `AppDelegate`, then again in `OlanoBridgeViewController.capacitorDidLoad`
     once the bridge has put its router in place). A button press is answered
     natively with the session cookie in `HTTPCookieStorage.shared`:
     `POST /api/chat/approval` or `/api/hitl/resolve`, or for a reply
     `POST /api/chat/stream` let go once the response starts (the turn runs on
     detached, and its reply comes as the next push). A failure posts a local
     notification saying so. Taps go on to Capacitor and `routeFor`.
   - **Android.** FCM draws a notification message itself while the app is in
     the background, without buttons, so the relay sends a notification with a
     category as data only (`olano_category`, `olano_title`, `olano_body`,
     `olano_channel`, `olano_tag`). `OlanoMessagingService` (in place of
     Capacitor's service, `tools:node="remove"` in the manifest) draws it with
     its buttons (`RemoteInput` for Reply), and `NotificationActionReceiver`
     answers a press the same way as iOS, with the cookie from the web view's
     `CookieManager`. A tap opens `MainActivity` with the data and
     `google.message_id`, which Capacitor hands to the screens as a tap.
   - `src/native/pushActions.ts` still answers an `approve` / `decline`
     `actionId` that reaches the screens (an older native build).

The deployment sends a reply or approval notification only when nobody is
watching the turn. The app closes every stream it holds when it goes to the
background (`src/native/lifecycle.ts`), so a phone in a pocket never counts as
watching; coming back rejoins each turn from the last frame it saw.

What each platform needs, none of it committed:

| | Where | What |
|---|---|---|
| Android build | `android/app/google-services.json` | From the Firebase project's Android app registered as `ai.olano.app`. The Gradle build applies the Google Services plugin only when this file exists. |
| iOS build | `ios/App/App/App.entitlements` (committed) | `aps-environment` is `development`; an App Store or TestFlight build is signed with a distribution profile, which turns it into `production`. The same file, `ShareExtension.entitlements` and `OlanoWidgets.entitlements` hold the App Group `group.ai.olano.app` the share extension hands shares over in, and the app's and the widgets' files list the keychain access group `$(AppIdentifierPrefix)ai.olano.app` the widgets read the session from. No `GoogleService-Info.plist` is needed. **Release only**: the Debug configuration (what Xcode runs on a plugged-in phone) signs with `App.debug.entitlements`, `ShareExtension.debug.entitlements` and `OlanoWidgets.debug.entitlements`, which carry no push and no App Group, because a personal (free) Apple ID team is refused the Push Notifications and App Groups capabilities and could not run the app at all; they keep the keychain access group, which every team is granted. On such a build notifications stay off (Settings says so) and the share sheet's Olano does nothing; both work in a TestFlight or App Store build, which is Release and needs the paid Developer Program anyway. The widgets work on either. To test push on a device from Xcode on a paid team, run the Release configuration (Edit Scheme, Run, Build Configuration). |
| Cloud relay | GitHub Actions secrets of `olano-ai/cloud` | The APNs `.p8` key with its Key ID and Team ID, and the Firebase service-account JSON (see that repository's push document). |

`google-services.json`, `GoogleService-Info.plist`, `*.p8` and signing files are
listed in `mobile/.gitignore`.

## Release checklist

### Every release, both stores

1. Bump the version everywhere and keep the three in step
   (`npm run check:versions` fails otherwise; `npm test` runs it too):
   - `package.json` `version` (this is also the `OlanoApp/<version>` user agent);
   - `android/app/build.gradle`: `versionName` = the same version, `versionCode`
     + 1 (Google Play refuses a code it has seen);
   - Xcode, target `App`, *General*: *Version* (`MARKETING_VERSION`) = the same
     version, *Build* (`CURRENT_PROJECT_VERSION`) + 1 for every upload.
2. `npm install && npm test && npm run sync`.
3. The demo deployment is up and **Try the demo** works from a fresh install
   (`store/DEMO.md`); the review notes (`store/REVIEW_NOTES.md`) say so.
4. `store/SUBMISSION.md` is the step-by-step checklist for both stores, and
   the rest of `store/` holds what gets pasted into them: the listing copy,
   the privacy and Data safety answers, screenshots, age rating, export
   compliance.

### App Store Connect

- Bundle IDs `ai.olano.app` (display name *Olano*) and `ai.olano.app.share`
  (the share extension). The app's App ID needs the Push Notifications and App
  Groups capabilities, the extension's App Groups, both with the group
  `group.ai.olano.app`. Automatic signing with the workflow's API key registers
  them; if a first archive fails on the group, create it under Certificates,
  Identifiers & Profiles, Identifiers, App Groups, and run it again.
- Signing: automatic signing with the team in Xcode, or a distribution
  certificate and an App Store provisioning profile that includes push.
- Archive in Xcode (*Product*, *Archive*, scheme `App`, *Any iOS Device*) and
  upload through the Organizer. `ITSAppUsesNonExemptEncryption` is `false` in
  `Info.plist`: the app uses only the encryption built into iOS (HTTPS).
- App Privacy, what the app itself sends (see also the questions below):
  - the push token, with a device name and the app version, goes to the
    person's own deployment, which stores it to send them notifications;
  - photos the person chooses to attach go to their own deployment, re-encoded
    without location or other metadata;
  - voice notes the person records are turned into text on an iPhone itself
    (on the device, or by Apple's speech recognition for a language the device
    cannot do alone) and only the text goes to their own deployment;
    otherwise the recording goes to their own deployment, which turns it into
    text with its own speech-to-text service;
  - replies the person asks to hear go back to their own deployment to be
    spoken when it has text to speech set up; otherwise the phone reads them;
  - nothing goes to Olano except the push token inside each notification the
    relay delivers; the relay does not store it;
  - no analytics, advertising or tracking SDKs; no tracking across apps.
  Open question for whoever owns the privacy policy: on a managed deployment
  (`*.olano.run`) Olano operates the infrastructure that stores what people type,
  attach and record in the dashboard; decide whether that is declared as User
  Content linked to the user.
- Privacy manifests: `App/PrivacyInfo.xcprivacy`, `ShareExtension/` and
  `OlanoWidgets/` each bundle their own (required-reason APIs: UserDefaults
  `CA92.1` and `1C8F.1`, file timestamps `C617.1` in the app). Keep their
  collected data types in step with `store/PRIVACY.md`;
  `scripts/check-store-readiness.mjs` (part of `npm test`) fails when a manifest,
  a purpose string or the encryption flag goes missing.
- Purpose strings in `Info.plist`: the camera (taking a photo to attach), the
  microphone (recording a voice note), speech recognition (turning a voice
  note into text), Face ID (the app lock), the photo library (choosing
  photos) and photo library additions. The app never
  saves to the library; the last is declared because the camera plugin links
  that API, and an upload without it is rejected.
- Sign in with Apple is not required: the app signs in only with the
  deployment's own email and password, no third-party login (guideline 4.8).
- Account deletion (guideline 5.1.1(v)) does not apply: the app cannot create
  accounts. Accounts exist on deployments and are created by their
  administrators (`store/COMPLIANCE.md` has the wording for the review notes;
  the demo's visitor accounts are deleted with their conversations after a
  day and are not the person's).
- Review notes for guideline 4.2 (minimum functionality): the app is its own
  interface, not a wrapped website. A tab bar (Chats, Cortex, Inbox, Settings)
  with platform navigation, swipe back and pull to refresh; streamed
  conversations with the agent's steps and in-line approvals; photos from the
  camera or the library, with a full-screen viewer; voice notes held or
  tapped to record, transcribed on the phone, with waveform playback and
  transcripts; replies read aloud; light and dark
  themes; Cortex items with
  promote and discard; an inbox of approvals and alerts; push notifications for
  replies, approvals, Cortex findings and deployment alerts; haptics; and a
  connect screen that checks an address and keeps several deployments one tap
  apart.

### Google Play

- Application ID `ai.olano.app`, app name *Olano*.
- Signing: create an upload key once and keep it outside the repository (the
  `.gitignore` refuses `*.jks` and `*.keystore`); enrol in Play App Signing.
  `app/build.gradle` signs a release with it when `android/keystore.properties`
  (git-ignored; copy `keystore.properties.example`) or the
  `OLANO_UPLOAD_STORE_FILE`, `OLANO_UPLOAD_STORE_PASSWORD`,
  `OLANO_UPLOAD_KEY_ALIAS` and `OLANO_UPLOAD_KEY_PASSWORD` variables name it:
  `cd android && ./gradlew bundleRelease -PolanoVersionCode=<n>` then writes
  `app/build/outputs/bundle/release/app-release.aab`. Without them Android
  Studio's *Generate Signed App Bundle* still works.
- The manifest strips permissions a library could merge in that the app must
  not hold (`tools:node="remove"`): the advertising ID and `READ_MEDIA_*` /
  external storage. `targetSdkVersion` is 36, Google Play's floor for new apps
  and updates since August 31, 2026.
- `google-services.json` must be in `android/app/` for the release build, or the
  app ships without push.
- Data safety: the same flows as the App Store section. The app includes the
  Firebase Cloud Messaging SDK, which Google's Firebase data-disclosure page
  lists as collecting a Firebase installation ID to deliver messages.
- Permissions to explain in the listing if asked: notifications, asked for when
  someone turns them on in Settings; the camera, asked for the first time
  someone takes a photo to attach; the microphone, asked for the first time
  someone records a voice note. Choosing photos uses the system photo picker,
  which needs no permission. The app asks for no other permission.
- Backups: only the saved deployment address is backed up or moved to a new
  phone (`res/xml/backup_rules.xml`, `res/xml/data_extraction_rules.xml`); the
  session cookie (in the WebView cookie store) and the app's storage stay on
  the device.

## Known limits

- No way into the full dashboard from the app yet: an in-app browser does not
  share the app's session, so it would need its own sign-in. Settings had an
  "Open the full dashboard" row and the file viewer an "Open in the dashboard"
  button; both are gone until the app can hand its session over.
- Photos, documents and voice notes; video is not in the app yet (the dashboard
  has it). A member's photos, recordings and documents are not saved on the
  deployment (the runtime saves attachments for administrators), so a reopened
  conversation shows a member's voice note as its transcript and leaves out
  their photos.
- Sharing into Olano on iOS takes files only: text and links need a share
  extension, which the app does not have yet. Android takes all three.
- Alerts whose fix is a dashboard page the app does not have (the vault, disk,
  budgets) say so rather than open anything (`src/inbox/alertActions.ts` maps
  each alert to what the app can do about it).
- A recording in progress is thrown away when the app goes to the background:
  the phone takes the microphone back there anyway.
- Voice notes are transcribed on the phone on iOS only, in one language (the
  first of the phone's languages the recognizer supports); Android sends the
  recording to the deployment. Reading aloud does not cover group chats'
  replies by itself (the speaker under each one does).
- A reply's files open in the app (`src/ui/FileViewer.tsx`): Markdown
  rendered, text and code highlighted, PDFs drawn on iOS (Android's web view
  cannot, so there they go to another app, like Word and Excel files). Files
  over 20 MB are not downloaded to the phone; the dashboard in a browser
  opens them.
- Chat history loads whole; very long conversations load more slowly than the
  dashboard's.
