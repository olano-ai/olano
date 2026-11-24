# Pages: publishing sites and web apps

An agent can put its work on the web at `/pages/<slug>/` on your deployment:
a report, a dashboard, a single-page app built with npm, or a small web app
with its own server and live data. Every page sits behind a password that only
you set, per agent, on the agent's **Pages** tab. Until you set it, the agent's
pages answer `503` and show nothing. Agents can neither see nor change that
password.

There are three kinds of page, and the agent picks the tool that fits.

| Kind | Tool | What runs | Good for |
|---|---|---|---|
| Files | `pages_start` | Nothing. The folder is served as it is. | A report, an HTML export, a static site the agent wrote by hand. |
| Static build | `pages_deploy` with `build` and `output` | A build command, once per deploy. | React, Vue, Svelte, Astro or Vite apps: `npm ci && npm run build`, served from `dist`. |
| Server | `pages_deploy` with `start` | A long-running HTTP server. | Express, Fastify, Flask or FastAPI apps with an API, forms, or live data from a database. |

A page can also have both a build and a server: the build runs first, then the
server starts.

## Asking for a page

Tell the agent what you want, in plain words: "publish the weekly report as a
page", "deploy the CRM dashboard so I can open it from my phone". The agent
chooses the kind, runs it, and tells you the URL. Then:

1. Open the agent's **Pages** tab and set the viewer password if it is not set.
2. Open the URL and sign in with that username and password.

`pages_deploy` runs commands, so it goes through the same approval as the
agent's shell: under the **safe** and **production** security profiles you
approve each deploy before anything is recorded. An agent with no shell access
cannot use it at all.

## Static builds

The agent gives a `build` command and the folder the build writes (`output`,
usually `dist` for Vite or `build` for Create React App). Each successful build
is copied into a fresh, read-only release and the page switches to it in one
step, so a viewer never sees a half-written build. **A failed build changes
nothing**: the previous release keeps serving, and the card says the build
failed and why. The last three releases are kept.

For apps with client-side routing (React Router, Vue Router), the agent sets
`spa`: any address that is not a file then loads the app's `index.html`, while
a missing script or image still answers `404`.

The app must load its files from under its own address. Relative paths
(`./assets/app.js`) work everywhere; with Vite the agent sets `base` to
`/pages/<slug>/`. `pages_start` and `pages_deploy` warn when an `index.html`
links paths starting with `/` that would miss the page.

## Servers

The agent gives a `start` command, such as `node server.js` or
`python3 app.py`. The server must listen on `127.0.0.1` and the port in the
`PORT` environment variable (the agent can pin a port instead with `port`, for
a server that cannot read `PORT`). Olano:

- **starts it** in the agent's sandbox, the same isolated place its shell
  commands run, with no access to your credentials;
- **waits** until its health path (`/` unless the agent set another) answers,
  up to one minute by default, and shows "starting" to viewers meanwhile;
- **forwards** every request for the page to it, including form posts, API
  calls, uploads and streamed responses;
- **watches** it: a crash or three failed health checks in a row restarts it,
  waiting 1, 2, 4, 8… seconds between attempts;
- **gives up cleanly** after five crashes without a stable run in between. The
  page then reads **failed** with the end of the server's output, and the agent
  is expected to fix the cause and restart it. Restarting, or deploying again,
  clears it.

A server that loses its connection for a reason that is not its own (the
sandbox was rebuilt, the deployment restarted) is brought back at once and is
not counted as a crash. Servers come back on their own after a restart of the
deployment.

The page lives under `/pages/<slug>/`, and by default the server sees requests
without that prefix: `/pages/crm/api/people` arrives as `/api/people`. Its own
pages should therefore call `fetch("api/people")` (relative), not
`fetch("/api/people")`. A framework configured with base path `/pages/<slug>`
(Next.js `basePath`) sets `strip_prefix: false` to receive the full path. The
prefix also arrives in the `X-Forwarded-Prefix` header.

### What a server can and cannot see

A page server is code the agent wrote, so Olano keeps it on a short leash:

- It never receives the viewer's password or your dashboard sign-in. It learns
  **who** is viewing from the `X-Olano-Viewer` header (the username they signed
  in with), never how they proved it.
- Its cookies are confined to its own page, and it cannot set cookies that
  belong to the dashboard, clear the browser's data for the whole site, or
  register a service worker outside its page.
- Its redirects stay under its page.
- A form post or API call from another website is refused, so a page cannot be
  driven by a link someone else sends you.
- Viewers' requests are bounded: a request waits at most two minutes for the
  server, uploads are capped at 25 MB, and at most 64 requests run at once per
  page. All three are adjustable in **Config → Deployed pages**.

Each agent's sandbox is isolated on the network: no other agent's sandbox, and
no other container on the machine, can open a connection to a server running
in it, so the page password is the only way in. On the rare machine where that
isolation is switched off or cannot be set up, a server listening on all
network interfaces instead of `127.0.0.1` could be reached around the
password, and the page card warns about it.

## Watching and fixing a page

Each card on the agent's **Pages** tab shows the page's kind and state:

| State | Meaning |
|---|---|
| **building** | The build is running. A page that already had a release keeps serving it. |
| **starting** | The server is up and Olano is waiting for its health path to answer. |
| **running** | The server answers and viewers reach it. |
| **live** | A static build's release is being served. |
| **restarting** | The server crashed or stopped answering; it is restarting after a short wait. |
| **waiting** | Something outside the page blocks it, such as the agent's sandbox being down or deployed pages being switched off. The card says what. |
| **failed** | The build failed with nothing earlier to serve, or the server crashed five times in a row. The card shows the last error. |

**Logs** opens the server's output and the last build's output. **Restart**
serves a stopped page again; for a deployed page it re-runs the build and
restarts the server, which is also how to pick up code changes and clear a
failed state. **Stop** stops serving the page and stops its server; the address
stays reserved for the agent.

The agent has the same view through its own tools: `pages_status` reports the
state and the last error, and `pages_logs` reads the server or build output.
The same logs are in the agent's workspace under `.olano/pages/<slug>/`.

## Limits and switches

All of these live in **Config → Deployed pages** (the
[`pages:` block of `config.yaml`](../config-yaml.md#pages-deployed-pages)):

- **Run page builds and servers**: the master switch. Off stops every page
  server within a few seconds and refuses new deploys; plain pages and static
  releases that were already built keep serving.
- **Servers per agent**: how many server pages one agent may run at once
  (default 5).
- **Health check every**, **Request timeout**, **Largest upload** and
  **Requests at once, per page**, as described above.

Not supported yet: WebSockets, custom domains, and pages without a password.
