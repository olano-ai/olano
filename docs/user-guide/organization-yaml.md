# `organization.yaml` — the org chart

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
