# Store listing copy

What gets pasted into App Store Connect (App Information and the version page)
and Google Play Console (Main store listing). Character limits are the stores'
own; every field below is inside them. Never name the model providers behind
Olano's managed tiers, plan prices or credit rates here: the offering changes,
and the listing is reviewed against the app.

## Both stores

| Field | Value |
|---|---|
| App name (App Store, 30) | Olano |
| App name (Google Play, 30) | Olano |
| Category | Productivity (primary), Business (secondary, App Store only) |
| Support URL | https://olano.ai/support |
| Marketing URL (App Store, optional) | https://olano.ai |
| Privacy policy URL | https://olano.ai/privacy |
| Contact email (Play) | support@olano.ai |
| Copyright (App Store) | 2026 Olano |

Check that the three URLs answer before submitting: both stores reject a
listing whose privacy policy or support page does not load.

## App Store

**Subtitle** (30):

> Your AI agents, in your pocket

**Promotional text** (170, editable without a new build):

> Talk to the AI agents on your Olano deployment from anywhere: streamed replies, approvals with one tap, voice notes and photos. No deployment yet? Try the demo.

**Keywords** (100, comma separated, no spaces after commas, no app name repeated):

> ai agents,assistant,automation,chat,approvals,workflow,team,voice notes,productivity,agent,tasks

**Description** (4000):

> Olano puts the AI agents that work for you on your phone. Each agent has its own role, tools and memory, runs on your Olano deployment, and keeps working when you close the app: Olano tells you when a reply lands or when an agent needs your approval.
>
> CHAT WITH YOUR AGENTS
> - Streamed replies, with every step the agent takes and the plan it follows.
> - Send photos from the camera or your library, documents and files.
> - Voice notes, held or tapped to record, turned into text on your phone; hear replies read aloud, or talk hands-free.
> - Pick the model and the reasoning level per conversation.
> - Group chats where several agents answer, each under its own name.
>
> STAY IN CONTROL
> - Approve or decline an agent's action from the conversation, the Inbox, a notification or a widget.
> - Watch an agent use its browser live, and take over when it asks for help.
> - Cortex: every idea, prototype and project your agents are developing, with their ratings; promote, discard or talk an item over.
> - Scheduled tasks, alerts and your deployment's credits and usage.
>
> MADE FOR YOUR PHONE
> - Native navigation, light and dark themes, haptics, and an app lock with Face ID.
> - Siri and Shortcuts: ask an agent a question without opening the app.
> - Home Screen and Lock Screen widgets, Control Center controls and the Action button.
> - Share a link, photo or file from any app to an agent.
>
> YOUR DEPLOYMENT, YOUR DATA
> Olano connects only to your own Olano deployment, at the address you open its dashboard with, and signs in with the same account. Photos are sent without location data. There are no ads, no analytics and no tracking.
>
> NO DEPLOYMENT YET?
> Tap Try the demo on the first screen to open a demo with sample agents, already signed in. Create your own deployment at olano.ai.

**What's New** (first release):

> The first release of Olano for iPhone and iPad.

## Google Play

**Short description** (80):

> Chat with and approve the work of the AI agents on your Olano deployment.

**Full description** (4000): the App Store description above, unchanged except
"Face ID" becomes "your fingerprint or face", "Siri and Shortcuts" and the
widgets paragraph are replaced by:

> - Launcher shortcuts to talk to your favourite agents.
> - Share a link, photo, file or text from any app to an agent.

(Android has no widgets, Siri or Action button support yet; the Play listing
must not promise them.)

**Release notes** (first release):

> The first release of Olano for Android.

## Graphics checklist

- App Store icon: generated into `ios/App/App/Assets.xcassets/AppIcon.appiconset`
  by `scripts/make_icons.py` (1024 px, no alpha); App Store Connect takes it from
  the build.
- Google Play icon: 512 x 512 PNG, 32-bit, under 1 MB. Export from
  `assets/icon.svg`.
- Google Play feature graphic: 1024 x 500 PNG or JPEG, no alpha. To design;
  keep text out of the outer 15 % (Play crops it on some surfaces).
- Screenshots: `SCREENSHOTS.md`.
