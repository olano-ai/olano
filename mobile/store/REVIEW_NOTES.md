# Review notes

Templates to paste. Replace `<PASSWORD>` with the demo template account's
password from the team's password manager, in the store consoles only; never
commit it. Keep each under the field's limit (App Store notes: 4000
characters).

## App Store review notes (App Store Connect, App Review Information, Notes)

```
Olano is the mobile app for Olano, a platform where a business runs AI agents on its own Olano deployment (a server at its own address, e.g. acme.olano.run). The app is a native client for that deployment: it signs in with the account the person already has there. It cannot create accounts, and it contains no purchases.

HOW TO REVIEW
1. On the first screen, tap "Try the demo". The app opens a demo deployment we run for review, with sample agents, already signed in as a demo member. No address or password is needed.
2. Chats: tap an agent, send a message, watch the reply stream in with the steps the agent takes. Try a photo (camera or library) and a voice note (hold the microphone).
3. Cortex: the agents' ideas and projects; open one, promote or discard it.
4. Inbox: approvals and alerts from the deployment.
5. Settings: notifications, appearance, the app lock, and "Switch deployment".

If "Try the demo" ever fails, the same demo can be reached manually: type demo.olano.run in the address field, tap Continue, and sign in with
  Email: demo@olano.run
  Password: <PASSWORD>
(also given in the Sign-In Information fields).

NOTES
- Guideline 4.2: the app is its own native interface (tab bar, stack navigation, swipe back, pull to refresh, haptics, widgets, Siri intents, a share extension), not a website in a wrapper; it never loads the deployment's web pages.
- Guideline 5.1.1(v): the app does not support account creation. Accounts are created by each deployment's administrator in its web dashboard, which is also where accounts and their data are deleted.
- Guideline 2.3.1: the demo entry is visible to every user permanently. A small public configuration file can hide it or point it at another demo server; it cannot enable any feature.
- Generative AI: replies come from the AI models the deployment's owner configured. The demo's agents run on an inexpensive model with conservative instructions; each visitor has a separate account that sees only its own conversations and is deleted with them after a day, and nothing in the demo reaches the outside world.
- Push notifications, the camera, the microphone, speech recognition, Face ID and the photo library are requested only when the person uses the feature that needs them.
- Encryption: standard HTTPS only (ITSAppUsesNonExemptEncryption is false).
- The app talks only to the deployment the person chose, plus Apple's speech recognition when on-device transcription is unavailable and our push relay. No analytics, advertising or tracking.

Contact: support@olano.ai
```

## Google Play: App access (Play Console, App content, App access)

Choose "All or some functionality in my app is restricted", then add one set
of instructions:

- **Name**: Olano demo
- **Username**: demo@olano.run
- **Password**: `<PASSWORD>`
- **Any other information required to access your app**:

```
No credentials are needed: on the first screen tap "Try the demo" and the app opens our demo deployment, signed in, with sample agents. To use the credentials above instead, type demo.olano.run in the address field, tap Continue, then sign in. The app is a client for a business's own Olano server; it cannot create accounts.
```

- **Is this login information reusable?** Yes (the account itself is kept;
  each "Try the demo" visit gets a fresh visitor account of its own).

## Google Play: other App content answers

| Declaration | Answer |
|---|---|
| Ads | No, the app does not contain ads |
| Advertising ID | No (the permission is removed from the manifest) |
| Target audience | 18 and over (a business tool; see `COMPLIANCE.md`) |
| News app | No |
| Government app | No |
| Financial features | None |
| Health | None |
| COVID-19 contact tracing | No |
| Data safety | `PRIVACY.md` |
| Content rating (IARC) | `COMPLIANCE.md` |
| Account deletion | `COMPLIANCE.md` (the app does not create accounts; give the URL anyway, Play asks every app that has sign-in) |
