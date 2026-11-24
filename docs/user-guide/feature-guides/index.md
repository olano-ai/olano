# Feature guides

The reference pages say what every field and tab does. These guides say how
the pieces fit together for one job at a time: what to turn on, in which
order, and what to expect once it runs. Each one points back at the exact
fields in [`agent.json5`](../agent-json5.md) and [`config.yaml`](../config-yaml.md).

- [Agent-to-agent and the org chart](agent-to-agent.md): who may ask whom for
  work, and how the org chart and the allow-lists combine.
- [Human in the loop and approvals](human-in-the-loop.md): which actions wait
  for a person, and where the approval cards appear.
- [Privacy and security](privacy-and-security.md): what an agent may read,
  reach and run, and how to tighten it.
- [Multi-user](multi-user.md): team accounts on one deployment, and
  per-customer isolation for a public-facing agent. Two different features.
- [Subscriptions and monetization](subscriptions-and-monetization.md): charging
  for access to an agent.
- [MCP servers](mcp-servers.md): giving an agent external tools, with or
  without a sign-in.
- [Webhooks](webhooks.md): letting outside systems trigger an agent.
- [Pages, sites and web apps](pages.md): publishing reports, npm-built apps and
  small web apps with their own server, behind a password you set.
- [Scheduler and heartbeat](scheduler-and-heartbeat.md): recurring work, and
  the agent's own periodic check-in.
- [Memory and self-learning](memory-and-self-learning.md): what an agent keeps
  between conversations and how it distils it.
- [Notes](notes.md): the agent's linked wiki.
- [Issues](issues.md): the shared tracker between people and agents.
- [Revisions](revisions.md): the automatic history of configuration and
  markdown changes.
- [Channels and connectors](channels-and-connectors.md): WhatsApp, Telegram,
  Slack, Discord, email and the rest, including the OpenAI-compatible API.
- [Cortex and Pulse](cortex-and-pulse.md): the background reasoning engine and
  the proactive suggestions it produces.
- [The mobile app](mobile-app.md): the Olano app for phones, tablets and Macs,
  and the notifications it receives.
- [Budgets](budgets.md): token ceilings and spending warnings.
- [Ask the agent](ask-the-agent.md): self-configuration, magic links and the
  AgentFather admin tools.
- [Beyond the deployment, A2A and MCP](a2a-and-mcp.md): talking to outside
  agents, and letting outside platforms reach yours.
