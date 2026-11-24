# The mobile app

The Olano app puts your agents on your phone: conversations with them, what
Cortex is working on, the approvals and alerts waiting for you, and
notifications when an agent needs you. It runs on iPhone, iPad and Android
phones and tablets, and on Apple silicon Macs as the iPad app, and comes from
the App Store and Google Play.

The app is built for the phone: a tab bar along the bottom, a back button and
the swipe back gesture on every screen, pull down to refresh, and the look of
the platform it runs on (iOS on an iPhone, Material on Android), in light or
dark (see [Appearance](#appearance)). It talks to your own deployment, with the
same agents, conversations and permissions as the dashboard. Nothing about your
agents is stored by Olano on the way.

## Connecting

The first screen asks for **your Olano address**: the address you open your
dashboard at in a browser, such as `acme.olano.run`. A bare name like `acme`
means `acme.olano.run`; a full address (`https://agents.example.com`) works for
a deployment on its own domain. The app checks that the address answers as an
Olano deployment before it continues, and says so if it does not (no such
address, a website that is not Olano, no network).

You then sign in with the email and password you use for the dashboard. The
sign-in is kept on the phone for two weeks of inactivity, so you sign in again
only when it expires or you sign out. A deployment with no accounts yet asks
you to finish setting it up in its dashboard first. A single-user deployment
(no accounts at all) opens straight away.

The app remembers the last few addresses you opened, so someone with more than
one deployment switches between them with one tap. **Settings, Switch
deployment** goes back to that screen.

### Trying the app without a deployment

Under the address field, **Try the demo** opens a demo deployment Olano runs,
with sample agents, already signed in: no address, email or password to type.
Each visitor gets a demo account of their own, and no other visitor sees your
conversations, but the agents themselves are shared with other visitors, so
type nothing private there. Your demo account and its conversations are
deleted after a day; after that, **Enter the demo again** on the sign-in
screen opens a fresh one. **Settings** says when you are in the demo, and
**Switch deployment** takes you back to connect your own. Olano may remove the
demo entry at any time; it never changes anything about your own deployment.

If the deployment cannot be reached when the app opens, it shows **Can't
connect** with **Try again** and **Use a different deployment**. The usual
causes are no connection, or the deployment restarting or stopped.

## The four tabs

Your administrator may have hidden some of what is described here; see
[What your administrator chooses](#what-your-administrator-chooses).

**Chats** lists the agents you have access to, each with its latest
conversation. A green dot means the agent is running. The list is arranged the
way the dashboard's sidebar is: **Pinned** at the top, then your own folders,
then **All agents**, then [group chats](#group-chats). Tap a section's name to
fold it shut. To arrange it:

- **Pin** an agent by swiping its row to the right, or press and hold it and
  choose **Pin**. Pinned agents stay at the top in the order you pinned them.
- **Folders**: the **+** button at the top makes a new folder. Move an agent
  into one by swiping its row to the left (**Move**), or press and hold it and
  choose **Move to**. An agent is in one place at a time: pinned, in one
  folder, or under All agents. The **...** button beside a folder's name
  renames or deletes it; deleting a folder moves its agents back under All
  agents.
- **Move Up** and **Move Down** (press and hold an agent) change the order
  within a section.

The arrangement is yours: it is kept on the deployment for your account, so it
follows you to another phone or a reinstall, and it is separate from how the
dashboard's sidebar is arranged in your browser. Searching shows matches in
every section, folded or not.

Tap an agent to go straight back to the last conversation you had with it,
like the dashboard's chat does: one you had in the app, the dashboard or a
messenger. Scheduled runs, conversations with other agents, Cortex threads and
calls from other systems never open this way. If you have no conversation with
the agent yet, a new one opens. The agent's row shows that conversation's
title.

Every other thread is one step away: the **...** button at the top of a
conversation, **All Conversations**. That list has every thread the agent
has, newest first, twenty at a time: scrolling down loads the next twenty, so
an agent with months of history opens as fast as a new one. The search bar at
the top looks through the messages of the agent's conversations on the
deployment, not just their titles, and lists the ones that mention the words
with a line showing where; **Search older conversations** keeps looking
further back when the history is long. The pencil button starts a new
conversation. Each thread carries
the same coloured badge the dashboard shows for where it came from: **Mobile**
for a conversation had in this app, **Dashboard** for one in the dashboard,
the messenger it arrived on (**Telegram**, **WhatsApp**, **Email**...), or how
it started (**Scheduled**, **Agent** for a conversation between two agents,
**Cortex**, **External** for a call from another system). Chips above the list
show one source at a time (**All**, **Dashboard**, **Mobile**, each messenger,
**Scheduled**, **Agents**), with how many threads each holds; the app remembers
your choice for each agent. Swipe a thread to rename it, or to delete it if you
are an administrator of that agent.

In a conversation, the agent's reply streams in as it is written. What the
agent did on the way (searched the web, read a file, asked another agent)
shows as one line per step while it works and folds into "N steps" when it is
done; tap that line to see the steps. A plan the agent made shows as a
checklist. While the agent works and the message box is empty, the send button
becomes a stop button; see [While the agent works](#while-the-agent-works) for
adding to what it is doing. If the agent needs your approval before an action,
a card with **Approve** and **Decline** appears in the conversation.

The message box is one card at the bottom of the screen: what you type on top,
and under it **+**, the conversation's model, the microphone and the send
button. On a phone the keyboard's return key starts a new line, and only the
send button sends, so a message never goes out half written. With a hardware
keyboard (an iPad's, or a keyboard paired with the phone) Enter sends and
Shift+Enter starts a new line. **+** sends photos and files, and images the
agent produces show in the conversation (see [Photos](#photos) and
[Files](#files-and-sharing-into-olano)). The microphone beside the send button
dictates (tap it: your words appear in the box as you speak, see
[Dictation](#dictation)) or records a voice note (hold it, see
[Voice notes](#voice-notes)). On a phone that cannot dictate, the microphone
shows while the message box is empty and only records voice notes.

Other files the agent hands you (a report it saved, a data file, a PDF) appear
as cards under its reply; tap one to open it in the app. Markdown documents
open formatted, data and code files (JSON, CSV, logs, scripts) open as text,
and a PDF opens as a document on an iPhone. Files the app cannot show itself,
such as Word or Excel documents, and PDFs on Android, open with **Open in
another app**, which offers the phone's own choices (save to Files, mail it,
open it in Word). The share button in the corner does the same for any file.

When an agent asks you for an API key or a model provider's key, the form is
drawn under its reply: paste the value and press **Save to vault**. It goes to
the vault without becoming a message, and the agent is told the name once it
is saved. When an agent needs you to sign in to a service, connect a
messenger or edit a file, it sends a **magic link**: after you approve it, a
card appears under its reply with **Open** and, usually, a six-digit PIN.
**Open** copies the PIN and opens a secure page of your deployment in a
browser over the app; paste the PIN there and follow the page. That page needs
no sign-in of its own (the link and its PIN are the key), and the link expires
after a while (fifteen minutes unless your deployment says otherwise). When
you finish, the conversation shows **Connected** (or **Didn't complete**), and
offers to restart the agent when the change needs one and you are an
administrator. See [Magic links](ask-the-agent.md#magic-links-the-default-whenever-a-browser-is-needed).

A new conversation offers a few ways to start, such as "What can you help me
with?"; tap one to send it. Messages are grouped under the day they were sent
(Today, Yesterday, then the date). A long conversation opens on its newest
messages and fetches earlier ones as you scroll up, a little ahead of where
you are, so they are usually in place by the time you get there; **Start of
conversation** marks the first message. If they could not be fetched (no
signal), a line at the top says so and a tap tries again. If you have scrolled
up to read earlier messages, a round button with a down arrow takes you back
to the latest one, with a dot on it when something new arrived.

Next to **+** in the message box is the model the conversation runs on, with
its reasoning level in grey when the model has one (for example "Olano Pro
High"); the name turns orange when this conversation uses a model of its own
choosing. Tap it to pick another model, or how hard the model thinks (Low,
Medium, High and so on, as that model offers them; **Default** leaves it to the
model). The choice applies to that conversation only, the same as the model
picker in the dashboard's chat: every message you send in it uses that model,
reopening the conversation continues on it, and a new conversation starts on
the agent's own model again. The list offers the models your deployment can
run: its default models, the Olano models, and those of any provider an
administrator has added a key for. **Agent default** goes back to the agent's
own model. If a conversation last used a model the deployment no longer
offers, it continues on the agent's model and the app says so.

Web addresses in a conversation are links, in the agent's replies and in your
own messages, including an address in code formatting and one written as
`www.example.com`. Tap one to open it in a browser over the app; press and hold
one to **Copy Link** or **Share Link**. Anything in a reply that looks like web
page markup is shown as text.

Under each finished reply are buttons to **copy** it, **hear** it (see
[Hearing replies](#hearing-replies)) and **share** it to another app. Under your latest message are **copy** and **edit**: edit puts
your words back in the message box to correct, and sending them sends a new
message (the conversation's history is not rewritten, so the agent sees both).
When a reply fails, **Try again** under it sends your message again. Pressing
and holding any message offers the same choices, plus copying a voice note's
transcript.

To flag a reply that is offensive, harmful or wrong, press and hold it and
choose **Report**, then a reason. The report is recorded on the deployment as
negative feedback on that reply, with the reason kept beside it, and counted
for the administrators in the dashboard's Insights; nothing leaves the
deployment.

A conversation keeps going on your deployment if you leave it, lock the phone
or switch apps: come back and the app picks up where the reply has got to.

**Cortex** is what the agents have been working on by themselves. At the top,
the **Pulse** card says how many findings the agents posted for you, the same
news, research and ideas as the dashboard's Cortex Pulse; tap it for the cards. On each, **Do it** carries out the suggested next step, **Later**
brings the card back in a day, **Dismiss** drops it. A card about work the
agent already did on its own offers **Keep** and **Undo** instead: Keep
accepts it, Undo restores the backup the agent took first. Later and Dismiss
take the card away the moment you tap; Do it, Keep and Undo spin on the
button while the agent answers, then the card goes and a short notice, above
the tab bar, says what happened (when the step is a longer piece of work, the agent runs it in
the background and posts the result here when it is done).

Under it, the four stages in the order things move through them:
**Brainstorms**, **Ideas**, **Prototypes** and **Projects**, each with how
many items it holds. The list shows every stage; tap a stage to show only
that one, and tap it again (or **Show all**) to see everything. Chips narrow
the list to one agent when more than one has items. An item this phone has
not opened yet is lightly tinted; opening it clears the tint.

Triage happens on the list. Swipe a row to the right to **approve** it (move
it to the next stage) and to the left to **discard** it: the word shows as the
row slides, a full swipe does it, and a shorter swipe leaves the button there
to tap. An approved item moves to the next stage straight away, marked as on
its way while the agent writes it up; a discarded one leaves the list. To
discard several at once, tap **Select**, tap the rows (or **Select all**),
then **Discard** in the bar at the bottom and confirm.

Open an item for its write-up and Cortex's rating, then **Make it a** (the
next stage) to move it forward, **Talk it over** to discuss it with the agent,
or **Discard**. Moving an item forward is a piece of writing the agent does,
and it takes a minute or two: the button turns into **Making it an idea…**
as soon as you tap, then a banner under the title follows the run (with the
step the agent is on, when it reports one) and you can leave the page, since
the list already shows the item under its new stage. When it is done the
banner reads **Now an idea** with a button that opens the new item's page;
if the run failed, the banner says why and the button offers the tap again.
The first time, **Talk it over** opens a conversation with the
item already described in the message box (what it is, its title and where
its files are), so you only add your thoughts or questions after it; nothing
is sent until you send it. Later it reopens that conversation where you left
it. See [Cortex and Pulse](cortex-and-pulse.md).

**Inbox** has three parts:

- **Approvals**: actions agents are waiting to take until someone approves
  them, from a scheduled run, Cortex or a request from another agent, and Pulse
  cards that need an approval. Answer each with **Approve** or **Decline**.
- **Alerts**: the deployment's notifications, the same list as the bell in the
  dashboard. The line under an alert says what tapping it does; swipe one to
  dismiss it. See [Alerts](#alerts).
- **Issues**: the issue tracker, every issue you can see, not only those an
  alert pointed at. **Open** (the default), **Assigned to me** or **All**,
  closed ones included; tap one to read it, comment, hand it to an agent or
  close it (see [Alerts](#alerts) for what an issue screen offers).

The agents' Pulse findings are in the Cortex tab.

The tab's badge counts approvals waiting plus unread alerts.

**Settings** shows who you are signed in as and where, the agents'
[scheduled tasks](#scheduled-tasks), the app's [appearance](#appearance),
[voice](#hearing-replies), the phone's notifications, **Switch deployment** and
**Sign out**. What the app does not do (agent configuration, tools, files,
connections and the rest) stays in the dashboard, in a browser.

The app grants and removes no permission: a member sees the agents they have
access to, an administrator sees all of them, exactly as in a browser.

## While the agent works

You do not have to wait for a reply to finish before saying more. While the
agent is working, type in the message box and a bar above it says the agent is
working:

- **Send** (the arrow) hands your message to the agent while it works. It reads
  it at its next step, after the action it is in the middle of, and carries on
  with it in mind; nothing is stopped or started over. Your message appears in
  the conversation where the agent read it, marked **Added while it worked**.
- **Send after** keeps the message until the reply is done, then sends it as
  your next message.
- **Stop** stops the agent.

Messages waiting to be read show above the message box. **Edit** takes one
back into the message box to change it, the cross cancels it; either works
until the agent has read it. A message sent with the arrow just as the reply
finishes goes out as your next message instead, so nothing is lost.

## The conversation menu

The **...** button at the top of a conversation opens a sheet that names the
agent and its model at the top, then groups what you can do. Rows with an arrow
open another screen; the others act right away or ask one more question.

**Conversation**

- **New Conversation** starts a new one with the same agent.
- **Mode** is how the agent goes about your requests in this conversation; the
  current one shows on the right. **Auto** (the default) does the work, running
  tools without asking. **Plan first** has the agent write a plan and wait:
  under the plan are **Approve & run**, which tells it to carry the plan out,
  and the message box, to ask for changes instead. **Ask before each step**
  asks for your approval before every tool the agent runs. A bar above the
  message box shows the setting when it is not Auto. The app remembers the
  choice for each agent on this phone; approving a plan goes back to Auto.
- **Rename** gives the conversation a name of your own.
- **Compact** replaces the conversation's older messages with a summary the
  agent keeps, so a long conversation takes less of the agent's memory. It is
  the same as `/compact` in the dashboard's chat.

**Agent**

- **All Conversations** lists this agent's conversations.
- **Files** opens the [agent's files](#an-agents-files).
- **Scheduled Tasks** lists this agent's [scheduled tasks](#scheduled-tasks).
- **Agent Info** opens the [agent's page](#an-agents-page). Tapping the agent's
  picture at the top of the conversation opens it too.

**More**

- **Hands-Free Mode** lets you talk with the agent without touching the phone
  (see [Hands-free](#hands-free)).
- **Add an Agent** turns the conversation into a [group chat](#group-chats)
  with a second agent you pick. The conversation so far is copied into the
  group, so the new agent can read it, and the agent you were talking to keeps
  answering messages that name nobody. It appears when the deployment has group
  chats turned on.

### When an agent is stopped

An agent an administrator stopped (or that could not start, or that the
deployment's plan holds back) is marked **Stopped**, **Could not start** or
**Held back** in Chats. Its conversations still open and show everything said
so far, but where the message box would be, the app says why it will not reply.
An administrator sees a **Start** button there; anyone else is told to ask an
administrator.

## An agent's page

The agent's page says whether it is running and, when it is not answering,
why: **Could not start** with the error it stopped on (a missing key, a model
that cannot be reached), **Held back** with what holds it (a plan limit, for
example), or **Stopped** when someone stopped it. Administrators also get:

- **Start**: starts a stopped agent, or tries again after an error.
- **Restart**: stops the agent and starts it again, for example after changing
  one of its keys.
- **Stop**: the agent stops answering messages, channels and scheduled tasks
  until someone starts it again, and it stays stopped if the deployment
  restarts.

Members see the status and the reason, without the buttons. The page also
leads to the agent's conversations, files and scheduled tasks.

## Files and sharing into Olano

The **+** button next to the message box has **Choose Files** for documents:
PDFs, Word, Excel and PowerPoint files, text and data files, up to 10 MB each.
They wait above the message box with their size until you send; the cross
removes one. You can send files with or without a message.

The agent gets them the way the dashboard's chat sends them. A text or data
file (a `.txt`, `.csv`, `.md`, `.json` and so on) is read into your message, so
the agent sees its contents directly. Any other document is saved in the
agent's workspace, and the agent reads it with its document tools. In the
conversation your message shows the files by name, never their contents.

**Sharing into Olano.** From another app (a link from Safari, a photo, a PDF
from Mail or Files, some text), choose Olano in the share menu: the app opens,
asks which agent it is for, and puts it in a new conversation with that agent,
ready to send with a message of your own. Nothing is sent until you send it. A
link or text arrives in the message box, a photo or a file above it. If the app
does not open by itself after sharing, open it: what you shared is waiting.

## An agent's files

**Settings, Files** lists your agents; pick one to see its files, the same
folders as the dashboard's **Files** page. The conversation menu's **Files**
opens the agent's files directly. Tap a folder to open it (the back button
goes up one folder) and a file to read it: photos open full screen, Markdown is
laid out, text and code are shown as they are, a PDF is drawn, and anything
else can be handed to another app with the share button.

The search box at the top finds files and folders by name across all of the
agent's files, wherever you are. Folders the dashboard hides from its file list
(version control, installed packages, caches) are left out here too.

Administrators can upload a file from the phone with the button at the top:
it goes into the folder that is open. A file with the same name there can be
replaced, or kept beside the new one under a new name. Members can open and
search files but not upload.

## Scheduled tasks

A scheduled task is a message an agent gets on a timetable, such as "every
weekday at 8, send me what is on my calendar". Each run is a conversation of
its own, which you find in the agent's conversations with the **Scheduled**
badge. Agents can set these up themselves when you ask them to; the app lets
you see and manage them too.

**Settings, Scheduled Tasks** lists every agent's tasks, grouped by agent;
**Scheduled Tasks** in a conversation's menu lists that agent's. Each row says
when the task runs and when it runs next, or that it is paused. The switch on a
row pauses or resumes it; swipe a row for **Run now**.

Tap a task for its details: when it runs, the agent it runs as, the time zone
its times are in, its instructions and its recent runs (tap a run to read what
it produced, or why it failed). **Run Now** runs it straight away, whatever
its schedule. The pencil button changes it, and **Delete Task** at the bottom
removes it; its past runs stay in the agent's conversations.

The **+** button adds a task: pick the agent, write what it should do (those
words are the message the agent gets each time), optionally a name, and when
it repeats: every day, week (on the days you pick) or month at a time, every
few hours or minutes, or once on a date. The sentence under the choices reads
the schedule back. A new task uses your phone's time zone; changing a task
keeps the time zone it has. A task with a custom schedule made in the
dashboard stays as it is unless you change it. Running more often than every
five minutes shows a warning: each run is a full turn of the agent, which takes
time and uses credits, and when a run is still going as the next one is due,
that next run is skipped.

You can manage the tasks of the agents you have access to, as in the
dashboard's Scheduler.

## Alerts

The Alerts part of the Inbox lists the deployment's notifications. Most of them
come with a fix, and the line under each one says what tapping it does:

- **Open issue**: an issue assigned to you opens in the app. Read it and its
  comments, add a comment, or use the **...** button to **Hand to an Agent**
  (it picks the issue up the next time it checks its issues, and comments as it
  works), **Talk It Over with an Agent** (a new conversation about it), or
  **Close Issue** and **Reopen Issue**.
- **Ask the agent to fix it**: for problems an agent can fix with your help,
  such as a service that needs reconnecting or an MCP server that needs signing
  in again. Tapping opens a new conversation with that agent with a message
  about the alert ready to send; the agent usually answers with a
  [magic link](ask-the-agent.md#magic-links-the-default-whenever-a-browser-is-needed).
- **Repair the folders**: when skills have folder names that do not match their
  names, an administrator can repair them from the phone. A folder is renamed
  to match a valid name; a name that is not valid (a title with spaces or
  capitals) is set to its folder's name. The app reports what it repaired and
  anything it left alone, and the alert clears once nothing is left.
- **Open Cortex**, for a background run that failed.
- **Fixed on the dashboard**: some problems, such as a locked vault, low disk
  space or a usage budget, are fixed in the dashboard. The alert says where.

Tapping an alert marks it read. Its menu always has **Dismiss Alert**.

## The agent's browser

When your deployment runs the browser in **live** mode (see
[Browser](browser.md)), an agent that opens its browser shows it in the
conversation as a small floating window: the page it is on, live, and a line
saying who has it (**The agent is browsing**, **You have control**, **The
agent needs you** when it asked for help, or **Waiting for your OK in the
chat** when a click needs your approval). Drag the window to any corner. Its
**x** hides it until the agent opens a new browser or asks for you again; the
globe button at the top of the conversation opens the browser at any time,
with a dot on it when the agent asked for you.

Tap the window for the full view. While you watch, a dot shows where the agent
is about to click or type, and two fingers (or a double tap) zoom in on the
picture. **Take control** pauses the agent's browsing and lets you use the
page yourself, for a sign-in, a security check or anything only a person
should do:

- **tap** to click (two or three quick taps for a double or triple click);
- **swipe** to scroll;
- **rest your finger, then move it** to press and drag;
- the **keyboard** button opens a box to type into the page, with Backspace,
  Tab and Enter keys;
- **back**, **forward** and **reload**, and tap the address to go somewhere
  else. When the page has several tabs, the tabs button switches between
  them.

The picture is sent at the size the phone shows it and only as fast as the
phone draws it, so taps get through on any page, however much it redraws
(a sign-in's verification step, a feed): on a slow connection it turns a
little rougher for a moment and sharpens again as soon as the phone keeps
up. If a page is too busy to take a tap for a few seconds, the view says so;
tap again, or press reload.

Two buttons at the top of the full view keep what the screen shows, whether
the agent is working or you are: **Record** makes a video of the browser
(**REC** and a clock show while it runs, and **Stop** saves it in the agent's
`recordings/` folder, which the toast opens in Files), and the camera button
saves a picture of the page under `screenshots/`. A recording ends on its own
after 30 minutes or when the browser closes. The agent can record itself too:
ask it to record a demo of what it does.

The page's own questions (a confirmation, a file to upload, a drop-down list,
a date, time or colour to pick) come to you while you have control, as the
phone's own pickers; and when a page asks the browser for something it does
not have (your location, a camera, a microphone, leave to notify) you are
told what it asked for and that it was declined, so a site that then behaves
oddly is not a mystery. **Hand back** returns the browser to the agent; if it
had asked for you, it is told you are done. **Done** closes the
full view and leaves the browser as it is. With the browser in headless mode
there is nothing to watch: the agent's screenshots arrive in its replies when
it names them.

## Group chats

A group chat is one conversation with two to six agents, the same group chats
as in the dashboard. They are listed under **Group chats** at the bottom of the
Chats tab, with the last message in each. Start one with **+**, **New Group
Chat** (or **Start a group chat with several agents** when you have none): pick
the agents and, optionally, a name. The first agent you pick answers messages
that do not name anyone.

To ask a particular agent, write `@` and its name; typing `@` suggests the
members. Name several (`@nova and @aria, what do you think?`) and each answers
in turn, in the order you named them, seeing the replies before its own. The
line above the message box says who will answer. Each reply shows the agent's
picture and name above it, and your own messages say who they went to.

Agents in a group can also hand the conversation to each other after replying.
While they do, a line above the message box says so, with **Stop** to end it
once the current reply is finished.

A one-to-one conversation becomes a group chat with **Add an Agent** in its
**...** menu: the conversation so far comes along, so nobody has to repeat it.

The **i** button at the top of a group chat opens its details: rename it, add
an agent, remove one (swipe it to the left; a group keeps at least two), or
delete the group chat. Group chats appear when the deployment has them turned
on; an administrator turns them on or off, and decides whether agents may
answer each other, at **Config, Group chats** in the dashboard (see
[`groups:`](../config-yaml.md#groups-multi-bot-conversations)).

## Photos

The **+** button next to the message box adds photos: **Take Photo** opens the
camera, **Choose Photos** opens your photo library, where you can pick up to
ten at once. The photos wait above the message box until you send them: tap one
to see it larger, or its **x** to leave it out. Add a few words about what you
want done with them, or send them on their own.

Each photo is sent at a size suited to reading it (at most 2048 pixels on its
longest side) and without its location or other details the camera recorded.
For an administrator, each photo is saved in the agent's `uploads/` folder
(where it also shows in the dashboard's Files view) with a note telling the
agent where, and the agent decides from your message what to do with it: look
at it with its vision tool, pass it on, or keep it. A member's photos are shown
to the agent with that message only and not saved, so they do not come back
when the conversation is reopened.

In a conversation, one photo shows at its own shape and several as a grid.
Tap a photo, yours or one the agent produced, to see it full screen: swipe
sideways for the others in that message, pinch or double-tap to zoom, and swipe
down (or tap the close button) to go back.

The first time you take or choose a photo, your phone may ask whether Olano
may use the camera or your photos. If you declined, allow it again in the
phone's Settings, under Olano.

## Dictation

Tap the microphone and talk: your words appear in the message box as you say
them, and what you send is the text, never a recording. Your phone works the
words out itself, so nothing you say leaves the phone until you send the
message, and the agent gets text it can start on at once.

While you dictate, the message box shows what has been heard so far, with four
controls under it:

- **x** throws away what you dictated this time. Anything you had typed
  before stays in the box.
- The waveform moves while the phone hears you (bars) and goes flat (dots)
  when you pause.
- **Stop** (the square) stops listening and leaves the words in the box, so
  you can correct them, type more, or tap the microphone again to keep
  talking. Dictated words are added after whatever is already in the box.
- **Send** (the arrow) stops listening and sends the message, the words still
  being worked out included. While the agent is working, it adds them to what
  the agent is doing, like a typed message.

Dictation keeps listening through pauses until you stop it, for up to five
minutes. Switching away from the app, or a phone call, stops it and leaves
what was heard in the box.

Dictation is offered where the phone can do it without sending your voice
anywhere: an iPhone that recognizes your language on the phone itself, and an
Android phone (Android 12 or later) with on-device speech recognition. It
listens for the language your phone is set to; Android may need that
language's speech recognition downloaded first (in the phone's settings for
speech recognition, or by using voice typing once). On other phones a tap on
the microphone records a voice note, as below. The first time you dictate,
the phone asks whether Olano may use the microphone and, on an iPhone, speech
recognition. To have a tap record a voice note instead, turn off **Dictate
with the microphone** in the app's Settings.

## Voice notes

There are two ways to record a voice note:

- **Hold** the microphone and talk, then let go to send. Slide your finger left
  to throw the recording away, or up to lock it, so it keeps recording without
  your finger on it.
- **Tap** the microphone to record hands-free, on a phone that cannot dictate
  or with dictation turned off in Settings. A bin button throws the recording
  away and the send button sends it.

Where a tap dictates, a press becomes a voice note once it lasts longer than a
tap, and the microphone is there whether or not you have typed anything.

The dot beside the timer is grey while the phone opens the microphone and
turns red, with a small tick under your finger, the moment it is listening:
what you say from then on is in the note. After a note the microphone stays
ready for about twenty seconds, so the next one starts at once (the phone's
microphone indicator stays on for that long); it closes when you leave the
app. A recording shorter than about 0.7 seconds is not sent, and a recording
sends itself when it reaches five minutes. Leaving the conversation, or
switching away from the app, throws away a recording you have not sent.

By default the recording is sent to the deployment, which turns it into text
(below). With **Transcribe on this phone** turned on in the app's Settings, an
iPhone turns what you said into text itself, and the text is what is sent: the
agent starts on your words at once, the recording stays on the phone, and
nothing depends on the deployment's speech-to-text. Your administrator chooses
the default, and may hide the switch. While the
iPhone works it out, **Transcribing…** shows under the voice note, then your
words (tap them to read all of them). The iPhone does this on the phone when it
supports your language, and with Apple's speech recognition otherwise. It
listens for the iPhone's own language (**Settings, General, Language &
Region**): if you speak another language to your agents, turn off **Transcribe
on this phone** in the app's Settings, and the deployment transcribes
your voice notes instead. The first time, the iPhone asks whether Olano may use
speech recognition.

Otherwise (on Android, with **Transcribe on this phone** turned off, when you
declined speech recognition, or when the iPhone heard no words), the recording
is sent to the deployment, which turns it into text with its speech-to-text
service. The text appears under your voice note once the deployment has heard
it. If speech-to-text is switched off or fails on your deployment, the voice
note says it was not transcribed, and the agent is told it could not hear it.
An administrator turns speech-to-text on in the dashboard, under **Config,
Speech**.

A recording sent to the deployment by an administrator is saved in the agent's
`uploads/` folder, like a photo, and plays again when the conversation is
reopened. A member's recording is used for its text only and not saved. When the
recording was not sent (the iPhone transcribed it, or a member sent it), a
reopened conversation shows what you said, marked as a voice note, without the
recording.

Tap a voice note's play button to hear it, or tap or drag along its waveform to
jump to a point. While it plays, the speed button switches between 1x, 1.5x and
2x. Starting another voice note pauses the first. Audio files an agent sends you
play the same way.

The first time you record, your phone asks whether Olano may use the
microphone. If you declined, allow it again in the phone's Settings, under
Olano.

## What your administrator chooses

An administrator can shape the app for everyone on the deployment, in the
dashboard under **Config, Mobile app**: hide the **Cortex** tab, the whole
**Inbox** or any of its sections (Approvals, Alerts, Issues), and the
**Files**, **Credits and Usage** and **Transcribe on this phone** rows in
Settings, and choose where voice notes are transcribed for people who have not
picked. The app checks for changes every time you open it or come back to it,
so a change shows up without updating the app. If you are on a screen that has
just been hidden, the app takes you to Chats (or back to Settings). Hiding only
removes it from the app; nothing is deleted.

## Hearing replies

The speaker button under a finished reply reads it aloud; tap it again to stop.
Replies to your voice notes are read aloud as they arrive, so a conversation
can go back and forth by voice. **Read replies aloud** in the app's Settings
chooses when that happens:

- **After my voice notes** (the default): a reply to a voice note is read aloud.
- **Always**: every reply is read aloud as it arrives.
- **Never**: replies are read only when you tap the speaker.

The voice depends on the deployment. When an administrator has turned on text
to speech (**Config, Speech** in the dashboard), replies are read in the voice
chosen there, and each reading is billed like the deployment's other speech
use. Otherwise the phone reads them in its own voice, at no cost; an iPhone
picks the best voice it has for the language the reply is written in (better
voices can be downloaded in the iPhone's **Settings, Accessibility, Spoken
Content, Voices**).

A reply is read the way a person would read it to you: code blocks are left
out, a link is read as its text and a web address as the name of its site.
Playing a voice note or starting a recording stops the reading, and so does
leaving the app. These choices are kept on that phone only.

## Hands-free

**Hands-Free Mode** in a conversation's **...** menu is for when your hands and
eyes are busy, such as driving or cooking. The phone listens; when you stop
talking it sends what you said, reads the reply aloud when it arrives, and
listens again. On a phone that can [dictate](#dictation), your words appear
under the button as you say them and go as a text message, worked out on the
phone itself, so nothing waits on the deployment's speech-to-text; a pause of
about two seconds after your last word sends them. On other phones, or with
**Dictate with the microphone** turned off in the app's Settings, what you say
goes as a voice note. If dictation cannot start (the phone's speech
recognition is turned off for Olano, say), the app says so and sends voice
notes for the rest of that hands-free conversation. The message box gives way
to one large button:

- While it listens, tapping it sends straight away instead of waiting for the
  pause.
- While a reply is being read, tapping it stops the reading and listens.
- After about fifteen seconds of silence it pauses; tap it to talk again.

**End Hands-Free** brings the message box back, and so does leaving the app.
Every reply is read aloud while hands-free is on, whatever **Read replies
aloud** is set to, in the voice described in [Hearing replies](#hearing-replies),
so a hands-free conversation costs nothing beyond the messages themselves.

## Siri, the Action button and Shortcuts

On an iPhone, two things can be asked of the app from outside it. Both name
one of your agents, and Siri learns the agents' names from the app, so say
them as they appear in Chats.

**Ask an agent** sends what you say and reads the reply back, without the app
opening. It takes two breaths: name the agent, then answer Siri's question.

- "Hey Siri, ask Jane in Olano". Siri asks "What do you want to ask?", and
  you say the question.
- "Hey Siri, ask an agent in Olano" (or "Hey Siri, ask Olano"). Siri shows
  your agents and asks which, then asks for the question.

Keep the first sentence to the agent's name and the app's: words after it in
the same breath ("...what is on my calendar") make Siri treat it as a text
message to send, and it offers Messages instead. "Olano, ask Jane" and "ask
Jane on Olano" work too. The two actions appear in the **Shortcuts** app under
Olano once the app has been opened; if Siri still does not know an agent's
name, open the app once so it learns the current names, or say "ask an agent
in Olano" and choose.

Siri speaks the reply in its own voice. A reply that is mostly code or links
is read the way the app reads one: code is left out and a link is read as its
site. A few words from Siri follow soon after you speak; when the agent takes
longer, Siri says the agent is still working and the reply comes to the app
instead, and to the phone as a [notification](#notifications) when those are
on. A question asked within half an hour of the last one continues the same
conversation, so "and tomorrow?" means what you think; after that a fresh one
starts. These conversations appear in Chats like any other.

**Talk to an agent** opens the app straight into [Hands-free](#hands-free)
with that agent, in a new conversation: it listens, sends what you say, reads
the reply in the agent's voice when the deployment has one, and listens again.

- "Hey Siri, talk to Hannah in Olano"
- **Settings, Action Button, Shortcut** on an iPhone with an Action button:
  pick **Talk to an Agent** (or **Ask an Agent**) and the agent it should
  use, and a press opens the conversation listening.
- The same two actions are in **Control Center**, on the Lock Screen, and in
  the **Shortcuts** app, where **Ask an Agent** can be a step of your own
  shortcut or automation: its reply is passed on as text (a morning briefing
  that is read to you at seven, say).

The actions need iOS 16.4 or later, the app signed in to a deployment, and
work with whichever agents you can chat with. On Android, a long press on the
app's icon offers **Talk to ...** for the first few agents, which opens the
app the same way; those can be dragged to the home screen.

**Settings, Siri and widgets, Default agent** is the agent these reach when
none is named: "Hey Siri, talk to Olano" suggests it first, the Action
button and a Control Center control with no agent chosen open it, a widget
shows it until you pick another, and on Android it is the first shortcut.
Left at **First in the list**, it is whichever agent Chats lists first. The
choice is kept on that phone.

## Widgets (iPhone)

Touch and hold the Home Screen, tap **Edit**, then **Add Widget** and choose
Olano. The widgets read the deployment on their own, with the session the app
signed in with, so they keep up while the app is closed; they refresh every
few minutes, and at once when you do something in the app or on a widget.
They need iOS 18 and the app opened once after signing in. A widget added
while the app was closed fills in the next time the app is opened.

Every widget that shows one agent shows the **default agent** (**Settings,
Siri and widgets**) until you pick another for it: touch and hold the widget
and choose **Edit Widget**.

- **Ask** (small, medium, Lock Screen): the simplest one, a way in. The
  small one is a single "Ask Anna" pill that opens a new conversation with
  the keyboard up. The medium one adds the composer's buttons: **Message**,
  **Photo** (the camera opens), **Photos** (the picker opens) and **Talk**
  (the conversation opens hands-free), so a question, a picture or a
  conversation is one tap from the Home Screen.
- **Agent** (small, medium, and every Lock Screen shape): one agent's
  picture, its status, the last reply in your conversation with it, and how
  many approvals it is waiting on. Tap it to open that conversation, the
  same one a tap on the agent in Chats opens (a new one when you have none
  yet; scheduled runs and the agent's other threads stay in its
  conversation list). The medium one has **Talk**, which opens a new
  conversation hands-free, and for administrators **Start** or **Stop**.
- **Quick asks** (medium, large): an agent and up to three questions you set
  when adding the widget, such as "Give me a short briefing of what matters
  right now". A tap sends one; the reply appears in the widget when it
  arrives (the large size keeps it in view). A widget cannot take typing, so
  the questions are fixed; tap the agent to open the conversation for
  anything else.
- **Approvals** (small, medium, Lock Screen): what is waiting on you. The
  medium one shows the oldest approval with **Approve** and **Decline** and a
  countdown to when it expires; a high-risk action has no buttons, so you see
  in the app exactly what would run. Tap to open the Inbox.
- **Team** (medium, large): every agent with its status and the last word
  in your conversation with it, each opening that conversation.
- **Pulse** (small, medium, Lock Screen): the newest Pulse card from Cortex.
- **Next run** (small, medium, Lock Screen): the next scheduled task,
  counting down, with the ones after it.

In **Control Center** (and the Lock Screen's two bottom buttons, and the
Action button on an iPhone that has one), two controls can be added:
**Talk to an Agent**, which opens the app listening to the chosen agent, and
**Message an Agent**, which opens a new conversation with it. A control with
no agent chosen reaches the default agent.

What a widget shows is the last thing the deployment answered, so a reply
written seconds ago may take a moment to appear; the app itself always shows
the latest. Signing out in the app clears the widgets.

## Appearance

**Settings, Appearance** chooses how the app looks:

- **System** (the default): light or dark following the phone's own setting,
  changing with it.
- **Light**: always light, whatever the phone is set to.
- **Dark**: always dark, whatever the phone is set to.
- **Volt**: always a black page with an electric chartreuse accent, whatever
  the phone is set to. It is the dashboard's Volt theme, so a browser set to it
  and the phone look alike; the status bar and keyboard follow it as they do
  Dark.

The choice is kept on that phone only, for every deployment it opens.

## Locking the app

The app keeps you signed in for two weeks, and your agents can act on your
accounts. **Settings, Privacy, Lock with Face ID** (Touch ID, or a fingerprint
or face unlock on Android, depending on the phone) makes the app ask for it
before anything shows: when the app opens, and when you come back to it. The
phone's own passcode always works too. Turning it on asks once, to make sure
the phone can unlock it.

**Ask again** chooses how long the app can be in the background before it asks
again: **Immediately** (the default), or after 1, 5 or 15 minutes. While the
app is in the background its screen is covered, so the phone's app switcher
shows nothing either. The setting needs a passcode on the phone, and is kept
on that phone only.

## Credits and usage

On a deployment that runs on Olano credits, **Settings, Credits and Usage**
shows the credits left, how much of this month's included credits are used and
when they renew, any top-ups left, and what each Olano service (chat, images,
speech and so on) used over the last 30 days. It is for reading only: buying
credits is done from the dashboard. Deployments that use their own provider
keys have no credits, and the row does not appear.

## Notifications

Notifications reach you while the app is closed or in the background. While it
is open on screen you see things happen live instead, so it does not also
notify you.

Turn them on in **Settings, Push notifications**; your phone asks for
permission once. There are four kinds, and each phone chooses its own:

| Kind | When it notifies | Who receives it |
|---|---|---|
| **Replies** | An agent finished answering a message you sent, and you were not looking at that conversation (the app was in the background and no browser had it open). Not sent for a reply you stopped. | You, the person who sent the message. |
| **Approvals** | An agent is waiting for you to approve or reject an action: in a conversation you started, or in the Approvals inbox for an agent you can act on (a scheduled run, Cortex, a request from another agent). | The person who started the conversation; for the inbox, every administrator and every member with access to that agent. |
| **Cortex** | Cortex wants approval for a change to an agent, or found something urgent. Ordinary findings wait in the Cortex tab. | Administrators and members with access to that agent. |
| **Alerts** | A warning or error appeared on the bell, such as a channel that cannot start. Each problem notifies at most once every six hours. | Administrators only. |

Tapping a notification opens what it is about: the conversation for a reply or
an approval in a conversation, Pulse in the Cortex tab for a Cortex finding, and
the Inbox for an inbox approval, a Cortex card that needs an approval, or an
alert.

**Answering from the notification.** An approval notification has **Approve**
and **Decline** buttons, and a reply notification has a **Reply** field: press
and hold the notification (or pull it down) on an iPhone, or expand it on
Android. The phone asks to be unlocked first, then the app answers without
opening: the approval is decided, or your reply goes into that conversation as
your next message, and the agent's answer arrives as another notification. An
action marked high risk has no buttons, so that it is always looked at in the
app first, and a reply that ran into a problem has no Reply field. If answering
does not work (the deployment cannot be reached, or you need to sign in again),
a notification says so; tap it to open the app. A reply sent while the agent is
still working on your last message is not sent; the notification says so.

An approval nobody answers is rejected when the agent's approval timeout runs
out, five minutes unless the agent sets `security.hitl_timeout_seconds`
(see [Human in the loop and approvals](human-in-the-loop.md)). Keep
**Approvals** on if you rely on answering them from your phone.

### What a notification contains

By default a notification names the agent and the reason ("maria needs your
approval", "Replied to your message.") and never contains what anyone wrote, or
the details of the action awaiting approval beyond its name. An administrator
can set `push.previews: true` in [`config.yaml`](../config-yaml.md) to include
the start of replies and Cortex finding titles; that text then passes through
Apple's or Google's notification service on its way to the phone.

Signing out in the app, or switching deployment, stops that deployment's
notifications on that phone. Deleting a person's account stops them on every
phone they had.

### Who can have notifications

Notifications are delivered through the Olano Cloud, so they work on
deployments that run on Olano Cloud and are recent enough to send them. Any
other deployment can still be used in the app for chat, Cortex and the inbox;
**Settings** says why its notifications are unavailable. An administrator
controls them for the whole deployment with the `push:` block in
[`config.yaml`](../config-yaml.md): switching them off, the previews above, the
lowest Cortex priority that notifies, and the lowest alert level that notifies.
