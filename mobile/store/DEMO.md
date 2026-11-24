# The review demo

The app connects to a person's own Olano deployment, so an App Reviewer, a
Play reviewer or a prospect has nothing to connect to. The answer is a demo
deployment Olano runs, reachable from a **Try the demo** button on the
connect screen, signed in without a password.

## Why it is shaped this way

| Rule | How the design meets it |
|---|---|
| Apple 2.1: a working demo account, or a fully featured demo mode | Try the demo opens a real deployment with real agents, signed in; every screen works. The review notes also give a manual sign-in as a fallback. |
| Apple 2.3.1: no hidden or undocumented features, nothing switched on after review | The entry is visible to everyone, always, in the reviewed build. The remote config can only **hide** the entry or **move** it to another demo deployment; it carries no feature flags (`src/appConfig.ts` parses nothing else, and both repositories have a test pinning the shape). |
| Apple 1.2: user-generated content between strangers needs safeguards | Each visitor gets a member account of their own (`visitor-<hex>@demo.invalid`) that sees only the conversations it started: the thread list, search, a direct read and the live stream all hold it to its own (`demo_access.scopes_threads`). The runtime deletes each visitor account with its conversations after a day. What stays shared (the agents' memory and workspace) is hardened below and reset by hand (section 3). |
| Google Play: App access instructions for login-gated apps | "Tap Try the demo" needs no credentials; the manual sign-in is also given. |
| No secret in the app or in public | No password ships in the app or in the public config. The demo box mints the session (`POST /api/auth/demo`), only for an account that is not an administrator, checked on every call. |

### The credentials trade-off

Three options were weighed:

1. **A demo username and password in the public config.** Simplest, and works
   with any runtime, but the password is public forever (cached copies,
   decompiled builds), works in the dashboard too, and every visitor shares one
   account and sees everyone else's chats.
2. **A demo token in the config.** The same exposure as a password, with a
   new credential type to build and rotate.
3. **The demo box signs visitors in itself (chosen).** `OLANO_DEMO_ACCOUNT` on
   the demo box names a template member; `POST /api/auth/demo` creates a fresh
   member per visitor with the template's agents and returns its session
   cookie. Off (404) on every other box. Nothing reusable is ever published,
   it can be switched off on the box in seconds (unset the variable and
   restart, or make the template an admin, which the route refuses), and
   visitors are isolated from each other. The cost is that the demo box must
   run a runtime with the route (any release after this change).

A real password still exists for the template account, for App Review's
"Sign-in required" fields and the manual fallback. It lives only in App Store
Connect and Play Console (both private to the reviewers) and in the team's
password manager, never in this repository.

## 1. Provision the demo deployment

1. Create a managed deployment in the Olano console for an Olano-owned
   organization (not a customer's), named so its address is
   `https://demo.olano.run`. Any other `*.olano.run` or `*.olano.ai` name works
   if the config's `MOBILE_DEMO_URL` names it; the app refuses other domains.
   Smallest machine that runs three agents comfortably.
2. Finish the dashboard's first-run setup with an **admin** account owned by
   the team (a shared mailbox such as `mobile-demo@olano.ai`), password in the
   team's password manager. This account is never given out.
3. Turn multi-user mode on (it is on by default for managed boxes).
4. Create the agents a reviewer should see, from archetypes, with friendly
   names, avatars and a first conversation each. Suggested: a research analyst,
   a writer and a scheduler. Give each a short `description`.
5. Create the **template** member in Users: email `demo@olano.run`, name
   "Demo", role **Member** (never Admin), a long random password (password
   manager, entry "Olano demo template"). Grant it the demo agents as a
   member.
6. In the deployment's environment (console, deployment settings, environment
   variables), set `OLANO_DEMO_ACCOUNT=demo@olano.run` and restart.
7. Check from a terminal:
   `curl -si -X POST https://demo.olano.run/api/auth/demo | head -20`
   must answer 200 with a `Set-Cookie` and a `visitor-...@demo.invalid` user.
   On any customer box the same request answers 404.

## 2. Harden it

The demo is public. Assume anyone will try anything.

- **Model and spend.** Point every demo agent at an inexpensive managed tier
  (or `fake:realistic` for a demo that needs no model at all), and give the
  deployment a hard monthly credit budget in the console with alerts at 50 %
  and 80 %; the budget is the hard bound on cost. Also set
  `message_policy.max_input_chars` (for example 2000) and `max_output_tokens`
  on each demo agent; check on the release in use which surfaces enforce them
  (they are external-ingress caps), so they are a second belt, not the first.
- **No outbound reach.** No connectors, no MCP servers, no messengers, no
  email, no webhooks, no external access, no A2A. Disable `talk_to_agent` to
  anything outside the demo agents. Browser control **off**. Agent shell on the
  default sandbox only, and preferably `backend: filesystem` (no shell).
- **No autonomy that acts outside.** Cortex may stay on for the screens to have
  content, but Fully Autonomous **off** and `approval_required` at its default.
  Scheduled tasks only ones that write inside the workspace.
- **Shared memory.** Turn self-learning off on the demo agents (otherwise one
  visitor's words reach another's replies through MEMORY.md), and keep the
  agents' instructions telling them not to store visitor details.
- **Members only.** The template stays a member; the route refuses an admin
  template and never grants more than member rights. Do not grant the
  template "owner" on any agent (visitors get member either way, but keep the
  template honest).
- **Rate limits.** The route allows 30 sign-ins a minute per process and 5000
  visitor accounts; the runtime's own request rate limiter applies on top.
- **Push.** Allowed (it is a managed box, so the relay works) and useful for a
  reviewer who backgrounds the app. Keep `push.previews` off (the default), so
  no message text leaves the box in a notification.
- **Abuse handling.** Watch the deployment's alerts. A reviewer may report
  objectionable content: the runtime deletes it with the visitor's account
  within a day; for something urgent, delete the visitor account in Users (or
  every `@demo.invalid` account), which leaves its conversations unreachable
  until the hourly sweep removes them.

## 3. Resetting the demo

**Visitors: automatic.** Every hour the runtime deletes each visitor account
older than a day (`runtime/demo_access.asweep_visitors`, on the demo box only,
because only there is `OLANO_DEMO_ACCOUNT` set), and with it every thread under
that visitor's prefix: transcripts, their sidecar files and their
checkpoints. A visitor's demo sign-in lasts a day for the same reason. This
is what the app's demo hint and Settings note promise, so nothing about it is
manual.

**What visitors share: by hand, until a job exists.** The agents' own state
is not a visitor's and the sweep never touches it:

1. Restore each demo agent's workspace (`MEMORY.md`, `memories/`, uploads,
   Cortex items) from a snapshot taken after step 1.4 of provisioning.
