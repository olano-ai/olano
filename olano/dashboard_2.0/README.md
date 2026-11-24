# Olano Mission Control 2.0

A modular React + Vite + TypeScript rebuild of the dashboard, replacing the
18k-line single-file `dashboard/public/index.html`. Ported incrementally
(strangler-fig) — the original dashboard keeps running until 2.0 reaches parity.

## Why

The old dashboard packed CSS + HTML + 16 views + 100+ functions into one global
scope with a mutable global `state` object and zero tests. 2.0 splits that into
typed modules: a small `core/` (api, sse, auth) and one folder per view.

## Run (dev)

```bash
cd olano/dashboard_2.0
npm install
npm run dev          # Vite on http://localhost:3201, proxies /api → :8765
```

Start the Olano runtime separately so the API is up:

```bash
olano serve       # FastAPI runtime on :8765
```

Point the proxy at a non-default runtime with `LIONCLAW_RUNTIME_URL`.

## Build

```bash
npm run build        # → dist/  (static; serve behind the same proxy as today)
npm run typecheck    # tsc, no emit
```

The build is **backend-free** — `tsc --noEmit && vite build` needs no running runtime
(`types.gen.ts` is optional and imported nowhere). So any static host can build from source.

## Serving the built SPA

The runtime (`olano serve`, FastAPI on `:8765`) is a long-running stateful process, and
the SPA must be served from the **same origin** as it. That is not a preference: the
`lc_session` login cookie (`__Host-lc_session` under HTTPS) is first-party only there,
the terminal WebSocket (`/api/terminal/ws`) cannot traverse a cross-site rewrite, chat
streams get capped by any intermediary proxy's timeout, and the installable dashboard's
service worker has to sit on the origin that serves `/api`.

> **Do not host this bundle on a separate CDN in front of the backend.** That shape
> existed once and was removed — it breaks all four of the above. See
> [`deploy/README.md`](../../deploy/README.md).

Two supported ways to serve `dist/`:

| | What runs it | Used by |
|---|---|---|
| **Caddy** | `docker/dashboard/Dockerfile` bakes `dist/` into a `caddy:2` image; `deploy/Caddyfile.allinone` serves `/` and proxies `/api/*` | every all-in-one deploy, including every managed cloud box |
| **`server.mjs`** | a small Node static server + `/api` proxy on `:3200` | `olano dashboard` (local and host-native installs) |

Both serve real files before falling back to `index.html`, so `/sw.js`, `/manifest.json`
and the icons are served as themselves rather than as the SPA shell.

**Before exposing the backend publicly:**

- Never expose raw `0.0.0.0:8765` — front it with a TLS reverse proxy (Caddy / nginx /
  Cloudflare Tunnel). HTTPS is required for the session cookie.
- Enable **multi-user mode** (bootstrap an admin) — otherwise the API (incl. `/api/files*`
  CRUD and the host shell) is unauthenticated.
- Consider `LIONCLAW_ENABLE_TERMINAL=0` for a public deploy (the terminal is a host root
  shell). Restrict who gets admin.
- Ensure the reverse proxy forwards the real client IP, or the `60/60s` rate limiter
  (skipped for localhost) is effectively bypassed.
- Leave `LIONCLAW_DASHBOARD_TOKEN` **unset** under this baseline — pure rewrites can't
  inject the bearer header; rely on cookie/login auth + network protection.

> To later enable the terminal + an uninterrupted live stream (Option B), point the
> WebSocket and the persistent EventSource directly at the backend domain via a build-time
> `VITE_API_DIRECT_BASE`, switch the backend CORS to an origin allow-list with
> `allow_credentials=True`, set the cookie `SameSite=None; Secure`, and set
> `LIONCLAW_DASHBOARD_ORIGIN`. This needs a custom domain sharing a registrable domain with
> the backend to survive browser third-party-cookie blocking.

## Auth gate & onboarding

`core/auth.tsx` calls `GET /api/auth/me` on load and routes to:

- **bootstrap** — first run, no users yet (`setup_required`) → "Create your account" (the owner/admin). This is the only sign-up.
- **login** — users exist, not signed in (`auth_required`) → Sign in + **Forgot password?** (→ "ask your admin").
- **ready** — authenticated (or single-user implicit owner).

**First-run setup wizard.** On a deployed (multi-user) instance, `/api/auth/me`
also returns `onboarding: {required}` — true for admins until any admin
completes or skips the guided setup tour. While required, `GatedApp` renders the
full-screen `views/onboarding/OnboardingView` *instead of* the app (like
Login/Bootstrap): welcome/mission → change login password → change vault
passphrase → connect an LLM provider (API key, or OpenAI Codex via the
ChatGPT-subscription OAuth device flow) → create a first agent → connect a
messaging app (Telegram featured as easiest; WhatsApp, Discord, Slack + a
dropdown of every other platform connector, with catalog-driven fields and
guides) → enable web search (Parallel's free hosted search MCP attached to an agent, or
a Tavily API key for the built-in web toolkit) → optionally install the
Agent Father / Customer Service archetypes. Every step is skippable (and the whole tour is
one-click skippable); finishing POSTs `/api/onboarding/complete` and calls
`refresh()`, which drops the gate. The flag is deployment-wide
(`<lionclaw_home>/onboarding.json`), so it follows the server, not the browser;
`DELETE /api/onboarding` re-arms the tour. Single-user installs never see it.

**Users are invite-only.** An admin invites a person by email in **Admin → Users**
(blank password → an **invite link + QR** modal to share via any channel). The
public route **`/invite/:token`** (`AcceptInviteView`, outside the gate) is where
the invited user — or a user the admin sent a **Reset password** link — sets their
own password and is signed in. Tokens are single-use, 7-day expiry. The admin's
own reset is `olano users reset-password --email …`.

In single-user installs (`multi_user_enabled = false`, the default) the API
treats the caller as the implicit owner/admin, so the gate lands straight on the
app with no login.

## Typed API contract

`core/types.ts` is hand-written for the slice consumed today. Generate the full
source of truth from the live FastAPI schema:

```bash
npm run gen:types    # openapi-typescript → src/core/types.gen.ts
```

## Layout

```
src/
  core/    api.ts  sse.ts  auth.tsx  types.ts
  ui/      toast.tsx
  styles/  tokens.css  globals.css      # design tokens lifted from the old dashboard
  views/   auth/{Login,Bootstrap}  chat/  agents/  admin/  Layout.tsx
  app.tsx  main.tsx
```

## Ported so far

- ✅ Auth gate (bootstrap / login / logout)
- ✅ Chat (SSE streaming + tool events)
- ✅ Agents (access-filtered list)
- ✅ Admin (user CRUD + per-agent access management)

## Remaining views to port

Overview · Cortex/Pulse · Scheduler · Files · Memory · Knowledge · Tools ·
Providers · Vault · Connections · Logs · Activity · Playground 2. Flip the
default and delete the old `dashboard/public/index.html` once at parity.
