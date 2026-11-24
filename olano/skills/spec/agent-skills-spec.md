# Agent Skills Spec

The spec is now located at <https://agentskills.io/specification>

## Olano frontmatter extensions

Olano reads a few extra keys from `SKILL.md` frontmatter, all optional:

| Key | Type | Meaning |
|---|---|---|
| `category` | string | Grouping label for listings (default `general`). |
| `tier` | `light` \| `subagent` | `light` (default) is described in the parent prompt; `subagent` runs on demand through `delegate_task`. |
| `trigger` | string | One line on when to reach for the skill. |
| `blocked_tools` | list | Tools a `subagent`-tier skill may not call. |
| `max_iterations`, `timeout_seconds` | int | Per-skill budget for a `subagent` run (defaults follow the shared delegate budget). |
| `requires_tools` | list | Tool names the skill assumes. The skill is advertised to an agent only when **every** listed tool is in that agent's effective tool set; otherwise it is hidden from the prompt, not delegable, and listed as *gated* with the missing names. It never grants a tool. Use it for skills written around a specific toolkit — the AgentFather playbooks declare `requires_tools: [olano_list_agents]`. Names are case-sensitive registry tool names; MCP tools and per-agent dynamic closures cannot be named here. |
