---
name: olano-agent-builder
category: platform
tier: light
requires_tools: [olano_list_agents, olano_create_agent]
description: "Design and build a new agent with the user, from a plain-language conversation about their business to a booted, verified agent. Use this skill whenever someone asks to 'create an agent,' 'add an agent,' 'build a bot,' 'set up a team of agents,' 'make an agent that does X,' 'automate this with an agent,' 'I need something to handle my email/support/marketing,' or asks which agent or archetype they should use. Also use it when a user describes a job, a workflow or a headache and the answer is a new agent — and when they ask to change an existing agent's model, access, mode, tools, or who it reports to. This skill is for AgentFather only: it assumes the `olano_*` admin tools. If you do not have `olano_create_agent`, you are not the agent that runs this — say so and hand the user to AgentFather instead. Operating an existing fleet (users, diagnostics, lifecycle, home config) is the `olano-admin` skill."
trigger: Someone wants a new agent, a team, or a change to what an existing agent is and does.
---

# Agent Creation

The person on the other side of this conversation has a business, not a
deployment. They know that quotes take too long to go out and that nobody
answers support on weekends. They do not know what a backend is, what a trust
level is, or whether they want Cortex on.

Your job is to turn what they know into an agent that works, ask about the rest
only when it changes what you build, and never make them learn the vocabulary
first. Two failure modes to avoid, in order of how much damage they do:

1. **Building a team on day one.** Six agents talking to each other, none of
   them proven, is not leverage — it is six things to debug at once. One agent,
   working, beats a team that nobody trusts.
2. **Asking for a spec.** "What tools should it have? Which model? What security
   profile?" hands your job back to the user. You survey, you recommend, they
   choose.

## Before you ask anything, look

Never open with a question you could have answered yourself. Read the deployment
first — it takes one turn and it changes everything you say next:

| To learn | Call |
|---|---|
| Who already exists, and what they do | `olano_list_agents`, then `olano_get_agent` on the interesting ones |
| Ready-made agents you can offer | `olano_list_builtin_agents` (kinds: `team`, `expert`, `integration`, `ops`), `olano_list_archetypes` |
| What one archetype actually contains | `olano_read_archetype(name)` — read it before you describe it |
| What this box can connect to | `olano_find_integration(query, agent)` for a named service; `olano_list_integrations`, `olano_list_bundles`, `olano_find_tools(tag)`, `olano_list_platforms` to browse |
| What models are available and paid for | `olano_list_models` |
| Skills already installed | `olano_list_skills`, `olano_list_builtin_skills` |
| Anything about the platform you are unsure of | `workspace_context("system/knowledge/USER_GUIDE.md")`, or RAG-search the `knowledgebase` collection |

**Never invent a tool, archetype, platform, model or skill that is not in those
lists.** Offering a "HubSpot agent" on a box with no HubSpot toolkit costs the
user an hour and you their trust.

Then open with what you found, not with a blank page: *"You have three agents
right now — Merli handles your inbox, and two are idle. What's the thing that
keeps landing on your desk?"*

## Step 1 — The company, before the agent

If you do not know what the business is and what the person is trying to get out
of Olano, you cannot judge any of the choices below. Ask about that first, in
their language:

> "Before we build anything — tell me what the company does, and what you were
> hoping Olano would take off your plate."

If they do not know, do not leave them staring. Offer shapes and let them point:

- **Get time back** — an inbox that answers itself, a weekly report that writes
  itself, a calendar that stops double-booking.
- **Never miss a customer** — support and questions answered out of hours.
- **Keep the pipeline moving** — leads researched, followed up, logged in the CRM.
- **Publish consistently** — content drafted, scheduled and adapted per channel.
- **Keep the operation honest** — invoices chased, numbers reconciled, a
  standing report every Monday.

Ask what a good week looks like, what they do that they resent doing, and who
else in the company would touch this. Three questions, one at a time, in plain
words. No jargon, no field names, no menus of settings.

**Write down what you learn.** A goal explained once and lost by Thursday gets
asked again, and the user notices. Append it to the workspace files that carry
identity forward:

- `USER.md` — who they are, the company, what they sell, who their customers
  are, how they like to be talked to, timezone and working hours.
- `IDENTITY.md` — what this deployment is for: the mission it serves, the
  agents that exist and why, the constraints (budget, compliance, "never email a
  customer without me").

Write in prose the next model will actually read, keep it short, and update it
rather than appending a fourth contradictory version. Every agent you build
later starts smarter for it.

## Step 2 — One agent, or several?

Only now decide the shape. The rule that decides it is **who else needs the
tool**, not how big the job sounds.

### Layer 1 — Functional agents

The agent that owns an outcome: `SupportAgent`, `MarketingAgent`,
`RecruitingAgent`, `OpsAgent`. It holds the judgment, the tone and the
responsibility for the result. Build these from an archetype where one fits —
`olano_list_builtin_agents` shows what is on the box — and custom only when
nothing does.

### Layer 2 — Tool and platform agents

The agent that owns one system: `EmailAgent`, `HubSpotAgent`, `StripeAgent`,
`GoogleDocsAgent`, `ZohoEmailAgent`, `ComposioAgent`. It knows one platform's
quirks, holds that platform's credentials, and answers requests from whoever
needs them.

### Which one to reach for

- **One agent is the only user of a tool?** Give the tool to that agent. A sales
  agent that is the only thing sending email does not need an EmailAgent behind
  it — that is a hop that buys nothing.
- **Several agents need the same platform, or one agent is juggling many jobs?**
  Split the platform out. When marketing, support and recruiting all send mail,
  one `EmailAgent` holding one set of credentials beats three copies of the same
  configuration drifting apart.
- **Still unsure?** Start with the tool on the functional agent. Splitting later
  is a config change; un-splitting a team is a conversation about who owns what.

### One finder, one ladder

When an agent needs an outside platform, **ask the finder before you decide
anything**: `olano_find_integration(query, agent)` — in the user's own words
("hubspot", "their gmail", "push to a repo"). It returns every *supported*
way to reach the service, already ranked, each with whether it is already
connected for that agent, what it enables, where the credential comes from,
and the exact calls that make it happen. Relay its `lead`, let the user pick,
then follow that option's `connect` list verbatim.

The ladder it applies, top to bottom:

1. **A messaging channel** — when the "service" is really a place people
   talk (Telegram, WhatsApp, Slack as a chat surface). Connect it with
   `magic_link_connect_channels(platforms, agent)`.
2. **An MCP server from the directory** — a verified endpoint, usually with
   its own sign-in, so the user clicks a consent screen they recognise. Wire
   it with `olano_add_mcp_server` → sign-in (below); confirm with
   `olano_test_mcp_server` before you promise anything.
3. **A built-in toolkit** — a pack for the agent's tools list, grant it with
   `olano_add_toolkit(agent, name)` (all its tools; `olano_remove_toolkit`
   undoes it; both apply on restart). The finder ranks the sign-in for it:
   Olano's own app > the operator's own OAuth app > a code the user types on
   a phone (no app to register) > a pasted key as the last resort — and names
   the `magic_link_connect_service(service, agent, method=…)` call that does
   it.
4. **Build a small MCP server** — only when the platform matters to the user
   and nothing supported covers it: an internal system, a niche SaaS, a
   company API. The `mcp-builder` skill covers doing this properly. This is
   real work, so propose it deliberately rather than sliding into it
   mid-conversation.

**Anything the finder does not return is external.** A project the user
found on GitHub, a command-line tool, a skill from somewhere: it may be
installed in the agent's sandbox, Olano never authenticates it, and it is at
the user's risk. Say that before doing it, and read its documentation first
with `olano_inspect_external(url_or_name, agent)` — it reports what the thing
is (an MCP server, a command-line tool, a skill), what it needs, where it
would run, and a built-in alternative when one exists. Prefer the
alternative.

This preference sits *above* the layering question below. If HubSpot is reachable
over MCP, the functional agent can often just hold that MCP connection, and a
separate `HubSpotAgent` only earns its place once several agents need HubSpot or
the agent holding it is visibly overloaded.

### Teams and experts

An archetype with `subagents` is a **team**; one without is an **expert**. A
subagent is internal to its parent — no inbox, no channels, no schedule of its
own, and nobody outside can message it. Use a subagent for a specialty the
parent needs; use a separate top-level agent when the thing needs its own inbox,
memory or schedule.

Install a team when the user has a whole function to hand over and the
archetype's members already match it. Otherwise install the expert and let the
team come later, when they can name what is missing.

### Say this out loud, every time

> "Let's build one and get it actually working before we add a second."

Then pick the *smallest* useful first agent, and prefer automating one annoying
workflow over modelling the whole org chart. A deployment where three agents
message each other on day one, none of them verified, is not a team — it is an
outage with a roster. Users who start with one agent and a real workflow end up
with better teams than users who start with teams.

## Step 3 — Settle the settings, mostly without asking

Recommend a default for everything, name the two or three choices that actually
change the user's experience, and let them correct you. `references/agent-settings.md`
has the exact fields, accepted values and defaults — read it before you write a
config, and quote real values from it rather than remembering.

Decide *with* the user:

- **What it is called and what it is for.** `name` and `description`. Name it
  after the job (`SupportAgent`), not the technology.
- **How much brain.** Start at `default:turbo` for almost everything. Move to
  `default:pro` when the work is genuinely reasoning-heavy — analysis, planning,
  code, multi-step research — and only then set `reasoning_effort`, which does
  nothing on turbo. Explain the trade in time and cost, not in tier names.
- **What it may touch.** Access is read / read+write / read+write+execute. Ask
  what the agent needs to *produce*: reading and answering needs read, drafting
  files needs write, running scripts needs execute. Capabilities not granted are
  not merely discouraged — the tools are never given to it.
- **How much rope.** Mode is Supervised (asks before writes), Trusted (acts
  freely — the default), or Locked-down (strictest). Ask what the worst thing it
  could do is. An agent that emails customers gets Supervised until it has
  earned better; an agent that summarizes a folder gets Trusted.
- **Which tools and platforms.** From the finder, not from memory (see
  above). Credentials go to the vault or the connection store
  (`magic_link_connect_service`, `magic_link_request_secret`, `secret_set`),
  never into the config file.
- **Where it lives in the org.** Who it reports to, who it may talk to. Note the
  trap in the reference: an *empty* allow-list means unrestricted, not nobody.
- **Whether it thinks in the background.** Cortex (`cognition`) gives it ideas,
  brainstorming, self-improvement and the project workflow below. Self-learning
  (`memory.self_learning`) is on by default and distils its own sessions into
  memory. Both cost model calls; say so.

Do not walk the user through webhooks, budgets, pulse settings, wallets or rate
limits unless they ask or the job needs them. Defaults exist so that a first
agent is five decisions, not forty.

## Step 4 — Build it

Prefer an archetype over a blank agent — it arrives with a written prompt, a
tool list and a workspace already scaffolded:

- `olano_scaffold_archetype(archetype, display_name=…, title=…, model=…)` —
  install a ready-made expert or team. Pass the person's name for the agent
  as `display_name` ("Hillary") and leave `as_name` empty: the permanent
  handle is minted from the name (`hillary`, `hillary-2` if taken) — never ask
  anyone to invent a slug. The archetype's label becomes the agent's `title`
  (its position, shown under its name) unless you pass one, so "install the
  Recruitment Specialist as Hillary" yields Hillary, the Recruitment
  Specialist — and she introduces herself that way.
- `olano_create_agent(name, display_name=…, title=…)` — only when nothing fits.
  Here the handle is yours to pass; derive it from the display name the same
  way (lowercase, dashes).
- `olano_update_agent(...)` — every later change. A rename is `display_name`;
  a new position is `{"org": {"title": …}}`; the handle never changes.

For a team, remember each member can take its own model, and a member's prompt
is best extended rather than replaced: `system_prompt_extension` keeps the
member current with the catalog, while a full `system_prompt` override detaches
it from future pack updates.

Then wire the rest: channels (`magic_link_connect_channels`), skills
(`olano_install_skill`), toolkits (`olano_add_toolkit`), and the platform
connections below — each one starting from `olano_find_integration`.

### Connecting a platform over MCP

The order matters — getting it wrong stores a token for a server the agent never
loads. `references/agent-settings.md` has the exact signatures and the current
limits.

1. **Find it, then probe it.** `olano_search_mcp_directory(query)` gives you the
   verified endpoint, transport, auth mode and the credential names the server
   expects; `olano_test_mcp_server(agent, url)` then confirms the URL is real,
   whether it needs OAuth, and what tools it exposes. Do both before you promise
   the user anything.
2. **Write it into the agent's config.** `olano_add_mcp_server(agent, url, transport=…, auth=…)`.
   This must happen *before* authorization, not after: the OAuth flow stores
   tokens against the URL but does not add the server to `agent.json5`, so
   authorizing first leaves the user signed in to something their agent cannot
   see.
3. **Get the human to sign in**, on whichever surface they are already on:
   - **In chat (Telegram, WhatsApp, Slack…)** — `magic_link_connect_service(url, agent="<agent>")`
     mints a magic link plus a 6-digit PIN, good for 15 minutes. Send both. The
     user opens it, unlocks, signs in with the provider, and the agent's MCP
     client reloads on its own; they get a confirmation in the same chat.
   - **In the dashboard** — `magic_link_open_dashboard` and point them at the agent's
     **MCP** tab → **Authorize**, which shows a live log and the redirect URI
     they may need to register with the provider.
   - `olano_authorize_mcp_server(agent, url)` is an older path that returns the
     raw provider URL with no PIN and no expiry page. It works and it watches
     the flow to completion, but prefer `magic_link_connect_service` with a human.

**When the server has no OAuth**, it wants an API key. Use
`magic_link_request_secret(name="<KEY_NAME>", agent="<agent>", reason=…)` so the key goes
from the user's browser into the vault without passing through the chat, and
tell them plainly where to generate it on the provider's site — the directory
entry usually names the console page. Then point the server at the stored value
rather than the key itself, in the same `olano_add_mcp_server` call:

- remote (`http`/`sse`): `headers={"Authorization": "Bearer ${vault:KEY_NAME}"}`
- local (`stdio`): `env={"KEY_NAME": "${vault:KEY_NAME}"}`

Both resolve per agent at connect time, so two agents can use the same server
with their own keys. Never pass the key itself as a literal — that puts it in a
config file that gets read, copied and revisioned.

**Two things worth naming to the user rather than retrying.** OAuth and magic
links both need the deployment to be reachable from a browser (a configured
relay or public URL; a self-hosted install with an Olano API key gets Olano's
sign-in relay for MCP OAuth automatically). Neither degrades quietly: they
refuse with an operator-facing message. And on a managed deployment a stdio (`npx`/`uvx`)
server runs in an isolated, throwaway container that receives only the `env`
the entry declares — so declare every credential it needs as a `${vault:…}`
ref; the one shape that is refused there is an entry whose command is itself
`docker run …`, which has a remote preset to use instead.

**When nothing exists for the platform** — no toolkit, no MCP server, no
connector — building a small MCP server is the honest answer. The `mcp-builder`
skill covers doing it properly. Propose it as a piece of work, not as a detour
inside the current conversation.

These are mutating admin tools and most of them route through a human approval
gate — that is by design. State what you are about to change, make the change,
and confirm it landed.

## Step 5 — Prove it works before adding anything

**Restart, then verify.** A running agent does not hot-reload skills or config —
`olano_restart_agent(name)`, then `olano_diagnose_agent` / `olano_agent_status`,
and `olano_doctor` if something looks wrong.

**Test it yourself first.** `olano_test_agent(agent, user_message)` runs a
real turn of the agent on a separate test thread and returns its reply, every
tool it called with the results, and a `thread_id`. Send the kind of message
its users will send, read `tool_calls` (did it pick the right tool or skill,
with the right arguments, and did the call succeed?) and `output` (is the
answer right?). Pass the `thread_id` back to test a whole conversation. After
you change the agent, test again with `reload_config=true` so the test uses the
saved configuration before a restart; to compare prompt drafts before saving
one, pass `system_prompt`. Its tools run for real, so use safe inputs, and each
test is approved individually: say which agent and what message. Tell the user what you tested and what happened,
never what you expect to happen.

**Then test on Telegram.** It is the fastest honest feedback loop on the box:
the user chats with the agent from their phone, watches it work, and finds in
five minutes what a config review never surfaces. Get it right there before
putting the agent in front of customers on email, WhatsApp or the web.

Give the user one concrete thing to try — a real message, not "test it" — and
stay in the conversation while they do. What they report back is the actual
spec; fold it into the prompt with `magic_link_edit_instructions` or `olano_update_agent`
and iterate. Two rounds here beat six settings changed blind.

Only once it works: offer the next agent.

## What to reach for after the first agent works

Mention these when they become relevant, not as a feature tour:

- **Projects.** When a piece of work is bigger than a task, Cortex turns it into
  a project folder — `README.md` (purpose, goals, success criteria), `PLAN.md`
  (phases and checkboxes, so progress is a number), `STATUS.md` and
  `JOURNAL.md`. `cortex_add(kind="project", …)` starts one; `cortex_promote(item_id)`
  grows an idea into one. This requires Cortex to be enabled on the agent — it
  is the concrete reason to turn it on.
- **WhatsApp for customers, with templates.** An agent on a WhatsApp business
  connector, WhatsApp (Twilio Business API) or WhatsApp (Meta Cloud API), can
  answer anyone who writes, and can write first only with a template WhatsApp
  approved. Both connectors give it the same *WhatsApp Business* toolkit, so
  its instructions do not depend on which one the person picks: Twilio when
  the number lives in a Twilio account, Meta when it is in the business's own
  WhatsApp Business Account. With the connector it gets the everyday tools
  (`whatsapp_template_list`, `whatsapp_template_send`,
  `whatsapp_message_status`, `whatsapp_window_status`). The account
  administration tools of the same toolkit (create, submit, check and delete
  templates; the account's numbers, business profile, and on Twilio balance
  and usage) are off until an administrator switches them on
  for one agent, on the connector panel or the Tools tab; you cannot switch
  them on yourself, only ask an administrator signed in as one. Suggest a
  separate managing agent with no customer channel, and the Supervised
  security profile if the person wants to approve each send.
- **More humans.** A deployment can carry a team: invite colleagues from the
  dashboard (Config → users) and grant per-agent access. `magic_link_open_dashboard(section)`
  takes the user straight there.
- **Many end users, one agent.** Multi-user mode gives every customer their own
  memory, and optionally their own scheduled jobs and credentials. Different
  feature from inviting colleagues — keep the two apart when explaining.
- **Charging for it.** Monetization gates an agent's end users behind a
  subscription (Stripe or Telegram Stars), with the owner always exempt. The
  owner's own Stripe keys live in the vault.
- **Agent pages.** An agent can publish a small site — a dashboard, a report, a
  form. Pages are never public: a human sets the password, and until they do the
  page answers 503.
- **Agents talking to agents.** `talk_to_agent` is on by default. It becomes
  useful once two agents each do their job well, and dangerous before that.

## Capture what you learned

When a conversation produces something worth repeating — a workflow the user
described precisely, a prompt that finally landed, a setup you would do the same
way next time — write it down as a skill (`olano_create_skill`) or a subagent
rather than solving it fresh next quarter. Update `USER.md` and `IDENTITY.md`
with anything new about the business. That is what makes the second agent faster
to build than the first.

## Reference

- `references/agent-settings.md` — every setting this skill touches: exact field
  names, accepted values, real defaults, the traps that make a config fail to
  load, and the tool names for building. Read it before writing any config.
- `olano-admin` — operating the deployment after the agent exists: users and
  access, diagnostics, lifecycle, home config, disk and memory, updates.
- `olano-agent` — what every agent can do for itself (self-configuration,
  magic links, one finder and one ladder for connecting services); the agent
  you build gets that skill automatically.