2. Clear the agents' threads other than the seeded first conversations
   (scheduled and Cortex runs; visitors' own threads are already gone).

The simplest faithful implementation is a box-level restore: snapshot the
deployment's home volume once it is provisioned and seeded, and restore that
snapshot from the control plane (a scheduled worker job). Until that job
exists, do the two steps by hand weekly and before every submission. With
self-learning off (section 2) visitors' words do not reach this shared state
in the first place, which is why weekly is enough. The owner of this job is a
product decision (see the end of this file).

## 4. The remote config

`GET https://cloud.olano.ai/api/v1/mobile/config` (cloud repository,
`apps/web/lib/mobile-config.ts`), set through the web app's environment:

| Variable | Default | Effect |
|---|---|---|
| `MOBILE_DEMO_ENABLED` | on | `false` hides the entry for every new launch |
| `MOBILE_DEMO_URL` | `https://demo.olano.run` | Where the button goes (https, olano.run or olano.ai only) |
| `MOBILE_DEMO_LABEL` | `Try the demo` | The button (40 characters) |
| `MOBILE_DEMO_ACCOUNT_HINT` | the bundled sentence | The line under the button (280 characters) |
| `MOBILE_MIN_SUPPORTED_VERSION` | none | Older apps show an "update available" note; nothing is blocked |

The app's bundled default has the demo **on**, so a phone that has never
reached the config (first launch offline) still shows it. A phone keeps the
last good copy and refetches at most every ten minutes; the server caches five.

## 5. Review notes

Paste from `REVIEW_NOTES.md`. In App Store Connect, App Review Information:

- **Sign-in required**: checked. User name `demo@olano.run`, password: the
  template's password from the password manager. (The reviewer will normally
  not need it: Try the demo signs in without one.)
- **Notes**: the "App Store review notes" text.

In Play Console, App content, App access: "All or some functionality is
restricted", one instruction set with the same address and credentials, and
the "Google Play App access" text.

## 6. After review: the switch

The demo is meant to stay, as the guideline-safe default: prospects use it
too. If it must go:

1. Set `MOBILE_DEMO_ENABLED=false` on the cloud web app and redeploy its
   configuration. Within about ten minutes new launches stop showing the
   entry; nothing else in the app changes, and nothing is unlocked.
2. Keep the demo box running until the next app version has been reviewed:
   a phone that is offline keeps its last copy, and **every future submission
   needs a working demo again**, so turn the entry back on (and the box up)
   before submitting an update. The "switch to production" is this one
   variable; there is no hidden mode in the app.
3. To retire the demo for good, remove the entry from the app in a normal,
   reviewed release, and only then unset `OLANO_DEMO_ACCOUNT` and delete the
   box.

To move the demo (a new box, a new name), stand up the new box, set
`MOBILE_DEMO_URL`, and keep the old one for a day while phones refresh.

## Decisions for the product owner

- The demo box's address (default `demo.olano.run`) and the organization that
  owns it.
- Who owns the template account, its password and the shared-state reset job.
- The model the demo agents run on and the monthly credit cap.
- Whether the demo stays visible after launch (recommended: yes).
