# Common problems

**My agent replies with an empty message, or stops mid-sentence.**
Check **Multi-user → Message policy → Maximum reply length**. A reply that hits
that ceiling is cut where it hits it and is deliberately not continued. If it
comes back *empty*, the model is a reasoning one and spent the whole ceiling
thinking: raise the number above about 1024, or set **Reasoning effort** to
`none` or `low` on the Overview tab.

**My agent went silent for a real customer.**
Something in **Multi-user → Message policy** matched their message. In this
dashboard an ignored message always shows a short note saying so, so send the
same text here to see which case it was. A pattern is matched anywhere in the
message and ignores case, so a broad one like `test` catches "is this the
latest version?" — anchor it with `^…$` if you meant the whole message. If it
was a silence *rule* instead, remember the agent is judging it, so an
ambiguous rule produces ambiguous behaviour; make it a statement that is
plainly true or false about one message. Setting **What an ignored sender
sees** turns silence into one polite line, which is usually right when a real
customer can act on the rule and wrong when the sender is a spammer.

**Long messages to my own agent get refused.**
The message-length cap applies on the dashboard too, not only on messengers —
deliberately, so that a cap you set is one you can see working. Raise or clear
**Maximum message length**, or paste the long text into a file and point the
agent at it. Slash commands are never screened, so `/` commands keep working
whatever the cap is.

**The agent introduces itself by the template's name, not the name I gave it.**
An agent created before titles existed may carry the archetype's label as its
**Display name** ("Recruitment Specialist") with your name only as its handle
(`hillary`) — and its prompt tells it it is *called* Recruitment Specialist.
Open the agent, set **Display name** to the name you want and **Title** to the
role (both on the Overview tab; the title also lives on the Talk to Agent
tab); the next conversation introduces itself as "Hillary, Recruitment
Specialist". Agents created now get this right from the start: the name you
type is the display name and the template's label becomes the title.

**No "New agent" button in the simple view.**
Admins always have it. For a member it means the deployment turned member bot
creation off: an admin can switch it back on under **Config → Member bot
creation** (it is on by default). A member who has the button but gets a
"limit reached" message owns as many agents as the per-member limit allows —
the same Config section raises it.

**Can't find Sign out in the simple view.**
It is in the account menu — click your name at the bottom of the agent list;
Sign out is the last item, always visible at the menu's foot. It only appears
on team installs (with per-person sign-in); a single-user box has no session
to sign out of. Theme and language are one dropdown each, so the menu is short
enough that nothing in it scrolls; on older builds a tall menu could hide Sign
out below the language list, and with the list collapsed to a strip of faces the
menu itself was cut off at the strip's edge — update if you see either.

**An agent never replies on Telegram/Slack/Discord.**
Almost always a missing or shared credential. A connector with no credential is
skipped at boot and raises a dashboard notification naming the exact key to set.
If two agents use the same platform, each needs its **own** token stored as a
per-agent override — otherwise they authenticate as the same bot and one stops
receiving messages. Set the token, then restart the agent.

**The agent answered "Sorry, I had trouble processing that one — could you send
your message again?"**
The connection to the model broke part-way through the reply. It is a network
blip, not a problem with your deployment, your keys or your credit balance, and
nothing needs changing: send the same message again and it almost always goes
straight through. There is no need to start a new conversation — the thread is
intact and the agent still has the context.

Olano-managed models retry a dropped connection themselves before the agent says
anything, so seeing this message means either the retries were used up or the
answer had already begun arriving (a half-delivered reply is never re-sent — you
would get the second half of one answer glued to the first half of another).
Models you connected with your own API key have no such retry, so they show it
more often. If it happens on *every* message rather than occasionally, the model
provider itself is likely having an outage — check its status on the
**Providers** page.

