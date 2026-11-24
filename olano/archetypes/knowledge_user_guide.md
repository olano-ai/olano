# Olano User Guide

<!-- Generated from docs/user-guide/ by scripts/gen_user_guide.py. Do not edit
this file: edit the page under docs/user-guide/ and regenerate. The same pages
are published at https://docs.olano.ai. -->

This is the reference for people who run an Olano deployment, and for agents
that build other agents. It covers the dashboard, every field in the two
configuration files, every major feature, the Olano model tiers, and the
Olano Cloud service.

**How to use this guide.** It is long on purpose: it is meant to be searched,
not read front to back. Jump to the section you need from the table of contents.
Every configuration field lists its accepted values and its default, so you can
write a valid `agent.json5` from this document alone.

**If you are an agent reading this:** this file is your source of truth for
platform questions. Never guess a field name, a tool name, or a model string;
look it up here. When a value you write has to match something the runtime
already knows about (an agent name, a tool, an archetype, a model), confirm it
with the matching `olano_list_*` tool before writing it into a config.

---

## Table of contents

1. [What Olano is](#what-olano-is)
2. [Getting started](#getting-started)
3. [The dashboard](#the-dashboard)
    - [Overview](#overview)
    - [Insights](#insights)
    - [Chat](#chat)
    - [Agents](#agents)
    - [Cortex](#cortex)
    - [Scheduler](#scheduler)
    - [Issues](#issues)
    - [Revisions](#revisions)
    - [Calendar](#calendar)
    - [Files](#files)
    - [Memory](#memory)
    - [Notes](#notes)
    - [Knowledge](#knowledge)
    - [Tools](#tools)
    - [Providers](#providers)
    - [Connections](#connections)
    - [Vault (admin only)](#vault-admin-only)
    - [Agent to Agent](#agent-to-agent)
    - [Org Chart](#org-chart)
    - [Logs (admin only)](#logs-admin-only)
    - [Terminal](#terminal)
    - [Playground](#playground)
    - [Config (admin only)](#config-admin-only)
    - [Admin (admin only)](#admin-admin-only)
4. [`agent.json5` reference](#agentjson5-reference)
5. [`config.yaml` reference](#configyaml-reference)
6. [`organization.yaml` — the org chart](#organizationyaml-the-org-chart)
7. [Feature guides](#feature-guides)
    - [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart)
    - [Human in the loop (HITL) and approvals](#human-in-the-loop-hitl-and-approvals)
    - [Privacy and security](#privacy-and-security)
    - [Multi-user: two different features](#multi-user-two-different-features)
    - [Subscriptions and monetization](#subscriptions-and-monetization)
    - [MCP servers](#mcp-servers)
    - [Browser control](#browser-control)
    - [Webhooks](#webhooks)
    - [Pages: publishing sites and web apps](#pages-publishing-sites-and-web-apps)
    - [Scheduler and heartbeat](#scheduler-and-heartbeat)
    - [Memory and self-learning](#memory-and-self-learning)
    - [Notes](#notes)
    - [Issues](#issues)
    - [Revisions](#revisions)
    - [Channels and connectors](#channels-and-connectors)
    - [Cortex and Pulse](#cortex-and-pulse)
    - [The mobile app](#the-mobile-app)
    - [Budgets](#budgets)
    - [Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools)
    - [Beyond the deployment: A2A and MCP](#beyond-the-deployment-a2a-and-mcp)
8. [Models and credits](#models-and-credits)
9. [Olano Cloud](#olano-cloud)
10. [Building agents from a conversation](#building-agents-from-a-conversation)
11. [Common problems](#common-problems)

---

## What Olano is

Olano runs **agents**: long-lived AI workers that hold their own memory, reach
real systems through tools, and talk to people on the messaging platforms those
people already use.

An agent is four things:

| Piece | What it is |
|---|---|
| **Configuration** | `agent.json5` — the agent's name, model, prompt, tools, channels, schedule, permissions. |
| **Workspace** | A directory the agent owns: its memory, notes, files, skills, and scratch space. |
| **A model** | The reasoning engine. Either an Olano-managed tier billed in credits, or your own provider key. |
| **Surfaces** | How work reaches it: the dashboard, a messaging channel, a schedule, a webhook, or another agent. |

### The pieces

- **The runtime** is one long-running process. It boots every agent, keeps the
  channels connected, runs the scheduler, and serves the dashboard API.
- **The dashboard** is the web control panel — the primary way to operate a
  deployment. Everything in this guide that mentions a "tab" means a dashboard
  section.
- **The agents themselves.** Every agent carries built-in tools to configure
  itself, connect services, and send **magic links** so a person can finish a
  sign-in or hand over a key from their own phone; **AgentFather** carries the
  tools to manage the whole deployment. Asking an agent is a first-class way
  to operate Olano, not a shortcut — see [Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools).
- **The vault** is the encrypted credential store. Every API key, bot token and
  service password lives there. Credentials never belong in a config file.

How the pieces fit together:

```mermaid
flowchart TB
    subgraph surfaces["Where work comes from"]
        direction LR
        D["Dashboard"] ~~~ M["Messengers"] ~~~ S["Schedules"] ~~~ W["Webhooks<br/>and email"] ~~~ X["Other agents,<br/>outside platforms"]
    end
    subgraph runtime["The Olano runtime"]
        direction LR
        AG["Your agents<br/>each with its own workspace,<br/>memory and tools"] ~~~ AF["AgentFather<br/>manages the deployment"] ~~~ V[("Vault<br/>every credential")]
    end
    subgraph reach["What agents use"]
        direction LR
        LLM["Models<br/>Olano tiers or your keys"] ~~~ T["Files, web,<br/>a sandboxed shell"] ~~~ C["Connected services"] ~~~ MCP["MCP servers"]
    end
    surfaces --> runtime --> reach
```

### Where things live on disk

Olano keeps everything under one home directory, `~/.olano/` by default
(override with `OLANO_HOME`). An install made under an earlier default keeps
the `~/olano/` or `~/.lionclaw/` it already has; nothing is moved.

**About the name `lionclaw`.** Olano was first called lionclaw: it was built in
Singapore, the Lion City. The name survives in a few identifiers that are kept
so existing installs keep working: the `LIONCLAW_*` environment variables
(every one also accepts the `OLANO_*` spelling), the `lionclaw_docker`
backend, the `lionclaw_memory` default collection, and the older home and
state-folder names `~/.lionclaw/` and `.lionclaw/`. Wherever you see it, read
it as Olano.

```
~/.olano/
  config.yaml                       # deployment-wide settings  (config.yaml reference)
  organization.yaml                 # the org chart             (organization.yaml)
  agents/<name>/
    agent.json5                     # that agent's config       (agent.json5 reference)
    workspace/                      # that agent's own files
      AGENTS.md                     # operating instructions
      SOUL.md                       # persona, tone, boundaries
      IDENTITY.md                   # who the agent is
      USER.md                       # who it works for
      TOOLS.md                      # notes on tool usage
      MEMORY.md                     # durable learned facts
      memories/YYYY-MM-DD.md        # daily logs
      notes/                        # the markdown wiki
      knowledgebase/                # documents indexed for search
      skills/                       # reusable procedures
      reports/                      # every file the agent produces for you
      uploads/                      # files you send it in chat
      downloads/  scratch/  trash/  # conventions the tools use
      system/                       # Olano-owned; replaced on upgrade
  sessions/                         # conversation transcripts
  vault/                            # encrypted credentials
```

The files under `system/` are managed by Olano and overwritten when you upgrade.
Everything else is yours — edit it freely, from the dashboard Files tab or a
text editor. The agent reads `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`,
`MEMORY.md`, `TOOLS.md` and the recent daily logs at the start of every
conversation, so editing them changes how the agent behaves on its next reply.

### Self-hosted or Olano Cloud

Olano runs the same either way:

- **Self-hosted** — you install it on your own machine or server and bring your
  own model provider keys, or run on Olano credits with an Olano API key from
  `cloud.olano.ai`.
- **Olano Cloud** (`cloud.olano.ai`) — a managed instance you deploy in a few
  minutes, kept up to date for you, with Olano-managed models billed in credits
  so you never need a provider account. See [Olano Cloud](#olano-cloud).

You can mix: a cloud deployment can still use your own provider keys, and a
self-hosted install can use whatever providers you have alongside Olano
credits.

---

## Getting started

### Olano Cloud

Deploy an instance on `cloud.olano.ai`, open the dashboard address it gives
you, and the setup wizard below takes over. Nothing to install and nothing to
keep running yourself — see [Olano Cloud](#olano-cloud). Your sign-in is
created on the instance before it is reachable, so the first screen you see is
**Sign in**: use the email you signed up with and the temporary password from
your welcome email (also copyable from the deployment card in the cloud
console). The dashboard address carries no credential — nothing signs you in
by itself, and a **Create your account** screen on a fresh cloud instance
means its setup has not finished; wait a minute and reload rather than
creating an account.

While the instance still has that temporary password, the sign-in screen says
so itself: it greets you as a new instance and shows a **First sign-in** note
that the password is in the email we just sent you, with a link that opens
your deployment's **Security** tab in the cloud console, where the password
stays copyable. **Forgot password?** points to the same place. Once you have
set a password of your own (the setup wizard's profile panel, or a password
change), the note is gone for good and the screen is the ordinary sign-in
form. The note never shows your email or the password; it only says where to
find them.

### Self-hosted

The quickest install is the Docker Compose stack in the Olano repository
(`deploy/self-host/`): three containers from the public images, set up and
started by one line on a Linux server, a Mac with Docker Desktop, or Windows
with Docker Desktop and WSL 2:

```bash
curl -fsSL https://raw.githubusercontent.com/olano-ai/olano/main/install.sh | bash
```

The installer checks for Docker with the Compose plugin (on Linux it offers to
install it, and asks first), downloads the stack's files into `~/olano`, and
runs its setup script, which writes the generated settings into `.env`, pulls
the images and starts the stack. Options for the setup script go after
`bash -s --`, for example
`curl -fsSL …/install.sh | bash -s -- --host olano.example.com --sandbox`.
If you would rather download the files yourself, the README beside the compose
file lists them and the same script runs as `bash setup.sh`.

Everything the install keeps lives in a `data` directory next to the compose
file; backing that directory up backs up the install. Updating is
`docker compose pull && docker compose up -d`; the README covers pinning a
version, moving the data directory and the day-to-day commands.

#### The address

Unless you say otherwise, the setup script picks the address on its first run:

- **A server with a public IP address on its own network interface** (a
  typical VPS): it serves **https** on `<your-ip-with-dashes>.sslip.io`, for
  example `https://203-0-113-7.sslip.io`. sslip.io is a public service whose
  names point at the IP address written in them, which lets the dashboard get a
  Let's Encrypt certificate with no domain of your own.
- **Anything else** (a laptop, a home server, a cloud VM behind NAT such as
  AWS EC2): `http://localhost:3200`, reachable on that machine only.

Choose explicitly with an option:

| Option | Address |
|---|---|
| `--host olano.example.com` | https on your own domain; point its DNS record at the machine first. The reliable route for anything long-lived |
| `--host auto` | https on `<your-ip>.sslip.io`, also on a NAT'd cloud VM whose ports 80 and 443 are forwarded to it |
| `--local` | `http://localhost:3200` only |

For either https mode, ports **80** and **443** must be reachable from the
internet: in a cloud firewall or security group, open **22** (SSH), **80** and
**443**, and nothing else. The certificate is obtained on the first visit and
renewed on its own. sslip.io names share Let's Encrypt's rate limit with every
other sslip.io user, so issuance can occasionally be refused for a while; a
domain of your own avoids that. Whatever the mode, the plain-http port 3200 is
published on the machine's loopback only, never to the network, so from
another computer it is reached through an SSH tunnel
(`ssh -L 3200:localhost:3200 you@server`, then `http://localhost:3200`), which
also works while a certificate is still pending.

#### The setup code

A fresh install has no accounts, and the first person to create one becomes
its admin. So that this is you and not someone who found the address first,
the dashboard asks for a one-time **setup code** before it creates that first
account. It is three groups of four letters and digits, like `ABCD-EFGH-JKMN`,
and you will find it:

- in the setup script's output, printed next to the address;
- in the `.env` file beside the compose file, on the `OLANO_SETUP_CODE` line;
- in the runtime's log (`docker compose logs olano`), under
  **FIRST-RUN SETUP CODE**.

Case, spaces and dashes do not matter. A wrong code is refused with a message
saying where to look, and after ten wrong tries the form waits fifteen minutes
before accepting another. The code stops working the moment the first account
exists; from then on the dashboard asks for that account's email and password.
An instance on Olano Cloud never asks for it: its account is created before the
instance is reachable.

#### Sandbox mode

By default an agent's shell commands (and the Terminal) run inside the
runtime's own container. `--sandbox` gives every agent its own **sandbox
container** instead, the way an Olano Cloud instance runs them: a non-root
container per agent with only that agent's workspace in it and resource
limits, so one agent's commands cannot read another agent's files or the
runtime's. If an agent's sandbox cannot start, that agent has no shell rather
than falling back to the runtime container.

The cost is on the host: to start those containers the runtime is given the
host's Docker socket, and access to the Docker socket is equivalent to root on
the machine. Agents never get it, but a flaw in the runtime would reach the
whole host. Use sandbox mode on a machine you alone control. `--no-sandbox`
turns it off again; both are remembered across runs of the setup script.

Running from source (the Python package, with the dashboard on port **3200**
and the runtime API on **8765**, both configurable in `config.yaml`) is
described in the repository's README. Either way, from then on everything
happens in the dashboard and in conversation with your agents, exactly as on
the cloud.

Models are paid for either way you like: paste your own provider keys, or run
on Olano credits with an Olano API key from `cloud.olano.ai` (see
[Models and credits](#olano-credits-on-a-self-hosted-install)).
The wizard offers both.

### The setup wizard

The first time an admin opens the dashboard on a fresh deployment, a
full-screen wizard walks through four short panels, then drops you straight
into a conversation with Agent Father:

```mermaid
flowchart LR
    A["Sign in"] --> B["Setup wizard<br/>four short panels"] --> C["Chat with<br/>Agent Father"] --> D["Describe your<br/>first agent"]
```

<div class="grid" markdown>

</div>

1. **Welcome.**
2. **Your profile.** A deployed instance ships with one account: the email
   you signed up with and a temporary password from your welcome email. You
   signed in with that password a moment ago; this panel is where you create
   your own user on it — it keeps your email and asks for your name, a
   password of your own, and the passphrase that protects the vault.
   Your **handle** (the permanent `@name` agents and the issue tracker refer to
   you by, like `carlos-rivera`) is generated from your name and shown as you
   type; it is not editable, here or later, while the name always is. Saving
   replaces the temporary password everywhere — it stops working at once, and
   any other browser still using it is signed out — while **this** browser
   stays signed in as the user you just created: the rest of the tour, and
   Agent Father's chat at the end of it, run as you, not as the provisioned
   sign-in. On an instance with no accounts yet the same panel also asks for
   your email (and, on a self-hosted install, the
   [setup code](#the-setup-code)) and creates the admin account, and signs you
   in as it. On a
   managed cloud deployment this panel is required, because the instance was
   provisioned with temporary credentials that must be replaced; the vault
   half asks for the temporary passphrase only if the vault is locked, and
   then links to your deployment card where it is copyable.
3. **AI provider.** On a managed deployment your plan's Olano Credits already
   cover the AI, so this panel just shows what is included; bringing your own
   keys stays optional. Self-hosted installs choose here: paste an Olano API
   key to run on Olano credits (the panel then lists what is included and your
   balance), paste a provider key, or both.
4. **Meet Agent Father.** What Agent Father does for you — creating agents
   from a description, reconfiguring them, connecting messaging channels and
   services, inviting your team, keeping things running, and asking for your
   approval before it changes anything. The panel also tells you that Agent
   Father wakes up when you continue and greets you in the chat — usually
   within seconds, up to a minute on a brand-new instance — and offers
   starting points: things to try (create an agent from a description,
   change an agent's model, tools or channels, list the available providers
   and connectors) and things worth telling it about yourself (whether this
   is personal or business use, what your business or interests are, which
   systems and platforms you use, and which repetitive daily or weekly tasks
   you would like to hand to AI). Press **Meet Agent Father** and setup is
   over: the dashboard opens Agent Father's chat showing *Agent Father is
   waking up…* until its hello arrives, and it then asks what you would like
   to build first.

There is no separate step for a first agent, a messenger or web search: you
ask Agent Father for those in the chat, and web search is already available
on a managed deployment. Everything the wizard touches lives in the dashboard
afterwards (your profile in the account menu, Providers, Vault).

### Three ways to do everything

Every task in this guide can be done from one of three places, and they never
disagree:

| Surface | Good for |
|---|---|
| **The dashboard** | seeing everything at once; admin work; anything with a form |
| **Asking an agent** | connecting a service, a messenger or a key; changing the agent's model, instructions, subagents or skills — the agent uses its built-in `self_*` tools and sends a **magic link** when a browser is needed ([Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools)) |
| **Asking AgentFather** | anything about the deployment as a whole: new agents, teammates and access, diagnostics, settings, updates — with an approval card in front of every change ([Building agents from a conversation](#building-agents-from-a-conversation)) |

There is no command line to learn: an agent never asks you to open a terminal,
and nothing in this guide requires one.

Inside a chat session, slash commands control the conversation — `/model` to
switch models, `/new` to start a fresh thread, `/memory` to inspect what the
agent remembers, `/verbosity` to change how much it narrates. `/help` lists
them — the ones *that* agent exposes on *that* surface, plus any custom
commands you defined, so it never offers something it will then refuse.

---

## The dashboard

### Two interfaces: full dashboard and simple chat

Olano can show a person one of two entirely different screens after they sign
in, chosen per account:

- **Full dashboard** — the complete control panel described in the rest of this
  section: every agent, the scheduler, files, memory, tools, connectors,
  configuration and diagnostics. This is the default for **every** account,
  including all existing ones, so nothing changes for anyone unless you change
  it deliberately.
- **Simple chat** — a stripped agent-chat app. A list of the agents that person has
  been given, the conversation with the selected one, attachments, voice, and
  the agent's profile. No settings, no diagnostics, no token counts, no model
  pickers. Suited to staff who only need to talk to an agent and would find the
  full console noisy.

  People can also **create agents of their own** here: the **New agent** button in
  the agent list (and on the you-have-no-agents screen) builds one from a template
  or from a plain-words description. It asks for a **name** — a real name,
  like "Hillary" — and shows the permanent handle it will generate from it;
  a template's own label becomes the agent's **title** (shown under its name in
  the profile and chat header), never its name. A **Model** dropdown lists only
  the models this deployment can run right now — the deployment defaults, the
  Olano tiers and any provider connected with its own key — pre-selected to
  **Default · Turbo**; with a template picked, **Template's own model** keeps
  the one it was designed for. Admins always have the button;
  members have it while member bot creation is on, which it is by default —
  see the `self_serve_agents:` block in [`config.yaml` reference](#configyaml-reference) and
  **Config → Member bot creation**.

  By default the simple view shows replies only, with a single "working on it"
  line while the agent acts. A **detail button in the chat header** cycles three
  levels — Quiet, Normal (one compact line per tool the agent uses), Verbose
  (each tool's full input and result, the same rendering the full dashboard
  shows) — and typing `/quiet`, `/normal` or `/verbose` does the same. The
  choice is remembered per browser. Typing `/context` shows the same
  context-usage card the full dashboard's chat offers: how much of the agent's
  working memory this conversation and its setup are using.

  When the agent asks **another agent** for something, that "working on it" line is
  replaced by a more specific one — *"maria is thinking…"*, with that agent's own
  face beside it — and it clears the moment the answer comes back. It shows at
  every level, Quiet included, because it is about who is working rather than
  about tools. Without it the chat looks frozen for as long as the other agent
  takes, which can be a while. What that agent says comes back inside this agent's
  reply rather than as a message of its own.

  The agent list is built for real fleets. The selected agent's chats unfold under
  its row, each labelled with where it came from — **SCHEDULED**, **TELEGRAM**,
  **WHATSAPP**, **AGENT** and so on, the same badges the full dashboard's
  conversation list uses; an ordinary chat typed here carries no badge. A row of **filter
  chips** (All · Chats · Telegram · Scheduled · Agents · …) narrows the list to
  one source — the way to hide a chatty schedule's runs — and the choice is
  remembered per agent on that browser. The list under an agent shows only the most
  recent handful; **"Show all N chats"** opens the full list in the right-hand
  panel, with a search box, the same filter chips, and paging ("Show more") so
  an agent with hundreds of conversations stays fast. **Selecting an agent opens
  its latest conversation** — the newest one a person is in, typed here or
  arrived over a messenger, then the newest of any kind, and a fresh chat only
  when there is nothing at all (a fresh chat is not saved until you send
  something, so reopening an agent never piles up empty ones); switching agents
  and back keeps the chat you had open. A **chevron** at the end of every
  agent row folds or unfolds its chats in place: on the selected agent it is
  the fold, on any other it selects that agent and unfolds its chats without
  leaving the list. It shows on hover on a desktop (always on the selected
  row) and is always visible on a phone, where it is how you browse an agent's
  chats from the drawer — tap the chevron, then the chat you want; tapping the
  agent's *name* goes to its latest chat and closes the drawer. On a desktop,
  clicking the selected agent's row also folds its chats away (click again to
  unfold); **right-clicking any agent**
  opens a quick menu — new chat, see all chats, its profile, pin/unpin, move
  the row up or down, mark its unread as read; **right-clicking a chat** offers
  to rename, open or delete it. On a wide screen the whole list
  can be **resized by dragging its right edge** (double-click the edge to
  reset); the width is remembered per browser.

  **Arranging it.** Each of the three headings — **Pinned**, **All agents**,
  **Group chats** — folds away on a click and stays folded on that browser, so
  a long list can be cut down to the part you use. Agents can be **dragged up and
  down** to the order you want, and dragged across the divider between Pinned
  and All agents to pin or unpin them; that order is remembered per browser too,
  and an untouched list stays alphabetical. Dragging is off while the search box
  has something in it (the order it would write covers agents the filter is
  hiding) — use **Move up** / **Move down** on the right-click menu instead,
  which is also the way to do it from a phone or a keyboard.

  **Offline agents fold away.** Under **All agents**, the agents that can
  answer come first. Agents that are **paused** (someone pressed Stop) or
  **not running** sit behind one **N offline** row at the foot of the list,
  closed until you click it; the choice is remembered on this browser. Opened,
  those rows are drawn dimmed. A few agents never fold: pinned ones, the agent
  you have open, and any with unread replies. When nothing under All agents is
  running (while the deployment starts up, say), the rows are listed flat
  instead, and while the search box has text in it every match shows, folded
  or not. The dot on each face says where an agent stands: **green** answers
  right away, **amber** is starting, stopping or not available on this
  deployment, **red** failed to start, and **grey** is paused or not running.
  For amber and red the reason is written on the row itself, under the name.

  The button beside the **Agents** heading **collapses the list to a strip of
  faces**: names, previews, search and the open agent's chats go, the three
  headings stay, and clicking a face switches agents. Handy when a conversation
  wants the width; the same button brings the list back, at the width you had
  set.

  **Right-clicking the empty space** below the list offers the two things this
  rail can create — **New agent** and **New group** — which is the fast way to
  either from anywhere in the list. (Right-clicking a *row* still offers that
  row's own actions; see above.)

  A **notification bell** sits at the head of the list, the same one the full
  dashboard carries and fed by the same source: anything this deployment needs a
  person for — a channel that failed to start, a connector asking to be signed
  in, an action waiting on an approval — shows a count there and lists itself
  when you click it. **View all** opens the Overview panel below.

  **More.** Below the group section, under a divider, a collapsible **More**
  group holds five of the full dashboard's pages — **Overview**, **Insights**,
  **Issues**, **Cortex** and **Files** — and opens them in place of the
  conversation. They are the same pages, not cut-down versions: Overview is the
  deployment snapshot with its notifications and approvals, Insights is usage and
  spend, Issues is the tracker, Cortex is what the agent has been thinking about,
  and Files is its workspace. Picking an agent, one of its chats, a group, or
  **New chat** while a page is open returns to the conversation (on a phone the
  drawer closes on it, as it does over a chat), and the browser's Back button
  brings the page back. A page a deployment has switched off in **Config** is
  not listed here either.

  More starts **closed** on a browser that has never opened it, and after that it
  is whatever you left it — open it and it stays open across reloads, close it and
  it stays closed. Pages can also be **dragged out of More** into the space just
  above it, exactly as the full dashboard's own nav items can, so the one or two
  you actually use stay visible with More shut; drag them back in to undo it.
  Right-clicking a page offers the same move, which is how to do it from a phone
  or a keyboard. The arrangement is remembered per browser and is separate from
  the full dashboard's own — rearranging one does not disturb the other.

  The account menu at the bottom of the agent list holds theme and language —
  one dropdown each — and, on a team install, **Sign out**; for an admin it also
  has the **Simple view** switch, which flips back to the full dashboard. It
  opens whole with the list collapsed to a strip too, hanging off the strip
  rather than being cut to its width.

Set it per person in **Admin → Users**, in the **Interface** column. On a
single-user install (no team accounts) the equivalent switch is the `ui_mode`
key in `config.yaml` — see [`config.yaml` reference](#configyaml-reference).

**This is a display choice, not a permission.** Moving someone to Simple chat
does not restrict what they are allowed to do, and moving them to the full
dashboard does not grant them anything. What a person may actually see and do
is decided entirely by their **role** (admin or member) and by which agents
they have been given access to, and those rules are enforced by the server
whichever interface is on their screen. Use the Interface setting to reduce
clutter; use Role and per-agent access to control access.

### The full dashboard

The dashboard is the control panel for the whole deployment. The sidebar lists
every section; you can reorder it by dragging, and the search box (top left)
jumps to a section, an agent, a task, or an individual setting by name.

| Section | What it is for |
|---|---|
| [Overview](#overview) | Which agents are online, notifications, and the Approvals inbox. |
| [Insights](#insights) | Token and cost usage over time, per agent and per model. |
| [Chat](#chat) | Talk to any agent you have access to, one thread per conversation. |
| [Agents](#agents) | The roster: create, start, stop and edit agents. |
| [Cortex](#cortex) | The background work agents do between conversations. |
| [Scheduler](#scheduler) | Every scheduled job: what runs, when, and how it went. |
| [Issues](#issues) | A work tracker shared by agents and people. |
| [Revisions](#revisions) | Version history of configuration and markdown files. |
| [Calendar](#calendar) | Scheduled work laid out on a calendar. |
| [Files](#files) | Browse and edit agent workspaces. |
| [Memory](#memory) | What each agent remembers, and where you correct it. |
| [Notes](#notes) | Each agent's linked markdown wiki. |
| [Knowledge](#knowledge) | Documents indexed so agents can search them by meaning. |
| [Tools](#tools) | Every tool on the deployment, and why a locked one is locked. |
| [Providers](#providers) | Model providers and which of them are usable here. |
| [Connections](#connections) | Sign agents in to GitHub, Google, Microsoft 365 and more. |
| [Vault](#vault-admin-only) | Encrypted API keys, tokens and passwords. Admin only. |
| [Agent to Agent](#agent-to-agent) | Messages between agents, and calls from outside the deployment. |
| [Org Chart](#org-chart) | Who manages whom, which also decides who may message whom. |
| [Logs](#logs-admin-only) | Runtime logs by agent and severity. Admin only. |
| [Terminal](#terminal) | A shell in an agent's workspace, in the browser. |
| [Playground](#playground) | Try prompts against an agent without touching a real conversation. |
| [Config](#config-admin-only) | Deployment-wide settings. Admin only. |
| [Admin](#admin-admin-only) | Accounts, roles and per-agent access. Admin only. |

#### Addresses, reload and Back

Every page's address says where you are, so **reloading the page, copying the
link, or using the browser's Back and Forward buttons** lands you in the same
place. The address names:

- **The agent**, on pages that are about one agent: `/chat/<agent>`,
  `/memory/<agent>`, `/notes/<agent>`, `/knowledge/<agent>`,
  `/terminal/<agent>`, `/cortex/<agent>` and `/agents/<agent>`. On pages that
  cover every agent (Files, Connections, Scheduler, Logs, Overview) the agent is
  optional and only narrows the page: `/scheduler/<agent>` shows that agent's
  jobs, `/scheduler` shows everyone's.
- **The tab** inside a page: `/agents/<agent>/tools`, `/cortex/<agent>/ideas`,
  `/knowledge/<agent>/sources`, `/insights/drill`, `/calendar/week`.
- **What is open**: the conversation (`?thread=`), the file in Files
  (`?path=`), the issue (`?issue=`), the note (`?note=`), the Cortex item, the
  scheduled task (`?task=`), the Agent to Agent conversation (`?pair=`), the
  revision, the Org Chart node.

Moving somewhere (another conversation, another agent, another tab, opening an
item) is a step the Back button returns from. Filtering or searching is not:
Back never replays your typing. Opening a page from the sidebar still returns
you to the tab and item you were last on.

Older links keep working: an address in an earlier form (for example a
notification that says `?tab=mcp`, or a bookmark of `/chat?thread=…`) is
rewritten to the current one when it opens. A link naming an agent that no
longer exists, or that your account cannot see, opens the same page for your
current agent instead, with a notice.

#### Finding a setting by name

The search box — or **⌘K** / **Ctrl+K** from anywhere — also searches the
deployment's settings, not just the list of sections. Type what the setting is
called ("voice replies", "sandbox memory", "link lifetime", "deep model") and
the results list the matching controls under a **Settings** heading, each
showing the section it lives in (for example *Config › Speech*). Choosing one
opens that page and scrolls straight to the control, highlighting it for a
moment so you can see which of the page's many fields you were sent to.

Searching matches a setting's name, the section it sits in, common synonyms,
and its help text — so a word you remember from the description finds it even
when you don't recall the exact label. Several words narrow the results rather
than widening them: "telegram allowed" finds only settings matching both.

Settings you are not allowed to change never appear: the whole Config page is
admin-only, so a member account searching for "sandbox memory" gets no result
for it rather than a link that then refuses them.

The sidebar has three parts. At the top, the search box and, right under it,
the **New agent** button. Then **your agents**, the same list the simple chat
shell shows: a **Pinned** heading, any groups you have made, **All agents** and,
when group chats are on, **Group chats**. There is no separate agent dropdown:
clicking an agent here selects it everywhere (the pages that are about one
agent — Memory, Notes, Knowledge, Terminal — also carry their own picker in
their top bar). Hover an agent to reveal its **pin** button (always visible on
a touch screen): pinning keeps it under Pinned, on this device; click again to
unpin. Right-click an agent for the rest — new chat, all chats, its page, move
up or down, move to a group — and right-click **anywhere else on the sidebar**
(a heading, the empty space, even the bar's foot) to make a **new group** to
sort agents into. Below the agents, under a rule, a collapsible **More** group
holds every day-to-day section: Overview, Insights, Cortex and Issues lead it,
then the rest. Drag any tab to reorder More; that order is saved to your
account, so it follows you to any browser you sign in from.

The setup and administration pages are not tabs at all. They live in the
**account menu** — click your name at the bottom-left of the sidebar — in two
groups: **Setup** holds Providers, Connections and Tools; **Administration**
(admins only) holds Vault, Logs, Config and Admin. While one of those pages is
open, your avatar carries the highlight the tabs would otherwise show. Every one
of them is also a keystroke away in the ⌘K search.

The same menu holds **Theme** and **Language**, one dropdown each; both apply
the moment you pick and are remembered per browser. Thirteen themes. The two
house themes lead the list, and the other eleven follow dark-first:

| Theme | Page | Accent |
|---|---|---|
| **Olano Dark** (the default) | warm charcoal | Olano orange |
| **Olano Light** | neutral off-white | Olano orange |
| **Midnight** | deep navy | azure |
| **Mocha** | espresso brown | copper |
| **Cool Black** | pure black | green |
| **Volt** | black, dark-grey cards | electric chartreuse, with black type on buttons |
| **Black & White** | white | black — no colour but the states |
| **Harbor** | cool slate | deep teal |
| **Sepia** | warm parchment — nothing in it is pure white | umber |
| **Sakura** | blush pink | deep rose |
| **Pastel Green** | soft sage | emerald |
| **Singapore** | red blush, white cards | the flag's red |
| **High Contrast** | white | deep blue, with visible hairlines everywhere |

Cortex keeps its violet in every one of them. **High Contrast** is the
accessibility choice rather than a taste one: body text is black on white and
every status colour is pushed well past the usual readability floor. **Sepia**
is the one to pick for long reading at night — no surface in it is pure white,
so nothing on the page glares. **Volt** is the same theme the mobile app offers
under that name, so a phone and a browser can match. The sun/moon button beside
the bell at the top of the sidebar cycles through the same thirteen.

There is no separate *Chat* tab: **click an agent** to open the chat with it
(the full chat, with its mode, detail, model and reasoning controls). An
agent's row shows a pulsing dot while it is working on a reply and a count of
replies that landed while you were elsewhere. Type "chat" into the search box
if you want the chat page by name.

**Clicking an agent opens its latest conversation**, not an empty one: the most
recent chat a person is in — typed in the dashboard or arrived over a messenger
such as Telegram or WhatsApp — wins, then the most recent thread of any kind (a
scheduled run, an exchange with another agent), and only an agent with no
history at all starts a fresh chat. A fresh chat is not saved until you send
something, so opening an agent again and again never litters its list with
empty conversations. Switching agents and back keeps the conversation you had
open. The address names both the agent and the conversation
(`/chat/<agent>?thread=…`), so a reload, a copied link or the browser's Back
button reopens exactly that conversation with that agent.

**The selected agent unfolds into its recent chats** right under its name, the
way the simple view's rail does: a **New chat** row, a row of filter chips
(**All**, **Chats** for conversations typed in the dashboard, **Mobile** for
those had in the Olano mobile app, one chip per messaging app the agent talks
on, **Scheduled**, **Agents**), the newest few
conversations badged with where they came from, and **Show all N chats** —
which opens the chat page's full list with search and multi-select delete. The
chips only appear once the agent has more than one kind of chat, and the one
you pick is remembered per agent on this device (the simple view reads the same
choice). **Click a chat** to open it; **right-click it** for **Open**,
**Rename…** (a name of your own, in place — Enter saves, Escape cancels; leave
it empty to go back to the automatic title) and **Delete chat** (after a
confirmation; admins of the agent only). The trash can on a hovered row deletes
too. The **chevron** at the end of every agent row folds or unfolds its chats
without leaving the page you are on — on a phone it is always visible, and it
is how you browse an agent's chats from the drawer: tap the chevron, then the
chat you want. Tapping the agent's *name* opens its latest chat instead.

**Right-click an agent** for its menu: **New chat** (a fresh conversation),
**Show / Hide its chats** (the chevron's fold, for keyboards and long-presses),
**See all chats** (the chat page with its conversation list open), **About
this agent** (its page — profile, model, tools, channels), **Pin** / **Unpin**,
**Move up** / **Move down**, **Move to** a group, and **Mark N new as read**
when it has unread replies. Every item explains itself in its tooltip.

**Groups** are sections you make yourself to sort agents into — "Sales",
"Personal". Right-click the empty space in the agent list (or any heading) and
choose **New group…**, type a name and press Enter. Right-click a group's
heading to **Rename** it or **Delete** it; only an *empty* group can be
deleted — move its agents out first — and Pinned and All agents can never be.
An agent is only ever in one place: pinning it takes it out of its group, and
moving it into a group unpins it. Click any heading to fold that section
away. Pins, groups and their order are remembered **in this browser only**;
they change nothing about the agents themselves. With a row focused, the Up
and Down arrow keys move through the list and Home and End jump to its ends.

**Offline agents fold away** here too. Under **All agents**, the agents that
can answer come first, and the ones that are **paused** (someone pressed
**Stop** on the agent's page) or **not running** sit behind one **N offline**
row, closed until you click it and remembered on this browser. Opened, they are
drawn dimmed. Pinned agents, agents in your own groups, the agent you have open
and agents with unread replies always show, whatever their state, and when
nothing under All agents is running the rows are listed flat. The dot on each
face: **green** answers right away, **amber** is starting, stopping or not
available on this deployment (for example over its agent limit), **red** failed
to start, **grey** is paused or not running. Hover a row for what its dot means
and, for amber and red, why. In the collapsed strip the row shows just the
count.

Admins — and members, while **Config → Member bot creation** is on — also
get a **New agent** button under the search box (or **⌘N** / **Ctrl+N**). It
is the same short flow as the simple shell's: a name, what the agent should be
good at (a template, or from scratch), and which **model** it runs on. The
model dropdown lists only what this deployment can run right now — the
deployment defaults (Default · Turbo / Pro / Max), the Olano managed tiers
(billed in credits, no key needed) and any provider you have connected with
your own key — and comes pre-selected to **Default · Turbo**, the fast everyday
tier. With a template picked, **Template's own model** keeps the model the
template was designed for. The model can be changed later from the agent's
page.

Four sections are **admin-only** and never appear for member accounts: Vault,
Logs, Config, Admin. On a managed cloud deployment, other tabs can also be
hidden by your plan's entitlements — if a section you expect is missing and you
are an admin, that is why.

#### Installing it on a phone or tablet

There is also an **Olano app** for iPhone, iPad, Android and Apple silicon
Macs, from the App Store and Google Play: chats, Cortex and an inbox of
approvals and alerts, built for the phone, with notifications; see
[The mobile app](#the-mobile-app). Everything below is about
the browser version, which needs no store at all.

The dashboard can be added to a phone's home screen and then behaves like an
app: its own icon, full screen with no browser address bar, and the sign-in
kept between launches. There is nothing to download — it is the same dashboard
at the same address, and both interfaces (full dashboard and simple chat) work
this way.

- **iPhone and iPad.** Open the dashboard in Safari, tap the **Share** button in
  the toolbar, scroll down to **Add to Home Screen**, and tap **Add**. Safari is
  the only browser on iOS that can do this. A prompt at the bottom of the screen
  offers the same steps the first time you visit on a phone; dismissing it is
  remembered.
- **Android.** Chrome shows an **Install** button in the same prompt, and also
  offers *Install app* in its own menu. Once installed, Olano appears in the
  Android share sheet: sharing a link or a note from another app opens the chat
  with that text already in the message box, waiting for you to add to it and
  send. Nothing is ever sent automatically.
- **Desktop.** Chrome and Edge can install it too, from the icon at the right of
  the address bar. There is no prompt for this — a browser tab is a perfectly
  good home for the dashboard on a computer.

An installed copy keeps working well enough to read the last screen you were on
when the network drops, but it is not an offline app: agents run on the
deployment, so sending a message needs a connection. When a new version of the
dashboard has been deployed, a small **A new version of Olano is ready** bar
appears with a **Reload** button; the update is applied when you choose it, so
it can never interrupt a message you are part-way through writing.

Installing requires the dashboard to be served over **https**, which cloud
deployments always are. A self-hosted box reached over plain `http` (or by IP
address on a home network) cannot be installed — it still works normally in the
browser.

The installed app follows the phone's screen shape: on an iPhone it runs edge to
edge, filling the screen from the top to the bottom edge, with the header clear
of the status bar and the message box clear of the home indicator — no unused
band at either end. It measures the screen it is actually on, so the same is
true on every iPhone and Android size, on an iPad, and after rotating the
device. Both the full dashboard and simple chat behave this way.

**If a screen ever says "This view failed to load"**, press **Reload** on the
card. That clears the app's stored copy of the old version and fetches the
current one — it is the fix for a page that was left open across a deployment.
If it keeps happening on the same screen, close the app entirely and open it
again.

### Overview

The landing page and the recovery surface: it is the one section that always
renders, even when something else is broken. It shows which agents are online,
recent activity, disk health, and — most importantly — the **Approvals inbox**.

Four tiles head the page: **Agents**, **Channels**, **Scheduled tasks** and
**Status**. Click or tap the **Agents** tile to jump straight to the Agents
section. The **Channels** tile counts the connectors your agents are configured
with, and the line beneath it says how many are actually connected right now —
"2 active" when everything is up, or "1 of 2 active" when one is configured but
did not come online. That second reading usually means an expired or wrong
credential; open the agent's **Connectors** tab, where the connector reports what
it is waiting for.

Above the tiles, a usage strip summarizes the last 30 days. On an Olano-managed
deployment it leads with **Credits left** — your account balance, the same
number the deployment page at cloud.olano.ai shows — followed by **Credits used
(30d)**, which is what the window *consumed*, not what remains. A freshly
deployed box correctly reads "used: 0 credits" while the balance is full;
that zero means nothing has been spent yet, not that the account is empty. The
**Olano credits** card further down the page shows the same balance with the
monthly-allowance meter and a top-up link. A self-hosted install running on an
Olano API key ([Models and credits](#olano-credits-on-a-self-hosted-install))
shows the same strip and card for the account's balance, without the monthly
meter: account credits have no period.

**Notifications** is the section above Approvals: everything the deployment
needs you to look at — a connector that did not start because its credential is
missing, an issue assigned to you, a background run that failed, disk over
threshold. A row that names something opens it, so following the row is how you
act on it. Each row says **how long ago** it happened — "5 mins ago", "2 hours
ago", "3 days ago" — which is what separates a problem that is still live from
one that fixed itself overnight; on a desktop browser, hovering that age shows
the exact date and time. When the same problem keeps recurring it collapses into
a single row with a *seen N times* badge, and the age then tracks the **most
recent** occurrence, with the first one named in the same hover. Dismiss rows
one at a time, or use **Mark all read** and **Clear read**; a notification also
disappears on its own once its condition clears. The **bell** in the sidebar
(the topbar on a phone) carries the unread count and the newest few rows.

**Approvals** is a section on this page, not a separate tab. When an agent wants
to run something that needs human sign-off (see
[HITL](#human-in-the-loop-hitl-and-approvals)), the request appears here with
the exact action described, and it waits for you to approve or reject. The
sidebar shows a badge with the pending count.

**Disk** is a collapsible card on the same page, and it answers two different
questions. Expand it and the first block, **Where the space went**, accounts for
the *whole* machine in a few groups:

- **Container images** — the software this deployment runs on: the agent
  runtime, the sandbox agents execute their commands in, the dashboard, and the
  database. Several gigabytes on any install, and re-downloaded whenever the
  deployment updates. The "reclaimable" note next to it is what **Clean now**
  would recover (superseded copies left behind by past updates).
- **Container volumes and containers** — the database's stored data (threads,
  agent memory), TLS certificates, and the running containers' own working
  space and logs.
- **Olano home** — your side of the machine: agent workspaces, conversation
  transcripts, memory, skills, the credentials vault and the shared tool layer
  agents install packages into.
- **Swap file** — a file the machine uses as overflow memory. It takes its full
  size on disk whether or not it is being used.
- **System & other host files** — the operating system, system logs, package
  caches and the deployment's own checkout. Measured as what is left over,
  because the runtime is sandboxed and cannot read those directories directly.

On a typical managed deployment the Olano home is the *smallest* of these — a
box can be 60% full with only a few hundred megabytes of your own data on it.
That is expected, and this card is how you confirm it rather than guess.

The second block, **Largest directories in the Olano home**, then breaks down
just that home slice folder by folder; click any row to see one level deeper.
Both blocks refresh on their own (the home is walked once a day); **Scan now**
re-measures everything immediately.

### Insights

Charts and numbers: token and cost usage over time, per agent and per model,
thread counts, and activity trends. If your deployment displays costs in
credits rather than dollars, this section follows that setting.

The figures cover **everything your deployment runs, not just chat**: Cortex
background thinking, scheduled tasks, delegated skill workers, the daily
highlights and periodic reports, post-turn PULSE curation, subagent calls, and
the non-chat services (image generation and editing, vision, voice, web
search) all count. A **"Where credits went"** card breaks the window down by
kind of work — chat and channels, Cortex, scheduled tasks, and so on — which
is the place to look when usage seems higher than your conversations alone
explain, and a **"Credits by tier"** card splits managed model spend across
the Turbo / Pro / Max tiers so you can see which tier is doing the work.

Every number here is **measured on your own deployment**: each model call and
service call records its charge locally as it happens, so the charts, the
breakdown cards, and the headline credits figure all come from the same local
record and stay available even when the deployment is offline. That one record
also backs every range button, so the ranges nest the way you would expect —
**1 day** is a rolling last-24-hours view and is always contained in **7 days**,
which is contained in **30 days** and **90 days**. A shorter range can never
show more calls or more spend than a longer one; if it ever does, something is
wrong and it is worth reporting. The range buttons mean the same thing on the
**Drill-down** tab, which shares the selector — "1 day" is the last 24 hours
there too, scoped to the conversations active in that time.

Drill-down's agent ranking and its chat-vs-autonomous cards read the same
record as the Overview, so an agent's total is the same number on both tabs.
What changes is when you **open** an agent: from there down, the figures are
attributed from stored conversation history, the only record that ties a cost
to a particular conversation or tool. Long conversations are compacted as they
grow, which removes older calls from that history, so the conversations you can
open account for **part** of an agent's total rather than all of it. The page
says how much — "openable conversations account for $X of $Y" — so a short list
under a large total reads as coverage rather than as a contradiction. One more
difference follows from what it ranks: a conversation is counted whole, in the
range its most recent activity falls into, so a long-running thread brings its
entire cost with it. Take the Overview and the agent ranking as the totals, and
the levels below as the answer to *where inside a conversation* the cost went.

Billing itself
still lives with your Olano account — the **Credits left** balance shown on
the Overview page and the usage bar on your cloud deployment page track the
account's remaining balance for the billing period, which is a different
window than the charts' last-N-days view, so the two totals are not expected
to match digit for digit.

On deployments upgraded from an older version, the dashboard reconstructs
past Cortex background spend from its stored history the first time it starts,
so the charts show the full picture rather than beginning at the upgrade day.
Reconstructed history is marked internally and never re-billed — your balance
is unaffected.

The **Reply feedback** card, in the Fleet activity section, counts the thumbs up
and thumbs down people gave agents' replies in the chat during the selected
range, per agent (see [Chat → Rating a reply](#rating-a-reply)). A vote
counts once, as it stands now, in the range it was last changed in; a vote
taken back does not count. It reads the votes kept on this deployment, so it
works without Langfuse. Members see the counts for their own agents only.

When tracing to Langfuse is on, an admin with **Langfuse** links switched on
(Admin → Users) drilling into a conversation sees an
**Open in Langfuse** link beside its name, which opens all of that
conversation's traces in Langfuse.

### Chat

Talk to any agent you have access to. Pick the agent in the header, and switch
threads in the conversation list — each thread is a separate conversation with
its own memory of what was said. The header also exposes a per-conversation
model override and, for models that support it, a reasoning-effort control. Live
progress shows what the agent is doing as it works.

**The model chip belongs to the conversation you are in.** A new conversation
always opens on the agent's configured model; pick another in the chip and only
that conversation changes, from its next reply on. Every reply records the
model that wrote it, so when you reopen a conversation later, from any device,
it continues on the model it last answered with rather than on whatever another
chat was set to. You can switch as often as you like within one conversation
and each reply is billed to the model that produced it. If that model is no
longer available on the deployment, the conversation says so once and falls
back to the agent's default. To change the model for every new conversation,
change it on the agent itself.

**While the agent is still replying you have three choices for your next
message.** Press **Enter** and it **waits**: it is sent as your next message
the moment the reply finishes. Press **Steer** (the ▸▸ button, or
**Cmd/Ctrl+Enter**) and the agent gets it *now*, without stopping: it reads
your message at its next step — as soon as the tool it is running finishes,
before it decides what to do next — and adjusts the work in progress. Steering
is for "not that file", "use blue", "also include Q3": corrections you would
otherwise watch it finish a version without. If the reply finishes before the
agent reaches its next step, the message is simply sent as your next one, so
nothing is lost either way. Press **Stop** (■) to cancel the reply; whatever
was waiting stays waiting. Every waiting message shows as a chip above the
message box with its own **Steer**, **Edit** (take it back into the message
box) and **Cancel** buttons, and **Cancel all** clears them. A message the
agent read mid-reply is tagged **Steered in** in the conversation. Messages
with files attached wait for the reply; they cannot be steered.

**Attach any file with the paperclip**, or by pasting or dropping it onto the
conversation. Each file can be up to 10 MB. What happens next depends on what
the file *is*, judged from its contents rather than its name:

| You attach | The agent gets |
|---|---|
| A photo, screenshot or other image | The file, saved in `uploads/`; it looks at it with its vision tool |
| A voice note, audio or video | The file, saved in `uploads/`, plus a transcript when speech-to-text is on |
| A short text file of any kind (`.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.py`, `.sql`, a `Dockerfile`, a log) | The text itself, in your message |
| A long text file (over about 60 KB) | The file, saved in `uploads/`, so it can read every line rather than a cut-off copy |
| PDF, Word, Excel or PowerPoint, including older `.doc`, `.xls`, `.ppt` and OpenDocument files | The file, saved in `uploads/`; it reads it with its document reader, which converts older formats and reads **scanned** PDFs with OCR |
| A `.zip` or any other file | The file, saved in `uploads/`; an agent with a shell can unpack or inspect it |

Agents open these files only with the tool made for each kind. If an agent tries
to read a recording, a video, a document or an archive as a plain file, it is
told which tool to use instead (speech-to-text for audio and video, the
document reader for PDF and Office files, the shell for archives), so a voice
note never enters the conversation as raw audio data. Images in PNG, JPEG, GIF
or WebP it can still look at directly, up to about 3.7 MB each; larger ones go
through its vision tool.

Because saved files are the real files, the agent works on every row and formula
of a spreadsheet, not a summary. A saved file shows as a card in your message
with **Download** and **Open in Files**. Team members with read-only access
cannot save into the agent's workspace, so for them the text of a document or
text file is read once and added to the message instead, and other types arrive
as a note that the file could not be read.

**Add an agent** sits in the same header row, beside the MCP status, whenever
group chats are on and you have a second agent. It brings another agent into
this conversation by turning it into a *group chat*: the agent you were talking
to stays the one that answers anything that names nobody, the agents you add
reply when you @mention them, and — unless you switch **Bring the conversation
along** off in the dialog — the recent messages of this chat are copied in so
nobody starts blind. Your one-to-one chat is left exactly as it is; the group
is a separate conversation, listed under **Group chats** in the sidebar, and
it opens as soon as it is made.

**When the agent is using its live browser, you can watch it.** A small window
appears in the corner of the conversation with the agent's screen and a cursor
that moves to whatever it clicks. Click the window to open it large and take
control (to sign in, enter a code or finish a step yourself), hand back, and
collapse it again. When the agent waits for your approval (to submit a form,
buy or send), the window folds into a small **Approval needed** pill and the
approval card is scrolled into view, in every approval mode. On a phone the
window is always the pill; tap it to open the browser full screen. It appears
only when browser control is set to live; see
[Browser control](#watching-and-taking-control-live-mode).

**A conversation is live on every device, whichever surface the turn came from.**
Open the same thread on your laptop and your phone and both follow it as it
happens: a message that arrives over Telegram, a scheduled run, a turn sent
through the OpenAI-compatible connector, or something you type in another
browser shows up in the open conversation without a reload — the agent's
working indicator, its tool calls as they run, the reply forming, and the final
answer. The indicator names the surface the turn is running from ("Working on
this from Telegram…"). New conversations started elsewhere appear in the
conversation list the same way, and one touched elsewhere moves to the top. If
a device was asleep or offline, the thread catches up on its own when the tab
comes back.

**The agent keeps working when you put the device down.** Closing the tab,
locking your phone, switching apps or moving between Wi-Fi and mobile data does
not stop a reply in progress: the turn runs on the deployment, not in your
browser, and only the *view* of it goes away. Come back — the same tab, a new
tab, or a different device — and it rejoins the reply exactly where you stopped
seeing it, filling in the words, tool steps and files you missed, then carries
on live. A reply that finished while you were away is simply there when you
return, and a conversation the agent is still working on shows as busy in the
conversation list even on a device that never saw the turn start.

This is why **Stop** (■) is the way to end a reply: it tells the deployment to
stop, so it works from any device, and the agent stops whether or not the tab
that started the reply is still open. Closing the tab is not a stop.

Two consequences worth knowing. Sending again while a reply is still running
does not start a second one — a conversation runs one reply at a time, so the
message either steers the reply in progress or waits its turn, exactly as
described above. And "Connection lost while … was replying" now means what it
says: the reply is genuinely gone (the deployment restarted, or it ended long
enough ago to have been cleared), which is when **Retry** appears. While the
reply is merely catching up you see **Reconnecting** instead, with no Retry —
nothing was lost, so there is nothing to resend.

**Everyone who can chat with an agent shares its whole conversation list, and
can pick up any conversation in it.** A scheduled run, a heartbeat, a
conversation that arrived over Telegram or WhatsApp, a chat a colleague started
here — open it from the list and reply, and the agent continues with everything
that was said in it (ask "what did you just do?" under a scheduled run and it
answers about that run). Messages show who wrote them, so a thread several
people have typed into stays readable. Two things to know: a reply you type here
into a messaging-platform conversation is recorded and badged in that thread but
is not sent out to the platform, and deleting a conversation removes it for
everyone, so that is reserved for admins and the agent's owner — anyone with
access can still rename or compact it.

Three buttons in your theme's own colour frame the header row, laid out the
way the simple view's chat header is. A filled pair leads it: a **+** that
starts a new conversation and, right beside it, a **clock** that opens your
past ones. An **info** button (ⓘ) closes the row at the right: it opens the
agent's **profile card** — its face, name, title and description, an **Edit
profile** button for those allowed to change them, and (for admins) doors
into its settings, connections and keys — as a panel that slides in from the
right, exactly where the simple view keeps the same card. Only one of the two
panels, the conversation list or the profile card, is open at a time; pressing
either button closes the other.

**The conversation list is a panel that slides in from the right, not a column
that sits there.** The conversation itself always gets the full width of the
window; press the clock and your past conversations slide in over the right-hand
side. Pick one (or start a new one) and the panel closes again — so does
clicking the page beside it, pressing Escape, or the ✕ in its corner. It opens
closed every time you arrive, at every screen size, so the list is one click
away without ever costing you room to read. On a desktop you can drag its left
edge to make it wider or narrower, and that width is remembered; on a phone it
is sized to always leave a strip of the page beside it to tap on.

**Every conversation shows where it came from.** A thread that arrived over a
messaging platform carries a coloured badge with that platform's name beside its
title — TELEGRAM, WHATSAPP, SLACK, DISCORD, WECHAT and so on, one colour each —
and the person you are talking to on the line underneath. Conversations you
start here in the dashboard carry no badge: they are the default that everything
else is read against. The same badge appears in the header of the open
conversation.

Inside a conversation, a badge appears on an individual message only when that
message arrived by a **different** route than the thread itself. So a reply you
type here inside a Telegram conversation is marked, and the Telegram messages
around it are not — which is what makes a conversation that moved between
surfaces readable at a glance, without putting the same label on every line.

Conversations that predate this all still get labelled: the agent already
remembers every chat it has been messaged in, and the badge is recovered from
that. A conversation that only ever existed here stays unbadged, as it should.

One badge is not about a messaging platform: a violet **CORTEX** marks a
conversation you opened from the Cortex tab — pressing **Chat** on a Pulse card,
a self-check finding, or a Cortex item brings the discussion here, and the badge
is how you find it again among your other dashboard chats. It is an ordinary
conversation in every other respect: reply to it, rename it, keep it, delete it.

#### Rating a reply

Under each finished reply, beside **Copy**, are a thumbs up and a thumbs down.
They are there for everyone who can read the conversation, in the dashboard
chat and in the simple chat.

- **Thumbs up** records that the reply was good. **Thumbs down** records that
  it was not, and opens a small box for an optional comment on what went wrong
  (up to 2000 characters). The vote counts as soon as you press the thumb; the
  comment is extra, and **Skip** closes the box without one.
- The thumbs are the same plain colour as **Copy** until you vote. Your vote
  then turns green (up) or red (down) and stays lit, so you can see it later
  and after a reload. Press the
  other thumb to change it, or the lit one again to take it back.
- Each person has one vote per reply, and only you see your own. **Insights**
  shows the totals per agent.
- Votes are kept on this deployment, with or without Langfuse. When the
  agent's turns are traced to Langfuse (Config → Observability), the vote is
  also sent there as a `user_feedback` score on that reply's trace (1 for up,
  -1 for down; taking the vote back removes the score). The comment is sent
  only when traces include full content; with the default, metadata-only
  traces, it stays on the deployment. Votes cast while tracing is off are never
  sent later.
- Replies from before this feature have no thumbs.
- Deleting a conversation deletes its votes too.

#### Opening a reply in Langfuse

When tracing to Langfuse is on (Config → Observability) and the connection has
been tested, an admin whose account has **Langfuse** links switched on (Admin →
Users, off by default) sees a **Langfuse** link beside **Copy** under each traced
reply. It opens that reply's trace in Langfuse in a new tab: the turn with every
model call and tool call under it. Members never see the link, and a reply that
was not traced (tracing off, or not chosen by the sampling rate) has none. Deleting
a conversation also deletes its traces in Langfuse.

### Agents

The roster: every agent, online or offline, with its emoji, model, and status.
From here you create agents (from a ready-made archetype or from scratch),
start, stop and restart them, and open one for editing.

**Stopping is not deleting.** A stopped agent keeps its config, workspace,
memory and history, and stays offline across runtime restarts until you start it
again. Prefer stopping over deleting when you just want an agent out of the way.

Opening an agent gives you a detail page with these tabs:

| Tab | What you set there |
|---|---|
| **Overview** | Name, emoji, colour, avatar, description, model, system prompt, and the core behaviour switches. |
| **Subagents** | Specialists this agent can delegate to (see [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart)). |
| **Tools** | Which tools the agent may use, by category, with a description of each. |
| **Connectors** | Messaging channels — Telegram, Slack, WhatsApp, email and the rest (see [Channels and connectors](#channels-and-connectors)). |
| **MCP** | External MCP servers this agent connects to, with live connection health (see [MCP servers](#mcp-servers)). |
| **Browser** | The agent's web browser: what it has open, who is in control, and whether it has saved sign-ins. Watching the screen and **Take control** happen in the browser window in the agent's chat; **Open chat** goes there, and **Hand back to the agent** shows here while you hold control. **Close browser** and **Forget sign-ins** are here too. Below them, **When a site asks for a person** (wait for you, hold with no time limit, or skip; empty uses Config → Browser, see [Browser control](#when-a-site-asks-for-a-person)) a **Debug trace** for a check that keeps coming back (see [Browser control](#when-the-check-keeps-coming-back)), and **Actions that don't ask**: the narrow rules (a site, and send / submit / sign up on it) that let the agent act there without an approval card; admins and the agent's owners edit them, everyone else sees them read-only (see [Browser control](#actions-that-dont-ask)). |
| **Webhooks** | Inbound and outbound webhooks (see [Webhooks](#webhooks)). |
| **Commands** | Which slash commands are exposed, plus your own custom ones. What you enable here is what the messengers' slash menus offer; changes apply without a restart. |
| **Monetization** | Paid subscriptions for this agent's end users (see [Subscriptions and monetization](#subscriptions-and-monetization)). |
| **Multi-user** | Per-end-user isolation for a shared agent (see [Multi-user: two different features](#multi-user-two-different-features)), plus **Message policy** — message length caps, silence rules and a reply-length ceiling, which apply to any agent (see [`agent.json5` reference](#message_policy-message-length-caps-and-silence-rules)). |
| **Skills** | Reusable procedures the agent can follow. Every skill opens: see [Reading a skill](#reading-a-skill). |
| **Talk to Agent** | Who may message this agent and who it may message, plus its place in the org chart (see [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart)). |
| **Pages** | Pages this agent publishes at `/pages/<slug>/`: each page's kind (files, static build or server), its state, last error and logs, with Restart and Stop, plus the viewer password that guards all of them. See [Pages](#pages-publishing-sites-and-web-apps). |
| **Variables** | Per-agent environment values. |
| **Advanced** | Backend/sandbox, ACP, budgets, prompt mode, iteration limits. |
| **Config** | The raw `agent.json5`, when the raw editor is enabled. |

The Overview tab also carries an **Observability (Langfuse)** card: whether
this agent's turns are traced, what each trace contains and the share of turns
traced. Each choice can be **Follow Config**, which keeps the deployment's
setting from Config → Observability, or a value of this agent's own. The line
at the bottom says what is in effect right now, including where the keys come
from. Changes apply from the agent's next turn, with no restart. To send this
agent's traces to a different Langfuse project, an admin saves keys for it; the
card's settings and the Config card's keys are admin-only.

Most changes need a **Restart** of that agent to take effect. The UI tells you
when one is pending.

#### Reading a skill

The **Skills** tab lists every skill this agent can actually reach, and each row
carries two labels that tell you what you are looking at before you open it:

- **Where it lives.** *Common skill* — the shared set every agent on the box
  loads. *Agent skill* — this agent's own, in its workspace. *Learned skill* —
  one the agent wrote for itself. *Configured skill* — from an extra directory
  named in the agent's `skills` setting.
- **Whether you can change it here.** *Editable* or *Read-only*. Common skills
  are read-only from an agent's page on purpose: they are shared, so editing one
  from a single agent would quietly change what every other agent loads. Change
  those in the shared skills folder on the box, or ask AgentFather
  (`olano_install_skill` for every agent, `olano_create_skill` for a new one).

#### Switching a skill on or off

Every row also carries an **on/off switch**. It decides whether *this* agent
loads that skill — nothing more:

- **Off** means the skill is not described in the agent's prompt, cannot be
  delegated to, and is ignored by the agent's self-learning. The agent behaves
  as though it does not have it.
- The skill is **not deleted**. It stays on disk and stays in this list, marked
  *Off*, so you can switch it back on whenever you want.
- Turning off a **common skill affects only this agent**. Every other agent on
  the box keeps it. That is the whole reason the switch exists: common skills
  live in one shared folder, so before this the only way to take one away from a
  single agent was to delete it for everybody.
- All skills are **on** by default.

Changes save immediately and apply the next time the agent restarts — the page
reminds you when a restart is pending. Under the hood this writes the agent's
`disabled_skills` list (see [`agent.json5` reference](#agentjson5-reference)), so you can also set
it by hand in the raw config.

A row marked **Needs admin tools** has no switch. That skill declares the
tools it is written for (the AgentFather playbooks *olano-admin* and
*olano-agent-builder* need the `olano_*` tools) and this agent does not have
them, so it is never described in the agent's prompt whatever the switch
would say. Granting the tools — the agent's **Tools** tab, or `tools` in its
config — is what makes it appear; *Turn all on* leaves such rows alone.

An agent can work its own switches from a conversation too (*"stop using the
spreadsheet skill"*), if **Manage its own skills** is on in its
self-configuration settings — see
[Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools). It can list its skills and switch
them on and off for itself; it can never install, edit or delete one that way.

Click any row to open the skill. The viewer takes most of the screen, because
a `SKILL.md` is a document you are meant to read, not a summary to glance at.
You get it rendered — the actual instructions the agent follows — with the YAML
header folded away above it, and a list of everything bundled with the skill
beside it: scripts, forms, reference documents, example images. Click a file to
preview it; images render inline, and anything the browser cannot show offers a
link to open it raw. On a phone the file list becomes a short index above the
text.

Under the labels, every skill explains in a line where it lives and what that
means for changing it. Editable ones also get an **Open in Files** button that
jumps straight to the folder in the Files tab, which is where you edit it.

### Cortex

Olano's autonomous cognition engine. When Cortex is on, the agent does useful
background work between conversations: distilling what it learned into memory,
tidying its own notes, expanding ideas, and generating proactive suggestions.
This tab shows what it has been doing and what is scheduled. It is off until
you turn it on: the **Cortex engine** switch is inside **Cortex settings**
below, for the whole deployment or for one agent (see
[Cortex and Pulse](#cortex-and-pulse)).

Everything you can change about it sits in one card, **Cortex settings**, at the
top of the tab. It is folded away by default — the line beside its title is the
live state (on or off, the pace, and today's deep-run count), so you only open it
when you want to change something. Inside are three parts: **How it runs**, the
switches; **Phases & schedule**, what runs when; and **Creative direction**, what
the agent should aim at when it thinks on its own. Fleet-wide defaults live in
Config; per-agent overrides live here and in the agent's own settings
(see [Cortex and Pulse](#cortex-and-pulse)).

The **Brainstorming**, **Ideas**, **Prototypes** and **Projects** tabs are the
four stages of one funnel (see [the funnel](#the-funnel-brainstorms-ideas-prototypes-projects)).
A card that the **Rate & Promote** phase has reviewed carries its score as a
badge (`7/10`; hover for the recommendation and the reason), and the drawer
shows the reason and the suggested next step under the status line. **Create
project** on a card jumps the item straight to a project; the agent's own
`cortex_promote` moves it one stage at a time. A promoted card shows a link to
the folder it became. **Open** on a card (or clicking the card) shows the
item's full write-up in a drawer, and the drawer carries the card's own
buttons too: **Create project**, **Discard**, **Explore more** and **Chat**,
with the same confirmation and the same busy state, so you can act on an
item as you read it. Discarding from the drawer closes it; Create project
hands over to the run's panel. The **Phases & schedule** list has a row for Rate &
Promote like every other phase, with **Run now** to rate on demand.

**Every button on a card answers the click itself.** While its request is
out, the button you pressed turns into a spinner with what it is doing
(**Discarding…**, **Starting…**, **Approving…**) and the card waits dimmed, so
a second click cannot start a second run; the moment the server agrees the
card leaves the list (a discarded item to **Discarded**, an answered Pulse
card out of **Pending**) and a short notice confirms it. Approving a Pulse
card also puts an outcome card at the top of the tab that says exactly what
happened and links to the file it changed; the page scrolls to it. A Pulse
card about work the agent already applied on its own offers **Keep** and
**Undo** rather than Approve and Reject: Keep accepts the change, Undo
restores the backup the agent took first.

**Everything Cortex does in the background keeps running when you look away.**
**Create project**, **Think now**, **Optimize workspace**, **Optimize skills**
and a phase's **Run now** each start a run on the server and open a panel that
follows it: what it is doing right now, how long it has been going, and every
step it has taken (each tool it called, what came back, what the model said).
Closing that panel, leaving the page, locking your phone or reloading does not
stop the run. While a project is being created its card reads **Creating
project…** with the step it is on, and its button opens the same run again
rather than starting a second one; a phase that is running shows **Running**
in its row for the same reason. Pressing a button for something already
running (from another tab or another device) always joins that run. When it
ends you get a short notice, the card moves on (a promoted idea goes to
**Promoted**, the new project to **Projects**), and a run that failed within
the last hour leaves a **Last attempt failed** line on its card with a **View
log** link. **Stop** in the panel ends a run early.

**Runs**, next to **Refresh** at the top, lists every run for every agent you
can see: what is running now (the button shows a count while anything is) and
the history before it, including the phases Cortex runs on its own schedule
and the tasks you approved in Pulse. **All**, **Running** and **Failed**
narrow the list, the agents filter beside the tabs narrows it too, and
opening a row shows that run's full step log. A run that was going when the
service restarted shows as **Interrupted**. The most recent 200 runs per agent
are kept.

**Two things on this page have a different scope, on purpose.** The buttons
along the top — **Think now**, **Optimize workspace**, **Optimize skills**,
**Activity** — and the settings card act on **one agent**: the one named under
the title, which you change with the agent dropdown in the page's top bar (the
same dropdown Notes and Memory carry). The tabs below — **Pulse**,
**Brainstorming**, **Ideas**, **Prototypes**, **Projects**, **Learned
Skills**, **Self-Improve**, **Self-Checks** — show **every agent's items at
once**, so reviewing what the fleet has come up with no longer means switching
the dropdown once per agent. Every card carries a small label with the agent's
face and name it belongs to, and the buttons on that card act on *that* agent
(a **Chat** switches you to it). The **agents filter** beside the tabs — a
dropdown with a checkbox per agent, a search box, **Select all** and
**Clear** — narrows the tabs to the agents you tick; the line next to it says
what is showing ("Showing items from all agents", "from 2 of 8 agents"). Every
agent is shown by default, an agent created later is shown too, and the choice
is remembered on this device. The counts on the tabs follow the filter.

### Scheduler

Every scheduled job across the deployment: what runs, when it runs next, and
what happened last time. Create, edit, pause and delete jobs, and trigger one
immediately to test it. Jobs are defined per agent (see [Scheduler and heartbeat](#scheduler-and-heartbeat)).

Each job has a **Model** field and, for models that advertise effort levels,
a **Reasoning effort** field. "Agent default" (the first choice, and what an
untouched job uses) follows whatever the agent is configured with; picking a
specific model pins that one job to it without changing the agent, which is
how the same agent can run a cheap nightly digest and a bigger weekly analysis.
The card shows the job's model and effort, each run is billed to the model that
ran, and a model whose provider is not connected is refused when you save.

#### The task form

Besides the name, the agent, the prompt and the schedule, a task carries two
optional overrides. Both default to the agent's own settings, so a task that
leaves them alone behaves exactly as the agent would in chat.

| Field | What it does |
|---|---|
| **Model** | The model this task runs on. *Agent default* follows whatever model the agent is configured with. Pick one to run only this task on it. The list holds the models this deployment can actually use. Changing the model changes which effort levels are offered below. |
| **Reasoning effort** | How much thinking the model does before answering on this task's runs. Shown only when the model the task runs on advertises effort levels, and every option is a level that model accepts, so a task can never be saved with an effort its model rejects. *Agent default* uses the agent's own setting. Higher levels cost more tokens and time per run. |

The same two fields appear on the agent page's **Schedule** card for jobs
defined in the agent's configuration, and an agent creating a task for itself
can set them through its scheduler tools.

### Issues

A shared work tracker where **agents and humans are both participants**. An
issue has a title, a body, a type, comments, an assignee and a status. You can
assign an issue to an agent and it will pick it up on its next poll and work on
it; agents can open issues for each other or for you. Search is hybrid — plain
keyword matching plus meaning-based matching — so "the customer can't log in"
finds an issue titled "authentication failure". See [Issues](#issues).

The list's filters — search text, Open/Closed, type, assignee, label and sort —
live in the page's address, so the URL in your browser always reopens the same
list and can be sent to a teammate; **Copy link** (next to **Clear filters**,
shown while any filter is active) puts it on the clipboard for you. Opening an
issue keeps the filters, and coming back to Issues from another tab restores
the last filters you used.

Inside an issue, **Copy issue** (top right, across from **Back to list**) puts
the whole thread on the clipboard as Markdown: the title and number, a line with
its state, type, author, assignee and labels, the description, then every
comment with who wrote it and when (times in UTC). Assignment, label and
close/reopen events are left out. To copy a single card instead, use the copy
icon next to its time: it copies that description or comment exactly as it was
written, so lists and code blocks paste intact, and turns into a check mark for
a moment once copied.

### Revisions

Automatic version history for your deployment's configuration and markdown
files. Every meaningful change is committed, attributed to the agent or human
who made it, and browsable here — so you can see what an agent changed in
`MEMORY.md` last Tuesday, and what a config looked like before an edit. See
[Revisions](#revisions).

- **History**: the list of commits, newest first, ten to a page. The arrows
  above and below the list go to the newest page, the previous page, the next
  page and the oldest page, and the label says which page you are on and how
  many there are. Type a path in the filter box to see only the commits that
  touched one file (the list starts again on its newest page), and **View** to
  read that file as it was at a commit. A **merge** badge marks a commit that joined GitHub's
  changes with this deployment's, and **not on GitHub** marks commits a push has
  not sent yet.
- **Select a commit** to open its details: the full commit id, author, date and
  message, a summary (files changed, lines added and removed, how many files
  were added, modified and deleted), and the list of files. Select a file to see
  its diff, **side by side** (the default on a computer) or **unified** (the
  default on a phone); your choice is remembered. **Whole file** shows every
  line around the changes, and **Open file at this commit** shows the file as it
  was.
- **GitHub sync** (admins only): connect a private GitHub repository, then
  **Push**, **Pull** and **Check for updates**, choose **Automatic push**, and
  resolve a pull whose changes collide with this deployment's. The card shows
  the repository, its branch, how the deployment signs in and the automatic
  push mode; a status line (up to date, commits to push, commits to pull, or
  GitHub has moved on); and when the last push, pull and check happened. While
  a connection, push or pull runs, the card shows each step as it happens, a
  progress bar, and while the history moves over the network how many objects
  and megabytes have been sent or received and at what speed. Everyone viewing
  the page sees it, including who started it.
- **Held back**: a warning lists any tracked file whose current version is not
  being recorded (too large, looks like it holds a credential, or an agent
  configuration that does not parse), with the reason.
- Documents, images, audio and video show as **binary** in a commit, with
  **Download** buttons for the version before and after; opening one at a
  commit offers a download instead of a text view.
- **Snapshot now** records the current state immediately instead of waiting for
  the next automatic commit.

### Calendar

A time-based view of scheduled work and events — the schedule laid out on a
calendar rather than as a list.

### Files

A file browser for agent workspaces. Read, edit, download and delete files,
including the identity and memory markdown files that shape the agent's
behaviour. Deleting through the agent's own tools moves files to a recoverable
`trash/` folder rather than destroying them.

The tree lists **every agent you have access to**, each as a collapsed row
with a `workspace` folder under it, so you browse the whole team from one
place without switching the agent picked elsewhere in the dashboard. Nothing
is loaded until you open it: expand an agent, then its workspace, then the
folders you want — each one lists on demand. Right-click an agent or its
workspace row to create a file or folder there or refresh it; the toolbar's
**New file / New folder** buttons land wherever the row you last clicked lives.
The footer names the agent whose files you are looking at. Links from other
tabs (a Cortex item's files, a skill's folder) open straight to the file under
its agent.

**Upload files from your computer or phone.** Tap or click the folder you want
the files in (or a file, to use its folder; or the agent or `workspace` row,
for the workspace root), then press **Upload** in the toolbar, the arrow next
to New folder, and pick one or more files. Right-clicking a folder, an agent or
its workspace row offers **Upload files…** too. Each file can be up to 100 MB;
larger ones are skipped with a message naming them. A line under the toolbar
shows progress, and when the upload finishes the folder opens in the tree (a
single file opens in the preview). If a file of the same name is already
there, you are asked whether to replace it; **Keep existing** skips just that
file. Files keep the names you gave them, and the agent can use them straight
away: tell it in chat where you put them. Members with read-only access do not
see the Upload button.

**Files an agent produces.** Every agent keeps one layout: `reports/` holds
everything it makes for you (charts, images, PDFs, spreadsheets, documents,
exports — one canonical file per deliverable, revised in place), `scratch/`
holds its scripts and intermediate files, and `uploads/` holds what you sent
it in chat or on a messenger. When an agent names a file in its reply — its own `Saved:` line,
a path in backticks, or a full path — the chat shows it: images, audio and
video render inline, and every other file appears as a card with **Download**
and **Open in Files**, which jumps to that file in this tab. The same works
when you reopen an old conversation. On a messaging channel the agent's
`Saved:` line attaches the file to the chat instead. Members see and download
the files of the agents they have access to; editing and deleting stay with
admins.

Only the agent's **finished answer** attaches files, and only the files it
names there. A path that came up while it was working — a file it opened to
read, a scratch file, a path quoted out of some document — is not attached, so
you get the deliverables and not everything the agent touched on the way. If it
names the same file more than once in one answer, you still get one card. Agents
are told to list every file they made or changed for you, with its full path, so
if one leaves something out, say so — and if you would rather it not attach a
particular file, tell it that too.

**What an agent can do with documents.** Every box now carries the tools an
office document actually needs, so an agent does not have to explain that it
cannot: it can turn a Word file, a spreadsheet or a slide deck into a PDF (and
open a legacy `.doc`), read a **scanned** PDF or photographed page with OCR,
merge, split, rotate and unlock PDFs, and check its own work by looking at the
pages it produced. Two limits worth knowing: OCR reads **English** — for another
language the agent reads the page with its vision instead, which it will say —
and a converted document uses substitute fonts for any typeface the box does not
have, so the common ones (Arial, Times New Roman, Courier New) keep their layout
and an unusual one may shift. Ask for the result you want ("turn this into a
PDF", "what does this scan say") rather than naming a tool.

If an agent runs on your own machine rather than a hosted box, whoever installed
Olano could have declined these packages — the agent checks before it relies on
one, and tells you what it did instead.

On a hosted box every agent also has Olano's four document skills, **docx**,
**xlsx**, **pptx** and **pdf**: step-by-step know-how for building, editing and
checking Word files, spreadsheets, slide decks and PDFs. They arrive with the
box's shared skills and update on their own; you see them on the agent's
**Skills** tab as common skills. A self-hosted installation does not include
those four skills. Its agents can still work with documents using the tools
above, they just do it without that extra guidance.

The tree is fully keyboard-driven: ↑/↓ move, → opens a folder or steps into
it, ← closes one or steps back to its parent, Home/End jump, Enter opens,
**F2** renames, ⌘⌫ / Ctrl+⌫ deletes, and typing a few letters jumps to the next
row that starts with them. Very large folders show their first 2,000 entries
and say how many more there are — use the filter box to narrow. A very large
text file previews its beginning, says so, and offers the download for the
rest; editing is switched off for it so a save can never lose the part you
were not shown.

The **Filter** box searches by name across **every workspace you can access**,
not just the folders you have opened: type `memor` and each agent's
`MEMORY.md` appears under its agent row even with everything collapsed. Matching
is case-insensitive and looks at the file or folder name and at its path, so
`memories/2026` finds a daily log by its folder. It starts from two characters,
waits for you to pause typing, and searches everything at once — press Enter to
search without the pause, Escape to clear. Version-control, dependency and cache
folders are skipped, and dot-files only take part while the eye button shows
them. Click a match to open it and reveal it in the tree; when there are more
matches than it shows, it says so — keep typing to narrow.

Markdown and HTML files open in a rendered preview, with a **Preview / Raw**
toggle in the preview header to switch to the source. An HTML page renders
live in an isolated frame: stylesheets, scripts and images it references by
relative path are pulled from the same workspace folder, so a small prototype
site looks the way a browser would show it. The frame is sandboxed — the page
runs cut off from your dashboard session and cannot read or change anything
outside its own preview.

Images, audio, video and PDFs play or display inline. A file that is none of
those and not text either (a `.zip`, an Office document, a database, a program)
is never printed as text: the preview shows its type and size with a
**Download** button, so you can open it in an app that understands it.

On a phone the tree and the preview are not side by side — whichever you are
using gets the whole screen. The tree is what you browse; opening a file hands
the screen to the preview, and the **All files** button at the left of its
header hands it back. The **Preview / Raw** toggle and the file actions sit on
their own row under the file's name, so the name stays readable however long it
is.

Files whose name starts with a dot — `.env`, `.olano/`, `.git/` — are left
out of the tree by default, so a workspace opens on the files you work in. The
**eye button** in the tree toolbar shows them; click it again to hide them. The
button lights up while they are showing, and your choice is remembered the next
time you open the dashboard. It only changes what the tree *lists*: a hidden
file you open, edit or delete behaves like any other file, and a link that
points at one turns the toggle on for you.

### Memory

What each agent remembers: `MEMORY.md` (durable facts) and the daily logs.
Edit them directly — this is the fastest way to correct something an agent has
learned wrongly. See [Memory and self-learning](#memory-and-self-learning).

The page names the agent whose memory it shows in its own top bar, with an
agent picker — it is the same selection the sidebar's agent list drives, so
picking an agent here or clicking one in the sidebar changes both. (On a phone
the sidebar is a drawer, which is why the page carries a picker of its own.)

### Notes

The agent's markdown wiki: linked notes with backlinks, searchable, editable by
both you and the agent. Good for structured knowledge that is neither a fleeting
memory nor a document — project pages, contact sheets, running decisions.
See [Notes](#notes). Like Memory, the page carries the agent picker in its
top bar.

### Knowledge

The searchable document library (RAG). Add files, folders or URLs and they are
indexed so agents can search them by meaning. Documents you drop into an agent's
`knowledgebase/` folder are picked up automatically. Use this for manuals,
policies, contracts and reference material you want agents to cite.

The page has no store of its own: it runs every search, listing and ingest
through the knowledge-base tools of **the selected agent** — the agent picker in
the page's top bar, the same selection the sidebar's agent list drives, so the
agent named here is always the one named everywhere else. Those tools are not granted
automatically, so an agent has them only when its `tools` list names them (see
[`agent.json5` reference](#tools)). When the selected agent is missing them the page says so,
names exactly which are absent, and disables the controls it cannot serve —
pick another agent, or add the tools to that one and restart it. A knowledge
call needs a running agent: if the selected one is stopped, the page says so
and answers through another agent that is running until you start it.

### Tools

Opened from the **account menu** (bottom-left) under **Setup**, or from ⌘K.

Browse every tool available on the deployment, grouped by category, each with a
description of what it does. Tools that exist but are switched off show as
locked with the reason — for example a tool that needs a credential you have not
set yet — so you can find out a capability exists instead of it being invisible.

You do not have to come here to give an agent a toolkit. Ask the agent —
*"connect yourself to GitHub"* — and it adds the toolkit to itself
(`self_add_toolkit`), tells you which sign-in it needs and sends the link for
it, then restarts when you say so; `self_remove_toolkit` takes one away again.
AgentFather does the same for any agent (`olano_add_toolkit`). A toolkit added
this way is the same as one you tick on the agent's **Tools** tab: it applies
after the agent restarts.

**Fleet-wide tools.** A few tools are badged **fleet-wide**, with a note under
the row explaining what that means. They act across the *whole deployment*
rather than only for the agent holding them: reading the shared runtime log,
another agent's chat transcripts or workspace, the credentials vault, or
running a command as a different agent. Anyone who can chat with an agent can
ask it to use the tools it holds — so a fleet-wide tool belongs only on an
operations agent you trust with the entire deployment. AgentFather ships with
them; nothing else gets them unless you tick them yourself.

Two rules back the badge up. Agents cannot hand these to each other or to
themselves — a bot asked to "give yourself the log reader" is refused, and so
is AgentFather doing it on a scheduled run or on another agent's behalf;
adding one is a decision an administrator makes here, in the dashboard. And
holding the tool is not enough to use it: these tools answer only when an
administrator is the person on the turn, so an ordinary member chatting with
an agent that holds them still cannot read another agent's logs or
transcripts. Ordinary tools are unaffected — the badge is the whole list.

### Providers

Opened from the **account menu** (bottom-left) under **Setup**, or from ⌘K.

Model providers and their models. Shows which providers are genuinely usable
here (a key in the vault, a live sign-in session, or a local model server), and
lets you set the deployment's preferred model. Olano-managed tiers appear at the
top (see [Models and credits](#models-and-credits)).

A provider's model chips turn **green** once everything it needs is in place —
the package installed *and* its key present or its sign-in live — so you can see
at a glance which models you can actually run. Chips stay neutral while a
requirement is missing, while a connected provider is reporting that it is not
currently working, and on models the provider has deprecated (shown struck
through). Click any chip to copy its `provider:model` identifier.

#### The Olano card on a self-hosted install

On a self-hosted install the **Olano** card carries a **Run on Olano credits**
form. Paste an Olano API key created under **API keys** on `cloud.olano.ai`
(it starts with `sk-olano-`); the dashboard checks it with your account, stores
it in the vault and hands it to every running model. The card then shows the
key's prefix, **Buy credits** and **Manage keys** links to your account, and
**Replace key** / **Remove**. Removing it returns the install to your own
provider keys at the agents' next start. On an Olano Cloud deployment the card
has no form: the deployment bills its own credits.

If the card says the key was **refused**, it was revoked on `cloud.olano.ai` or
pasted incompletely; paste a valid one. If it says **stored but not in use**,
another key outranks it on this box (a gateway key set in the environment, or
an enrolled deployment key): the install keeps that key and the pasted one
waits in the vault until the other is removed. A key that comes from the
environment variable `OLANO_CREDITS_KEY` cannot be replaced or removed from
the card; change it where it is set and restart the runtime.

Connecting a key on an account with **no credits yet** is fine: the card and
the setup wizard say so, agents on the Olano tiers answer with an out-of-credits
message until you buy some on the Billing page, and nothing else changes.

If the box's environment sets a gateway
key directly, the card says so and that key wins over a pasted one.

### Connections

Opened from the **account menu** (bottom-left) under **Setup**, or from ⌘K.

One card per service an agent can be signed in to — GitHub, Google, Microsoft
365, Salesforce, HubSpot, QuickBooks, Shopify, Slack, Notion, LinkedIn, X, Meta, Reddit,
DocuSign and more. Each card shows whether it is connected, which sign-in
methods it offers, and — when a connection has expired — a way to renew it.
A connection made here is *the* credential for that service: the service's
toolkit, any MCP server for it and the git tools all use it, with nothing else
to configure and no restart.

Each card also lists the tools that service brings. They turn **green** once the
service is connected — those tools are live for your agents; while it is not
connected they stay neutral, as a preview of what connecting would grant.

There are four ways to connect, and a card shows the ones its service offers:

- **Connect with Olano's app** (shown when available): one click, nothing to
  configure. You approve access on the service's own consent screen — no
  developer account, no OAuth client, no redirect URI to register. The card
  shows *via Olano's app* on connections made this way, and tokens are stored
  only on your deployment. Which services offer this arrives via background
  sync: opening this page checks for updates within seconds, and Config →
  Cloud catalogs → **Sync now** forces an immediate refresh. This option is
  for Olano Cloud deployments: an Olano API key on a self-hosted install does
  not unlock it, so a self-hosted install uses its own OAuth app instead.
- **Use your own OAuth app**: the classic **Connect** form — create an OAuth
  client in the service's developer console, register the shown redirect URI,
  and paste the Client ID and Secret. Your own client always takes precedence
  when both are configured.
- **Sign in with a code**: for services that offer it (GitHub does), no app to
  create and no fields to fill. The panel shows a short one-time code and a
  button to the service's device page; enter the code there on any device — a
  phone is fine — approve, and the card turns Connected on its own. This is
  the route to use when you have no OAuth app and do not want to create one.
- **Paste a key**: store the service's API key or personal access token in the
  **Vault** under the name the service's toolkit reads. The reference list is
  on each card's setup guide. A key still works for every service — it is
  simply the last resort, since it never expires on its own and has to be
  rotated by hand.

**Scopes.** Every card works at two scopes: **System** (one connection every
agent uses) and **Per agent** (that agent's own account, optionally shared
with the others as a fallback). Inside a tool call the agent's own connection
wins, then a shared one, then the system one — which is how three agents can
post from three accounts of the same service. The **Vault** page (account menu
→ Administration) remains the place to see and manage stored keys.

An agent can hand you any of these from a chat: ask it to connect a service
and it sends a magic link ([Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools))
for the best method the card offers — Olano's app if there is one, otherwise
your own app, otherwise a code, and a secure form for a key. Nothing is ever
pasted into the conversation.

**When a connection stops working.** Any service on this page, Google
included, can refuse a saved sign-in while its card still says *Connected*: an
admin revoked the session, the grant died with a password change, or the token
outlived what was stored. The card cannot see that; the agent's tools can,
because they are the ones handed the `401 Unauthorized`. The first refusal is
repaired silently when it can be (the token is refreshed and the call retried
once), whichever service and whichever tool. When that fails, the card turns
**Needs reconnecting** with the service's own words under the badge, its
button reads **Reconnect**, and the agent does not stop at the error: it tells
you the sign-in has expired, offers the routes this deployment has (a
reconnect link, a sign-in code or a pasted personal token where the service
allows one, the card's Disconnect then Connect as the manual route), suggests
what else can reach the same service if you cannot reconnect right now, and
finishes your request once you have signed in again. A reconnect replaces the
broken connection in place; you never have to disconnect first. A token you
pasted into the Vault by hand gets the same treatment with a different fix,
since nothing renews it: connect the service through this page so its tokens
refresh on their own, or store a fresh token when asked. MCP servers get the
same care in the agent's MCP tab (see [MCP servers](#mcp-servers)).

### Vault (admin only)

Opened from the **account menu** (bottom-left) under **Administration**, or
from ⌘K.

The encrypted credential store: every API key, bot token and service password.
You can add, rename and delete entries, and set **per-agent overrides** so two
agents using the same credential name authenticate as different identities.

**Who can see a stored value.** An admin can reveal one here in the dashboard.
An agent cannot, by default: asking the vault for a credential returns a
*masked* preview (`PKBM************`), enough to confirm which key is stored
without putting the secret in the conversation. The full value is an explicit,
owner-only request that needs human approval before it reaches the agent. So an
agent can list names and *use* a credential freely, and only a person can turn
one back into plaintext.

**Entries the runtime keeps for itself are not listed.** The deployment's own
connection credentials (the key it uses to reach Olano-managed models, the
signing key for its link to Olano Cloud) live in the same encrypted store, but
they are created and renewed by the runtime, never by you. They do not appear
on this page, cannot be revealed, changed, removed or overridden from the
dashboard, and agents cannot read or use them. Whether the deployment is
connected is shown on the **Overview** provider card instead.

**Rekey** changes the vault passphrase and re-encrypts every stored secret under
the new one. Use it if the passphrase may have been exposed.

The vault re-locks whenever the server or dashboard restarts, and the dashboard
prompts you to unlock it before anything that needs a credential can run.

### Agent to Agent

A live view of messages passing between agents: who asked whom to do what, and
what came back. The place to look when a delegation chain does not behave the
way you expected.

Calls that cross the deployment boundary are listed here too, under the
**External** segment, and never mixed in with peer conversations: an outside
system asking one of your agents over MCP or A2A shows the caller by the label
of the token or sign-in it used, with an **MCP** or **A2A** badge and a globe
instead of an agent avatar; an agent calling a remote A2A agent shows the
remote by its configured name with an **A2A** badge. Open a row to read the
full exchange. The same conversations appear in the agent's **Chat** history
carrying an **External** pill plus the protocol pill, and the agent's
**External access** tab lists its most recent inbound calls with links to both
views.

### Org Chart

A visual map of your agents: who manages whom, who reports to whom, and who
counts as a peer. Editing the chart here changes what the agents are actually
allowed to do — the hierarchy grants messaging permissions (see
[Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart)).

**Connectors on each card.** Every agent card shows a row of small icons, one
per messaging connector the agent is set up with (WhatsApp, Telegram, Discord,
Slack, Signal, email, SMS and the rest), so you can see at a glance which agent
answers where. A healthy connector shows just its icon. A corner dot means it
needs a look:

| What you see | Meaning |
|---|---|
| Icon only | Connected and working. |
| Amber dot | Connecting: still starting, reconnecting, or waiting for you to finish pairing (for example a WhatsApp QR code that has not been scanned yet). |
| Red dot and red ring | Needs attention: the connector could not start (often a missing token or password) or the service refused it. Hover the icon to read the reason. |
| Greyed out | The agent itself is stopped, so none of its connectors are running. |
| Pulsing ring | The agent is answering a message that arrived on that connector right now. |

When a card is too narrow for every icon, the last slot shows **+N**; hover it
to list the rest. If any of the hidden connectors has a problem, the **+N**
carries its dot. Click a card to open its details: the **Connectors** section
lists each connector with its status and, for a failing one, the reason, and
**Manage connectors** opens that agent's **Connectors** tab to fix it.
Typing a connector name in the chart's search box (for example `whatsapp`)
highlights every agent on it. The **Connectors** toggle above the chart hides
or shows the icons; the chart refreshes their status on its own every half
minute and whenever an agent starts or stops.

### Logs (admin only)

Opened from the **account menu** (bottom-left) under **Administration**, or
from ⌘K.

Runtime logs, filterable by agent and severity. First stop when an agent will
not boot or a connector is silent.

### Terminal

A shell in the agent's workspace, from the browser. Subject to the same
boundaries the agent has — it reaches that workspace and the shared folders, not
other agents' directories.

Every terminal belongs to one agent, because its shell runs in that agent's
workspace: on a managed deployment, or a self-hosted install set up in
[sandbox mode](#sandbox-mode), inside that agent's own sandbox. The
**Workspace agent** picker at the top of the page says whose shells you are
looking at: switch it and the tabs swap to that agent's terminals (its first one
opens on its own if it has none), while the other agents' shells keep running
out of sight. Each row of the picker shows how many terminals that agent has
open, and a hint beside it says whether the shell runs inside the agent's
sandbox or on the host in its workspace. **+** opens another terminal for the
selected agent; closing an agent's last tab leaves an **Open terminal** button
rather than starting a shell you did not ask for. If the agent's sandbox is not
running, the tab says so and waits — start the agent, then press **Reconnect**.
When the runtime knows *why* there is no sandbox (for example the container
failed to start), that reason is shown under the message.

The terminal does not depend on the agent's **Access** level. On a managed
deployment every running agent has a sandbox container, whether its Access is
Read, Read & Write, Read, Write & Execute or Virtual Files — the container is
started with the agent. For an agent without Execute the container is yours,
not the agent's: it is where the Terminal, the chat `!` shell and the agent's
own media/CLI tools run in isolation, while the agent itself still has no shell
tool and keeps exactly the file permissions its Access level grants. Switching
that agent to Read, Write & Execute later reuses the same container, so anything
you installed in it from the Terminal is still there.

It is a real shell, so it behaves like one: **Tab** completes file and directory
names as you type a path, ↑ and ↓ walk back through what you have run, and
`tmux` is installed if you want a session that survives closing the tab.

Two additions on top of the shell itself:

- **Command suggestions.** Once you have typed a couple of characters, commands
  you have run here before appear next to the line you are typing. Press `→` (right arrow,
  at the end of the line) to accept the first one, or tap any of them. They are
  remembered in this browser only, they are never sent anywhere, and a command
  typed with a leading space is not remembered at all — the same convention the
  shell itself uses for keeping a secret out of history. Tab still does file
  completion; the suggestions never take it over.
- **A key bar on phones and tablets.** A touch keyboard has no Tab, arrows, Esc
  or Ctrl, and a browser terminal has no right-click menu, so those live in a
  row under the terminal: Copy (the selection, or the visible screen when
  nothing is selected), Paste, Tab, ↑, ↓, Esc, Ctrl+C and Enter. If your browser
  refuses clipboard access, Paste falls back to asking for the text.

### Playground

A sandbox for trying prompts and configurations against an agent without
touching a real conversation. Off by default; enable it in Config.

### Config (admin only)

Not a tab in the left navigation: open it from the **account menu** — your name
at the bottom-left of the sidebar — under **Administration**, or type "Config"
into the ⌘K search. Admins only; members do not see the entry.

Deployment-wide settings — everything in [`config.yaml`](#configyaml-reference)
with a form for each block: team accounts, member bot creation (whether
non-admins may create their own bots, how many each may own, and which
templates they may pick from), cognition defaults, heartbeat, disk monitoring,
MCP connection health, deployed pages (the switch for page builds and servers, servers per agent, health checks and request limits; see [Pages](#pages-publishing-sites-and-web-apps)), browser control (mode, where it runs, how many agents
may browse at once and the idle timeout, plus the live browser's install
status; see [Browser control](#browser-control)), context window (what a compaction keeps and the MCP
result cap), revisions, issues, speech, media, web search, rate limits,
catalog sync, and the ports.

The **Mobile app** card decides what the Olano app on people's phones shows:
the Cortex tab, the Inbox tab and each of its sections (Approvals, Alerts,
Issues), and the Files, Credits and Usage, and Transcribe on this phone rows in
the app's Settings, plus where voice notes are transcribed for people who have
not chosen (on the deployment, by default). It is saved with the page's Save
button, and phones pick it up the next time the app is opened or brought back
to the foreground. The keys are in the
[`config.yaml` reference](#mobile_app-what-the-mobile-app-shows).

The **Cloud catalogs** card shows each synced catalog (model pricing, the MCP
directory, credit rates, agent archetypes, official connector clients) with its
revision and where it is being served from, and a **Sync now** button that
re-fetches everything immediately instead of waiting for the hourly background
check — useful right after a new official connector is enabled for your
deployment, so its one-click Connect button appears without the wait.

The **Observability (Langfuse)** card sends each agent turn to a
[Langfuse](https://langfuse.com) project as one trace, so you can see every model
call and tool call of a conversation with its timings, tokens and credits, and
search them over time. It is off by default and agents cannot change it. The
switches (on or off, the Langfuse address, what each trace contains, the share
of turns traced, an environment name, whether Cortex background work is traced)
are saved with the page's Save button, like the rest of the page; the field
meanings are in the [`config.yaml` reference](#observability-tracing-to-langfuse).
Choosing **Full content** puts a warning on the card before you save: traces
then hold the conversations themselves. The keys and the connection test act at
once instead:

- **Langfuse keys**: paste the public key (`pk-lf-…`) and secret key
  (`sk-lf-…`) from Langfuse → Settings → API keys and press **Save keys**. They
  are stored in the vault, hidden from agents and from the Vault page, and never
  shown again. The line above the boxes says which keys are in use: the ones
  saved here, the server's `LANGFUSE_*` environment variables, or none.
- **Test connection** checks the keys against the Langfuse address and
  remembers the project, which is what lets conversations link to their traces.
  A failure says why; a refusal on Langfuse Cloud is usually the wrong region.
- **Status** shows how many steps were sent since the last restart, or the last
  problem, with a link to open the project in Langfuse, and **Agents with their
  own setting** lists the agents that override the card from their own
  Observability card (see [Agents](#agents)).

The **Disk encryption** card appears only on a managed instance whose data
lives on an encrypted volume (see [Privacy and security](#privacy-and-security)); a
self-hosted install never shows it. It reads the state of that volume (the
custody mode, the volume's identifier, whether a header backup exists and which
generation is current, whether the instance's encryption service is answering)
and holds the one action the volume asks of you: **Create recovery key**. The
key is generated on the instance, shown to you once, and confirmed by typing it
back, so a copy you never checked cannot be the one you rely on. A key that was
shown and not confirmed stays pending for fifteen minutes (**Confirm the shown
key** or **Cancel pending key** on the card), and **Replace recovery key** makes
a new one that takes over only once it is confirmed. The **Encryption
service** row names the version of the service on the instance that answers
these actions; when it reads *Unavailable*, that service is not answering: your
data stays encrypted and the instance keeps working, only the recovery-key
actions pause (**Refresh** re-reads the state; if it stays unavailable after a
restart of the instance from the cloud console, contact support). Every row's
hint explains what the value means and what the buttons do.

Some fields (ports, team accounts) only take effect after a runtime restart; the
form says so.

**Saving.** The page is long, so **Save changes** and **Discard** live in the
bar at the top, which stays put while you scroll — you never have to scroll
back up to save. While anything on the page differs from what is saved, an
**Unsaved changes** badge sits beside the buttons. Three things then guard the
edits: clicking anywhere that would leave the page (a sidebar entry, the ⌘K
search, a link inside the page) asks **Save and leave / Leave without saving /
Stay on this page** — *Save and leave* is greyed out while a field is invalid,
and the dialog says which case you are in; closing or reloading the tab gets
the browser's own "leave site?" prompt; and if the edits sit untouched for
about a minute, one reminder toast says so. The browser's **Back** button is
the one exit that is not intercepted, so use the sidebar or the dialog to
leave. The **Usage budgets** card has its own Save button beside its fields
and is not part of this — save it where you edit it.

### Admin (admin only)

Opened, like Config, from the **account menu** at the bottom-left of the
sidebar (under **Administration**) or from the ⌘K search.

User accounts and access. Invite people, set them as admin or member, and grant
per-agent access. The access overview shows, for every agent, exactly who can
reach it — including admins, who can administer every agent without holding an
explicit grant. Bulk actions apply one role across many users and agents at once,
which is what makes onboarding and offboarding a single step.

All of it is also available from an **AgentFather conversation**: ask it to
invite someone (you get back a single-use set-password link plus a QR code
image to forward — it never asks for or sets a password in chat), reset a
password (a fresh link + QR; the person sets their own and is signed in),
make someone admin or member, enable/disable an account, assign an owner or
member to an agent, or show the whole access grid. Every change asks a human
for approval first, and only admins can trigger these — a member chatting
with AgentFather cannot see or manage accounts. If the link comes back
without a QR or absolute address, the deployment doesn't know its public
URL yet: open the dashboard once from a browser (or set the public URL) and
ask again.

Each account carries two names, and neither is ever empty. **Name** is what
everyone sees, and it can be changed at any time — but never erased (a blank
edit is refused). **Handle** is a permanent public identifier — lowercase
letters, digits and dashes, like `carlos-m` — and it cannot be changed once the
account exists, because it is what identifies that person in the issue tracker
(as `human-<handle>`). A third, optional field is the person's **Title** — their
position, like `Head of Sales` — shown under their name in the account menu,
on the org chart and in the access tables, the same way an agent's title is.
Set it when inviting someone or edit it in the Users table at any time; it
changes nothing about what they may do.

The **Langfuse** column decides whether an admin sees **Langfuse** links in the
dashboard: one under each traced reply in the chat (in both interfaces) and the
**Open in Langfuse** link on an Insights conversation. It is **off for every
account by default**, existing ones included, so turning tracing on does not put
a debugging link under everyone's replies; switch it on for the admins who debug
with Langfuse. It only shows or hides the links: tracing itself is set in Config
→ Observability and keeps running either way. Members show *Admins only* there
and never see the links, because trace data is admin-only. A single-user install
has no Users table, so its owner always sees them.

The handle field appears everywhere an account is created: the first-run
owner screen, this invite form, and the chat "secure your dashboard" link
(the onboarding wizard's profile panel shows the handle it will generate from
your name, read-only). It checks
availability live as you type and shows exactly what will be saved. **A
handle you type that is already taken is refused** with the next free one
suggested — you are never silently given a variant of the identity you
chose. Leave the field empty instead and one is generated automatically
from the name (or the part of the email before the `@`); auto-generated
handles quietly get a number on a clash, since nobody chose them. An
account created with no name at all gets a presentable one derived from
its handle (`dana-ray` → `Dana Ray`). A handle is not a login: people
still sign in with their email address and password.

---

## `agent.json5` reference

One file per agent, at `~/.olano/agents/<name>/agent.json5`. JSON5, so
comments, trailing commas and unquoted keys are allowed. Most people edit it
through the dashboard's Agents tab; this section is the complete field list for
when you edit it directly, or when an agent writes one for you.

`${VAR}` in a string is expanded from the environment when the file loads.
**Never put a secret in this file** — put it in the vault and reference it by
name (see [Channels and connectors](#channels-and-connectors)).

A minimal agent:

```json5
{
  name: "finance",
  description: "Watches revenue and flags anomalies",
  emoji: "📊",
  model: "olano:pro",
  system_prompt: "You are a finance analyst. Be precise with numbers.",
  tools: ["sql", "stripe"],
  channels: [{ type: "telegram", allowed_users: ["123456789"] }],
}
```

### Identity

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | The agent's permanent handle. Lowercase letters, digits, dash and underscore, starting with a letter or digit. It is the folder its workspace lives in, the path segment in every `/api/agents/…` URL, and the exact string other agents use to reach it. **It cannot be changed after the agent is created** — a rename would strand its threads, memories, saved credentials and issue history. Put anything you may want to change later in `display_name`. |
| `display_name` | string | the handle | What the agent is *called*, as opposed to how it is addressed. Never empty: leave it unset and it becomes the handle itself. Free text — spaces, capitals, accents and punctuation are all fine (`"Merli the Analyst"`). It appears on the agent's dashboard cards and chat header, in its own system prompt and `whoami` output, in the roster other agents read when choosing who to delegate to, and in the `From:` line of any email it sends. Empty falls back to `name`. Unlike `name` this can be edited at any time — nothing is keyed off it, so changing it keeps every thread, memory and issue intact. |
| `org.title` | string \| null | the archetype's label, else `null` | The agent's **title** — its position, what it *is* as opposed to what it is called (`"Recruitment Specialist"` for an agent named `Hillary`). Shown under the name on its cards, in the detail header, in the simple chat's header and profile, and on the org chart; told to the agent in its own prompt and `whoami` output, so it introduces itself by its name and describes its role by its title; and listed beside its name in the roster other agents read when delegating. Installing an archetype fills it with the archetype's human label unless you give one, so naming an install "Hillary" never costs her the role. Free text, optional, editable any time — **Title** on the Overview tab or the Talk to Agent tab. Empty means no position. |
| `description` | string | `""` | One line explaining what the agent is for. Shown in the roster and to other agents deciding whether to delegate to it. |
| `emoji` | string | `"🦁"` | The agent's face wherever no avatar is set. |
| `color` | string \| null | `null` | CSS hex accent, e.g. `"#c96442"`. Themes the dashboard when this agent is active. |
| `avatar_url` | string \| null | `null` | URL of a square image used as the agent's face. Falls back to `emoji`. |
| `archetype` | string \| null | `null` | The template this agent was created from. Informational. |

Every place an agent is created asks for **one name** — the New agent dialog
(the custom tab and the Team/Expert/Integration tabs alike) and the simple
chat's New bot dialog — and that name is the display name. The handle is **generated from it**, never typed: the
form shows what it will be under the name field (`Hillary` → `hillary`), and
if that handle is already taken a number is appended (`hillary-2`). Installing
a catalog archetype with a name of your own keeps the archetype's human label
as the agent's **title** (so "Hillary" installed from the Recruitment
Specialist archetype is Hillary, the Recruitment Specialist — and introduces
herself that way); the New agent dialog prefills its **Title** field with
that label so you can change it before installing. Installing an archetype
without a name of your own keeps the archetype's label as the display name
too (so a support archetype installs as "Customer Service Agent", not as its
internal handle). Asking AgentFather to "install the Recruitment Specialist
as Hillary" does the same thing.

### Model and prompt

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | `default:turbo` | `provider:model`, e.g. `anthropic:claude-sonnet-4-6`, `openai:gpt-5.5`, an Olano tier like `olano:pro` — or a **deployment default alias**: `default:turbo`, `default:pro`, `default:max`. An alias follows whatever target is set in **Config → Default models**, so retargeting it there re-points every agent using it with no per-agent edits. New agents start on `default:turbo`, AgentFather included. See [Models and credits](#models-and-credits). |
| `system_prompt` | string | `""` | The agent's core instructions. Merged with the workspace identity files. |
| `reasoning_effort` | string \| null | `null` | `"low"`, `"medium"` or `"high"`, for models that support it. Ignored otherwise. |
| `model_kwargs` | object | `{}` | Extra parameters passed to the model (e.g. `temperature`). |
| `prompt_mode` | `"full"` \| `"task"` \| `"minimal"` \| `"none"` | `"full"` | How much scaffolding goes into the system prompt. `full` is the everyday value; the narrower modes suit single-purpose agents where a shorter prompt is cheaper and more focused. |
| `vibes_preset` | `"precise"` \| `"warm"` \| `"executive-brief"` \| `"hacker-lab"` | `"precise"` | Baseline tone. `SOUL.md` refines it. |
| `default_verbosity` | `"quiet"` \| `"normal"` \| `"verbose"` | `"normal"` | How much of its work the agent narrates. `quiet` = the answer only; `normal` = the answer plus a note when a turn ran tools silently; `verbose` = full play-by-play. This is the **starting point** for new chats, not a ceiling — a user who runs `/verbosity` pins their own chat and that wins from then on. |
| `prompt_cache` | object | see below | `optimize_boundaries` (default `true`) and `sort_tools` (default `true`). Leave both on unless you are debugging prompt construction. |

**A model string that will be rejected:** `olano:vision` and the Olano image
group are *utility* models the product calls on the agent's behalf. They cannot
be an agent's brain, and setting one as `model` fails to load.

### Tools

| Field | Type | Default | Meaning |
|---|---|---|---|
| `tools` | string[] | `[]` | Toolkits and individual tools to enable, e.g. `["sql", "slack", "stripe"]`. Listing a tool here binds it unconditionally. |
| `disabled_tools` | string[] | `[]` | Remove specific tools that would otherwise be granted automatically. Use this to drop one tool without switching off a whole category. |
| `default_internal_tools` | boolean | `true` | Master switch for every implicit grant — the universal tools, memory, notes, knowledge-graph, workspace context. Turning it off gives you a locked-down agent that only has what `tools` names. |
| `top_level_memory_tools` | string[] \| null | `null` | When set, an **allow-list**: only these memory tools survive. `null` means "all of them". |
| `top_level_rag_tools` | string[] \| null | `null` | Same, for document-search tools. |
| `top_level_graph_tools` | string[] \| null | `null` | Same, for knowledge-graph tools. |
| `top_level_workspace_context` | boolean | `true` | Whether the agent can load extra workspace files on demand. |
| `allowed_actions` | object | `{}` | `{ "action_name": "path/to/script" }` — named scripts the agent may run via `execute_action`. Only bound when per-customer memory is on. |

**Every agent automatically gets a universal set** you do not have to ask for:
self-awareness (`whoami`), time (`get_current_time`), peer messaging
(`talk_to_agent`), outbound
notifications (`notification_send_text` / `_photo` / `_document`), chat
directory lookups, the issue-tracker tools, web search and fetch, document
readers, HTML rendering to PNG / PDF / SVG (`render_html_image` /
`render_html_pdf` / `render_html_svg` — how charts, slides and social graphics
are made, see the `charts` skill), a recoverable trash can, and the
operations tools: the scheduler (`scheduler_*`), heartbeat checks
(`heartbeat_*`), PULSE (`pulse_*`), approvals (`approvals_*`) and the ops queue
(`ops_*`). Keep these unless you have a specific reason not to; several
features assume they exist.

Any of them can be switched off one at a time: untick it on the agent's
**Tools** tab, or name it in `disabled_tools` (for example
`"disabled_tools": ["scheduler_delete", "approvals_approve"]`). Ticking it
again, or removing it from `disabled_tools`, puts it back. Listing it under
`tools` does not remove it, because these are added to every agent whatever
`tools` says. `default_internal_tools: false` turns the whole set off at once.

Two things to know about the operations tools. An agent in Customer Service
mode does not get them (a customer must never see or change your scheduler or
approvals); list one under `tools` if such an agent really needs it. And the
ones that act on held work (`approvals_approve`, `approvals_reject`,
`approvals_defer`, `approvals_clear_expired`, `pulse_accept`, `pulse_undo`,
`scheduler_run_now`, `heartbeat_configure`) only work for an owner: another
agent, an outside caller or a non-admin member asking for them is refused.

**The knowledge-base tools are not in that set.** `kb_search`, `kb_list`,
`kb_add_file`, `kb_add_url`, `kb_create_store`, `kb_sync`, `kb_delete` and
`kb_remove_file` bind only when `tools` names them. An agent without them cannot
search your documents, and the dashboard's Knowledge tab cannot route through
it. Add them to any agent you want answering from the document library.

Tools are grouped into these categories in the dashboard: Self & Time, Agent
Mesh, User Interaction, Web & Documents, Multimodal, Housekeeping, Operations, Cortex,
Memory, Customer Memory, Multi-user Actions & Scheduling, Knowledge (RAG),
Knowledge Graph, Notes, Workspace, Agent Loop, Filesystem & Shell — plus 80+
service toolkits (Slack, Notion, Stripe, SQL, Google, GitHub, HubSpot,
Salesforce, Apollo.io, Lusha, Shopify, Jira, Linear, and many more).

**Multimodal tools are automatic when they can work.** `image_describe`,
`image_generate`, `image_edit`, `text_to_speech` and `audio_to_text` bind
whenever the credentials to run them resolve. On a managed deployment
everything except `text_to_speech` runs on Olano credits with no configuration
at all; a self-hosted install with an Olano API key on Providers → Olano gets
the same. On an install with neither they appear once you set the matching
provider key. `text_to_speech` is the one deliberate exception on every install: voice
replies stay off until you pick a text-to-speech provider under Config →
Speech (on a managed deployment choose *Olano* there to run it on credits).

**Rendering HTML to an image, a PDF or an SVG.** The `render` tools turn HTML
and CSS into a PNG, a PDF or a vector SVG at exact pixel dimensions, using the
browser that ships with every install — no API key, no network, nothing
billed. They are on for every agent (switch them off with `disabled_tools`).

| Tool | What it does |
|---|---|
| `render_html_image` | An HTML document or `.html` file → a PNG at exactly the width and height you ask for. `scale: 2` exports at twice the pixel size for retina. |
| `render_html_pdf` | The same, to a PDF, one page per `break-after: page` block — reports, printable documents, and what a LinkedIn native document (carousel) post needs. |
| `render_html_svg` | Saves one `<svg>` of the rendered page as a standalone vector file — a chart that scales in a slide deck or a design tool. |

All three take `wait_for`, a CSS selector to wait for before capturing, for
pages that draw with JavaScript (the `charts` skill's pages add
`.render-done` when finished). The tools pair with `image_generate`: the
image model paints the background, and anything a person is meant to *read* —
headline, statistic, logo, call to action — is set in CSS, so it comes out
correct every time and a copy change is a re-render rather than a new image.
Each accepts an inline HTML document or a path to an `.html` file, and writes
to `workspace/renders/` unless you pass `save_path`, which must point inside
the agent's workspace or a shared folder it can write to. Data charts go through
the `charts` skill, which builds the page (recipes, or any Vega-Lite spec, in
the clean / midnight / editorial / brand themes) and renders it with these
tools; no plotting library is ever installed. A document may only load local
files from the agent's workspace, its skills and its shared folders (an HTML
path outside them is refused), and remote images, stylesheets and fonts follow
the agent's outbound-access lists. Anything refused is left out of the render
and named at the end of the result.

**Driving a web browser.** When browser control is turned on for the machine
(see [Browser control](#browser-control)), the `browser` toolkit gives
an agent a Chrome it can navigate, read, click and type in. `browser_snapshot` lists what
is on the page (headings, links, buttons, fields) with a short ref such as
`e12` next to each element, and `browser_click` / `browser_type` accept that
ref, so the agent acts on exactly the element it saw instead of guessing a
selector. `page_screenshot` takes a one-off screenshot with nothing kept
afterwards.

- **Each agent has its own browser session.** Cookies, site storage and the
  open page belong to one agent; a site an agent signs in to is not signed in
  for any other agent on the box.
- **Sign-ins are kept.** The session's cookies and site storage are saved by
  the deployment, outside every workspace (readable only by the deployment, not
  by the agent or its shell), and restored the next time the agent opens the
  browser, including after a restart. Asking the agent to close the browser and
  forget its profile signs it out everywhere; the agent's **Browser** tab has
  the same button.
- **Idle sessions close themselves.** After `browser.idle_timeout` seconds
  without a browser call (10 minutes by default, set in `config.yaml`) the
  session is saved and closed.
- **A limited number run at once.** Each open session uses a few hundred MB of
  memory, so at most `browser.max_sessions` are open at a time. When the limit
  is reached, the session idle the longest is saved and closed to make room; if
  every session is in use, the agent is told the browser is busy and retries.
- **Outbound access applies to everything the browser loads**, not only the
  address the agent opens. See [Security](#security).
- **Screenshots stay in the workspace.** A `save_path` outside the agent's
  workspace (or a shared folder it can write to) is refused, the same rule
  the agent's file tools follow.
- **Local files.** In `headless` mode the browser can open a file in the
  agent's workspace (a `file://` address), and a page opened that way can load
  other files from the workspace, the agent's skills and its shared folders.
  The live browser and a remote provider run elsewhere and open no local files
  (one-shot screenshots still can, unless the provider is remote). Every other file on
  the machine is refused, including through a link or a shortcut (symlink) that
  points outside, and so are browser-internal addresses such as
  `view-source:` or `chrome:`.

**Private repositories.** Local git — `status`, `add`, `commit`, `diff`,
`checkout`, `log` — runs in the agent's own shell and needs no credential. The
verbs that talk to GitHub go through the `git` toolkit, which runs them with
the connected GitHub account and lands the files in the agent's workspace; the
credential itself never enters the agent's sandbox, so a `printenv` there
shows nothing before, during or after.

| Tool | What it does |
|---|---|
| `git_clone(url, dest, branch, depth)` | Clone a repository into the workspace (`dest` relative to it; `branch` and a shallow `depth` optional). |
| `git_fetch(repo_path, remote)` | Fetch a remote of a repository in the workspace. |
| `git_pull(repo_path, remote, branch)` | Pull into a checkout in the workspace. |
| `git_push(repo_path, remote, branch, set_upstream)` | Push a branch; `set_upstream` for a new one. Always raises an approval card. Force-pushing is refused. |

HTTPS URLs only — an SSH URL is refused with the HTTPS form to use instead. The
account comes from whatever way GitHub was connected (Connections → GitHub, a
sign-in code, a magic link from the chat, or a `GITHUB_TOKEN` in the Vault); a
missing one answers `[missing-credentials]` and names the link to send, never a
request to paste a token. Add `"git"` to `tools` — or ask the agent, which adds
it alongside the GitHub toolkit when you ask it to connect GitHub.

**Bookkeeping.** The `quickbooks` toolkit works a connected QuickBooks Online
company across the four areas Olano's Intuit app is registered for. Connect it
under Connections → QuickBooks (the **environment** field picks a sandbox
company instead of the live one), then add `"quickbooks"` to `tools` — or use
the built-in **QuickBooksAgent** subagent, which carries the whole set.

| Area | Tools |
|---|---|
| Customers | `quickbooks_list_customers`, `quickbooks_search_customers`, `quickbooks_get_customer`, `quickbooks_create_customer`, `quickbooks_update_customer` |
| Invoicing & payments | `quickbooks_create_invoice`, `quickbooks_list_invoices`, `quickbooks_get_invoice`, `quickbooks_send_invoice`, `quickbooks_void_invoice`, `quickbooks_list_estimates`, `quickbooks_create_estimate`, `quickbooks_list_sales_receipts`, `quickbooks_create_sales_receipt`, `quickbooks_list_payments`, `quickbooks_record_payment` |
| Expenses | `quickbooks_list_vendors`, `quickbooks_create_vendor`, `quickbooks_list_bills`, `quickbooks_create_bill`, `quickbooks_pay_bill`, `quickbooks_list_expenses`, `quickbooks_create_expense` |
| Business insights | `quickbooks_profit_and_loss`, `quickbooks_balance_sheet`, `quickbooks_cash_flow`, `quickbooks_ar_aging`, `quickbooks_ap_aging`, `quickbooks_sales_by_customer`, `quickbooks_expenses_by_vendor`, `quickbooks_trial_balance` |

Three behaviours are worth knowing before you ask an agent to post anything:

- **Writes need the right reference id, and there are tools to find them.**
  `quickbooks_list_accounts` returns the chart of accounts (filter it by type,
  e.g. `Expense`) — a bill or an expense needs one to categorise the money, and
  paying a bill needs the bank or credit-card account it comes out of.
  `quickbooks_list_items` returns the products and services an invoice line can
  point at. An invoice left without an item id falls back to the company's first
  active product/service rather than failing.
- **Invoices can carry many lines.** Pass `line_items` a JSON array —
  `[{"amount": 100, "description": "Design"}, {"quantity": 2, "unit_price": 50}]`
  — and it replaces the single amount/description pair. Estimates and sales
  receipts take the same shape; bills and expenses take the same shape with an
  `account_id` per line.
- **Invoices are voided, never deleted.** `quickbooks_void_invoice` keeps the
  document on the books at zero value so the audit trail survives, which is what
  an accountant expects. Updates change only the fields you pass; everything
  else on the record is left alone.

Reports come back as a readable outline rather than raw data, so an agent can
quote a figure straight into a chat message or feed one into the `charts` skill.

**Microsoft 365.** The `microsoft365` toolkit works a connected Microsoft 365
account: Outlook mail and calendar, contacts, To Do, OneDrive and SharePoint
files, Excel workbooks, and Teams. Connect it under Connections → Microsoft 365
(or ask the agent for a sign-in link), then add `"microsoft365"` to `tools`, or
use the built-in **Microsoft365Agent** subagent, which carries the whole set.

| Area | Tools |
|---|---|
| Mail | `ms365_list_messages`, `ms365_get_message`, `ms365_send_mail`, `ms365_create_draft`, `ms365_update_draft`, `ms365_send_draft`, `ms365_reply`, `ms365_forward`, `ms365_update_message`, `ms365_move_message`, `ms365_list_mail_folders`, `ms365_download_attachment` |
| Calendar | `ms365_list_events`, `ms365_get_event`, `ms365_create_event`, `ms365_update_event`, `ms365_delete_event`, `ms365_respond_to_event`, `ms365_find_meeting_times`, `ms365_get_availability`, `ms365_list_calendars` |
| People & mailbox | `ms365_find_people`, `ms365_get_profile`, `ms365_list_contacts`, `ms365_create_contact`, `ms365_set_auto_reply` |
| Files | `ms365_list_drive_items`, `ms365_search_drive`, `ms365_search_files`, `ms365_upload_drive_file`, `ms365_download_drive_file`, `ms365_create_folder`, `ms365_share_drive_item`, `ms365_move_drive_item`, `ms365_delete_drive_item`, `ms365_find_sites` |
| Excel | `ms365_excel_list_sheets`, `ms365_excel_read_range`, `ms365_excel_write_range`, `ms365_excel_add_rows` |
| Teams | `ms365_list_teams`, `ms365_list_channels`, `ms365_send_channel_message`, `ms365_list_chats`, `ms365_read_chat`, `ms365_send_chat_message` |
| To Do | `ms365_list_task_lists`, `ms365_list_tasks`, `ms365_create_task`, `ms365_update_task` |

What to know before asking an agent to act on it:

- **Drafts are the safe default.** `ms365_create_draft` saves a message (or a
  reply or forward that keeps the thread) in Outlook's Drafts folder and returns
  a link to review it there; nothing leaves until `ms365_send_draft`, or until
  you send it yourself from Outlook. Ask for "a draft" when you want to read it
  first.
- **Mail is HTML, and pictures can sit inside it.** Every message, reply,
  forward and draft goes out as an HTML body, so headings, bullet lists, tables
  and links render the way Outlook shows them. A picture can appear inside the
  text in two ways: a hosted image by its `https://` address, or a file from
  the agent's workspace embedded in the message (`inline_images`, referenced in
  the body by its file name), which is how a header banner or a signature logo
  travels with the email. Regular `attachments` take any file type up to 150 MB
  each. Reading works the same way round: `ms365_get_message` lists every
  attachment, including a screenshot someone pasted into the body, and
  `ms365_download_attachment` saves them into the workspace so the agent can
  open or describe them (`include_inline: false` skips the embedded pictures).
- **Meetings send real invitations.** `ms365_create_event` with attendees makes
  Outlook email each of them an invitation, and `online_meeting` adds a Teams
  join link. Changing the time or the attendee list sends an update, and
  deleting a meeting you organize sends a cancellation with your note. When you
  name people rather than addresses, the agent looks them up first
  (`ms365_find_people` searches the people you work with, your organization's
  directory and your contacts), and it can ask Outlook for a time that suits
  everyone (`ms365_find_meeting_times`) before booking.
- **Times follow your mailbox.** An agent reads and writes times in the time
  zone set in your Outlook settings unless you name another one.
- **Anything that reaches other people asks first** under the `safe` and
  `production` security profiles: sending, replying and forwarding mail, sending
  a draft, creating, changing or cancelling a meeting, answering an invitation,
  turning on an automatic reply, uploading, sharing or deleting a file, writing
  to a workbook, and posting to Teams. Drafts, flags, folder moves, contacts and
  To Do tasks do not.
- **Files land in the workspace.** Attachments and downloaded files are saved in
  the agent's `downloads` folder, where its other tools can open them; a
  download can also be converted to PDF on the way.
- **Some features need a work or school account.** Teams, meeting-time
  suggestions and availability, the organization directory, SharePoint, search
  across shared files, and Excel are not available on a personal Microsoft
  account; mail, calendar, contacts, OneDrive files and To Do are.
- **A connection made before these features existed needs one fresh sign-in.**
  It keeps working for what it could already do, and anything newer (drafts,
  mail folders, availability, people search, contacts, To Do, SharePoint and
  shared files, Teams chats) answers that the permission is missing; the agent
  then offers you a sign-in link. Signing in again replaces the connection in
  place. If your organization requires an administrator to approve app
  permissions, the administrator approves the new ones once.

**HubSpot.** The `hubspot` toolkit works a HubSpot CRM account: contacts,
companies, deals, tickets, products, line items, quotes and invoices; notes,
tasks, calls, emails and meetings logged against them; pipelines, owners,
associations, properties and lists; imports and exports; workflows, forms,
marketing and transactional email; blog posts, pages and HubDB. Connect it under
Connections → HubSpot (or ask the agent for a sign-in link), then add
`"hubspot"` to `tools`, or use the built-in **HubSpotAgent** subagent, which
carries the whole set. A private-app token stored in the **Vault** as
`HUBSPOT_ACCESS_TOKEN` still works instead of a connection, but it never renews
on its own.

- **The sign-in adapts to your HubSpot plan.** Contacts, companies, deals and
  owners are part of every plan and are always granted. Everything else (tickets,
  quotes, workflows, CMS, marketing email, …) is granted only when your plan
  includes it and the person signing in has access to it in HubSpot. A tool for a
  feature the connection was not granted answers that a scope is missing instead
  of failing; if your plan does include it, sign in again as a HubSpot user with
  access to that feature.
- **Sign in as a user with the access the agent needs.** HubSpot applies that
  user's permissions to every call the agent makes.

**Apollo.io.** The `apollo` toolkit works an Apollo.io account: searching
Apollo's database for people and companies, enriching a person (work email,
title, employment history) or a company (industry, headcount, revenue, funding,
technologies), job postings and news about a company; the contacts, accounts and
deals saved in Apollo, with their stages, lists, notes and tasks; and sequences
(finding them, enrolling contacts, removing or stopping them, checking the
emails they sent). Add `"apollo"` to `tools`, or use the built-in
**ApolloAgent** subagent, which carries the whole set. It needs an Apollo API
key stored in the **Vault** as `APOLLO_API_KEY`; ask the agent and it sends you
a secure form for it.

- **Use a master key unless you want to limit the agent.** In Apollo (Settings,
  Integrations, API keys) a key is either a master key or limited to the
  endpoints ticked when it was created. A limited key answers "not allowed to
  call this endpoint" for anything else, and listing your team's users or the
  API usage needs a master key. Apollo runs every call as your workspace's
  longest-standing active admin, whoever created the key.
- **Some steps spend Apollo credits.** The people search is free and never
  returns emails or phone numbers. Enriching a person costs a credit when Apollo
  finds them (personal emails and phone numbers cost more), enriching a company
  costs one per company, and company search, job postings and news cost one per
  page. When you ask for one of these, the agent runs it without asking you to
  confirm, then reports the credits used and can show your remaining balance.
  Phone numbers arrive a few minutes later; the agent gives you the rest first
  and fetches them when Apollo has finished.
- **Enrolling contacts in a sequence sends real email.** Only saved contacts can
  be enrolled, so the agent saves a prospect first (matching an existing contact
  instead of duplicating it). Under the Supervised and Locked-down profiles the
  enrollment waits for your approval, as does the agent's general-purpose
  Apollo request, which can reach any endpoint. Searches, enrichment and record
  edits do not ask.

**Lusha.** The `lusha` toolkit works a Lusha account: looking up people and
companies you already know and revealing their emails, phone numbers and company
data (industry, headcount, revenue, funding, technologies, open jobs, parent
companies); finding new prospects and companies by role, seniority, size,
industry, technology, buying intent and recent activity; people and companies
similar to examples you give; the buying committee at a target company; job
changes, promotions, hiring surges, news and intent changes; scores against your
Target Profiles and by buying signals; the companies visiting your tracked
websites; sales calls recorded by Lusha Conversations; Lusha tables; and webhook
subscriptions. Add `"lusha"` to `tools`, or use the built-in **LushaAgent**
subagent, which carries the whole set. It needs a Lusha API key stored in the
**Vault** as `LUSHA_API_KEY` (Lusha dashboard: Enrich, API, Manage API keys);
ask the agent and it sends you a secure form for it.

- **Your Lusha plan decides what works.** The key needs a plan with API access,
  and everything beyond basic lookups (prospecting, lookalikes, signals, scores,
  tables) needs Lusha's v3 API access, which Lusha's account team turns on. A
  call your plan does not cover answers that the account is inactive or lacks v3
  access; tell your Lusha contact. Website visitors need the sites set up for
  tracking in Lusha first.
- **Most steps spend Lusha credits.** Searches and prospecting return previews
  without emails or phone numbers and charge per result; revealing emails or
  phones charges per field and per person; company data, lookalikes, signals,
  scores and reading a table's rows charge too. Listing filter values, signal
  types, Target Profiles and tables is free. The agent runs a step you ask for
  without asking first and reports the credits it used; it can show your
  balance and the price of every action. Phone numbers that Lusha fetches from
  other providers (Data Waterfall, when you have it on) can arrive a little
  later; the agent fetches them when Lusha has finished.
- **Tables act as a person.** Lusha needs the email of a user on your Lusha
  account to create, read or change a table; the agent asks you for it.
- **Some steps wait for approval.** Under the Supervised and Locked-down
  profiles, the agent asks before it creates or changes a webhook subscription
  (Lusha would send your contact and company data to the address it names),
  deletes a subscription, a table or a table column, rotates the webhook
  signing secret, or uses its general-purpose Lusha request, which can reach any
  endpoint; reading the webhook signing secret asks too. Searches, enrichment,
  scores and table edits do not ask.

Gmail (the `google` toolkit) works the same way for evidence and files:

- **Each message shows two times.** *Date* is what the sender's mail program
  wrote; *Received (UTC)* is Gmail's own record of when the message arrived or
  was sent. Cite the second when the time matters, for example when an order
  was placed.
- **Attachments save into the workspace.** A message lists its attachments by
  name, size and type, and `gmail_download_attachments` saves them (all of
  them, or the names you pick) into `downloads/gmail/<message id>/` unless you
  name another folder inside the workspace. Saving the same file twice reuses
  the copy already there. Saving needs no approval: the file stays inside the
  agent's own workspace.
- **Mail written only in HTML** (common for broker and bank notifications) is
  read as plain text instead of coming back empty.
- **New mail can wake the agent.** See [`gmail_watch`](#gmail_watch-wake-on-new-email).

### Subagents

| Field | Type | Default | Meaning |
|---|---|---|---|
| `subagents` | object[] | `[]` | Specialists this agent can delegate to. |
| `builtin_subagents` | list or object | `[]` | Ready-made subagents. `"all"`, `"none"`, or a list of names. The operations manager (`"ops"`) is no longer added by default, since its scheduler, heartbeat, PULSE and approvals tools are on every agent directly; list `"ops"` to add it anyway. |

Each entry in `subagents`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | How the parent addresses it. |
| `description` | string | **required** | What it is for — the parent reads this to decide when to delegate. |
| `system_prompt` | string | **required** | Its instructions. |
| `model` | string \| null | `null` | Inherits the parent's model when unset. Use a cheaper model here for narrow work. |
| `tools` | string[] | `[]` | Its own tools. |
| `skills` | string[] | `[]` | Its own skills. |
| `subagents` | object[] | `[]` | Nested specialists. |
| `enabled` | boolean | `true` | |
| `from_archetype` | string \| null | `null` | Build this subagent from a ready-made archetype instead of writing it out. |
| `system_prompt_extension` | string \| null | `null` | Text **appended** to the subagent's prompt at build time, under an "Additional owner instructions" heading. Made for catalog-sourced subagents: the catalog prompt stays live, your additions ride on top. |
| `backend` | object \| null | `null` | Give the subagent its own filesystem/sandbox. |

Subagents are **internal** to their parent — they are not agents in the roster,
they have no channels of their own, and nobody outside can message them. Use a
subagent for a specialty the parent needs; use a separate top-level agent when
the thing needs its own inbox, schedule or memory.

**Customizing a catalog-sourced member.** A `from_archetype` entry (and a
`builtin_subagents` integration) takes overrides *next to* the reference — any
field you set wins over what the catalog ships, everything else keeps tracking
the catalog:

```json5
subagents: [
  { from_archetype: "sales-coach" },                          // all catalog defaults
  { from_archetype: "sales-engineer", model: "default:pro" }, // stronger brain, same prompt
  {
    from_archetype: "sales-pipeline-analyst",
    system_prompt_extension: "Report figures in EUR. Our quarter ends in February.",
  },
]
```

In the dashboard this is the pencil on any subagent card (**Subagents** tab):
pick a model ("Inherit" keeps the member's own default), read the base prompt,
type additions into **Additional instructions**, or — deliberately, behind a
warning — **detach** and replace the whole prompt, which freezes it against
catalog updates. Team installs offer the same model choice per member in the
New agent dialog before anything is created.

### `org` — who may talk to whom

Covered in depth in [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | string \| null | the archetype's label | The agent's position — shown under its name on cards, in the detail and chat headers and on the org chart, and told to the agent so it introduces itself by name and describes its role by title. Editable from the **Title** field on the Overview tab as well as here; see [Identity](#identity). |
| `manager` | string \| null | `null` | The agent this one reports to. |
| `reports` | string[] | `[]` | Its direct reports. |
| `coworkers` | string[] | `[]` | Peers at a similar level. Display only. |
| `input_agents` | string[] | `[]` | Agents allowed to message this one. **Empty means no restriction.** |
| `output_agents` | string[] | `[]` | Agents this one may message. **Empty means no restriction.** |
| `input_mode` | `"everyone"` \| `"none"` \| `"list"` \| null | `null` | Explicit inbound policy. `null` infers from the list. |
| `output_mode` | `"everyone"` \| `"none"` \| `"list"` \| null | `null` | Explicit outbound policy. |
| `mutating_from_manager_only` | boolean | `false` | When true, only the manager may trigger actions that change things; other peers get read-only service. |
| `autogenerate` | boolean | `false` | Derive both allow-lists from the hierarchy instead of maintaining them by hand. |

Two rules worth committing to memory: an **empty list means unrestricted**, not
"nobody"; and an agent's **manager and direct reports can always exchange
messages**, whatever the allow-lists say, so declaring a reporting line is
enough to make delegation work.

### `agent_visibility` — who this agent knows about

| Field | Type | Default | Meaning |
|---|---|---|---|
| `mode` | `"none"` \| `"all"` \| `"list"` | `"none"` | Which other agents this one is told exist. |
| `agents` | string[] | `[]` | The list, when `mode: "list"`. |

Visibility is **awareness, not permission**. It controls what the agent is told
about its colleagues; `org` controls what it is allowed to do. An agent that can
see a peer but has no `org` permission still cannot message it.

### Access and lifecycle

| Field | Type | Default | Meaning |
|---|---|---|---|
| `human_only` | boolean | `false` | Refuse all inbound agent-to-agent messages. Use for an agent that only ever takes direction from a person — the deployment operator, for example. |
| `fleet_access` | boolean | `false` | Lets this agent's shell and file tools reach the whole deployment instead of only its own workspace: every agent's folder under the Olano home's `agents/` (read and write: workspaces, `agent.json5` files and connection files, including agents created later), the common skills folder `skills/` (read and write) and the deployment logs in `logs/` (read only). The vault, the conversation databases and `config.yaml` stay out of reach. Meant for AgentFather, which ships with it on, so it can search every agent's files and logs and fix a shared skill in place. Only an administrator can turn it on for an agent: AgentFather cannot grant it to another agent without an administrator in the conversation. Approvals for file writes and shell commands still apply. Changing it rebuilds the agent's sandbox on its next restart. In the dashboard it is the **Fleet file access** switch on the agent's Overview tab (**Access & Safety** card), which asks you to confirm before turning it on. |
| `hidden` | boolean | `false` | An internal/test agent: not booted, and excluded from every listing — the dashboard roster, the org chart, admin and access-management views included. |
| `external_access` | object | all off | Whether systems **outside** the deployment may send this agent messages, over MCP and/or A2A, and the public description they see. Edited on the agent's **External access** tab; see [`external_access`](#external_access-letting-outside-systems-reach-this-agent) and [Beyond the deployment: A2A and MCP](#beyond-the-deployment-a2a-and-mcp). |
| `browser` | object | no rules | This agent's browser settings. `auto_approve` lists narrow exceptions to the approval the browser asks for before sending, posting or submitting; see [`browser`](#browser-actions-that-dont-ask) and [Browser control](#actions-that-dont-ask). Only admins and the agent's owners can change it. |
| `skills` | string[] | `[]` | Extra directories to load skills from, absolute or relative to the workspace. The agent's own `workspace/skills/` and the shared common skills are always included and do not need listing here. |
| `disabled_skills` | string[] | `[]` | Skills this agent must **not** load, by skill name (the `name:` at the top of its `SKILL.md`, which is also its folder name) — e.g. `["canvas-design", "xlsx"]`. One name per entry; matching ignores capitalisation. A named skill is not described in the agent's prompt, cannot be delegated to, and is ignored by self-learning, but it stays on disk and stays listed (marked *Off*), so it can be switched back on. Naming a common skill takes it away from **this agent only** — every other agent keeps it, which is the reason this is a denylist rather than a deletion. An entry naming a skill that is not installed is harmless and does nothing. Empty (the default) means every discovered skill is loaded. Editable in the dashboard: **Agents → agent → Skills**, the switch on each row. Applies after a restart. |
| `workspace` | string \| null | `null` | Derived automatically. **Do not set this by hand.** |
| `env` | object | `{}` | Per-agent environment values. Non-secret settings only. |

### `mcp_servers` — external tool servers

See [MCP servers](#mcp-servers) for the full walkthrough.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `url` | string | **required** | The server endpoint. |
| `transport` | `"sse"` \| `"http"` \| `"stdio"` | `"sse"` | How to reach it. `stdio` runs the server as a process instead of connecting over the network — on managed deployments that process runs inside an isolated sandbox container. |
| `enabled` | boolean | `true` | |
| `command` | string \| null | `null` | For `stdio`: the executable to run. |
| `args` | string[] | `[]` | For `stdio`: its arguments. |
| `env` | object | `{}` | For `stdio`: environment for the child process. Values may reference a stored credential as `${vault:NAME}`. |
| `headers` | object | `{}` | Extra HTTP headers — the usual way to pass an API key to an `http`/`sse` server. Values may reference a stored credential as `${vault:NAME}`. |
| `auth` | `"none"` \| `"oauth"` | `"none"` | Use `oauth` for servers that require a sign-in. |
| `oauth_scope` | string \| null | `null` | Requested scope. |
| `oauth_client_id` | string \| null | `null` | For servers that need a pre-registered client. |
| `oauth_client_secret` | string \| null | `null` | Prefer the vault over writing this here. |
| `disabled_tools` | string[] | `[]` | Hide specific tools the server offers. A hidden tool is never offered to the model; only the enabled ones are bound and listed in its prompt. The agent can still see what is hidden through `self_list_mcp_tools` and suggest enabling one when a task needs it. |

### `remote_agents` — external agents over A2A

Agents hosted **outside** this deployment that this agent may hand work to,
over the open Agent2Agent (A2A) protocol. See
[Beyond the deployment: A2A and MCP](#beyond-the-deployment-a2a-and-mcp) for the walkthrough. Listing at
least one enabled entry gives the agent the `a2a_list_agents`,
`a2a_discover_agent`, `a2a_send_message` and `a2a_get_task` tools; an empty
list (the default) means it can reach no external agent at all.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | The handle the agent uses, e.g. `acme-support`. Letters, digits, `-`, `_`; matching ignores capitalisation. |
| `url` | string | **required** | Base URL of the remote agent, e.g. `https://agents.acme.com/support`. Its Agent Card is read from `<url>/.well-known/agent-card.json`. Must be `https://`; hosts on private, link-local or cloud-metadata addresses are always refused. |
| `enabled` | boolean | `true` | Off keeps the entry but removes the agent from reach. |
| `description` | string | `""` | What the remote agent is for, shown to your agent when it lists them. Empty falls back to the remote card's own description. |
| `auth` | `"none"` \| `"bearer"` \| `"api_key"` | `"none"` | How to authenticate. `bearer` sends `Authorization: Bearer <credential>`; `api_key` sends the credential in the header named by `api_key_header`. |
| `credential` | string | `""` | The secret for `bearer`/`api_key`, ideally a vault reference such as `${vault:ACME_A2A_TOKEN}` so it never sits in this file. Resolved per agent (this agent's override, then the shared entry). |
| `api_key_header` | string | `"X-API-Key"` | Header name for `api_key` mode. |
| `timeout_seconds` | integer | `120` | How long one message waits for an answer (5–900). A task still running at the deadline comes back as in progress with an id to poll. |
| `allow_insecure_http` | boolean | `false` | Permit `http://` for a loopback host only (local development). Never for anything on a network. |
| `same_origin_endpoint` | boolean | `true` | Require the service endpoint the remote card advertises to share scheme, host and port with `url`, so a compromised card cannot redirect your credential elsewhere. |

### `external_access` — letting outside systems reach this agent

The **inbound** direction: someone else sending this agent messages. Not to be
confused with `mcp_servers` (servers this agent uses) or `remote_agents`
(agents this agent calls). Edited on the agent's **External access** tab; the
matching home-wide switches live under **Config → External access**
([`external_access:` — letting outside systems reach your agents](#external_access-letting-outside-systems-reach-your-agents)). Both
flags are off by default; the walkthrough is [Beyond the deployment: A2A and MCP](#beyond-the-deployment-a2a-and-mcp).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `mcp` | boolean | `false` | Publish the agent as an `ask_<agent>` tool on the deployment's MCP endpoint for platforms that speak the Model Context Protocol (Claude, ChatGPT, Cursor, agent frameworks). Needs the home-wide MCP switch too. Applies on the agent's next restart. |
| `a2a` | boolean | `false` | Publish an Agent Card and JSON-RPC endpoint for agents and platforms that speak the Agent2Agent protocol. Needs the home-wide A2A switch too. Applies on the next restart. |
| `description` | string | `""` | **Required while either flag is on.** One to three plain sentences on what the agent can do for an outside caller, what it needs and what it will not do — the MCP tool description and the A2A card description, i.e. everything a remote caller sees. Written for a stranger; it is *not* the system prompt and is never shown to the agent itself. Up to 2000 characters. A config that turns a flag on without it is refused. |

### `browser` — actions that don't ask

Every send, post, form submit or purchase the agent tries in its browser waits
for a person to approve it. `auto_approve` lists narrow exceptions: each rule
names one site and the kinds of action there that go ahead without asking.
Edited on the agent's **Browser** tab (admins and the agent's owners only);
the walkthrough is [Actions that don't ask](#actions-that-dont-ask).
Rules apply to the agent's next action, with no restart.

```json5
browser: {
  auto_approve: [
    { site: "linkedin.com", actions: ["send"] },
  ],
},
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `auto_approve` | object[] | `[]` | The rules, at most 20, one per site. Empty means every committing action asks. |
| `auto_approve[].site` | string | **required** | A bare host name such as `linkedin.com` (covers the site and its sub-domains) or `*.example.com` (sub-domains only). No `https://`, path or port. The action must happen on this site and a form must send its data to it. |
| `auto_approve[].actions` | string[] | **required** | One or more of `send` (send, reply, post, publish, invite), `submit` (other forms that save or change something; *Submit*, *Confirm*), `signup` (subscribe, sign up, create account, book, reserve). |
| `auto_approve[].in_background` | boolean | `false` | Also apply in scheduled jobs and background (Cortex) runs. When off, the rule applies only in a conversation started by a person who owns the agent or is an admin. |

Purchases, payments, transfers, donations, deletions, approvals and merges
always ask, whatever a rule says, and no rule applies to a request from another
agent, an outside MCP/A2A caller, a webhook or a watched email.

### `observability`: tracing this agent to Langfuse

Overrides the deployment's tracing settings (`observability` in
[`config.yaml`](#configyaml-reference)) for this agent. Every field is empty by
default, which keeps the deployment's value; a value here wins. Only an admin
can change it: an agent's own tools and AgentFather refuse to, unless an admin
is the person asking.

```json5
observability: {
  langfuse: {
    enabled: true,      // trace this agent even if the deployment does not
    capture: "full",    // or "metadata"
    sample_rate: 0.2,   // one turn in five
  },
},
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `langfuse.enabled` | boolean or empty | empty | `true` traces this agent's turns, `false` never traces them, empty follows the deployment. Tracing still needs Langfuse keys. |
| `langfuse.capture` | string or empty | empty | `metadata` or `full`, with the same meaning as in `config.yaml`. Anything else reads as `metadata`. |
| `langfuse.sample_rate` | number or empty | empty | Share of this agent's turns to trace, `0.0` to `1.0`, clamped. |

An agent can also send its traces to a different Langfuse project: an admin
saves that agent's own keys in the Observability settings.

### `channels` — messaging connectors

A list of objects, each with a `type` plus that connector's own settings.
Connector fields vary widely and each is documented in the dashboard's
Connectors tab. **Credentials are never written here** — see
[Channels and connectors](#channels-and-connectors).

```json5
channels: [
  { type: "telegram", allowed_users: ["123456789"] },
  { type: "slack", channels: ["#ops"] },
]
```

Supported types: `telegram`, `slack`, `discord`, `whatsapp`, `whatsapp_twilio`,
`whatsapp_meta_api`, `signal`, `email` / `imap`, `matrix`, `mattermost`, `sms`, `homeassistant`,
`dingtalk`, `feishu`, `wecom`, `weixin`, `qqbot`, `bluebubbles`, `debox`,
`api_server`, `webhook`.

### `schedule` — recurring work

See [Scheduler and heartbeat](#scheduler-and-heartbeat).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `cron` | string \| null | `null` | Standard 5-field cron (minute, hour, day of month, month, day of week), e.g. `"0 9 * * 1-5"` for weekdays at 09:00. The day of week is standard cron: `0` or `7` is Sunday, `1` is Monday; names (`mon-fri`, `mon,thu`) work too. Shorthand for the five fields below. |
| `minute` / `hour` / `day` / `month` / `day_of_week` | string | `"*"` | The fields, when you are not using `cron`. |
| `timezone` | string \| null | `null` | IANA zone, e.g. `"America/Costa_Rica"`. Defaults to the deployment's zone. |
| `message` | string | `""` | What to send the agent when the job fires. Required for a normal job. |
| `name` | string \| null | `null` | A label for the dashboard. |
| `thread_id` | string \| null | `null` | Run in a specific conversation thread. |
| `enabled` | boolean | `true` | |
| `model` | string \| null | `null` | Run this job on a different model than the agent's, as `provider:model` (`"olano:pro"`, `"openai:gpt-6.1-sol"`). `null` follows the agent's configured model. |
| `reasoning_effort` | string \| null | `null` | Reasoning effort for this job's runs, one of the levels the model it runs on accepts (`model` when set, else the agent's). `null` uses the agent's own setting. A level the model does not accept is dropped at run time with a log line; the dashboard and the scheduler tools refuse to save one. |
| `for_each_customer` | boolean | `false` | Fan the job out: run once per end user. Requires multi-user mode. |
| `action` | string \| null | `null` | Run a named entry from `allowed_actions` instead of sending a message. Only valid with `for_each_customer`. |

In fan-out mode the message may use `{customer_key}`, `{platform}` and
`{user_id}` placeholders.

### `heartbeat` and `issues` — inherit-by-default blocks

Both use the same three-state pattern: `null` (or the key omitted) means
**inherit the deployment default from `config.yaml`**; an explicit value
overrides it for this agent only.

`heartbeat`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean \| null | `null` (inherit; on) | Whether the agent wakes up periodically to check `HEARTBEAT.md`. |
| `interval_minutes` | integer \| null | `null` (inherit; 60) | How often. Clamped to 1–1440. |

`issues`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `auto_process` | boolean \| null | `null` (inherit; on) | Whether the agent picks up issues assigned to it on its own. `false` = manual: issues wait for a person to prompt the agent. |
| `poll_minutes` | integer \| null | `null` (inherit; 15) | How often it checks. Clamped to 1–1440. |

`context_window` — how this agent's conversations are bounded inside its
model's context window. Every field is `null` = inherit the home-wide
`context_window:` block in [`context_window:` — compaction and oversized tool results](#context_window-compaction-and-oversized-tool-results),
which also explains what each one does:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `keep_fraction` | number \| null | `null` (inherit; 0.35) | Share of the window kept verbatim after a compaction, when the window is known. 0.05–0.8. |
| `keep_tokens` | integer \| null | `null` (inherit; 40000) | The same budget in tokens, used when the window is unknown. 2000–2000000. |
| `trigger_fraction` | number \| null | `null` (inherit; 0.85) | When automatic compaction fires, as a share of the window. 0.3–0.98. |
| `trigger_tokens` | integer \| null | `null` (inherit; 170000) | The same trigger in tokens, when the window is unknown. 8000–4000000. |
| `mcp_result_token_limit` | integer \| null | `null` (inherit; 5000) | MCP results larger than this are saved to a workspace file and replaced by a preview. `0` = no cap. |

### `gmail_watch`: wake on new email

Starts a turn for the agent when new mail matching a Gmail search reaches its
connected Gmail. Off by default. Set it on the agent's **Overview** tab, in the
**Gmail watch** card, which also shows the agent's Google connection and the
watch's last check; AgentFather can set it too. Changes apply immediately, no
restart.

**It needs the agent's Google connection.** The watch reads the mailbox through
the same Google sign-in the Gmail tools use (**Connections**, per agent or
shared). Without one it reads nothing: the card shows *Not connected* with a
**Connect Google** button, and the bell raises one notification that clears
itself on the first check after you connect. Mail that arrived before the
connection is not replayed. If Google refuses the saved sign-in later, the card
and the bell say so the same way. If the mailbox cannot be connected to Google
at all, add it on the **Connectors** tab as an **Email** connector instead: it
checks the mailbox over IMAP with an app password and wakes the agent for every
new message, without a search filter.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Turns the watch on. The agent needs the Google connection (Connections) with Gmail access. |
| `query` | string | `""` (watches `in:inbox`) | A Gmail search, typed exactly as in Gmail's search box, for example `from:(@broker.example) has:attachment`. |
| `poll_minutes` | integer | `5` | How often Gmail is checked, 1 to 1440. New mail is picked up within this many minutes. |
| `prompt` | string | `""` | What the agent should do with the new messages. Empty tells it to read each one, save the attachments it needs and act on its instructions. |
| `max_per_check` | integer | `10` | At most this many messages go into one turn, oldest first, 1 to 50. The rest wait for the next check. |

```json5
gmail_watch: {
  enabled: true,
  query: "from:(@broker.example) has:attachment",
  poll_minutes: 5,
  prompt: "Match each confirmation to its Trade ID and record the execution evidence.",
}
```

How it behaves:

- **Switching it on never replays your inbox.** The first check only records
  the time; mail that arrives after it is delivered. Changing `query` starts
  over the same way.
- **One message wakes the agent once.** The ids already delivered are kept
  in the workspace, so a restart or a busy inbox never hands the same message
  over twice.
- **A failed turn is retried.** If the agent's turn fails, the same messages
  are offered again on the next check, up to three times, after which they are
  skipped so one bad message cannot wake the agent forever.
- **Email is treated as a stranger's words.** The turn starts with the list of
  new messages (sender, subject, received time, ids) and the agent reads them
  with its Gmail tools. Low-risk actions go ahead; anything that needs approval
  waits in the dashboard's Approvals list, whatever the agent's security
  profile says about scheduled jobs. The turn cannot change the agent's own
  settings or use owner-only tools.
- These runs appear in the agent's chat list with its other scheduled runs.

Gmail can also push new mail instantly, but only through a Google Cloud
Pub/Sub topic you would have to create and authorize. The watch avoids that
setup at the cost of a few minutes' delay.

### `webhooks`

See [Webhooks](#webhooks) for examples of both directions.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | A label. |
| `direction` | `"outbound"` \| `"inbound"` | `"outbound"` | |
| `enabled` | boolean | `true` | |
| `url` | string | `""` | Outbound: where to POST. Required for outbound, must be `http(s)`. |
| `events` | string[] | `["*"]` | Outbound: which events to send. |
| `secret` | string \| null | `null` | Outbound: HMAC signing secret. Supports `${VAR}`. Never returned by the API. |
| `headers` | object | `{}` | Outbound: extra headers. |
| `timeout_seconds` | number | `10.0` | |
| `max_retries` | integer | `2` | |
| `rate_limit_per_minute` | integer | `60` | `0` = unlimited. |
| `verify_tls` | boolean | `true` | |
| `block_private_addresses` | boolean | `true` | Refuse to deliver to internal addresses. Leave on. |
| `prompt` | string | `""` | Inbound: what the agent should do with each payload. |
| `respond` | boolean | `false` | Inbound: run synchronously and return the agent's reply in the HTTP response. |
| `accumulate_thread` | boolean | `false` | Inbound: one persistent conversation for all payloads, rather than a fresh one each time. |
| `max_body_bytes` | integer | `256000` | Inbound: reject larger payloads. |

Outbound event names: `message.received`, `message.sent`, `agent.started`,
`agent.stopped`, `agent.error`, `task.scheduled`, `task.completed`,
`tool.called`, `approval.requested`, `budget.exceeded`, or `*` for all.

### `memory`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `markdown` | boolean | `true` | Keep `MEMORY.md` and daily logs. |
| `rag` | boolean | `false` | Also index memory for meaning-based search. |
| `rag_collection` | string | `"lionclaw_memory"` | Where indexed memory is stored. The default keeps Olano's previous name, lionclaw, so indexes built by earlier versions stay readable. |
| `self_learning` | boolean | `true` | Distil conversations into durable memory in the background. |
| `self_learning_interval` | integer | `3600` | Seconds between distillation passes. |
| `per_customer` | boolean | `false` | Give every end user their own isolated memory. Turns on multi-user mode (see [Multi-user: two different features](#multi-user-two-different-features)). |

### `customer_service` — per-end-user isolation

For a shared agent that many people talk to. See
[Multi-user: two different features](#multi-user-two-different-features).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Turn on per-end-user isolation. Mirrors `memory.per_customer`. |
| `prompt_extension` | string | `""` | Extra instructions for handling many users. Blank uses a sensible default. |
| `user_scheduling` | boolean | `false` | Let end users schedule their own reminders. |
| `max_jobs_per_user` | integer | `3` | Cap per user. |
| `min_job_interval_minutes` | integer | `60` | Minimum spacing between a user's jobs. |
| `connections` | boolean | `false` | Let end users connect to each other, with consent. |
| `max_connections_per_user` | integer | `25` | |
| `max_connection_sends_per_day` | integer | `20` | |
| `connection_invite_ttl_hours` | integer | `168` | How long an invite stays valid. |
| `connection_contact_info` | string | `""` | Shown to users in connection flows. |
| `user_credentials` | boolean | `false` | Let end users store their own credentials for the agent to use on their behalf. |
| `customer_credential_names` | string[] | `[]` | The allow-list of credential names they may store. |

`user_scheduling`, `connections`, `user_credentials` and per-customer schedule
fan-out all require multi-user mode; setting one without it makes the config
fail to load rather than silently doing nothing.

### `message_policy` — message length caps and silence rules

For an agent whose chat address is public. Everything here is off unless you
set it, so an existing agent is unaffected. Edit it in **Agents → your agent →
Multi-user → Message policy**.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `max_input_chars` | integer \| null | `null` | Longest inbound message accepted, in characters of the sender's own text. Checked before any model call, so an over-long message costs nothing. `null` = unlimited. |
| `on_oversize` | `"reject"` \| `"truncate"` \| `"ignore"` | `"reject"` | What happens past that limit. `reject` sends a short notice and runs no turn; `truncate` trims to the limit and answers the shortened text; `ignore` drops it with no reply. Only applies while `max_input_chars` is set. |
| `oversize_notice` | string | `""` | Your wording for the `reject` notice. Blank uses the built-in line, which names both the limit and the length actually sent. |
| `ignore_patterns` | string[] | `[]` | Regular expressions. A message matching any of them is dropped before any model call — free and exact. Matching is case-insensitive and unanchored; use `^…$` to require the whole message. An invalid expression makes the config fail to load rather than silently never matching. Keep them simple: a pattern with nested repeats (`(a+)+`) can take seconds per message, and only the first 20,000 characters of a message are scanned. |
| `ignore_rules` | string[] | `[]` | Plain-language conditions the **model** judges, e.g. `"The message is not written in English"`. Added to the system prompt; the agent replies with a silence marker and the runtime sends nothing. Handles what a regex cannot — language, tone, intent — at the cost of one model turn per ignored message. |
| `ignore_notice` | string | `""` | What an ignored sender sees. Blank = true silence (nothing is sent). Text = that one line instead. Applies to patterns, rules, and `on_oversize: "ignore"`. |
| `max_output_tokens` | integer \| null | `null` | Hard ceiling on one reply, translated to whichever parameter your provider uses. `null` = the provider's own maximum. |

Four things worth knowing before you rely on these:

- **Slash commands are never screened.** `/new`, `/help`, `/model` and the rest
  pass through whatever you set, so a tight limit or a broad pattern cannot
  lock you out of your own bot.
- **Only external conversations are screened** — messenger channels, dashboard
  and REST chat, and the OpenAI-compatible API. Scheduled runs, heartbeats and
  background thinking are not, so a capped support desk can still write a long
  nightly report.
- **A capped reply stops mid-sentence and is not continued.** Without a cap,
  Olano automatically asks the model to carry on when a reply is cut short;
  with one, that retry is switched off, because otherwise every capped reply
  would cost several extra turns — the opposite of what a cap is for.
- **On reasoning models the output ceiling counts thinking as well as the
  answer**, so a low value can return an empty reply. Keep it above ~1024
  there, or set `reasoning_effort` to `none`/`low`.

Patterns and rules are the cheap and the flexible halves of the same job.
Anything a regular expression can already decide belongs in `ignore_patterns`,
where it costs nothing; `ignore_rules` is for the judgements only a model can
make. Pairing rules with `max_input_chars` and `max_output_tokens` keeps the
turn they cost small.

```json5
message_policy: {
  max_input_chars: 500,
  on_oversize: "reject",
  ignore_patterns: ["^\\W*$", "\\b(buy|cheap) followers\\b"],
  ignore_rules: [
    "The message is not written in English",
    "The message is nonsense or just testing the bot",
  ],
  ignore_notice: "",          // stay completely silent
  max_output_tokens: 300,
}
```

### `monetization`

See [Subscriptions and monetization](#subscriptions-and-monetization).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | |
| `providers` | `"stripe"` \| `"telegram_stars"` list | `[]` | How people pay. |
| `trial_days` | integer | `3` | Free trial length. |
| `channels` | string[] | `[]` | Which channels the paywall applies to. |
| `gate_groups` | boolean | `false` | Also require a subscription in group chats. |
| `plan.price_cents` | integer | `500` | Display amount. |
| `plan.currency` | string | `"usd"` | |
| `plan.interval` | `"month"` \| `"year"` | `"month"` | |
| `plan.stripe_price_id` | string | `""` | Empty means Stripe is off. |
| `plan.stars_amount` | integer | `0` | `0` means Telegram Stars is off. |
| `plan.product_name` | string | `"Subscription"` | |
| `plan.offer_copy` | string | see default | The pitch shown to users. |
| `plan.success_url` / `plan.cancel_url` | string | `""` | Where checkout returns to. |

Payment provider secrets go in the vault, never in this block.

### `security`

See [Privacy and security](#privacy-and-security) for what each control actually does.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `profile` | `"safe"` \| `"developer"` \| `"production"` | `"developer"` | Preset that fills in the unset fields below. |
| `trust_level` | integer 0–4 | `2` | `0` observer, `1` assistant, `2` collaborator, `3` autonomous, `4` developer. |
| `hitl_enabled` | boolean | `false` | Require human approval before risky actions. |
| `hitl_shell` | boolean | `false` | Approve every shell command. |
| `hitl_write_tools` | boolean | `false` | Approve every tool that changes something. |
| `hitl_auto_approve_low_risk` | boolean | `true` | Let clearly-safe actions through without asking. |
| `hitl_timeout_seconds` | integer | `300` | How long a request waits before expiring. |
| `pii_scan_input` | boolean | `true` | Scan what comes in for personal data. |
| `pii_scan_output` | boolean | `true` | Scan what goes out. |
| `pii_scan_peer` | boolean | `false` | Also scan agent-to-agent traffic. |
| `pii_action` | `"warn"` \| `"redact"` \| `"block"` | `"warn"` | What to do on a hit. |
| `input_scan` | boolean | `true` | Detect prompt-injection attempts in incoming text. |
| `output_scan` | boolean | `true` | Screen outgoing text. |
| `web_scan` | boolean | `false` | Also screen fetched web content. |
| `shell_guard` | boolean | `true` | Block dangerous shell patterns. |
| `skill_audit` | boolean | `true` | Audit skills before running them. |
| `skill_audit_level` | `"permissive"` \| `"strict"` \| `"paranoid"` | `"strict"` | |
| `credential_scrub` | boolean | `true` | Strip anything that looks like a secret from output. |
| `ssrf_protection` | boolean | `true` | Block requests to internal network addresses. |
| `ssrf_allowed_domains` | string[] | `[]` | The older spelling of the top-level `egress_allow` (below). Still honoured — the two are combined. |
| `ssrf_blocked_domains` | string[] | `[]` | The older spelling of the top-level `egress_deny`. Still honoured — the two are combined. |
| `rate_limits` | object | see [Privacy and security](#privacy-and-security) | Per-tool and per-message caps. |
| `tools` | object | `{}` | Fine-grained per-tool permissions. |

Setting `profile` fills in any of `trust_level`, `pii_action`, `input_scan`,
`output_scan`, `hitl_enabled`, `hitl_auto_approve_low_risk` and
`skill_audit_level` you have not set yourself. Anything you set explicitly wins.

#### `egress_allow` / `egress_deny` — which internet destinations the agent may reach

Two **top-level** lists in `agent.json5` (not under `security`), edited in the
agent's **Advanced** tab:

```json5
egress_allow: ["api.github.com", "example.com"],   // empty = inherit the deployment's list
egress_deny:  ["pastebin.com"],                    // empty = add nothing
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `egress_allow` | string[] | `[]` | Domains this agent's web and HTTP tools may reach. Empty inherits the deployment's list (**Config → Outbound access**); when that is empty too, every public destination is allowed. A non-empty list here **replaces** the deployment's allow list for this agent. |
| `egress_deny` | string[] | `[]` | Domains this agent's tools must never reach. **Added to** the deployment's deny list — an agent can block more, never less. |

**Format.** One bare domain per entry, like `api.github.com`. A domain covers
its subdomains, so `example.com` also matches `files.example.com`. A leading
`*.`, an `https://` prefix, a port or a path are stripped when the config
loads; anything that is not a domain name or an IPv4 address fails the config
to load, the way a bad `sandbox.memory` does. Deny is checked before allow, so
a denied domain stays blocked even when it is also allowed.

**What it governs.** Everything the agent can reach the internet with:

- its web and HTTP tools, the headless browser, the HTML render tools, and
  calls to remote agents.
  For the browser that means every page a click, a link or a script opens,
  every image, script and request a page loads, and redirects: an allowed page
  that redirects to a refused site is stopped before the browser follows the
  redirect, and a page whose images or scripts come from refused sites loads
  without them;
- every integration toolkit (Stripe, Salesforce, Slack and the rest), whichever
  service they talk to;
- **its sandbox shell** — a `curl`, `pip install`, `npm install` or `git clone`
  inside a shell command.

A blocked destination comes back as a readable error (`Host 'x' is not in the
allowlist.`) rather than a silent failure or a hang, so the agent can tell you
what it could not reach. Internal and private-network addresses are blocked
regardless of these lists (`ssrf_protection`), and your deployment can always
reach its own control plane whatever you put in the lists — otherwise a typo
could take a box offline with no way to tell you.

**When it applies.** The very next tool or shell command after you save — no
restart, no container rebuild. An agent needs every service it actually uses on
its allow list: a support agent that reads email and writes to your CRM needs
both, and a missing entry shows up as that one integration failing. If an agent
suddenly cannot install a package, check whether its allow list covers the
package index it is downloading from.

**One limit worth knowing.** Confining the sandbox shell needs the host's
firewall. On a deployment where that is unavailable — an unusual setup without
the required privileges — the shell is still *pointed* at the policy and
ordinary tools obey it, but a program written to ignore that setting could go
around it. The runtime writes a warning at startup naming this, and the agent's
tools are unaffected either way. Managed deployments always have it.

### `budget`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `daily_usd` / `weekly_usd` / `monthly_usd` | number \| null | `null` | Spending caps. `null` = no cap. |
| `alert_at` | number | `0.8` | Warn at this fraction of the cap. |
| `fallback_model` | string \| null | `null` | Switch to a cheaper model when the cap is hit, instead of stopping. |
| `hard_stop` | boolean | `false` | Stop the agent entirely at the cap. |

### `pulse` — proactive suggestions

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `in_channels` | boolean | `false` | Push them to messaging channels. |
| `email_digest` | boolean | `false` | Send a daily digest. |
| `digest_time` | string | `"09:00"` | Local time for the digest. |
| `quiet_hours_start` / `quiet_hours_end` | string | `""` | e.g. `"22:00"` / `"08:00"`. Empty = no quiet hours. |
| `max_items_per_session` | integer | `5` | |
| `max_items_per_day` | integer | `20` | |
| `generated_items_per_cycle` | integer | `3` | |
| `pulse_item_ttl_hours` | number | `168.0` | How long a suggestion stays relevant. |
| `min_turns_for_generation` | integer | `4` | Don't generate from very short conversations. |
| `curation_cadence` | `"session"` \| `"daily"` \| `"weekly"` | `"daily"` | |
| `curation_interval_hours` | number | `24.0` | |
| `focus_topics` | string[] | `[]` | Bias suggestions toward these. |
| `sources` | string[] | `["agent","user","approvals","ops"]` | Where suggestions may come from. |

### `cognition` — the Cortex engine, per agent

Every boolean here is three-state: `null` (or omitted) means **inherit the
deployment default**. That includes the master `enabled` switch, so "off for
the fleet, on for one agent" works.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean \| null | `null` (inherit; off) | Master switch for this agent. `null` follows the deployment's `cognition.enabled`, which is off unless someone turned it on, so a new agent runs no Cortex. `true` runs Cortex for this agent even while the fleet is off; `false` keeps it off even while the fleet is on. |
| `fast_model` / `deep_model` | string \| null | `null` (inherit) | Models for light and heavy background work. |
| `autonomous` | boolean \| null | `null` (inherit) | Whether it acts on its own initiative. |
| `auto_approve` | boolean \| null | `null` (inherit) | Whether its own actions skip the approval queue. |
| `intensity` | `"relaxed"` \| `"balanced"` \| `"aggressive"` \| null | `null` (inherit; relaxed) | How much background work it does. `null` follows the deployment's `cognition.intensity`, `"relaxed"` unless changed. |
| `approval_required` | string[] | `["external_send","irreversible","identity_edit"]` | Categories that always need a human, whatever `auto_approve` says. `identity_edit` is any change to SOUL.md, AGENTS.md or IDENTITY.md; `external_send` is anything that sends something outside the deployment (email, calendar, posts, messages to other agents); `irreversible` is moving money or overwriting a stored secret. Applies both to the Fully Autonomous sweep of the approval queue and to tool calls made during a background run — a held item waits in **Pulse → Approvals** with its usual Approve / Reject buttons. Set it to `[]` for an agent whose owner wants everything unattended, or list only the categories to keep; unknown names are ignored. |
| `self_learning`, `brainstorming`, `idea_generation`, `prototyping`, `skills_optimization`, `workspace_optimization`, `memory_optimization`, `pulse` | boolean \| null | `null` (inherit) | Individual capabilities. |
| `deep_self_learning` | boolean | `true` | Use the deep model for distillation. |
| `memory_garden_cron` | string \| null | `null` (inherit) | When to tidy memory (merge duplicate facts in MEMORY.md, roll up old daily logs; snapshotted and undoable from Pulse). Standard 5-field cron, e.g. `"30 4 * * *"`. **Leave it `null` to follow the deployment's `intensity` preset** — once a day around 04:30 at every intensity, with the exact minute spread out per agent so a fleet does not all start at once. An explicit value is used exactly as written and is never spread. |
| `pulse_agent_cron` | string \| null | `null` (inherit) | When to generate Pulse suggestions. Standard 5-field cron. `null` follows the `intensity` preset (daily at 09:00 on `relaxed`, every 12 h on `balanced`, every 6 h on `aggressive`), spread per agent; an explicit value is used as written. |
| `idea_expand_cron` | string \| null | `null` | Reserved for scheduled idea research — currently has no effect. Ideas are expanded when you ask for it or approve a Pulse suggestion; leave this `null`. |
| `workspace_optimize_cron` | string \| null | `null` (inherit) | When the self-optimization pass over the agent's identity and behavior files runs (snapshotted and undoable from Pulse). Standard 5-field cron. `null` follows the `intensity` preset (Sundays at 04:00 on `relaxed`/`balanced`, every third day on `aggressive`), spread per agent; an explicit value is used as written. |
| `skill_librarian_cron` | string \| null | `null` (inherit) | When the learned-skill library is tidied — duplicates merged, survivors polished (snapshotted and undoable). Standard 5-field cron. `null` follows the `intensity` preset (Sundays at 04:30 on `relaxed`/`balanced`, every third day on `aggressive`), spread per agent; an explicit value is used as written. |
| `max_deep_runs_per_day` | integer | `12` | Cost ceiling on heavy background work. |
| `promote_threshold` | integer \| null | `null` (inherit; 8) | Score from 1 to 10 at or above which the Rate & Promote phase moves an item it recommends promoting to the next funnel stage on its own (brainstorm → idea → prototype → project). Only applies while Fully Autonomous (`auto_approve`) is on; otherwise every candidate waits as a card in **Pulse → Approvals**. Whole number 1 to 10; out-of-range values are pulled to the nearest end. `null` follows the deployment's `cognition.promote_threshold`. |
| `max_promotions_per_run` | integer \| null | `null` (inherit; 1) | How many items one Rate & Promote run may promote. Each promotion is a deep run and also counts against the daily cap. Whole number 0 to 5; `0` rates without ever promoting. |
| `max_self_scheduled_tasks` | integer \| null | `null` (inherit; 6) | How many follow-up steps this agent may have pending at once from `cortex_schedule`, the tool it uses to continue a prototype or project after a run (they appear in **Scheduler** named `Cortex: <item>`). Two per item and at most hourly regardless. Whole number 0 to 20; `0` turns self-scheduling off. |
| `auto_discard` | boolean \| null | `null` (inherit; on) | Whether a Fully Autonomous run may discard an item the reviewer rated 3 or lower with a "discard" recommendation. Reversible: the folder stays and **Restore** brings the item back. Has no effect while `auto_approve` is off. |
| `worker_max_iterations` | integer \| null | `null` (inherit) | Step budget for each background reasoning run (Think phases, self-learning, the optimizers). Counted in internal steps — roughly 4 per round of thinking plus tool use, so the shipped default of 100 allows about 25 rounds per run. A run that exceeds it stops with a "hit its step limit" error and its work is discarded; raise this if that error keeps appearing, lower it to cap the cost of any single run. Accepts a whole number from 10 to 400; a value outside that range is pulled to the nearest end rather than rejected. **Leave it unset (or `null`) to inherit the deployment-wide value** set in Cortex → Autonomy under "All agents" — that is what most agents should do. Caps one run — how *many* runs happen per day is governed by `intensity` and the agent's `budget` — and a run also stops at `worker_timeout_seconds`, whichever comes first. Editable in the dashboard: **Cortex → Autonomy → Step budget**. |
| `worker_timeout_seconds` | integer \| null | `null` (inherit) | Wall-clock limit for each background reasoning run, in seconds. A run that exceeds it stops with a timeout and its partial work is discarded. Accepts a whole number from 30 to 7200; the shipped default is 1200 (20 minutes). **Leave it unset (or `null`) to inherit the deployment-wide value.** Raise it whenever you raise `worker_max_iterations` — a large step budget behind a small time limit simply fails on time instead, losing the same work under a different name. Editable in the dashboard: **Cortex → Autonomy → Time limit**. |
| `force_tier` | `"fast"` \| `"deep"` \| null | `null` | Pin background work to one tier. |
| `enable_memory_gardener`, `enable_pulse_agent`, `enable_idea_expansion`, `enable_skillsmith`, `enable_workspace_optimizer`, `enable_skill_librarian` | boolean | `true` | Individual background workers. |

### `wallet` — on-chain operations

Off unless you need it.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | |
| `mode` | `"cold"` \| `"hot"` | `"cold"` | `cold` can read but not sign. |
| `chains` | string[] | `["ethereum"]` | |
| `hot_mode_lock_minutes` | integer | `30` | Auto re-lock after this long. |
| `lock_on_session_end` | boolean | `true` | |
| `spending_limits` | object | `{}` | Per-chain caps. |
| `require_approval` | `"always"` \| `"high_value_only"` \| `"never"` | `"always"` | |

### `commands` — slash commands

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | object | `{}` | Only the deviations from the defaults, e.g. `{ "model": false }`. |
| `custom` | object[] | `[]` | Your own commands. |

Each custom command has `name` (lowercase, `[a-z0-9][a-z0-9_-]*`, must not
collide with a built-in), `prompt` (supports `{args}`), and an optional
`description`. This governs chat surfaces and the dashboard.

What you set here is also **what the messengers advertise**. Each connector's
slash menu — Telegram's `/` autocomplete, Discord's slash-command list — carries
exactly the built-ins this agent exposes plus its custom commands, and `/help`
lists the same set. Edits apply **immediately, without restarting the agent**;
a messenger may cache its own autocomplete for a minute (on Telegram, reopen the
chat to refresh it), but typing a command always follows the current setting.

Two naming caveats for custom commands. Telegram's menu accepts only lowercase
letters, digits and `_`, so a hyphenated `/daily-report` still works when typed
but will not be listed there — name it `daily_report` to have it appear.
Discord accepts hyphens, but skips a name longer than 32 characters.

### `shared_paths` — sharing files between agents

| Field | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | **required** | The path on the host. |
| `target` | string | **required** | Where it appears for the agent. Normalised under `/shared/`. |
| `mode` | `"ro"` \| `"rw"` | `"ro"` | |
| `persistence` | `"session"` \| `"persistent"` | `"persistent"` | |
| `subjects` | object[] | `[{type:"agent",name:"self"}]` | Who gets it: `{type: "agent"\|"subagent"\|"all_subagents", name}`. |

### `backend` — where the agent's tools actually run

| `type` | What it is |
|---|---|
| `local_shell` **(default)** | Real filesystem plus a shell, rooted at the agent's workspace. What you want almost always. |
| `filesystem` | Files without a shell. Add `access_mode: "read"` for a read-only agent. |
| `state` | In-memory only — no real files. Testing. |
| `store` | A namespaced key-value store. |
| `composite` | Route different paths to different backends. |
| `lionclaw_docker` | Run the agent's tools in a container (the value keeps Olano's previous name, lionclaw). Options: `persistent`, `image`, `network` (`"none"` \| `"bridge"` \| `"host"`), `snapshot_on_stop`, `memory`, `cpus`. See the note below before choosing it. |
| `daytona`, `modal`, `runloop`, `agentcore`, `langsmith` | Hosted sandbox providers. |

`local_shell` and `filesystem` take `root_dir` (default `"workspace"`).

**Choosing `lionclaw_docker`.** Most agents should stay on `local_shell`: on a
managed deployment it *already* runs in the isolated container, so naming the
docker backend by hand buys nothing and costs you the shorthand the dashboard's
Environment dropdown understands. Reach for it only when you want to control the
container settings directly.

Whichever you pick, the container is only provided when the deployment has
sandbox execution enabled. Without it the agent is downgraded to files-without-a-shell
rather than being given a shell on the host — so `execute` and the file tools go
away together. If an agent's file or shell tools stop working, check
**sandbox** in the agent's diagnosis: it names the container, or the reason
there isn't one.

On a managed deployment three of the options are pinned and your value is
ignored (a warning says so at startup): `network` cannot be `"host"`, `image` is
the deployment's own sandbox image, and `persistent` is forced on. On your own
self-hosted box all three are honoured as written.

`mounts` is accepted by the config but **does nothing** — what the container
can see is the agent's workspace plus any shared paths you have configured.
`egress_allow` and `egress_deny` on this block are the older home of the agent's
outbound lists: they are still read, and combined with the top-level
`egress_allow` / `egress_deny` described in [`security`](#security), but they
govern the agent's **web tools**, not the sandbox shell — a `curl` inside
`execute` is decided by `network` and your host firewall, not by these lists.
Move them to the top level of `agent.json5`, which is where the dashboard edits
them; a config that still sets them here logs a reminder at startup.

**`sandbox` — this agent's share of the machine.** A top-level block, separate
from `backend`, that applies wherever this agent's shell runs in a sandbox:

```json5
sandbox: { memory: "8g", cpus: 4 }
```

`memory` is a size like `4g` or `2048m` (minimum `6m`); `cpus` is a number of
cores, `0.1`–`64`, where `0.5` means half a core. Set one and the other still
follows the deployment default, so raising an agent's memory does not quietly
pin its CPU. Omit the block entirely and the agent inherits the deployment
answer — normally 25% of the machine's RAM and 50% of its cores (see the
`sandbox` key in [the `config.yaml` reference](#root-keys)). Use this for
the one agent that runs heavy builds or data jobs; the limits are ceilings, so
an agent that sits idle costs nothing regardless of what you set here.
Changing a value rebuilds that agent's sandbox on its next start: its workspace
survives, anything installed by hand inside the sandbox does not.

### Runtime knobs

| Field | Type | Default | Meaning |
|---|---|---|---|
| `acp_enabled` | boolean | `false` | Expose an HTTP endpoint so external code can drive this agent. Binds to localhost with **no authentication** — reach it over an SSH tunnel, never publish the port. For an authenticated remote surface use an inbound webhook instead. |
| `acp_port` | integer | `8765` | Agents sharing a port share one server. |
| `iteration_budget` | integer | `0` | Cap on reasoning steps per turn. `0` = no cap. |
| `truncation_retry_limit` | integer | `3` | Retries when a reply is cut off. |
| `tool_error_retry_limit` | integer | `3` | Circuit breaker for a repeatedly failing tool: the turn stops once the agent has made the identical failing call (same tool, same arguments) this many times within one turn. Every failure is handed back to the agent with the tool's error output, so it can fix the arguments or the cause and try again; the breaker only stops a call that is repeated unchanged. Identical calls the agent sends together in one step count as one attempt, so the agent always sees an error before the breaker can stop it. One failure before the limit, the failing result itself carries a warning that one more identical failure ends the turn. A successful `edit_file` or `write_file` resets the count: a test command that fails, gets a fix, and fails again is a new attempt, not a repeat. A command's full output is always saved to a log under the workspace (`.olano/artifacts/shell/`) when it is too long to show inline, and the result names the path; for a failing command the inline preview keeps the error and traceback lines rather than the first and last lines. `0` disables the breaker. |

### What makes a config fail to load

Olano refuses to load a broken config rather than booting a half-configured
agent. The common causes:

- **Legacy keys.** `sandbox_backend`, a plain string `backend:`, or
  `security.sandbox_mode` / `shell_use_docker` / `docker_image` /
  `docker_network`. Use the `backend` object instead. `sandbox` is a live key
  again, but only as a block of resource limits — `sandbox: true` or
  `sandbox: "docker"` (the old on/off spelling) is still refused.
- **A utility model as a brain.** `olano:vision` or the image group in `model`,
  `cognition.fast_model` or `cognition.deep_model`.
- **Multi-user features without multi-user mode.** `user_scheduling`,
  `connections`, `user_credentials`, or a schedule entry with
  `for_each_customer`, when `customer_service.enabled` / `memory.per_customer`
  is off.
- **An invalid ignore pattern.** A line in `message_policy.ignore_patterns`
  that is not a valid regular expression — caught on save, so a rule can never
  silently match nothing.
- **A dangling archetype reference.** A `from_archetype` naming something that
  does not resolve.
- **A malformed custom command name**, or one that collides with a built-in.
- **A schedule entry with neither `message` nor `action`**, or `action` without
  `for_each_customer`.

---

## `config.yaml` reference

Deployment-wide settings at `~/.olano/config.yaml`. Almost every field here
is editable from the dashboard's **Config** section (admin only), which is the
recommended way — the forms validate as you type. A few keys have no form yet
and are edited in the file directly; each says so in its description.

**Out-of-range numbers are clamped, not rejected.** If you write
`poll_minutes: 9999` where the range is 1–1440, you get 1440 and no error.
If you write something unparseable for a field, that field falls back to its
default silently. Check the value the dashboard shows back after saving.

Most changes apply on the next tick. The exceptions — the ports and team
accounts — need a runtime restart, and the dashboard says so.

**Every key and block in this section can also be changed from an AgentFather
conversation** ("set the disk warning threshold to 85%", "turn magic links
off"). AgentFather reads the whole file, patches the field through the same
validation a dashboard save gets, and reports honestly which keys were applied
and which were ignored (with the reason) — if it says a key was ignored,
nothing changed on disk. Each change surfaces an approval card first. Secrets
never go in this file — they live in the vault.

### Root keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_agent` | string | `"merli"` | The agent the dashboard chat and the simple chat open first, and the one API calls that name no agent go to. |
| `preferred_model` | string \| null | `null` | Default model for new agents. |
| `runtime_api_port` | integer | `8765` | The runtime API port. *Restart required.* |
| `dashboard_port` | integer | `3200` | The dashboard port. *Restart required.* |
| `multi_user_enabled` | boolean \| null | `null` | **Team accounts.** Unset resolves by deployment type — on for managed deployments, off for a manual install. See [Multi-user: two different features](#multi-user-two-different-features). *Restart required.* |
| `ui_mode` | `"advanced"` \| `"simple"` | `"advanced"` | **Single-user installs only.** Which interface the operator sees: `advanced` is the full dashboard, `simple` is the stripped bot-chat app described in [The dashboard](#two-interfaces-full-dashboard-and-simple-chat). Team deployments ignore this and use each person's own **Interface** setting in **Admin → Users**. Edited in `config.yaml` directly — no dashboard form yet. |
| `highlights_all_agents` | boolean | `false` | Show highlights across every agent instead of the current one. |
| `extra_bind_roots` | string[] | `[]` | Extra host directories agents may reach. |
| `archetype_dirs` | string[] | `[]` | Extra directories to search for agent templates. |
| `egress_allow` | string[] | `[]` | Domains every agent's tools may reach — bare domains like `api.github.com`, a domain covering its subdomains. Empty means every public destination is allowed. An agent whose own `egress_allow` is non-empty uses its list instead; an agent with an empty list inherits this one. Governs every outbound request a tool makes — web and HTTP tools, the headless browser, remote-agent calls and every integration toolkit — but not the sandbox shell. Edited in **Config → Outbound access**; applies to the next tool call. See [`agent.json5` reference](#security). |
| `egress_deny` | string[] | `[]` | Domains no agent's tools may reach, same format. Checked before the allow list. Every agent's own `egress_deny` is added to this list — an agent can block more, never less. Edited in **Config → Outbound access**. |
| `sandbox_tool_sets` | string[] | `[]` | Extra tool bundles to install into the sandbox your agents run commands in. Empty means none — agents still get the standard toolkit (git, Python, Node, jq, sqlite3, ripgrep, tmux, ffmpeg, pandoc, PDF tools, and the GitHub command-line tool `gh` — present but not signed in, see [Sandbox tool bundles](#sandbox-tool-bundles)). Accepts `"chinese"` (Alibaba, Tencent, Qiniu and Baidu cloud + storage SDKs, WeChat official APIs, Bilibili, `yt-dlp`) and `"coding"` (the `claude` and `codex` coding-agent command-line tools, each needing its own sign-in). A bundle installs **once**, in the background, into a shared area every agent sees, and stays there across restarts — so it costs its download size on your disk (the `chinese` bundle is around 740 MB). Turning a bundle back off stops future installs but does not delete what it already installed. Edited in `config.yaml` directly — no dashboard form yet. See [Sandbox tool bundles](#sandbox-tool-bundles). |
| `sandbox` | object | `{}` | Resource ceilings for the sandbox each agent runs shell commands in: `memory` (a size like `4g` or `2048m`, minimum `6m`) and `cpus` (cores, `0.1`–`64`; `0.5` is half a core). Leave either unset for the automatic limit — 25% of the machine's RAM and 50% of its cores, never below 512m and half a core — so a bigger machine gives every agent more room with nothing to configure. These are ceilings, not reservations: an idle agent uses nothing. A single agent can override both in its own `agent.json5`, and the `LIONCLAW_SANDBOX_MEMORY` / `LIONCLAW_SANDBOX_CPUS` environment variables (also accepted as `OLANO_SANDBOX_MEMORY` / `OLANO_SANDBOX_CPUS`; lionclaw is Olano's previous name) override everything. Edited in **Config → Sandbox resources**. Changing a limit rebuilds each agent's sandbox on its next start: workspaces and installed tool bundles are kept, anything installed by hand inside the sandbox is not. |
| `checkpoint_backend` | `"postgres"` \| `"sqlite"` | `"postgres"` | Where conversation state is stored. |
| `checkpoint_db_url` | string \| null | `null` | Required when using `postgres`. |
| `store_backend` | `"postgres"` \| `"sqlite"` | `"postgres"` | |
| `store_db_url` | string \| null | `null` | |
| `mcp_relay_url` | string \| null | `null` | Central relay for MCP sign-ins. |
| `instance_id` | string \| null | `null` | This deployment's identifier. |

Selecting `postgres` (including by leaving the default) **without a URL is an
error**, not a silent fallback to SQLite. Set the URL, or switch the backend
to `sqlite` for a zero-configuration single-machine install.

### Sandbox tool bundles

When an agent runs a shell command on a managed deployment, it runs inside an
isolated sandbox rather than on the machine itself. That sandbox comes with a
standard toolkit — git, Python and pip, Node and npm, `jq`, `sqlite3`,
ripgrep, `tmux`, `ffmpeg`, `pandoc`, PDF tools, `zstd`, and the GitHub (`gh`)
and Google (`gog`) command-line tools, present but not signed in (see below) —
which covers most of what agents actually do. (`tmux` is there so a long job
outlives the command that started it: an agent can start a build in a detached
session and read its output back on a later turn, and you can do the same from
the Terminal tab without a browser reconnect killing it. `zstd` is there so
`tar --zstd` can unpack the `.tar.zst` archives many projects now ship
releases as.) The everyday shell commands are there too: `zip` and `unzip`,
`7z`, `.tar.xz` and `.tar.bz2` support, `file`, `less`, `tree`, `ps`, `top`,
`htop`, `free`, `pkill`, `ip`, `ping`, `dig`, `nc`, `ssh`, `scp`, `rsync` and
`make`. Both `python` and `python3` work, so a command copied from
documentation runs as written — and that holds on every kind of install, not
just inside the sandbox: `python` and `pip` always resolve for an agent's shell
commands, with a `python3`-only system simply forwarding to `python3`. The
Terminal tab's shell sees the same tools an agent does (anything an agent
`pip install`s or `npm install -g`s is on its PATH), and ships `vim` and `nano`
so you can open and edit files there directly.

Two heavier bundles are available but **off by default**, because each one
costs real disk on your machine and most deployments never touch them:

| Bundle | What it adds | Rough size |
|---|---|---|
| `chinese` | Alibaba, Tencent, Qiniu and Baidu cloud + storage SDKs, the Tencent Cloud CLI, WeChat official APIs (Official Account, WeCom, Pay, MiniProgram), Bilibili, `yt-dlp` | ~740 MB |
| `coding` | The `claude` and `codex` coding-agent command-line tools | ~620 MB |

There are two ways to turn one on. The easiest, and the only one that needs no
file access, is to **ask AgentFather**: "enable the coding tool set for the
sandbox" — it patches the deployment config for you and tells you what it did.
Or edit `sandbox_tool_sets` in `~/.olano/config.yaml` yourself (this key has
no form in the dashboard's Config section yet):

```yaml
sandbox_tool_sets:
  - chinese
  - coding
```

The install runs once, in the background, the next time an agent starts a
sandbox — nothing blocks and nothing restarts. It lands in a shared area that
every agent on the deployment sees and that survives restarts and updates, so
you pay for it once.

Two things worth knowing. The coding command-line tools each need their own
sign-in (a Claude subscription or API key; a ChatGPT or OpenAI login) —
installing them does not authenticate them. And removing a bundle from the list
stops future installs but leaves what it already installed in place; Olano will
not delete tools you may still be using. If you want that space back, clear the
bundle's folder from the **Files** tab under `sandbox/tools`.

Agents can also install packages themselves at any time (`pip install`,
`npm install -g`) — those land in the same shared area, and show up under
**Olano home** in the Overview disk card.

**Command-line tools in the sandbox are not signed in, and Olano never signs
them in.** The sandbox holds no credential — not the GitHub connection, not a
vault key, nothing — so `gh`, `gog`, and anything you or an agent install
there start out logged out and stay that way. That is deliberate: it is what
keeps a stray command from reading a token it should never see. Whatever is
installed in the sandbox beyond the standard toolkit is yours: unsupported,
unauthenticated, and used at your own risk. You may sign a tool in yourself
from the **Terminal** tab (`gh auth login`, say); that sign-in is your own, it
is stored inside the sandbox only, and it lasts until the sandbox is rebuilt —
a resource-limit change or an update does that. An agent will never do it for
you and will never ask you for a token to do it. For everything the sandbox's
tools would have reached, use the built-in integrations instead: the GitHub
tools for repositories, issues and pull requests, the `git` toolkit for
cloning and pushing private repositories ([`agent.json5` reference](#tools)), and the
**Connections** page (account menu → Setup) to sign in once for all of them.

### `cognition:` — Cortex defaults for the fleet

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Master switch for the Cortex engine, off until you turn it on. While it is off, an agent that does not set its own `cognition.enabled` runs no background cognition at all: no scheduled phases, no distilling of conversations into memory, and no `cortex_*` tools in chat. Agents can override in either direction (`true` runs Cortex for that one agent while the fleet stays off). Editable in **Cortex → Autonomy → Cortex engine**, "All agents" column. |
| `autonomous` | boolean | `true` | Whether agents act on their own initiative. |
| `auto_approve` | boolean | `true` | Whether background work skips the approval queue. |
| `intensity` | `"relaxed"` \| `"balanced"` \| `"aggressive"` | `"relaxed"` | How much background work: each phase's schedule and the daily cap on deep runs (6 / 12 / 24). An unknown value reads as `"relaxed"`. Editable in **Cortex → Autonomy → Cadence**, "All agents" column. |
| `fast_model` | string | see note | Model for light background work. |
| `deep_model` | string \| null | see note | Model for heavy background work. |
| `worker_max_iterations` | integer | `100` | Step budget for one background reasoning run, for every agent that does not set its own. Counted in internal steps — about 4 per round of thinking plus tool use, so 100 allows roughly 25 rounds. A run that exceeds it stops with "hit its step limit" and its work is discarded. Whole number from 10 to 400; out-of-range values are pulled to the nearest end. Editable in **Cortex → Autonomy → Step budget**, "All agents" column. |
| `worker_timeout_seconds` | integer | `1200` | Wall-clock seconds one background reasoning run may take, for every agent that does not set its own. Whole number from 30 to 7200 (default 1200 = 20 minutes). Raise it alongside `worker_max_iterations` — a large step budget behind a small time limit just fails on time instead. Editable in **Cortex → Autonomy → Time limit**, "All agents" column. |
| `promote_threshold` | integer | `8` | Score (1 to 10) at or above which the Rate & Promote phase promotes an item it recommends promoting to the next funnel stage on its own, for every agent that does not set its own. Applies only under Fully Autonomous; with `auto_approve` off every candidate waits in **Pulse → Approvals**. Out-of-range values are pulled to the nearest end. |
| `max_promotions_per_run` | integer | `1` | Items one Rate & Promote run may promote (each is a deep run, counted against the daily cap). 0 to 5; `0` rates without promoting. |
| `max_self_scheduled_tasks` | integer | `6` | Follow-up steps an agent may hold pending at once from `cortex_schedule` (the Scheduler tab shows them as `Cortex: <item>`). 0 to 20; `0` turns self-scheduling off fleet-wide. |
| `auto_discard` | boolean | `true` | Whether Fully Autonomous runs may discard items rated 3 or lower with a "discard" recommendation. Reversible from the card's **Restore**. |
| `worker_max_concurrency` | integer | `2` | How many background reasoning runs may be talking to a model at the same time across the whole deployment — every agent's Cortex phases, self-learning and Pulse actions share the pool. A run that finds the pool full waits its turn; the wait does not count against its time limit. Whole number from 1 to 16; out-of-range values are pulled to the nearest end. Raise it on a large fleet with generous provider limits; lower it to 1 if a provider keeps answering "rate limit". Ordinary chat is never held by this. |
| `features.self_learning` | boolean | `true` | Distil conversations into memory. |
| `features.brainstorming` | boolean | `true` | |
| `features.idea_generation` | boolean | `true` | |
| `features.prototyping` | boolean | `true` | |
| `features.skills_optimization` | boolean | `true` | |
| `features.workspace_optimization` | boolean | `true` | |
| `features.memory_optimization` | boolean | `true` | |
| `features.pulse` | boolean | `true` | |

On a managed deployment, leaving `fast_model` and `deep_model` unset resolves
them to `olano:turbo` and `olano:pro` — so Cortex works out of the box on
credits with no provider account. Setting either explicitly always wins;
clearing it in the dashboard returns it to the deployment's own default rather
than to a hardcoded provider model.

**Resolution rule for every cognition setting:** the agent wins when it has an
opinion, the deployment applies when it doesn't.

### `default_models:` — where the `default:*` aliases point

| Key | Type | Default | Meaning |
|---|---|---|---|
| `turbo` | string | unset | The concrete `provider:model` behind `default:turbo`. |
| `pro` | string | unset | The concrete `provider:model` behind `default:pro`. |
| `max` | string | unset | The concrete `provider:model` behind `default:max`. |

Editable in **Config → Default models**. An unset (or cleared) key falls back
to the deployment's own built-in target — the matching Olano tier on a managed
deployment, a bring-your-own-key model elsewhere. A target must be a concrete
model: another `default:*` alias is rejected (aliases never chain), and so are
the utility groups. Changes apply to each agent on its next boot or reload.
See [Deployment default models (`default:*`)](#deployment-default-models-default) for how the aliases behave.

### `heartbeat:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Agents wake periodically to check `HEARTBEAT.md`. |
| `interval_minutes` | integer | `60` | Clamped 1–1440. |

### `issues:` — how agents pick up work

| Key | Type | Default | Meaning |
|---|---|---|---|
| `auto_process` | boolean | `true` | Agents pick up issues assigned to them automatically. |
| `poll_minutes` | integer | `15` | Clamped 1–1440. |

### `revisions:` — automatic version history

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Keep automatic version history. |
| `include_memory` | boolean | `true` | Include memory files. Turn off if you have data-deletion obligations — memory files carry distilled personal data. |
| `debounce_seconds` | integer | `45` | How long to wait for edits to settle before committing. Clamped 5–3600. |
| `max_file_kb` | integer | `51200` | Size limit for a tracked file, in KB. The default is 50 MB: GitHub warns about any file larger than that, and every version of a large file stays in history. The maximum is 100 MB (`102400`), because GitHub refuses any file larger than 100 MB. A file over it keeps its last recorded version in history (it is not recorded as deleted, and is not removed from GitHub) until it is back under the limit; the **Revisions** page lists such files. Clamped 16 to 102400. |
| `remote` | mapping | not connected | The GitHub repository the history syncs with (below). |

`revisions.remote:` is written by **Revisions → GitHub sync** when you connect a
repository. Nothing in it is secret, which matters because `config.yaml` is
itself part of the history that gets pushed: the access token lives encrypted in
the vault, never here.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `url` | string | `""` | The repository, as `https://github.com/<owner>/<name>.git`. Empty means not connected. |
| `branch` | string | `main` | The GitHub branch pushed to and pulled from. |
| `auth` | `token` \| `connection` | `token` | `token`: the access token pasted when connecting. `connection`: the GitHub account connected on the **Connections** page. |
| `sync` | `manual` \| `daily` \| `on_commit` | `manual` | When new commits are pushed on their own. `manual`: only with **Push**. `daily`: at most once every 24 hours, when there is something new. `on_commit`: right after each commit. Automatic pushing never forces; pulling is always manual. Unknown values read as `manual`. |

### `push:`, notifications to the mobile app

Notifications to the Olano app on people's phones. They are delivered through
the Olano Cloud, so they work on a deployment that runs on Olano Cloud and not
on a self-hosted one. Each person chooses on their own phone which kinds they
receive; these keys are the deployment-wide limits. See
[The mobile app](#the-mobile-app).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Send notifications at all. Takes effect at the next restart; phones that registered stay registered and receive nothing while it is off. |
| `previews` | boolean | `false` | Put the start of a reply, or a Cortex finding's title, into the notification. Off, a notification names the agent and the reason ("Replied to your message.") and nothing anyone wrote. On, that text passes through Apple's or Google's notification service on its way to the phone. Applies to the next notification. |
| `cortex_min_priority` | `"urgent"` \| `"normal"` \| `"low"` | `"urgent"` | The lowest Pulse priority that notifies. `urgent` notifies only for urgent findings; `normal` adds ordinary ones; `low` notifies for every finding. Cortex proposals waiting for approval notify whatever this says. An unknown value means `urgent`. |
| `alert_min_level` | `"warn"` \| `"error"` | `"warn"` | The lowest bell-notification level that notifies administrators' phones. `warn` includes warnings and errors; `error` only errors. Each distinct problem notifies at most once every six hours. An unknown value means `warn`. |

### `mobile_app:`, what the mobile app shows

How the Olano app looks to everyone who signs in to this deployment on a phone,
administrators included. Edit it in the dashboard under **Config, Mobile app**.
Phones download it when the app opens and every time it comes back to the
foreground, so a change reaches a phone the next time it is opened, with no app
update and no restart. Every switch defaults to shown, so a deployment that
never sets the block looks as it always has. Hiding only removes an entry from
the phone: the dashboard and everything behind it keep working, and nothing is
deleted. See [The mobile app](#what-your-administrator-chooses).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `cortex_tab` | boolean | `true` | Show the **Cortex** tab. Off, the tab is removed and a phone on a Cortex screen moves to Chats. |
| `inbox.enabled` | boolean | `true` | Show the **Inbox** tab. Off, the tab and its badge are removed whatever the three keys below say. The tab is also removed when all three are off. |
| `inbox.approvals` | boolean | `true` | The Inbox's **Approvals** section (actions paused for a person, Cortex items waiting for approval). Off, it is hidden and stops counting toward the badge. |
| `inbox.alerts` | boolean | `true` | The Inbox's **Alerts** section (the deployment's notifications). Off, it is hidden and unread alerts stop counting toward the badge. |
| `inbox.issues` | boolean | `true` | The Inbox's **Issues** section. |
| `settings_tab.files` | boolean | `true` | The **Files** row in the app's Settings, and the Files entries on agent screens. Files an agent names in a reply still open from the chat. |
| `settings_tab.credits_usage` | boolean | `true` | The **Credits and Usage** row in the app's Settings (shown only on deployments that bill in credits). |
| `settings_tab.transcribe_on_phone` | boolean | `true` | The **Transcribe on this phone** switch in the app's Settings. Off, every phone uses `transcribe_on_phone_default`, including phones where someone had chosen. |
| `transcribe_on_phone_default` | boolean | `false` | Where a voice note becomes words when the person has not chosen. `false`: the recording goes to the deployment's speech-to-text. `true`: a phone that can recognize speech (an iPhone with permission granted) transcribes it and sends only the words; any other phone still sends the recording. |

An unknown key or a value that is not `true` or `false` is refused when saved
from the dashboard; in a hand-edited file it is ignored.

### `usage_budgets:` — token ceilings and budget warnings

Every deployment carries six usage budgets: four token ceilings (input and
output, each per-agent and deployment-wide) and two cost ceilings. A turn is
**refused** once one is exhausted, and the refusal names the budget that
stopped it. All six are shown in **Config → Usage budgets** with their current
usage, and in **Admin → Deployment** alongside the rest of your entitlements.

The token ceilings exist to stop a runaway loop, not to meter you, so this
block lets you move them. Note what they count: **every token an agent spends,
including calls made on your own provider keys** — so a token budget can stop a
bring-your-own-key agent even though nothing was billed to Olano. If that is
not what you want for a particular agent, raise or lift the ceiling here.

The two cost budgets are **not** listed below and cannot be set here or in the
file: they cap what Olano itself pays upstream, and they come down with your
plan. Managed spend is separately limited by your credit balance, so lifting a
token ceiling cannot make a deployment spend credits it does not have.

Each token key takes three shapes, and they mean different things:

| You write | It means |
|---|---|
| *(key absent)* | Use your plan's ceiling. This is the default. |
| a number, e.g. `50000000000` | Use this ceiling instead. |
| `unlimited` | No ceiling at all for this budget. |

The long form `{value: 50000000000, window: monthly}` is also accepted if you
want a different reset window (`daily`, `weekly`, `monthly`, `lifetime`);
windows reset in UTC — daily at midnight, weekly on Mondays, monthly on the 1st.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `input_tokens_per_agent` | integer \| `unlimited` | plan's | Input-token ceiling for any single agent. |
| `input_tokens_total` | integer \| `unlimited` | plan's | Input-token ceiling across the whole deployment. |
| `output_tokens_per_agent` | integer \| `unlimited` | plan's | Output-token ceiling for any single agent. |
| `output_tokens_total` | integer \| `unlimited` | plan's | Output-token ceiling across the whole deployment. |
| `alerts_enabled` | boolean | `true` | Raise a dashboard notification as a budget fills. Turning this off silences **every** budget notification, including the one saying a budget is spent and turns are being refused. |
| `alert_percents` | integer[] | `[50, 60, 70, 80, 90, 95, 99]` | Percentages that raise a notification. Each fires once per budget per window; a turn that jumps several at once raises only the highest. Values must be 1–99 — 100% is always announced and needs no entry. Applies to all six budgets, cost included. |

```yaml
usage_budgets:
  input_tokens_per_agent: 50000000000
  output_tokens_total: unlimited
  alert_percents: [50, 75, 90, 99]
```

On an unmanaged install nothing is counted or enforced at all, and the Config
card says so — the ceilings only apply once a deployment is licensed.

### `disk_monitor:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `path` | string | `"/"` | Which filesystem to watch. |
| `check_interval_minutes` | integer | `30` | Clamped 5–1440. |
| `warn_percent` | integer | `80` | Warn at this usage. Clamped 50–99. |
| `cleanup_percent` | integer | `85` | Start cleaning at this usage. Clamped 50–99, and never below `warn_percent`. |

`disk_monitor.cleanup:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `actions` | string[] | all actions | Which cleanups may run — everything is on by default. `olano_caches`, `revisions_gc`, `olano_stale_images`, `docker_image_prune`, `npm_cache`, `journald_vacuum` are safe (caches and old logs only); `docker_prune` is more aggressive (removes ALL unused Docker images/containers) — untick it if other software on the server relies on cached Docker data. Unknown names are ignored; the old name `lionclaw_caches` (lionclaw is Olano's previous name) still works. |
| `cooldown_minutes` | integer | `360` | Clamped 30–10080. |
| `force_interval_days` | integer | `0` | Run periodically regardless of usage. `0` = only on threshold. Clamped 0–90. |
| `journald_vacuum_size` | string | `"200M"` | Size to keep, e.g. `500M`, `2G`. |

`disk_monitor.scan:` — the daily "what is using my disk" report.

| Key | Type | Default | Range |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `interval_hours` | integer | `24` | 1–168 |
| `depth` | integer | `2` | 1–4 |
| `agent_depth` | integer | `2` | 0–4 |
| `time_budget_seconds` | integer | `600` | 30–3600 |
| `throttle_ms` | integer | `20` | 0–1000 |
| `max_entries` | integer | `400` | 50–2000 |

### `mcp_health:` — keeping MCP connections alive

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `check_interval_minutes` | integer | `10` | Clamped 1–1440. |
| `refresh_tokens` | boolean | `true` | Renew sign-ins before they expire. Leave this on — a token that lapses while the agent is idle can become unrecoverable. |
| `refresh_window_minutes` | integer | `20` | How far ahead of expiry to renew. Clamped 1–720. |
| `auto_reconnect` | boolean | `true` | Retry servers that timed out or errored, with backoff. A server that needs a sign-in you have not given is never retried — reconnecting cannot invent consent. |

### `checkpoint_gc:`, trimming conversation history on disk

Conversation state is saved as a chain of checkpoints, one per step an agent
takes, and nothing in the engine ever deletes an old one. A reply only ever
needs the newest few, so the runtime sweeps the rest: after boot and then on a
timer it removes, per conversation, every checkpoint older than the last full
snapshot and the data only those checkpoints referenced. Conversations keep
working exactly as before; what goes is history nothing can read back. Only
the Postgres backend is swept; the SQLite backend is left alone. The sweep
re-reads this block on every pass, and each pass is recorded in the runtime
log with what it removed.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Master switch. Off, history grows without bound; a long-running deployment once reached 40 GB of it. |
| `interval_hours` | integer | `6` | How often a pass runs after the first. Clamped 1–168. |
| `boot_delay_minutes` | integer | `10` | How long after the agents start the first pass waits, so it never competes with the boot. Clamped 0–1440. |
| `keep_min` | integer | `3` | Checkpoints always kept per conversation, whatever the snapshots say. Clamped 1–1000. |
| `max_minutes` | integer | `30` | Budget per pass; a pass that runs out stops between conversations and the next one continues, largest first. Clamped 1–1440. |

Edited from **Config → Conversation history cleanup**.

### `scheduler:`, how late a scheduled job may still run

A scheduled job is due at an instant, and the runtime is not always free at
that instant: a long turn, a heavy page load or a restart can hold it past the
minute. This block says what happens then. Inside the window the job runs late
and its message says by how much; past it the firing is recorded as *skipped*
in the job's history and a notification names it. Read when a job is
registered, so a change applies on the next restart or the next time the job
is saved. Applies to the Scheduler tab's tasks and to `schedule` entries in
`agent.json5`; heartbeat polls and Cortex phases keep a short fixed window. See
[Scheduler and heartbeat](#scheduler-and-heartbeat).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `misfire_grace_minutes` | integer | `360` | How late a firing may still run. Clamped 1–1440. Under the old fixed five minutes, a busy morning lost the day's run outright. |
| `catch_up_on_boot` | boolean | `true` | At startup, run a job whose most recent due time fell inside the window with no run recorded since (the runtime was down or restarting at the time). `false` lets a restart drop it, as before. |
| `max_run_minutes` | integer | `180` | The wall clock one scheduled run (or heartbeat poll) may use. A run still going at the limit is stopped and recorded as failed with the reason; what it wrote is kept and the job's next firing runs. Only one run of a job executes at a time, so without a limit a run that never ended held its job forever. `0` removes the limit. Clamped 0–1440. |

### `peer_threads:`, how much two agents carry between them

When one agent asks another for something with `talk_to_agent`, the pair shares
one conversation thread, so the second agent remembers what the first already
asked. Left unbounded that thread grows for the life of the deployment and every
exchange carries all of it. These bounds retire the shared thread and start a
fresh one; the old transcript stays readable in the Agent to Agent view.
Agent-to-agent exchanges are mostly one-shot requests, so the defaults are
short. Read at every exchange, so a change applies to the next one. Edited from
**Config → Agent-to-agent conversations**.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `rotate` | boolean | `true` | Master switch. Off, a pair keeps its one thread forever and the two bounds are ignored. |
| `max_age_days` | integer | `3` | A pair's thread is retired once its first exchange is this many days old. Clamped 1–365. |
| `max_turns` | integer | `200` | The same, once the thread's transcript holds this many rows. A row is one message, so an exchange is about two. Clamped 10–100000. |

### `pages:`: deployed pages

How pages published with `pages_deploy` are built, run and reached. Plain pages
published with `pages_start` only serve files and ignore this block. Read again
every few seconds, so changes apply without a restart. Edited from **Config →
Deployed pages**. See [Pages](#pages-publishing-sites-and-web-apps) for the whole feature.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `services_enabled` | boolean | `true` | Master switch. `false` runs no build, stops every page server within a few seconds (those pages answer `503`), and makes `pages_deploy` refuse. Static releases already built keep serving. |
| `max_services_per_agent` | integer | `5` | Server pages one agent may run at once. A deploy over the limit is refused, naming the agent's running servers. Static builds do not count. Clamped 1–50. |
| `health_interval_seconds` | integer | `30` | How often each running server's health path is requested. Any answer below `500` is healthy; three failures in a row restart the server. Clamped 5–3600. |
| `request_timeout_seconds` | integer | `120` | The longest a viewer's request waits for the server: for the first byte, and between chunks of a streamed answer. Past it the viewer gets `504`. Clamped 5–3600. |
| `max_request_body_mb` | integer | `25` | The largest request body forwarded to a server; bigger uploads get `413`. Clamped 1–1024. |
| `max_concurrent_requests` | integer | `64` | Requests in flight per server. Past it viewers get `503` with `Retry-After` instead of an unbounded queue. Applies the next time a server starts. Clamped 1–1024. |

### `browser:` (browser control)

Whether agents get the browser tools at all, and where the browser runs. Off by
default. Turning it on from the dashboard (Config → Browser) checks that the
machine can carry the mode first and refuses with the reason if not; a value
written into the file by hand that the machine cannot carry is applied one step
lower (live → headless → off) and the Config card shows why. Changes apply
within a minute: open browsers of the old mode are closed (sign-ins saved).
See [Browser control](#browser-control) for what each mode does.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | string | `off` | `off`: no agent has a browser. `headless`: an invisible browser, one isolated session per agent (needs 1.5 GB of memory). `live`: a full browser per agent in its own container that you can watch and take over (needs 2 CPUs, about 4 GB, Docker, and a plan that includes it; the browser is downloaded when you turn it on). Anything else reads as `off`. |
| `provider` | string | `local` | `local`: the browser runs on this machine. `remote`: agents connect to a hosted browser service at the DevTools address stored in the vault as `BROWSER_REMOTE_CDP_URL`; no browser memory is used here, and the live view is not available. |
| `max_sessions` | integer | `1` | How many agents may hold an open browser at once. Clamped 1–8. At the limit, the session idle longest is closed to make room. |
| `idle_timeout` | integer | `10` | Minutes without browser activity before an agent's browser is closed (sign-ins saved first). Clamped 1–240. A browser someone is watching is not closed for being idle. |
| `timezone` | string | empty | The time zone every agent browser reports to websites, as a name such as `Europe/Madrid`. Empty: this machine's own clock. Set it to where the browser's proxy (`BROWSER_PROXY_URL` in the vault) has its address, so the two agree. A name this machine does not know reads as empty. |

### `observability:` (tracing to Langfuse)

Sends every agent turn to a [Langfuse](https://langfuse.com) project you run or
subscribe to, as one trace: the turn, each model call and each tool call under
it, with timings, token counts and credits, grouped by conversation. Off by
default. Langfuse runs outside this deployment (Langfuse Cloud or your own
server); nothing extra is installed here.

This block is the one exception to the AgentFather rule above: an agent cannot
change it, because it decides whether conversations leave this deployment. An
admin changes it in the dashboard. The Langfuse keys never go in this file:
they are kept in the vault, hidden from agents and from the Vault page, and
saved through the Observability settings. If no keys are saved there, the
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` environment
variables of the server are used. Keys alone never turn tracing on.

```yaml
observability:
  langfuse:
    enabled: true
    host: https://jp.cloud.langfuse.com
    capture: metadata
    sample_rate: 1.0
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `langfuse.enabled` | boolean | `false` | Trace this deployment's agent turns. An agent can override it either way (`observability` in `agent.json5`). Needs Langfuse keys, or nothing is traced. |
| `langfuse.host` | string | empty | The Langfuse address: `https://cloud.langfuse.com` (EU), `https://us.cloud.langfuse.com`, `https://jp.cloud.langfuse.com`, or your own server. Langfuse Cloud keys only work in the region they were created in. Empty uses the address saved with the keys, then the EU region. Must be `https://` (plain `http://` only for a server on this machine); anything else reads as empty. |
| `langfuse.capture` | string | `metadata` | What each trace contains. `metadata`: the structure of the turn, timings, token counts, credits, model and tool names, and errors as a level only; no message, tool argument, tool result or error text, and the person's id is replaced with a stable pseudonym. `full`: also messages, tool arguments and results and error text, so the trace shows exactly what the agent saw and said. In both modes every secret the agent could see (vault values, keys in its settings, credential-shaped text) is replaced with `[secret]`, and a managed Olano model is shown by its tier name. Anything but `full` reads as `metadata`. |
| `langfuse.sample_rate` | number | `1.0` | Share of turns traced, from `0.0` to `1.0`. A turn is traced whole or not at all. Clamped to the range. |
| `langfuse.environment` | string | empty | An environment name shown in Langfuse (`production`, `staging`): lowercase letters, digits, `-` and `_`, up to 40 characters, not starting with `langfuse`. Empty sends none. |
| `langfuse.trace_background` | boolean | `true` | Also trace background work (Cortex runs). Scheduled jobs and conversations are traced either way. |
| `langfuse.project_id` | string | empty | Written by the dashboard when you test the connection, and used to link from a conversation to its trace. You do not need to set it by hand. |

Tracing never slows down or breaks a turn: traces are sent in the background,
and if Langfuse cannot be reached the turn goes ahead and the Observability
settings show the error. Deleting a conversation here also deletes its traces
in Langfuse, in the background, with the keys that agent's traces were sent with.

Thumbs up and down on a traced reply in the chat are sent as a `user_feedback`
score on that reply's trace (see [Chat → Rating a reply](#rating-a-reply));
the comment only when `capture` is `full`. The votes themselves are kept on the
deployment whether or not tracing is on.

### `context_window:` — compaction and oversized tool results

A conversation cannot grow past the model's context window, so two things
bound it. **Compaction**: when a thread reaches the *trigger*, everything older
than the *keep* budget is summarised into one paragraph, the full text of what
was summarised is saved under the agent workspace's
`.olano/conversation_history/` folder (the agent can read it back with its
file tools), and from then on the model sees the summary plus the kept tail.
`/compact` in the chat does the same on demand; it becomes available at half
the trigger and reports how many messages it folded. A model can only remember
what is in the kept tail, so an agent asked "which tools did you use?" right
after a compaction answers from the summary. **MCP result offload**: a result
from an MCP server larger than the limit is saved to
`.olano/large_tool_results/<call id>` in the workspace and replaced in the
conversation by a head-and-tail preview plus the file path; the agent reads
the rest a part at a time. One page of search results is roughly the default
limit, so a search-heavy task no longer spends a quarter of the window on one
call.

The keep and trigger each have two spellings: a *share of the window* when
the model's window is known (the model catalog lists it, or the provider
reports it), and an *absolute token count* used only when it is not (a model
newer than the catalog, a self-hosted one). Both keep values must be below
their trigger; the block corrects a keep at or above it, the dashboard refuses
it. Changes reach an agent at its next start or reload. Every field can be
overridden per agent ([`agent.json5` reference](#heartbeat-and-issues-inherit-by-default-blocks))
and edited in **Config → Context window**.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `keep_fraction` | number | `0.35` | Share of the window kept verbatim after a compaction. Clamped 0.05–0.8. Lower frees more per compaction; higher keeps more of the session. |
| `keep_tokens` | integer | `40000` | The keep budget in tokens, when the window is unknown. Clamped 2000–2000000. |
| `trigger_fraction` | number | `0.85` | Share of the window at which compaction fires automatically. Clamped 0.3–0.98. |
| `trigger_tokens` | integer | `170000` | The trigger in tokens, when the window is unknown. Clamped 8000–4000000. Keep it below the smallest window your unknown models really have. |
| `mcp_result_token_limit` | integer | `5000` | MCP results above this many tokens (about four characters each) are saved to a workspace file. The conversation keeps the file path and a preview of about 1,000 tokens that follows the content: JSON keeps its structure with long lists sampled; tables and logs keep the header, the first and last lines, every error line and one example of each kind of line, with a count of what was left out. `0` turns the cap off. Clamped 0–200000. Results from Olano's own tools use a separate 20000-token offload. |

### `remote_agents:` — fleet rules for talking to outside agents (A2A)

The per-agent `remote_agents` list ([`agent.json5` reference](#remote_agents-external-agents-over-a2a))
decides *which* external agents an agent may reach; this block sets the rules
every such call obeys.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Master switch. Off ⇒ no agent can reach any remote agent; the tools answer with an explanatory error rather than disappearing. |
| `allow_ad_hoc_discovery` | boolean | `false` | Whether `a2a_discover_agent` may read the Agent Card of a URL that is **not** in the calling agent's list. Sending a message is never ad hoc, in any mode. |
| `max_reply_chars` | integer | `20000` | Replies longer than this are truncated before the agent reads them. Clamped 500–500000. |
| `max_message_chars` | integer | `20000` | Longer outbound messages are refused. Clamped 500–500000. |

### `external_access:` — letting outside systems reach your agents

Home-wide switches and policy for the **inbound** surfaces: the MCP endpoint
at `<public URL>/api/mcp`, the per-agent A2A endpoints at
`<public URL>/api/a2a/agents/<agent>`, and Olano's own OAuth sign-in for
clients that cannot paste a token. Both surfaces are **off by default**; each
agent still has to opt in on its **External access** tab
([`agent.json5` reference](#external_access-letting-outside-systems-reach-this-agent)). Edited
from **Config → External access**, which also lists the tokens and OAuth
clients. See [Beyond the deployment: A2A and MCP](#beyond-the-deployment-a2a-and-mcp).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mcp.enabled` | boolean | `false` | Serve the MCP endpoint. Off answers 404 to everyone whatever the agents say. Applies immediately. |
| `a2a.enabled` | boolean | `false` | Serve the A2A endpoints. Off answers 404. Applies immediately. |
| `require_https` | boolean | `true` | Refuse to serve any of this when the deployment's public URL is plain http (tokens travel in a header). Loopback and private-network URLs are exempt for development. |
| `token_ttl_days` | integer | `90` | Default lifetime of a bearer token minted without an explicit expiry; `0` = never expires. Clamped 0–3650. |
| `a2a_task_retention_days` | integer | `7` | How long the record of each inbound A2A call (task id, status, message, reply) stays retrievable by the caller through the protocol's task lookup before it is deleted. Only the A2A envelope: the conversation itself stays in the agent's thread and transcript, and the Agent to Agent view keeps its own log. Clamped 1–3650. |
| `allowed_hosts` | string[] | `[]` | Extra `Host` header values accepted by the DNS-rebinding protection, on top of the public URL's host and loopback (`host:*` allows any port). Only needed when the endpoints are reached under another hostname. |
| `allowed_origins` | string[] | `[]` | Extra browser `Origin` values to accept. Server-to-server clients send none. |
| `oauth.enabled` | boolean | `true` | Olano's own OAuth 2.1 authorization server at `<public URL>/api/oauth`. It only answers while a surface above is on. Off ⇒ OAuth-only clients cannot connect; pasted tokens still work. |
| `oauth.dynamic_registration` | boolean | `true` | Accept RFC 7591 client registrations so an application can sign up by itself before its first sign-in. Registration grants nothing; the consent page does. |
| `oauth.access_token_minutes` | integer | `60` | Lifetime of the access tokens the sign-in issues. Clamped 5–1440. |
| `oauth.refresh_token_days` | integer | `30` | How long a client may keep renewing without a new consent; every refresh rotates the token. Clamped 1–365. |
| `jwt.issuer` | string | `""` | With `jwt.jwks_url`, also accept access tokens issued by your own authorization server (Auth0, Keycloak, Okta, Entra…). |
| `jwt.jwks_url` | string | `""` | Where that issuer publishes its signing keys. |
| `jwt.audience` | string | `""` | The audience such a token must carry. Empty means the URL of the endpoint the token is presented to, which is what the standard requires: `<public URL>/api/mcp` for MCP, and the agent's own `<public URL>/api/a2a/agents/<agent>` for A2A (`<public URL>/api/a2a/agents` is accepted there too). A value set here replaces both. |
| `jwt.required_scopes` | string[] | `[]` | Scopes every such token must include, in addition to the agent scopes (`agent:<name>` / `agents:*`) that decide reach. |

### `catalog_sync:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Keep the model, MCP and agent catalogs current. |
| `manifest_poll_minutes` | integer | `60` | Clamped 15–1440. |

### `credits:` — how costs are displayed

| Key | Type | Default | Meaning |
|---|---|---|---|
| `display` | `"usd"` \| `"credits"` | `"usd"` | How costs are shown. Display only — the underlying accounting is unchanged. |

### `magic_links:` — secure links for connecting people

A **magic link** is a short-lived, PIN-protected page an agent sends into a
chat so someone with no dashboard account (and no server access) can finish one
task on their own phone: sign in to a service, connect a messenger, edit an
agent's instructions, hand over a credential, or edit a file. The link is
single-use, and the agent is told the outcome in the same chat when it lands.

Dashboard: **Config → Magic links** (and the mint UI under Connections).

> Renamed from `handshake:`. A config still using the old key keeps working —
> it is read as a fallback — and the first save from the dashboard rewrites it
> under `magic_links:`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Allow agents to send magic links. Off ⇒ nothing can mint one and existing links stop opening. |
| `ttl_minutes` | integer | `15` | Link lifetime. Clamped 2–1440. |
| `pin` | `"required"` \| `"off"` | `"required"` | Whether links also need a PIN. A forwarded or leaked link is useless without it. |
| `max_pin_attempts` | integer | `5` | Wrong PINs before the link burns itself. Clamped 1–10. |
| `allow_groups` | boolean | `false` | Allow links to be minted from group chats, where every member sees the link and its PIN. |
| `owner_login.enabled` | boolean | `false` | Let an agent send the owner a magic link that signs them into the dashboard. |
| `owner_login.auto_bind` | boolean | `false` | On a single-admin deployment, skip the one-time password step. |
| `owner_login.session_hours` | integer | `24` | Clamped 1–720. |

**How the outcome comes back.** Whichever chat the link was sent from is told
when it lands — you never have to ask "did that work?" or refresh anything. On a
messenger the agent posts a ✅ or ⚠️ message saying what connected and whether
the agent still needs a restart. In the dashboard chat the same result appears
as a card in the same conversation:

- when the connection is already live — connecting messengers restarts the
  agent for you, and signing in to an MCP server reloads its tools — the card
  says so and there is nothing to do;
- when the agent has to restart before it can use what you just added (a new
  API key, a newly connected service), the card says that and offers a
  **Restart** button. Dashboard admins get the button; anyone else gets a line
  asking an admin to restart it.

The card stays in the conversation, so reopening the thread later — or opening
it in another tab — still shows what happened. A link that expired or failed
reports that instead, and never offers a restart.

**Editing a file over a magic link.** An agent can send a link that opens one
file in an editable box (`magic_link_request_file_edit`, or `/olano connect edit <path>`,
or the Connections mint form). A path relative to the agent's workspace edits
that agent's own file; an absolute path edits a file anywhere the server can
write, and only an admin agent or the dashboard may send one of those. The file
must already exist, be text, and be under 256 KB; saving replaces the whole file
with what the recipient submits. Whoever opens the link sees the file's current
contents, so it should only go to someone meant to read that file.

### `prompt_refresh:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Pick up edits to identity/instruction files without a restart. |
| `check_interval_minutes` | integer | `2` | Clamped 1–1440. |
| `notify_threads` | boolean | `true` | Tell active conversations that the prompt changed. |

### `speech:` — voice in and out

Two identical blocks, `speech.stt` (speech to text) and `speech.tts`
(text to speech):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `provider` | `"disabled"` \| `"olano"` \| `"openai"` \| `"groq"` \| `"qwen"` \| `"custom"` | `"disabled"` | |
| `model` | string | `""` | |
| `url` | string | `""` | For `provider: custom`. |
| `api_key_name` | string | `""` | Which vault entry holds the key. |
| `voice` | string | `""` | TTS only. |
| `audio_format` | string | `""` | TTS only: `aac`, `flac`, `m4a`, `mp3`, `opus`, `pcm`, `wav`. |

On a managed deployment, an `stt` block with **no `provider` key at all**
resolves to `olano` — voice notes transcribe on credits with no setup. An
explicit `disabled` always wins. `tts` never turns itself on: voice replies
stay off on every install until you pick a provider here or in Config →
Speech (on a managed deployment, picking `olano` runs them on credits).

To hear a voice before you keep it, press the play button beside **Voice** in
Config → Speech. It reads two short sample sentences with the provider, model
and voice currently picked in the form, even if you have not saved them yet,
and pressing it again stops playback. Each preview is a real speech call,
billed like a voice reply. If the provider cannot be reached or its key is
missing, the error appears as a message instead.

The mobile app uses both blocks too. A reply it reads aloud is spoken in the
`tts` voice when one is set (each reading is billed like any other speech
call), and in the phone's own voice when `tts` is off. On an iPhone, the phone
turns voice notes into text itself, so they reach the agent as text and do not
use `stt` unless the person turns that off in the app. See
[Mobile app](#hearing-replies).

Transcription applies to voice notes on every messenger that carries them,
WhatsApp included. If a voice note on one channel is answered as though nothing
was said while the same words work elsewhere, the audio never reached the
transcriber — not a speech setting. See
[Channels and connectors](#channels-and-connectors) for the WhatsApp-specific case.

### `media:` — vision and image generation

| Key | Type | Default | Meaning |
|---|---|---|---|
| `vision` | string | `""` | `""` = decide automatically, `"olano"` = force the managed route, or a `"provider:model"` pin for your own key. |
| `image` | string | `""` | Same, for image generation and editing. |

### `web_search:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `provider` | `""` \| `"olano"` \| `"tavily"` \| `"disabled"` | `""` (auto) | `tavily` always needs `TAVILY_API_KEY` in the vault. |

### `rate_limits:` — message caps

| Key | Type | Default | Meaning |
|---|---|---|---|
| `messages.per_minute` | integer \| null | `null` | `null` = unlimited. An explicit `0` blocks entirely. |
| `messages.per_hour` | integer \| null | `null` | |
| `messages.per_day` | integer \| null | `null` | |
| `per_platform.<platform>` | same three keys | `{}` | Per-connector overrides, e.g. `per_platform.telegram.per_minute`. |

An agent's own `security.rate_limits` overrides these for that agent.

### `playground:` — the prompt sandbox

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Show the Playground tab. Off also skips generating its assets. |

### `self_serve_agents:` — let people make their own bots

**On by default**: every signed-in member gets a "New bot" button (most
visibly in the simple view) and can create bots of their own, within the
quota below. Admins can always create agents regardless of this block —
nothing here applies to them.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | When `true` (the default), a non-admin member may create an agent for themselves. They become its **owner**, so they can edit and delete the one they made — and nothing else changes about what a member may do. Set `false` to make agent creation admin-only. |
| `max_per_user` | integer | `3` | How many agents one person may own before further creation is refused. Clamped to 1–100. Admins are not counted or limited. |
| `allowed_archetypes` | string[] | `[]` | Which templates a member may build from. **Empty means any template**, which is the default. A non-empty list is strict: only those templates, and building from scratch is refused too — so use it when you want people choosing from a menu you curated rather than describing a bot freehand. Names are the template ids shown in the New bot picker. |

All three knobs are editable from the dashboard under **Config → Member bot
creation** — changes apply on save, no restart. A member who creates a bot
gets a bot that runs on your deployment and spends your credits;
`max_per_user` is the ceiling on how much of that any one person can start,
and `enabled: false` closes the door entirely if that is not a trade your
deployment wants.

### `agent_autonomy:` — what agents may change about themselves

Deployment-wide defaults for **chat self-configuration**: when someone asks a
bot in conversation to "hook yourself up to Notion", "switch to a cheaper
model", "rewrite your instructions" or "add a research helper", these switches
decide whether the bot has the tools to do it. **Everything is on by
default.** Each key is a boolean; the block is editable from **Config → Agent
self-configuration**, and a single agent can override any switch in either
direction from its own page (**Agents → agent → Chat self-configuration**,
three-state with *Inherit*). Changes apply to the agent's next tool call — no
restart.

| Key | What the agent may do when `true` (the default) |
|---|---|
| `explore_archetypes` | Search and read the template library (read-only). |
| `change_model` | Switch its own model, from the models this deployment can actually reach. Applies after it restarts. |
| `modify_prompt` | Rewrite its own system prompt (full replacement; recorded in Config → Revisions under the agent's name). |
| `restart_self` | Restart itself so saved changes take effect — scheduled just after its reply, so the answer isn't cut off. |
| `add_subagents` | Add specialist subagents to itself, from scratch or drafted from a template. |
| `connect_mcps` | **Connect services (MCP servers and built-in toolkits).** Find every supported way to reach a service (`self_find_integration`), add or remove an MCP server or a built-in toolkit on itself, and start the sign-in. OAuth and sign-in codes happen in the user's own browser; API keys go through the vault. Also lets it read an outside project's documentation before anyone installs it (`self_inspect_external`). |
| `connect_platforms` | Wire messaging channels onto itself — token platforms (Telegram, Discord, Slack) and WhatsApp by a QR code shown right in the chat. |
| `manage_skills` | List the skills it has and switch individual ones on or off **for itself** — the same switches on its Skills tab, writing the same `disabled_skills` list. It can never install, edit or delete a skill this way, and switching off a common skill leaves every other agent's copy alone. Applies after it restarts. |
| `magic_links` | Send **magic links** — the `magic_link_*` tools: sign a person in to a service or MCP server, collect an API key straight into the vault, connect messengers by QR or token, let someone edit its instructions or a file, open the dashboard on their phone. The default way a bot connects itself to anything, so it ships on. Off removes the whole family from the bot; a single link tool can also be switched off on its Tools tab. Owners only, group chats refused, and an approval card on the stricter profiles; **Config → Magic links** stays the hard off switch for the whole deployment and separately gates the dashboard link. |
| `external_access` | Publish itself for outside callers (MCP / A2A) and mint access tokens — `self_set_external_access` and `magic_link_mint_access_token`. The tools additionally refuse anyone who is not an administrator, so this only decides whether a bot holds them at all. Off removes them from the bot; the External access tab still works for admins. |

Four guardrails apply regardless of these switches. **Only people who own the
bot can make it change itself** (see below). Every change is written through
the same validated path the dashboard uses, so a broken edit is refused rather
than half-saved. Every `agent.json5` change lands in **Config → Revisions**
attributed to the acting agent, so it can be reviewed and rolled back. And on
the stricter security profiles each self-change first raises a human approval
card. These switches govern the **agent acting on itself** only — they grant
nothing to humans and nothing over other agents.

**Who may ask is a separate question from what the bot may change.** The
switches above decide what the bot is *capable* of; ownership decides who is
allowed to trigger it. Anything that writes — model, instructions, subagents,
MCP services, messaging channels, restart, sending a magic link — is limited to:

- an **owner** of that bot (whoever creates a bot owns it; more owners are
  granted under **Admin → Access**, where the roles are *Owner* and *User*),
- any **deployment admin**, and
- on a messaging channel, a sender on that channel's **allowed users** list —
  which is what keeps "pair yourself with WhatsApp" workable from a phone.

Everyone else who may chat with the bot gets a plain refusal naming the
remedy, and keeps the read-only half: it can still list the models it could
switch to, the services it could connect, and the templates it could draw
from. So a colleague you shared a bot with can explore what it *could* become
and then ask you to approve it, rather than silently reconfiguring your bot.

Two cases are refused outright, with no way to grant them: an agent asking
*another* agent to reconfigure it, and a scheduled or background run trying to
reconfigure the agent while nobody is watching.

One built-in exception: an agent in **customer-service mode** (per-customer
isolation — a support bot serving strangers on a shared channel) inherits
**off** for every capability, whatever the deployment default says. A
stranger must not be able to talk a support bot into rewiring itself. You can
still enable an individual capability for such an agent deliberately, with an
explicit **On** in its own Chat self-configuration card.

### `groups:` — multi-bot conversations

On by default, and editable from the dashboard at **Config → Group chats** —
no file editing needed. While it is off, group threads do not exist and every
group address on the API answers "not found"; turning it off never deletes a
group, and turning it back on restores every group and transcript untouched.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Group threads: one conversation several bots share, where you can address any member. Replies stream in word by word, the same as a one-to-one chat. Whether the bots then answer *each other* is the separate `autonomous` switch below. |
| `min_members` | integer | `2` | Fewest bots in a group. |
| `max_members` | integer | `6` | Most bots in a group. |
| `autonomous` | boolean | `true` | **A second, separate switch, also on by default.** With it on, a bot whose reply names *another* member by `@name` hands that member the next turn — so the bots carry on with each other without you in between, inside the two limits below. With it off, you post, the bots you named answer, and the thread waits for you. |
| `max_hops` | integer | `3` | How many bot-to-bot turns one message of yours may set off. Clamped to 1–10. When it runs out the thread stops and says so in the conversation. |
| `cascade_timeout_seconds` | integer | `180` | How long the whole chain may run before it stops itself, in seconds. Clamped to 10–1800. |

`autonomous` is the one to think about. It is what makes a group a conversation
rather than a switchboard, and it is also the setting that spends model calls
nobody is watching, because one message from you can start a chain of bots
answering each other. That is why the two limits exist, why the switch is
separate from `enabled`, and why every stop — hop limit, timeout, or two bots
going back and forth — is written into the conversation instead of the thread
just going quiet. If you would rather relay every hand-off yourself, turn it off
at **Config → Group chats**; the group surface stays exactly as it is. Raise
`max_hops` only once you have watched a few chains and know what they cost you,
and raise `cascade_timeout_seconds` with it — more turns under the same time
limit only converts "out of turns" into "out of time".

If you are upgrading from a release where this switch was off by default and
your `config.yaml` never mentioned it, your groups start doing this after the
upgrade. Set `autonomous: false` there, or switch it off in the dashboard, to
keep the old behaviour.

**Using groups.** A **Group chats** section appears in the agent list of both
interfaces — in the full dashboard, under your agents in the sidebar. Click a
group to open it; right-click it to rename it; use **New group chat…** at the
foot of the section (or right-click its heading) to start one, picking the
agents in the order you want them to answer. Each group has a details panel —
the ⓘ button in its header — for renaming it, adding an agent, removing one,
or deleting the whole group; on a phone the panel fills the screen. Order
matters: the first agent in the roster answers anything that doesn't name
someone. The **Add an agent** button in a one-to-one chat's header is the
other way in: it turns that conversation into a group (see the Chat section).

**Sharing a group with a teammate.** With team accounts on, the person who
created a group (or an admin) can let other team accounts into it from the
group's details panel, under **People**: pick a person from **Share with a
teammate** and they see the group in their own sidebar, marked **Shared**, can
read everything in it and post into it. The list offers only people who already
have access to every agent in the group, because sharing never grants access
to an agent; grant that first on the Admin page if the person you want is
missing. For the same reason an agent can only be added to a shared group when
everyone shared into it already has access to that agent. A shared person is a
guest: they can leave from the details panel, and what they posted stays in the
conversation under their name, but only the creator and admins can rename the
group, add or remove agents, share it with more people, remove someone, or
delete it. Admins see every group whether or not they are listed.

The **People** section lists everyone in the room who is not an agent: the
creator, the people it is shared with, and any admin who has posted. Each
person's messages are labelled with their handle, and every agent in the group
is told who is in the room on every turn, so it answers the person who asked
and knows the others are reading. Once more than one person is in a group,
one rule changes: **a message that names no agent is for the other people**,
is saved, and wakes no agent, so "thanks Maria, I'll take it" does not cost a
model call. The line under the message box says so, and typing `@` and an
agent's name asks that agent exactly as before. A group with one person in it
keeps answering with its first agent.

**Addressing one agent, or several.** Start a message with `@` and an agent's name
to put it to that agent; typing `@` opens a list of the members to pick from. You
can name more than one — *"@ana @luis @ravi what would each of you do here?"* —
and then each of them takes its own turn, in the order you named them, and
**each one can see what the ones before it said**, so they add to each other
instead of writing the same answer three times. Name nobody and the group's
first agent answers. A line under the message box says which of those is about to
happen — *"ana answers"*, *"ana, luis answer, each in turn"* — and it changes as
you type, so there is nothing to set before sending.

Agents you did **not** name still read everything. They stay out of the way
unless they are named — and any agent can bring a teammate in by writing
`@their-name` in its own reply, which is what turns a group into a
conversation rather than a switchboard. With `autonomous` on (the default) that
mention wakes the other agent for the next turn; with it off, the mention marks
who should pick the work up and the thread waits for you. When an agent answers a
teammate that called on it, its message carries a small "↩ name" tag beside the
"→ name" one, so you can always tell who is answering whom — and answering is
not handing over: the chain only continues when a reply names somebody else.

Every agent in a group is told, on every turn, who else is in the room and what
each of them does (their description from the agent's own profile), that a
message naming somebody else is context to read rather than a cue to answer,
and which ways it has of reaching the others — so the group behaves like a room
of colleagues rather than several agents that happen to share a page.

**Watching them work.** The group header has the same **Quiet / Normal /
Verbose** control as a one-to-one chat, and it starts on **Normal** rather than
Quiet: Normal shows one line per tool an agent uses — including it calling another
agent or handing work to one of its own helpers — and Verbose adds each tool's
full input and result plus the helper transcripts. In a group that activity is
usually the point, which is why the starting level differs. The choice is
remembered in your browser and applies to every group.

The reply is laid out **in the order it happened**: what the agent said before
it went to work, then what it did, then what it said afterwards — rather than a
pile of tool lines above one block of text. That order is kept, so reopening the
group later shows the same conversation you watched.

**Files an agent makes.** When an agent saves a file and names it in its reply,
the group shows it: a picture appears inline, anything else gets a card with
**Download** and **Open in Files**, and the file's name in the text becomes a
link. Each agent has its own workspace, so a link goes to the files of the agent
that made it. A file the agent later deletes stops being offered rather than
leaving a dead link behind.

When one agent asks another one directly, the transcript says so while it waits:
a line reading *"maria is thinking…"*, with that agent's own face beside it,
appears under the member doing the asking and clears the moment the answer
comes back. It shows at every level, Quiet included, because it is about who is
talking rather than about tools — and without it a group looks frozen for as
long as the other agent takes. What that agent says comes back inside the asking
agent's reply, not as its own message in the group; an agent pulled in with `@their
name` is the other route, and that one does post its own message.

**Renaming a group.** Right-click a group in the sidebar and choose **Rename**,
or open the group's info panel and use the pencil beside its name (that is the
route on a phone, which has no right-click). One-to-one conversations rename the
same way in both views: right-click the chat under its agent in the sidebar (or
in the simple view's rail), use the pencil on a row of the chat page's
conversation list, or type `/rename` followed by the name you want. A name is
only a label — renaming changes nothing about the conversation, who is in it,
or what the agents remember of it. The name shows everywhere the conversation
is listed the moment it is saved.

**Turning a one-to-one chat into a group.** In any conversation, **Add an agent**
in the header brings other agents in. The agent you were already talking to leads
the new group, so it keeps answering by default, and the recent history is
copied across labelled with who said what — so the agents you add can read what
was discussed instead of starting blind. Long conversations are trimmed to the
most recent messages and the group says so in the transcript. You can switch
that copying off if you would rather start clean. Two things are worth knowing:
your original one-to-one chat is left exactly as it was (this creates a
separate conversation), and what an agent privately remembers of that chat does
not transfer — every member, including the original one, starts the group with
only what the transcript shows.

**Letting bots use groups themselves.** Adding `"groups"` to an agent's
`tools:` list gives it four abilities: list the groups it belongs to, read a
group's recent messages, post into a group (optionally addressing one member
with `@name`), and create a new group for the person it is talking to. A bot
does **not** need these to take part in a group it is already in — replying and
`@mention`ing a teammate work with no tools at all; these are for reaching a
group it is not currently being spoken to in. A bot
can only see and speak in groups it is a **member** of, its own name is always
stamped as the author, and whether an addressed member replies on its own
still follows `autonomous` and its limits — a bot's post starts exactly the
same bounded exchange a message of yours would, never more. Creating a group
works only in a live conversation with a signed-in person (the group becomes
theirs, in their sidebar); from a scheduled run the bot is told to ask a human
instead.

---

## `organization.yaml` — the org chart

A deployment-wide file at `~/.olano/organization.yaml` holding the reporting
structure and messaging rules for every agent in one place. Once it exists it
**supersedes** each agent's own `org:` block — the per-agent settings are seeded
into it once, and after that this file is the source of truth.

Edit it from the dashboard's **Org Chart** tab rather than by hand. The chart
enforces consistency for you: if you say A manages B, it makes B report to A,
and it refuses structures that would create a management cycle.

Why it exists: reporting lines are inherently a statement about two agents, and
keeping both halves in two separate files invites them to disagree. One file
means one answer to "who can talk to whom".

---

## Feature guides

The reference pages say what every field and tab does. These guides say how
the pieces fit together for one job at a time: what to turn on, in which
order, and what to expect once it runs. Each one points back at the exact
fields in [`agent.json5`](#agentjson5-reference) and [`config.yaml`](#configyaml-reference).

- [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart): who may ask whom for
  work, and how the org chart and the allow-lists combine.
- [Human in the loop and approvals](#human-in-the-loop-hitl-and-approvals): which actions wait
  for a person, and where the approval cards appear.
- [Privacy and security](#privacy-and-security): what an agent may read,
  reach and run, and how to tighten it.
- [Multi-user](#multi-user-two-different-features): team accounts on one deployment, and
  per-customer isolation for a public-facing agent. Two different features.
- [Subscriptions and monetization](#subscriptions-and-monetization): charging
  for access to an agent.
- [MCP servers](#mcp-servers): giving an agent external tools, with or
  without a sign-in.
- [Webhooks](#webhooks): letting outside systems trigger an agent.
- [Pages, sites and web apps](#pages-publishing-sites-and-web-apps): publishing reports, npm-built apps and
  small web apps with their own server, behind a password you set.
- [Scheduler and heartbeat](#scheduler-and-heartbeat): recurring work, and
  the agent's own periodic check-in.
- [Memory and self-learning](#memory-and-self-learning): what an agent keeps
  between conversations and how it distils it.
- [Notes](#notes): the agent's linked wiki.
- [Issues](#issues): the shared tracker between people and agents.
- [Revisions](#revisions): the automatic history of configuration and
  markdown changes.
- [Channels and connectors](#channels-and-connectors): WhatsApp, Telegram,
  Slack, Discord, email and the rest, including the OpenAI-compatible API.
- [Cortex and Pulse](#cortex-and-pulse): the background reasoning engine and
  the proactive suggestions it produces.
- [The mobile app](#the-mobile-app): the Olano app for phones, tablets and Macs,
  and the notifications it receives.
- [Budgets](#budgets): token ceilings and spending warnings.
- [Ask the agent](#ask-the-agent-self-configuration-magic-links-and-admin-tools): self-configuration, magic links and the
  AgentFather admin tools.
- [Beyond the deployment, A2A and MCP](#beyond-the-deployment-a2a-and-mcp): talking to outside
  agents, and letting outside platforms reach yours.

### Agent-to-agent and the org chart

Agents can send each other work with `talk_to_agent`. Who may do that is
governed by two things that combine: the **org chart** and the **allow-lists**.

**The org chart** (`org.manager`, `org.reports`, `org.coworkers`, `org.title`)
places an agent in a hierarchy. A manager and its direct reports may **always**
exchange messages, regardless of any allow-list. That is deliberate: declaring a
reporting line is meant to be enough to make delegation work, without
duplicating names into two more lists.

```mermaid
flowchart TD
    S["A asks B through talk_to_agent"] --> H(["Is B human_only?"])
    H -- yes --> X["Refused"]
    H -- no --> O(["May A send to B?"])
    O -- no --> X
    O -- yes --> I(["Does B accept A?"])
    I -- no --> X
    I -- yes --> M(["Does B take orders only from its manager,<br/>and A is not that manager?"])
    M -- yes --> R["Delivered: B answers<br/>with read-only tools"]
    M -- no --> F["Delivered: B can act"]
```

Sending and accepting always pass between a manager and its direct reports.
Between anyone else, each side's allow-list decides, as described below.

**The allow-lists** control everyone else:

- `org.input_agents` — who may message this agent.
- `org.output_agents` — who this agent may message.
- `org.input_mode` / `org.output_mode` — `everyone`, `none`, or `list`.

The rule that surprises people: **an empty list means unrestricted**, not
"nobody". A fresh agent with no `org` block can talk to any other agent, which
is the historical default. To lock an agent down, set the mode explicitly:
`input_mode: "none"` blocks every peer *except* its own manager and reports, so
delegation still works.

Three more controls:

- **`org.mutating_from_manager_only: true`** — admitted peers may ask this agent
  for information, but only its manager can make it *do* something. A good
  setting for an agent that owns a system of record.
- **`human_only: true`** — refuse all inbound agent messages. Use it for an
  agent that must only ever take direction from a person.
- **`org.autogenerate: true`** — derive the allow-lists from the hierarchy
  (manager + reports + coworkers) instead of maintaining them yourself.

**Awareness is separate from permission.** `agent_visibility` decides which
peers an agent is *told about*. `mode: "none"` (the default) means it does not
know its colleagues exist; `"all"` tells it about everyone; `"list"` names
specific peers. Telling an agent about a peer does not grant it permission to
message that peer, and granting permission does not automatically tell it the
peer exists — for a delegation to happen, you usually want both.

**Supervisors, managers, co-workers, specialists.** These are patterns, not
separate features:

- A **supervisor / manager** is an agent with `reports`. It receives goals and
  splits them across its reports.
- **Co-workers** are peers listed in `coworkers`. Display-only on the chart —
  give them messaging rights through the allow-lists or `autogenerate`.
- A **specialist** is either a top-level agent (if it needs its own inbox,
  schedule or memory) or a **subagent** of the agent that uses it (if it does
  not). Subagents are internal, invisible to the roster, and cheaper to run.

Watch what happens on the **Agent to Agent** dashboard tab, and edit the
structure on **Org Chart**.

### Human in the loop (HITL) and approvals

When an agent wants to do something consequential, it can stop and ask. The
request appears in the **Approvals** section of the dashboard Overview with the
exact action described, and the agent waits for your decision (up to
`security.hitl_timeout_seconds`, default 300).

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent
    participant O as Olano
    participant P as Person
    A->>O: wants to run a gated action
    O->>P: the action and its details, in Overview, Approvals<br/>(an owner on WhatsApp gets a YES/NO code instead)
    Note over A: the agent waits,<br/>up to hitl_timeout_seconds (300 by default)
    alt approved
        P->>O: Approve
        O->>A: the action runs
    else rejected, or no answer in time
        P-->>O: Reject
        O->>A: the action does not run
    end
    O->>O: the decision goes into the approval record
```

Approval is decided in three layers, each overriding the one before:

**1. The profile** (`security.profile`) sets the baseline:

| Profile | Meaning | Always asks before |
|---|---|---|
| `developer` *(default)* | Trusted — act freely | money, writing secrets, running commands in another agent's workspace |
| `safe` | Supervised — ask before writes | the above, plus external writes, shell commands, reading secrets |
| `production` | Locked down — strictest | the above, plus filesystem writes and delegating to subagents |

**2. Toggles** add categories on top: `security.hitl_write_tools: true` adds
external writes; `security.hitl_shell: true` adds shell commands.

**3. Per-tool overrides** win over everything:
`security.tools.<tool_name>.approval` set to `"always"` or `"never"`.

`security.hitl_enabled: false` is the master off switch — full autonomy, no
approvals at all. All three profiles enable it by default.

**Two things are always individually approved, in every profile:** running a
command in *another agent's* workspace, and a [browser](#browser-control) action that
submits a form, buys, pays, sends, posts or deletes. There is no configuration
that turns either off, and neither the chat's *Auto* mode nor *Accept all*, a
remembered "always allow", a scheduled run or a background run approves one on
a person's behalf.

`security.hitl_auto_approve_low_risk` (default `true`) lets clearly-safe actions
through without asking, so the queue stays meaningful.

Cortex has its own approval setting. `cognition.auto_approve` decides whether
background work skips the queue, but `cognition.approval_required` — default
`["external_send", "irreversible", "identity_edit"]` — always needs a human,
whatever `auto_approve` says. Anything that sends something outside, cannot be
undone, or rewrites who the agent is, gets a person in the loop: the item waits
in **Pulse → Approvals** instead of being applied, and a background run that
tries such a step pauses on it the same way. So does a step the agent scheduled
for itself from a background run (`cortex_schedule`, shown in **Scheduler** as
`Cortex: <item>`): it runs under the same rule as the run that booked it, not
with the free hand a task you scheduled yourself gets. Fully Autonomous still
handles everything else on its own: new skills, memory, procedures, tidy-ups,
promotions down the funnel and the execution of prototypes and projects. An owner
who really wants an agent to act unattended on all of it sets
`approval_required: []` for that agent; one who wants only identity protected
lists just `["identity_edit"]`.

**Approvals from WhatsApp.** When someone talks to an agent on any of the
three WhatsApp connectors, the outcome of a risky action depends on who they
are:

- **An owner** has risky actions approved without being asked, unless **Ask
  owners in chat before risky actions** is on in the connector's **Behavior**
  section (`chat_approvals: true`). Owners are the numbers in *Owner numbers*;
  on the QR connector the numbers in *Allowed phone numbers* count as owners
  too, while on the Twilio and Meta connectors *Allowed sender numbers* is a
  list of customers and makes nobody an owner. Then the agent posts the action and its details in
  the chat with a four-character code and waits; the owner replies `YES 4F2A`
  to let it run or `NO 4F2A` to stop it. Only the owner whose message started
  the request can answer, and a reply without the code does nothing. With no
  answer before `security.hitl_timeout_seconds` the action is rejected, and so
  is one whose prompt could not be delivered.
- **Anyone else** never decides: their risky actions wait in the dashboard's
  Approvals list for someone with access to the agent.

**The approval record.** Every approval decision is written to an append-only
file, `audit/approvals.jsonl` in the Olano home: the action and its details,
when it was asked, the outcome (approved, rejected, timed out, or abandoned when
the server stopped before anyone answered), who decided (a dashboard user as
`human-<username>`, a messenger sender as `<platform>:<id>`) and through which
surface. Actions an owner's messages had approved without asking are recorded
too. Read it on the **Overview** page under **Approvals → Approval history**,
which can be narrowed to one agent. Admins see the whole record; a member sees
the rows for agents they have access to.

### Privacy and security

Several independent checks stand between what reaches an agent and what it
does. The settings on this page switch them on, off or tighter:

```mermaid
flowchart TB
    subgraph inbound["On the way in"]
        direction LR
        IN["Message, email,<br/>web page, document"] --> RL["Rate limits<br/>external traffic only"] --> IS["Input scan<br/>injection, personal data"]
    end
    inbound --> AG["The agent"]
    AG --> acts
    AG --> outbound
    subgraph acts["When it acts"]
        direction LR
        AP["Approvals<br/>when the profile asks"] --> GD["Shell and<br/>network guards"] --> ACT["Action in its<br/>own workspace"]
    end
    subgraph outbound["On the way out"]
        direction LR
        OS["Output scan,<br/>credential scrub"] --> OUT["Reply"]
    end
```

**Trust level** (`security.trust_level`, 0–4) is the broad autonomy dial:

| Level | Name | What it can do |
|---|---|---|
| 0 | Observer | Read only; can suggest, not act |
| 1 | Assistant | Write to memory and logs only |
| 2 | Collaborator *(default)* | Create and edit workspace files; containerised shell |
| 3 | Autonomous | Full workspace; auto-approves low-risk operations |
| 4 | Developer | Local shell; auto-approves most things. Development and CI only |

**Personal data.** `pii_scan_input` and `pii_scan_output` (both on by default)
look for personal data moving in and out; `pii_action` decides what happens —
`warn` (default), `redact`, or `block`. `pii_scan_peer` extends scanning to
agent-to-agent traffic; it is off by default because internal traffic is usually
already inside your boundary.

**Prompt injection.** `input_scan` (on) checks incoming text for attempts to
hijack the agent's instructions — the classic risk when an agent reads email,
web pages or documents from people you do not control. `output_scan` (on)
screens what goes out. `web_scan` (off) extends the check to fetched web
content; turn it on for agents that browse widely.

**Credentials.** `credential_scrub` (on) strips anything that looks like a
secret from what the agent says, so a key that ends up in a log or an error
message does not get repeated into a chat.

**Network.** `ssrf_protection` (on) blocks requests to internal network
addresses — the defence against an agent being talked into fetching
`http://localhost/admin`. Which *public* destinations an agent's tools may
reach is a separate pair of lists, `egress_allow` / `egress_deny`, set for the
whole deployment in **Config → Outbound access** and narrowed per agent in its
**Advanced** tab; both empty (the default) allows everything. See
[`agent.json5` reference](#security) for the format and exactly what they govern.

**Shell.** `shell_guard` (on) blocks dangerous command patterns.
`skill_audit` (on, `strict`) reviews skills before running them.

**Sandboxes are isolated from each other.** An agent's shell commands, and any
server it runs (such as the server behind a [page](#pages-publishing-sites-and-web-apps)), run in its own
sandbox. No other agent's sandbox, and no other container on the machine, can
open a network connection to it, so one agent cannot reach what another agent
is serving. Sandboxes can still reach the internet, subject to the outbound
lists above. The isolation is set up automatically; the agent's Terminal tab
says when a machine could not set it up.

**Disk encryption (managed instances).** A managed instance created on Olano
Cloud keeps everything it stores (agents, memory, files, conversations, the
vault, the database) on an encrypted volume that is unlocked automatically
when the instance boots. This is *standard custody*: it protects your data
against disclosure from stolen disks, snapshots, backups and offline storage
access; the instance decrypts at boot using Olano-operated key infrastructure,
and Olano's production control plane retains the technical capability to
authorize key release. What is yours alone is the **recovery key**: a second
key that opens the volume without Olano's key service, for example to read your
data from a disk image if the instance or Olano is gone. Create it from
**Config → Disk encryption** in the instance's own dashboard: the instance
generates it, shows it exactly once, and asks you to type it back before it
counts. Olano never sees the key and cannot show it again; keep it in a password
manager or printed somewhere safe. Losing it is not fatal while the instance
runs (create a new one; the lost one stops working once the new one is
confirmed), but without one your data can be opened by Olano's key service
alone. The same card shows whether a backup of the volume's encryption header
(the keyslots, never your data) is held off the instance, which is what lets the
recovery key open the volume again if the header on the disk is damaged.
Conversation history on such an instance is additionally encrypted inside the
database, so a database file read outside the instance shows no message text.

**Rate limits.** `security.rate_limits` caps how much an agent can do:

- `per_tool: { tool_name: max_calls_per_minute }` — cap a specific tool.
- `messages: { per_minute, per_hour, per_day }` — cap inbound messages across
  all platforms. Omit a window, or set it to `null`, for unlimited.
- `per_platform: { telegram: { per_minute, … } }` — per-connector caps.

Two things to know. **Message limits are opt-in**: the schema shows a default
of 60, but limiting only activates once you set a `rate_limits` block
explicitly. And they apply **only to external inbound traffic** — scheduled
jobs, heartbeat runs and the agent's own background work are exempt, so a rate
limit never throttles the agent's own initiative.

(`requests_per_minute` is a legacy alias for `messages.per_minute`. Prefer the
`messages` form.)

These override the deployment-wide `rate_limits:` in `config.yaml` for that
agent.

**A note on boundaries.** An agent reaches its own workspace, plus shared
folders you declare and read-only skill sources. Other agents' directories are
deliberately invisible to it — a "permission denied" there is the isolation
working, not a bug to work around. Cross-agent work happens through tools that
are individually approved, not through the filesystem.

### Multi-user: two different features

These have confusingly similar names. They are unrelated, and you may want
either, both, or neither.

**Team accounts** — *the humans who operate the deployment.*
Turned on with `multi_user_enabled: true` in `config.yaml` (labelled "Team
accounts" in the dashboard), or automatically by creating the first admin. It
gives you a login screen, invite-only accounts, admin and member roles, and
per-agent access grants. Without it, Olano runs single-user: everyone who can
reach the dashboard is the owner. Managed deployments have it on by default.

How access works: one or more **admins** configure everything and manage users.
Each agent has an owner plus a flat list of members who may chat with it. Admins
can reach every agent without holding an explicit grant — so the per-agent
access list under-reports who can administer an agent, and the **Admin** page's
access overview (account menu → Admin) is the complete picture. Bulk actions apply a role across many
users and agents at once.

Access to an agent is access to all of its conversations: a member granted an
agent sees the same conversation list an admin does — its scheduled runs,
heartbeats, messaging-platform chats and everyone's dashboard chats — and can
continue any of them. Only deleting a conversation is reserved for admins and
the agent's owner.

Manage it in the dashboard's **Admin** page (account menu → Admin), or by asking
**AgentFather** in chat — it can invite people (returning a single-use
set-password link + QR image, never a password), reset passwords the same way,
change admin/member roles, disable or delete accounts, and grant or revoke
per-agent access, each behind a human approval card and admin-only.

Group chats follow the same grants and add one thing on top: the person who
created a group (or an admin) can **share** it with other team accounts from
the group's details panel, so several people and several agents sit in one
conversation. Only people who already have access to every agent in the group
can be shared in, a shared person can read, post and leave but not change the
group, and in a room with more than one person a message that names no agent
is for the other people and wakes none. The details are in the
[`groups:` section](#groups-multi-bot-conversations).

**Multi-user mode / per-user isolation** — *the end users who chat with one
shared agent.*
Turned on per agent with `customer_service.enabled: true` (equivalently
`memory.per_customer: true` — the two mirror each other). This is for a support
bot, a community assistant, a paid service: many people talk to the same agent,
and each gets isolated memory, so the agent never mixes one person's context
into another's conversation.

It swaps the shared memory tools for per-user ones (`remember`, `recall`,
`forget_me`) and unlocks optional capabilities:

- `user_scheduling` — end users can set their own reminders, capped by
  `max_jobs_per_user` and `min_job_interval_minutes`.
- `connections` — end users can be introduced to each other, with consent, with
  their own caps and invite expiry.
- `user_credentials` — end users store their own credentials for the agent to
  act on their behalf, restricted to the names in `customer_credential_names`.

These three, and per-customer schedule fan-out, **require** multi-user mode; a
config that uses one without it fails to load rather than quietly ignoring it.

Configure it on the agent's **Multi-user** tab, which also lists end users and
blocks or unblocks them.

### Subscriptions and monetization

Charge end users for access to an agent. Set it up on the agent's
**Monetization** tab or in the `monetization` block.

Two payment routes, usable together: **Stripe** (set `plan.stripe_price_id`;
empty means Stripe is off) and **Telegram Stars** (set `plan.stars_amount`;
`0` means it is off). `trial_days` (default 3) gives newcomers a free window.
`channels` limits the paywall to specific connectors; `gate_groups` extends it
to group chats.

Payment provider secrets go in the vault, never in the config file. The plan
fields here are what users *see* — your payment provider remains the source of
truth for what is actually charged.

Monetization pairs naturally with multi-user mode ([Multi-user: two different features](#multi-user-two-different-features)):
you need per-user isolation for per-user subscriptions to mean anything.

### MCP servers

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
  [Models and credits](#olano-credits-on-a-self-hosted-install))
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
developer console (see [Connections](#connections)).

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
`mcp_health:` settings in [`mcp_health:` — keeping MCP connections alive](#mcp_health-keeping-mcp-connections-alive).
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

### Browser control

Agents can use a web browser: open a page, read it, click, type, take a
screenshot. It is **off by default**, and the machine's administrator turns it
on in **Config → Browser** (or in [`config.yaml`](#browser-browser-control)).

#### The three modes

| Mode | What agents get | What it needs |
|---|---|---|
| **Off** (default) | No browser. The browser tools disappear from every agent's tools and instructions, even an agent whose tool list names them. | Nothing. |
| **Headless** | An invisible browser inside Olano, one separate session per agent. | At least 1.5 GB of memory. Nothing to install. |
| **Live** | A full, visible browser per agent, each in its own isolated container, which you can **watch** and **take over** from the agent's **Browser** tab. | At least 2 CPUs, about 4 GB of memory, Docker, and a plan that includes it. |

Choosing **Live** and saving fetches the live browser in the background
(Config → Browser shows the progress, and **Get image** starts it again). It is
a separate image rather than something installed into Olano itself, so updates
never remove it. When the machine cannot download it (a self-hosted install
usually cannot, because the published image needs a registry login), Olano
builds it on the machine instead from the files it ships with. That takes a
few minutes, about 1.5 GB of free disk and access to the operating system's
package mirror; if it fails too, the card names both reasons. Headless needs
neither.

Olano refuses a mode the machine cannot carry when you save, and says why. A
machine with 1 GB of memory gets no browser on it at all; if live is chosen on
a machine that later turns out too small, agents fall back to headless and the
Config card says so.

**Where the browser runs.** By default on this machine. Choose **Remote
browser service** instead to have agents use a hosted browser over the
DevTools protocol, which costs this machine no memory: store the service's
connection address (a `ws://` or `wss://` address, usually with its key in it)
in the [vault](#vault-admin-only) as `BROWSER_REMOTE_CDP_URL`. The live view
and taking control need the browser on this machine.

#### Going through a proxy

Websites see where a visit comes from. A browser on a server visits from a
data-center address, and some sites (Reddit and X among them) turn most
data-center visitors away or treat their sign-ins as suspicious. To have the
browser visit from another address, store a proxy in the
[vault](#vault-admin-only) as `BROWSER_PROXY_URL`:

| Format | Example |
|---|---|
| HTTP or HTTPS proxy, with or without a sign-in | `http://user:password@proxy.example.com:8000` |
| SOCKS proxy, without a sign-in | `socks5://proxy.example.com:1080` (also `socks4://`) |

- It applies to the headless and live browsers on this machine, including
  one-shot screenshots, from each agent's next browser action. An open
  browser is reopened under the new setting (sign-ins kept).
- A value set for one agent in the vault overrides the shared one, so agents
  can use different exits, or one agent a proxy and the rest none.
- A value the browser cannot use (a missing port, a SOCKS proxy with a
  password, which browsers do not support, a scheme other than the four above)
  stops the browser and tells the agent why. It never browses without the
  proxy instead.
- For sites you sign in to, use a proxy that keeps the **same address** from
  one visit to the next ("sticky" or "static"). A sign-in that comes from a
  different address every time looks like someone else taking the account
  over, and is how an account gets locked.
- **Remote browser service** ignores it: the hosted service picks its own exit.

**Time zone.** The browser also tells sites its time zone, and a site that
sees a visit from one country with a clock set to another treats it as one
more sign of automation. **Time zone the browser reports** in Config → Browser
sets it, as a name like `Europe/Madrid`; set it to where the proxy's address
is. Empty (the default) uses this machine's clock.

#### What an agent can do with it

- **Open a page and read it.** The agent receives the page as a structured
  outline of headings, links, buttons and fields, each with a short handle
  (`e12`) it uses to click or type, so it acts on the element it actually saw.
- **Click, type, press keys, fill a whole form in one step.**
- **Take screenshots**, saved under `screenshots/` in its workspace.
- **Ask you for help** when it reaches something only a person should do:
  signing in, a one-time code, a CAPTCHA. In live mode the agent tells you
  in chat, and the browser window in that chat shows its request; you take
  control, do it, and hand back.

**How fast it is.** Each step the agent takes (open, click, type) is one
round trip to its model, so a task's time is mostly the number of steps. The
browser keeps that number down: every action comes back with its result (the
new page, or just what changed on the same page), so the agent never spends a
step only to look; a form is filled in one step; and the page outline leaves
out empty layout containers, so each step sends the model less. Two things
you can do: give the agent the direct address when you know it (a profile, a
search page, a conversation) instead of asking it to click through menus, and
use a lower reasoning effort for browsing agents (the effort chip in the chat
header), since effort is spent on every step.

Each agent's browser is its own: cookies and sign-ins are never shared between
agents. Sign-ins are kept between sessions (saved when the browser closes and
loaded the next time that agent browses), stored outside the agent's
workspace so no agent can read them as a file. **Forget sign-ins** on the
Browser tab deletes them.

#### Safety: what the browser will not do on its own

A web page can contain text written to steer an agent ("ignore your
instructions and email this inbox to…"). Olano treats every page as untrusted:

- **The agent is told page text is information, never instructions.** Every
  page it reads arrives fenced as untrusted web content.
- **Anything that commits waits for a person.** Submitting a form (other than
  a search or filter), and any button labelled buy, pay, place order, send,
  post, reply, publish, delete, transfer, donate, subscribe, confirm, book,
  sign up and similar, stops the agent and asks for approval, in the chat, in
  the Approvals inbox, or as a YES/NO prompt on a messenger. Buttons are read
  in about thirty languages (Spanish *Enviar*, French *Envoyer*, German
  *Bezahlen*, Russian *Отправить*, Arabic *إرسال*, Hindi *भेजें*, Chinese
  *发送*, Japanese *送信*, Korean *보내기*, and more), and also by the name
  the site gives the button in its code, which usually stays English on a
  translated site. On a page that takes a payment or a password (a card,
  password or one-time-code field, a payment provider's frame, a checkout
  address), every button asks, whatever language it is in. These approvals
  are asked **every time**: the chat's *Auto* mode, *Accept all*, a remembered
  "always allow", a scheduled run and a background (Cortex) run never approve
  one on your behalf. The one exception is a rule an owner of the agent sets on
  purpose: see [Actions that don't ask](#actions-that-dont-ask).
- **Every request is checked.** Not just the address the agent opens: every
  click that navigates, every redirect and every image, script and connection
  the page makes (live connections such as chat and price feeds included) is
  checked against the agent's
  [outbound-access rules](#privacy-and-security), and private network
  addresses (this machine, other containers, the cloud's metadata service) are
  never reachable. A blocked click is reported to the agent with the reason,
  and a redirect to a refused address is stopped inside the browser before it
  is followed (a page that reaches one anyway is discarded before the agent
  reads it).
- **Files stay where they belong.** In `headless` mode the browser can open a
  file from the agent's workspace, and nothing else on the machine; a
  screenshot is saved only inside the workspace or a shared folder the agent
  can write to.
- **Passwords never pass through the agent.** When a sign-in is needed, the
  agent asks you to take over the live browser and you type it into the page
  yourself. Never paste a password into the chat.

#### Actions that don't ask

An agent that sends many messages on one site (say, LinkedIn) turns the
approval card into a stream of clicks nobody reads. For that case an admin or
an owner of the agent can add a narrow rule: **one site, and the kinds of
action on it that go ahead without asking**. Set it on the agent's **Browser**
tab, under *Actions that don't ask*, or ask the agent (or AgentFather) to do
it in a chat; either way you confirm the change in an approval card.

**Adding a rule.** With no rules yet, the section offers one-click examples
(*Send messages on LinkedIn*, *Post and reply on X*, *Book meetings on
Calendly*) next to **Add a site**. Each rule reads back as a sentence while you
edit it ("On linkedin.com, the agent may send messages and posts without
asking you, only while you chat with it"), so you can check it says what you
mean before you press **Save rules**. Saved rules fold into that sentence;
**Edit** opens one again and **Remove** deletes it (both take effect when you
save). A mistake (a website that is not one, the same site twice, a rule with
no action ticked) is explained under the field and keeps **Save rules** off.

| Field | What it means |
|---|---|
| **Website** | The site's name, such as `linkedin.com`, or any link from it pasted in: only the site name is kept, and a leading `www.` is dropped (`https://www.linkedin.com/feed/` becomes `linkedin.com`). It covers the whole site, every page and sub-domain; `*.example.com` covers only sub-domains. The action must happen on this site, and a form must also send its data to it, or the agent asks as usual. |
| **Send messages and posts** | A button or link that sends a message, replies, posts, publishes, connects or invites (a message's *Send*, *Connect*). |
| **Submit forms** | Any other form that saves or changes something on the site, and buttons labelled *Save*, *Submit* or *Confirm*. |
| **Sign up and book** | Subscribe, sign up, create account, book, reserve. |
| **When** | **Only while chatting** (the default, recommended): the rule applies only while the agent works in a conversation started by a person who owns it or is an admin, so someone sees each action. **Also when it works on its own**: it also applies to scheduled jobs and background (Cortex) runs. |

Anything you leave unticked keeps asking.

What a rule never does, whatever it says:

- **Purchases, payments, transfers, donations, deletions, approvals and
  merges always ask.** A button whose label mentions any of them, in any
  language the browser reads (*Send payment*, *Pagar ahora*, *購入する*), is
  never covered, and neither is any button on a page that takes a payment or a
  password.
- **An action the browser cannot name always asks.** If a button's label is
  in a language or wording the browser does not recognize, the action still
  waits for you and no rule covers it.
- **It never applies to someone else's request**: another agent, an outside
  MCP/A2A caller, a webhook or a watched email. Nor to a person who could not
  have approved the action themselves: a member who may chat with the agent
  but does not own it still gets asked.
- **It is never invisible.** Every action a rule let through says so in the
  chat ("Approved automatically by this agent's browser rule for
  linkedin.com"), and the agent tells you in its reply.

Rules apply to the agent's next action; no restart. Everyone who can chat with
the agent can read them on the Browser tab; only admins and the agent's owners
can change them.

#### Watching and taking control (live mode)

**From the chat.** While the agent you are chatting with has a browser open, a
small window in the corner of the conversation shows its screen. A cursor
moves to whatever the agent is about to click or type into, and ripples on
each click, so you can follow what it does as it does it. Click the window (or
its expand button) to open it large; collapse it to go back to the small
window. The window shows the site the agent is on and a one-line status; the
**–** button folds it into a small **Live browser** pill (remembered on that
device), and on a phone it is always the pill, which opens the browser full
screen. Hiding it with **×** keeps it away until the agent uses its browser
again (on your next question, for example), opens a new one, or asks for you.
To keep it out of the way while the agent works, use **–** instead. When the
agent asks for a person, the window
is outlined and says so.

**Approvals while you watch.** An approval is answered in the chat, never in
the browser. When the agent stops for one, the window folds into an
**Approval needed** pill, a large view you were watching closes, and the card is
scrolled into view; **Review** on the pill takes you back to it. The card says
what the agent wants to do in words: the button or link, the site, and why it
needs you. In Auto mode too: the chat's approval mode never answers these for
you.

**On the agent page.** The agent's **Browser** tab shows the session's
details (the page it has open, who is in control) and **Open chat**, which
takes you to the window above. If you leave the chat while you hold control,
**Hand back to the agent** is there too.

The screen updates as the page changes, and watching never acts on the page.
The picture is sent at the size it is shown and only as fast as your device
draws it: on a slow phone or connection, or on a page that never stops
moving, it turns a little rougher for a moment and sharpens again as soon as
it keeps up, and your clicks and taps always go through. If a page is too
busy to take a tap for a few seconds, the view says so; tap again, or press
**Reload**.

1. Press **Take control**. The agent is paused: its browser actions wait and it
   is told a person is using the browser.
2. Click and type in the picture as in any browser. A double click selects a
   word and a triple click a paragraph (on a touch screen, a double or triple
   tap). A right click reaches the page, so a site's own right-click menu
   works; the browser's own menu (Copy, Paste, Inspect) is not shown, so use
   the shortcuts below instead. Paste works with your usual shortcut (⌘V on a
   Mac, Ctrl+V elsewhere), and copy or cut (⌘C / ⌘X, Ctrl+C / Ctrl+X) puts the
   text you selected on the page onto your own clipboard. A page's own copy
   button (the icon next to an API key or a client secret, say) does the same
   when you press it. If your browser asks for a click first, a **Copy**
   button appears above the picture. On a Mac, ⌘ shortcuts such as ⌘A and ⌘Z
   do what they do on a Mac. Text in a password field is never copied out of
   the page by a selection; a page's copy button copies whatever the page
   chose to copy.

   Above the picture is a browser's usual toolbar:

   - **Tabs.** Every page the browser has open, the one you see highlighted.
     Tap a tab to switch to it, its **×** to close it, and **+** for a new
     tab. Closing the last tab leaves an empty page rather than closing the
     browser. A link that opens a new window shows up as a new tab.
   - **Back** and **Forward** (greyed out when there is nowhere to go), and
     **Reload**, which turns into **Stop** while a page loads. A thin bar
     runs across the top of the picture while the page is loading.
   - **The address bar** shows the address of the page you are on and follows
     it as the page changes. A padlock means the connection is encrypted
     (https); a warning sign means it is not (http), so do not type
     passwords there. To go somewhere else, edit the address (or type a new
     one; `example.com` is enough) and press **Go**. Escape puts the current
     address back.

   Some things a web page asks for are drawn by the browser rather than the
   page, so they appear as a panel over the dashboard instead:

   - **Dropdown lists.** Click or tap one and its options open in a list you
     can scroll (long lists, such as countries, get a filter box). Pick an
     option to set it on the page.
   - **Messages and questions from the page** ("Are you sure?", "Enter a
     name", "Leave this page?"). Answer them in the panel; the page waits
     until you do. If you hand back with one still open, it is cancelled.
   - **File uploads.** When you click an upload button on the page, choose
     files from your own device (up to 10 MB in total). They go straight to
     the page. Files the agent's browser downloads are not saved.
   - **Date, time and colour fields.** Click or tap one and a picker of your
     own opens (on a phone, the phone's). **Set** puts the value on the page
     as if its own calendar had; a value the page does not accept (a date
     outside its range) is refused with a note, so choose another.
   - **What the page cannot have.** The agent's browser has no location, no
     camera, no microphone and shows no notifications, so a page that asks
     for one of them is refused at once. You are told what it asked for, so
     a site that then behaves oddly ("enable location to continue") is not a
     mystery; there is nothing to allow.

   **Recordings and pictures.** Two buttons at the end of the address bar (on
   a phone, at the top of the full view) keep what the screen shows:

   - **Record** makes a video of the browser (MP4, what the live view
     shows), whether the agent is working or you are; the button turns into
     a red **REC** clock while it runs, every viewer sees it, and **Stop**
     saves the file under the agent's `recordings/` folder, where the Files
     tab shows it, a message can carry it and the agent can read it. A
     recording ends on its own after 30 minutes, or when the browser closes.
     The agent can record itself too: ask it to "record a demo of ..." and it
     starts, works and stops, naming the file in its reply.
   - **Picture** saves the page as it is now, a PNG under `screenshots/`.

   On a phone or tablet, the picture works like a page on your phone: tap to
   click and drag one finger to scroll. To drag something on the page (select
   text, move a slider), touch and hold, then move. Pinch with two fingers to
   zoom into the picture; while zoomed, dragging moves around the picture
   before it scrolls the page, and **Reset zoom** shows the whole page again.
   To type, tap the field on the page, write in the text box above the picture
   and press **Send text** (your keyboard closes so the whole picture is back
   in view). **Delete** removes the character before the cursor, **Tab** moves
   to the next field, and **Enter** presses Enter on the page, which usually
   submits the form.

   When a sign-in window that a page opened closes itself (as "Sign in with
   Google" windows do once you are signed in), the view returns to the page
   that opened it.

   Passkeys and security keys cannot work in the agent's browser: they live
   on your own phone or computer, which the browser cannot reach. When a site
   asks for one, the browser answers as if you had pressed Cancel, and the
   site offers its other ways to sign in (for example an authenticator app, a
   text message or a password; on GitHub, **More options**).
3. Press **Hand back to the agent**. Any sign-in you made is saved straight
   away, and the agent continues. When the agent asked you to take over and you
   hand back from the chat, the chat also sends it a short message saying you
   are done, so you do not need to type one. (In the chat's large view, collapse
   back to the small window once you have handed back.)

**Close browser** ends the session and frees its memory (sign-ins are kept). A
browser also closes on its own after the idle time set in Config → Browser,
unless someone is watching it.

#### Limits

- **Agents browsing at once** (Config → Browser) caps how many agents hold a
  browser at the same time. When it is reached, the browser idle longest is
  closed to make room; if every one is busy, the agent is asked to try again
  shortly.
- Each headless browser uses roughly 250 to 350 MB of memory while open; each
  live one roughly 500 to 700 MB. Nothing is used while no agent is browsing.
- Some sites refuse automated browsers: see the next section.
- Downloads are not saved: a file the agent needs should come from a direct
  link through its web tools.

#### Sites that turn the browser away

Some sites check whether a visitor is a person and refuse a browser they
think is not: Reddit's *"You've been blocked by network security"*, X's
*"Could not log you in now"* or a locked account, a *"Verify you are human"*
or *"Just a moment..."* check, an *"Access denied"* page. Retrying does not get
past these, and repeated sign-in attempts are exactly what gets an account
locked. So the browser recognises them and stops:

- **A check that passes on its own is waited out first.** Cloudflare's
  *"Just a moment..."* often lets an ordinary browser through by itself a few
  seconds later. The browser gives it up to 25 seconds, without clicking
  anything, and carries on with the site if it clears. Only a check that is
  still there after that, or that asks a person to do something, counts as a
  refusal.
- **A check only a person can pass goes to a person**, as described in
  [When a site asks for a person](#when-a-site-asks-for-a-person) below.
- **The agent is told plainly** that the site refused the browser, that it
  must not try that site again in this task, and to tell you. It gets only a
  short excerpt of the refusal page, so it does not mistake it for the content
  it was looking for. In live mode, when a person could get past it (by signing
  in or completing a check), the agent may ask you once to take over.
- **The next attempt never reaches the site.** For the next 15 minutes the
  agent's browser refuses to open any page of that site (any address on it,
  `old.reddit.com` included) or to click on the refusal page, and tells the
  agent why. Nothing is sent to the site. This stops the agent only: while
  you hold control you can open, reload and complete the check on that site
  yourself.
- **It lifts** when you take control of the live browser and hand it back
  (you may have signed in), when the agent's proxy changes, or after the 15
  minutes. A one-shot screenshot that is refused stops further screenshots of
  that site, not the agent's own, possibly signed-in, browser.

Ordinary pages are not mistaken for refusals: a sign-in form that mentions it
is "protected by reCAPTCHA", or an article that talks about CAPTCHAs, reads
normally.

#### When a site asks for a person

Some checks only a person can pass: Cloudflare's *"Verify you are human"* box,
a CAPTCHA, a *"press and hold"* button. The browser never passes one itself. It
does not tick the box, solve the puzzle or disguise itself to avoid being shown
one. What the agent does instead is set in **Config → Browser → When a site
asks for a person**:

| Option | What happens |
|---|---|
| **Wait for a person** (default) | The browser window in the agent's chat shows the check with **Verify now** and **Skip**, and everyone who can open the agent's chat gets a notification on their phone. The agent waits up to **Minutes to wait for a person** (default 5, up to 120). |
| **Hold until a person does it** | The same, with no time limit. A scheduled or background run, which nobody is watching, waits the set minutes instead. |
| **Skip and move on** | Nobody is asked. The agent is told at once that the site needs a person, and uses another source or asks you. |

**Verify now** opens the large browser view with control already yours. Tick
the box. As soon as the page gets past the check, control goes back to the
agent by itself and it carries on with what it was doing; there is nothing to
hand back. **Skip** tells the waiting agent to move on. If nobody comes in time,
the agent is told so, and can ask you again or use another site. The check
stays usable while it waits: Cloudflare refreshes its box every two minutes or
so, and the browser lets those refreshes through.

- **Per agent.** The agent's **Browser** tab has the same choice, which
  overrides Config → Browser for that agent. Empty uses Config → Browser. Only
  an admin or an owner of the agent can change it.
- **Per task.** An agent picks another option for one task when your request
  makes it clear: "search PropertyGuru for..." means waiting for you, while
  "find me jobs in Singapore" means skipping a site that asks and trying
  another. You can also say it outright ("if a site asks for verification,
  wait for me").
- **Live browser only.** Only the live browser has a screen a person can use,
  so with the headless browser every check is skipped.
- Pressing **Stop** in the chat ends the wait too.
- When nobody is going to pass the check (the wait ran out, or it was
  skipped), the live browser shows a short page saying it stopped at the
  check, rather than leaving the check to turn into an error page at its next
  refresh.

##### When the check keeps coming back

If the box turns green and then the check starts again, the agent's **Browser**
tab has a **Debug trace** card (admins and the agent's owners). A trace starts
by itself whenever the browser waits for a person on a check and keeps going
five minutes after; **Start recording** records the open browser by hand.
It reads the record and names the likely cause:

| What it says | What it means |
|---|---|
| The exit address changed | The address the site sees moved between requests. Cloudflare's pass belongs to one address, so a proxy that hands out a new address per connection loops forever. Use one with a fixed ("sticky") address, or none. |
| The pass was sent and refused | The browser handed back Cloudflare's pass and Cloudflare challenged again: it no longer trusts it for this visitor. |
| The pass was not sent back | The browser did not return the pass on the next page load; the trace says why, in Chromium's words. |
| The pass was never issued | The tick was not accepted on Cloudflare's side. |
| The browser's guard refused a request | One of this browser's own rules stopped a request the check needed. |

**Download trace** saves the whole record as a JSON file to send to whoever is
helping. It keeps page addresses without their query values and cookie names
without their values; nothing you type is in it.

The browser adds nothing to the pages it opens that a bot check could take
for automation: the checks above happen outside the page.

The agent's saved sign-ins keep a site's own sign-in, never a bot check's
pass. Cloudflare ties its pass to the one browser it checked, so each new
browser earns its own (or, more often, is let straight through). A pass
carried over from an earlier browser is what used to bring the check back on
every visit.

What makes a refusal less likely: a [proxy](#going-through-a-proxy) with a
steady address and a matching time zone, signing in yourself through **Take
control** rather than having the agent type a password, and not hammering a
site with many visits in a row.

### Webhooks

Two directions, both configured in the agent's `webhooks` list and manageable
from its **Webhooks** tab.

**Outbound — tell an external system what happened.**

```json5
{
  name: "ops-pager",
  direction: "outbound",
  url: "https://hooks.example.com/olano",
  events: ["agent.error", "budget.exceeded"],
  secret: "${WEBHOOK_SIGNING_SECRET}",
  rate_limit_per_minute: 30,
}
```

Payloads are signed with HMAC-SHA256 so the receiver can verify they came from
you. Delivery retries with backoff, and delivery to internal network addresses
is blocked by default. Webhooks re-register automatically after a server
restart.

**Inbound — let an external system give the agent work.**

```json5
{
  name: "stripe-events",
  direction: "inbound",
  prompt: "This webhook receives Stripe billing events. Summarise what happened, and if a payment failed, draft a dunning email.",
  respond: false,
  accumulate_thread: false,
}
```

Each inbound webhook gets its own unguessable ingress URL. Whatever the sender
POSTs — JSON, form data, plain text — is appended to your `prompt` and handed to
the agent, so you do **not** need to know or control the sender's payload
format. That is the point: you describe what the data means and what to do with
it, and the agent handles the shape.

- `respond: true` runs the agent synchronously and returns its reply in the HTTP
  response. Use for something waiting on an answer.
- `respond: false` (default) acknowledges immediately and works in the
  background.
- `accumulate_thread: true` keeps one running conversation across all payloads
  instead of starting fresh each time.

Both directions keep a delivery history, and the **Test** button sends a sample
(outbound) or simulates an ingest (inbound) so you can check the wiring before
going live.

### Pages: publishing sites and web apps

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

#### Asking for a page

Tell the agent what you want, in plain words: "publish the weekly report as a
page", "deploy the CRM dashboard so I can open it from my phone". The agent
chooses the kind, runs it, and tells you the URL. Then:

1. Open the agent's **Pages** tab and set the viewer password if it is not set.
2. Open the URL and sign in with that username and password.

`pages_deploy` runs commands, so it goes through the same approval as the
agent's shell: under the **safe** and **production** security profiles you
approve each deploy before anything is recorded. An agent with no shell access
cannot use it at all.

#### Static builds

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

#### Servers

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

##### What a server can and cannot see

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

#### Watching and fixing a page

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

#### Limits and switches

All of these live in **Config → Deployed pages** (the
[`pages:` block of `config.yaml`](#pages-deployed-pages)):

- **Run page builds and servers**: the master switch. Off stops every page
  server within a few seconds and refuses new deploys; plain pages and static
  releases that were already built keep serving.
- **Servers per agent**: how many server pages one agent may run at once
  (default 5).
- **Health check every**, **Request timeout**, **Largest upload** and
  **Requests at once, per page**, as described above.

Not supported yet: WebSockets, custom domains, and pages without a password.

### Scheduler and heartbeat

**Scheduled jobs** live in the agent's `schedule` list and appear in the
dashboard's **Scheduler** tab, where you can also create, pause, edit and
trigger them.

```json5
schedule: [
  {
    name: "Morning revenue check",
    cron: "0 9 * * 1-5",
    timezone: "America/Costa_Rica",
    message: "Check yesterday's revenue and flag anything unusual.",
  },
  {
    name: "Weekly deep analysis",
    cron: "0 7 * * 1",
    message: "Review last week's numbers in depth and write the report.",
    model: "olano:pro", // this job only; the agent's own model is unchanged
  },
]
```

**Schedules are standard cron.** Every cron expression here (this list, a
stored task, the Advanced field of the task form, a `when:` line in the
heartbeat file) has five fields: minute, hour, day of month, month and day of
week. The day of week counts from Sunday: `0` or `7` is Sunday, `1` is Monday,
so `1-5` is Monday to Friday. Names work as well (`mon-fri`, `mon,thu`, `sun`).
The Scheduler tab's weekday picker and calendar show the days a task runs on,
and an agent that creates or reads a task is told the same days by name.

Schedules saved before this numbering was adopted were rewritten once into
weekday names for the days they were already running on, so no existing task
changed its days. If one of yours now reads, say, `tue,wed,thu,fri,sat` where
you meant weekdays, it had been running a day late; edit it to `mon-fri`.

A job is a **message sent to the agent** at a time you choose — so anything the
agent can do in a conversation, it can do on a schedule. Use `thread_id` to keep
a job's history in one continuous conversation.

**A model per job.** `model` (`provider:model`, for example `olano:turbo` or
`anthropic:claude-sonnet-4-6`) runs that one job on a model other than the
agent's configured one, so a cheap tier can do the nightly digest and a bigger
one the weekly analysis on the same agent. Leave it out and the job follows the
agent's model, including later changes to it. Each run is billed to the model
that ran, and the run's conversation in the chat list shows which model it was;
on a managed deployment a bigger tier spends credits faster. A model whose
provider is not connected is refused when the job is saved, and a model that
stops working later fails the run with that reason in the job's history rather
than falling back silently. The same field exists on tasks in the Scheduler tab
("Model", with "Agent default" as the first choice) and on the scheduling tools
an agent or AgentFather uses.

**And a reasoning effort per job.** `reasoning_effort` sets how much thinking
the model does before answering on this job's runs, independently of the
agent's own `reasoning_effort`: a nightly digest can run at `low` while the
weekly analysis runs at `high` on the same agent. The value must be one of the
levels the model the job runs on accepts (`model` when set, else the agent's
configured model); the Scheduler tab's "Reasoning effort" field appears only
for such a model and lists exactly that ladder, and every surface that saves a
job refuses a level the model does not take, naming the ones it does. Leave it
out to follow the agent's own setting. Higher levels cost more tokens and time
per run; a level that stops being valid later (a model retired, a package
changed) is dropped for that run with a log line rather than failing the job.

```mermaid
flowchart TB
    subgraph start["What starts the work"]
        direction LR
        S1["schedule list in agent.json5<br/>cron only"] ~~~ S2["Scheduler store<br/>the New task form or the agent itself,<br/>cron or one-time"] ~~~ HB["Heartbeat<br/>every 60 minutes by default,<br/>reads HEARTBEAT.md"]
    end
    start --> RUN["The agent runs, exactly as in a conversation:<br/>tools, files, approvals"]
```

**What a run knows about the time.** Every scheduled run's message opens with
the current date, weekday and time in the job's own `timezone` (UTC beside it),
and every model call the agent makes, scheduled or not, ends its instructions
with today's date. A brief that says "weekdays only" is decided from that
clock, never from the newest date the agent happens to find in its files. The
agent still calls `get_current_time` for the exact time of day during a long
run. Set `LIONCLAW_CLOCK_TIMEZONE` in the deployment's environment to have the
date line name your business timezone beside UTC; `LIONCLAW_CLOCK_STAMP=0`
turns the date line off.

**Late and missed runs.** A firing is not always on the second: the runtime can
be busy, or restarting, at the moment a job is due. A job that fires late still
runs, for up to `scheduler.misfire_grace_minutes` (six hours by default, set in
`config.yaml`), and its message says when it was due and how late it is, so a
morning report that starts at 08:40 is written as a late morning report rather
than dated to the wrong moment. A firing that fell inside a restart runs as
soon as the runtime is back, as long as it is still inside that window
(`scheduler.catch_up_on_boot`, on by default); a dashboard **Run now** never
counts as late. Past the window the firing is recorded in the job's history as
*skipped*, naming the reason, and a notification on the bell says which job
was skipped or failed and links to it; the notification clears by itself on
the job's next completed run. Heartbeat polls and Cortex background phases
keep a short five-minute window, since their next tick is minutes away.

**A run that never ends.** Only one run of a job executes at a time, so a run
that keeps going holds the job: every later firing is recorded as *skipped:
the previous run was still executing*, and a job that fired once and then
never again usually means exactly that. A scheduled run is therefore bounded
by `scheduler.max_run_minutes` (three hours by default). A run still going at
the limit is stopped, its history row says so, the bell carries a notice, and
what it wrote before the cut is kept; the next firing runs normally. Set the
limit to `0` to remove it. The **Next** column on the Scheduler tab shows the
job's next fire time; a dash there on an active job means the runtime holds
no job for it, which saving the task again repairs.

**Fan-out.** With multi-user mode on, `for_each_customer: true` runs the job once
per end user, and the message can use `{customer_key}`, `{platform}` and
`{user_id}`. Use `action` instead of `message` to run a named script from
`allowed_actions`.

**Two places jobs live.** The `schedule` list above is one of them; the other is
the scheduler's own store, which is what the Scheduler tab's "New task" form
writes and what an agent creates when it schedules something itself. The
Scheduler tab merges both and badges each row, so nothing is hidden — but the
difference matters when you go looking for what is triggering a job. A
`schedule` entry is cron-only and lives in the agent's configuration; a stored
task can also be one-time and can be handed to a different agent later.

**Asking an agent about the schedule.** Any agent can read and change its own
scheduled work in chat, and AgentFather can do the same for *every* agent in the
deployment — across both places — so "what wakes the finance agent at 3am?",
"pause that job", or "schedule a Monday summary for the support agent" are all
things you can simply ask for. Changing, creating or immediately running another
agent's job always raises an approval card first, because it makes that agent
start working on instructions you supplied. Ask to see a job's full instructions
before approving an edit: a listing shows only its name, schedule and the first
couple of lines.

**Heartbeat** is different and simpler: the agent wakes up periodically (default
every 60 minutes) and reads `HEARTBEAT.md` in its workspace — a plain markdown
file of standing instructions. It is the low-ceremony way to say "keep an eye on
this" without writing a cron entry. Control it with the `heartbeat` block per
agent, or `heartbeat:` in `config.yaml` for the fleet.

### Memory and self-learning

Memory is markdown files in the agent's workspace, which means you can read and
correct it directly — in the dashboard's **Memory** tab, the **Files** tab, or a
text editor.

- **`MEMORY.md`** — durable facts the agent has learned and should keep.
- **`memories/YYYY-MM-DD.md`** — a daily log. Today's and yesterday's are loaded
  into every conversation.

```mermaid
flowchart LR
    subgraph ws["The agent's workspace"]
        M["MEMORY.md<br/>durable facts"]
        D["memories/YYYY-MM-DD.md<br/>daily logs and the Activity block"]
    end
    C["Every conversation"] -. "loaded at the start" .-> M
    C -. "today's and yesterday's" .-> D
    SL["Self-learning<br/>with Cortex on"] -- "distils recent conversations" --> D
    SL -- "keeps what lasts" --> M
    Y["You, in Memory or Files"] -- "edit to correct" --> M
```

**Self-learning** (`memory.self_learning`, on by default) is part of the Cortex
engine, so it runs only for an agent Cortex is on for, and Cortex is off until
you turn it on ([Cortex and Pulse](#cortex-and-pulse)). While it runs it reads recent
conversations, distils what is worth keeping, and writes it into the daily log
and `MEMORY.md`. A weekly pass consolidates. `self_learning_interval` (default
3600 seconds) sets the pace.

The same background pass (so, again, only with Cortex on for the agent) also
keeps an **Activity** record in the daily log —
what the agent *did*, as opposed to what it learned: each scheduled run,
heartbeat finding, request from another agent, and Cortex phase, one line each
with the time and a short excerpt of the request and the reply. It is written
deterministically (no model call, no credits) on every cycle, so it is there
even on days when the distillation itself is skipped, and it is bounded — one
block per day, capped in length — so a busy day cannot crowd out the rest of the
prompt. Ordinary conversations with people are not copied into it; those go
through distillation only. Ask the agent what it did yesterday and it reads
this block; the dashboard **Overview → Daily highlights** shows the fleet-wide
version of the same day.

Turning on `memory.rag` also indexes memory for meaning-based search, so the
agent can find something it learned months ago without it being in the prompt.

**Per-customer memory** (`memory.per_customer`) is the multi-user variant — see
[Multi-user: two different features](#multi-user-two-different-features).

If an agent has learned something wrong, editing `MEMORY.md` is the fix, and it
takes effect on the next reply.

### Notes

A markdown wiki in the agent's workspace, at the **Notes** tab. Notes link to
each other, and every note shows its backlinks. Both you and the agent can read
and write them (`notes_add`, `notes_get`, `notes_search`, `notes_list`,
`notes_backlinks`).

Use notes for structured knowledge that is neither a passing memory nor a
document you uploaded: project pages, contact sheets, decision records, runbooks
the agent maintains itself.

### Issues

A work tracker where **agents and humans are both first-class participants**.
An issue has a title, body, type, status, comments and an assignee, and every
change is recorded in an audit trail.

Participants are identified by kind: agents as `ai-<name>`, dashboard humans as
`human-<handle>`. You never pass an actor as a parameter — the tracker knows who
is acting from the surface being used, so an agent cannot file an issue as
someone else.

Both halves of those names are **permanent**: an agent's `name` and a person's
**Handle** (set once when their account is created, in Admin → Users). That is
deliberate — an issue assigned last month has to keep pointing at the same
participant after someone edits their display name. So renaming an agent's
**Display name**, or a person's **Name**, never moves their issues. You can
assign by either form: `carlos-m` or `Carlos M.` both resolve, and if two
people match you get an error naming both rather than a guess.

**Delivery differs by kind.** Assign an issue to an agent and it picks it up on
its own poll (default every 15 minutes) and starts working. Assign one to a
person and they get a dashboard notification — click the row, in the bell or in
the Overview list, and it opens that issue's own page. Set
`issues.auto_process: false` on an agent for manual mode, where issues wait
until a human prompts the agent or a scheduled job runs.

**Search is hybrid** — keyword matching plus meaning-based matching — so
"customer can't log in" finds an issue titled "authentication failure". Creating
an issue that closely duplicates an open one is refused, with the existing issue
shown; pass `allow_duplicate` if you meant it.

Surfaces: the **Issues** tab and seven `issue_*` tools every agent has
(`issue_create`, `issue_list`, `issue_show`, `issue_comment`, `issue_assign`,
`issue_close`, `issue_reopen`).

### Revisions

Automatic version history for the deployment's configuration and the files your
agents work with, in the **Revisions** tab.

#### What is tracked

History follows a deliberate **allow-list**, so a new kind of file can never
slip in by accident:

- `config.yaml`, `organization.yaml`, the approval rules, and every agent's
  `agent.json5` (with its `env` values replaced by `__vault__`, so secrets are
  never stored).
- Inside every agent's workspace and in installed skills, files of these types:
    - notes and documents: Markdown, text, HTML, CSV, PDF, Word, Excel,
      PowerPoint (and their OpenDocument equivalents), EPUB;
    - data and configuration: JSON, JSON5, YAML, TOML, INI, XML;
    - scripts and source code: Python, notebooks, JavaScript and TypeScript,
      shell, and other common languages (executable scripts keep their
      executable flag);
    - images, audio and video: PNG, JPEG, GIF, WebP, SVG, MP3, WAV, M4A, MP4,
      MOV, WebM and similar.

Never tracked: `.env` files, the vault, databases, session transcripts, logs,
archives, hidden folders, dependency and cache folders (`node_modules`,
virtual environments, `__pycache__`), another git repository inside a
workspace, a customer-service agent's private per-customer memory, and
configuration files whose name says they hold a credential (`token.json`,
`client_secret.json`, `credentials.yaml`).

Commits are attributed to whoever caused the change (the agent or the human),
so you can answer "who changed this, and what did it look like before". Changes
are debounced (default 45 seconds) so a burst of edits becomes one entry.

If you have data-deletion obligations, set `revisions.include_memory: false`:
memory files carry distilled personal data, and this keeps them out of history.

#### Files that are held back

Some files are tracked but cannot be recorded as they are right now. Each one
keeps the **last version that was recorded** (it is never recorded as deleted,
so a push never removes it from GitHub), and the **Revisions** page lists it
with the reason, until the cause is fixed:

| Reason | Cause | Fix |
|---|---|---|
| Too large | Larger than `revisions.max_file_kb`: 50 MB by default. It can be raised to 100 MB, the most GitHub accepts for one file; above 50 MB GitHub warns on every push. | Make the file smaller, or keep it elsewhere. |
| Looks like a credential | A text file (or an `agent.json5` outside its `env` block) contains something shaped like an API key, an access token, a private key or a password inside a database URL. History is pushed to GitHub, so it never stores one. | Move the secret to the **Vault** and reference it; the file is recorded again on the next change. |
| Does not parse | An `agent.json5` that cannot be read, so its secrets cannot be removed safely. | Fix the file's syntax. |

Very large files make history grow: every version of a 90 MB video is kept.
The **Revisions** page shows the history's total size, and the disk clean-up
repacks it.

#### Seeing what changed

Select any commit on the **Revisions** page to see what it did: how many files
it changed, how many lines were added and removed, which files were added,
modified or deleted, and the diff of each file side by side or unified. A
commit made by a pull from GitHub is a *merge*: its file list shows what the
pull changed on this deployment.

#### Syncing with GitHub

The history can be copied to a GitHub repository you own, as an off-site backup
and as a place to edit files by hand. The history on your deployment stays the
source of truth; GitHub is a copy you push to and pull from.

##### Connecting

1. On GitHub, [create a new repository](https://github.com/new). Make it
   **private** and leave it **empty**: no README, license or `.gitignore`. A
   repository that already has commits is refused the first time, and a public
   one needs an explicit confirmation, because the history holds your
   configuration and your agents' memory.
2. Choose how the deployment signs in to GitHub. When GitHub is already
   connected on the **Connections** page, that connection is selected for you
   and there is nothing to paste; otherwise the access token is.
    - **GitHub connection**: reuse the GitHub account connected on the
      **Connections** page. It reaches every repository that account can reach.
    - **Access token**: create a
      [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
      with *Repository access* set to *Only select repositories* (the new one)
      and the permission *Contents: Read and write*. A classic token with the
      `repo` scope also works. The token is stored encrypted in the vault under
      a reserved name: it is not shown on the Vault page, it is never written to
      `config.yaml` or the history, and agents cannot read it. A token can be
      limited to this one repository, which a connection cannot.
3. On **Revisions**, choose **Connect a GitHub repository**, enter the
   repository as `owner/name` (or paste its URL), the token, the branch
   (default `main`) and the automatic push mode, then **Connect and push**. The
   whole history is pushed at once. The window then shows each step (checking
   the repository, recording a snapshot, uploading the history, saving the
   connection) with a progress bar and the upload's size and speed; a large
   history can take several minutes. Closing the window does not stop it: the
   **GitHub sync** card keeps showing the progress until it finishes.

What is pushed is exactly what the history tracks: `config.yaml`,
`organization.yaml`, approval rules, every `agent.json5` with its `env` values
replaced by `__vault__`, and workspace markdown, including memory unless
`revisions.include_memory` is off. `.env` files, the vault, databases, sessions
and tokens are never in the history, so they are never pushed.

##### Pushing

**Push** sends new commits to GitHub. **Automatic push** decides whether the
deployment also pushes on its own:

| Mode | Behaviour |
|---|---|
| Manual (default) | Only when you press **Push**. |
| Once a day | At most once every 24 hours, when there is something new to send. |
| After every commit | Right after each commit (commits are already grouped: by default 45 seconds after the last change). |

A push never overwrites commits on GitHub that the deployment does not have.
When GitHub has moved on (someone edited a file there), the push stops, the
page says a pull is needed, and automatic pushing pauses until you pull. You can
also choose **Force push**, which replaces GitHub's branch with the deployment's
history; the page asks for confirmation, because the commits only GitHub had are
dropped from the branch.

##### Pulling

**Pull** brings GitHub's changes into the deployment. Pulling is always manual.
**Check for updates** only asks GitHub what is new, without changing anything.

- When only GitHub has new commits, its files are applied as they are.
- When both sides changed *different* parts, the changes are merged
  automatically.
- When both sides changed the *same* lines, nothing is applied. The page lists
  the files in conflict; for each one, compare this deployment's version with
  GitHub's and choose **Keep this box's version**, **Take GitHub's version**,
  or **Edit the merged text** (the text starts with both versions between
  `<<<<<<<` and `>>>>>>>` markers; remove the markers before completing). Then
  **Complete merge**. **Abort merge** drops the pull and changes nothing.

After a pull the page lists what was updated, added and deleted, and offers to
restart agents whose configuration changed (their new configuration applies
from the restart). A pull is careful about what it writes:

- Only files the history tracks are written. Anything else in the GitHub
  repository (a README, a `.env`, any other file) is ignored and listed, and
  the next push removes it from GitHub's branch.
- `env` values in `agent.json5` keep this deployment's real values. A variable
  that only exists on GitHub is left out, with a note to set it on the agent's
  **Variables** tab.
- The `revisions:` block of `config.yaml` always stays as it is on the
  deployment, so a pull can never disconnect GitHub or switch history off.
- Deleting `config.yaml`, `organization.yaml`, the approval rules or an
  agent's `agent.json5` on GitHub is not applied: remove an agent from the
  dashboard instead.
- Files with invalid YAML or JSON5 stop the pull before anything is written.
- If an agent writes one of the affected files while the pull is running, the
  pull stops without applying anything; pull again.

##### When several people sync at once

Only one GitHub operation runs at a time for the deployment: a push, a pull,
**Check for updates**, a merge, connecting, disconnecting, or the automatic
push. If another admin (or the automatic push) is already running one, the
page says which operation and who started it, and the buttons come back as soon
as it finishes; nothing is queued behind it. Pressing **Push** when GitHub
already has every commit sends nothing.

##### Disconnecting

**Disconnect** stops syncing and deletes the stored access token. The GitHub
repository and the history on the deployment are both left as they are.
Connecting again later needs an empty repository.

### Channels and connectors

Channels are how people reach an agent. Configure them on the agent's
**Connectors** tab, which documents every field for every connector.

```mermaid
flowchart LR
    P["A person on WhatsApp,<br/>Telegram, Slack, email, ..."] -- "message" --> C["The agent's connector<br/>allowed senders and owners,<br/>rate limits, message policy"]
    C --> A["The agent"]
    A -- "reply, on the same channel" --> P
```

Supported: **Telegram**, **Slack**, **Discord**, **WhatsApp** (three
connectors, see below), **Signal**,
**Email/IMAP**, **Matrix**, **Mattermost**, **SMS**, **Home Assistant**,
**DingTalk**, **Feishu** and **Lark**, **WeCom (Enterprise WeChat)**, **WeChat
(Weixin / iLink Bot)**, **QQ**, **BlueBubbles (iMessage)**, **DeBox**, the
**OpenAI-compatible API** (below), and inbound webhooks.

Three of those names are easy to mix up:

- **Feishu** and **Lark** are the same connector: Lark is Feishu's
  international edition, run by ByteDance from a separate cloud with its own
  App IDs. The picker offers each as its own row; the Lark row is the Feishu
  form with **Brand / region** preset to Lark (saved as `extra.domain: "lark"`
  under `type: "platform:feishu"`). An app created in one console never
  authenticates on the other, so pick the row that matches where the app was
  made.
- **WeChat (Weixin / iLink Bot)** is the personal messenger: a WeChat account
  linked by QR, no developer console. **WeCom (Enterprise WeChat)**, formerly
  *WeChat Work*, is the company product: an AI Bot with a bot ID and secret
  from the WeCom admin console. Asking an agent to *"connect WeChat"* gets the
  personal one; say **WeCom**, **Enterprise WeChat** or **WeChat Work** for the
  company product, and the agent offers only that.

#### Three ways to connect WhatsApp

**WhatsApp** pairs by scanning a QR code with your phone, the way WhatsApp Web
does. Nothing to register and no business account, but it drives a personal
WhatsApp session, so it is best for your own number and small-scale use.

**WhatsApp (Twilio Business API)** connects through a Twilio account to a
registered WhatsApp business sender. There is no QR code and no session to keep
alive, and it is built for customer-facing traffic. Pick it when your WhatsApp
business number already lives in Twilio, or you want Twilio's console, balance
and usage reports.

**WhatsApp (Meta Cloud API)** connects straight to Meta's own WhatsApp Business
Platform with your Meta app, with no provider in between. Templates, their
approval status and the category WhatsApp bills each one under, and every
delivery report come directly from Meta. Pick it when your number is in your own
WhatsApp Business Account, or you want no second provider's fees or console.

The two business connectors give an agent the same WhatsApp tools, with the
same names and the same instructions (see
[below](#whatsapp-business-the-agents-tools-and-who-may-use-which)), so an
agent's prompt does not change when you move from one to the other.

| | WhatsApp | WhatsApp (Twilio Business API) | WhatsApp (Meta Cloud API) |
|---|---|---|---|
| How it connects | Scan a QR code with your phone, like WhatsApp Web | A Twilio account and a registered WhatsApp business sender | Your own Meta app on the WhatsApp Business Platform |
| Best for | Your own number, small-scale use | Customer-facing traffic, a number already in Twilio | A number in your own WhatsApp Business Account |
| Group chats | Yes | No, one-to-one only | No, one-to-one only |
| Quoting the message being answered | Yes | No | When **Quote the message being answered** is on |
| Starting a conversation | Any time | Within 24 hours of the customer's last message, otherwise an approved template | Within 24 hours of the customer's last message, otherwise an approved template |
| Usage and billing reports | None | Twilio's balance and spend reports | Per message, from Meta |

####### Setting up WhatsApp (Twilio Business API)

Setting it up takes values from the Twilio console and nothing else:

1. Log in at console.twilio.com. The **Account Info** panel on the home page
   shows the **Account SID** (it starts with `AC`) and the **Auth Token** (click
   **Show**). Enter both in the connector on the Connectors tab.
2. To try it without waiting for Meta, use Twilio's WhatsApp sandbox: open
   Messaging, Try it out, Send a WhatsApp message, and from your phone send the
   join code shown there to +1 415 523 8886. Under **Sandbox settings** on the
   same page, clear **When a message comes in** and save, or Twilio's demo
   answers your messages as well.
3. Enter `+14155238886` as the **WhatsApp sender number**, add your own phone
   number under **Allowed sender numbers**, save and restart the agent.

There is no webhook to configure. When the agent starts, the connector tells
Twilio to deliver incoming messages to the deployment's own public address, and
it checks every delivery against your Auth Token before the agent sees it. With
your own registered number, anyone who writes to it reaches the agent. The
shared sandbox number cannot open a conversation with a new number by itself, so
there the agent hears only the numbers under **Allowed sender numbers**, and
only once each has sent the join code (a join lasts three days; send it again
after that). A listed number that has not joined yet is retried automatically.
If a message does not arrive, the connector's diagnostics on the Connectors tab
say whether incoming messages are set up and, if not, why. A self-hosted install
needs a public https address Twilio can reach; Olano Cloud deployments always
have one.

Moving to your own number later means registering it as a WhatsApp sender
(Messaging, Senders, WhatsApp senders), which requires Meta business
verification; clear **Allowed sender numbers** then to accept everyone. Register
a Twilio phone number or a number your business owns that is not on WhatsApp
yet. When WhatsApp's sign-up offers **Use a display name only**, do not pick
it: that gives a free number in area code 555, which Twilio does not support
(see below). If the number was already routed somewhere else in Twilio (a
Studio flow or another app), connecting it here sends its incoming messages to
the agent instead, and the diagnostics name what was replaced.

The **WhatsApp sender number** has to be one of the account's own WhatsApp
senders, or the sandbox number. Any other number (a typo, a number registered
on another Twilio account) fails in the worst way on WhatsApp: Twilio accepts
every message and WhatsApp drops each one later. So the connector checks the
number against the account when it starts: the **sender** row of its
diagnostics turns red and names the numbers the account does have, and every
send from it, ordinary or template, is refused with that reason until the
number is corrected. **whatsapp_sender_status** shows the same list to the
agent.

One kind of number passes that check and still does not work: **a free 555
number from WhatsApp** (a United States number starting +1 555, what **Use a
display name only** gives at sign-up). The Twilio Console lists it as online
under your WhatsApp Business Account, yet Twilio does not support these
numbers: Twilio refuses to route its incoming messages (the diagnostics show
*Invalid Address*) and templates sent from it fail with error 63027. The
connector recognizes one: the **sender** row turns red and says so, and the
agent explains it when a template fails. The fix is to register a Twilio phone
number, or a number your business owns, as a WhatsApp sender on the same
Twilio account and the same WhatsApp Business Account, then enter it as the
**WhatsApp sender number**. Templates belong to the Business Account, not to a
number, so the ones already approved send from the new number without another
review.

####### Setting up WhatsApp (Meta Cloud API)

It takes two IDs, a token and your app's secret, all from Meta:

1. In the Meta App Dashboard (developers.facebook.com/apps), create or open a
   **Business** app and add the **WhatsApp** product. Under WhatsApp, **API
   Setup**, copy the **WhatsApp Business Account ID** and the **Phone number
   ID** of the number the agent should use (the phone number ID is a long
   string of digits shown under the number, not the number itself).
2. Create a token that does not expire: in Business settings
   (business.facebook.com/settings), **System users**, add a system user,
   assign it your app and the WhatsApp Business Account, then **Generate
   token** with the `whatsapp_business_messaging` and
   `whatsapp_business_management` permissions. The temporary token on the API
   Setup page works for a first try but expires after 24 hours.
3. In the App Dashboard, **App settings**, **Basic**, click **Show** next to
   **App secret**.
4. On the agent's Connectors tab add **WhatsApp (Meta Cloud API)**, enter the
   token, both IDs and the app secret, save and restart the agent. The token and
   the app secret are kept in the agent's vault, never in its configuration.

There is no webhook to configure. When the agent starts, the connector
subscribes your app to the WhatsApp Business Account (only if it is not
subscribed yet) and points this one number's webhook at the deployment's own
public address. Meta checks that address once, then delivers every message
and delivery report for the number there, and the connector checks each
delivery's signature against the app secret before the agent sees it. Without
the app secret the agent can still send, but every incoming message is refused.
Only the number's own webhook is changed, never the address set for the whole
Business Account or the app, so several agents can share one Business Account
and one app, each with its own number, and each hears only its own number. If
the number was pointed at another address before, it is replaced and the
diagnostics name the old one. If Meta refuses the setup, the diagnostics show
the address and the verify token to enter by hand in the App Dashboard
(WhatsApp, **Configuration**, subscribing the **messages** field). A
self-hosted install needs a public https address Meta can reach; Olano Cloud
deployments always have one.

The connector checks everything it can when it starts, and its diagnostics on
the Connectors tab show the result, one row each:

- **Connection**: whether Meta accepts the token, and a warning when the token
  expires within 7 days.
- **Permissions**: whether the token holds the two permissions above, and for
  this Business Account. Without `whatsapp_business_messaging` nothing can be
  sent or received; without `whatsapp_business_management` the connector can
  neither read templates nor set up incoming messages.
- **Sender**: the number, its display name and whether Meta has it online. A
  phone number ID that is not on the Business Account you entered turns this
  row red and names the numbers that are; a token that cannot read the number
  at all stops the connector.
- **Incoming**: whether the webhook is set up, and when the last delivery
  arrived.
- **Window**: how many customers can currently receive an ordinary message
  (see the next section).

**Meta's test number** from the API Setup page works for trying the connector,
but it only delivers to up to five phone numbers you add on that page (under
**To**). A send to anyone else fails, and the agent says why. For real
customers, add your own number to the Business Account in WhatsApp Manager.

**Allowed sender numbers** (optional) limits who the agent answers. Leave it
empty to answer everyone who writes to the number. Besides phone numbers it
takes the ID Meta sends instead of a number for someone who hides it behind a
WhatsApp username, such as `US.13491208655302741918`; the agent can reply to
such a person as usual. **Read receipts and typing indicator** (on by default)
marks each message read as it arrives and shows *typing...* while the agent
prepares its answer. **Quote the message being answered** (off by default)
makes each reply quote the person's message.

####### What differs between the connectors

All three connectors handle text, images, documents and voice notes in both
directions, and all three transcribe an inbound voice note and can reply with
generated speech. The differences worth knowing before you choose:

- **Quoting a specific message does not work outbound on the Twilio
  connector.** Twilio has no way to send a quoted reply, so the agent answers
  in the conversation rather than threading onto one message. It still *sees*
  which message a customer replied to. The QR connector quotes normally, and
  the Meta connector quotes when **Quote the message being answered** is on.
- **Both business connectors start a conversation only with an approved
  template.** WhatsApp allows a business to send freely only within **24
  hours** of the customer's last message, and otherwise only a template Meta
  approved in advance. This is Meta's rule and applies to every business
  connection, whichever provider carries it.
- **Groups work on the QR connector.** The Meta connector answers one-to-one
  chats only; a group message is ignored.
- **Account balance and spend reports are Twilio's.** The Meta connector
  reports instead, per message, whether Meta billed it and under which
  category (see the tools below); Meta's own invoices are in WhatsApp Manager.

#### The 24-hour window, and why a scheduled WhatsApp message may not send

More than 24 hours after a customer's last message, WhatsApp delivers only
**pre-approved template messages**, and it discards an ordinary message with no
error at all. So that a message can never vanish silently, the agent refuses
such a send and says the window has closed, instead of reporting success for
something the customer never received.

The Meta connector knows a window only from the messages it received itself,
because Meta keeps no message history anyone can ask for: someone who last
wrote before the connector was set up counts as outside the window until they
write again. What it received is kept across restarts.

Outside the window the agent sends an approved template instead: it lists the
templates WhatsApp approved for the account, picks the one that fits, and fills
in its placeholders (see the next section). There is nothing to set up for this
on the Connectors tab. Attachments cannot be sent outside the window at all,
with or without a template.

This affects scheduled jobs, reminders and any other message the agent starts
on its own. A scheduled job's reply to a WhatsApp chat is an ordinary message,
so it arrives only if that person wrote in the last 24 hours. For a job that
must reach someone either way, name the template in the job's instructions, for
example *every Monday at 9, send the weekly_summary template to +1 555 123 4567
with this week's total*. Replies to an incoming message are always inside the
window and are never affected.

#### WhatsApp Business: the agent's tools, and who may use which

Beyond replying in a conversation, an agent with either business connector,
WhatsApp (Twilio Business API) or WhatsApp (Meta Cloud API), gets one toolkit,
**WhatsApp Business**. The tools have the same names, take the same values and
answer in the same shape on both connectors, so one set of instructions works
whichever carries the agent's number; when an agent has both, the tools use the
Meta one. Every tool is listed on the connector's own panel (Connectors tab,
edit the connector, the **Tools** section) with an on/off switch, and the same
switches are on the agent's Tools tab. The tools come in two kinds, so the
agent that talks to customers and the agent that manages the WhatsApp Business
account can be different agents with different powers.

**Everyday tools**, on for every agent that has the connector. Switch one off
to take it away from that agent.

- **whatsapp_template_list** lists the templates WhatsApp approved for the
  account, with their placeholders and buttons. On the Meta connector it also
  shows each template's header and footer, and the category WhatsApp actually
  bills it under, noting when WhatsApp moved it to another category than the
  one it was created in.
- **whatsapp_template_send** sends an approved template to a number: the only
  message WhatsApp delivers to someone who has not written in the last 24
  hours, and the way a business starts a conversation. The agent fills every
  placeholder (a template is never sent with one left empty, and never unless
  WhatsApp approved it), then waits a few seconds for WhatsApp's verdict and
  says whether the message was delivered or why WhatsApp refused it, for
  example a Twilio sandbox number that has not joined, or a recipient missing
  from the list of Meta's test number. A line break inside a placeholder
  becomes a space, because WhatsApp refuses one there. A template whose name
  exists in several languages is sent in the language the agent names. On the
  Meta connector a placeholder in the template's header is filled as
  `header` and one in a button's link as `button_1`, `button_2` and so on, as
  the list shows; an image, video or document header takes an https link to
  the file.
- **whatsapp_message_status** checks later whether a message was sent,
  delivered or read, or why it failed. On the Meta connector it also says
  whether Meta billed the message and under which category, or that it was
  free (a reply inside the 24-hour window, for example).
- **whatsapp_window_status** says whether the 24-hour window is open for a
  number, so the agent knows whether an ordinary message or only a template
  will arrive, and when the window closes.

**Account administration**, off until an administrator switches them on for
the one agent that manages the account, because what they change or read
belongs to the whole WhatsApp Business account (or Twilio account) and every
agent on it. No agent can switch
them on by itself, for itself or for another agent: it takes an administrator,
on the connector panel or the Tools tab, or by asking AgentFather while signed
in as one.

- **whatsapp_template_create** creates a template and submits it to WhatsApp
  for approval: the text with its placeholders, and optionally buttons, either
  up to ten reply buttons the person answers with one tap, or up to two
  website buttons and one phone button.
  On the Meta connector a template goes to WhatsApp's review the moment it is
  created, so there are no unsubmitted drafts; the agent shows you the text
  first.
- **whatsapp_template_submit** submits a template that was created without
  submitting (Twilio). On the Meta connector, where creating already submits,
  it says where the template stands instead.
- **whatsapp_template_status** reports the approval status of one template or
  of all of them, with WhatsApp's reason when it rejected one, and on the Meta
  connector its quality rating and any change of category.
- **whatsapp_template_delete** deletes a template. It needs the template's
  name typed back, because a deleted template is gone for every agent on the
  account and a replacement waits for WhatsApp's review again. On the Meta
  connector it deletes the one language the agent picked, not every language
  of that name.
- **whatsapp_sender_status** lists the account's WhatsApp numbers: whether
  each is online, Meta's quality rating, the daily messaging limit, and the
  business profile.
- **whatsapp_profile_update** changes the business profile customers see when
  they open the business in WhatsApp: the about line, description, address,
  email, website and category. The display name is changed in the Twilio
  Console or in WhatsApp Manager, since Meta reviews it.
- **twilio_account_balance** shows the Twilio account's balance (Twilio only).
- **twilio_usage_summary** shows the WhatsApp spend for this month, last
  month, today or yesterday, by category, with the account's total (Twilio
  only; on Meta, billing is per message, above, and invoices are in WhatsApp
  Manager).

The managing agent uses its own connector's account when it has a business
connector. Otherwise it needs keys in its vault, which the Tools tab asks for:
**WHATSAPP_META_ACCESS_TOKEN** and **WHATSAPP_META_WABA_ID** for a Meta
account, or **TWILIO_ACCOUNT_SID** and **TWILIO_AUTH_TOKEN** for a Twilio
account.

What keeps this safe:

- **Only the owner, or an administrator, can have the agent use any of these
  tools.** A customer writing to the agent on WhatsApp cannot make it list,
  send, create or delete a template, or read the account, even on an agent
  that holds the tools. Scheduled jobs you set up run with your authority, so a
  reminder job can send a template.
- **Approval before acting.** Under the Supervised and Locked-down security
  profiles, every template send, creation, submission and deletion, and every
  profile change, waits for your approval. The Trusted profile does not ask, so
  choose Supervised for an agent whose template sends you want to review.

**On Twilio, templates need your own WhatsApp number.** Twilio's shared
sandbox number (+1 415 523 8886) can send only Twilio's own three sample templates, never the
ones you created, so from the sandbox every template send fails (Twilio error
63027), even right after the person wrote to you. The agent says so instead of
trying. Register your own number in the Twilio Console (Messaging, Senders,
WhatsApp senders) and enter it as the connector's **WhatsApp sender number**;
templates approved for your account then send from it. Ordinary replies work
from the sandbox as before. If a send from your own number still comes back
with error 63027, the agent reports the number's state and the WhatsApp
Business Account it belongs to. A free 555 number from WhatsApp is not
supported by Twilio (see above). A number that is not online delivers nothing.
For an online number, the usual cause is that WhatsApp does not hold that
template for its Business Account: it was approved while the Twilio account
was connected to another Business Account, or deleted or disabled in WhatsApp
Manager since. Look for it in WhatsApp Manager, under that Business Account,
Message templates; if it is not there, creating and submitting the template
again puts it there.
**whatsapp_sender_status** shows every number with its account.

On the Meta connector the same kind of failure reads *template does not exist*
(error 132001): WhatsApp holds no approved template of that name, in that
language, on the connector's Business Account. The agent names the Business
Account to look in; WhatsApp Manager, Message templates, under that account,
shows whether the template is there and in which languages.

A typical conversation with the managing agent:

- *"Create a utility template that tells a customer their order is ready, with
  their name and order number, and a button to track it."* The agent drafts
  it, shows you the draft, checks it against WhatsApp's rules, and submits it
  once you agree.
- *"Is the order-ready template approved yet?"* It asks WhatsApp and
  answers with the status, or WhatsApp's reason if it was rejected.
- *"Is our number online, and how much did WhatsApp cost us this month?"* It
  reads the sender status and, on Twilio, the usage summary.
- *"Send the order_ready template to +1 555 123 4567 for Maria, order 1042."*
  That is the sending side, on the customer-facing agent.

WhatsApp's rules for a template, which the agent checks before submitting so a
review is not wasted: placeholders are either numbered {{1}}, {{2}} and so on
without gaps, or named in lowercase such as {{first_name}}, never both in one
template (the agent fills a named one by its name); two placeholders never sit
side by side; the text neither starts nor ends with one; each placeholder needs
at least two more words of fixed text around it; every placeholder has a
realistic sample value for the reviewers; and the text is at most 1024
characters (640 with website or phone buttons). A website button's address may
end with one placeholder; button titles are at most 20 characters. **UTILITY**
is for updates about something the customer asked for, like orders,
appointments or account alerts. **MARKETING** is for promotions. One-time-code
templates are built in the Twilio Console or in WhatsApp Manager.

WhatsApp usually reviews a template within 24 hours. The status then reads
*approved*, *rejected* (with a reason), or later *paused* or *disabled* if
recipients block or report it. A submitted template cannot be edited: to change
one, create a corrected copy under a new name. Meta bills each template message
it delivers, and WhatsApp's policy requires that people agreed to receive
messages from the business.

#### WhatsApp owners, files and repeated messages

These apply to the WhatsApp connector paired by QR code, except where a point
names the business connectors.

- **Owners can be listed by phone number, on all three connectors.** An entry
  such as `+65 9123 4567` in *Owner numbers* is recognised as that person's
  account whatever form WhatsApp, Twilio or Meta reports them in, including
  WhatsApp's privacy identifiers; on the Meta connector a business-scoped user
  ID works too. Owners can always write to the agent, even when the allowed
  list omits them, and get the treatment described in
  [Human in the loop (HITL) and approvals](#human-in-the-loop-hitl-and-approvals). On the Twilio sandbox
  number, also list an owner under *Allowed sender numbers*, because the
  sandbox only hears numbers on that list.
- **Files sent to the agent are kept in its workspace.** Documents
  (spreadsheets, PDFs, Word files), videos and other files are saved in the
  agent's `uploads/` folder, like photos. A document's text is also given to
  the agent directly, but only the saved file has everything: a spreadsheet's
  every row and its formulas, a template the agent can fill in. Files over
  100 MB are not copied.
- **A message is answered once.** When the linked phone reconnects, WhatsApp
  can deliver messages again that the agent already handled. The connector
  remembers the message ids it processed, including across restarts, and
  ignores the repeats.

#### Group replies, and making an agent stay quiet in groups

Every group-capable connector (Telegram, WhatsApp, Discord, Slack, Matrix,
Mattermost, DingTalk, DeBox) carries two independent selects under **Behavior**
on its Connectors panel:

- **Group replies** — which group messages get an answer. *All messages* (the
  usual default) answers everything; *@mentions only* answers when the bot is
  @mentioned, replied to, sent a `/command`, or matched by one of the wake-word
  patterns on the same panel; *Never reply (silent)* answers nothing in groups.
- **Group listening & history** — which group messages the agent did *not*
  answer are kept in its own local history, so it can be asked later what a
  group discussed. Off by default, so nothing is stored until you turn it on.

Direct messages are always answered, in every mode — these two settings govern
group traffic only.

Two things to know when you want an agent genuinely silent in groups:

- **Set both.** *Never reply (silent)* stops the answers; *Group listening &
  history* set to *Off* (its default) stops the recording. They are separate
  choices because "watch without answering" is the common case.
- **Restart the agent.** A connector's settings are read when the agent starts,
  so a saved change reaches the running connector only on its next restart —
  the panel says as much at the top. Until then the form shows the new value
  while the live connector still uses the old one. On WhatsApp you can confirm
  which one is in force without waiting for someone to post: the connector's
  **Diagnose** panel reports the modes the *running* connector resolved, in
  words ("Group replies: none — never answers in groups"). If that disagrees
  with the form, the agent has not been restarted since the change.

Two deliberate exemptions survive *Never reply (silent)*: a group listed under
**Free-response chats** on the same panel is answered anyway (a per-chat
exemption is more specific than the global mode), and a `/command` is answered
when its sender is listed under **Allowed users** or **Owner users** — the
owner's way back in. With both of those user lists empty nobody is singled out,
so in silent mode even a `/command` goes unanswered rather than letting any
group member wake the agent.

#### Replying to a specific message

When someone long-presses an earlier message and taps **Reply**, the agent now
sees what they are replying to. Before, a reply arrived looking like any other
message — so "does this still apply?" reached the agent with no "this", and it
had to guess or ask.

The quoted message is shown to the agent above the new one, marked as a quote.
It is treated as **context, not instructions**: quoting a message never gives
its text authority over the agent, which matters in group chats where the
quoted words were usually written by someone else. Long quotes are shortened.

**Replying to a photo, a voice note or a file works too.** Pointing at a
picture and asking "what does this say?" is how people use chat, and a photo
sent without a caption has no text to quote — so the agent is told what kind of
attachment was quoted and is handed the file itself, saved into the agent's
workspace exactly like an attachment sent directly to it. It can then look at
the image, transcribe the voice note, or read the document, and answer about
the thing you pointed at. If the file cannot be fetched — too large, or an old
message whose copy has expired on the messaging service — the agent is told a
reply happened and that the attachment was unavailable, rather than being left
to guess.

Text quoting works today on **Telegram**, **WhatsApp**, **Discord**,
**Feishu/Lark** and **WeCom**. Quoted attachments work on **WhatsApp** (images,
video, voice notes and documents), **Telegram** (photos, voice notes, audio and
documents) and **Discord** (images and audio); on the others a quoted
attachment is announced but not fetched. On **Slack**, **Matrix**, **iMessage**,
**Email** and the two WhatsApp business connectors (**Twilio** and **Meta Cloud
API**) the agent is told that a reply happened but not yet what was quoted. The remaining connectors do not report replies yet.

Nothing to configure — it is on for every agent. On WhatsApp the feature lives
in the bundled bridge, so it starts working after the agent's WhatsApp channel
next restarts; your pairing is kept and you will not be asked to scan a QR
again.

#### The OpenAI-compatible API

Add the **OpenAI-compatible API** connector to let any OpenAI-compatible app or
SDK talk to one agent — Open WebUI, LobeChat, LibreChat, the official OpenAI
client libraries, or your own code. The agent answers as itself: same prompt,
same tools, same memory as on any other channel.

The form shows you both things you need. Press the generate button beside
**API key** to mint one (or paste your own), save it, and restart the agent.
The **API base URL** field above it is the real address for this agent on this
deployment — copy it as-is with the copy button; it is not a template to edit.
Give your client that URL as its base URL and the key as its API key.

The agent's name is a segment of that URL — that is what selects which agent
answers, so several agents can each serve their own endpoint on one deployment.
The model field your client sends is ignored (the agent *is* the model);
`/v1/models` advertises the agent's name if the client wants to pick from a
list.

**The API key is required for that public URL.** Without a real one it answers
403 and the agent is reachable only from the deployment machine itself. Keys
shorter than 8 characters, and obvious placeholders like `changeme`, count as
missing.

Available endpoints: `/v1/chat/completions` (streaming and non-streaming),
`/v1/responses` (including `previous_response_id` chains), `/v1/runs` with
`/v1/runs/{id}/events` for structured progress as an agent works, `/v1/models`
and `/health`.

Conversations continue automatically: a client that resends its conversation
each turn is recognised and kept on one thread. To control that explicitly, send
an `X-Olano-Session-Id` header — the same value continues the same conversation,
a new value starts a fresh one. (That header needs the API key set; without one
it is refused, so nobody can read another conversation by guessing ids.) A
`system` message applies to that request only and is never stored.

**Bind host** and **Bind port** live under **Advanced** and most setups never
touch them: the port is allocated per agent automatically, and the base URL
above works regardless of either. Set them only when something on your own
network must reach the agent's port directly. If you do expose it on the
network, an API key is mandatory — the server refuses to start without one.

To rotate the key, clear the current one, generate a new one and save the
connector; the eye and copy buttons beside the field show and copy the value
when you need it again.

**The rule that causes the most confusion: credentials never go in
`agent.json5`.** Write the channel with its settings only —

```json5
channels: [{ type: "telegram", allowed_users: ["123456789"] }]
```

— and store the token in the vault under the name that connector expects
(`TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`,
`DISCORD_BOT_TOKEN`, and so on; the Connectors tab names the key for each).
The Connectors tab does that for you: type the token into the connector's
credential field and press **Save**, and it is stored in the vault as that
agent's own value, never in `agent.json5`. If the vault cannot take it (for
example, it is locked), nothing is saved and the message says why.

**WhatsApp and WeChat (Weixin) don't start from a token — they pair by QR
code.** Their connector forms open on a **Pair / Link** button: click it, scan
the QR with the phone that owns the account, and the credentials are issued
and stored automatically (for WeChat the scanned login is also pinned to the
agent, so several agents can each hold their own WeChat account). The
credential fields for these two sit collapsed under the form as an advanced
manual fallback — you never need them for a normal setup. Asking the agent in
chat works too: it shows the same QR right in the conversation.

**Two agents on the same platform need per-agent overrides.** Every agent reads
the same credential name, so if two agents both use the shared
`TELEGRAM_BOT_TOKEN` they authenticate as the *same bot* — and one of them
silently stops receiving messages. Store each agent's token as a per-agent
override: ask the agent to connect itself (it sends a magic link and stores the
token under its own name), or in the **Vault** page pick the agent when saving
the key. Never paste a token into a chat — a magic link is how an agent
collects one ([Ask the agent: self-configuration, magic links and admin tools](#ask-the-agent-self-configuration-magic-links-and-admin-tools)).

Restart the agent after adding a connector. A connector with no credential is
skipped at boot and raises a dashboard notification naming the key to set, and
the connector's diagnostics on the Connectors tab say the same. That is the
symptom to look for when a bot never replies.

**WhatsApp attachments need a current bridge.** WhatsApp hands out media
encrypted, so voice notes, photos, videos and files are decrypted by the local
WhatsApp bridge before an agent ever sees them — which means an out-of-date
bridge shows up as attachments that arrive with nothing in them. A voice note is
the visible case: it carries no text of its own, so the agent is left answering
an empty message and replies as if nothing had been said, while the same voice
note on Telegram transcribes normally. Updating the deployment ships a current
bridge and the agent restarts it on its own, keeping the existing pairing — no
re-scanning a QR. If an attachment still cannot be read, the agent is told so
explicitly and asks the sender to resend, rather than answering an empty
message. Very large attachments are skipped by design; the ceiling is
`WHATSAPP_MEDIA_MAX_BYTES` (25 MB by default), and the decrypted files are kept
for `WHATSAPP_MEDIA_RETENTION_HOURS` (24) before being cleaned up.

**For non-technical owners**, an agent with the right tools can send a single
secure link that walks the person through connecting their own accounts —
scanning a QR for WhatsApp, pasting a validated bot token for Telegram or
Slack — without any token passing through the chat.

#### LinkedIn outreach: profile visits, connection requests, inbox and posts

The **LinkedIn** connection on the Connections page can post as you, but
LinkedIn does not let ordinary apps send connection requests, follow people,
read messages or comment. The **LinkedIn outreach** toolkit does those things on
your own LinkedIn account, in one of two ways: through Olano Cloud, as a paid
add-on (of a managed deployment, or of your account for a self-hosted install
running on an Olano API key), or through your own Unipile workspace (below),
on any install. With neither, the tools say what is missing and do nothing.

**Turning it on.** First switch the **LinkedIn outreach** add-on on, in the
place that matches your install:

- **An Olano Cloud deployment:** in the Olano Cloud console, on the instance's
  page, open the **Plan** tab, and under **LinkedIn outreach** choose how many
  LinkedIn accounts to pay for (one seat per account, so one per agent that
  will do outreach). You can also pick it while deploying a new instance, in
  the Add-ons step. It takes effect right away.
- **A self-hosted install on an Olano API key** ([Models and credits](#olano-credits-on-a-self-hosted-install)):
  on `cloud.olano.ai`, under **Billing**, the **LinkedIn outreach for your API
  keys** card. The seats are bought for your account on their own monthly
  subscription and shared by every install using your API keys; the first
  switch-on opens a checkout, and the seats appear once the payment is
  confirmed.

Either way you can add seats, disconnect an account or switch the add-on off
from the same place later. See [cloud.olano.ai](https://cloud.olano.ai) for the
current price. Without the add-on the tools answer that it is not switched on,
and say which of the two places to go.

Then add the **LinkedIn outreach** toolkit to an agent (Tools tab, or ask the
agent to add it with `self_add_toolkit`) and ask the agent to connect LinkedIn:
it calls `linkedin_connect_account` and gives you a secure link. Open it, sign in to LinkedIn on that page (two-step verification, if you
use it, happens there too) and approve. Your password never passes through the
chat or the agent. The link expires after about an hour; ask for a new one if
it lapses. Each agent has its own LinkedIn account, so two agents can work two
different accounts (each uses one seat), and connecting a different account
replaces the agent's current one. When every seat is taken, the agent says
which agents hold them; add a seat or disconnect one first.

**Using your own Unipile workspace instead.** If you already have a
[Unipile](https://www.unipile.com) account, put its API key and DSN in the
Vault as `UNIPILE_API_KEY` and `UNIPILE_DSN` (the DSN is shown in the Unipile
dashboard, for example `api1.unipile.com:13111`; with or without `https://`).
An agent can ask you for them with a secure link (`magic_link_request_secret`),
so they never pass through the chat. As soon as both resolve for an agent, its
LinkedIn tools talk to your workspace directly: no add-on and no credits,
Unipile bills you, and it works on self-hosted installs too. Set them on one
agent (its Vault override or `env`) to use them for that agent only. Which
LinkedIn account of the workspace an agent uses:

| How | When |
|---|---|
| `UNIPILE_ACCOUNT_ID` in the Vault | Always wins: the Unipile account id of the LinkedIn account this agent must use |
| The sign-in link | Ask the agent to connect LinkedIn: the account the link adds to your workspace becomes this agent's |
| Automatic | A workspace with exactly one LinkedIn account that no other agent uses is picked up without asking |

The same daily limits and the same approvals apply as on the managed service.
Disconnecting deletes the account from your workspace only when it was added
through the agent's sign-in link; an account you connected in the Unipile
dashboard yourself is left there.

**What the agent can then do:**

| Tool | What it does | Needs your approval | Charged |
|---|---|---|---|
| `linkedin_account_status` | Is LinkedIn connected, did the last sign-in link finish, what are the daily limits | No | No |
| `linkedin_visit_profile` | Opens a member's profile, so they see the visit in "Who viewed your profile", and reads it | No | Yes |
| `linkedin_follow_profile` | Follows a member | Yes | Yes |
| `linkedin_send_invitation` | Sends a connection request, with a personal note (up to 300 characters) or without one | Yes | Yes |
| `linkedin_list_sent_invitations` / `linkedin_withdraw_invitation` | Lists pending requests you sent, and withdraws one. LinkedIn shows how long ago a pending request was sent only in its own words ("Sent today", "Sent 1 week ago"), never an exact time, so that is what the list shows | No | No |
| `linkedin_list_conversations` / `linkedin_read_conversation` | Lists your inbox and reads one conversation, oldest message first | No | No |
| `linkedin_send_message` | Replies in a conversation, or sends a first message to someone you are already connected with | Yes | Yes |
| `linkedin_list_posts` | Lists a member's recent posts (does not notify them) | No | No |
| `linkedin_comment_on_post` | Comments on a post, publicly, under your name | Yes | Yes |
| `linkedin_react_to_post` | Reacts to a post (like, celebrate, support, love, insightful, funny) | No | Yes |
| `linkedin_disconnect_account` | Disconnects the account, which frees its seat for another agent | Yes | No |

"Needs your approval" follows your approval settings: with the default
settings, anything that speaks for you to another person waits for a yes on
the Approvals page or in chat first.

**What it costs.** The add-on is billed with your subscription, per seat; adding
a seat charges the rest of the current billing period right away, and removing
one or switching the add-on off credits the unused part to your balance.
Switching it off disconnects every LinkedIn account at once. On top of the
seats, each charged action above costs a small amount of credits. Nothing is
charged when LinkedIn refuses an action, or when nothing was sent (for example
a connection request to someone you are already connected with). The agent
tells you what each action cost; the Insights tab shows the total as
**LinkedIn outreach**.

**Daily limits.** LinkedIn restricts accounts that act too fast, so each account
has a rolling 24-hour limit per kind of action (visits, follows, connection
requests, messages, comments, reactions). The agent can tell you the current
limits with `linkedin_account_status`. When one is reached the agent stops and
continues the next day. LinkedIn has limits of its own on top: a weekly cap on
connection requests, and, on free accounts, only a few requests with a personal
note per month. When LinkedIn refuses, the agent passes on LinkedIn's reason and
can offer to send the request without a note.

**Messages and posts are other people's words.** What the agent reads in your
inbox or in someone's posts is treated as information, never as instructions to
the agent, so a message that says "ignore your instructions" changes nothing.

**When LinkedIn signs you out.** LinkedIn ends sessions on its own schedule. The
agent then says the account needs reconnecting and offers a new sign-in link.

LinkedIn's terms do not allow automating an account, and LinkedIn may restrict
accounts it believes are automated. The limits above keep the pace well below
what LinkedIn tolerates, but the risk stays with the account owner: keep the
outreach personal and relevant.

### Cortex and Pulse

**Cortex** is the autonomous cognition engine: the work an agent does between
conversations. Distilling learnings into memory, tidying its notes and
workspace, refining its skills, expanding ideas, and generating suggestions.
Watch it in the **Cortex** tab.

It runs on two model tiers — a `fast_model` for light passes and a `deep_model`
for heavy ones — with `max_deep_runs_per_day` (default 12) as the cost ceiling.
`intensity` (`relaxed` by default, or `balanced` / `aggressive`) sets the
overall pace.

```mermaid
flowchart TB
    IN["Conversations, scheduled runs, the workspace"] --> CX["Cortex phases<br/>a fast model for light passes, a deep model for heavy ones"]
    CX --> MEM["Memory and notes<br/>distilled, tidied"]
    CX --> SK["Skills<br/>refined"]
    CX --> ID["Ideas, brainstorms,<br/>prototypes"]
    CX --> PU["Pulse<br/>suggestions"]
    PU --> YOU["You: at the start of a session,<br/>on a channel or in a daily digest"]
    CX -. "outward, irreversible<br/>or identity-changing" .-> AP["Waits in Pulse, Approvals"]
```

**Cortex is off until you turn it on.** On a new deployment the **Cortex
engine** row in **Cortex → Autonomy** reads Off in the "All agents" column, so
no agent does background cognition: no scheduled phases, no distilling of
conversations into memory, and no `cortex_*` tools in chat (an agent can still
read and write its memory when you ask it to). Switch the "All agents" column
on to start it for every agent, at the relaxed cadence unless you pick another;
or leave the fleet off and set one agent's own column to On to pilot it there
first. An agent that was set Off on its own stays off either way.

Settings resolve with one rule in both directions: **the agent wins when it has
an opinion, the deployment applies when it doesn't**. Every per-agent toggle is
three-state, including the master switch, so "off for the fleet, on for one
agent" works as well as the reverse.

#### The funnel: brainstorms, ideas, prototypes, projects

Everything creative Cortex produces lives in one staged funnel, **brainstorm →
idea → prototype → project**. Each item is a folder in the agent's workspace
(`brainstorming/`, `ideas/`, `prototypes/`, `projects/`) tracked by a single
index, and the Cortex tab shows all four stages for every agent. Items move down
the funnel by **promotion**: a brainstorm's strongest angle becomes an idea, an
idea becomes a small runnable prototype (run on the spot when it is safe to, with
the output kept in its `RESULTS.md`), and a prototype that has proved itself
becomes a project with a `README`, a phased `PLAN.md` of checkboxes, a `STATUS.md`
and a `JOURNAL.md`. A promoted item stays where it was, marked promoted and
linked to what it became; the new item links back. **Create project** on a card
still jumps straight to a project, for the case where you have already decided.

You can also review the funnel from a conversation with **AgentFather**: ask it
what an agent (or every agent) has been brainstorming, which ideas score below
4, to discard all of an agent's brainstorms, or to promote a specific idea to a
prototype. It reads the items and their ratings, recommends what to keep, and
does the same discard, restore and promote the cards do, each behind an
approval card. See [Asking the agent](#reviewing-cortex-across-agents).

```mermaid
flowchart LR
    B["Brainstorm"] -- "strongest angle" --> I["Idea"]
    I -- "small build, run" --> P["Prototype"]
    P -- "proved itself" --> J["Project: PLAN, STATUS, JOURNAL"]
    R["Rate & Promote<br/>scores every item 1-10"] -.-> B & I & P & J
    J --> X["Advance Work: executes steps,<br/>schedules its own follow-ups,<br/>publishes results"]
```

**Ratings.** The **Rate & Promote** phase reads every active item's folder and
gives it a score from 1 to 10 with a one-line reason, a recommendation
(**promote**, **execute**, **explore**, **keep** or **discard**) and the single
most useful next step. The score shows on the card and in the item's drawer, and
the agent reports it when you ask about an item in chat. Ratings are always
written, whatever the autonomy posture; what happens next depends on it.

**With Fully Autonomous on** (the default once Cortex is on), the funnel runs
itself:

- An item recommended for promotion and scored at or above the **promotion
  threshold** (`cognition.promote_threshold`, default 8) is promoted to the next
  stage, at most `max_promotions_per_run` (default 1) per run and never past the
  daily deep-run cap. An item recommended for discarding and scored 3 or lower is
  discarded; its folder stays and **Restore** brings it back
  (`cognition.auto_discard`, default on, turns this half off).
- **Advance Work** then executes. It takes the highest-rated prototype or project
  with a doable next step and finishes one real step end to end: builds, runs,
  verifies, ticks the `PLAN.md` box it genuinely completed and records the result
  in the item's folder. When the work continues past the run, it **schedules a
  follow-up step for itself** (a check tomorrow, a daily pull, the next phase);
  those appear in the **Scheduler** tab named `Cortex: <item>`, bounded to a few
  pending steps per agent (`cognition.max_self_scheduled_tasks`, default 6), two
  per item and at most hourly, and each runs as a normal turn of the agent inside
  the item's folder. It writes procedures it will need again as learned skills,
  publishes results as a page from the item's folder when they are worth showing
  (you set the viewer password under the agent's **Pages**), asks another agent
  on the deployment when that agent has a capability it lacks (sending an email,
  say), and sends you a notification when something is ready to look at.
- **It works as a team.** Every background run that executes, rates or plans
  is handed the same roster the agent sees in chat, with what each teammate can
  do: the services its tools reach (email, calendar, Slack, GitHub, a CRM, a
  trading account), whether it has a shell, and the MCP servers it is connected
  to with their tool counts. When a step needs one of those, the run asks that
  agent for exactly that piece and records the reply in the item's folder, so a
  project whose plan says "post the summary to Slack" or "pull the open tickets
  from Linear" gets done by the agent that holds the connection. Who an agent
  may see and message is still yours to set: the agent's **Agent visibility**
  and the org chart apply to background runs exactly as they do in chat.
- `cognition.approval_required` still holds on every one of those runs, the
  self-scheduled steps included: by default an email, a payment or an identity
  edit waits in **Pulse → Approvals**, and the item's `STATUS.md` says so.

**With Fully Autonomous off**, ratings are advice. Each promotion candidate
becomes a card in **Pulse → Approvals** whose **Approve** does exactly what the
autonomous run would have done; nothing is discarded, and Advance Work develops
items without scheduling, publishing or reaching out.

**Nothing is generated twice.** Every generating run (brainstorming, ideas,
prototyping, a brainstorm promoted to an idea) is handed the whole portfolio:
every brainstorm, idea, prototype and project, including the ones already
promoted and the ones discarded, and is told that a rewording, a narrower or
wider cut, or a recombination of existing items is not new. What the model
returns is then checked against all of it on content, not just title: a
candidate that restates an item at any stage or status is dropped and named in
the run's summary ("Skipped 2 near-duplicates: ..."), a candidate that matches
an active item of the same kind improves that item instead of adding a copy,
and two candidates from the same run that repeat each other keep only the
first. A discarded item stays discarded; it is never recreated under a new
name. The Rate & Promote reviewer looks for duplicates among what already
exists too, and marks the weaker copy **discard** with a link to the one to
keep, so the pair is visible in the item's details rather than silently
merged.

Steer all of it with **Creative direction** (`CORTEX.md`): the reviewer reads
your goals and your Avoid list and rates against them.

**When a background run fails.** A run that a provider refuses with a
temporary condition — a rate limit, an overloaded model — is not lost: the
scheduler runs that phase again about 15 minutes later with a fresh start, and
once more an hour after that if needed, before waiting for the next scheduled
time. Runs across the deployment also never all start in the same minute (each
agent's scheduled minute is spread out) and no more than `worker_max_concurrency`
of them talk to a model at once, so the deployment does not bring a rate limit on
itself. A failure the deployment can heal on its own reaches the notification
bell and the Pulse **Ops** lane only once the same run has failed **twice in a
row**; a failure that needs you — an expired sign-in, a locked vault, an empty
balance — rings immediately, with the fix-it button. A success clears the
streak.

Where `intensity` and `max_deep_runs_per_day` govern *how many* runs happen,
two settings bound what a *single* run may spend: the **Step budget** (how many
internal steps of thinking and tool use it may take, default 100 ≈ 25 rounds)
and the **Time limit** (wall-clock seconds, default 1200). A run that exceeds
either stops and its work for that run is discarded. Both follow the same
two-tier rule as everything else and are edited side by side in **Cortex →
Autonomy** — the "All agents" column for the whole deployment, the per-agent
column for one, `Inherit` to follow the deployment. Raise them together: a
larger step budget behind the old time limit just fails on time instead. The
Step budget **is** the recursion limit — there is no second control by that name,
and none for ordinary chat, which is not bounded this way. Each field's help text
names the config key it writes, so a value you have read about in a config file
can be traced back to the row that sets it.

**Phases & schedule**, the second part of the settings card, is one row per phase:
what it is, when it next runs, how the last run went, the same two columns of
on/off, and **Log** / **Run now**. On a phone each phase becomes a small block
instead of a line — the phase's full name, then its schedule, then the two
scopes each behind an "All agents" / agent-name label, then the two buttons —
so it is always clear which phase you are about to run and which scope a switch
belongs to.

Background work has no one watching it, so a run that fails, times out or needs
setup raises a **notification** — the sidebar bell and the Overview
Notifications section — naming the agent, the run and the fix, and clearing
itself when that run next succeeds. You do not have to open Cortex to find out
something went wrong. Runs stopped by a guard rail you configured (over budget,
nested too deep) stay quiet: they are working as intended.

**Talking about any of it happens in Chat.** Every button in Cortex that asks
the agent to do something — "Chat" on a Pulse card, an idea, brainstorm,
prototype or project; "Explore more" on an idea, brainstorm or prototype; "Send
to Agent" on a prototype; "Fix with agent" on a self-check finding — takes you
to the Chat tab on that item's own conversation, switching to the agent the item
belongs to. The first time, the message box arrives **pre-filled, not sent**: a
short framing that says what you are there to do ("The user wants to explore
more about this idea …") and gives the agent the full workspace path to the
item's files, ending in `User:` so you type what you actually want and press
Send yourself. Nothing goes to the agent until you do. Each item keeps one
conversation, so coming back opens its history with an empty message box and
continues where you left off rather than starting over; it is a normal chat,
with attachments, the model override, tool approvals and the full history. The
conversation carries a violet **CORTEX** badge in your conversation list so you
can find it later. The same hand-off works from the simple shell's Cortex page.

**Pulse** is the output you actually see: short, proactive suggestions surfaced
at the start of a session, optionally pushed to channels or emailed as a daily
digest. `quiet_hours_start` / `quiet_hours_end` keep it from interrupting, and
the per-session and per-day caps keep it from becoming noise.

### The mobile app

The Olano app puts your agents on your phone: conversations with them, what
Cortex is working on, the approvals and alerts waiting for you, and
notifications when an agent needs you. It runs on iPhone, iPad and Android
phones and tablets, and on Apple silicon Macs as the iPad app, and comes from
the App Store and Google Play.

The app is built for the phone: a tab bar along the bottom, a back button and
the swipe back gesture on every screen, pull down to refresh, and the look of
the platform it runs on (iOS on an iPhone, Material on Android), in light or
dark (see [Appearance](#appearance)). It talks to your own deployment, with the
same agents, conversations and permissions as the dashboard. Nothing about your
agents is stored by Olano on the way.

#### Connecting

The first screen asks for **your Olano address**: the address you open your
dashboard at in a browser, such as `acme.olano.run`. A bare name like `acme`
means `acme.olano.run`; a full address (`https://agents.example.com`) works for
a deployment on its own domain. The app checks that the address answers as an
Olano deployment before it continues, and says so if it does not (no such
address, a website that is not Olano, no network).

You then sign in with the email and password you use for the dashboard. The
sign-in is kept on the phone for two weeks of inactivity, so you sign in again
only when it expires or you sign out. A deployment with no accounts yet asks
you to finish setting it up in its dashboard first. A single-user deployment
(no accounts at all) opens straight away.

The app remembers the last few addresses you opened, so someone with more than
one deployment switches between them with one tap. **Settings, Switch
deployment** goes back to that screen.

##### Trying the app without a deployment

Under the address field, **Try the demo** opens a demo deployment Olano runs,
with sample agents, already signed in: no address, email or password to type.
Each visitor gets a demo account of their own, and no other visitor sees your
conversations, but the agents themselves are shared with other visitors, so
type nothing private there. Your demo account and its conversations are
deleted after a day; after that, **Enter the demo again** on the sign-in
screen opens a fresh one. **Settings** says when you are in the demo, and
**Switch deployment** takes you back to connect your own. Olano may remove the
demo entry at any time; it never changes anything about your own deployment.

If the deployment cannot be reached when the app opens, it shows **Can't
connect** with **Try again** and **Use a different deployment**. The usual
causes are no connection, or the deployment restarting or stopped.

#### The four tabs

Your administrator may have hidden some of what is described here; see
[What your administrator chooses](#what-your-administrator-chooses).

**Chats** lists the agents you have access to, each with its latest
conversation. A green dot means the agent is running. The list is arranged the
way the dashboard's sidebar is: **Pinned** at the top, then your own folders,
then **All agents**, then [group chats](#group-chats). Tap a section's name to
fold it shut. To arrange it:

- **Pin** an agent by swiping its row to the right, or press and hold it and
  choose **Pin**. Pinned agents stay at the top in the order you pinned them.
- **Folders**: the **+** button at the top makes a new folder. Move an agent
  into one by swiping its row to the left (**Move**), or press and hold it and
  choose **Move to**. An agent is in one place at a time: pinned, in one
  folder, or under All agents. The **...** button beside a folder's name
  renames or deletes it; deleting a folder moves its agents back under All
  agents.
- **Move Up** and **Move Down** (press and hold an agent) change the order
  within a section.

The arrangement is yours: it is kept on the deployment for your account, so it
follows you to another phone or a reinstall, and it is separate from how the
dashboard's sidebar is arranged in your browser. Searching shows matches in
every section, folded or not.

Tap an agent to go straight back to the last conversation you had with it,
like the dashboard's chat does: one you had in the app, the dashboard or a
messenger. Scheduled runs, conversations with other agents, Cortex threads and
calls from other systems never open this way. If you have no conversation with
the agent yet, a new one opens. The agent's row shows that conversation's
title.

Every other thread is one step away: the **...** button at the top of a
conversation, **All Conversations**. That list has every thread the agent
has, newest first, twenty at a time: scrolling down loads the next twenty, so
an agent with months of history opens as fast as a new one. The search bar at
the top looks through the messages of the agent's conversations on the
deployment, not just their titles, and lists the ones that mention the words
with a line showing where; **Search older conversations** keeps looking
further back when the history is long. The pencil button starts a new
conversation. Each thread carries
the same coloured badge the dashboard shows for where it came from: **Mobile**
for a conversation had in this app, **Dashboard** for one in the dashboard,
the messenger it arrived on (**Telegram**, **WhatsApp**, **Email**...), or how
it started (**Scheduled**, **Agent** for a conversation between two agents,
**Cortex**, **External** for a call from another system). Chips above the list
show one source at a time (**All**, **Dashboard**, **Mobile**, each messenger,
**Scheduled**, **Agents**), with how many threads each holds; the app remembers
your choice for each agent. Swipe a thread to rename it, or to delete it if you
are an administrator of that agent.

In a conversation, the agent's reply streams in as it is written. What the
agent did on the way (searched the web, read a file, asked another agent)
shows as one line per step while it works and folds into "N steps" when it is
done; tap that line to see the steps. A plan the agent made shows as a
checklist. While the agent works and the message box is empty, the send button
becomes a stop button; see [While the agent works](#while-the-agent-works) for
adding to what it is doing. If the agent needs your approval before an action,
a card with **Approve** and **Decline** appears in the conversation.

The message box is one card at the bottom of the screen: what you type on top,
and under it **+**, the conversation's model, the microphone and the send
button. On a phone the keyboard's return key starts a new line, and only the
send button sends, so a message never goes out half written. With a hardware
keyboard (an iPad's, or a keyboard paired with the phone) Enter sends and
Shift+Enter starts a new line. **+** sends photos and files, and images the
agent produces show in the conversation (see [Photos](#photos) and
[Files](#files-and-sharing-into-olano)). The microphone beside the send button
dictates (tap it: your words appear in the box as you speak, see
[Dictation](#dictation)) or records a voice note (hold it, see
[Voice notes](#voice-notes)). On a phone that cannot dictate, the microphone
shows while the message box is empty and only records voice notes.

Other files the agent hands you (a report it saved, a data file, a PDF) appear
as cards under its reply; tap one to open it in the app. Markdown documents
open formatted, data and code files (JSON, CSV, logs, scripts) open as text,
and a PDF opens as a document on an iPhone. Files the app cannot show itself,
such as Word or Excel documents, and PDFs on Android, open with **Open in
another app**, which offers the phone's own choices (save to Files, mail it,
open it in Word). The share button in the corner does the same for any file.

When an agent asks you for an API key or a model provider's key, the form is
drawn under its reply: paste the value and press **Save to vault**. It goes to
the vault without becoming a message, and the agent is told the name once it
is saved. When an agent needs you to sign in to a service, connect a
messenger or edit a file, it sends a **magic link**: after you approve it, a
card appears under its reply with **Open** and, usually, a six-digit PIN.
**Open** copies the PIN and opens a secure page of your deployment in a
browser over the app; paste the PIN there and follow the page. That page needs
no sign-in of its own (the link and its PIN are the key), and the link expires
after a while (fifteen minutes unless your deployment says otherwise). When
you finish, the conversation shows **Connected** (or **Didn't complete**), and
offers to restart the agent when the change needs one and you are an
administrator. See [Magic links](#magic-links-the-default-whenever-a-browser-is-needed).

A new conversation offers a few ways to start, such as "What can you help me
with?"; tap one to send it. Messages are grouped under the day they were sent
(Today, Yesterday, then the date). A long conversation opens on its newest
messages and fetches earlier ones as you scroll up, a little ahead of where
you are, so they are usually in place by the time you get there; **Start of
conversation** marks the first message. If they could not be fetched (no
signal), a line at the top says so and a tap tries again. If you have scrolled
up to read earlier messages, a round button with a down arrow takes you back
to the latest one, with a dot on it when something new arrived.

Next to **+** in the message box is the model the conversation runs on, with
its reasoning level in grey when the model has one (for example "Olano Pro
High"); the name turns orange when this conversation uses a model of its own
choosing. Tap it to pick another model, or how hard the model thinks (Low,
Medium, High and so on, as that model offers them; **Default** leaves it to the
model). The choice applies to that conversation only, the same as the model
picker in the dashboard's chat: every message you send in it uses that model,
reopening the conversation continues on it, and a new conversation starts on
the agent's own model again. The list offers the models your deployment can
run: its default models, the Olano models, and those of any provider an
administrator has added a key for. **Agent default** goes back to the agent's
own model. If a conversation last used a model the deployment no longer
offers, it continues on the agent's model and the app says so.

Web addresses in a conversation are links, in the agent's replies and in your
own messages, including an address in code formatting and one written as
`www.example.com`. Tap one to open it in a browser over the app; press and hold
one to **Copy Link** or **Share Link**. Anything in a reply that looks like web
page markup is shown as text.

Under each finished reply are buttons to **copy** it, **hear** it (see
[Hearing replies](#hearing-replies)) and **share** it to another app. Under your latest message are **copy** and **edit**: edit puts
your words back in the message box to correct, and sending them sends a new
message (the conversation's history is not rewritten, so the agent sees both).
When a reply fails, **Try again** under it sends your message again. Pressing
and holding any message offers the same choices, plus copying a voice note's
transcript.

To flag a reply that is offensive, harmful or wrong, press and hold it and
choose **Report**, then a reason. The report is recorded on the deployment as
negative feedback on that reply, with the reason kept beside it, and counted
for the administrators in the dashboard's Insights; nothing leaves the
deployment.

A conversation keeps going on your deployment if you leave it, lock the phone
or switch apps: come back and the app picks up where the reply has got to.

**Cortex** is what the agents have been working on by themselves. At the top,
the **Pulse** card says how many findings the agents posted for you, the same
news, research and ideas as the dashboard's Cortex Pulse; tap it for the cards. On each, **Do it** carries out the suggested next step, **Later**
brings the card back in a day, **Dismiss** drops it. A card about work the
agent already did on its own offers **Keep** and **Undo** instead: Keep
accepts it, Undo restores the backup the agent took first. Later and Dismiss
take the card away the moment you tap; Do it, Keep and Undo spin on the
button while the agent answers, then the card goes and a short notice, above
the tab bar, says what happened (when the step is a longer piece of work, the agent runs it in
the background and posts the result here when it is done).

Under it, the four stages in the order things move through them:
**Brainstorms**, **Ideas**, **Prototypes** and **Projects**, each with how
many items it holds. The list shows every stage; tap a stage to show only
that one, and tap it again (or **Show all**) to see everything. Chips narrow
the list to one agent when more than one has items. An item this phone has
not opened yet is lightly tinted; opening it clears the tint.

Triage happens on the list. Swipe a row to the right to **approve** it (move
it to the next stage) and to the left to **discard** it: the word shows as the
row slides, a full swipe does it, and a shorter swipe leaves the button there
to tap. An approved item moves to the next stage straight away, marked as on
its way while the agent writes it up; a discarded one leaves the list. To
discard several at once, tap **Select**, tap the rows (or **Select all**),
then **Discard** in the bar at the bottom and confirm.

Open an item for its write-up and Cortex's rating, then **Make it a** (the
next stage) to move it forward, **Talk it over** to discuss it with the agent,
or **Discard**. Moving an item forward is a piece of writing the agent does,
and it takes a minute or two: the button turns into **Making it an idea…**
as soon as you tap, then a banner under the title follows the run (with the
step the agent is on, when it reports one) and you can leave the page, since
the list already shows the item under its new stage. When it is done the
banner reads **Now an idea** with a button that opens the new item's page;
if the run failed, the banner says why and the button offers the tap again.
The first time, **Talk it over** opens a conversation with the
item already described in the message box (what it is, its title and where
its files are), so you only add your thoughts or questions after it; nothing
is sent until you send it. Later it reopens that conversation where you left
it. See [Cortex and Pulse](#cortex-and-pulse).

**Inbox** has three parts:

- **Approvals**: actions agents are waiting to take until someone approves
  them, from a scheduled run, Cortex or a request from another agent, and Pulse
  cards that need an approval. Answer each with **Approve** or **Decline**.
- **Alerts**: the deployment's notifications, the same list as the bell in the
  dashboard. The line under an alert says what tapping it does; swipe one to
  dismiss it. See [Alerts](#alerts).
- **Issues**: the issue tracker, every issue you can see, not only those an
  alert pointed at. **Open** (the default), **Assigned to me** or **All**,
  closed ones included; tap one to read it, comment, hand it to an agent or
  close it (see [Alerts](#alerts) for what an issue screen offers).

The agents' Pulse findings are in the Cortex tab.

The tab's badge counts approvals waiting plus unread alerts.

**Settings** shows who you are signed in as and where, the agents'
[scheduled tasks](#scheduled-tasks), the app's [appearance](#appearance),
[voice](#hearing-replies), the phone's notifications, **Switch deployment** and
**Sign out**. What the app does not do (agent configuration, tools, files,
connections and the rest) stays in the dashboard, in a browser.

The app grants and removes no permission: a member sees the agents they have
access to, an administrator sees all of them, exactly as in a browser.

#### While the agent works

You do not have to wait for a reply to finish before saying more. While the
agent is working, type in the message box and a bar above it says the agent is
working:

- **Send** (the arrow) hands your message to the agent while it works. It reads
  it at its next step, after the action it is in the middle of, and carries on
  with it in mind; nothing is stopped or started over. Your message appears in
  the conversation where the agent read it, marked **Added while it worked**.
- **Send after** keeps the message until the reply is done, then sends it as
  your next message.
- **Stop** stops the agent.

Messages waiting to be read show above the message box. **Edit** takes one
back into the message box to change it, the cross cancels it; either works
until the agent has read it. A message sent with the arrow just as the reply
finishes goes out as your next message instead, so nothing is lost.

#### The conversation menu

The **...** button at the top of a conversation opens a sheet that names the
agent and its model at the top, then groups what you can do. Rows with an arrow
open another screen; the others act right away or ask one more question.

**Conversation**

- **New Conversation** starts a new one with the same agent.
- **Mode** is how the agent goes about your requests in this conversation; the
  current one shows on the right. **Auto** (the default) does the work, running
  tools without asking. **Plan first** has the agent write a plan and wait:
  under the plan are **Approve & run**, which tells it to carry the plan out,
  and the message box, to ask for changes instead. **Ask before each step**
  asks for your approval before every tool the agent runs. A bar above the
  message box shows the setting when it is not Auto. The app remembers the
  choice for each agent on this phone; approving a plan goes back to Auto.
- **Rename** gives the conversation a name of your own.
- **Compact** replaces the conversation's older messages with a summary the
  agent keeps, so a long conversation takes less of the agent's memory. It is
  the same as `/compact` in the dashboard's chat.

**Agent**

- **All Conversations** lists this agent's conversations.
- **Files** opens the [agent's files](#an-agents-files).
- **Scheduled Tasks** lists this agent's [scheduled tasks](#scheduled-tasks).
- **Agent Info** opens the [agent's page](#an-agents-page). Tapping the agent's
  picture at the top of the conversation opens it too.

**More**

- **Hands-Free Mode** lets you talk with the agent without touching the phone
  (see [Hands-free](#hands-free)).
- **Add an Agent** turns the conversation into a [group chat](#group-chats)
  with a second agent you pick. The conversation so far is copied into the
  group, so the new agent can read it, and the agent you were talking to keeps
  answering messages that name nobody. It appears when the deployment has group
  chats turned on.

##### When an agent is stopped

An agent an administrator stopped (or that could not start, or that the
deployment's plan holds back) is marked **Stopped**, **Could not start** or
**Held back** in Chats. Its conversations still open and show everything said
so far, but where the message box would be, the app says why it will not reply.
An administrator sees a **Start** button there; anyone else is told to ask an
administrator.

#### An agent's page

The agent's page says whether it is running and, when it is not answering,
why: **Could not start** with the error it stopped on (a missing key, a model
that cannot be reached), **Held back** with what holds it (a plan limit, for
example), or **Stopped** when someone stopped it. Administrators also get:

- **Start**: starts a stopped agent, or tries again after an error.
- **Restart**: stops the agent and starts it again, for example after changing
  one of its keys.
- **Stop**: the agent stops answering messages, channels and scheduled tasks
  until someone starts it again, and it stays stopped if the deployment
  restarts.

Members see the status and the reason, without the buttons. The page also
leads to the agent's conversations, files and scheduled tasks.

#### Files and sharing into Olano

The **+** button next to the message box has **Choose Files** for documents:
PDFs, Word, Excel and PowerPoint files, text and data files, up to 10 MB each.
They wait above the message box with their size until you send; the cross
removes one. You can send files with or without a message.

The agent gets them the way the dashboard's chat sends them. A text or data
file (a `.txt`, `.csv`, `.md`, `.json` and so on) is read into your message, so
the agent sees its contents directly. Any other document is saved in the
agent's workspace, and the agent reads it with its document tools. In the
conversation your message shows the files by name, never their contents.

**Sharing into Olano.** From another app (a link from Safari, a photo, a PDF
from Mail or Files, some text), choose Olano in the share menu: the app opens,
asks which agent it is for, and puts it in a new conversation with that agent,
ready to send with a message of your own. Nothing is sent until you send it. A
link or text arrives in the message box, a photo or a file above it. If the app
does not open by itself after sharing, open it: what you shared is waiting.

#### An agent's files

**Settings, Files** lists your agents; pick one to see its files, the same
folders as the dashboard's **Files** page. The conversation menu's **Files**
opens the agent's files directly. Tap a folder to open it (the back button
goes up one folder) and a file to read it: photos open full screen, Markdown is
laid out, text and code are shown as they are, a PDF is drawn, and anything
else can be handed to another app with the share button.

The search box at the top finds files and folders by name across all of the
agent's files, wherever you are. Folders the dashboard hides from its file list
(version control, installed packages, caches) are left out here too.

Administrators can upload a file from the phone with the button at the top:
it goes into the folder that is open. A file with the same name there can be
replaced, or kept beside the new one under a new name. Members can open and
search files but not upload.

#### Scheduled tasks

A scheduled task is a message an agent gets on a timetable, such as "every
weekday at 8, send me what is on my calendar". Each run is a conversation of
its own, which you find in the agent's conversations with the **Scheduled**
badge. Agents can set these up themselves when you ask them to; the app lets
you see and manage them too.

**Settings, Scheduled Tasks** lists every agent's tasks, grouped by agent;
**Scheduled Tasks** in a conversation's menu lists that agent's. Each row says
when the task runs and when it runs next, or that it is paused. The switch on a
row pauses or resumes it; swipe a row for **Run now**.

Tap a task for its details: when it runs, the agent it runs as, the time zone
its times are in, its instructions and its recent runs (tap a run to read what
it produced, or why it failed). **Run Now** runs it straight away, whatever
its schedule. The pencil button changes it, and **Delete Task** at the bottom
removes it; its past runs stay in the agent's conversations.

The **+** button adds a task: pick the agent, write what it should do (those
words are the message the agent gets each time), optionally a name, and when
it repeats: every day, week (on the days you pick) or month at a time, every
few hours or minutes, or once on a date. The sentence under the choices reads
the schedule back. A new task uses your phone's time zone; changing a task
keeps the time zone it has. A task with a custom schedule made in the
dashboard stays as it is unless you change it. Running more often than every
five minutes shows a warning: each run is a full turn of the agent, which takes
time and uses credits, and when a run is still going as the next one is due,
that next run is skipped.

You can manage the tasks of the agents you have access to, as in the
dashboard's Scheduler.

#### Alerts

The Alerts part of the Inbox lists the deployment's notifications. Most of them
come with a fix, and the line under each one says what tapping it does:

- **Open issue**: an issue assigned to you opens in the app. Read it and its
  comments, add a comment, or use the **...** button to **Hand to an Agent**
  (it picks the issue up the next time it checks its issues, and comments as it
  works), **Talk It Over with an Agent** (a new conversation about it), or
  **Close Issue** and **Reopen Issue**.
- **Ask the agent to fix it**: for problems an agent can fix with your help,
  such as a service that needs reconnecting or an MCP server that needs signing
  in again. Tapping opens a new conversation with that agent with a message
  about the alert ready to send; the agent usually answers with a
  [magic link](#magic-links-the-default-whenever-a-browser-is-needed).
- **Repair the folders**: when skills have folder names that do not match their
  names, an administrator can repair them from the phone. A folder is renamed
  to match a valid name; a name that is not valid (a title with spaces or
  capitals) is set to its folder's name. The app reports what it repaired and
  anything it left alone, and the alert clears once nothing is left.
- **Open Cortex**, for a background run that failed.
- **Fixed on the dashboard**: some problems, such as a locked vault, low disk
  space or a usage budget, are fixed in the dashboard. The alert says where.

Tapping an alert marks it read. Its menu always has **Dismiss Alert**.

#### The agent's browser

When your deployment runs the browser in **live** mode (see
[Browser](#browser-control)), an agent that opens its browser shows it in the
conversation as a small floating window: the page it is on, live, and a line
saying who has it (**The agent is browsing**, **You have control**, **The
agent needs you** when it asked for help, or **Waiting for your OK in the
chat** when a click needs your approval). Drag the window to any corner. Its
**x** hides it until the agent opens a new browser or asks for you again; the
globe button at the top of the conversation opens the browser at any time,
with a dot on it when the agent asked for you.

Tap the window for the full view. While you watch, a dot shows where the agent
is about to click or type, and two fingers (or a double tap) zoom in on the
picture. **Take control** pauses the agent's browsing and lets you use the
page yourself, for a sign-in, a security check or anything only a person
should do:

- **tap** to click (two or three quick taps for a double or triple click);
- **swipe** to scroll;
- **rest your finger, then move it** to press and drag;
- the **keyboard** button opens a box to type into the page, with Backspace,
  Tab and Enter keys;
- **back**, **forward** and **reload**, and tap the address to go somewhere
  else. When the page has several tabs, the tabs button switches between
  them.

The picture is sent at the size the phone shows it and only as fast as the
phone draws it, so taps get through on any page, however much it redraws
(a sign-in's verification step, a feed): on a slow connection it turns a
little rougher for a moment and sharpens again as soon as the phone keeps
up. If a page is too busy to take a tap for a few seconds, the view says so;
tap again, or press reload.

Two buttons at the top of the full view keep what the screen shows, whether
the agent is working or you are: **Record** makes a video of the browser
(**REC** and a clock show while it runs, and **Stop** saves it in the agent's
`recordings/` folder, which the toast opens in Files), and the camera button
saves a picture of the page under `screenshots/`. A recording ends on its own
after 30 minutes or when the browser closes. The agent can record itself too:
ask it to record a demo of what it does.

The page's own questions (a confirmation, a file to upload, a drop-down list,
a date, time or colour to pick) come to you while you have control, as the
phone's own pickers; and when a page asks the browser for something it does
not have (your location, a camera, a microphone, leave to notify) you are
told what it asked for and that it was declined, so a site that then behaves
oddly is not a mystery. **Hand back** returns the browser to the agent; if it
had asked for you, it is told you are done. **Done** closes the
full view and leaves the browser as it is. With the browser in headless mode
there is nothing to watch: the agent's screenshots arrive in its replies when
it names them.

#### Group chats

A group chat is one conversation with two to six agents, the same group chats
as in the dashboard. They are listed under **Group chats** at the bottom of the
Chats tab, with the last message in each. Start one with **+**, **New Group
Chat** (or **Start a group chat with several agents** when you have none): pick
the agents and, optionally, a name. The first agent you pick answers messages
that do not name anyone.

To ask a particular agent, write `@` and its name; typing `@` suggests the
members. Name several (`@nova and @aria, what do you think?`) and each answers
in turn, in the order you named them, seeing the replies before its own. The
line above the message box says who will answer. Each reply shows the agent's
picture and name above it, and your own messages say who they went to.

Agents in a group can also hand the conversation to each other after replying.
While they do, a line above the message box says so, with **Stop** to end it
once the current reply is finished.

A one-to-one conversation becomes a group chat with **Add an Agent** in its
**...** menu: the conversation so far comes along, so nobody has to repeat it.

The **i** button at the top of a group chat opens its details: rename it, add
an agent, remove one (swipe it to the left; a group keeps at least two), or
delete the group chat. Group chats appear when the deployment has them turned
on; an administrator turns them on or off, and decides whether agents may
answer each other, at **Config, Group chats** in the dashboard (see
[`groups:`](#groups-multi-bot-conversations)).

#### Photos

The **+** button next to the message box adds photos: **Take Photo** opens the
camera, **Choose Photos** opens your photo library, where you can pick up to
ten at once. The photos wait above the message box until you send them: tap one
to see it larger, or its **x** to leave it out. Add a few words about what you
want done with them, or send them on their own.

Each photo is sent at a size suited to reading it (at most 2048 pixels on its
longest side) and without its location or other details the camera recorded.
For an administrator, each photo is saved in the agent's `uploads/` folder
(where it also shows in the dashboard's Files view) with a note telling the
agent where, and the agent decides from your message what to do with it: look
at it with its vision tool, pass it on, or keep it. A member's photos are shown
to the agent with that message only and not saved, so they do not come back
when the conversation is reopened.

In a conversation, one photo shows at its own shape and several as a grid.
Tap a photo, yours or one the agent produced, to see it full screen: swipe
sideways for the others in that message, pinch or double-tap to zoom, and swipe
down (or tap the close button) to go back.

The first time you take or choose a photo, your phone may ask whether Olano
may use the camera or your photos. If you declined, allow it again in the
phone's Settings, under Olano.

#### Dictation

Tap the microphone and talk: your words appear in the message box as you say
them, and what you send is the text, never a recording. Your phone works the
words out itself, so nothing you say leaves the phone until you send the
message, and the agent gets text it can start on at once.

While you dictate, the message box shows what has been heard so far, with four
controls under it:

- **x** throws away what you dictated this time. Anything you had typed
  before stays in the box.
- The waveform moves while the phone hears you (bars) and goes flat (dots)
  when you pause.
- **Stop** (the square) stops listening and leaves the words in the box, so
  you can correct them, type more, or tap the microphone again to keep
  talking. Dictated words are added after whatever is already in the box.
- **Send** (the arrow) stops listening and sends the message, the words still
  being worked out included. While the agent is working, it adds them to what
  the agent is doing, like a typed message.

Dictation keeps listening through pauses until you stop it, for up to five
minutes. Switching away from the app, or a phone call, stops it and leaves
what was heard in the box.

Dictation is offered where the phone can do it without sending your voice
anywhere: an iPhone that recognizes your language on the phone itself, and an
Android phone (Android 12 or later) with on-device speech recognition. It
listens for the language your phone is set to; Android may need that
language's speech recognition downloaded first (in the phone's settings for
speech recognition, or by using voice typing once). On other phones a tap on
the microphone records a voice note, as below. The first time you dictate,
the phone asks whether Olano may use the microphone and, on an iPhone, speech
recognition. To have a tap record a voice note instead, turn off **Dictate
with the microphone** in the app's Settings.

#### Voice notes

There are two ways to record a voice note:

- **Hold** the microphone and talk, then let go to send. Slide your finger left
  to throw the recording away, or up to lock it, so it keeps recording without
  your finger on it.
- **Tap** the microphone to record hands-free, on a phone that cannot dictate
  or with dictation turned off in Settings. A bin button throws the recording
  away and the send button sends it.

Where a tap dictates, a press becomes a voice note once it lasts longer than a
tap, and the microphone is there whether or not you have typed anything.

The dot beside the timer is grey while the phone opens the microphone and
turns red, with a small tick under your finger, the moment it is listening:
what you say from then on is in the note. After a note the microphone stays
ready for about twenty seconds, so the next one starts at once (the phone's
microphone indicator stays on for that long); it closes when you leave the
app. A recording shorter than about 0.7 seconds is not sent, and a recording
sends itself when it reaches five minutes. Leaving the conversation, or
switching away from the app, throws away a recording you have not sent.

By default the recording is sent to the deployment, which turns it into text
(below). With **Transcribe on this phone** turned on in the app's Settings, an
iPhone turns what you said into text itself, and the text is what is sent: the
agent starts on your words at once, the recording stays on the phone, and
nothing depends on the deployment's speech-to-text. Your administrator chooses
the default, and may hide the switch. While the
iPhone works it out, **Transcribing…** shows under the voice note, then your
words (tap them to read all of them). The iPhone does this on the phone when it
supports your language, and with Apple's speech recognition otherwise. It
listens for the iPhone's own language (**Settings, General, Language &
Region**): if you speak another language to your agents, turn off **Transcribe
on this phone** in the app's Settings, and the deployment transcribes
your voice notes instead. The first time, the iPhone asks whether Olano may use
speech recognition.

Otherwise (on Android, with **Transcribe on this phone** turned off, when you
declined speech recognition, or when the iPhone heard no words), the recording
is sent to the deployment, which turns it into text with its speech-to-text
service. The text appears under your voice note once the deployment has heard
it. If speech-to-text is switched off or fails on your deployment, the voice
note says it was not transcribed, and the agent is told it could not hear it.
An administrator turns speech-to-text on in the dashboard, under **Config,
Speech**.

A recording sent to the deployment by an administrator is saved in the agent's
`uploads/` folder, like a photo, and plays again when the conversation is
reopened. A member's recording is used for its text only and not saved. When the
recording was not sent (the iPhone transcribed it, or a member sent it), a
reopened conversation shows what you said, marked as a voice note, without the
recording.

Tap a voice note's play button to hear it, or tap or drag along its waveform to
jump to a point. While it plays, the speed button switches between 1x, 1.5x and
2x. Starting another voice note pauses the first. Audio files an agent sends you
play the same way.

The first time you record, your phone asks whether Olano may use the
microphone. If you declined, allow it again in the phone's Settings, under
Olano.

#### What your administrator chooses

An administrator can shape the app for everyone on the deployment, in the
dashboard under **Config, Mobile app**: hide the **Cortex** tab, the whole
**Inbox** or any of its sections (Approvals, Alerts, Issues), and the
**Files**, **Credits and Usage** and **Transcribe on this phone** rows in
Settings, and choose where voice notes are transcribed for people who have not
picked. The app checks for changes every time you open it or come back to it,
so a change shows up without updating the app. If you are on a screen that has
just been hidden, the app takes you to Chats (or back to Settings). Hiding only
removes it from the app; nothing is deleted.

#### Hearing replies

The speaker button under a finished reply reads it aloud; tap it again to stop.
Replies to your voice notes are read aloud as they arrive, so a conversation
can go back and forth by voice. **Read replies aloud** in the app's Settings
chooses when that happens:

- **After my voice notes** (the default): a reply to a voice note is read aloud.
- **Always**: every reply is read aloud as it arrives.
- **Never**: replies are read only when you tap the speaker.

The voice depends on the deployment. When an administrator has turned on text
to speech (**Config, Speech** in the dashboard), replies are read in the voice
chosen there, and each reading is billed like the deployment's other speech
use. Otherwise the phone reads them in its own voice, at no cost; an iPhone
picks the best voice it has for the language the reply is written in (better
voices can be downloaded in the iPhone's **Settings, Accessibility, Spoken
Content, Voices**).

A reply is read the way a person would read it to you: code blocks are left
out, a link is read as its text and a web address as the name of its site.
Playing a voice note or starting a recording stops the reading, and so does
leaving the app. These choices are kept on that phone only.

#### Hands-free

**Hands-Free Mode** in a conversation's **...** menu is for when your hands and
eyes are busy, such as driving or cooking. The phone listens; when you stop
talking it sends what you said, reads the reply aloud when it arrives, and
listens again. On a phone that can [dictate](#dictation), your words appear
under the button as you say them and go as a text message, worked out on the
phone itself, so nothing waits on the deployment's speech-to-text; a pause of
about two seconds after your last word sends them. On other phones, or with
**Dictate with the microphone** turned off in the app's Settings, what you say
goes as a voice note. If dictation cannot start (the phone's speech
recognition is turned off for Olano, say), the app says so and sends voice
notes for the rest of that hands-free conversation. The message box gives way
to one large button:

- While it listens, tapping it sends straight away instead of waiting for the
  pause.
- While a reply is being read, tapping it stops the reading and listens.
- After about fifteen seconds of silence it pauses; tap it to talk again.

**End Hands-Free** brings the message box back, and so does leaving the app.
Every reply is read aloud while hands-free is on, whatever **Read replies
aloud** is set to, in the voice described in [Hearing replies](#hearing-replies),
so a hands-free conversation costs nothing beyond the messages themselves.

#### Siri, the Action button and Shortcuts

On an iPhone, two things can be asked of the app from outside it. Both name
one of your agents, and Siri learns the agents' names from the app, so say
them as they appear in Chats.

**Ask an agent** sends what you say and reads the reply back, without the app
opening. It takes two breaths: name the agent, then answer Siri's question.

- "Hey Siri, ask Jane in Olano". Siri asks "What do you want to ask?", and
  you say the question.
- "Hey Siri, ask an agent in Olano" (or "Hey Siri, ask Olano"). Siri shows
  your agents and asks which, then asks for the question.

Keep the first sentence to the agent's name and the app's: words after it in
the same breath ("...what is on my calendar") make Siri treat it as a text
message to send, and it offers Messages instead. "Olano, ask Jane" and "ask
Jane on Olano" work too. The two actions appear in the **Shortcuts** app under
Olano once the app has been opened; if Siri still does not know an agent's
name, open the app once so it learns the current names, or say "ask an agent
in Olano" and choose.

Siri speaks the reply in its own voice. A reply that is mostly code or links
is read the way the app reads one: code is left out and a link is read as its
site. A few words from Siri follow soon after you speak; when the agent takes
longer, Siri says the agent is still working and the reply comes to the app
instead, and to the phone as a [notification](#notifications) when those are
on. A question asked within half an hour of the last one continues the same
conversation, so "and tomorrow?" means what you think; after that a fresh one
starts. These conversations appear in Chats like any other.

**Talk to an agent** opens the app straight into [Hands-free](#hands-free)
with that agent, in a new conversation: it listens, sends what you say, reads
the reply in the agent's voice when the deployment has one, and listens again.

- "Hey Siri, talk to Hannah in Olano"
- **Settings, Action Button, Shortcut** on an iPhone with an Action button:
  pick **Talk to an Agent** (or **Ask an Agent**) and the agent it should
  use, and a press opens the conversation listening.
- The same two actions are in **Control Center**, on the Lock Screen, and in
  the **Shortcuts** app, where **Ask an Agent** can be a step of your own
  shortcut or automation: its reply is passed on as text (a morning briefing
  that is read to you at seven, say).

The actions need iOS 16.4 or later, the app signed in to a deployment, and
work with whichever agents you can chat with. On Android, a long press on the
app's icon offers **Talk to ...** for the first few agents, which opens the
app the same way; those can be dragged to the home screen.

**Settings, Siri and widgets, Default agent** is the agent these reach when
none is named: "Hey Siri, talk to Olano" suggests it first, the Action
button and a Control Center control with no agent chosen open it, a widget
shows it until you pick another, and on Android it is the first shortcut.
Left at **First in the list**, it is whichever agent Chats lists first. The
choice is kept on that phone.

#### Widgets (iPhone)

Touch and hold the Home Screen, tap **Edit**, then **Add Widget** and choose
Olano. The widgets read the deployment on their own, with the session the app
signed in with, so they keep up while the app is closed; they refresh every
few minutes, and at once when you do something in the app or on a widget.
They need iOS 18 and the app opened once after signing in. A widget added
while the app was closed fills in the next time the app is opened.

Every widget that shows one agent shows the **default agent** (**Settings,
Siri and widgets**) until you pick another for it: touch and hold the widget
and choose **Edit Widget**.

- **Ask** (small, medium, Lock Screen): the simplest one, a way in. The
  small one is a single "Ask Anna" pill that opens a new conversation with
  the keyboard up. The medium one adds the composer's buttons: **Message**,
  **Photo** (the camera opens), **Photos** (the picker opens) and **Talk**
  (the conversation opens hands-free), so a question, a picture or a
  conversation is one tap from the Home Screen.
- **Agent** (small, medium, and every Lock Screen shape): one agent's
  picture, its status, the last reply in your conversation with it, and how
  many approvals it is waiting on. Tap it to open that conversation, the
  same one a tap on the agent in Chats opens (a new one when you have none
  yet; scheduled runs and the agent's other threads stay in its
  conversation list). The medium one has **Talk**, which opens a new
  conversation hands-free, and for administrators **Start** or **Stop**.
- **Quick asks** (medium, large): an agent and up to three questions you set
  when adding the widget, such as "Give me a short briefing of what matters
  right now". A tap sends one; the reply appears in the widget when it
  arrives (the large size keeps it in view). A widget cannot take typing, so
  the questions are fixed; tap the agent to open the conversation for
  anything else.
- **Approvals** (small, medium, Lock Screen): what is waiting on you. The
  medium one shows the oldest approval with **Approve** and **Decline** and a
  countdown to when it expires; a high-risk action has no buttons, so you see
  in the app exactly what would run. Tap to open the Inbox.
- **Team** (medium, large): every agent with its status and the last word
  in your conversation with it, each opening that conversation.
- **Pulse** (small, medium, Lock Screen): the newest Pulse card from Cortex.
- **Next run** (small, medium, Lock Screen): the next scheduled task,
  counting down, with the ones after it.

In **Control Center** (and the Lock Screen's two bottom buttons, and the
Action button on an iPhone that has one), two controls can be added:
**Talk to an Agent**, which opens the app listening to the chosen agent, and
**Message an Agent**, which opens a new conversation with it. A control with
no agent chosen reaches the default agent.

What a widget shows is the last thing the deployment answered, so a reply
written seconds ago may take a moment to appear; the app itself always shows
the latest. Signing out in the app clears the widgets.

#### Appearance

**Settings, Appearance** chooses how the app looks:

- **System** (the default): light or dark following the phone's own setting,
  changing with it.
- **Light**: always light, whatever the phone is set to.
- **Dark**: always dark, whatever the phone is set to.
- **Volt**: always a black page with an electric chartreuse accent, whatever
  the phone is set to. It is the dashboard's Volt theme, so a browser set to it
  and the phone look alike; the status bar and keyboard follow it as they do
  Dark.

The choice is kept on that phone only, for every deployment it opens.

#### Locking the app

The app keeps you signed in for two weeks, and your agents can act on your
accounts. **Settings, Privacy, Lock with Face ID** (Touch ID, or a fingerprint
or face unlock on Android, depending on the phone) makes the app ask for it
before anything shows: when the app opens, and when you come back to it. The
phone's own passcode always works too. Turning it on asks once, to make sure
the phone can unlock it.

**Ask again** chooses how long the app can be in the background before it asks
again: **Immediately** (the default), or after 1, 5 or 15 minutes. While the
app is in the background its screen is covered, so the phone's app switcher
shows nothing either. The setting needs a passcode on the phone, and is kept
on that phone only.

#### Credits and usage

On a deployment that runs on Olano credits, **Settings, Credits and Usage**
shows the credits left, how much of this month's included credits are used and
when they renew, any top-ups left, and what each Olano service (chat, images,
speech and so on) used over the last 30 days. It is for reading only: buying
credits is done from the dashboard. Deployments that use their own provider
keys have no credits, and the row does not appear.

#### Notifications

Notifications reach you while the app is closed or in the background. While it
is open on screen you see things happen live instead, so it does not also
notify you.

Turn them on in **Settings, Push notifications**; your phone asks for
permission once. There are four kinds, and each phone chooses its own:

| Kind | When it notifies | Who receives it |
|---|---|---|
| **Replies** | An agent finished answering a message you sent, and you were not looking at that conversation (the app was in the background and no browser had it open). Not sent for a reply you stopped. | You, the person who sent the message. |
| **Approvals** | An agent is waiting for you to approve or reject an action: in a conversation you started, or in the Approvals inbox for an agent you can act on (a scheduled run, Cortex, a request from another agent). | The person who started the conversation; for the inbox, every administrator and every member with access to that agent. |
| **Cortex** | Cortex wants approval for a change to an agent, or found something urgent. Ordinary findings wait in the Cortex tab. | Administrators and members with access to that agent. |
| **Alerts** | A warning or error appeared on the bell, such as a channel that cannot start. Each problem notifies at most once every six hours. | Administrators only. |

Tapping a notification opens what it is about: the conversation for a reply or
an approval in a conversation, Pulse in the Cortex tab for a Cortex finding, and
the Inbox for an inbox approval, a Cortex card that needs an approval, or an
alert.

**Answering from the notification.** An approval notification has **Approve**
and **Decline** buttons, and a reply notification has a **Reply** field: press
and hold the notification (or pull it down) on an iPhone, or expand it on
Android. The phone asks to be unlocked first, then the app answers without
opening: the approval is decided, or your reply goes into that conversation as
your next message, and the agent's answer arrives as another notification. An
action marked high risk has no buttons, so that it is always looked at in the
app first, and a reply that ran into a problem has no Reply field. If answering
does not work (the deployment cannot be reached, or you need to sign in again),
a notification says so; tap it to open the app. A reply sent while the agent is
still working on your last message is not sent; the notification says so.

An approval nobody answers is rejected when the agent's approval timeout runs
out, five minutes unless the agent sets `security.hitl_timeout_seconds`
(see [Human in the loop and approvals](#human-in-the-loop-hitl-and-approvals)). Keep
**Approvals** on if you rely on answering them from your phone.

##### What a notification contains

By default a notification names the agent and the reason ("maria needs your
approval", "Replied to your message.") and never contains what anyone wrote, or
the details of the action awaiting approval beyond its name. An administrator
can set `push.previews: true` in [`config.yaml`](#configyaml-reference) to include
the start of replies and Cortex finding titles; that text then passes through
Apple's or Google's notification service on its way to the phone.

Signing out in the app, or switching deployment, stops that deployment's
notifications on that phone. Deleting a person's account stops them on every
phone they had.

##### Who can have notifications

Notifications are delivered through the Olano Cloud, so they work on
deployments that run on Olano Cloud and are recent enough to send them. Any
other deployment can still be used in the app for chat, Cortex and the inbox;
**Settings** says why its notifications are unavailable. An administrator
controls them for the whole deployment with the `push:` block in
[`config.yaml`](#configyaml-reference): switching them off, the previews above, the
lowest Cortex priority that notifies, and the lowest alert level that notifies.

### Budgets

Per-agent spending caps in the `budget` block: `daily_usd`, `weekly_usd`,
`monthly_usd`. `alert_at` (default 0.8) warns at 80% of the cap.

What happens at the cap is your choice: set `fallback_model` to switch to
something cheaper and keep working, or `hard_stop: true` to stop the agent.
With neither, you get the alert and the agent continues.

Budgets emit a `budget.exceeded` event, so you can route it to an outbound
webhook.

### Ask the agent: self-configuration, magic links and admin tools

Every agent can reconfigure **itself** when asked to in a conversation — on
any surface, the simple chat view included — and can hand a person a **magic
link** whenever a browser is needed. AgentFather can do the same for the whole
deployment. This is the default way to connect, configure and operate Olano:
an agent never asks you to open a terminal, and there is no command line to
learn.

Ask a bot to:

- *"Connect yourself to Notion"* — every connection starts with the finder:
  the bot lists the supported ways to reach the service (a channel, an MCP
  server from the directory, a built-in toolkit — see
  [MCP servers](#mcp-servers)), adds the one you pick to itself, and hands you the
  sign-in: a **Connect button card** in the dashboard chat, a tappable link on
  messaging channels. You sign in from your own browser — Olano's app, your
  own OAuth app, or a code you type on your phone; the connection completes on
  its own and the bot's new tools appear after that. API keys are collected
  through a secure page and stored in the vault — never pasted into the chat
  or a config file. Something the bot cannot find in any catalog is external:
  it will read the project's page and tell you what it is before anything is
  installed, and it will never sign it in for you.
- *"Get yourself on Telegram"* — it sends you one link whose page walks you
  through the bot token (validated live) or, for **WhatsApp** and **WeChat
  (Weixin)**, shows a **QR code to scan** — in the dashboard chat and on
  messaging channels alike. The channel and the token land in its config and
  vault, and it restarts to go live.
- *"Switch to a cheaper model"* / *"Rewrite your instructions to always answer
  in Spanish"* / *"Add a research assistant"* — it lists real options, saves
  the change, and restarts itself when you say so. Prompt edits land in
  **Config → Revisions** under the agent's name, ready to roll back.
- *"What could you become?"* — it browses the archetype library of ready-made
  experts and teams and suggests specialists worth adding.
- *"Which skills do you have?"* / *"Stop using the spreadsheet skill"* — it
  lists its skills with their on/off state and switches individual ones off (or
  back on) for itself. Only a switch: it never installs, edits or deletes a
  skill, and turning off a common skill leaves every other agent's copy alone.
  See [Switching a skill on or off](#switching-a-skill-on-or-off).

#### Magic links — the default whenever a browser is needed

A magic link is a short-lived, PIN-protected page the agent sends into the
chat; whoever opens it finishes one task on their own phone or laptop, and the
chat is told the outcome ([`magic_links:`](#magic_links-secure-links-for-connecting-people)
has the knobs). Every agent has these tools by default:

| Ask | Tool | What the person sees |
|---|---|---|
| Sign in to a service, an MCP server or an official connector (GitHub, Google, OpenAI Codex, Microsoft 365, Slack, Notion, …) | `magic_link_connect_service` | the provider's own consent screen, or a one-time code to type on the provider's device page — whichever method the service offers and the bot picked (Olano's app, your own OAuth app, a code, or a secure form for a key); an MCP server that needs an OAuth app of your own (HubSpot) first asks for its Client ID and secret on the same page, or offers Composio instead ([MCP servers](#mcp-servers)); the connection serves the service's tools, MCP servers and the git tools at once, no restart |
| Hand over an API key, token or password | `magic_link_request_secret` | a secure form; the value goes straight into the vault, the bot only learns its name. In the dashboard chat and the mobile app the form appears right under the reply (no link to open, no PIN to type); on a messenger it is a link |
| Connect a messenger | `magic_link_connect_channels` | one page per platform: QR for WhatsApp/WeChat, a validated bot token for Telegram/Discord/Slack |
| Edit the bot's instructions | `magic_link_edit_instructions` | a one-page editor; saving applies live, no restart |
| Edit a file | `magic_link_request_file_edit` | a one-page editor showing the file's current contents |
| Open the dashboard on a phone | `magic_link_open_dashboard` | a one-time sign-in focused on a section (prompt, vault, connections, providers, config, files, overview) |
| Check a link you sent | `magic_link_status` | — |

Only the bot's owners can ask for a link (on a messenger, the channel's
allowed-users list), group chats are refused, and on the stricter security
profiles every link raises an approval card first. **Never paste a key into a
chat** — if a bot asks you to, it has been misconfigured; the link is the
route.

**In the dashboard chat and the mobile app, a key request is a card, not a
link.** You are already signed in there, so when a bot asks for an API key or
a provider key the secure form is drawn under its reply: paste the value, pick
the provider if it asks, and press **Save to vault**. The value goes to the
vault the same way the link's page would send it, never as a message in the
conversation, and the bot is told the name once it is saved. Sign-ins to a
service (the provider's consent screen), messenger pairing and the editors
still open as a page, because those need a browser of their own. A request
that was not filled in stays a card until the link expires; ask the bot again
for a fresh one.

#### The `self_*` tools — what a bot may change about itself

| Capability | Tools | Restart? |
|---|---|---|
| Know thyself | `self_get_config` (always on while any capability is) | — |
| Browse archetype library | `self_list_archetypes`, `self_read_archetype` | — |
| Change its own model | `self_list_models`, `self_set_model` | yes |
| Edit its own instructions | `self_update_prompt` | yes |
| Restart itself | `self_restart` | — |
| Add subagents | `self_list_subagents`, `self_add_subagent` | yes |
| Connect services | `self_find_integration` (every supported way to reach a service, ranked, with the exact next steps), `self_add_toolkit`, `self_remove_toolkit`, `self_inspect_external` (read an outside project's documentation before anything is installed); `self_search_mcp_directory`, `self_list_mcp_servers` (each server's live state, stored sign-in and what would bring it back), `self_list_mcp_tools` (every tool a server offers, switched-off ones marked), `self_reload_mcp_servers` (reconnect and re-bind tools without a restart), `self_add_mcp_server`, `self_remove_mcp_server`, `self_authorize_mcp_server` | yes for a toolkit or server; a sign-in or a reload applies on its own |
| Be reachable from outside | `self_set_external_access` (publish itself over MCP / A2A with a public description — administrators only), `magic_link_mint_access_token` (send an admin a page that mints a caller token, shown once there) | yes; the token link applies on its own |
| Connect messaging platforms | `self_list_platforms`, `self_connect_platform`, `self_connect_whatsapp`, `self_connect_weixin` | yes |
| Manage its own skills | `self_list_skills`, `self_enable_skill`, `self_disable_skill` | yes |
| Send magic links | the `magic_link_*` family above | — |

What a bot may change about itself is controlled per capability — everything
on by default — in **Config → Agent self-configuration** (whole deployment)
and per agent under **Agents → agent → Chat self-configuration** (three-state:
Inherit / On / Off). See [`agent_autonomy:`](#configyaml-reference). A
single tool can also be switched off for one agent on its **Tools** tab, by
AgentFather (`olano_update_agent` with `disabled_tools`), or for every agent
with `default_internal_tools: false`. A bot never gains power over *other*
agents this way: each one can only touch itself. Agents in customer-service
mode inherit everything off.

**Only the bot's owners can ask for a change.** Whoever creates a bot owns it;
deployment admins count as owners of every bot, and more owners are granted in
**Admin → Access**. Someone who was merely given access to chat with the bot
can still ask what it *could* do — list models, browse services and templates
— but a request to actually change something comes back as a refusal telling
them to ask an owner. On a messaging channel the bot's **allowed users** list
plays the same role, so the person who set the bot up can still configure it
from their phone.

#### AgentFather — the `olano_*` tools for the whole deployment

One agent, **AgentFather**, holds the tools that reach beyond itself. It is
human-only (other agents cannot message it), every write raises an approval
card, and it narrates what you are approving. Ask it to:

| Job | Tools |
|---|---|
| See and change any agent | `olano_list_agents`, `olano_get_agent`, `olano_create_agent`, `olano_update_agent`, `olano_delete_agent`; `olano_start_agent` / `olano_stop_agent` / `olano_restart_agent` |
| Build from the catalog | `olano_list_builtin_agents`, `olano_list_archetypes`, `olano_read_archetype`, `olano_scaffold_archetype`; `olano_list_models` |
| Tools and connections for any agent | `olano_find_integration` (every supported way to reach a service for an agent), `olano_add_toolkit`, `olano_remove_toolkit`, `olano_inspect_external`; `olano_list_tools`, `olano_list_bundles`, `olano_list_integrations`, `olano_find_tools`, `olano_set_tool_enabled`; `olano_search_mcp_directory`, `olano_get_mcp_directory_entry`, `olano_test_mcp_server`, `olano_add_mcp_server`, `olano_remove_mcp_server`, `olano_authorize_mcp_server`, `olano_mcp_oauth_status`; `olano_list_platforms`; the `magic_link_*` tools aimed at another agent |
| External access for any agent | `olano_set_agent_external_access` (publish or unpublish an agent over MCP / A2A and set its public description — administrators only); tokens are minted on the dashboard or with `magic_link_mint_access_token` |
| Skills and subagents | `olano_list_skills`, `olano_list_builtin_skills`, `olano_create_skill`, `olano_install_skill`, `olano_repair_skill_names`; `olano_list_subagents`, `olano_add_subagent`, `olano_toggle_subagent` |
| Teammates and access | `olano_list_users`, `olano_get_user`, `olano_create_user` (an invite link, never a password), `olano_reset_user_password`, `olano_set_user_role`, `olano_update_user`, `olano_delete_user`, `olano_grant_agent_access`, `olano_revoke_agent_access`, `olano_access_overview` |
| Diagnose | `olano_diagnose_agent`, `olano_runtime_status`, `olano_agent_status`, `olano_agent_activity`, `olano_agent_errors`, `olano_recent_errors`, `olano_tail_log`, `olano_list_sessions`, `olano_read_session`, `olano_mcp_status`, `olano_doctor` |
| Deployment settings, disk, memory, updates | `olano_get_home_config`, `olano_update_home_config` (every `config.yaml` block); `olano_disk_report`, `olano_disk_monitor`, `olano_memory_report`, `olano_memory_monitor`; `olano_update` |
| Another agent's files | `olano_list_agent_workspace`, `olano_read_agent_file`, `olano_write_agent_file`, `olano_run_agent_command` |
| Try an agent out | `olano_test_agent` (send it a test message and see its reply and every tool it used; see below) |
| Review any agent's Cortex | `olano_cortex_list`, `olano_cortex_view`, `olano_cortex_discard`, `olano_cortex_restore`, `olano_cortex_promote` (see below) |

##### Testing an agent before you rely on it

Ask AgentFather to *"test the support agent with a refund question"* and it
sends that agent a message on a separate **test thread**, then reads back the
reply and every tool the agent called, with what each one returned. It uses
this after building or changing an agent: it tests, adjusts the agent, tests
again, and tells you what it tried and what happened rather than what it
expects to happen. A test can be one message or a whole conversation, since
AgentFather can keep sending follow-ups on the same test thread.

It can also test **instructions that are not saved yet**, to compare a prompt
rewrite before applying it, and test against the agent's **saved
configuration** so a tool or MCP server it just added can be tried before the
agent restarts. An agent that is stopped can be tested too. Nothing a test does
changes the agent's instructions or configuration.

- **Administrators only.** It runs from a conversation with AgentFather, never
  from a scheduled run, another agent or an outside caller, and you approve
  each test individually: the card names the agent and the message.
- **The agent's tools run for real**, and a test costs a normal turn of that
  agent's model. Anything the agent needs approval for raises its own card.
- **The agent under test cannot change itself** (its instructions, model or
  connections) during the test.
- **Test threads stay separate from real conversations.** They appear in that
  agent's conversation list with the *Autonomous* badge (the **Agents** filter
  shows them), so you can read the whole exchange, and they are never added to
  the agent's long-term memory.
- A test turn stops after 10 minutes unless AgentFather asks for a different
  limit (at most 30).

##### Reviewing Cortex across agents

Every agent with [Cortex](#cortex-and-pulse) on keeps a funnel of
brainstorms, ideas, prototypes and projects. AgentFather can review it with you
so you do not have to open each agent's Cortex view. Ask, for example:

- *"What has the research agent been brainstorming? Which of those are worth
  keeping?"* It lists the items with Cortex's scores, reads the ones that
  matter and recommends what to keep, promote or drop, with reasons.
- *"Which ideas across all agents score 4 or lower?"* One list for the whole
  deployment.
- *"Show me what the research agent discarded"*, or *"show everything,
  promoted and discarded included"*. Lists show only the items still in play
  unless you ask for discarded, promoted or all items, and AgentFather can
  tell you how many it left out.
- *"Discard all of the research agent's brainstorms"*, or only those scored 3
  or lower. It shows you what the discard would touch before doing it.
- *"Promote the pricing-page idea to a prototype"*, or *"turn that prototype
  into a project"*.

What it does is exactly what the buttons on a Cortex card do:

- **Discard** keeps the item's folder; *"restore those"* brings items back.
  Items that were already promoted, and items a promotion or exploration is
  working on right now, are left alone and named in the answer.
- **Promote** moves an item one stage down the funnel, or straight to a project
  when you say so. The work is done by **that agent**, with its own models,
  workspace and spending budget (a promotion is refused when that budget is
  spent), exactly as if you had clicked the card. A prototype or project can take a few minutes; the item's card shows the run
  while it is going, and asking twice joins the same run instead of starting a
  second one.
- Every discard, restore and promotion raises an approval card naming the agent
  and the items. Only administrators can use these, and the change is recorded
  in that agent's Cortex activity log.

The playbooks AgentFather follows ship as two skills only it can see:
**olano-agent-builder** (designing and building an agent from a conversation,
[Building agents from a conversation](#building-agents-from-a-conversation)) and **olano-admin**
(operating the deployment). Every other agent gets **olano-agent** (how to do
the things above for itself — one finder, one ladder), **olano-platform** (the
map of the platform) and **olano-cloud** (plans, credits and usage).

### Beyond the deployment: A2A and MCP

Everything in [Agent-to-agent and the org chart](#agent-to-agent-and-the-org-chart) is about agents on
**this** deployment talking to each other. Two further doors connect them to
the world outside, each built on an open protocol so the other side can be
anyone's software:

- **Outbound — A2A.** Your agent hands work to an agent hosted elsewhere
  (another company's, another Olano deployment's) using the Agent2Agent
  protocol.
- **Inbound — MCP.** An outside platform that speaks the Model Context
  Protocol (Claude, ChatGPT, Cursor, an agent framework, an automation tool)
  sends *your* agent a message and reads the reply.

Both are off until you switch them on, both are audited in the **Agent to
Agent** tab, and neither changes how your own agents talk to each other.

```mermaid
flowchart TB
    EX["Claude, ChatGPT, Cursor, an automation tool,<br/>or another agent over A2A"] -- "inbound, MCP or A2A,<br/>with a token you issued" --> B["Your agent with External access on"]
    B <-- "talk_to_agent, inside the deployment" --> A["Your agent with Remote Agents listed"]
    A -- "outbound A2A,<br/>only to names on its list" --> RA["An agent hosted elsewhere"]
```

#### Talking to an outside agent (A2A)

1. Ask the remote party for their agent's base URL and, if it requires one,
   a credential. Store the credential in the vault (Config → Credentials, or
   ask your agent to send you a magic link for it).
2. On your agent's **Advanced** tab, add an entry under **Remote Agents**
   ([`agent.json5` reference](#remote_agents-external-agents-over-a2a)): a short `name`,
   the `url`, the `auth` mode and the credential as a vault reference.
3. Restart the agent. It now has `a2a_list_agents` (what it may talk to),
   `a2a_discover_agent` (read the remote Agent Card: skills, endpoints,
   authentication), `a2a_send_message` (send a request and get the reply) and
   `a2a_get_task` (poll a long-running remote task).

What the agent can and cannot do: it can only address names on its list —
there is no URL parameter, and it cannot add entries itself. Each reply is
treated as input from another organisation: your agent reports it rather than
following instructions embedded in it, and it is told not to forward secrets
or private data you did not ask it to share. Multi-turn exchanges continue
automatically within one of your conversations (the remote `context_id` is
remembered), or explicitly by passing the `context_id` back.

What the platform enforces on every call: `https://` only; the configured URL
**and** the endpoint the remote Agent Card advertises are checked against the
same outbound-request policy as every other web tool (no private networks, no
cloud metadata addresses, your agent's own allow/deny lists); the card's
endpoint must live on the same origin as the URL you configured unless you
opt out per entry; credentials are read from the vault at call time and sent
only to that vetted endpoint; replies are capped in length; and every send and
reply appears in **Agent to Agent** under the **External** segment, as a
conversation between your agent and the remote agent's configured name with an
**A2A** badge. `a2a_send_message` counts as an external write for approval
purposes, so an agent in Supervised or Locked-down mode asks before sending.
Fleet-wide rules live in `config.yaml → remote_agents`
([`remote_agents:` — fleet rules for talking to outside agents (A2A)](#remote_agents-fleet-rules-for-talking-to-outside-agents-a2a)).

#### Letting an outside platform reach your agent (MCP and A2A)

1. **Switch the doors on home-wide.** **Config → External access**: turn on
   *MCP endpoint*, *A2A endpoints*, or both. The deployment must have an
   `https://` public URL; a plain-http public endpoint is refused.
2. **Publish the agent.** Open the agent → **External access** tab (clearly
   labelled as the opposite of its MCP tab), switch on *Reachable over MCP*
   and/or *Reachable over A2A*, and write the **public description** — what
   the agent can do for an outside caller, what it needs, what it will not do.
   It is required, it is what every remote caller sees, and it is not the
   system prompt. Save and restart the agent. The tab then shows the exact
   endpoints: the MCP endpoint and the agent's tool name `ask_<agent>`, and
   the A2A Agent Card and JSON-RPC address.
3. **Give the caller a credential.** Either
   - **mint a token** on the same tab (or on **Config → External access** for
     the fleet view): a label, the agents it may reach (or *every exposed
     agent*), a lifetime. The token is shown **once**; only a hash is kept.
     Revoke it from the same list at any time. An agent can also send an
     administrator a token-minting page with `magic_link_mint_access_token`
     — the token appears on that page, never in the chat; or
   - **let the client sign in with OAuth**. Clients that cannot paste a
     header (a Claude.ai custom connector, an A2A client following the card)
     discover the deployment's own authorization server, register themselves,
     and send you to the dashboard's **consent page**. There an administrator
     sees who is asking and which agents they requested, ticks the agents to
     grant (or *every exposed agent*), and allows or declines. The client then
     receives a short-lived access token and a rotating refresh token; both
     appear in the token list tagged *OAuth*, and **Config → External access →
     OAuth clients** lists the applications, each with a *Forget* button that
     revokes everything it holds.
4. **Point the client at it.** For MCP: a server of type *Streamable HTTP* at
   `<public URL>/api/mcp` with the header `Authorization: Bearer <token>` (or
   the OAuth sign-in). Its tool list shows `olano_list_agents` plus one
   `ask_<agent>` tool per exposed agent; `thread_id` on a call continues a
   conversation. For A2A: the agent's card URL
   `<public URL>/api/a2a/agents/<agent>/.well-known/agent-card.json`; the
   card declares both the bearer scheme and the OAuth flow. A client that
   calls an agent without a token is pointed at that agent's own sign-in
   details, so an OAuth-capable client can find the sign-in by itself.
   Applications that cannot keep a secret (command-line and desktop tools)
   can register and sign in as public clients.

**From chat, only an administrator can do any of this.** AgentFather (or any
agent holding the admin tools) has `olano_set_agent_external_access` to flip
an agent's flags and set its description, and every agent has
`self_set_external_access` for itself plus `magic_link_mint_access_token`;
all three refuse unless the person on the turn is an administrator (a peer
agent, a scheduled run, an inbound MCP or A2A caller, or a non-admin member
is refused with an explanation). The per-agent switch that decides whether an
agent even holds those tools is the `external_access` self-configuration
capability ([`agent_autonomy:`](#agent_autonomy-what-agents-may-change-about-themselves)).

You can also plug in **your own identity provider** instead of, or next to,
the built-in sign-in: fill *External identity provider* (issuer and JWKS URL)
under Config → External access, and the endpoints accept that issuer's
access tokens — bound to this deployment as audience, with `agent:<name>` /
`agents:*` scopes deciding reach.

What the platform enforces: no unauthenticated access (a missing or bad token
gets a standard 401 pointing at the metadata document); a token reaches only
the agents it names, and only while they are published; the `Host` header is
checked against the public URL to defeat DNS-rebinding; each inbound call runs
as a normal turn on a thread named after the caller (`mcp::<label>::<thread>`
or `a2a::<label>::<conversation>`) **as an outside, non-owner caller** — the
agent's approval (HITL) rules apply, so an action that needs approval waits in
the **Approvals** inbox rather than running because a stranger asked, and
while it waits the call is held; the caller never sees the agent's
configuration, files or other threads; OAuth codes are single-use, tokens are
short-lived and refresh tokens rotate. Every call appears in **Agent to
Agent** as a conversation between `mcp:<label>` or `a2a:<label>` and the
agent, and the agent's transcript keeps the turn under that thread.

---

## Models and credits

### Two ways to pay, not two modes

| | Olano-managed | Bring your own key (BYOK) |
|---|---|---|
| What you pick | `olano:turbo`, `olano:pro`, `olano:max` | `anthropic:claude-sonnet-4-6`, `openai:gpt-5.5`, … |
| Who holds the provider account | Olano | you |
| What you are billed in | Olano credits | your provider bills you directly |
| Setup needed | none on Olano Cloud; an Olano API key on a self-hosted install | a provider API key in the vault |

```mermaid
flowchart LR
    A["The model an agent<br/>or a conversation uses"] --> T{"olano:turbo, olano:pro<br/>or olano:max?"}
    T -- yes --> O["Olano runs the call,<br/>billed in Olano credits"]
    T -- "no, provider:model" --> K["Your provider key from the vault,<br/>billed by your provider"]
```

**A deployment can do both at once.** Billing is a property of the model you
choose, not a mode the whole box is in — one agent can run on credits while
another runs on your own OpenAI key. Pick per agent, and per conversation with
`/model`.

BYOK shows tokens and estimated dollars, because you own that provider bill and
want to reconcile it. Olano-managed shows credits.

### The Olano tiers

| Tier (what you see) | Value to write in config | Reasoning effort | Use it for |
|---|---|---|---|
| **Olano Turbo** *(default)* | `olano:turbo` | not applicable | Everyday work. Fast, cheap, no knobs. Start here. |
| **Olano Pro** | `olano:pro` | `low` / `medium` / `high` | Harder analysis, longer chains of work, better judgement. |
| **Olano Max** | `olano:max` | `low` / `medium` / `high` | The most powerful frontier models, for genuinely hard problems. |

The name you see and the value you write are the same word for every tier. They
were not always: the top tier's id used to be `olano:reasoning`, which is why
you may still find that string in an older `agent.json5`.

> **`olano:reasoning` no longer works.** It was retired, not aliased, so an
> agent still configured with it fails to build, with an error naming
> `olano:turbo`, `olano:pro`, `olano:max` — and the model picker shows the raw
> `olano:reasoning` instead of a product name, which is how you spot one.
> **The fix is to edit the value to `olano:max`.** Nothing rewrites it for you,
> and the change is nothing more than the new spelling: same tier, same
> routing, same credit rate.

The word "reasoning" does still appear here, in the **Reasoning effort** column
above and in a model's `reasoning_effort` setting. That is how hard a model
thinks about a single request — an unrelated setting that happens to share the
word, and it is not going anywhere.

Every tier offers a very large context window; the dashboard's model picker
shows the exact figure for each, and the context bar in a conversation shows how
much of it you are using.

**What backs a tier is not fixed.** Olano routes each tier to whatever currently
serves that capability and speed point best, and re-routes as the model
landscape changes. That is the value of a tier: you buy a capability level, and
you get upgrades without changing a config. It also means you should choose a
tier by the *job*, not by assumptions about a particular underlying model.

`olano:turbo` is the default and always available. When in doubt, start an agent
on Turbo and move it up only when its work demonstrably needs more.

### Deployment default models (`default:*`)

Three **aliases** sit above every concrete model:

| Alias | Picker name | Out of the box it points at |
|---|---|---|
| `default:turbo` | **Default · Turbo** | `olano:turbo` on an Olano-managed deployment; a bring-your-own-key model elsewhere |
| `default:pro` | **Default · Pro** | `olano:pro` on managed; a bring-your-own-key model elsewhere |
| `default:max` | **Default · Max** | `olano:max` on managed; a bring-your-own-key model elsewhere |

An alias is not a model — it is a pointer the whole deployment shares. Any
agent, subagent, team member or chat conversation set to one follows whatever
target is configured in **Config → Default models** (the `default_models:`
block of `config.yaml`), and the target must be a concrete `provider:model` —
never another alias. Change a target once and every agent on that alias
switches the next time it boots or reloads, with no per-agent edits. The
pickers show what an alias currently resolves to ("Default · Turbo — currently
Olano Turbo"), and clearing a target in Config restores the deployment's own
built-in default.

Use an alias when an agent should simply run on "the deployment's
everyday/heavier/top brain"; pin a concrete model only when that one agent
genuinely needs that one model. Catalog agents install on the aliases —
experts on `default:turbo`, team supervisors on `default:pro` — which is what
makes "move the whole fleet to a different model" a one-field change.

One caveat on a bring-your-own-key install: an alias is only as usable as its
current target. If the target's provider has no key or connection, every agent
on that alias fails at its next turn — the chat picker greys the alias out for
the same reason.

### What brain a new agent starts on

You almost never have to pick one. Every agent you add — from the New agent
dialog, from an integration, or asked for in plain language — starts on **`default:turbo`**, the deployment's
everyday brain. On an Olano-managed deployment that resolves to `olano:turbo`,
so it works on credits from its first turn with nothing to configure; on a
bring-your-own-key install it resolves to the bring-your-own-key target shown
in **Config → Default models**.

**AgentFather starts on `default:turbo` as well.** Its turns are conversation
and tool calls — a catalog lookup, a config change behind an approval card, a
log read — and the everyday brain answers those in a beat where the deeper tier
made every one of them wait. It used to start on `default:pro`; an AgentFather
still on that tier from an earlier version is moved to `default:turbo` on the
next runtime start, unless you picked a different model for it yourself. If you
want it on the deeper tier for a hard design session, change its model in the
**Overview** tab or use `/model` for that one conversation — a choice you make
is never rewritten.

Two things override this, in order:

1. **A model you name.** Ask for an agent on a specific model, or set one in
   the New agent dialog, and that is what you get — even if its provider is not
   connected yet (you get a warning, not a substitution).
2. **A `preferred_model` in your home config**, which applies to agents
   installed without naming a model.

Changing an agent's model later is one field — the **Overview** tab of the
agent's page in the dashboard, `model` in `agent.json5`, or `/model` for a
single conversation. Nothing rewrites a model you chose yourself.

### Utility models

Two Olano groups exist that are **not** tiers and never appear in a model
picker:

- **`olano:vision`** — backs `image_describe`, so an agent can see images.
- **the Olano image group** — backs `image_generate` and `image_edit`.

The product calls these on the agent's behalf. They guarantee a *capability*
(accepts images, produces images) rather than a speed-and-capability point,
which is why they stay independent of the tiers. **They cannot be an agent's
brain** — setting one as `model`, `cognition.fast_model` or
`cognition.deep_model` makes the config fail to load.

On a managed deployment these run on credits automatically, so vision, image
generation, image editing, speech-to-text and text-to-speech all work with zero
configuration. To use your own provider instead, pin it in `config.yaml` under
`media:` (vision, image) and `speech:` (voice) — see
[`media:` — vision and image generation](#media-vision-and-image-generation).

### Credits

Credits are the unit for everything Olano-managed: model calls on the tiers,
the utility models behind vision, image and speech, and managed services such as
web search and [LinkedIn outreach](#channels-and-connectors).

- The dashboard shows your balance and what has been consumed; the **Insights**
  tab breaks usage down by agent, by model tier ("Credits by tier"), and by
  kind of work ("Where credits went": chat, Cortex, scheduled tasks,
  highlights, subagents…). Every model and service call records its charge on
  the deployment as it happens, so these figures are measured locally and
  include background work. Your account **balance** (Overview's "Credits
  left", and the cloud deployment page) is the billing-period view — a
  different window than the charts, so the totals need not match exactly.
- `credits.display` in `config.yaml` switches cost displays between `usd` and
  `credits`. It is a display setting only.
- Buy more from Olano Cloud (see [Olano Cloud](#olano-cloud)).
- Per-agent `budget` caps (see [Budgets](#budgets)) work alongside credits to
  stop one agent consuming everything.

### Olano credits on a self-hosted install

A self-hosted install can run on Olano credits too. It takes an **Olano API
key** instead of a deployment:

1. On `cloud.olano.ai`, open **API keys** and create a key. It is shown once;
   copy it whole (it starts with `sk-olano-`). You can create several, one per
   install, and revoke any of them there at any time.
2. Buy **account credits** on the Billing page. They belong to your account,
   not to any deployment, and they never expire.
3. Paste the key on the dashboard's **Providers → Olano** card, or in the
   **AI provider** panel of the setup wizard. The dashboard checks the key with
   your account before storing it in the vault.

From then on the install behaves like a managed one for billing: the Olano
tiers, image generation and editing, vision, transcription, web search and page
fetching run on your account's credits with no provider account; agents on the
`default:*` tiers switch to the Olano tiers at their next start; the Overview
and Insights pages show the credit book, and `credits.display` defaults to
`credits` (you can still choose dollars). Everything else about the box is
unchanged: it is still yours, with no plan, no cloud card and no hidden
sections.

The same key also lets the install **sign in to MCP servers through Olano's
sign-in service**, so a box without a domain or certificate of its own can
still finish an MCP server's sign-in ([MCP servers](#mcp-servers)).
That covers MCP servers only: the one-click **Connect with Olano's app** on the
Connections tab stays an Olano Cloud feature, and a self-hosted install
connects those services with its own OAuth app.

Removing the key (same card) reverses all of it: default-tier agents go back
to your provider keys at their next start, and the managed image, speech and
web services switch off until a key is connected again. A box with no key and
no deployment has no credit balance; there BYOK is the only route, and cost
tracking is in dollars against your own provider bills.

### When a managed service says it is temporarily unavailable

Sometimes an agent answers with something like *"The turbo tier (Olano-managed)
is temporarily unavailable — the request was refused upstream of this
deployment."* That is an Olano-side problem, not yours:

- **Your credits are untouched.** Nothing was consumed, and the balance you see
  is still yours to spend.
- **Nothing on your deployment is misconfigured.** There is no key to set and no
  setting to change — a message that asks you to fix a credential is a different
  error with different wording.
- **Your own provider keys keep working.** If you have BYOK configured, pointing
  the agent at one of your own models (dashboard → the agent → **Model**, or
  `/model` in chat) keeps it running until the tier recovers.

**The message names what is affected.** It can be a tier you chose (*the turbo
tier*, *the pro tier*) or one of the services Olano runs on the agent's behalf —
*image understanding*, for instance, which is what reading an image uses. That
distinction matters: if it names a single service, everything else keeps working,
and only the requests that need that service fail.

**You will not see it for a passing blip.** Olano retries a request like this a
few times on its own, waiting a little longer between attempts, before it gives
up and tells you — so a message that does appear means the problem has already
outlasted several tries.

Wait a few minutes and send the message again. If it still fails, write to
**support@olano.ai** and say which service the message named. In dashboard chat
the error carries an **Email support** button that opens a pre-filled message
naming the service and your deployment.

Two nearby errors that read similarly but mean the opposite, because the fix is
yours in both cases:

| What it says | What it means |
|---|---|
| *out of olano credits … buy more on your deployment card* | Your balance is spent. Buy credits (see [Olano Cloud](#olano-cloud)); BYOK models keep working meanwhile. |
| *Your Olano account is out of credits* | Same, on a self-hosted install running on an Olano API key: buy account credits on the Billing page at `cloud.olano.ai`. |
| *Your Olano API key was not accepted* | The key pasted on Providers → Olano was revoked on `cloud.olano.ai` or pasted incompletely. Create a new one under API keys and paste it whole; nothing retries on its own. |
| *No Olano API key is connected on this box* | A self-hosted install with no Olano API key (none pasted, or it was removed) still has an agent on an Olano tier. Connect a key on Providers → Olano, or switch that agent to a provider model. |
| *your `<provider>` account is out of balance* | A **BYOK** provider account of yours ran dry. Top it up with that provider. |

---

## Olano Cloud

`cloud.olano.ai` is the managed service: instead of installing and maintaining
Olano yourself, you deploy an instance there and Olano runs it for you.

**What you can do there:**

- **Deploy** a dedicated Olano instance in a few minutes. You choose the size —
  instances come in a range of CPU, memory and disk configurations — and you get
  your own dashboard at your own address.
- **Scale** an instance up as your workload grows, and buy additional disk space
  when you need it.
- **Stay current.** New Olano releases are made available to your instance, and
  you are notified when an update is ready, so you decide when to take it rather
  than discovering a version drift months later.
- **Take snapshots** of an instance, so you have a restore point before a risky
  change.
- **Encrypted at rest.** New instances keep all of their data on an encrypted
  volume that unlocks itself at boot (standard custody, [Privacy and security](#privacy-and-security)).
  The console's Security tab shows the state of the volume; the recovery key,
  the one part only you hold, is created from the instance's own dashboard
  under **Config → Disk encryption**.
- **Buy credit packs** on top of what your subscription includes, for months
  where your agents do more.
- **Run a self-hosted install on credits.** Create an **API key** and buy
  **account credits**: they belong to your account, never expire, and any
  install you paste the key into spends them (see
  [Models and credits](#olano-credits-on-a-self-hosted-install)).
- **Watch usage** — what your agents consumed, by agent and over time.
- **Manage billing and your subscription** — plans, payment method, invoices.
- **Organizations.** Deployments belong to an organization, so a team can share
  access to the same instances.

Everything in this guide applies identically to a cloud deployment: the same
dashboard, the same `agent.json5`, the same features. The differences are that
Olano handles the infrastructure, Olano-managed models are available out of the
box on credits, and your plan's entitlements may hide dashboard sections your
deployment does not include.

Current plans, sizes and pricing are on `cloud.olano.ai` — they change as the
offering evolves, so check there rather than relying on a figure written down
somewhere else.

---

## Building agents from a conversation

This section is the recipe an agent-building agent (AgentFather) follows, and
it works just as well as a checklist for a person.

```mermaid
flowchart LR
    subgraph plan["Before building"]
        direction TB
        S1["1. Understand the goal"] --> S2["2. Survey what exists"] --> S3["3. Recommend, get a yes"] --> S4["4. Pick the model"]
    end
    subgraph build["Building"]
        direction TB
        S5["5. Wire the capabilities"] --> S6["6. Credentials in the vault"] --> S7["7. Place it in the org"] --> S8["8. Set the safety posture"]
    end
    subgraph after["After"]
        direction TB
        S9["9. Restart, then verify"] --> S10["10. Capture what you learned"]
    end
    plan --> build --> after
```

**1. Understand the goal before naming a solution.** What work needs doing, who
it is for, what systems it must reach, and what "done" looks like. Restate it
back and confirm before building anything.

**2. Survey what already exists.** Olano ships a catalog of ready-made agents,
teams and integration agents. Always look before building custom:

- `olano_list_builtin_agents` — every ready-made agent. Each can be installed
  standalone *or* composed as a subagent of another agent.
- `olano_list_archetypes` — the picker list (label, description, category).
- `olano_read_archetype` — **read this before describing an archetype.** The
  list only returns picker fields; it does not tell you an archetype's prompt,
  tools, memory model or security posture. Never answer "what does this ship
  with?" from memory.
- `olano_list_tools` / `olano_list_integrations` / `olano_list_bundles` — tools
  for the `tools` list.
- `olano_find_tools(tag)` — search by capability (`customer_service`, `privacy`,
  `memory`, …) when you know what you need but not what it is called. Some tools
  are wired from a config flag rather than a `tools` entry; when a result says
  so, enable that config instead of adding the name.
- `olano_list_platforms` — connectors for `channels`, and the vault key each
  one expects.
- `olano_search_mcp_directory(query)` / `olano_get_mcp_directory_entry(slug)` —
  the catalog of MCP servers, with each one's verified endpoint, transport,
  auth mode and the credential names it expects.
- `olano_list_builtin_skills` — skills that ship with Olano.

Never invent a tool, agent, platform or skill that is not in these lists — and
never write an MCP server URL from memory. A wrong endpoint saves without
complaint and fails later, in front of whoever is using the agent.

**3. Recommend before you build.** When more than one approach fits, lay out two
or three concrete options with their trade-offs — effort, capability, cost,
safety, maintenance — and give a recommendation. Surface the simpler option the
person may not have considered. Get explicit confirmation on the approach before
writing anything.

**4. Pick the model deliberately.** Call `olano_list_models` before creating any
agent — it reports which providers are genuinely usable on this deployment and
ranks their models by cost. Default to the cheapest model that can do the job;
on a managed deployment that means `olano:turbo`. Reserve a top tier for work
that genuinely needs deep reasoning, and say why you chose it. Never write a
model string from memory — the available models differ per deployment.

**5. Wire the capabilities.**
- To reach an outside platform, prefer an MCP server. It usually carries its own
  sign-in, so the person clicks a consent screen they recognise instead of
  hunting for an API key in a settings page they have never opened. Search the
  directory first, add the server, then sign in — in that order ([MCP servers](#mcp-servers)).
- A matching toolkit (`sql`, `google`, `slack`, `stripe`) when it is already
  credentialled, or when no MCP server exists for the platform.
- Building a small MCP server when the platform matters and nothing covers it.
- Skills for repeatable procedures.
- Subagents for specialties that do not need their own inbox.

**6. Credentials go in the vault, never in the config.** Check what is already
stored (names only, `secret_list`) before asking for anything. Prefer a magic
link that lets the person enter the value themselves
(`magic_link_request_secret`) — it goes straight into the vault without passing
through the chat, the transcript, or the agent's context. Never ask anyone to
paste a key into a conversation. For a connector, remember the
per-agent override rule from [Channels and connectors](#channels-and-connectors): two agents
sharing one token become one bot.

**7. Place it in the organization.** Set `org.manager` / `org.reports` so
delegation works, and set the allow-lists or `human_only` if the agent should be
reachable by fewer parties than the default (which is: everyone).

**8. Set the safety posture.** Choose a `security.profile` that matches how much
the agent can affect the outside world, and decide what needs human approval
([Human in the loop (HITL) and approvals](#human-in-the-loop-hitl-and-approvals)). An agent that sends email or
moves money should not be on `developer`.

**9. Restart, then verify.** Config changes need a restart to take effect. Then
check it actually booted, its connectors came up, and its MCP servers connected
— do not report success from a config write alone.

**10. Capture what you learned.** If you taught the agent a repeatable flow,
make it a skill or a subagent so it survives the conversation.

---

## Common problems

**My agent replies with an empty message, or stops mid-sentence.**
Check **Multi-user → Message policy → Maximum reply length**. A reply that hits
that ceiling is cut where it hits it and is deliberately not continued. If it
comes back *empty*, the model is a reasoning one and spent the whole ceiling
thinking: raise the number above about 1024, or set **Reasoning effort** to
`none` or `low` on the Overview tab.

**My agent went silent for a real customer.**
Something in **Multi-user → Message policy** matched their message. In this
dashboard an ignored message always shows a short note saying so, so send the
same text here to see which case it was. A pattern is matched anywhere in the
message and ignores case, so a broad one like `test` catches "is this the
latest version?" — anchor it with `^…$` if you meant the whole message. If it
was a silence *rule* instead, remember the agent is judging it, so an
ambiguous rule produces ambiguous behaviour; make it a statement that is
plainly true or false about one message. Setting **What an ignored sender
sees** turns silence into one polite line, which is usually right when a real
customer can act on the rule and wrong when the sender is a spammer.

**Long messages to my own agent get refused.**
The message-length cap applies on the dashboard too, not only on messengers —
deliberately, so that a cap you set is one you can see working. Raise or clear
**Maximum message length**, or paste the long text into a file and point the
agent at it. Slash commands are never screened, so `/` commands keep working
whatever the cap is.

**The agent introduces itself by the template's name, not the name I gave it.**
An agent created before titles existed may carry the archetype's label as its
**Display name** ("Recruitment Specialist") with your name only as its handle
(`hillary`) — and its prompt tells it it is *called* Recruitment Specialist.
Open the agent, set **Display name** to the name you want and **Title** to the
role (both on the Overview tab; the title also lives on the Talk to Agent
tab); the next conversation introduces itself as "Hillary, Recruitment
Specialist". Agents created now get this right from the start: the name you
type is the display name and the template's label becomes the title.

**No "New agent" button in the simple view.**
Admins always have it. For a member it means the deployment turned member bot
creation off: an admin can switch it back on under **Config → Member bot
creation** (it is on by default). A member who has the button but gets a
"limit reached" message owns as many agents as the per-member limit allows —
the same Config section raises it.

**Can't find Sign out in the simple view.**
It is in the account menu — click your name at the bottom of the agent list;
Sign out is the last item, always visible at the menu's foot. It only appears
on team installs (with per-person sign-in); a single-user box has no session
to sign out of. Theme and language are one dropdown each, so the menu is short
enough that nothing in it scrolls; on older builds a tall menu could hide Sign
out below the language list, and with the list collapsed to a strip of faces the
menu itself was cut off at the strip's edge — update if you see either.

**An agent never replies on Telegram/Slack/Discord.**
Almost always a missing or shared credential. A connector with no credential is
skipped at boot and raises a dashboard notification naming the exact key to set.
If two agents use the same platform, each needs its **own** token stored as a
per-agent override — otherwise they authenticate as the same bot and one stops
receiving messages. Set the token, then restart the agent.

**The agent answered "Sorry, I had trouble processing that one — could you send
your message again?"**
The connection to the model broke part-way through the reply. It is a network
blip, not a problem with your deployment, your keys or your credit balance, and
nothing needs changing: send the same message again and it almost always goes
straight through. There is no need to start a new conversation — the thread is
intact and the agent still has the context.

Olano-managed models retry a dropped connection themselves before the agent says
anything, so seeing this message means either the retries were used up or the
answer had already begun arriving (a half-delivered reply is never re-sent — you
would get the second half of one answer glued to the first half of another).
Models you connected with your own API key have no such retry, so they show it
more often. If it happens on *every* message rather than occasionally, the model
provider itself is likely having an outage — check its status on the
**Providers** page.

**A command-line tool in the sandbox says it is not logged in (`gh`, `gog`, …).**
Expected, and not something to fix: the sandbox holds no credential, and Olano
never signs a command-line tool in there ([Sandbox tool bundles](#sandbox-tool-bundles)).
Use the built-in integration instead — the GitHub tools for repositories,
issues and pull requests, the `git` toolkit to clone or push a private
repository — after connecting GitHub once in **Connections** (Connect, or Sign
in with a code) or by asking the agent for the link. If you really want the
tool itself signed in, do it yourself from the **Terminal** tab; that sign-in
is yours alone and lasts until the sandbox is rebuilt.

**The GitHub CLI card is gone from Connections.**
It merged into the **GitHub** card, which now offers both **Connect** (an
OAuth app — Olano's or your own) and **Sign in with a code** (no app needed),
at the System scope and per agent. A connection made either way serves the
GitHub tools, GitHub MCP servers and the `git` toolkit alike — it never signed
anything in inside the sandbox, and it still does not.

**An agent is refused with "the monthly budget for … is exhausted".**
It hit one of the six usage budgets — see
[`usage_budgets:`](#usage_budgets-token-ceilings-and-budget-warnings). Open
**Config → Usage budgets** to see which one and how full it is. A *token*
budget is yours to raise or lift there, and the change applies to the next
turn; a *cost* budget comes from your plan and resets with its window. Note
token budgets count calls made on your own provider keys too, so an agent on
its own API key can still be stopped by one. Notifications at 50%, 60%, 70%,
80%, 90%, 95% and 99% warn you long before this happens — if this was the first
you heard of it, check that budget notifications are on.

**The agent says it cannot message someone by name.**
Agents address a chat or a person by the platform's own id. A name or handle
works too, but only for chats and people whose messages have already reached
*that* agent — each agent keeps its own directory of who it has heard from, so
someone well known to one agent is unknown to another until they message it as
well. On Telegram and Discord, where a chat id is always a number, a name the
agent does not recognise now fails immediately and says what to do about it,
rather than being sent and bouncing back as an unreadable error. Either have the
person message the bot once, or give the agent the numeric id to use.

**A reply says the model "could not read part of this conversation's history".**
Change a bot's model and the conversation carries on where it left off — the
thread keeps everything the previous model wrote. A few models cannot read
certain things an earlier model left behind (its private reasoning, an image
attached in a shape it does not accept), and when that happens the whole thread
stops working for that model rather than just one turn: retrying it fails the
same way every time. Start a new chat and the bot works normally again; the old
conversation stays readable, and switching that bot back to its previous model
also restores it. If you would rather not lose the thread, switch the model
back, ask the bot to summarise where things stand, and paste that into the new
chat. This is rare, and mostly affects locally hosted models.

**An agent is offline and will not start.**
Check the **Logs** page (account menu → Administration → Logs) for that agent.
The usual causes: an invalid `agent.json5`
(see [`agent.json5` reference](#what-makes-a-config-fail-to-load) for what gets rejected), a
model string that names a provider with no usable key, or the agent having been
deliberately stopped — a stopped agent stays offline across restarts until you
start it again.

**An MCP server shows as disconnected.**
Look at the **MCP** tab for the reason. If it says a sign-in is required, you
have to authorize it — Olano will not retry a server that needs consent it does
not have, because reconnecting cannot invent consent. Timeouts and errors are
retried automatically with backoff; an explicit reload from the tab always wins
over the retry schedule.

**Approvals are piling up / the agent keeps asking permission.**
Its `security.profile` is stricter than the work needs, or `hitl_shell` /
`hitl_write_tools` are on. Move it to a more permissive profile, or override the
one noisy tool with `security.tools.<name>.approval: "never"`. Note that running
commands in another agent's workspace is always approved individually and cannot
be switched off.

**The agent gave away or acted on something it should not have.**
Check `security.pii_action` (default `warn` only warns — set `redact` or
`block`), `output_scan`, and `credential_scrub`. If the agent reads untrusted
content like email or web pages, make sure `input_scan` is on and consider
`web_scan`.

**A dashboard tab is missing.**
Four are admin-only: Vault, Logs, Config, Admin. On a managed deployment, other
tabs can be hidden by your plan's entitlements. If you are an admin and a
section you expect is gone, that is why.

**A Cortex Think phase says it "hit its step limit" (or, on older builds, shows
"Recursion limit of N reached").**
The background run needed more rounds of thinking and tool use than its step
budget allows, so it stopped before finishing and its work for that run was
discarded. Run the phase again — a retry often plans more tightly. If it keeps
happening, raise the **Step budget** in **Cortex → Autonomy**: the "All agents"
column raises it for the whole deployment, the per-agent column for just this
one (each unit of ~4 steps is roughly one round of thinking plus tool use).
Raise the **Time limit** beside it at the same time — a bigger step budget
behind the old time limit just fails on time instead. Note that a phase
reporting "No active items to advance" or "No fresh idea to build" is a normal
no-op, not a failure — the generative phases (Brainstorming, Ideas) have to
succeed first to give the later phases material to work on.

**A background run failed and I only found out by opening Cortex.**
You should not have to go looking. A background run that fails, times out or
needs setup now raises a notification — the bell in the sidebar and the
Notifications section on Overview — naming the agent, which run failed and what
to do about it, with a link straight to Cortex. Repeats of the same failure
collapse into one row with a count rather than filling the list, and the row
clears itself the next time that run succeeds. Runs stopped by a guard rail you
already configured (over the daily budget, or nested too deep) do not notify:
they are working as intended and clear on their own.

**An agent says it can't do something its MCP server should handle.**
Usually the connection to that server is down, and the most common reason is an
OAuth grant that can no longer be renewed — the provider expired or revoked it.
Olano retries everything it can on its own: it renews access tokens ahead of
expiry, re-discovers a provider's token endpoint if the one it had stopped
answering, and reconnects servers that failed with exponential backoff. What it
cannot do is give consent on your behalf.

When a grant reaches that point you get a notification — the bell in the
sidebar and the Notifications section on Overview — saying which server needs
re-authorizing for which agent, with a link straight to that agent's **MCP**
tab. Open it and click **Authorize**. Reloading the server will not fix this one
(the notification says so); only a fresh authorization will. The row clears
itself as soon as the server connects again, and there is one row per agent per
server, since each agent authorizes its servers independently. If you no longer
need that server, switching it off or removing it on the **MCP** tab clears the
row too, within a couple of minutes at most, and deleting the agent clears
every notification about it.

To check the state of every server without waiting for a notification, use
**Config → MCP connection health**, the agent's own MCP tab, or `/mcps` in chat.

**"Skill X does not follow Agent Skills specification: name must match
directory name".**
A skill's `name:` in its `SKILL.md` differs from the folder containing it. The
skill still loads and works, but the warning repeats on every agent start and a
future update may stop loading it.

The usual cause was an agent writing a skill with a title as its name
(`Professional HTML Email Formatting` in a folder called
`professional-html-email-formatting`). That no longer sticks: when an agent
writes a skill, a title in `name:` is set to the folder's name on the spot, and
every agent start and every background run does the same for skills written any
other way. A skill switched off under its old name stays switched off.

What is left gets a notification listing the affected skills: a valid name in
a folder called something else. **Repair** (on the notification in the mobile
app, or AgentFather's `olano_repair_skill_names`) renames each **folder** to
match the skill's name, since the name is what the agent refers to. Repair
leaves two cases alone and says so: a folder of that name already exists, or
the skill is an agent's improved copy of another skill.

**The agent says there are no Cortex items, but the folders are still there.**
The Cortex tab and the `cortex_*` tools read one index file per agent. If that
file was damaged by two edits landing at the same moment (an older release could
do that when the agent edited several items in one reply), it now repairs itself
on the next read: the items reappear, and a copy of the damaged file is kept
beside it with a `.corrupt-<time>` suffix that you can delete once things look
right. If a tool answers that the index is **unreadable**, the file could not be
repaired automatically; nothing has been changed or written over it. Open the
agent's **Files** view, find `.olano/state/cortex/items.json`, and either fix the
JSON or move the file aside; the item folders (`ideas/`, `prototypes/`,
`projects/`, …) are untouched and the agent can re-add the items from them.

**Out of credits, or usage higher than expected.**
Check **Insights** — the **"Where credits went"** card splits the window by
kind of work (chat, Cortex background thinking, scheduled tasks, highlights,
subagents…), **"Credits by tier"** shows which managed tier is spending, and
the by-agent and by-model charts narrow it further. The usual cause is an
agent on a higher tier than its work needs, or Cortex running at `aggressive`
intensity across many agents. Move routine agents to `olano:turbo`, lower
`cognition.intensity`, reduce `max_deep_runs_per_day`, and set per-agent
`budget` caps. Buy more credits from Olano Cloud (on a self-hosted install
running on an Olano API key, under Billing → Account credits).

**An MCP sign-in on a self-hosted install says there is nowhere to return to.**
The provider needs a secure address to send you back to after you sign in. Paste
an Olano API key on **Providers → Olano** (a few seconds later the box is
registered with Olano's sign-in service and the sign-in works with no domain of
its own), or give the box a public https address. If a key is already there and
sign-ins still fail, the key may have been revoked on `cloud.olano.ai`, or the
account may already have five self-hosted installs using the sign-in service;
an install unused for 30 days frees its place on its own, and revoking an API
key frees the installs that registered with it
([MCP servers](#mcp-servers)).

**Olano models stopped working on a self-hosted install.**
Open **Providers → Olano**. If the card says the key was refused, it was
revoked on `cloud.olano.ai` or pasted incompletely: create a new key under API
keys and paste it whole. If it says the account is out of credits, buy more on
the Billing page. If the card shows no form at all, the box resolved as a
managed deployment, which bills its own credits (see
[Models and credits](#olano-credits-on-a-self-hosted-install)).

Also note the two surfaces measure different windows by design: the deployment
card at cloud.olano.ai shows the **current billing period** against your
plan's monthly allowance (including the daily snapshot-storage charge, listed
in the per-service view), while Insights charts the last-N-days window you
selected, measured on the deployment itself. They agree on what was metered;
they are not the same time span.

**Credits charts jumped after an upgrade.**
Expected once: the first start after upgrading reconstructs historical Cortex
background spend into the local record, so days that previously showed only
chat now show the full picture. Nothing was re-billed — your balance is
unchanged; only the charts got more complete.

**The agent remembers something wrong.**
Edit `MEMORY.md` in the **Memory** or **Files** tab. It takes effect on the
agent's next reply — no restart needed.

**Disk is filling up.**
Expand the **Overview** tab's disk card. **Where the space went** names every
group on the machine, so you can tell immediately whether the space is your
data or the platform itself; **Largest directories in the Olano home** then
drills into your data. Automatic cleanup runs on its own at the threshold in
`disk_monitor:`, and **Clean now** runs it immediately — it reclaims superseded
container images, caches and old logs. AgentFather reports the same with
`olano_disk_report`. If a specific agent's workspace is the problem,
its `trash/` and `scratch/` folders are safe to empty.

**The disk says 60% used but the directory list only adds up to a few hundred
megabytes.**
Those two numbers measure different things and both are right. The percentage is
the whole machine; the directory list is only the Olano home. The gap is the
container images, the swap file and the operating system — all of it named in
**Where the space went** at the top of the same card.

**A config change did not take effect.**
Most agent settings need that agent restarted; the dashboard shows when a
restart is pending. The ports and team-accounts settings need the whole runtime
restarted. Identity and instruction files (`AGENTS.md`, `SOUL.md`,
`IDENTITY.md`, `TOOLS.md`) are picked up without a restart, within a couple of
minutes.

**A number I set in Config came back different.**
`config.yaml` values are clamped to their valid range rather than rejected, and
an unparseable value falls back to the default silently. The value the dashboard
shows after saving is the one in effect.

**The Disk encryption card is missing from Config.**
The card exists only on a managed instance whose data lives on an encrypted
volume ([Privacy and security](#privacy-and-security)). A self-hosted install never shows it,
and neither does a managed instance created before encrypted volumes existed;
the cloud console's Security tab shows the custody of each instance and marks
such an instance as legacy. Nothing on the instance can turn the card on:
encryption is decided when the instance is created.

**The Disk encryption card says the encryption service is Unavailable.**
The service on the instance that performs the recovery-key actions is not
answering. Your data stays encrypted and the instance keeps working; only the
buttons on the card are paused. Press **Refresh** after a minute. If it stays
unavailable, restart the instance from the cloud console (the volume unlocks
again by itself at boot), and if that does not clear it, contact support.

**A recovery key is stuck at "Pending confirmation", or I lost my recovery key.**
A key that was shown and never confirmed stays pending for fifteen minutes and
then expires by itself; **Cancel pending key** removes it at once, and no other
key can be created while one is pending. A lost key cannot be shown again by
anyone, Olano included: while the instance runs, press **Replace recovery
key**, store the new key, and confirm it; the lost one stops opening the volume
the moment the new one is confirmed. Without any recovery key the volume can
still be opened, by Olano's key service alone.

**Nothing shows up in Langfuse.**
Open Config → Observability. The keys line must not say *No keys yet*, the
switch must be on (or the agent's own Observability card must say *On*), and the
agent card's *In effect now* line must say *traced*. Press **Test connection**:
a refusal from Langfuse Cloud almost always means the keys belong to another
region, so set the Langfuse address to that region (`https://cloud.langfuse.com`,
`https://us.cloud.langfuse.com` or `https://jp.cloud.langfuse.com`). The Status
line shows the last problem sending traces, if any.

**Langfuse traces show `withheld_chars` instead of the conversation.**
That is the default capture mode, *Metadata only*: traces carry the structure,
timings, tokens and names but no text. An admin can choose *Full content* in
Config → Observability, or for one agent on its Observability card.

**Some turns are missing from Langfuse.**
Check the *Share of turns traced* (Config, or the agent's card): below 1, only
that share of turns is sent. Cortex background runs are skipped when *Trace
background work* is off.

**There is no Langfuse link under a reply.**
The link needs an admin account with **Langfuse** links switched on in Admin →
Users (off by default), a traced reply, and a project the dashboard knows: press **Test connection** once in Config → Observability (or after
saving an agent's own keys) so the project is remembered.

**A reply has no thumbs up or down.**
Replies recorded before reply rating existed have none; newer replies in the
same conversation do. Thumbs also appear only once a reply has finished.

**A thumbs-down comment is not in Langfuse.**
Comments are sent only when traces include full content. With the default,
*Metadata only*, Langfuse gets the score (-1) and the comment stays on the
deployment. A vote reaches Langfuse only if that reply was traced and tracing is
still on when the vote is cast.

**A scheduled task did not run this morning, or ran late.**
Open **Scheduler**, the task, then **History**, and read the newest row. *Skipped:
the previous run was still executing* means the earlier run of the same task was
still going when the next one was due: open the task's recent conversations in
the chat list to see what it was doing, shorten the work or split the task, and
know that a run is stopped after `scheduler.max_run_minutes` (three hours by
default) so the job frees itself. *Skipped: firing was missed by more than the grace period* means the
runtime was busy or restarting for longer than `scheduler.misfire_grace_minutes`
allows (six hours by default); within that window a late firing runs late, and
its message says so. No row at all for the expected time means the task was not
registered in this process: saving the task again registers it, and the bell
carries a notice when the scheduler could not read its task list at startup.
Both kinds of problem raise a notification on the bell that links to the task,
and the notice clears on the task's next completed run. An agent that decided it
was the wrong day for its work was missing the date: every run's message now
opens with the current date and weekday in the task's timezone, so re-run the
task and read its first lines in the run's conversation.
