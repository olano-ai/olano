# Ask the agent: self-configuration, magic links and admin tools

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
  [MCP servers](mcp-servers.md)), adds the one you pick to itself, and hands you the
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
  See [Switching a skill on or off](../dashboard/agents.md#switching-a-skill-on-or-off).

## Magic links — the default whenever a browser is needed

A magic link is a short-lived, PIN-protected page the agent sends into the
chat; whoever opens it finishes one task on their own phone or laptop, and the
chat is told the outcome ([`magic_links:`](../config-yaml.md#magic_links-secure-links-for-connecting-people)
has the knobs). Every agent has these tools by default:

| Ask | Tool | What the person sees |
|---|---|---|
| Sign in to a service, an MCP server or an official connector (GitHub, Google, OpenAI Codex, Microsoft 365, Slack, Notion, …) | `magic_link_connect_service` | the provider's own consent screen, or a one-time code to type on the provider's device page — whichever method the service offers and the bot picked (Olano's app, your own OAuth app, a code, or a secure form for a key); an MCP server that needs an OAuth app of your own (HubSpot) first asks for its Client ID and secret on the same page, or offers Composio instead ([MCP servers](mcp-servers.md)); the connection serves the service's tools, MCP servers and the git tools at once, no restart |
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

## The `self_*` tools — what a bot may change about itself

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
Inherit / On / Off). See [`agent_autonomy:`](../config-yaml.md). A
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

## AgentFather — the `olano_*` tools for the whole deployment

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

### Testing an agent before you rely on it

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

### Reviewing Cortex across agents

Every agent with [Cortex](cortex-and-pulse.md) on keeps a funnel of
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
[Building agents from a conversation](../building-agents.md)) and **olano-admin**
(operating the deployment). Every other agent gets **olano-agent** (how to do
the things above for itself — one finder, one ladder), **olano-platform** (the
map of the platform) and **olano-cloud** (plans, credits and usage).
