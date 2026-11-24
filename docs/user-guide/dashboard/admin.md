# Admin (admin only)

Opened, like Config, from the **account menu** at the bottom-left of the
sidebar (under **Administration**) or from the ⌘K search.

User accounts and access. Invite people, set them as admin or member, and grant
per-agent access. The access overview shows, for every agent, exactly who can
reach it — including admins, who can administer every agent without holding an
explicit grant. Bulk actions apply one role across many users and agents at once,
which is what makes onboarding and offboarding a single step.

All of it is also available from an **AgentFather conversation**: ask it to
invite someone (you get back a single-use set-password link plus a QR code
image to forward — it never asks for or sets a password in chat), reset a
password (a fresh link + QR; the person sets their own and is signed in),
make someone admin or member, enable/disable an account, assign an owner or
member to an agent, or show the whole access grid. Every change asks a human
for approval first, and only admins can trigger these — a member chatting
with AgentFather cannot see or manage accounts. If the link comes back
without a QR or absolute address, the deployment doesn't know its public
URL yet: open the dashboard once from a browser (or set the public URL) and
ask again.

Each account carries two names, and neither is ever empty. **Name** is what
everyone sees, and it can be changed at any time — but never erased (a blank
edit is refused). **Handle** is a permanent public identifier — lowercase
letters, digits and dashes, like `carlos-m` — and it cannot be changed once the
account exists, because it is what identifies that person in the issue tracker
(as `human-<handle>`). A third, optional field is the person's **Title** — their
position, like `Head of Sales` — shown under their name in the account menu,
on the org chart and in the access tables, the same way an agent's title is.
Set it when inviting someone or edit it in the Users table at any time; it
changes nothing about what they may do.

The **Langfuse** column decides whether an admin sees **Langfuse** links in the
dashboard: one under each traced reply in the chat (in both interfaces) and the
**Open in Langfuse** link on an Insights conversation. It is **off for every
account by default**, existing ones included, so turning tracing on does not put
a debugging link under everyone's replies; switch it on for the admins who debug
with Langfuse. It only shows or hides the links: tracing itself is set in Config
→ Observability and keeps running either way. Members show *Admins only* there
and never see the links, because trace data is admin-only. A single-user install
has no Users table, so its owner always sees them.

The handle field appears everywhere an account is created: the first-run
owner screen, this invite form, and the chat "secure your dashboard" link
(the onboarding wizard's profile panel shows the handle it will generate from
your name, read-only). It checks
availability live as you type and shows exactly what will be saved. **A
handle you type that is already taken is refused** with the next free one
suggested — you are never silently given a variant of the identity you
chose. Leave the field empty instead and one is generated automatically
from the name (or the part of the email before the `@`); auto-generated
handles quietly get a number on a clash, since nobody chose them. An
account created with no name at all gets a presentable one derived from
its handle (`dana-ray` → `Dana Ray`). A handle is not a login: people
still sign in with their email address and password.
