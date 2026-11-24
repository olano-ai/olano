# Agent settings reference

Everything `agent-creation` touches, with the exact field names, accepted values
and real defaults. Quote from here rather than from memory — a plausible field
name that does not exist produces a config that fails to load, and a default
that is wrong by one level changes what the agent is allowed to do.

For anything not covered here, read `system/knowledge/USER_GUIDE.md` via
`workspace_context` (§4 is the full `agent.json5` reference) rather than
guessing.

---

## Identity

| Field | Type | Default |
|---|---|---|
| `name` | string | **required** |
| `description` | string | `""` |
| `emoji` | string | `"🦁"` |
| `color` | string \| null | `null` (CSS hex, e.g. `"#c96442"`) |
| `avatar_url` | string \| null | `null` |
| `archetype` | string \| null | `null` — set automatically on an archetype install, for traceability |

`workspace` is derived automatically (`<home>/agents/<name>/workspace/`). **Do not
set it by hand.**

---

## Model

Write one of these forms. `default:*` is the deployment alias and the right
choice for almost every new agent — it follows the box's configured default
rather than pinning a vendor.

| Value | Use for |
|---|---|
| `default:turbo` | **The starting point for nearly every agent.** Fast, cheap, enough for chat, drafting, routing, tool calls. |
| `default:pro` | Genuinely reasoning-heavy work: analysis, planning, code, multi-step research. |
| `default:max` | Only when `pro` has been tried and is demonstrably not enough. |
| `olano:turbo` / `olano:pro` / `olano:max` | The managed tiers directly. `default:*` resolves to these on a managed box. |
| `provider:model` | BYOK, e.g. `anthropic:claude-sonnet-4-6`. Check `olano_list_models` for what this box can actually reach. |

**Traps that fail the load or silently do nothing:**

- `olano:reasoning` was **retired, not aliased** — it produces a config that
  fails to load. Do not write it.
- `olano:auto` still resolves but is hidden — never offer it.
- `olano:vision` and `olano:olano-image` are utility services, **not brains**.
  Assigning either as `model` raises with a message telling you to use
  `olano:turbo`, `olano:pro` or `olano:max`.
- `flash` is a legacy alias for `turbo`; prefer `turbo`.

### `reasoning_effort`

`reasoning_effort: "low" | "medium" | "high"`, default unset.

Meaningful **only on `pro` and `max`** — the turbo tier exposes no reasoning
levels and the setting is dropped. Setting it on a model that cannot take it is
harmless (the kwarg is gated out), so a wrong value never breaks a boot — but it
also buys nothing, so do not set it to look thorough.

### Prompt shaping

| Field | Values | Default |
|---|---|---|
| `system_prompt` | string | `""` |
| `prompt_mode` | `full` \| `task` \| `minimal` \| `none` | `full` |
| `vibes_preset` | `precise` \| `warm` \| `executive-brief` \| `hacker-lab` | `precise` |
| `default_verbosity` | `quiet` \| `normal` \| `verbose` | `normal` |

---

## Access — the `backend:` block

This is what the user experiences as "what may it touch". Capabilities not
granted are not merely discouraged: **the corresponding tools are never given to
the agent.** Changes take effect on the agent's next restart.

| What the user picks | `backend:` block |
|---|---|
| **Read, Write & Execute** (default) | `{ type: "local_shell", root_dir: "workspace", virtual_mode: false }` |
| **Read & Write** | `{ type: "filesystem", root_dir: "workspace", access_mode: "read_write" }` |
| **Read only** | `{ type: "filesystem", root_dir: "workspace", access_mode: "read" }` |
| **Virtual files (temporary)** | `{ type: "state" }` |
| **Virtual files (persistent)** | `{ type: "store" }` |

Sandboxed backends also exist (`lionclaw_docker`, and hosted `daytona` / `modal`
/ `runloop` / `agentcore` / `langsmith`). Reach for `lionclaw_docker` when the
agent runs untrusted code or code on the user's behalf.

Every backend block rejects unknown keys, so a typo fails the boot loudly rather
than silently downgrading access. Legacy spellings `sandbox_backend`,
`backend: "<string>"` and a non-object `sandbox:` all raise.

Resource ceilings live in a **separate** top-level `sandbox:` block —
`{ memory: "2g", cpus: 2 }`. Both unset means inherit (25% host RAM, 50% host
cores).

