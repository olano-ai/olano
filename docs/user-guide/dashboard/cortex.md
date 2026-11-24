# Cortex

Olano's autonomous cognition engine. When Cortex is on, the agent does useful
background work between conversations: distilling what it learned into memory,
tidying its own notes, expanding ideas, and generating proactive suggestions.
This tab shows what it has been doing and what is scheduled. It is off until
you turn it on: the **Cortex engine** switch is inside **Cortex settings**
below, for the whole deployment or for one agent (see
[Cortex and Pulse](../feature-guides/cortex-and-pulse.md)).

Everything you can change about it sits in one card, **Cortex settings**, at the
top of the tab. It is folded away by default — the line beside its title is the
live state (on or off, the pace, and today's deep-run count), so you only open it
when you want to change something. Inside are three parts: **How it runs**, the
switches; **Phases & schedule**, what runs when; and **Creative direction**, what
the agent should aim at when it thinks on its own. Fleet-wide defaults live in
Config; per-agent overrides live here and in the agent's own settings
(see [Cortex and Pulse](../feature-guides/cortex-and-pulse.md)).

The **Brainstorming**, **Ideas**, **Prototypes** and **Projects** tabs are the
four stages of one funnel (see [the funnel](../feature-guides/cortex-and-pulse.md#the-funnel-brainstorms-ideas-prototypes-projects)).
A card that the **Rate & Promote** phase has reviewed carries its score as a
badge (`7/10`; hover for the recommendation and the reason), and the drawer
shows the reason and the suggested next step under the status line. **Create
project** on a card jumps the item straight to a project; the agent's own
`cortex_promote` moves it one stage at a time. A promoted card shows a link to
the folder it became. **Open** on a card (or clicking the card) shows the
item's full write-up in a drawer, and the drawer carries the card's own
buttons too: **Create project**, **Discard**, **Explore more** and **Chat**,
with the same confirmation and the same busy state, so you can act on an
item as you read it. Discarding from the drawer closes it; Create project
hands over to the run's panel. The **Phases & schedule** list has a row for Rate &
Promote like every other phase, with **Run now** to rate on demand.

**Every button on a card answers the click itself.** While its request is
out, the button you pressed turns into a spinner with what it is doing
(**Discarding…**, **Starting…**, **Approving…**) and the card waits dimmed, so
a second click cannot start a second run; the moment the server agrees the
card leaves the list (a discarded item to **Discarded**, an answered Pulse
card out of **Pending**) and a short notice confirms it. Approving a Pulse
card also puts an outcome card at the top of the tab that says exactly what
happened and links to the file it changed; the page scrolls to it. A Pulse
card about work the agent already applied on its own offers **Keep** and
**Undo** rather than Approve and Reject: Keep accepts the change, Undo
restores the backup the agent took first.

**Everything Cortex does in the background keeps running when you look away.**
**Create project**, **Think now**, **Optimize workspace**, **Optimize skills**
and a phase's **Run now** each start a run on the server and open a panel that
follows it: what it is doing right now, how long it has been going, and every
step it has taken (each tool it called, what came back, what the model said).
Closing that panel, leaving the page, locking your phone or reloading does not
stop the run. While a project is being created its card reads **Creating
project…** with the step it is on, and its button opens the same run again
rather than starting a second one; a phase that is running shows **Running**
in its row for the same reason. Pressing a button for something already
running (from another tab or another device) always joins that run. When it
ends you get a short notice, the card moves on (a promoted idea goes to
**Promoted**, the new project to **Projects**), and a run that failed within
the last hour leaves a **Last attempt failed** line on its card with a **View
log** link. **Stop** in the panel ends a run early.

**Runs**, next to **Refresh** at the top, lists every run for every agent you
can see: what is running now (the button shows a count while anything is) and
the history before it, including the phases Cortex runs on its own schedule
and the tasks you approved in Pulse. **All**, **Running** and **Failed**
narrow the list, the agents filter beside the tabs narrows it too, and
opening a row shows that run's full step log. A run that was going when the
service restarted shows as **Interrupted**. The most recent 200 runs per agent
are kept.

**Two things on this page have a different scope, on purpose.** The buttons
along the top — **Think now**, **Optimize workspace**, **Optimize skills**,
**Activity** — and the settings card act on **one agent**: the one named under
the title, which you change with the agent dropdown in the page's top bar (the
same dropdown Notes and Memory carry). The tabs below — **Pulse**,
**Brainstorming**, **Ideas**, **Prototypes**, **Projects**, **Learned
Skills**, **Self-Improve**, **Self-Checks** — show **every agent's items at
once**, so reviewing what the fleet has come up with no longer means switching
the dropdown once per agent. Every card carries a small label with the agent's
face and name it belongs to, and the buttons on that card act on *that* agent
(a **Chat** switches you to it). The **agents filter** beside the tabs — a
dropdown with a checkbox per agent, a search box, **Select all** and
**Clear** — narrows the tabs to the agents you tick; the line next to it says
what is showing ("Showing items from all agents", "from 2 of 8 agents"). Every
agent is shown by default, an agent created later is shown too, and the choice
is remembered on this device. The counts on the tabs follow the filter.
