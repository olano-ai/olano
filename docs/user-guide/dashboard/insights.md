# Insights

Charts and numbers: token and cost usage over time, per agent and per model,
thread counts, and activity trends. If your deployment displays costs in
credits rather than dollars, this section follows that setting.

The figures cover **everything your deployment runs, not just chat**: Cortex
background thinking, scheduled tasks, delegated skill workers, the daily
highlights and periodic reports, post-turn PULSE curation, subagent calls, and
the non-chat services (image generation and editing, vision, voice, web
search) all count. A **"Where credits went"** card breaks the window down by
kind of work — chat and channels, Cortex, scheduled tasks, and so on — which
is the place to look when usage seems higher than your conversations alone
explain, and a **"Credits by tier"** card splits managed model spend across
the Turbo / Pro / Max tiers so you can see which tier is doing the work.

Every number here is **measured on your own deployment**: each model call and
service call records its charge locally as it happens, so the charts, the
breakdown cards, and the headline credits figure all come from the same local
record and stay available even when the deployment is offline. That one record
also backs every range button, so the ranges nest the way you would expect —
**1 day** is a rolling last-24-hours view and is always contained in **7 days**,
which is contained in **30 days** and **90 days**. A shorter range can never
show more calls or more spend than a longer one; if it ever does, something is
wrong and it is worth reporting. The range buttons mean the same thing on the
**Drill-down** tab, which shares the selector — "1 day" is the last 24 hours
there too, scoped to the conversations active in that time.

Drill-down's agent ranking and its chat-vs-autonomous cards read the same
record as the Overview, so an agent's total is the same number on both tabs.
What changes is when you **open** an agent: from there down, the figures are
attributed from stored conversation history, the only record that ties a cost
to a particular conversation or tool. Long conversations are compacted as they
grow, which removes older calls from that history, so the conversations you can
open account for **part** of an agent's total rather than all of it. The page
says how much — "openable conversations account for $X of $Y" — so a short list
under a large total reads as coverage rather than as a contradiction. One more
difference follows from what it ranks: a conversation is counted whole, in the
range its most recent activity falls into, so a long-running thread brings its
entire cost with it. Take the Overview and the agent ranking as the totals, and
the levels below as the answer to *where inside a conversation* the cost went.

Billing itself
still lives with your Olano account — the **Credits left** balance shown on
the Overview page and the usage bar on your cloud deployment page track the
account's remaining balance for the billing period, which is a different
window than the charts' last-N-days view, so the two totals are not expected
to match digit for digit.

On deployments upgraded from an older version, the dashboard reconstructs
past Cortex background spend from its stored history the first time it starts,
so the charts show the full picture rather than beginning at the upgrade day.
Reconstructed history is marked internally and never re-billed — your balance
is unaffected.

The **Reply feedback** card, in the Fleet activity section, counts the thumbs up
and thumbs down people gave agents' replies in the chat during the selected
range, per agent (see [Chat → Rating a reply](chat.md#rating-a-reply)). A vote
counts once, as it stands now, in the range it was last changed in; a vote
taken back does not count. It reads the votes kept on this deployment, so it
works without Langfuse. Members see the counts for their own agents only.

When tracing to Langfuse is on, an admin with **Langfuse** links switched on
(Admin → Users) drilling into a conversation sees an
**Open in Langfuse** link beside its name, which opens all of that
conversation's traces in Langfuse.
