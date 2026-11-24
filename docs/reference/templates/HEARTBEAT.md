---
title: "HEARTBEAT.md Template"
summary: "Workspace template for HEARTBEAT.md"
read_when:
  - Bootstrapping a workspace manually
---

# HEARTBEAT.md Template

The runtime polls this file (per agent) on the configured heartbeat interval —
**hourly by default**; set it per agent in `agent.json5 → heartbeat` or
system-wide in `~/.olano/config.yaml → heartbeat:` — and sends each due task
to the agent as a `HEARTBEAT: <task>` message. Keep this file empty (or with
only comments) to skip heartbeat runs entirely, or disable polling outright
(dashboard Heartbeat card / `heartbeat_configure`).

```markdown
# HEARTBEAT
# Entries are YAML-ish list items; blank lines / comments separate tasks.
#
# when: always            → runs roughly once per poll (hourly by default)
# when: <5-field cron>    → fires at the next poll tick at/after the cron time
#                           (server-local time; standard cron day-of-week: 0 or 7 = Sunday)
# notes:                  → optional context passed along with the task

- task: Check the inbox for urgent messages
  when: always
  notes: Only surface things that need action today

- task: Post the weekly summary
  when: "0 9 * * 0"
```

Reply conventions: the agent answers `HEARTBEAT_OK` when there is nothing to
report; any other reply is captured as a PULSE reminder on the dashboard.
Last-run state is tracked in `memories/heartbeat-state.json`.