**A command-line tool in the sandbox says it is not logged in (`gh`, `gog`, …).**
Expected, and not something to fix: the sandbox holds no credential, and Olano
never signs a command-line tool in there ([Sandbox tool bundles](config-yaml.md#sandbox-tool-bundles)).
Use the built-in integration instead — the GitHub tools for repositories,
issues and pull requests, the `git` toolkit to clone or push a private
repository — after connecting GitHub once in **Connections** (Connect, or Sign
in with a code) or by asking the agent for the link. If you really want the
tool itself signed in, do it yourself from the **Terminal** tab; that sign-in
is yours alone and lasts until the sandbox is rebuilt.

**The GitHub CLI card is gone from Connections.**
It merged into the **GitHub** card, which now offers both **Connect** (an
OAuth app — Olano's or your own) and **Sign in with a code** (no app needed),
at the System scope and per agent. A connection made either way serves the
GitHub tools, GitHub MCP servers and the `git` toolkit alike — it never signed
anything in inside the sandbox, and it still does not.

**An agent is refused with "the monthly budget for … is exhausted".**
It hit one of the six usage budgets — see
[`usage_budgets:`](config-yaml.md#usage_budgets-token-ceilings-and-budget-warnings). Open
**Config → Usage budgets** to see which one and how full it is. A *token*
budget is yours to raise or lift there, and the change applies to the next
turn; a *cost* budget comes from your plan and resets with its window. Note
token budgets count calls made on your own provider keys too, so an agent on
its own API key can still be stopped by one. Notifications at 50%, 60%, 70%,
80%, 90%, 95% and 99% warn you long before this happens — if this was the first
you heard of it, check that budget notifications are on.

**The agent says it cannot message someone by name.**
Agents address a chat or a person by the platform's own id. A name or handle
works too, but only for chats and people whose messages have already reached
*that* agent — each agent keeps its own directory of who it has heard from, so
someone well known to one agent is unknown to another until they message it as
well. On Telegram and Discord, where a chat id is always a number, a name the
agent does not recognise now fails immediately and says what to do about it,
rather than being sent and bouncing back as an unreadable error. Either have the
person message the bot once, or give the agent the numeric id to use.

**A reply says the model "could not read part of this conversation's history".**
Change a bot's model and the conversation carries on where it left off — the
thread keeps everything the previous model wrote. A few models cannot read
certain things an earlier model left behind (its private reasoning, an image
attached in a shape it does not accept), and when that happens the whole thread
stops working for that model rather than just one turn: retrying it fails the
same way every time. Start a new chat and the bot works normally again; the old
conversation stays readable, and switching that bot back to its previous model
also restores it. If you would rather not lose the thread, switch the model
back, ask the bot to summarise where things stand, and paste that into the new
chat. This is rare, and mostly affects locally hosted models.

**An agent is offline and will not start.**
Check the **Logs** page (account menu → Administration → Logs) for that agent.
The usual causes: an invalid `agent.json5`
(see [`agent.json5` reference](agent-json5.md#what-makes-a-config-fail-to-load) for what gets rejected), a
model string that names a provider with no usable key, or the agent having been
deliberately stopped — a stopped agent stays offline across restarts until you
start it again.

**An MCP server shows as disconnected.**
Look at the **MCP** tab for the reason. If it says a sign-in is required, you
have to authorize it — Olano will not retry a server that needs consent it does
not have, because reconnecting cannot invent consent. Timeouts and errors are
retried automatically with backoff; an explicit reload from the tab always wins
over the retry schedule.

**Approvals are piling up / the agent keeps asking permission.**
Its `security.profile` is stricter than the work needs, or `hitl_shell` /
`hitl_write_tools` are on. Move it to a more permissive profile, or override the
one noisy tool with `security.tools.<name>.approval: "never"`. Note that running
commands in another agent's workspace is always approved individually and cannot
be switched off.

**The agent gave away or acted on something it should not have.**
Check `security.pii_action` (default `warn` only warns — set `redact` or
`block`), `output_scan`, and `credential_scrub`. If the agent reads untrusted
content like email or web pages, make sure `input_scan` is on and consider
`web_scan`.

**A dashboard tab is missing.**
Four are admin-only: Vault, Logs, Config, Admin. On a managed deployment, other
tabs can be hidden by your plan's entitlements. If you are an admin and a
section you expect is gone, that is why.

**A Cortex Think phase says it "hit its step limit" (or, on older builds, shows
"Recursion limit of N reached").**
The background run needed more rounds of thinking and tool use than its step
budget allows, so it stopped before finishing and its work for that run was
discarded. Run the phase again — a retry often plans more tightly. If it keeps
happening, raise the **Step budget** in **Cortex → Autonomy**: the "All agents"
column raises it for the whole deployment, the per-agent column for just this
one (each unit of ~4 steps is roughly one round of thinking plus tool use).
Raise the **Time limit** beside it at the same time — a bigger step budget
behind the old time limit just fails on time instead. Note that a phase
reporting "No active items to advance" or "No fresh idea to build" is a normal
no-op, not a failure — the generative phases (Brainstorming, Ideas) have to
succeed first to give the later phases material to work on.

**A background run failed and I only found out by opening Cortex.**
You should not have to go looking. A background run that fails, times out or
needs setup now raises a notification — the bell in the sidebar and the
Notifications section on Overview — naming the agent, which run failed and what
to do about it, with a link straight to Cortex. Repeats of the same failure
collapse into one row with a count rather than filling the list, and the row
clears itself the next time that run succeeds. Runs stopped by a guard rail you
already configured (over the daily budget, or nested too deep) do not notify:
they are working as intended and clear on their own.

**An agent says it can't do something its MCP server should handle.**
Usually the connection to that server is down, and the most common reason is an
OAuth grant that can no longer be renewed — the provider expired or revoked it.
Olano retries everything it can on its own: it renews access tokens ahead of
expiry, re-discovers a provider's token endpoint if the one it had stopped
answering, and reconnects servers that failed with exponential backoff. What it
cannot do is give consent on your behalf.

When a grant reaches that point you get a notification — the bell in the
sidebar and the Notifications section on Overview — saying which server needs
re-authorizing for which agent, with a link straight to that agent's **MCP**
tab. Open it and click **Authorize**. Reloading the server will not fix this one
(the notification says so); only a fresh authorization will. The row clears
itself as soon as the server connects again, and there is one row per agent per
server, since each agent authorizes its servers independently. If you no longer
need that server, switching it off or removing it on the **MCP** tab clears the
row too, within a couple of minutes at most, and deleting the agent clears
every notification about it.

To check the state of every server without waiting for a notification, use
**Config → MCP connection health**, the agent's own MCP tab, or `/mcps` in chat.

**"Skill X does not follow Agent Skills specification: name must match
directory name".**
A skill's `name:` in its `SKILL.md` differs from the folder containing it. The
skill still loads and works, but the warning repeats on every agent start and a
future update may stop loading it.

The usual cause was an agent writing a skill with a title as its name
(`Professional HTML Email Formatting` in a folder called
`professional-html-email-formatting`). That no longer sticks: when an agent
writes a skill, a title in `name:` is set to the folder's name on the spot, and
every agent start and every background run does the same for skills written any
other way. A skill switched off under its old name stays switched off.

What is left gets a notification listing the affected skills: a valid name in
a folder called something else. **Repair** (on the notification in the mobile
app, or AgentFather's `olano_repair_skill_names`) renames each **folder** to
match the skill's name, since the name is what the agent refers to. Repair
leaves two cases alone and says so: a folder of that name already exists, or
the skill is an agent's improved copy of another skill.

**The agent says there are no Cortex items, but the folders are still there.**
The Cortex tab and the `cortex_*` tools read one index file per agent. If that
file was damaged by two edits landing at the same moment (an older release could
do that when the agent edited several items in one reply), it now repairs itself
on the next read: the items reappear, and a copy of the damaged file is kept
beside it with a `.corrupt-<time>` suffix that you can delete once things look
right. If a tool answers that the index is **unreadable**, the file could not be
repaired automatically; nothing has been changed or written over it. Open the
agent's **Files** view, find `.olano/state/cortex/items.json`, and either fix the
JSON or move the file aside; the item folders (`ideas/`, `prototypes/`,
`projects/`, …) are untouched and the agent can re-add the items from them.

**Out of credits, or usage higher than expected.**
Check **Insights** — the **"Where credits went"** card splits the window by
kind of work (chat, Cortex background thinking, scheduled tasks, highlights,
subagents…), **"Credits by tier"** shows which managed tier is spending, and
the by-agent and by-model charts narrow it further. The usual cause is an
agent on a higher tier than its work needs, or Cortex running at `aggressive`
intensity across many agents. Move routine agents to `olano:turbo`, lower
`cognition.intensity`, reduce `max_deep_runs_per_day`, and set per-agent
`budget` caps. Buy more credits from Olano Cloud (on a self-hosted install
running on an Olano API key, under Billing → Account credits).

**An MCP sign-in on a self-hosted install says there is nowhere to return to.**
The provider needs a secure address to send you back to after you sign in. Paste
an Olano API key on **Providers → Olano** (a few seconds later the box is
registered with Olano's sign-in service and the sign-in works with no domain of
its own), or give the box a public https address. If a key is already there and
sign-ins still fail, the key may have been revoked on `cloud.olano.ai`, or the
account may already have five self-hosted installs using the sign-in service;
an install unused for 30 days frees its place on its own, and revoking an API
key frees the installs that registered with it
([MCP servers](feature-guides/mcp-servers.md)).

**Olano models stopped working on a self-hosted install.**
Open **Providers → Olano**. If the card says the key was refused, it was
revoked on `cloud.olano.ai` or pasted incompletely: create a new key under API
keys and paste it whole. If it says the account is out of credits, buy more on
the Billing page. If the card shows no form at all, the box resolved as a
managed deployment, which bills its own credits (see
[Models and credits](models-and-credits.md#olano-credits-on-a-self-hosted-install)).

Also note the two surfaces measure different windows by design: the deployment
card at cloud.olano.ai shows the **current billing period** against your
plan's monthly allowance (including the daily snapshot-storage charge, listed
in the per-service view), while Insights charts the last-N-days window you
selected, measured on the deployment itself. They agree on what was metered;
they are not the same time span.

**Credits charts jumped after an upgrade.**
Expected once: the first start after upgrading reconstructs historical Cortex
background spend into the local record, so days that previously showed only
chat now show the full picture. Nothing was re-billed — your balance is
unchanged; only the charts got more complete.

**The agent remembers something wrong.**
Edit `MEMORY.md` in the **Memory** or **Files** tab. It takes effect on the
agent's next reply — no restart needed.

**Disk is filling up.**
Expand the **Overview** tab's disk card. **Where the space went** names every
group on the machine, so you can tell immediately whether the space is your
data or the platform itself; **Largest directories in the Olano home** then
drills into your data. Automatic cleanup runs on its own at the threshold in
`disk_monitor:`, and **Clean now** runs it immediately — it reclaims superseded
container images, caches and old logs. AgentFather reports the same with
`olano_disk_report`. If a specific agent's workspace is the problem,
its `trash/` and `scratch/` folders are safe to empty.

**The disk says 60% used but the directory list only adds up to a few hundred
megabytes.**
Those two numbers measure different things and both are right. The percentage is
the whole machine; the directory list is only the Olano home. The gap is the
container images, the swap file and the operating system — all of it named in
**Where the space went** at the top of the same card.

**A config change did not take effect.**
Most agent settings need that agent restarted; the dashboard shows when a
restart is pending. The ports and team-accounts settings need the whole runtime
restarted. Identity and instruction files (`AGENTS.md`, `SOUL.md`,
`IDENTITY.md`, `TOOLS.md`) are picked up without a restart, within a couple of
minutes.

**A number I set in Config came back different.**
`config.yaml` values are clamped to their valid range rather than rejected, and
an unparseable value falls back to the default silently. The value the dashboard
shows after saving is the one in effect.

**The Disk encryption card is missing from Config.**
The card exists only on a managed instance whose data lives on an encrypted
volume ([Privacy and security](feature-guides/privacy-and-security.md)). A self-hosted install never shows it,
and neither does a managed instance created before encrypted volumes existed;
the cloud console's Security tab shows the custody of each instance and marks
such an instance as legacy. Nothing on the instance can turn the card on:
encryption is decided when the instance is created.

**The Disk encryption card says the encryption service is Unavailable.**
The service on the instance that performs the recovery-key actions is not
answering. Your data stays encrypted and the instance keeps working; only the
buttons on the card are paused. Press **Refresh** after a minute. If it stays
unavailable, restart the instance from the cloud console (the volume unlocks
again by itself at boot), and if that does not clear it, contact support.

**A recovery key is stuck at "Pending confirmation", or I lost my recovery key.**
A key that was shown and never confirmed stays pending for fifteen minutes and
then expires by itself; **Cancel pending key** removes it at once, and no other
key can be created while one is pending. A lost key cannot be shown again by
anyone, Olano included: while the instance runs, press **Replace recovery
key**, store the new key, and confirm it; the lost one stops opening the volume
the moment the new one is confirmed. Without any recovery key the volume can
still be opened, by Olano's key service alone.

**Nothing shows up in Langfuse.**
Open Config → Observability. The keys line must not say *No keys yet*, the
switch must be on (or the agent's own Observability card must say *On*), and the
agent card's *In effect now* line must say *traced*. Press **Test connection**:
a refusal from Langfuse Cloud almost always means the keys belong to another
region, so set the Langfuse address to that region (`https://cloud.langfuse.com`,
`https://us.cloud.langfuse.com` or `https://jp.cloud.langfuse.com`). The Status
line shows the last problem sending traces, if any.

**Langfuse traces show `withheld_chars` instead of the conversation.**
That is the default capture mode, *Metadata only*: traces carry the structure,
timings, tokens and names but no text. An admin can choose *Full content* in
Config → Observability, or for one agent on its Observability card.

**Some turns are missing from Langfuse.**
Check the *Share of turns traced* (Config, or the agent's card): below 1, only
that share of turns is sent. Cortex background runs are skipped when *Trace
background work* is off.

**There is no Langfuse link under a reply.**
The link needs an admin account with **Langfuse** links switched on in Admin →
Users (off by default), a traced reply, and a project the dashboard knows: press **Test connection** once in Config → Observability (or after
saving an agent's own keys) so the project is remembered.

**A reply has no thumbs up or down.**
Replies recorded before reply rating existed have none; newer replies in the
same conversation do. Thumbs also appear only once a reply has finished.

**A thumbs-down comment is not in Langfuse.**
Comments are sent only when traces include full content. With the default,
*Metadata only*, Langfuse gets the score (-1) and the comment stays on the
deployment. A vote reaches Langfuse only if that reply was traced and tracing is
still on when the vote is cast.

**A scheduled task did not run this morning, or ran late.**
Open **Scheduler**, the task, then **History**, and read the newest row. *Skipped:
the previous run was still executing* means the earlier run of the same task was
still going when the next one was due: open the task's recent conversations in
the chat list to see what it was doing, shorten the work or split the task, and
know that a run is stopped after `scheduler.max_run_minutes` (three hours by
default) so the job frees itself. *Skipped: firing was missed by more than the grace period* means the
runtime was busy or restarting for longer than `scheduler.misfire_grace_minutes`
allows (six hours by default); within that window a late firing runs late, and
its message says so. No row at all for the expected time means the task was not
registered in this process: saving the task again registers it, and the bell
carries a notice when the scheduler could not read its task list at startup.
Both kinds of problem raise a notification on the bell that links to the task,
and the notice clears on the task's next completed run. An agent that decided it
was the wrong day for its work was missing the date: every run's message now
opens with the current date and weekday in the task's timezone, so re-run the
task and read its first lines in the run's conversation.
