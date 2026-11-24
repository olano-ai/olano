# MCP servers

MCP (Model Context Protocol) servers give an agent tools that Olano does not
ship — a vendor's own integration, an internal service, a community server.

Add one from the agent's **MCP** tab, or ask the agent — *"connect yourself to
Notion"* — and it finds every supported way to reach the service, adds the one
you pick and starts the sign-in (`self_find_integration` →
`self_add_mcp_server` → sign-in). AgentFather does the same for any agent
(`olano_find_integration`, `olano_add_mcp_server`). Both read the Olano MCP
directory — a curated catalog of add-able servers with verified endpoints and
ready-made presets — so nobody has to type a server URL from memory.

**Supported integrations come first.** When you ask an agent to connect a
service, it offers the supported ways to reach it, in this order: (1) a
**messaging channel**, when the service is a place people talk; (2) an **MCP
server** from the directory — it usually carries its own sign-in, so you click
a consent screen you recognise; (3) a **built-in toolkit**, signed in through
the Connections card's best method — Olano's app, your own OAuth app, a code
typed on a phone, or as a last resort a key entered on a secure form. All
three run inside Olano, are authenticated by Olano and appear in
**Connections**. Anything the agent cannot find in a catalog is **external**:
it may be installed in the agent's sandbox, Olano never authenticates it, and
it is at your risk — the agent says so before doing it, and reads the
project's documentation first (`self_inspect_external`) to tell you what it
is, what it needs and whether a built-in integration already covers it. A
small purpose-built MCP server is the answer only when the platform matters
and nothing supported covers it. Whichever route: add the server or toolkit
first, then sign in — in that order.

**Transports:**

- `sse` (default) and `http` — the server runs somewhere and you connect to a URL.
- `stdio` — Olano launches the server as a process (`command` + `args` +
  `env`). On a managed deployment each stdio server runs inside an isolated,
  throwaway container with access to the agent's workspace but not to the
  platform itself, so `npx`/`uvx`-style servers work there too. That container
  receives **only the `env` the server declares** — write each credential as
  `${vault:NAME}` there and it resolves for that agent, a GitHub connection
  included — never the rest of the vault. It requires sandboxed execution to
  be enabled for the deployment (it is by default), and if the sandbox is
  unavailable the server is skipped with a message naming the reason — remote
  (`http`/`sse`) servers are always available. If a catalog entry's command is
  itself `docker run …`, use its remote preset instead on a managed
  deployment.

**Authentication — a server that signs you in.** Set `auth: "oauth"` for a
server that requires a sign-in; the **Authorize** button on the MCP tab walks
through it, and the grant is stored for you. From a chat, an agent can
start the same flow: in the **dashboard chat the sign-in appears as a Connect
button card** — the service's name and logo with one button to click, never a
wall of URL text — while on messaging channels the agent sends the link (and
PIN, when one applies) as a plain message you tap. Either way you sign in with
the provider in your own browser and the agent's new tools come online on
their own, without anyone touching the dashboard.

Add the server to the agent **before** signing in. Authorizing a URL the agent
does not have stores a grant for a server it will never load — the sign-in
looks successful and nothing works. If a sign-in does complete for a server
that was never added, Olano now registers it using the transport the sign-in
itself connected over; adding it first, with the settings you actually want, is
still the right order.

**Where the sign-in comes back to.** A provider sends you back after you sign
in, and it insists on a secure (https) address it can trust. Olano picks that
address for you, in this order:

- **Olano Cloud deployments** use Olano's sign-in service at
  `auth.olano.ai`. Nothing to set up.