---

## Mode — the `security:` block

There is no field called `mode`. The field is `security.profile`, and these are
the only three accepted values:

| User-facing name | `security.profile` | Meaning |
|---|---|---|
| **Trusted — act freely** | `developer` | **The default.** Auto-approves low-risk actions. |
| **Supervised — ask before writes** | `safe` | Human approval on writes; nothing auto-approved. |
| **Locked-down — strictest** | `production` | Blocks PII, paranoid skill audit, nothing auto-approved. |

An unknown value raises `Unknown security profile '<x>'. Valid: ['safe', 'developer', 'production']`.

What each profile actually sets (only for fields the config did not set
explicitly — an explicit value always wins):

| | `safe` | `developer` (default) | `production` |
|---|---|---|---|
| `trust_level` | 2 (collaborator) | 4 (developer) | 3 (autonomous) |
| `pii_action` | `redact` | `warn` | `block` |
| `skill_audit_level` | `strict` | `strict` | `paranoid` |
| `hitl_enabled` | true | true | true |
| `hitl_auto_approve_low_risk` | false | **true** | false |
| `memory_visibility` | full | full | filtered |

**Do not quote the class defaults.** `SecurityConfig`'s own field defaults say
`trust_level: 2` and `hitl_enabled: false`, but the profile validator overwrites
both — a plain new agent resolves to `trust_level 4`, `hitl_enabled true`,
`hitl_auto_approve_low_risk true`. The shipped user guide's table reflects the
class defaults and is misleading on these two rows.

Approval precedence, lowest to highest: `security.profile` → the
`hitl_write_tools` / `hitl_shell` toggles (these only *add* gates) →
`security.tools` per-tool overrides (`always ask` / `never ask`), which win.
`hitl_shell` does nothing unless access includes execute.

Also here: `rate_limits`, `ssrf_protection` (+ allow/block domain lists),
`credential_scrub`, `shell_guard`, `input_scan` / `output_scan`. Leave them at
their defaults unless the user raises a specific need.

---

## Tools

| Field | Type | Default |
|---|---|---|
| `tools` | list of names | `[]` |
| `disabled_tools` | list of names | `[]` |
| `default_internal_tools` | bool | `true` |

Every agent gets a universal set injected regardless of `tools` — time/date,
`whoami`, `talk_to_agent`, notifications, chat directory lookups,
`issue_*`, `web_search`, `web_fetch`, `document_to_text`, `pdf_to_text`,
`trash_*`, and the operations tools (`scheduler_*`, `heartbeat_*`, `pulse_*`,
`approvals_*`, `ops_*`) — plus the deepagents built-ins (`read_file`,
`write_file`, `edit_file`, `ls`, `glob`, `grep`, `execute`, `task`,
`write_todos`, `compact_conversation`). A bare new agent lands at roughly 70
tools and no subagents.

`default_internal_tools: false` is the deny-by-default posture: nothing implicit
is granted. Use it for an agent that must hold exactly one capability.

To remove a single universal tool, name it in `disabled_tools` — e.g.
`disabled_tools: ["talk_to_agent"]` for an agent that must never initiate
contact with peers.

**Cortex tools** (`cortex_list`, `cortex_view`, `cortex_add`, `cortex_edit`,
`cortex_discard`, `cortex_restore`, `cortex_promote`, `cortex_explore`) are
injected **only when Cortex is enabled** for the agent. This is the mechanism
behind "turn Cortex on to get projects".

**Pages tools** are not universal — add the `pages` toolkit to `tools:`
explicitly.

Find what exists with `olano_list_tools`, `olano_list_integrations`,
`olano_list_bundles`, `olano_find_tools(tag)`, and `olano_describe_tool`.

---

## MCP servers

`mcp_servers[]` entries take `url` (required), `transport` (`sse` default, or
`http` / `stdio`), `enabled` (default `true`), `auth` (`none` default, or
`oauth`), `oauth_scope`, `oauth_client_id`, `oauth_client_secret`, plus
`command` / `args` / `env` for stdio servers, `headers` for remote ones, and
`disabled_tools` to hide individual tools.

### The tools, and what they can and cannot do

Every one of these takes the target `agent` as a parameter, so AgentFather can
wire MCP for any agent, not just itself.

