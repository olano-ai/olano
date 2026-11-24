# Knowledge

The searchable document library (RAG). Add files, folders or URLs and they are
indexed so agents can search them by meaning. Documents you drop into an agent's
`knowledgebase/` folder are picked up automatically. Use this for manuals,
policies, contracts and reference material you want agents to cite.

The page has no store of its own: it runs every search, listing and ingest
through the knowledge-base tools of **the selected agent** — the agent picker in
the page's top bar, the same selection the sidebar's agent list drives, so the
agent named here is always the one named everywhere else. Those tools are not granted
automatically, so an agent has them only when its `tools` list names them (see
[`agent.json5` reference](../agent-json5.md#tools)). When the selected agent is missing them the page says so,
names exactly which are absent, and disables the controls it cannot serve —
pick another agent, or add the tools to that one and restart it. A knowledge
call needs a running agent: if the selected one is stopped, the page says so
and answers through another agent that is running until you start it.
