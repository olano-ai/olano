# Browser control

Agents can use a web browser: open a page, read it, click, type, take a
screenshot. It is **off by default**, and the machine's administrator turns it
on in **Config → Browser** (or in [`config.yaml`](../config-yaml.md#browser-browser-control)).

## The three modes

| Mode | What agents get | What it needs |
|---|---|---|
| **Off** (default) | No browser. The browser tools disappear from every agent's tools and instructions, even an agent whose tool list names them. | Nothing. |
| **Headless** | An invisible browser inside Olano, one separate session per agent. | At least 1.5 GB of memory. Nothing to install. |
| **Live** | A full, visible browser per agent, each in its own isolated container, which you can **watch** and **take over** from the agent's **Browser** tab. | At least 2 CPUs, about 4 GB of memory, Docker, and a plan that includes it. |

Choosing **Live** and saving fetches the live browser in the background
(Config → Browser shows the progress, and **Get image** starts it again). It is
a separate image rather than something installed into Olano itself, so updates
never remove it. When the machine cannot download it (a self-hosted install
usually cannot, because the published image needs a registry login), Olano
builds it on the machine instead from the files it ships with. That takes a
few minutes, about 1.5 GB of free disk and access to the operating system's
package mirror; if it fails too, the card names both reasons. Headless needs
neither.

Olano refuses a mode the machine cannot carry when you save, and says why. A
machine with 1 GB of memory gets no browser on it at all; if live is chosen on
a machine that later turns out too small, agents fall back to headless and the
Config card says so.

**Where the browser runs.** By default on this machine. Choose **Remote
browser service** instead to have agents use a hosted browser over the
DevTools protocol, which costs this machine no memory: store the service's
connection address (a `ws://` or `wss://` address, usually with its key in it)
in the [vault](../dashboard/vault.md) as `BROWSER_REMOTE_CDP_URL`. The live view
and taking control need the browser on this machine.

## Going through a proxy

Websites see where a visit comes from. A browser on a server visits from a
data-center address, and some sites (Reddit and X among them) turn most
data-center visitors away or treat their sign-ins as suspicious. To have the
browser visit from another address, store a proxy in the
[vault](../dashboard/vault.md) as `BROWSER_PROXY_URL`:

| Format | Example |
|---|---|
| HTTP or HTTPS proxy, with or without a sign-in | `http://user:password@proxy.example.com:8000` |
| SOCKS proxy, without a sign-in | `socks5://proxy.example.com:1080` (also `socks4://`) |

- It applies to the headless and live browsers on this machine, including
  one-shot screenshots, from each agent's next browser action. An open
  browser is reopened under the new setting (sign-ins kept).
- A value set for one agent in the vault overrides the shared one, so agents
  can use different exits, or one agent a proxy and the rest none.
- A value the browser cannot use (a missing port, a SOCKS proxy with a
  password, which browsers do not support, a scheme other than the four above)
  stops the browser and tells the agent why. It never browses without the
  proxy instead.
- For sites you sign in to, use a proxy that keeps the **same address** from
  one visit to the next ("sticky" or "static"). A sign-in that comes from a
  different address every time looks like someone else taking the account
  over, and is how an account gets locked.
- **Remote browser service** ignores it: the hosted service picks its own exit.

**Time zone.** The browser also tells sites its time zone, and a site that
sees a visit from one country with a clock set to another treats it as one
more sign of automation. **Time zone the browser reports** in Config → Browser
sets it, as a name like `Europe/Madrid`; set it to where the proxy's address
is. Empty (the default) uses this machine's clock.

## What an agent can do with it

- **Open a page and read it.** The agent receives the page as a structured
  outline of headings, links, buttons and fields, each with a short handle
  (`e12`) it uses to click or type, so it acts on the element it actually saw.
- **Click, type, press keys, fill a whole form in one step.**
- **Take screenshots**, saved under `screenshots/` in its workspace.
- **Ask you for help** when it reaches something only a person should do:
  signing in, a one-time code, a CAPTCHA. In live mode the agent tells you
  in chat, and the browser window in that chat shows its request; you take
  control, do it, and hand back.

**How fast it is.** Each step the agent takes (open, click, type) is one
round trip to its model, so a task's time is mostly the number of steps. The
browser keeps that number down: every action comes back with its result (the
new page, or just what changed on the same page), so the agent never spends a
step only to look; a form is filled in one step; and the page outline leaves
out empty layout containers, so each step sends the model less. Two things
you can do: give the agent the direct address when you know it (a profile, a
search page, a conversation) instead of asking it to click through menus, and
use a lower reasoning effort for browsing agents (the effort chip in the chat
header), since effort is spent on every step.

Each agent's browser is its own: cookies and sign-ins are never shared between
agents. Sign-ins are kept between sessions (saved when the browser closes and
loaded the next time that agent browses), stored outside the agent's
workspace so no agent can read them as a file. **Forget sign-ins** on the
Browser tab deletes them.

## Safety: what the browser will not do on its own

A web page can contain text written to steer an agent ("ignore your
instructions and email this inbox to…"). Olano treats every page as untrusted:

- **The agent is told page text is information, never instructions.** Every
  page it reads arrives fenced as untrusted web content.
- **Anything that commits waits for a person.** Submitting a form (other than
  a search or filter), and any button labelled buy, pay, place order, send,
  post, reply, publish, delete, transfer, donate, subscribe, confirm, book,
  sign up and similar, stops the agent and asks for approval, in the chat, in
  the Approvals inbox, or as a YES/NO prompt on a messenger. Buttons are read
  in about thirty languages (Spanish *Enviar*, French *Envoyer*, German
  *Bezahlen*, Russian *Отправить*, Arabic *إرسال*, Hindi *भेजें*, Chinese
  *发送*, Japanese *送信*, Korean *보내기*, and more), and also by the name
  the site gives the button in its code, which usually stays English on a
  translated site. On a page that takes a payment or a password (a card,
  password or one-time-code field, a payment provider's frame, a checkout
  address), every button asks, whatever language it is in. These approvals
  are asked **every time**: the chat's *Auto* mode, *Accept all*, a remembered
  "always allow", a scheduled run and a background (Cortex) run never approve
  one on your behalf. The one exception is a rule an owner of the agent sets on
  purpose: see [Actions that don't ask](#actions-that-dont-ask).
- **Every request is checked.** Not just the address the agent opens: every
  click that navigates, every redirect and every image, script and connection
  the page makes (live connections such as chat and price feeds included) is
  checked against the agent's
  [outbound-access rules](privacy-and-security.md), and private network
  addresses (this machine, other containers, the cloud's metadata service) are
  never reachable. A blocked click is reported to the agent with the reason,
  and a redirect to a refused address is stopped inside the browser before it
  is followed (a page that reaches one anyway is discarded before the agent
  reads it).
- **Files stay where they belong.** In `headless` mode the browser can open a
  file from the agent's workspace, and nothing else on the machine; a
  screenshot is saved only inside the workspace or a shared folder the agent
  can write to.
- **Passwords never pass through the agent.** When a sign-in is needed, the
  agent asks you to take over the live browser and you type it into the page
  yourself. Never paste a password into the chat.

## Actions that don't ask

An agent that sends many messages on one site (say, LinkedIn) turns the
approval card into a stream of clicks nobody reads. For that case an admin or
an owner of the agent can add a narrow rule: **one site, and the kinds of
action on it that go ahead without asking**. Set it on the agent's **Browser**
tab, under *Actions that don't ask*, or ask the agent (or AgentFather) to do
it in a chat; either way you confirm the change in an approval card.

**Adding a rule.** With no rules yet, the section offers one-click examples
(*Send messages on LinkedIn*, *Post and reply on X*, *Book meetings on
Calendly*) next to **Add a site**. Each rule reads back as a sentence while you
edit it ("On linkedin.com, the agent may send messages and posts without
asking you, only while you chat with it"), so you can check it says what you
mean before you press **Save rules**. Saved rules fold into that sentence;
**Edit** opens one again and **Remove** deletes it (both take effect when you
save). A mistake (a website that is not one, the same site twice, a rule with
no action ticked) is explained under the field and keeps **Save rules** off.

| Field | What it means |
|---|---|
| **Website** | The site's name, such as `linkedin.com`, or any link from it pasted in: only the site name is kept, and a leading `www.` is dropped (`https://www.linkedin.com/feed/` becomes `linkedin.com`). It covers the whole site, every page and sub-domain; `*.example.com` covers only sub-domains. The action must happen on this site, and a form must also send its data to it, or the agent asks as usual. |
| **Send messages and posts** | A button or link that sends a message, replies, posts, publishes, connects or invites (a message's *Send*, *Connect*). |
| **Submit forms** | Any other form that saves or changes something on the site, and buttons labelled *Save*, *Submit* or *Confirm*. |
| **Sign up and book** | Subscribe, sign up, create account, book, reserve. |
| **When** | **Only while chatting** (the default, recommended): the rule applies only while the agent works in a conversation started by a person who owns it or is an admin, so someone sees each action. **Also when it works on its own**: it also applies to scheduled jobs and background (Cortex) runs. |

Anything you leave unticked keeps asking.

What a rule never does, whatever it says:

- **Purchases, payments, transfers, donations, deletions, approvals and
  merges always ask.** A button whose label mentions any of them, in any
  language the browser reads (*Send payment*, *Pagar ahora*, *購入する*), is
  never covered, and neither is any button on a page that takes a payment or a
  password.
- **An action the browser cannot name always asks.** If a button's label is
  in a language or wording the browser does not recognize, the action still
  waits for you and no rule covers it.
- **It never applies to someone else's request**: another agent, an outside
  MCP/A2A caller, a webhook or a watched email. Nor to a person who could not
  have approved the action themselves: a member who may chat with the agent
  but does not own it still gets asked.
- **It is never invisible.** Every action a rule let through says so in the
  chat ("Approved automatically by this agent's browser rule for
  linkedin.com"), and the agent tells you in its reply.

Rules apply to the agent's next action; no restart. Everyone who can chat with
the agent can read them on the Browser tab; only admins and the agent's owners
can change them.

## Watching and taking control (live mode)

**From the chat.** While the agent you are chatting with has a browser open, a
small window in the corner of the conversation shows its screen. A cursor
moves to whatever the agent is about to click or type into, and ripples on
each click, so you can follow what it does as it does it. Click the window (or
its expand button) to open it large; collapse it to go back to the small
window. The window shows the site the agent is on and a one-line status; the
**–** button folds it into a small **Live browser** pill (remembered on that
device), and on a phone it is always the pill, which opens the browser full
screen. Hiding it with **×** keeps it away until the agent uses its browser
again (on your next question, for example), opens a new one, or asks for you.
To keep it out of the way while the agent works, use **–** instead. When the
agent asks for a person, the window
is outlined and says so.

**Approvals while you watch.** An approval is answered in the chat, never in
the browser. When the agent stops for one, the window folds into an
**Approval needed** pill, a large view you were watching closes, and the card is
scrolled into view; **Review** on the pill takes you back to it. The card says
what the agent wants to do in words: the button or link, the site, and why it
needs you. In Auto mode too: the chat's approval mode never answers these for
you.

**On the agent page.** The agent's **Browser** tab shows the session's
details (the page it has open, who is in control) and **Open chat**, which
takes you to the window above. If you leave the chat while you hold control,
**Hand back to the agent** is there too.

The screen updates as the page changes, and watching never acts on the page.
The picture is sent at the size it is shown and only as fast as your device
draws it: on a slow phone or connection, or on a page that never stops
moving, it turns a little rougher for a moment and sharpens again as soon as
it keeps up, and your clicks and taps always go through. If a page is too
busy to take a tap for a few seconds, the view says so; tap again, or press
**Reload**.

1. Press **Take control**. The agent is paused: its browser actions wait and it
   is told a person is using the browser.
2. Click and type in the picture as in any browser. A double click selects a
   word and a triple click a paragraph (on a touch screen, a double or triple
   tap). A right click reaches the page, so a site's own right-click menu
   works; the browser's own menu (Copy, Paste, Inspect) is not shown, so use
   the shortcuts below instead. Paste works with your usual shortcut (⌘V on a
   Mac, Ctrl+V elsewhere), and copy or cut (⌘C / ⌘X, Ctrl+C / Ctrl+X) puts the
   text you selected on the page onto your own clipboard. A page's own copy
   button (the icon next to an API key or a client secret, say) does the same
   when you press it. If your browser asks for a click first, a **Copy**
   button appears above the picture. On a Mac, ⌘ shortcuts such as ⌘A and ⌘Z
   do what they do on a Mac. Text in a password field is never copied out of
   the page by a selection; a page's copy button copies whatever the page
   chose to copy.

   Above the picture is a browser's usual toolbar:

   - **Tabs.** Every page the browser has open, the one you see highlighted.
     Tap a tab to switch to it, its **×** to close it, and **+** for a new
     tab. Closing the last tab leaves an empty page rather than closing the
     browser. A link that opens a new window shows up as a new tab.
   - **Back** and **Forward** (greyed out when there is nowhere to go), and
     **Reload**, which turns into **Stop** while a page loads. A thin bar
     runs across the top of the picture while the page is loading.
   - **The address bar** shows the address of the page you are on and follows
     it as the page changes. A padlock means the connection is encrypted
     (https); a warning sign means it is not (http), so do not type
     passwords there. To go somewhere else, edit the address (or type a new
     one; `example.com` is enough) and press **Go**. Escape puts the current
     address back.

   Some things a web page asks for are drawn by the browser rather than the
   page, so they appear as a panel over the dashboard instead:

   - **Dropdown lists.** Click or tap one and its options open in a list you
     can scroll (long lists, such as countries, get a filter box). Pick an
     option to set it on the page.
   - **Messages and questions from the page** ("Are you sure?", "Enter a
     name", "Leave this page?"). Answer them in the panel; the page waits
     until you do. If you hand back with one still open, it is cancelled.
   - **File uploads.** When you click an upload button on the page, choose
     files from your own device (up to 10 MB in total). They go straight to
     the page. Files the agent's browser downloads are not saved.
   - **Date, time and colour fields.** Click or tap one and a picker of your
     own opens (on a phone, the phone's). **Set** puts the value on the page
     as if its own calendar had; a value the page does not accept (a date
     outside its range) is refused with a note, so choose another.
   - **What the page cannot have.** The agent's browser has no location, no
     camera, no microphone and shows no notifications, so a page that asks
     for one of them is refused at once. You are told what it asked for, so
     a site that then behaves oddly ("enable location to continue") is not a
     mystery; there is nothing to allow.

   **Recordings and pictures.** Two buttons at the end of the address bar (on
   a phone, at the top of the full view) keep what the screen shows:

   - **Record** makes a video of the browser (MP4, what the live view
     shows), whether the agent is working or you are; the button turns into
     a red **REC** clock while it runs, every viewer sees it, and **Stop**
     saves the file under the agent's `recordings/` folder, where the Files
     tab shows it, a message can carry it and the agent can read it. A
     recording ends on its own after 30 minutes, or when the browser closes.
     The agent can record itself too: ask it to "record a demo of ..." and it
     starts, works and stops, naming the file in its reply.
   - **Picture** saves the page as it is now, a PNG under `screenshots/`.

   On a phone or tablet, the picture works like a page on your phone: tap to
   click and drag one finger to scroll. To drag something on the page (select
   text, move a slider), touch and hold, then move. Pinch with two fingers to
   zoom into the picture; while zoomed, dragging moves around the picture
   before it scrolls the page, and **Reset zoom** shows the whole page again.
   To type, tap the field on the page, write in the text box above the picture
   and press **Send text** (your keyboard closes so the whole picture is back
   in view). **Delete** removes the character before the cursor, **Tab** moves
   to the next field, and **Enter** presses Enter on the page, which usually
   submits the form.

   When a sign-in window that a page opened closes itself (as "Sign in with
   Google" windows do once you are signed in), the view returns to the page
   that opened it.

   Passkeys and security keys cannot work in the agent's browser: they live
   on your own phone or computer, which the browser cannot reach. When a site
   asks for one, the browser answers as if you had pressed Cancel, and the
   site offers its other ways to sign in (for example an authenticator app, a
   text message or a password; on GitHub, **More options**).
3. Press **Hand back to the agent**. Any sign-in you made is saved straight
   away, and the agent continues. When the agent asked you to take over and you
   hand back from the chat, the chat also sends it a short message saying you
   are done, so you do not need to type one. (In the chat's large view, collapse
   back to the small window once you have handed back.)

**Close browser** ends the session and frees its memory (sign-ins are kept). A
browser also closes on its own after the idle time set in Config → Browser,
unless someone is watching it.

## Limits

- **Agents browsing at once** (Config → Browser) caps how many agents hold a
  browser at the same time. When it is reached, the browser idle longest is
  closed to make room; if every one is busy, the agent is asked to try again
  shortly.
- Each headless browser uses roughly 250 to 350 MB of memory while open; each
  live one roughly 500 to 700 MB. Nothing is used while no agent is browsing.
- Some sites refuse automated browsers: see the next section.
- Downloads are not saved: a file the agent needs should come from a direct
  link through its web tools.

## Sites that turn the browser away

Some sites check whether a visitor is a person and refuse a browser they
think is not: Reddit's *"You've been blocked by network security"*, X's
*"Could not log you in now"* or a locked account, a *"Verify you are human"*
or *"Just a moment..."* check, an *"Access denied"* page. Retrying does not get
past these, and repeated sign-in attempts are exactly what gets an account
locked. So the browser recognises them and stops:

- **A check that passes on its own is waited out first.** Cloudflare's
  *"Just a moment..."* often lets an ordinary browser through by itself a few
  seconds later. The browser gives it up to 25 seconds, without clicking
  anything, and carries on with the site if it clears. Only a check that is
  still there after that, or that asks a person to do something, counts as a
  refusal.
- **A check only a person can pass goes to a person**, as described in
  [When a site asks for a person](#when-a-site-asks-for-a-person) below.
- **The agent is told plainly** that the site refused the browser, that it
  must not try that site again in this task, and to tell you. It gets only a
  short excerpt of the refusal page, so it does not mistake it for the content
  it was looking for. In live mode, when a person could get past it (by signing
  in or completing a check), the agent may ask you once to take over.
- **The next attempt never reaches the site.** For the next 15 minutes the
  agent's browser refuses to open any page of that site (any address on it,
  `old.reddit.com` included) or to click on the refusal page, and tells the
  agent why. Nothing is sent to the site. This stops the agent only: while
  you hold control you can open, reload and complete the check on that site
  yourself.
- **It lifts** when you take control of the live browser and hand it back
  (you may have signed in), when the agent's proxy changes, or after the 15
  minutes. A one-shot screenshot that is refused stops further screenshots of
  that site, not the agent's own, possibly signed-in, browser.

Ordinary pages are not mistaken for refusals: a sign-in form that mentions it
is "protected by reCAPTCHA", or an article that talks about CAPTCHAs, reads
normally.

## When a site asks for a person

Some checks only a person can pass: Cloudflare's *"Verify you are human"* box,
a CAPTCHA, a *"press and hold"* button. The browser never passes one itself. It
does not tick the box, solve the puzzle or disguise itself to avoid being shown
one. What the agent does instead is set in **Config → Browser → When a site
asks for a person**:

| Option | What happens |
|---|---|
| **Wait for a person** (default) | The browser window in the agent's chat shows the check with **Verify now** and **Skip**, and everyone who can open the agent's chat gets a notification on their phone. The agent waits up to **Minutes to wait for a person** (default 5, up to 120). |
| **Hold until a person does it** | The same, with no time limit. A scheduled or background run, which nobody is watching, waits the set minutes instead. |
| **Skip and move on** | Nobody is asked. The agent is told at once that the site needs a person, and uses another source or asks you. |

**Verify now** opens the large browser view with control already yours. Tick
the box. As soon as the page gets past the check, control goes back to the
agent by itself and it carries on with what it was doing; there is nothing to
hand back. **Skip** tells the waiting agent to move on. If nobody comes in time,
the agent is told so, and can ask you again or use another site. The check
stays usable while it waits: Cloudflare refreshes its box every two minutes or
so, and the browser lets those refreshes through.

- **Per agent.** The agent's **Browser** tab has the same choice, which
  overrides Config → Browser for that agent. Empty uses Config → Browser. Only
  an admin or an owner of the agent can change it.
- **Per task.** An agent picks another option for one task when your request
  makes it clear: "search PropertyGuru for..." means waiting for you, while
  "find me jobs in Singapore" means skipping a site that asks and trying
  another. You can also say it outright ("if a site asks for verification,
  wait for me").
- **Live browser only.** Only the live browser has a screen a person can use,
  so with the headless browser every check is skipped.
- Pressing **Stop** in the chat ends the wait too.
- When nobody is going to pass the check (the wait ran out, or it was
  skipped), the live browser shows a short page saying it stopped at the
  check, rather than leaving the check to turn into an error page at its next
  refresh.

### When the check keeps coming back

If the box turns green and then the check starts again, the agent's **Browser**
tab has a **Debug trace** card (admins and the agent's owners). A trace starts
by itself whenever the browser waits for a person on a check and keeps going
five minutes after; **Start recording** records the open browser by hand.
It reads the record and names the likely cause:

| What it says | What it means |
|---|---|
| The exit address changed | The address the site sees moved between requests. Cloudflare's pass belongs to one address, so a proxy that hands out a new address per connection loops forever. Use one with a fixed ("sticky") address, or none. |
| The pass was sent and refused | The browser handed back Cloudflare's pass and Cloudflare challenged again: it no longer trusts it for this visitor. |
| The pass was not sent back | The browser did not return the pass on the next page load; the trace says why, in Chromium's words. |
| The pass was never issued | The tick was not accepted on Cloudflare's side. |
| The browser's guard refused a request | One of this browser's own rules stopped a request the check needed. |

**Download trace** saves the whole record as a JSON file to send to whoever is
helping. It keeps page addresses without their query values and cookie names
without their values; nothing you type is in it.

The browser adds nothing to the pages it opens that a bot check could take
for automation: the checks above happen outside the page.

The agent's saved sign-ins keep a site's own sign-in, never a bot check's
pass. Cloudflare ties its pass to the one browser it checked, so each new
browser earns its own (or, more often, is let straight through). A pass
carried over from an earlier browser is what used to bring the check back on
every visit.

What makes a refusal less likely: a [proxy](#going-through-a-proxy) with a
steady address and a matching time zone, signing in yourself through **Take
control** rather than having the agent type a password, and not hammering a
site with many visits in a row.