| Tool | Signature |
|---|---|
| `olano_search_mcp_directory` | `(query="", kind="", category="", limit=20)` — the catalog of servers that exist |
| `olano_get_mcp_directory_entry` | `(slug)` — one catalog entry in full |
| `olano_list_mcp_servers` | `(agent)` — what this agent already has configured |
| `olano_test_mcp_server` | `(agent, url)` — returns `ok`, `status`, `tools`, `needs_oauth`, logs |
| `olano_add_mcp_server` | `(agent, url, transport="sse", auth="none", oauth_scope="", oauth_client_id="", oauth_client_secret="", command="", command_args=None, env=None, headers=None)` |
| `olano_remove_mcp_server` | `(agent, url)` |
| `olano_authorize_mcp_server` | `(agent, url)` — returns the provider `authorization_url`; raises if the server is not already in the config |
| `olano_mcp_oauth_status` | `(session_id)` |
| `olano_mcp_status` | `(agent="")` — empty means every agent |

Note the stdio arguments parameter is **`command_args`**, not `args` — the tool
layer reserves `args`.

There is no update tool: `olano_add_mcp_server` replaces by URL, so re-adding
with the full set of fields is how you change one.

### Order of operations for OAuth

1. `olano_search_mcp_directory(query)` — get the verified URL, transport and auth mode.
2. `olano_test_mcp_server(agent, url)` — confirm it and see whether it needs OAuth.
3. `olano_add_mcp_server(agent, url, …)` — **before** any sign-in, always.
4. `magic_link_connect_service(url, agent="<agent>")` — magic link + PIN for the human.

Step 3 before step 4 is not a style preference. `magic_link_connect_service` on an
`https://` target mints the `mcp-oauth` flow, which stores tokens against the
URL. A completed sign-in for a server that was never added back-fills a config
entry using the transport the sign-in itself connected over — but adding it
first, with the settings the directory gave you (headers, scope, client
credentials), is still the only way to be sure the entry is the one you meant.
`olano_authorize_mcp_server` raises outright when the URL is not configured.

If registering the back-filled entry fails (for example the deployment's MCP
server cap), the chat notice says so — "signed in, but the server could not be
added" — rather than reporting the connection as live. Treat that as your cue
to fix the config and reload, not to retry the sign-in.

### Credential wiring

`${vault:NAME}` references resolve per-agent at connect time in **both** the
`env` block (stdio children) and the `headers` block (remote servers), so the
usual remote pattern is safe:

```
headers={"Authorization": "Bearer ${vault:ACME_API_KEY}"}
env={"ACME_API_KEY": "${vault:ACME_API_KEY}"}   # stdio equivalent
```

An unresolvable ref is left verbatim rather than blanked — an empty credential
reaches the server looking valid and reads as a server bug, where the literal
`${vault:MISSING}` names the typo in the auth error.

### What blocks it

- **No browser-reachable redirect** — OAuth resolves its callback from a
  configured relay, then `LIONCLAW_PUBLIC_URL`, then the last origin a browser
  used to reach the dashboard. With none of those it raises an operator-facing
  error rather than degrading. Magic links share the same origin resolution, so
  a box with no public origin can mint none at all.
- **Managed deployments skip stdio MCP servers entirely** — the `npx` / `docker`
  recipes are self-host only. Use a remote URL.
- OAuth flows expire after 300s; the watcher gives up at 360s. Magic links live
  15 minutes by default (5–240 configurable), are owner-only, and are refused in
  group chats.

### Directory

`olano_search_mcp_directory` / `olano_get_mcp_directory_entry` read the synced
MCP directory, which carries the verified `url`, `transport`, `auth`,
`oauthClient` mode and expected `envKeys` per server (HubSpot, Stripe, Notion,
Linear, Sentry, GitHub, Intercom, PayPal, Square, Canva, Webflow, Wix, Asana,
Atlassian, Vercel, Zapier and more), plus the console page where a user
generates an API key. An empty query lists it.

Never substitute your own memory of an endpoint URL for a directory lookup, and
confirm with `olano_test_mcp_server` before adding. The same catalog is what the
dashboard's *Add MCP server* modal shows, so an operator sees the same entries
you do.

## Credentials

Secrets never go in `agent.json5`. Route them to the per-agent vault:

