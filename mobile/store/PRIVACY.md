# Privacy answers: App Store "App Privacy" and Google Play "Data safety"

Derived from the code, not from intent. Keep it in step with the privacy
manifests (`ios/App/*/PrivacyInfo.xcprivacy`) and the Android manifest;
`scripts/check-store-readiness.mjs` fails `npm test` when a manifest, a
permission or a purpose string changes without this file being looked at.

## What the app sends, and to whom

| Data | Where it goes | Code |
|---|---|---|
| Sign-in email and password | The person's own deployment (`POST /api/auth/login`); the session cookie is kept on the phone | `src/state/session.ts` |
| Messages, documents and files a person sends | Their own deployment | `src/net/api.ts`, `src/chat/` |
| Photos they attach | Their own deployment, re-encoded as JPEG at most 2048 px, which drops location and other metadata | `src/native/photos.ts` |
| Voice notes | On iOS turned into text on the phone (on the device, or by Apple's speech recognition) and only the text is sent; otherwise the recording goes to their own deployment, which transcribes it | `src/chat/conversation.ts`, `OlanoSpeechPlugin.swift` |
| Replies they ask to hear | Their own deployment, for text to speech, when it has a voice; otherwise the phone reads | `src/ui/readAloud.ts` |
| Push token, with a generic device name ("iPhone", "Android phone") and the app version | Their own deployment, which stores it to notify them; the deployment hands it to the Olano Cloud relay inside each notification, and the relay stores nothing | `src/native/push.ts`, `olano/push/` |
| The app's remote config request | Olano Cloud, `GET /api/v1/mobile/config`, no cookies, no identifiers beyond what any HTTPS request carries | `src/state/appConfig.ts` |
| Firebase installation ID (Android only, with push) | Google, by the Firebase Cloud Messaging SDK, to deliver notifications | `firebase-messaging` |

Nothing else: no analytics, advertising, attribution or crash-reporting SDK
on either platform, no tracking across apps or websites (`NSPrivacyTracking`
false, no tracking domains, the Android advertising ID permission stripped).
Location is never read.

**Whose servers.** A deployment is the customer's. On a self-hosted deployment
Olano never sees the data. On a managed one (`*.olano.run`) Olano runs the
servers that store it, as the customer's service provider. The answers below
take the conservative reading and declare the data as collected, because the
developer of the app (Olano) operates infrastructure that can hold it. This
is the decision the README's former open question asked for; if the privacy
policy owner decides otherwise, change this file, both privacy manifests and
the App Store answers together.

## App Store Connect: App Privacy

"Do you or your third-party partners collect data from this app?" **Yes.**

For every type below: **Linked to the user: Yes. Used for tracking: No.
Purpose: App Functionality** (and nothing else).

| Category | Data type | Why |
|---|---|---|
| Contact Info | Email Address | Signing in to the person's deployment |
| Identifiers | User ID | The deployment account the app signs in to |
| Identifiers | Device ID | The push token the deployment stores |
| User Content | Photos or Videos | Photos attached to messages |
| User Content | Audio Data | Voice notes sent as recordings (Android, or when the phone cannot transcribe) |
| User Content | Other User Content | Messages, documents and files sent to agents |

Not collected: Health and Fitness, Financial Info, Location, Sensitive Info,
Contacts, Browsing History, Search History, Purchases, Usage Data,
Diagnostics, Other Data. (Credits and usage shown in Settings are read from
the deployment, not collected by the app.)

## Google Play: Data safety

- Does your app collect or share any of the required user data types? **Yes.**
- Is all of the user data collected by your app encrypted in transit? **Yes**
  (release builds refuse anything but HTTPS: `usesCleartextTraffic="false"`
  and OlanoHttp/OlanoShell refuse `http://`).
- Do you provide a way for users to request that their data is deleted?
  **Yes**: data lives on the person's deployment, whose administrator deletes
  conversations and accounts in the dashboard; for managed deployments, by
  request at the deletion URL in `COMPLIANCE.md`.

For every type below: **Collected: Yes. Shared: No. Processed ephemerally:
No. Required or optional: Optional** (the person chooses to send it), except
where noted. **Purpose: App functionality.**

| Category | Data type | Notes |
|---|---|---|
| Personal info | Email address | Required to sign in |
| Personal info | User IDs | Required: the deployment account |
| Photos and videos | Photos | |
| Audio | Voice or sound recordings | |
| App activity | Other user-generated content | Messages, documents, files |
| Files and docs | Files and docs | Files shared into the app or attached |
| Device or other IDs | Device or other IDs | Push token and Firebase installation ID, required for notifications only when the person turns them on |

"Shared" stays No: Google's definition excludes transfer to a service
provider acting on the developer's behalf (the push relay, Firebase Cloud
Messaging) and transfer the user initiates to their own server.

Not collected: location, financial info, health and fitness, messages in the
SMS/email sense, contacts, calendar, app info and performance, web browsing.

## Permissions to explain (Play "permissions declaration" if asked, App Review notes)

| Permission | When it is asked | Why |
|---|---|---|
| Notifications (`POST_NOTIFICATIONS`; iOS prompt) | When the person turns on notifications in Settings | Replies, approvals, Cortex findings, alerts |
| Camera | The first time they take a photo to attach | Attach a photo |
| Microphone (`RECORD_AUDIO`) | The first time they record a voice note or dictate | Voice notes, dictation, hands-free |
| Speech recognition (iOS) | The first time a voice note or dictation is transcribed | Text on the phone |
| Face ID (iOS) / biometrics (Android) | When they turn on the app lock | App lock |
| Photo library (iOS) | When they choose photos | Attach photos; Android uses the system photo picker, no permission |

The app asks for no location, contacts, calendar, storage or background
permission.
