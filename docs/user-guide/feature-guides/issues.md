# Issues

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