- **A self-hosted install with an Olano API key** (the one you pasted on
  **Providers → Olano** for credits; see
  [Models and credits](../models-and-credits.md#olano-credits-on-a-self-hosted-install))
  uses the same sign-in service, even with no domain or certificate of its
  own: a box on a home network or a bare IP address can sign in to MCP
  servers. The box registers itself in the background a few seconds after the
  key is pasted or after a restart, and stays registered while the key is
  live. The sign-in service only ever passes back a one-time code that is
  useless without a secret that never leaves your box; the tokens are
  exchanged and kept on your box, as always. Revoking the key on
  `cloud.olano.ai` stops this at once. An account can have up to five
  self-hosted installs using it at a time; an install that has not been used
  for 30 days gives up its place on its own.
- **A self-hosted install with its own https address** (a domain with a
  certificate, set as the deployment's public address) keeps using that
  address, key or no key.
- **A self-hosted install with neither** has nowhere for the provider to send
  you back: the Authorize button and the agent explain that, and the fix is to
  paste an Olano API key or give the box a public https address.

This sign-in service is only for MCP servers. The built-in **Connect with
Olano's app** option on the Connections tab (for example Google or Microsoft
365) is offered on Olano Cloud deployments only; on a self-hosted install, use
**Use your own OAuth app** with an OAuth client you create in that service's
developer console (see [Connections](../dashboard/connections.md)).

**A server that wants an OAuth app of your own.** A few servers — HubSpot is
the common one — do not register clients automatically and only accept an
OAuth app you created in their developer console. (For HubSpot this is only
the MCP server: the built-in `hubspot` toolkit connects from Connections →
HubSpot, with Olano's app where your deployment offers it.) The directory knows which
ones, so the magic link opens straight on a form asking for the app's
**Client ID** and **Client secret**: it shows the exact redirect URL to
register on the app, links to the provider's console, and continues the
sign-in from the same page once the two values are saved. A server nobody had
catalogued that turns out to want the same thing comes back to that form
instead of failing the link. The Client ID stays on the agent's server entry
(visible on its **MCP** tab); the Client secret goes into the **Vault** as that
agent's own value, under a key named after the server
(`MCP_OAUTH_CLIENT_SECRET_…`), and the entry keeps a `${vault:…}` reference to
it — the same way a bot token or an API-key header is kept out of the config
file. A later reconnect never asks again. The same form offers **Composio** as
the route that needs no app of your own: one
Composio sign-in reaches hundreds of platforms, and the page gives you a
message to paste back to the agent — it connects the Composio MCP first, then
the platform through it.

**Authentication — a server that wants an API key.** Ask for the key over a
secure link rather than typing it into a chat, then point the server entry at
the stored value instead of pasting the key into the config:

```json5
{
  url: "https://mcp.example.com/mcp",
  transport: "http",
  headers: { Authorization: "Bearer ${vault:EXAMPLE_API_KEY}" },
}
```

`${vault:NAME}` works in both `headers` (for `http`/`sse` servers) and `env`
(for `stdio` ones), and resolves per agent — two agents can use the same server
with their own keys. Write the key as a literal only if you are content for it
to live in the config file.

Olano renews grants before they expire — see the
`mcp_health:` settings in [`mcp_health:` — keeping MCP connections alive](../config-yaml.md#mcp_health-keeping-mcp-connections-alive).
This matters more than it sounds: a token that lapses while an agent is idle can
become unrecoverable, so leave `refresh_tokens` on.

**Health.** The **MCP** tab shows each server's live status, and Config →
MCP connection health shows the fleet. Servers that time out or error are
retried with backoff. A server that needs a sign-in you have never given is
**not** retried — reconnecting cannot invent consent, so you have to authorize
it. AgentFather reports the same with `olano_mcp_status`.

**Narrowing a server.** `disabled_tools` on the server entry hides specific
tools it offers — useful when a server exposes fifty tools and your agent needs
three. Only the enabled tools are bound to the agent and named in its prompt,
so a hidden tool costs nothing per turn. The agent still knows the hidden
ones exist: `self_list_mcp_tools` lists every tool a server offers with the
switched-off ones marked, so when a task would need one the agent can say so
and suggest enabling it here rather than routing the task through another
service. Its prompt tells it how many tools are switched off per server.

In the dashboard this is the row of switches on the server's card, one per tool,
with **Enable all** and **Disable all** above them. Those two are the fast route
to "this agent only needs three of these": disable all, then switch the three
back on. Each switch takes effect the moment you click it, and a burst of clicks
is saved together, so working through a long list stays responsive.

Two things to know:

- **Disable all** needs to know the server's full tool list, so it stays greyed
  out until the card has actually connected — press **Refresh status** first if
  it is. **Enable all** always works, since "nothing disabled" needs no list.
- The switches change the *configuration*. A running agent keeps the toolset it
  booted with until you press **Reload agent** on the same tab (or restart it).

A tool you disable on one server stays available on any other server that
offers a tool by the same name — the list belongs to the server it is set on.

**When a server stops accepting the authorization.** An OAuth server can
revoke a grant after the agent connected to it, so the MCP tab still reads
*Connected* while every call fails. The agent's tools are the first to learn
it: the runtime renews the token and retries the call once, and when that
fails the server's row turns **Needs authorization**, the bell rings on the
next health check, and the agent tells you the authorization has expired and
offers the fix in the same message: a sign-in link (the same one it uses to
connect a server the first time; the tools reload on their own when you
finish), or **Authorize** on the server's row in this tab, plus what else can
reach the same service meanwhile. Reconnecting never needs a Disconnect
first.