- `magic_link_connect_service` — OAuth-style connection flow for a supported service.
- `magic_link_request_secret` — ask the user for a key over a magic link, so it never
  passes through chat.
- `secret_set` — set it directly when you already hold it legitimately.
- `secret_list` — see what an agent already has.

Resolution order is per-agent overlay → vault (per-agent, then common) → process
environment. `${VAR}` in a config is interpolated at load; an unset variable is
left as the literal `${VAR}`, which is a common cause of "the tool says it has
no credentials".

---

## Cortex — the `cognition:` block

Every gate here is three-state: **`null` (the default) = inherit the home-wide
setting; `true`/`false` = this agent overrides it.** The rule is *the agent wins
when it has an opinion; the global applies when it doesn't.*

| Field | Type | Default |
|---|---|---|
| `enabled` | bool \| null | `null` (inherit; the home-wide default is **off**) |
| `fast_model` / `deep_model` | string \| null | `null` (inherit; managed boxes resolve to `olano:turbo` / `olano:pro`) |
| `self_learning`, `brainstorming`, `idea_generation`, `prototyping`, `skills_optimization`, `workspace_optimization`, `memory_optimization`, `pulse` | bool \| null | `null` (inherit) |
| `autonomous`, `auto_approve` | bool \| null | `null` |
| `intensity` | `relaxed` \| `balanced` \| `aggressive` \| null | `null` (inherit; the home-wide default is `relaxed`) |
| `approval_required` | list | `["external_send", "irreversible", "identity_edit"]` |

**Cortex is off unless someone turned it on.** The home-wide
`cognition.enabled` defaults to `false`, so an agent that inherits runs no
background cognition: no scheduled phases, no learning from its conversations
into memory, no `cortex_*` tools. When the person wants this agent to learn and
think on its own, set its `cognition.enabled: true` (it runs even while the
fleet stays off) and say so in the approval narration; to turn it on for every
agent, change the home-wide `cognition.enabled` instead. Leave `intensity` at
`null` unless they ask for a busier pace: the inherited `relaxed` is the
cheapest.
| `max_deep_runs_per_day` | int | `12` |
| `worker_max_iterations` | int \| null | `null` (inherit; default 100, clamped 10–400) |
| `worker_timeout_seconds` | int \| null | `null` (inherit; default 1200, clamped 30–7200) |
| `force_tier` | `fast` \| `deep` \| null | `null` |

Out-of-range budgets are **clamped, not rejected**. Raise
`worker_max_iterations` and `worker_timeout_seconds` together — the timeout is
the wall the step budget runs into, so lifting one alone just converts step
exhaustion into timeouts.

`approval_required` is the one list `auto_approve` never overrides. Whatever
Fully Autonomous would otherwise apply, an item or background tool call in a
listed category waits in Pulse → Approvals for a human: `identity_edit` (any
write to SOUL.md / AGENTS.md / IDENTITY.md), `external_send` (email, calendar,
posts, messages to other agents), `irreversible` (moving money, overwriting a
stored secret). It is a plain list with a concrete default, not a three-state
inherit: `[]` means the owner wants this agent fully unattended, a shorter list
keeps only what it names, and an unknown name is ignored. Everything outside
those categories — learned skills, memory, procedures, tidy-ups — still goes
through on its own.

A deployment-level entitlement can withhold Cortex entirely; no per-agent
setting overrides that.

### What Cortex gives the user

Background thinking (ideas, brainstorming, prototypes), self-improvement, and
the **project workflow**: `cortex_add(kind="project", …)` scaffolds
`<workspace>/projects/<slug>/` with

- `README.md` — purpose, why it matters, origin, goals, success criteria
- `PLAN.md` — the single source of truth for progress: phases → substeps →
  `- [ ]` checkboxes, each ending in a `✅ Verify:` line, so completion is
  computable rather than claimed
- `STATUS.md` — current phase, in progress, blockers, last updated
- `JOURNAL.md` — dated log
- `ARCHITECTURE.md` — only for technical builds needing a design doc

`cortex_promote(item_id)` grows an existing idea, brainstorm or prototype into
one of these. Statuses are `active`, `exploring`, `promoted`, `discarded`. The
root-level `PROJECTS.md` / `IDEAS.md` / `BRAINSTORMING.md` files are generated
index tables — do not hand-edit them.

---

## Memory and self-learning

