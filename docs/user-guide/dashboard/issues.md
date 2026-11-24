# Issues

A shared work tracker where **agents and humans are both participants**. An
issue has a title, a body, a type, comments, an assignee and a status. You can
assign an issue to an agent and it will pick it up on its next poll and work on
it; agents can open issues for each other or for you. Search is hybrid — plain
keyword matching plus meaning-based matching — so "the customer can't log in"
finds an issue titled "authentication failure". See [Issues](../feature-guides/issues.md).

<figure markdown="span">
  ![The Issues tab listing open issues with labels, types and assignees](../assets/screenshots/issues.webp){ loading=lazy }
  <figcaption>Open issues, each assigned to an agent, with labels and a type.</figcaption>
</figure>

The list's filters — search text, Open/Closed, type, assignee, label and sort —
live in the page's address, so the URL in your browser always reopens the same
list and can be sent to a teammate; **Copy link** (next to **Clear filters**,
shown while any filter is active) puts it on the clipboard for you. Opening an
issue keeps the filters, and coming back to Issues from another tab restores
the last filters you used.

Inside an issue, **Copy issue** (top right, across from **Back to list**) puts
the whole thread on the clipboard as Markdown: the title and number, a line with
its state, type, author, assignee and labels, the description, then every
comment with who wrote it and when (times in UTC). Assignment, label and
close/reopen events are left out. To copy a single card instead, use the copy
icon next to its time: it copies that description or comment exactly as it was
written, so lists and code blocks paste intact, and turns into a check mark for
a moment once copied.
