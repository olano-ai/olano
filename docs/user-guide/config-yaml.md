# `config.yaml` reference

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

## Root keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `default_agent` | string | `"merli"` | The agent the dashboard chat and the simple chat open first, and the one API calls that name no agent go to. |
| `preferred_model` | string \| null | `null` | Default model for new agents. |
| `runtime_api_port` | integer | `8765` | The runtime API port. *Restart required.* |
| `dashboard_port` | integer | `3200` | The dashboard port. *Restart required.* |
| `multi_user_enabled` | boolean \| null | `null` | **Team accounts.** Unset resolves by deployment type — on for managed deployments, off for a manual install. See [Multi-user: two different features](feature-guides/multi-user.md). *Restart required.* |
| `ui_mode` | `"advanced"` \| `"simple"` | `"advanced"` | **Single-user installs only.** Which interface the operator sees: `advanced` is the full dashboard, `simple` is the stripped bot-chat app described in [The dashboard](dashboard/index.md#two-interfaces-full-dashboard-and-simple-chat). Team deployments ignore this and use each person's own **Interface** setting in **Admin → Users**. Edited in `config.yaml` directly — no dashboard form yet. |
| `highlights_all_agents` | boolean | `false` | Show highlights across every agent instead of the current one. |
| `extra_bind_roots` | string[] | `[]` | Extra host directories agents may reach. |
| `archetype_dirs` | string[] | `[]` | Extra directories to search for agent templates. |
| `egress_allow` | string[] | `[]` | Domains every agent's tools may reach — bare domains like `api.github.com`, a domain covering its subdomains. Empty means every public destination is allowed. An agent whose own `egress_allow` is non-empty uses its list instead; an agent with an empty list inherits this one. Governs every outbound request a tool makes — web and HTTP tools, the headless browser, remote-agent calls and every integration toolkit — but not the sandbox shell. Edited in **Config → Outbound access**; applies to the next tool call. See [`agent.json5` reference](agent-json5.md#security). |
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

## Sandbox tool bundles

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
cloning and pushing private repositories ([`agent.json5` reference](agent-json5.md#tools)), and the
**Connections** page (account menu → Setup) to sign in once for all of them.

## `cognition:` — Cortex defaults for the fleet

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

## `default_models:` — where the `default:*` aliases point

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
See [Deployment default models (`default:*`)](models-and-credits.md#deployment-default-models-default) for how the aliases behave.

## `heartbeat:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Agents wake periodically to check `HEARTBEAT.md`. |
| `interval_minutes` | integer | `60` | Clamped 1–1440. |

## `issues:` — how agents pick up work

| Key | Type | Default | Meaning |
|---|---|---|---|
| `auto_process` | boolean | `true` | Agents pick up issues assigned to them automatically. |
| `poll_minutes` | integer | `15` | Clamped 1–1440. |

## `revisions:` — automatic version history

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

## `push:`, notifications to the mobile app

Notifications to the Olano app on people's phones. They are delivered through
the Olano Cloud, so they work on a deployment that runs on Olano Cloud and not
on a self-hosted one. Each person chooses on their own phone which kinds they
receive; these keys are the deployment-wide limits. See
[The mobile app](feature-guides/mobile-app.md).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Send notifications at all. Takes effect at the next restart; phones that registered stay registered and receive nothing while it is off. |
| `previews` | boolean | `false` | Put the start of a reply, or a Cortex finding's title, into the notification. Off, a notification names the agent and the reason ("Replied to your message.") and nothing anyone wrote. On, that text passes through Apple's or Google's notification service on its way to the phone. Applies to the next notification. |
| `cortex_min_priority` | `"urgent"` \| `"normal"` \| `"low"` | `"urgent"` | The lowest Pulse priority that notifies. `urgent` notifies only for urgent findings; `normal` adds ordinary ones; `low` notifies for every finding. Cortex proposals waiting for approval notify whatever this says. An unknown value means `urgent`. |
| `alert_min_level` | `"warn"` \| `"error"` | `"warn"` | The lowest bell-notification level that notifies administrators' phones. `warn` includes warnings and errors; `error` only errors. Each distinct problem notifies at most once every six hours. An unknown value means `warn`. |

## `mobile_app:`, what the mobile app shows

How the Olano app looks to everyone who signs in to this deployment on a phone,
administrators included. Edit it in the dashboard under **Config, Mobile app**.
Phones download it when the app opens and every time it comes back to the
foreground, so a change reaches a phone the next time it is opened, with no app
update and no restart. Every switch defaults to shown, so a deployment that
never sets the block looks as it always has. Hiding only removes an entry from
the phone: the dashboard and everything behind it keep working, and nothing is
deleted. See [The mobile app](feature-guides/mobile-app.md#what-your-administrator-chooses).

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

## `usage_budgets:` — token ceilings and budget warnings

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

## `disk_monitor:`

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

## `mcp_health:` — keeping MCP connections alive

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `check_interval_minutes` | integer | `10` | Clamped 1–1440. |
| `refresh_tokens` | boolean | `true` | Renew sign-ins before they expire. Leave this on — a token that lapses while the agent is idle can become unrecoverable. |
| `refresh_window_minutes` | integer | `20` | How far ahead of expiry to renew. Clamped 1–720. |
| `auto_reconnect` | boolean | `true` | Retry servers that timed out or errored, with backoff. A server that needs a sign-in you have not given is never retried — reconnecting cannot invent consent. |

## `checkpoint_gc:`, trimming conversation history on disk

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

## `scheduler:`, how late a scheduled job may still run

A scheduled job is due at an instant, and the runtime is not always free at
that instant: a long turn, a heavy page load or a restart can hold it past the
minute. This block says what happens then. Inside the window the job runs late
and its message says by how much; past it the firing is recorded as *skipped*
in the job's history and a notification names it. Read when a job is
registered, so a change applies on the next restart or the next time the job
is saved. Applies to the Scheduler tab's tasks and to `schedule` entries in
`agent.json5`; heartbeat polls and Cortex phases keep a short fixed window. See
[Scheduler and heartbeat](feature-guides/scheduler-and-heartbeat.md).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `misfire_grace_minutes` | integer | `360` | How late a firing may still run. Clamped 1–1440. Under the old fixed five minutes, a busy morning lost the day's run outright. |
| `catch_up_on_boot` | boolean | `true` | At startup, run a job whose most recent due time fell inside the window with no run recorded since (the runtime was down or restarting at the time). `false` lets a restart drop it, as before. |
| `max_run_minutes` | integer | `180` | The wall clock one scheduled run (or heartbeat poll) may use. A run still going at the limit is stopped and recorded as failed with the reason; what it wrote is kept and the job's next firing runs. Only one run of a job executes at a time, so without a limit a run that never ended held its job forever. `0` removes the limit. Clamped 0–1440. |

## `peer_threads:`, how much two agents carry between them

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

## `pages:`: deployed pages

How pages published with `pages_deploy` are built, run and reached. Plain pages
published with `pages_start` only serve files and ignore this block. Read again
every few seconds, so changes apply without a restart. Edited from **Config →
Deployed pages**. See [Pages](feature-guides/pages.md) for the whole feature.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `services_enabled` | boolean | `true` | Master switch. `false` runs no build, stops every page server within a few seconds (those pages answer `503`), and makes `pages_deploy` refuse. Static releases already built keep serving. |
| `max_services_per_agent` | integer | `5` | Server pages one agent may run at once. A deploy over the limit is refused, naming the agent's running servers. Static builds do not count. Clamped 1–50. |
| `health_interval_seconds` | integer | `30` | How often each running server's health path is requested. Any answer below `500` is healthy; three failures in a row restart the server. Clamped 5–3600. |
| `request_timeout_seconds` | integer | `120` | The longest a viewer's request waits for the server: for the first byte, and between chunks of a streamed answer. Past it the viewer gets `504`. Clamped 5–3600. |
| `max_request_body_mb` | integer | `25` | The largest request body forwarded to a server; bigger uploads get `413`. Clamped 1–1024. |
| `max_concurrent_requests` | integer | `64` | Requests in flight per server. Past it viewers get `503` with `Retry-After` instead of an unbounded queue. Applies the next time a server starts. Clamped 1–1024. |

## `browser:` (browser control)

Whether agents get the browser tools at all, and where the browser runs. Off by
default. Turning it on from the dashboard (Config → Browser) checks that the
machine can carry the mode first and refuses with the reason if not; a value
written into the file by hand that the machine cannot carry is applied one step
lower (live → headless → off) and the Config card shows why. Changes apply
within a minute: open browsers of the old mode are closed (sign-ins saved).
See [Browser control](feature-guides/browser.md) for what each mode does.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | string | `off` | `off`: no agent has a browser. `headless`: an invisible browser, one isolated session per agent (needs 1.5 GB of memory). `live`: a full browser per agent in its own container that you can watch and take over (needs 2 CPUs, about 4 GB, Docker, and a plan that includes it; the browser is downloaded when you turn it on). Anything else reads as `off`. |
| `provider` | string | `local` | `local`: the browser runs on this machine. `remote`: agents connect to a hosted browser service at the DevTools address stored in the vault as `BROWSER_REMOTE_CDP_URL`; no browser memory is used here, and the live view is not available. |
| `max_sessions` | integer | `1` | How many agents may hold an open browser at once. Clamped 1–8. At the limit, the session idle longest is closed to make room. |
| `idle_timeout` | integer | `10` | Minutes without browser activity before an agent's browser is closed (sign-ins saved first). Clamped 1–240. A browser someone is watching is not closed for being idle. |
| `timezone` | string | empty | The time zone every agent browser reports to websites, as a name such as `Europe/Madrid`. Empty: this machine's own clock. Set it to where the browser's proxy (`BROWSER_PROXY_URL` in the vault) has its address, so the two agree. A name this machine does not know reads as empty. |

## `observability:` (tracing to Langfuse)

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
score on that reply's trace (see [Chat → Rating a reply](dashboard/chat.md#rating-a-reply));
the comment only when `capture` is `full`. The votes themselves are kept on the
deployment whether or not tracing is on.

## `context_window:` — compaction and oversized tool results

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
overridden per agent ([`agent.json5` reference](agent-json5.md#heartbeat-and-issues-inherit-by-default-blocks))
and edited in **Config → Context window**.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `keep_fraction` | number | `0.35` | Share of the window kept verbatim after a compaction. Clamped 0.05–0.8. Lower frees more per compaction; higher keeps more of the session. |
| `keep_tokens` | integer | `40000` | The keep budget in tokens, when the window is unknown. Clamped 2000–2000000. |
| `trigger_fraction` | number | `0.85` | Share of the window at which compaction fires automatically. Clamped 0.3–0.98. |
| `trigger_tokens` | integer | `170000` | The trigger in tokens, when the window is unknown. Clamped 8000–4000000. Keep it below the smallest window your unknown models really have. |
| `mcp_result_token_limit` | integer | `5000` | MCP results above this many tokens (about four characters each) are saved to a workspace file. The conversation keeps the file path and a preview of about 1,000 tokens that follows the content: JSON keeps its structure with long lists sampled; tables and logs keep the header, the first and last lines, every error line and one example of each kind of line, with a count of what was left out. `0` turns the cap off. Clamped 0–200000. Results from Olano's own tools use a separate 20000-token offload. |

## `remote_agents:` — fleet rules for talking to outside agents (A2A)

The per-agent `remote_agents` list ([`agent.json5` reference](agent-json5.md#remote_agents-external-agents-over-a2a))
decides *which* external agents an agent may reach; this block sets the rules
every such call obeys.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Master switch. Off ⇒ no agent can reach any remote agent; the tools answer with an explanatory error rather than disappearing. |
| `allow_ad_hoc_discovery` | boolean | `false` | Whether `a2a_discover_agent` may read the Agent Card of a URL that is **not** in the calling agent's list. Sending a message is never ad hoc, in any mode. |
| `max_reply_chars` | integer | `20000` | Replies longer than this are truncated before the agent reads them. Clamped 500–500000. |
| `max_message_chars` | integer | `20000` | Longer outbound messages are refused. Clamped 500–500000. |

## `external_access:` — letting outside systems reach your agents

Home-wide switches and policy for the **inbound** surfaces: the MCP endpoint
at `<public URL>/api/mcp`, the per-agent A2A endpoints at
`<public URL>/api/a2a/agents/<agent>`, and Olano's own OAuth sign-in for
clients that cannot paste a token. Both surfaces are **off by default**; each
agent still has to opt in on its **External access** tab
([`agent.json5` reference](agent-json5.md#external_access-letting-outside-systems-reach-this-agent)). Edited
from **Config → External access**, which also lists the tokens and OAuth
clients. See [Beyond the deployment: A2A and MCP](feature-guides/a2a-and-mcp.md).

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

## `catalog_sync:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Keep the model, MCP and agent catalogs current. |
| `manifest_poll_minutes` | integer | `60` | Clamped 15–1440. |

## `credits:` — how costs are displayed

| Key | Type | Default | Meaning |
|---|---|---|---|
| `display` | `"usd"` \| `"credits"` | `"usd"` | How costs are shown. Display only — the underlying accounting is unchanged. |

## `magic_links:` — secure links for connecting people

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

## `prompt_refresh:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | Pick up edits to identity/instruction files without a restart. |
| `check_interval_minutes` | integer | `2` | Clamped 1–1440. |
| `notify_threads` | boolean | `true` | Tell active conversations that the prompt changed. |

## `speech:` — voice in and out

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
[Mobile app](feature-guides/mobile-app.md#hearing-replies).

Transcription applies to voice notes on every messenger that carries them,
WhatsApp included. If a voice note on one channel is answered as though nothing
was said while the same words work elsewhere, the audio never reached the
transcriber — not a speech setting. See
[Channels and connectors](feature-guides/channels-and-connectors.md) for the WhatsApp-specific case.

## `media:` — vision and image generation

| Key | Type | Default | Meaning |
|---|---|---|---|
| `vision` | string | `""` | `""` = decide automatically, `"olano"` = force the managed route, or a `"provider:model"` pin for your own key. |
| `image` | string | `""` | Same, for image generation and editing. |

## `web_search:`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `provider` | `""` \| `"olano"` \| `"tavily"` \| `"disabled"` | `""` (auto) | `tavily` always needs `TAVILY_API_KEY` in the vault. |

## `rate_limits:` — message caps

| Key | Type | Default | Meaning |
|---|---|---|---|
| `messages.per_minute` | integer \| null | `null` | `null` = unlimited. An explicit `0` blocks entirely. |
| `messages.per_hour` | integer \| null | `null` | |
| `messages.per_day` | integer \| null | `null` | |
| `per_platform.<platform>` | same three keys | `{}` | Per-connector overrides, e.g. `per_platform.telegram.per_minute`. |

An agent's own `security.rate_limits` overrides these for that agent.

## `playground:` — the prompt sandbox

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Show the Playground tab. Off also skips generating its assets. |

## `self_serve_agents:` — let people make their own bots

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

## `agent_autonomy:` — what agents may change about themselves

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

## `groups:` — multi-bot conversations

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
