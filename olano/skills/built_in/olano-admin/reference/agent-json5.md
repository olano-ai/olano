# `agent.json5` field reference

Condensed reference for the fields most relevant when building agents. The
authoritative schema is `olano/config/agent_config.py` (`AgentConfig`).

## Identity
- `name` (str, required) — unique slug; also the directory name.
- `description` (str) — one-line summary shown in lists/dashboard.
- `emoji`, `color`, `label`, `avatar_url` — presentation.
- `archetype` (str|null) — the template this agent derives from.

## Model & prompt
- `model` (str) — `provider:model`, e.g. `anthropic:claude-sonnet-4-6`.
- `model_kwargs` (dict) — extra model params (caching, thinking…).
- `reasoning_effort` (str) — effort for reasoning-capable models (`low`/`medium`/…; levels from the model's profile). Unset → `low` on OpenAI/Codex, model default elsewhere; ignored where unsupported.
- `system_prompt` (str) — merged with workspace bootstrap files.
- `prompt_mode` — `full` | `task` | `minimal` | `none`.
- `default_verbosity` — `quiet` | `normal` (default) | `verbose`. How much of
  its work the agent shows in a chat that never ran `/verbosity`, on every
  messaging platform. `quiet` = answer only (also suppresses the
  live progress view), `normal` = answer + a one-line note on a tool-only turn,
  `verbose` = full play-by-play. Aliases (`full`, `v`, `silent`, …) are
  normalized; an unrecognized value falls back to `normal`. A chat that ran a
  verbosity command is pinned and never moved by a later change to this field.

## Tools & skills
- `tools` (list[str]) — tool or bundle names to enable (e.g. `["web", "sql"]`).
- `disabled_tools` (list[str]) — opt out of injected default tools (e.g.
  `["talk_to_agent"]`).
- `skills` (list[str]) — skill directories (absolute or workspace-relative).

## Subagents
- `subagents` (list) — each: `name`, `description`, `system_prompt`, `model?`,
  `tools[]`, `skills[]`, `enabled?`, `backend?`.
- `builtin_subagents` (list) — built-in toolkit specialists; default `[]` (`"ops"` is opt-in: its scheduler/heartbeat/pulse/approvals tools are universal defaults).

## Access & org
- `human_only` (bool) — when true, no other agent can reach this agent via
  `talk_to_agent`. Use for privileged operator agents. Channels are unaffected.
- `fleet_access` (bool) - when true, the agent's shell and file tools also reach
  every agent's directory (home `agents/`, read-write), the common `skills/`
  (read-write) and `logs/` (read-only). AgentFather's grant; only an
  administrator can turn it on, so `olano_update_agent` refuses to set it
  without one on the turn.
- `org` — org-chart placement and peer-messaging allow-lists (`manager`,
  `reports`, `coworkers`, `input_agents`, `output_agents`, `input_mode`,
  `output_mode`).

## Integrations
- `mcp_servers` (list) — each: `url`, `transport` (`sse`|`http`|`stdio`),
  `auth` (`none`|`oauth`), `oauth_scope?`, `command?`/`args?`/`env?` (stdio),
  `headers?`, `disabled_tools?`.
- `channels` (list) — messaging platforms. String form `["telegram"]` or full
  form `{ "type": "telegram", "token": "${TELEGRAM_BOT_TOKEN}",
  "allowed_users": ["<id>"] }`. **Always set `allowed_users` for privileged
  agents.**
- `schedule` (list) — recurring jobs (`cron`, `message`, `thread_id?`).

## Memory & cognition
- `memory` — `markdown`, `rag`, `rag_collection`, `self_learning`,
  `self_learning_interval`.
- `cognition` — background reasoning workers (pulse, idea generation, etc.).
- `pulse` — proactive curated insights.

## Security
- `security.profile` — `safe` | `developer` | `production`.
- `security.trust_level` — 0–4.
- `security.hitl_enabled` — master human-in-the-loop switch.
- `security.hitl_write_tools` / `hitl_shell` — require approval for external
  writes / shell.
- `security.tools` — per-tool `{approval: always|never}` overrides.
- `security.rate_limits` — inbound message caps per agent / per platform.
- `message_policy` — length caps and silence rules for a public agent, all off
  by default and enforced at external ingress only (never on scheduled or
  background turns, never on slash commands):
  - `max_input_chars` (+ `on_oversize`: `reject` | `truncate` | `ignore`, and
    `oversize_notice`) — checked before any model call, so an over-long message
    costs nothing.
  - `ignore_patterns` — regexes, case-insensitive, matched at ingress. Free.
    An invalid one fails the load.
  - `ignore_rules` — plain-language conditions the model judges, for what a
    regex cannot decide (language, intent). Costs one turn per ignored message.
  - `ignore_notice` — blank is true silence; text sends that one line instead.
  - `max_output_tokens` — hard per-reply ceiling, mapped to each provider's own
    parameter. Turns off automatic continuation of a cut-off reply, counts
    thinking tokens on reasoning models, and never applies to subagents.

## Backend (execution environment)
- `backend` — discriminated union. Default and most common:
  ```json5
  "backend": { "type": "local_shell", "root_dir": "workspace", "virtual_mode": false }
  ```
  `virtual_mode: false` is required for absolute paths to resolve. Other types:
  `filesystem`, `state`, `store`, `composite`, `lionclaw_docker`, and remote
  sandboxes.

## Misc
- `env` (dict) — per-agent environment variable overrides.
- `budget` — daily/weekly/monthly USD limits + fallback model.
- `iteration_budget`, `tool_error_retry_limit`, `truncation_retry_limit`.
- `acp_enabled`, `acp_port` — Agent Communication Protocol bridge.
- `workspace` — auto-derived from name; do NOT set it in `agent.json5`.