| Field | Type | Default |
|---|---|---|
| `memory.markdown` | bool | `true` |
| `memory.rag` | bool | `false` |
| `memory.rag_collection` | string | `"lionclaw_memory"` |
| `memory.self_learning` | bool | `true` |
| `memory.self_learning_interval` | int (seconds) | `3600` |
| `memory.per_customer` | bool | `false` |

**Two different self-learning fields — keep them apart.**
`memory.self_learning` is the background loop that distils this agent's own
sessions into memory. `cognition.self_learning` is the Cortex feature gate
(three-state, inherits). `cognition.deep_self_learning` (default `true`) decides
whether that distillation uses the deep model.

`memory.per_customer: true` disables the shared self-learning loop by design —
one file cannot hold every customer's distillation.

---

## Heartbeat, issues, schedules

| Field | Type | Default |
|---|---|---|
| `heartbeat.enabled` | bool \| null | `null` (inherit; on) |
| `heartbeat.interval_minutes` | int \| null | `null` (inherit; 60, clamped 1–1440) |
| `issues.auto_process` | bool \| null | `null` (inherit; on) |
| `issues.poll_minutes` | int \| null | `null` (inherit; 15, clamped 1–1440) |

`schedule[]` entries take either `cron` (5-field) or individual
`minute`/`hour`/`day`/`month`/`day_of_week` parts, plus `timezone` (IANA),
`message` (**required** for a normal entry), optional `name`, `thread_id`,
`enabled` (default `true`).

Fan-out entries (`for_each_customer: true`) require multi-user mode or the
config fails to load, and take either `message` or `action`. Placeholders:
`{customer_key}`, `{platform}`, `{user_id}`.

---

## Org — who talks to whom

| Field | Type | Default |
|---|---|---|
| `org.title` | string \| null | `null` |
| `org.manager` | string \| null | `null` |
| `org.reports` | list | `[]` |
| `org.coworkers` | list | `[]` — **display only on the chart** |
| `org.input_agents` | list | `[]` — peers allowed to message this agent |
| `org.output_agents` | list | `[]` — peers this agent may message |
| `org.input_mode` / `org.output_mode` | `everyone` \| `none` \| `list` \| null | `null` (inferred) |
| `org.mutating_from_manager_only` | bool | `false` |
| `org.autogenerate` | bool | `false` |

**The trap: an empty list means UNRESTRICTED, not "nobody".** With no explicit
mode and an empty list, the effective mode is `everyone`. To close an agent off,
set the mode explicitly.

**Hierarchy always wins.** A manager and its direct reports can always exchange
messages, regardless of allow-lists and even under `mode: "none"`. Names match
case-insensitively.

`mutating_from_manager_only: true` gives non-manager peers read-only service.

