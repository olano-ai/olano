# Multi-user: two different features

These have confusingly similar names. They are unrelated, and you may want
either, both, or neither.

**Team accounts** — *the humans who operate the deployment.*
Turned on with `multi_user_enabled: true` in `config.yaml` (labelled "Team
accounts" in the dashboard), or automatically by creating the first admin. It
gives you a login screen, invite-only accounts, admin and member roles, and
per-agent access grants. Without it, Olano runs single-user: everyone who can
reach the dashboard is the owner. Managed deployments have it on by default.

How access works: one or more **admins** configure everything and manage users.
Each agent has an owner plus a flat list of members who may chat with it. Admins
can reach every agent without holding an explicit grant — so the per-agent
access list under-reports who can administer an agent, and the **Admin** page's
access overview (account menu → Admin) is the complete picture. Bulk actions apply a role across many
users and agents at once.

Access to an agent is access to all of its conversations: a member granted an
agent sees the same conversation list an admin does — its scheduled runs,
heartbeats, messaging-platform chats and everyone's dashboard chats — and can
continue any of them. Only deleting a conversation is reserved for admins and
the agent's owner.

Manage it in the dashboard's **Admin** page (account menu → Admin), or by asking
**AgentFather** in chat — it can invite people (returning a single-use
set-password link + QR image, never a password), reset passwords the same way,
change admin/member roles, disable or delete accounts, and grant or revoke
per-agent access, each behind a human approval card and admin-only.

Group chats follow the same grants and add one thing on top: the person who
created a group (or an admin) can **share** it with other team accounts from
the group's details panel, so several people and several agents sit in one
conversation. Only people who already have access to every agent in the group
can be shared in, a shared person can read, post and leave but not change the
group, and in a room with more than one person a message that names no agent
is for the other people and wakes none. The details are in the
[`groups:` section](../config-yaml.md#groups-multi-bot-conversations).

**Multi-user mode / per-user isolation** — *the end users who chat with one
shared agent.*
Turned on per agent with `customer_service.enabled: true` (equivalently
`memory.per_customer: true` — the two mirror each other). This is for a support
bot, a community assistant, a paid service: many people talk to the same agent,
and each gets isolated memory, so the agent never mixes one person's context
into another's conversation.

It swaps the shared memory tools for per-user ones (`remember`, `recall`,
`forget_me`) and unlocks optional capabilities:

- `user_scheduling` — end users can set their own reminders, capped by
  `max_jobs_per_user` and `min_job_interval_minutes`.
- `connections` — end users can be introduced to each other, with consent, with
  their own caps and invite expiry.
- `user_credentials` — end users store their own credentials for the agent to
  act on their behalf, restricted to the names in `customer_credential_names`.

These three, and per-customer schedule fan-out, **require** multi-user mode; a
config that uses one without it fails to load rather than quietly ignoring it.

Configure it on the agent's **Multi-user** tab, which also lists end users and
blocks or unblocks them.

The **End users** list on that tab shows everyone who has talked to the agent,
one row per person per platform (a Telegram user and a WhatsApp user are
separate rows): their id on that platform, the platform, the name of their
private folder (for example `telegram-449281`) and when it last changed. It is
visible to admins and to the agent's owner.

- **Block** stops every scheduled run and every scheduled or proactive message
  to that person: their own reminders, the agent-wide per-user jobs and any
  message the agent sends them outside a reply. It applies at once, without a
  restart, and **Unblock** lifts it. Only an admin or the agent's owner can
  block or unblock; nothing said to the agent can.
- Blocking does **not** stop live chat: a blocked person can still message the
  agent and get answers. To refuse their messages, leave them out of the
  channel connector's allowed-users list.
- **Muted** means the person paused their own scheduled messages by asking the
  agent. It is theirs to lift by asking again, so the list shows it but has no
  button for it.
- A person who asked the agent to forget them has no folder and does not
  appear.