`human_only: true` means no agent may reach this one via `talk_to_agent` at all,
whatever the org says. The documented pattern (AgentFather's own) pairs it with
`disabled_tools: ["talk_to_agent"]` so the agent can neither be reached nor
initiate.

### Awareness is not permission

`agent_visibility.mode` is `none` (default) / `all` / `list`, with
`agent_visibility.agents` for the list. It controls which peers the agent is
*told about*. Reachable peers (manager, reports, coworkers, and `output_agents`
when in list mode) are always injected regardless.

---

## Subagents vs. separate agents

`subagents[]` members take `name`, `description`, `system_prompt` (all
required), plus optional `model` (inherits the parent's when unset), `tools`,
`skills`, `enabled`, `backend`, and the two archetype fields:

- `from_archetype: "<stem>"` — fill this member from a catalog archetype. A
  dangling reference **raises**, and referencing a *team* archetype also raises:
  members must be leaf specialists.
- `system_prompt_extension: "…"` — text appended to the catalog prompt. Prefer
  this over a full `system_prompt` override, which detaches the member from
  future pack updates.

A subagent is internal: no roster entry, no channels, no schedule, unreachable
from outside. When the thing needs its own inbox, memory or schedule, build a
top-level agent instead.

`builtin_subagents` defaults to `[]`. The operations manager (`"ops"`) is
opt-in now: its scheduler, heartbeat, PULSE and approvals tools are universal
defaults on the agent itself, removable one by one with `disabled_tools`. An
agent in Customer Service mode gets none of them unless `tools` names one, and
the ones that act on held work (`approvals_*`, `pulse_accept`, `pulse_undo`,
`scheduler_run_now`, `heartbeat_configure`) are owner-only.

---

## Channels

`channels[]` entries need a `type` and pass every other key through to the
adapter. Available types include telegram, slack, discord, whatsapp, signal,
email/imap, matrix, mattermost, sms, homeassistant, dingtalk, feishu, wecom,
weixin, qqbot, bluebubbles, debox, api_server and webhook — confirm against
`olano_list_platforms` on the actual box.

Use `magic_link_connect_channels` for the guided setup. Lock a channel down with its
`allowed_users` list.

**Telegram first** for any new agent: it is the fastest way for the user to see
the agent behave before it faces customers elsewhere.

---

## Multi-user vs. team accounts

Two unrelated features with similar names. Keep them apart when explaining.

**Team accounts — the humans running the deployment.** Not in `agent.json5`.
Invite colleagues from the dashboard (Config → users); omitting a password sends
an invite link instead of setting one. Roles are `owner` and `member`, granted
per agent; admins reach every agent without an explicit grant. `magic_link_open_dashboard`
takes the user there.

**Multi-user mode — the end users chatting with one agent.** The
`customer_service` block, mirrored by `memory.per_customer` (setting either
turns the other on).

| Field | Default |
|---|---|
| `customer_service.enabled` | `false` |
| `customer_service.user_scheduling` | `false` (max 3 jobs/user, 60 min apart) |
| `customer_service.connections` | `false` |
| `customer_service.user_credentials` | `false` |
| `customer_service.customer_credential_names` | `[]` — empty means the tools accept nothing |

`user_scheduling`, `connections`, `user_credentials` and any
`for_each_customer` schedule **fail the load** unless multi-user mode is on.
Enabling it swaps the shared memory tools for per-customer ones (`remember`,
`recall`, `recall_all`, `forget_me`). Per-customer credentials fail closed: a
customer-bound turn uses that customer's value or nothing, never the operator's.

---

## Message policy

Caps for an agent whose chat address is public. All off by default; enforced at
**external ingress only** (messengers, dashboard/REST chat, the
OpenAI-compatible API), so scheduled and background turns are never screened.
Slash commands are never screened either.

| Field | Default |
|---|---|
| `message_policy.max_input_chars` | `null` — longest inbound message in characters; checked before any model call |
| `message_policy.on_oversize` | `"reject"` (a short notice, no turn) · `"truncate"` · `"ignore"` |
| `message_policy.oversize_notice` | `""` — blank uses a built-in line naming the limit and the length sent |
| `message_policy.ignore_patterns` | `[]` — regexes, case-insensitive, unanchored; matched at ingress, **free** |
| `message_policy.ignore_rules` | `[]` — plain-language conditions the model judges; costs one turn per ignored message |
| `message_policy.ignore_notice` | `""` — blank is true silence; text sends that one line |
| `message_policy.max_output_tokens` | `null` — hard ceiling per reply, mapped to each provider's own parameter |

Choosing between the two silence mechanisms is the decision that matters.
Anything a regular expression can decide belongs in `ignore_patterns` — free,
exact, enforced before the model. `ignore_rules` is for judgements only a model
can make ("not written in English", "gibberish"), and the agent must read the
message to judge it, so an ignored message still costs a turn: bound it with
`max_input_chars` and `max_output_tokens`. An invalid pattern **fails the
load**, so a rule can never silently match nothing.

`max_output_tokens` switches off the automatic continuation of a cut-off reply
(otherwise every capped reply would cost extra turns), so a capped reply stops
mid-sentence. On reasoning models the ceiling counts thinking as well, so below
~1024 it can return an empty reply — raise it or set `reasoning_effort` to
`none`/`low`. It never applies to subagents.

---

## Monetization

Bills the agent's **end users** — distinct from the cloud billing that bills the
owner. The owner always bypasses the gate.

| Field | Default |
|---|---|
| `monetization.enabled` | `false` |
| `monetization.providers` | `[]` — `stripe` and/or `telegram_stars` |
| `monetization.trial_days` | `3` |
| `monetization.channels` | `[]` (every human channel) |
| `monetization.gate_groups` | `false` |
| `plan.price_cents` / `currency` / `interval` | `500` / `usd` / `month` |
| `plan.stripe_price_id` | `""` — **empty means Stripe is off** |
| `plan.stars_amount` | `0` — **0 means Telegram Stars is off** |

`STRIPE_API_KEY` and `STRIPE_WEBHOOK_SECRET` live in the per-agent vault, so
each owner bills through their own Stripe account.

---

## Pages

Host-wide, not an `agent.json5` field. Add the `pages` toolkit to `tools:` to
get `pages_start(slug, directory)` (serve files), `pages_deploy` (run a build,
a server, or both), `pages_status`, `pages_logs`, `pages_stop`,
`pages_restart`, `pages_delete`.

`pages_deploy` runs commands where the agent's shell runs, so it needs an agent
with shell access (a sandbox, a shell backend or a shell tool) and is approved
like `execute` under the `safe` and `production` profiles. Builds and servers
are switched, capped and tuned deployment-wide in `config.yaml → pages`
(Config → Deployed pages): `services_enabled`, `max_services_per_agent`
(default 5), health interval, request timeout, upload size and concurrency.

Slugs are claimed host-wide (2–63 chars, lowercase alphanumeric and hyphens;
`api`, `admin`, `assets`, `auth`, `dashboard`, `openapi`, `pages`, `static` and
`www` are reserved).

**Pages are never public.** They sit behind HTTP Basic Auth with one credential
per agent, and **only a human can set it** — dashboard Agent → Pages. Agent
tools can neither set nor read the password. Until a credential exists, every page for that agent returns
**503**. Tell the user this before they wonder why their site is broken.

---

## Building — the tools to call

| Purpose | Tool |
|---|---|
| See the roster / one agent | `olano_list_agents`, `olano_get_agent` |
| Browse ready-made agents | `olano_list_builtin_agents`, `olano_list_archetypes` |
| Read one archetype in full | `olano_read_archetype` |
| Install an archetype as an agent | `olano_scaffold_archetype(archetype, as_name, model, overwrite)` |
| Create from scratch | `olano_create_agent` |
| Change anything later | `olano_update_agent` |
| Prompt / file edits | `magic_link_edit_instructions`, `magic_link_request_file_edit`, `olano_write_agent_file` |
| Tools & catalogs | `olano_list_tools`, `olano_list_integrations`, `olano_list_bundles`, `olano_find_tools`, `olano_describe_tool`, `olano_set_tool_enabled` |
| Models | `olano_list_models` |
| MCP servers | `olano_add_mcp_server`, `olano_test_mcp_server`, `olano_authorize_mcp_server`, `olano_mcp_oauth_status`, `olano_list_mcp_servers`, `olano_remove_mcp_server` |
| Channels | `magic_link_connect_channels`, `olano_list_platforms` |
| Credentials | `magic_link_connect_service`, `magic_link_request_secret`, `secret_set`, `secret_list` |
| Skills | `olano_list_skills`, `olano_list_builtin_skills`, `olano_create_skill`, `olano_install_skill` |
| Subagents | `olano_list_subagents`, `olano_add_subagent`, `olano_toggle_subagent` |
| Lifecycle | `olano_start_agent`, `olano_stop_agent`, `olano_restart_agent` |
| Verify | `olano_diagnose_agent`, `olano_agent_status`, `olano_doctor`, `olano_agent_errors`, `olano_tail_log` |
| Send the user to a UI | `magic_link_open_dashboard(section)` |

Mutating admin tools pass through a human approval gate — expected, not an
error. `olano_run_agent_command` is always gated and cannot be un-gated.

**A running agent does not hot-reload skills or config.** After installing a
skill or changing a config, `olano_restart_agent(name)`, then verify.

---

## What makes a config fail to load

Worth knowing so you can recognize the error rather than guessing at it:

- `olano:reasoning` as a model (retired, not aliased).
- `olano:vision` or `olano:olano-image` assigned as a brain.
- `sandbox_backend:`, `backend: "<string>"`, or a non-object `sandbox:`.
- Legacy security keys `sandbox_mode`, `shell_use_docker`, `docker_image`,
  `docker_network`.
- An unknown `security.profile`.
- A `subagents[].from_archetype` that does not resolve, or that points at a team.
- `user_scheduling` / `connections` / `user_credentials` / a `for_each_customer`
  schedule without multi-user mode.
- A schedule entry with no `message`.
- An unknown key inside a `backend:` block.
