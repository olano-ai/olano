# `agent.json5` reference

One file per agent, at `~/.olano/agents/<name>/agent.json5`. JSON5, so
comments, trailing commas and unquoted keys are allowed. Most people edit it
through the dashboard's Agents tab; this section is the complete field list for
when you edit it directly, or when an agent writes one for you.

`${VAR}` in a string is expanded from the environment when the file loads.
**Never put a secret in this file** — put it in the vault and reference it by
name (see [Channels and connectors](feature-guides/channels-and-connectors.md)).

A minimal agent:

```json5
{
  name: "finance",
  description: "Watches revenue and flags anomalies",
  emoji: "📊",
  model: "olano:pro",
  system_prompt: "You are a finance analyst. Be precise with numbers.",
  tools: ["sql", "stripe"],
  channels: [{ type: "telegram", allowed_users: ["123456789"] }],
}
```

## Identity

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | The agent's permanent handle. Lowercase letters, digits, dash and underscore, starting with a letter or digit. It is the folder its workspace lives in, the path segment in every `/api/agents/…` URL, and the exact string other agents use to reach it. **It cannot be changed after the agent is created** — a rename would strand its threads, memories, saved credentials and issue history. Put anything you may want to change later in `display_name`. |
| `display_name` | string | the handle | What the agent is *called*, as opposed to how it is addressed. Never empty: leave it unset and it becomes the handle itself. Free text — spaces, capitals, accents and punctuation are all fine (`"Merli the Analyst"`). It appears on the agent's dashboard cards and chat header, in its own system prompt and `whoami` output, in the roster other agents read when choosing who to delegate to, and in the `From:` line of any email it sends. Empty falls back to `name`. Unlike `name` this can be edited at any time — nothing is keyed off it, so changing it keeps every thread, memory and issue intact. |
| `org.title` | string \| null | the archetype's label, else `null` | The agent's **title** — its position, what it *is* as opposed to what it is called (`"Recruitment Specialist"` for an agent named `Hillary`). Shown under the name on its cards, in the detail header, in the simple chat's header and profile, and on the org chart; told to the agent in its own prompt and `whoami` output, so it introduces itself by its name and describes its role by its title; and listed beside its name in the roster other agents read when delegating. Installing an archetype fills it with the archetype's human label unless you give one, so naming an install "Hillary" never costs her the role. Free text, optional, editable any time — **Title** on the Overview tab or the Talk to Agent tab. Empty means no position. |
| `description` | string | `""` | One line explaining what the agent is for. Shown in the roster and to other agents deciding whether to delegate to it. |
| `emoji` | string | `"🦁"` | The agent's face wherever no avatar is set. |
| `color` | string \| null | `null` | CSS hex accent, e.g. `"#c96442"`. Themes the dashboard when this agent is active. |
| `avatar_url` | string \| null | `null` | URL of a square image used as the agent's face. Falls back to `emoji`. |
| `archetype` | string \| null | `null` | The template this agent was created from. Informational. |

Every place an agent is created asks for **one name** — the New agent dialog
(the custom tab and the Team/Expert/Integration tabs alike) and the simple
chat's New bot dialog — and that name is the display name. The handle is **generated from it**, never typed: the
form shows what it will be under the name field (`Hillary` → `hillary`), and
if that handle is already taken a number is appended (`hillary-2`). Installing
a catalog archetype with a name of your own keeps the archetype's human label
as the agent's **title** (so "Hillary" installed from the Recruitment
Specialist archetype is Hillary, the Recruitment Specialist — and introduces
herself that way); the New agent dialog prefills its **Title** field with
that label so you can change it before installing. Installing an archetype
without a name of your own keeps the archetype's label as the display name
too (so a support archetype installs as "Customer Service Agent", not as its
internal handle). Asking AgentFather to "install the Recruitment Specialist
as Hillary" does the same thing.

## Model and prompt

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | `default:turbo` | `provider:model`, e.g. `anthropic:claude-sonnet-4-6`, `openai:gpt-5.5`, an Olano tier like `olano:pro` — or a **deployment default alias**: `default:turbo`, `default:pro`, `default:max`. An alias follows whatever target is set in **Config → Default models**, so retargeting it there re-points every agent using it with no per-agent edits. New agents start on `default:turbo`, AgentFather included. See [Models and credits](models-and-credits.md). |
| `system_prompt` | string | `""` | The agent's core instructions. Merged with the workspace identity files. |
| `reasoning_effort` | string \| null | `null` | `"low"`, `"medium"` or `"high"`, for models that support it. Ignored otherwise. |
| `model_kwargs` | object | `{}` | Extra parameters passed to the model (e.g. `temperature`). |
| `prompt_mode` | `"full"` \| `"task"` \| `"minimal"` \| `"none"` | `"full"` | How much scaffolding goes into the system prompt. `full` is the everyday value; the narrower modes suit single-purpose agents where a shorter prompt is cheaper and more focused. |
| `vibes_preset` | `"precise"` \| `"warm"` \| `"executive-brief"` \| `"hacker-lab"` | `"precise"` | Baseline tone. `SOUL.md` refines it. |
| `default_verbosity` | `"quiet"` \| `"normal"` \| `"verbose"` | `"normal"` | How much of its work the agent narrates. `quiet` = the answer only; `normal` = the answer plus a note when a turn ran tools silently; `verbose` = full play-by-play. This is the **starting point** for new chats, not a ceiling — a user who runs `/verbosity` pins their own chat and that wins from then on. |
| `prompt_cache` | object | see below | `optimize_boundaries` (default `true`) and `sort_tools` (default `true`). Leave both on unless you are debugging prompt construction. |

**A model string that will be rejected:** `olano:vision` and the Olano image
group are *utility* models the product calls on the agent's behalf. They cannot
be an agent's brain, and setting one as `model` fails to load.

## Tools

| Field | Type | Default | Meaning |
|---|---|---|---|
| `tools` | string[] | `[]` | Toolkits and individual tools to enable, e.g. `["sql", "slack", "stripe"]`. Listing a tool here binds it unconditionally. |
| `disabled_tools` | string[] | `[]` | Remove specific tools that would otherwise be granted automatically. Use this to drop one tool without switching off a whole category. |
| `default_internal_tools` | boolean | `true` | Master switch for every implicit grant — the universal tools, memory, notes, knowledge-graph, workspace context. Turning it off gives you a locked-down agent that only has what `tools` names. |
| `top_level_memory_tools` | string[] \| null | `null` | When set, an **allow-list**: only these memory tools survive. `null` means "all of them". |
| `top_level_rag_tools` | string[] \| null | `null` | Same, for document-search tools. |
| `top_level_graph_tools` | string[] \| null | `null` | Same, for knowledge-graph tools. |
| `top_level_workspace_context` | boolean | `true` | Whether the agent can load extra workspace files on demand. |
| `allowed_actions` | object | `{}` | `{ "action_name": "path/to/script" }` — named scripts the agent may run via `execute_action`. Only bound when per-customer memory is on. |

**Every agent automatically gets a universal set** you do not have to ask for:
self-awareness (`whoami`), time (`get_current_time`), peer messaging
(`talk_to_agent`), outbound
notifications (`notification_send_text` / `_photo` / `_document`), chat
directory lookups, the issue-tracker tools, web search and fetch, document
readers, HTML rendering to PNG / PDF / SVG (`render_html_image` /
`render_html_pdf` / `render_html_svg` — how charts, slides and social graphics
are made, see the `charts` skill), a recoverable trash can, and the
operations tools: the scheduler (`scheduler_*`), heartbeat checks
(`heartbeat_*`), PULSE (`pulse_*`), approvals (`approvals_*`) and the ops queue
(`ops_*`). Keep these unless you have a specific reason not to; several
features assume they exist.

Any of them can be switched off one at a time: untick it on the agent's
**Tools** tab, or name it in `disabled_tools` (for example
`"disabled_tools": ["scheduler_delete", "approvals_approve"]`). Ticking it
again, or removing it from `disabled_tools`, puts it back. Listing it under
`tools` does not remove it, because these are added to every agent whatever
`tools` says. `default_internal_tools: false` turns the whole set off at once.

Two things to know about the operations tools. An agent in Customer Service
mode does not get them (a customer must never see or change your scheduler or
approvals); list one under `tools` if such an agent really needs it. And the
ones that act on held work (`approvals_approve`, `approvals_reject`,
`approvals_defer`, `approvals_clear_expired`, `pulse_accept`, `pulse_undo`,
`scheduler_run_now`, `heartbeat_configure`) only work for an owner: another
agent, an outside caller or a non-admin member asking for them is refused.

**The knowledge-base tools are not in that set.** `kb_search`, `kb_list`,
`kb_add_file`, `kb_add_url`, `kb_create_store`, `kb_sync`, `kb_delete` and
`kb_remove_file` bind only when `tools` names them. An agent without them cannot
search your documents, and the dashboard's Knowledge tab cannot route through
it. Add them to any agent you want answering from the document library.

Tools are grouped into these categories in the dashboard: Self & Time, Agent
Mesh, User Interaction, Web & Documents, Multimodal, Housekeeping, Operations, Cortex,
Memory, Customer Memory, Multi-user Actions & Scheduling, Knowledge (RAG),
Knowledge Graph, Notes, Workspace, Agent Loop, Filesystem & Shell — plus 80+
service toolkits (Slack, Notion, Stripe, SQL, Google, GitHub, HubSpot,
Salesforce, Apollo.io, Lusha, Shopify, Jira, Linear, and many more).

**Multimodal tools are automatic when they can work.** `image_describe`,
`image_generate`, `image_edit`, `text_to_speech` and `audio_to_text` bind
whenever the credentials to run them resolve. On a managed deployment
everything except `text_to_speech` runs on Olano credits with no configuration
at all; a self-hosted install with an Olano API key on Providers → Olano gets
the same. On an install with neither they appear once you set the matching
provider key. `text_to_speech` is the one deliberate exception on every install: voice
replies stay off until you pick a text-to-speech provider under Config →
Speech (on a managed deployment choose *Olano* there to run it on credits).

**Rendering HTML to an image, a PDF or an SVG.** The `render` tools turn HTML
and CSS into a PNG, a PDF or a vector SVG at exact pixel dimensions, using the
browser that ships with every install — no API key, no network, nothing
billed. They are on for every agent (switch them off with `disabled_tools`).

| Tool | What it does |
|---|---|
| `render_html_image` | An HTML document or `.html` file → a PNG at exactly the width and height you ask for. `scale: 2` exports at twice the pixel size for retina. |
| `render_html_pdf` | The same, to a PDF, one page per `break-after: page` block — reports, printable documents, and what a LinkedIn native document (carousel) post needs. |
| `render_html_svg` | Saves one `<svg>` of the rendered page as a standalone vector file — a chart that scales in a slide deck or a design tool. |

All three take `wait_for`, a CSS selector to wait for before capturing, for
pages that draw with JavaScript (the `charts` skill's pages add
`.render-done` when finished). The tools pair with `image_generate`: the
image model paints the background, and anything a person is meant to *read* —
headline, statistic, logo, call to action — is set in CSS, so it comes out
correct every time and a copy change is a re-render rather than a new image.
Each accepts an inline HTML document or a path to an `.html` file, and writes
to `workspace/renders/` unless you pass `save_path`, which must point inside
the agent's workspace or a shared folder it can write to. Data charts go through
the `charts` skill, which builds the page (recipes, or any Vega-Lite spec, in
the clean / midnight / editorial / brand themes) and renders it with these
tools; no plotting library is ever installed. A document may only load local
files from the agent's workspace, its skills and its shared folders (an HTML
path outside them is refused), and remote images, stylesheets and fonts follow
the agent's outbound-access lists. Anything refused is left out of the render
and named at the end of the result.

**Driving a web browser.** When browser control is turned on for the machine
(see [Browser control](feature-guides/browser.md)), the `browser` toolkit gives
an agent a Chrome it can navigate, read, click and type in. `browser_snapshot` lists what
is on the page (headings, links, buttons, fields) with a short ref such as
`e12` next to each element, and `browser_click` / `browser_type` accept that
ref, so the agent acts on exactly the element it saw instead of guessing a
selector. `page_screenshot` takes a one-off screenshot with nothing kept
afterwards.

- **Each agent has its own browser session.** Cookies, site storage and the
  open page belong to one agent; a site an agent signs in to is not signed in
  for any other agent on the box.
- **Sign-ins are kept.** The session's cookies and site storage are saved by
  the deployment, outside every workspace (readable only by the deployment, not
  by the agent or its shell), and restored the next time the agent opens the
  browser, including after a restart. Asking the agent to close the browser and
  forget its profile signs it out everywhere; the agent's **Browser** tab has
  the same button.
- **Idle sessions close themselves.** After `browser.idle_timeout` seconds
  without a browser call (10 minutes by default, set in `config.yaml`) the
  session is saved and closed.
- **A limited number run at once.** Each open session uses a few hundred MB of
  memory, so at most `browser.max_sessions` are open at a time. When the limit
  is reached, the session idle the longest is saved and closed to make room; if
  every session is in use, the agent is told the browser is busy and retries.
- **Outbound access applies to everything the browser loads**, not only the
  address the agent opens. See [Security](#security).
- **Screenshots stay in the workspace.** A `save_path` outside the agent's
  workspace (or a shared folder it can write to) is refused, the same rule
  the agent's file tools follow.
- **Local files.** In `headless` mode the browser can open a file in the
  agent's workspace (a `file://` address), and a page opened that way can load
  other files from the workspace, the agent's skills and its shared folders.
  The live browser and a remote provider run elsewhere and open no local files
  (one-shot screenshots still can, unless the provider is remote). Every other file on
  the machine is refused, including through a link or a shortcut (symlink) that
  points outside, and so are browser-internal addresses such as
  `view-source:` or `chrome:`.

**Private repositories.** Local git — `status`, `add`, `commit`, `diff`,
`checkout`, `log` — runs in the agent's own shell and needs no credential. The
verbs that talk to GitHub go through the `git` toolkit, which runs them with
the connected GitHub account and lands the files in the agent's workspace; the
credential itself never enters the agent's sandbox, so a `printenv` there
shows nothing before, during or after.

| Tool | What it does |
|---|---|
| `git_clone(url, dest, branch, depth)` | Clone a repository into the workspace (`dest` relative to it; `branch` and a shallow `depth` optional). |
| `git_fetch(repo_path, remote)` | Fetch a remote of a repository in the workspace. |
| `git_pull(repo_path, remote, branch)` | Pull into a checkout in the workspace. |
| `git_push(repo_path, remote, branch, set_upstream)` | Push a branch; `set_upstream` for a new one. Always raises an approval card. Force-pushing is refused. |

HTTPS URLs only — an SSH URL is refused with the HTTPS form to use instead. The
account comes from whatever way GitHub was connected (Connections → GitHub, a
sign-in code, a magic link from the chat, or a `GITHUB_TOKEN` in the Vault); a
missing one answers `[missing-credentials]` and names the link to send, never a
request to paste a token. Add `"git"` to `tools` — or ask the agent, which adds
it alongside the GitHub toolkit when you ask it to connect GitHub.

**Bookkeeping.** The `quickbooks` toolkit works a connected QuickBooks Online
company across the four areas Olano's Intuit app is registered for. Connect it
under Connections → QuickBooks (the **environment** field picks a sandbox
company instead of the live one), then add `"quickbooks"` to `tools` — or use
the built-in **QuickBooksAgent** subagent, which carries the whole set.

| Area | Tools |
|---|---|
| Customers | `quickbooks_list_customers`, `quickbooks_search_customers`, `quickbooks_get_customer`, `quickbooks_create_customer`, `quickbooks_update_customer` |
| Invoicing & payments | `quickbooks_create_invoice`, `quickbooks_list_invoices`, `quickbooks_get_invoice`, `quickbooks_send_invoice`, `quickbooks_void_invoice`, `quickbooks_list_estimates`, `quickbooks_create_estimate`, `quickbooks_list_sales_receipts`, `quickbooks_create_sales_receipt`, `quickbooks_list_payments`, `quickbooks_record_payment` |
| Expenses | `quickbooks_list_vendors`, `quickbooks_create_vendor`, `quickbooks_list_bills`, `quickbooks_create_bill`, `quickbooks_pay_bill`, `quickbooks_list_expenses`, `quickbooks_create_expense` |
| Business insights | `quickbooks_profit_and_loss`, `quickbooks_balance_sheet`, `quickbooks_cash_flow`, `quickbooks_ar_aging`, `quickbooks_ap_aging`, `quickbooks_sales_by_customer`, `quickbooks_expenses_by_vendor`, `quickbooks_trial_balance` |

Three behaviours are worth knowing before you ask an agent to post anything:

- **Writes need the right reference id, and there are tools to find them.**
  `quickbooks_list_accounts` returns the chart of accounts (filter it by type,
  e.g. `Expense`) — a bill or an expense needs one to categorise the money, and
  paying a bill needs the bank or credit-card account it comes out of.
  `quickbooks_list_items` returns the products and services an invoice line can
  point at. An invoice left without an item id falls back to the company's first
  active product/service rather than failing.
- **Invoices can carry many lines.** Pass `line_items` a JSON array —
  `[{"amount": 100, "description": "Design"}, {"quantity": 2, "unit_price": 50}]`
  — and it replaces the single amount/description pair. Estimates and sales
  receipts take the same shape; bills and expenses take the same shape with an
  `account_id` per line.
- **Invoices are voided, never deleted.** `quickbooks_void_invoice` keeps the
  document on the books at zero value so the audit trail survives, which is what
  an accountant expects. Updates change only the fields you pass; everything
  else on the record is left alone.

Reports come back as a readable outline rather than raw data, so an agent can
quote a figure straight into a chat message or feed one into the `charts` skill.

**Microsoft 365.** The `microsoft365` toolkit works a connected Microsoft 365
account: Outlook mail and calendar, contacts, To Do, OneDrive and SharePoint
files, Excel workbooks, and Teams. Connect it under Connections → Microsoft 365
(or ask the agent for a sign-in link), then add `"microsoft365"` to `tools`, or
use the built-in **Microsoft365Agent** subagent, which carries the whole set.

| Area | Tools |
|---|---|
| Mail | `ms365_list_messages`, `ms365_get_message`, `ms365_send_mail`, `ms365_create_draft`, `ms365_update_draft`, `ms365_send_draft`, `ms365_reply`, `ms365_forward`, `ms365_update_message`, `ms365_move_message`, `ms365_list_mail_folders`, `ms365_download_attachment` |
| Calendar | `ms365_list_events`, `ms365_get_event`, `ms365_create_event`, `ms365_update_event`, `ms365_delete_event`, `ms365_respond_to_event`, `ms365_find_meeting_times`, `ms365_get_availability`, `ms365_list_calendars` |
| People & mailbox | `ms365_find_people`, `ms365_get_profile`, `ms365_list_contacts`, `ms365_create_contact`, `ms365_set_auto_reply` |
| Files | `ms365_list_drive_items`, `ms365_search_drive`, `ms365_search_files`, `ms365_upload_drive_file`, `ms365_download_drive_file`, `ms365_create_folder`, `ms365_share_drive_item`, `ms365_move_drive_item`, `ms365_delete_drive_item`, `ms365_find_sites` |
| Excel | `ms365_excel_list_sheets`, `ms365_excel_read_range`, `ms365_excel_write_range`, `ms365_excel_add_rows` |
| Teams | `ms365_list_teams`, `ms365_list_channels`, `ms365_send_channel_message`, `ms365_list_chats`, `ms365_read_chat`, `ms365_send_chat_message` |
| To Do | `ms365_list_task_lists`, `ms365_list_tasks`, `ms365_create_task`, `ms365_update_task` |

What to know before asking an agent to act on it:

- **Drafts are the safe default.** `ms365_create_draft` saves a message (or a
  reply or forward that keeps the thread) in Outlook's Drafts folder and returns
  a link to review it there; nothing leaves until `ms365_send_draft`, or until
  you send it yourself from Outlook. Ask for "a draft" when you want to read it
  first.
- **Mail is HTML, and pictures can sit inside it.** Every message, reply,
  forward and draft goes out as an HTML body, so headings, bullet lists, tables
  and links render the way Outlook shows them. A picture can appear inside the
  text in two ways: a hosted image by its `https://` address, or a file from
  the agent's workspace embedded in the message (`inline_images`, referenced in
  the body by its file name), which is how a header banner or a signature logo
  travels with the email. Regular `attachments` take any file type up to 150 MB
  each. Reading works the same way round: `ms365_get_message` lists every
  attachment, including a screenshot someone pasted into the body, and
  `ms365_download_attachment` saves them into the workspace so the agent can
  open or describe them (`include_inline: false` skips the embedded pictures).
- **Meetings send real invitations.** `ms365_create_event` with attendees makes
  Outlook email each of them an invitation, and `online_meeting` adds a Teams
  join link. Changing the time or the attendee list sends an update, and
  deleting a meeting you organize sends a cancellation with your note. When you
  name people rather than addresses, the agent looks them up first
  (`ms365_find_people` searches the people you work with, your organization's
  directory and your contacts), and it can ask Outlook for a time that suits
  everyone (`ms365_find_meeting_times`) before booking.
- **Times follow your mailbox.** An agent reads and writes times in the time
  zone set in your Outlook settings unless you name another one.
- **Anything that reaches other people asks first** under the `safe` and
  `production` security profiles: sending, replying and forwarding mail, sending
  a draft, creating, changing or cancelling a meeting, answering an invitation,
  turning on an automatic reply, uploading, sharing or deleting a file, writing
  to a workbook, and posting to Teams. Drafts, flags, folder moves, contacts and
  To Do tasks do not.
- **Files land in the workspace.** Attachments and downloaded files are saved in
  the agent's `downloads` folder, where its other tools can open them; a
  download can also be converted to PDF on the way.
- **Some features need a work or school account.** Teams, meeting-time
  suggestions and availability, the organization directory, SharePoint, search
  across shared files, and Excel are not available on a personal Microsoft
  account; mail, calendar, contacts, OneDrive files and To Do are.
- **A connection made before these features existed needs one fresh sign-in.**
  It keeps working for what it could already do, and anything newer (drafts,
  mail folders, availability, people search, contacts, To Do, SharePoint and
  shared files, Teams chats) answers that the permission is missing; the agent
  then offers you a sign-in link. Signing in again replaces the connection in
  place. If your organization requires an administrator to approve app
  permissions, the administrator approves the new ones once.

**HubSpot.** The `hubspot` toolkit works a HubSpot CRM account: contacts,
companies, deals, tickets, products, line items, quotes and invoices; notes,
tasks, calls, emails and meetings logged against them; pipelines, owners,
associations, properties and lists; imports and exports; workflows, forms,
marketing and transactional email; blog posts, pages and HubDB. Connect it under
Connections → HubSpot (or ask the agent for a sign-in link), then add
`"hubspot"` to `tools`, or use the built-in **HubSpotAgent** subagent, which
carries the whole set. A private-app token stored in the **Vault** as
`HUBSPOT_ACCESS_TOKEN` still works instead of a connection, but it never renews
on its own.

- **The sign-in adapts to your HubSpot plan.** Contacts, companies, deals and
  owners are part of every plan and are always granted. Everything else (tickets,
  quotes, workflows, CMS, marketing email, …) is granted only when your plan
  includes it and the person signing in has access to it in HubSpot. A tool for a
  feature the connection was not granted answers that a scope is missing instead
  of failing; if your plan does include it, sign in again as a HubSpot user with
  access to that feature.
- **Sign in as a user with the access the agent needs.** HubSpot applies that
  user's permissions to every call the agent makes.

**Apollo.io.** The `apollo` toolkit works an Apollo.io account: searching
Apollo's database for people and companies, enriching a person (work email,
title, employment history) or a company (industry, headcount, revenue, funding,
technologies), job postings and news about a company; the contacts, accounts and
deals saved in Apollo, with their stages, lists, notes and tasks; and sequences
(finding them, enrolling contacts, removing or stopping them, checking the
emails they sent). Add `"apollo"` to `tools`, or use the built-in
**ApolloAgent** subagent, which carries the whole set. It needs an Apollo API
key stored in the **Vault** as `APOLLO_API_KEY`; ask the agent and it sends you
a secure form for it.

- **Use a master key unless you want to limit the agent.** In Apollo (Settings,
  Integrations, API keys) a key is either a master key or limited to the
  endpoints ticked when it was created. A limited key answers "not allowed to
  call this endpoint" for anything else, and listing your team's users or the
  API usage needs a master key. Apollo runs every call as your workspace's
  longest-standing active admin, whoever created the key.
- **Some steps spend Apollo credits.** The people search is free and never
  returns emails or phone numbers. Enriching a person costs a credit when Apollo
  finds them (personal emails and phone numbers cost more), enriching a company
  costs one per company, and company search, job postings and news cost one per
  page. When you ask for one of these, the agent runs it without asking you to
  confirm, then reports the credits used and can show your remaining balance.
  Phone numbers arrive a few minutes later; the agent gives you the rest first
  and fetches them when Apollo has finished.
- **Enrolling contacts in a sequence sends real email.** Only saved contacts can
  be enrolled, so the agent saves a prospect first (matching an existing contact
  instead of duplicating it). Under the Supervised and Locked-down profiles the
  enrollment waits for your approval, as does the agent's general-purpose
  Apollo request, which can reach any endpoint. Searches, enrichment and record
  edits do not ask.

**Lusha.** The `lusha` toolkit works a Lusha account: looking up people and
companies you already know and revealing their emails, phone numbers and company
data (industry, headcount, revenue, funding, technologies, open jobs, parent
companies); finding new prospects and companies by role, seniority, size,
industry, technology, buying intent and recent activity; people and companies
similar to examples you give; the buying committee at a target company; job
changes, promotions, hiring surges, news and intent changes; scores against your
Target Profiles and by buying signals; the companies visiting your tracked
websites; sales calls recorded by Lusha Conversations; Lusha tables; and webhook
subscriptions. Add `"lusha"` to `tools`, or use the built-in **LushaAgent**
subagent, which carries the whole set. It needs a Lusha API key stored in the
**Vault** as `LUSHA_API_KEY` (Lusha dashboard: Enrich, API, Manage API keys);
ask the agent and it sends you a secure form for it.

- **Your Lusha plan decides what works.** The key needs a plan with API access,
  and everything beyond basic lookups (prospecting, lookalikes, signals, scores,
  tables) needs Lusha's v3 API access, which Lusha's account team turns on. A
  call your plan does not cover answers that the account is inactive or lacks v3
  access; tell your Lusha contact. Website visitors need the sites set up for
  tracking in Lusha first.
- **Most steps spend Lusha credits.** Searches and prospecting return previews
  without emails or phone numbers and charge per result; revealing emails or
  phones charges per field and per person; company data, lookalikes, signals,
  scores and reading a table's rows charge too. Listing filter values, signal
  types, Target Profiles and tables is free. The agent runs a step you ask for
  without asking first and reports the credits it used; it can show your
  balance and the price of every action. Phone numbers that Lusha fetches from
  other providers (Data Waterfall, when you have it on) can arrive a little
  later; the agent fetches them when Lusha has finished.
- **Tables act as a person.** Lusha needs the email of a user on your Lusha
  account to create, read or change a table; the agent asks you for it.
- **Some steps wait for approval.** Under the Supervised and Locked-down
  profiles, the agent asks before it creates or changes a webhook subscription
  (Lusha would send your contact and company data to the address it names),
  deletes a subscription, a table or a table column, rotates the webhook
  signing secret, or uses its general-purpose Lusha request, which can reach any
  endpoint; reading the webhook signing secret asks too. Searches, enrichment,
  scores and table edits do not ask.

Gmail (the `google` toolkit) works the same way for evidence and files:

- **Each message shows two times.** *Date* is what the sender's mail program
  wrote; *Received (UTC)* is Gmail's own record of when the message arrived or
  was sent. Cite the second when the time matters, for example when an order
  was placed.
- **Attachments save into the workspace.** A message lists its attachments by
  name, size and type, and `gmail_download_attachments` saves them (all of
  them, or the names you pick) into `downloads/gmail/<message id>/` unless you
  name another folder inside the workspace. Saving the same file twice reuses
  the copy already there. Saving needs no approval: the file stays inside the
  agent's own workspace.
- **Mail written only in HTML** (common for broker and bank notifications) is
  read as plain text instead of coming back empty.
- **New mail can wake the agent.** See [`gmail_watch`](#gmail_watch-wake-on-new-email).

## Subagents

| Field | Type | Default | Meaning |
|---|---|---|---|
| `subagents` | object[] | `[]` | Specialists this agent can delegate to. |
| `builtin_subagents` | list or object | `[]` | Ready-made subagents. `"all"`, `"none"`, or a list of names. The operations manager (`"ops"`) is no longer added by default, since its scheduler, heartbeat, PULSE and approvals tools are on every agent directly; list `"ops"` to add it anyway. |

Each entry in `subagents`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | How the parent addresses it. |
| `description` | string | **required** | What it is for — the parent reads this to decide when to delegate. |
| `system_prompt` | string | **required** | Its instructions. |
| `model` | string \| null | `null` | Inherits the parent's model when unset. Use a cheaper model here for narrow work. |
| `tools` | string[] | `[]` | Its own tools. |
| `skills` | string[] | `[]` | Its own skills. |
| `subagents` | object[] | `[]` | Nested specialists. |
| `enabled` | boolean | `true` | |
| `from_archetype` | string \| null | `null` | Build this subagent from a ready-made archetype instead of writing it out. |
| `system_prompt_extension` | string \| null | `null` | Text **appended** to the subagent's prompt at build time, under an "Additional owner instructions" heading. Made for catalog-sourced subagents: the catalog prompt stays live, your additions ride on top. |
| `backend` | object \| null | `null` | Give the subagent its own filesystem/sandbox. |

Subagents are **internal** to their parent — they are not agents in the roster,
they have no channels of their own, and nobody outside can message them. Use a
subagent for a specialty the parent needs; use a separate top-level agent when
the thing needs its own inbox, schedule or memory.

**Customizing a catalog-sourced member.** A `from_archetype` entry (and a
`builtin_subagents` integration) takes overrides *next to* the reference — any
field you set wins over what the catalog ships, everything else keeps tracking
the catalog:

```json5
subagents: [
  { from_archetype: "sales-coach" },                          // all catalog defaults
  { from_archetype: "sales-engineer", model: "default:pro" }, // stronger brain, same prompt
  {
    from_archetype: "sales-pipeline-analyst",
    system_prompt_extension: "Report figures in EUR. Our quarter ends in February.",
  },
]
```

In the dashboard this is the pencil on any subagent card (**Subagents** tab):
pick a model ("Inherit" keeps the member's own default), read the base prompt,
type additions into **Additional instructions**, or — deliberately, behind a
warning — **detach** and replace the whole prompt, which freezes it against
catalog updates. Team installs offer the same model choice per member in the
New agent dialog before anything is created.

## `org` — who may talk to whom

Covered in depth in [Agent-to-agent and the org chart](feature-guides/agent-to-agent.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `title` | string \| null | the archetype's label | The agent's position — shown under its name on cards, in the detail and chat headers and on the org chart, and told to the agent so it introduces itself by name and describes its role by title. Editable from the **Title** field on the Overview tab as well as here; see [Identity](#identity). |
| `manager` | string \| null | `null` | The agent this one reports to. |
| `reports` | string[] | `[]` | Its direct reports. |
| `coworkers` | string[] | `[]` | Peers at a similar level. Display only. |
| `input_agents` | string[] | `[]` | Agents allowed to message this one. **Empty means no restriction.** |
| `output_agents` | string[] | `[]` | Agents this one may message. **Empty means no restriction.** |
| `input_mode` | `"everyone"` \| `"none"` \| `"list"` \| null | `null` | Explicit inbound policy. `null` infers from the list. |
| `output_mode` | `"everyone"` \| `"none"` \| `"list"` \| null | `null` | Explicit outbound policy. |
| `mutating_from_manager_only` | boolean | `false` | When true, only the manager may trigger actions that change things; other peers get read-only service. |
| `autogenerate` | boolean | `false` | Derive both allow-lists from the hierarchy instead of maintaining them by hand. |

Two rules worth committing to memory: an **empty list means unrestricted**, not
"nobody"; and an agent's **manager and direct reports can always exchange
messages**, whatever the allow-lists say, so declaring a reporting line is
enough to make delegation work.

## `agent_visibility` — who this agent knows about

| Field | Type | Default | Meaning |
|---|---|---|---|
| `mode` | `"none"` \| `"all"` \| `"list"` | `"none"` | Which other agents this one is told exist. |
| `agents` | string[] | `[]` | The list, when `mode: "list"`. |

Visibility is **awareness, not permission**. It controls what the agent is told
about its colleagues; `org` controls what it is allowed to do. An agent that can
see a peer but has no `org` permission still cannot message it.

## Access and lifecycle

| Field | Type | Default | Meaning |
|---|---|---|---|
| `human_only` | boolean | `false` | Refuse all inbound agent-to-agent messages. Use for an agent that only ever takes direction from a person — the deployment operator, for example. |
| `fleet_access` | boolean | `false` | Lets this agent's shell and file tools reach the whole deployment instead of only its own workspace: every agent's folder under the Olano home's `agents/` (read and write: workspaces, `agent.json5` files and connection files, including agents created later), the common skills folder `skills/` (read and write) and the deployment logs in `logs/` (read only). The vault, the conversation databases and `config.yaml` stay out of reach. Meant for AgentFather, which ships with it on, so it can search every agent's files and logs and fix a shared skill in place. Only an administrator can turn it on for an agent: AgentFather cannot grant it to another agent without an administrator in the conversation. Approvals for file writes and shell commands still apply. Changing it rebuilds the agent's sandbox on its next restart. In the dashboard it is the **Fleet file access** switch on the agent's Overview tab (**Access & Safety** card), which asks you to confirm before turning it on. |
| `hidden` | boolean | `false` | An internal/test agent: not booted, and excluded from every listing — the dashboard roster, the org chart, admin and access-management views included. |
| `external_access` | object | all off | Whether systems **outside** the deployment may send this agent messages, over MCP and/or A2A, and the public description they see. Edited on the agent's **External access** tab; see [`external_access`](#external_access-letting-outside-systems-reach-this-agent) and [Beyond the deployment: A2A and MCP](feature-guides/a2a-and-mcp.md). |
| `browser` | object | no rules | This agent's browser settings. `auto_approve` lists narrow exceptions to the approval the browser asks for before sending, posting or submitting; see [`browser`](#browser-actions-that-dont-ask) and [Browser control](feature-guides/browser.md#actions-that-dont-ask). Only admins and the agent's owners can change it. |
| `skills` | string[] | `[]` | Extra directories to load skills from, absolute or relative to the workspace. The agent's own `workspace/skills/` and the shared common skills are always included and do not need listing here. |
| `disabled_skills` | string[] | `[]` | Skills this agent must **not** load, by skill name (the `name:` at the top of its `SKILL.md`, which is also its folder name) — e.g. `["canvas-design", "xlsx"]`. One name per entry; matching ignores capitalisation. A named skill is not described in the agent's prompt, cannot be delegated to, and is ignored by self-learning, but it stays on disk and stays listed (marked *Off*), so it can be switched back on. Naming a common skill takes it away from **this agent only** — every other agent keeps it, which is the reason this is a denylist rather than a deletion. An entry naming a skill that is not installed is harmless and does nothing. Empty (the default) means every discovered skill is loaded. Editable in the dashboard: **Agents → agent → Skills**, the switch on each row. Applies after a restart. |
| `workspace` | string \| null | `null` | Derived automatically. **Do not set this by hand.** |
| `env` | object | `{}` | Per-agent environment values. Non-secret settings only. |

## `mcp_servers` — external tool servers

See [MCP servers](feature-guides/mcp-servers.md) for the full walkthrough.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `url` | string | **required** | The server endpoint. |
| `transport` | `"sse"` \| `"http"` \| `"stdio"` | `"sse"` | How to reach it. `stdio` runs the server as a process instead of connecting over the network — on managed deployments that process runs inside an isolated sandbox container. |
| `enabled` | boolean | `true` | |
| `command` | string \| null | `null` | For `stdio`: the executable to run. |
| `args` | string[] | `[]` | For `stdio`: its arguments. |
| `env` | object | `{}` | For `stdio`: environment for the child process. Values may reference a stored credential as `${vault:NAME}`. |
| `headers` | object | `{}` | Extra HTTP headers — the usual way to pass an API key to an `http`/`sse` server. Values may reference a stored credential as `${vault:NAME}`. |
| `auth` | `"none"` \| `"oauth"` | `"none"` | Use `oauth` for servers that require a sign-in. |
| `oauth_scope` | string \| null | `null` | Requested scope. |
| `oauth_client_id` | string \| null | `null` | For servers that need a pre-registered client. |
| `oauth_client_secret` | string \| null | `null` | Prefer the vault over writing this here. |
| `disabled_tools` | string[] | `[]` | Hide specific tools the server offers. A hidden tool is never offered to the model; only the enabled ones are bound and listed in its prompt. The agent can still see what is hidden through `self_list_mcp_tools` and suggest enabling one when a task needs it. |

## `remote_agents` — external agents over A2A

Agents hosted **outside** this deployment that this agent may hand work to,
over the open Agent2Agent (A2A) protocol. See
[Beyond the deployment: A2A and MCP](feature-guides/a2a-and-mcp.md) for the walkthrough. Listing at
least one enabled entry gives the agent the `a2a_list_agents`,
`a2a_discover_agent`, `a2a_send_message` and `a2a_get_task` tools; an empty
list (the default) means it can reach no external agent at all.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | The handle the agent uses, e.g. `acme-support`. Letters, digits, `-`, `_`; matching ignores capitalisation. |
| `url` | string | **required** | Base URL of the remote agent, e.g. `https://agents.acme.com/support`. Its Agent Card is read from `<url>/.well-known/agent-card.json`. Must be `https://`; hosts on private, link-local or cloud-metadata addresses are always refused. |
| `enabled` | boolean | `true` | Off keeps the entry but removes the agent from reach. |
| `description` | string | `""` | What the remote agent is for, shown to your agent when it lists them. Empty falls back to the remote card's own description. |
| `auth` | `"none"` \| `"bearer"` \| `"api_key"` | `"none"` | How to authenticate. `bearer` sends `Authorization: Bearer <credential>`; `api_key` sends the credential in the header named by `api_key_header`. |
| `credential` | string | `""` | The secret for `bearer`/`api_key`, ideally a vault reference such as `${vault:ACME_A2A_TOKEN}` so it never sits in this file. Resolved per agent (this agent's override, then the shared entry). |
| `api_key_header` | string | `"X-API-Key"` | Header name for `api_key` mode. |
| `timeout_seconds` | integer | `120` | How long one message waits for an answer (5–900). A task still running at the deadline comes back as in progress with an id to poll. |
| `allow_insecure_http` | boolean | `false` | Permit `http://` for a loopback host only (local development). Never for anything on a network. |
| `same_origin_endpoint` | boolean | `true` | Require the service endpoint the remote card advertises to share scheme, host and port with `url`, so a compromised card cannot redirect your credential elsewhere. |

## `external_access` — letting outside systems reach this agent

The **inbound** direction: someone else sending this agent messages. Not to be
confused with `mcp_servers` (servers this agent uses) or `remote_agents`
(agents this agent calls). Edited on the agent's **External access** tab; the
matching home-wide switches live under **Config → External access**
([`external_access:` — letting outside systems reach your agents](config-yaml.md#external_access-letting-outside-systems-reach-your-agents)). Both
flags are off by default; the walkthrough is [Beyond the deployment: A2A and MCP](feature-guides/a2a-and-mcp.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `mcp` | boolean | `false` | Publish the agent as an `ask_<agent>` tool on the deployment's MCP endpoint for platforms that speak the Model Context Protocol (Claude, ChatGPT, Cursor, agent frameworks). Needs the home-wide MCP switch too. Applies on the agent's next restart. |
| `a2a` | boolean | `false` | Publish an Agent Card and JSON-RPC endpoint for agents and platforms that speak the Agent2Agent protocol. Needs the home-wide A2A switch too. Applies on the next restart. |
| `description` | string | `""` | **Required while either flag is on.** One to three plain sentences on what the agent can do for an outside caller, what it needs and what it will not do — the MCP tool description and the A2A card description, i.e. everything a remote caller sees. Written for a stranger; it is *not* the system prompt and is never shown to the agent itself. Up to 2000 characters. A config that turns a flag on without it is refused. |

## `browser` — actions that don't ask

Every send, post, form submit or purchase the agent tries in its browser waits
for a person to approve it. `auto_approve` lists narrow exceptions: each rule
names one site and the kinds of action there that go ahead without asking.
Edited on the agent's **Browser** tab (admins and the agent's owners only);
the walkthrough is [Actions that don't ask](feature-guides/browser.md#actions-that-dont-ask).
Rules apply to the agent's next action, with no restart.

```json5
browser: {
  auto_approve: [
    { site: "linkedin.com", actions: ["send"] },
  ],
},
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `auto_approve` | object[] | `[]` | The rules, at most 20, one per site. Empty means every committing action asks. |
| `auto_approve[].site` | string | **required** | A bare host name such as `linkedin.com` (covers the site and its sub-domains) or `*.example.com` (sub-domains only). No `https://`, path or port. The action must happen on this site and a form must send its data to it. |
| `auto_approve[].actions` | string[] | **required** | One or more of `send` (send, reply, post, publish, invite), `submit` (other forms that save or change something; *Submit*, *Confirm*), `signup` (subscribe, sign up, create account, book, reserve). |
| `auto_approve[].in_background` | boolean | `false` | Also apply in scheduled jobs and background (Cortex) runs. When off, the rule applies only in a conversation started by a person who owns the agent or is an admin. |

Purchases, payments, transfers, donations, deletions, approvals and merges
always ask, whatever a rule says, and no rule applies to a request from another
agent, an outside MCP/A2A caller, a webhook or a watched email.

## `observability`: tracing this agent to Langfuse

Overrides the deployment's tracing settings (`observability` in
[`config.yaml`](config-yaml.md)) for this agent. Every field is empty by
default, which keeps the deployment's value; a value here wins. Only an admin
can change it: an agent's own tools and AgentFather refuse to, unless an admin
is the person asking.

```json5
observability: {
  langfuse: {
    enabled: true,      // trace this agent even if the deployment does not
    capture: "full",    // or "metadata"
    sample_rate: 0.2,   // one turn in five
  },
},
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `langfuse.enabled` | boolean or empty | empty | `true` traces this agent's turns, `false` never traces them, empty follows the deployment. Tracing still needs Langfuse keys. |
| `langfuse.capture` | string or empty | empty | `metadata` or `full`, with the same meaning as in `config.yaml`. Anything else reads as `metadata`. |
| `langfuse.sample_rate` | number or empty | empty | Share of this agent's turns to trace, `0.0` to `1.0`, clamped. |

An agent can also send its traces to a different Langfuse project: an admin
saves that agent's own keys in the Observability settings.

## `channels` — messaging connectors

A list of objects, each with a `type` plus that connector's own settings.
Connector fields vary widely and each is documented in the dashboard's
Connectors tab. **Credentials are never written here** — see
[Channels and connectors](feature-guides/channels-and-connectors.md).

```json5
channels: [
  { type: "telegram", allowed_users: ["123456789"] },
  { type: "slack", channels: ["#ops"] },
]
```

Supported types: `telegram`, `slack`, `discord`, `whatsapp`, `whatsapp_twilio`,
`whatsapp_meta_api`, `signal`, `email` / `imap`, `matrix`, `mattermost`, `sms`, `homeassistant`,
`dingtalk`, `feishu`, `wecom`, `weixin`, `qqbot`, `bluebubbles`, `debox`,
`api_server`, `webhook`.

## `schedule` — recurring work

See [Scheduler and heartbeat](feature-guides/scheduler-and-heartbeat.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `cron` | string \| null | `null` | Standard 5-field cron (minute, hour, day of month, month, day of week), e.g. `"0 9 * * 1-5"` for weekdays at 09:00. The day of week is standard cron: `0` or `7` is Sunday, `1` is Monday; names (`mon-fri`, `mon,thu`) work too. Shorthand for the five fields below. |
| `minute` / `hour` / `day` / `month` / `day_of_week` | string | `"*"` | The fields, when you are not using `cron`. |
| `timezone` | string \| null | `null` | IANA zone, e.g. `"America/Costa_Rica"`. Defaults to the deployment's zone. |
| `message` | string | `""` | What to send the agent when the job fires. Required for a normal job. |
| `name` | string \| null | `null` | A label for the dashboard. |
| `thread_id` | string \| null | `null` | Run in a specific conversation thread. |
| `enabled` | boolean | `true` | |
| `model` | string \| null | `null` | Run this job on a different model than the agent's, as `provider:model` (`"olano:pro"`, `"openai:gpt-6.1-sol"`). `null` follows the agent's configured model. |
| `reasoning_effort` | string \| null | `null` | Reasoning effort for this job's runs, one of the levels the model it runs on accepts (`model` when set, else the agent's). `null` uses the agent's own setting. A level the model does not accept is dropped at run time with a log line; the dashboard and the scheduler tools refuse to save one. |
| `for_each_customer` | boolean | `false` | Fan the job out: run once per end user. Requires multi-user mode. |
| `action` | string \| null | `null` | Run a named entry from `allowed_actions` instead of sending a message. Only valid with `for_each_customer`. |

In fan-out mode the message may use `{customer_key}`, `{platform}` and
`{user_id}` placeholders.

## `heartbeat` and `issues` — inherit-by-default blocks

Both use the same three-state pattern: `null` (or the key omitted) means
**inherit the deployment default from `config.yaml`**; an explicit value
overrides it for this agent only.

`heartbeat`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean \| null | `null` (inherit; on) | Whether the agent wakes up periodically to check `HEARTBEAT.md`. |
| `interval_minutes` | integer \| null | `null` (inherit; 60) | How often. Clamped to 1–1440. |

`issues`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `auto_process` | boolean \| null | `null` (inherit; on) | Whether the agent picks up issues assigned to it on its own. `false` = manual: issues wait for a person to prompt the agent. |
| `poll_minutes` | integer \| null | `null` (inherit; 15) | How often it checks. Clamped to 1–1440. |

`context_window` — how this agent's conversations are bounded inside its
model's context window. Every field is `null` = inherit the home-wide
`context_window:` block in [`context_window:` — compaction and oversized tool results](config-yaml.md#context_window-compaction-and-oversized-tool-results),
which also explains what each one does:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `keep_fraction` | number \| null | `null` (inherit; 0.35) | Share of the window kept verbatim after a compaction, when the window is known. 0.05–0.8. |
| `keep_tokens` | integer \| null | `null` (inherit; 40000) | The same budget in tokens, used when the window is unknown. 2000–2000000. |
| `trigger_fraction` | number \| null | `null` (inherit; 0.85) | When automatic compaction fires, as a share of the window. 0.3–0.98. |
| `trigger_tokens` | integer \| null | `null` (inherit; 170000) | The same trigger in tokens, when the window is unknown. 8000–4000000. |
| `mcp_result_token_limit` | integer \| null | `null` (inherit; 5000) | MCP results larger than this are saved to a workspace file and replaced by a preview. `0` = no cap. |

## `gmail_watch`: wake on new email

Starts a turn for the agent when new mail matching a Gmail search reaches its
connected Gmail. Off by default. Set it on the agent's **Overview** tab, in the
**Gmail watch** card, which also shows the agent's Google connection and the
watch's last check; AgentFather can set it too. Changes apply immediately, no
restart.

**It needs the agent's Google connection.** The watch reads the mailbox through
the same Google sign-in the Gmail tools use (**Connections**, per agent or
shared). Without one it reads nothing: the card shows *Not connected* with a
**Connect Google** button, and the bell raises one notification that clears
itself on the first check after you connect. Mail that arrived before the
connection is not replayed. If Google refuses the saved sign-in later, the card
and the bell say so the same way. If the mailbox cannot be connected to Google
at all, add it on the **Connectors** tab as an **Email** connector instead: it
checks the mailbox over IMAP with an app password and wakes the agent for every
new message, without a search filter.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Turns the watch on. The agent needs the Google connection (Connections) with Gmail access. |
| `query` | string | `""` (watches `in:inbox`) | A Gmail search, typed exactly as in Gmail's search box, for example `from:(@broker.example) has:attachment`. |
| `poll_minutes` | integer | `5` | How often Gmail is checked, 1 to 1440. New mail is picked up within this many minutes. |
| `prompt` | string | `""` | What the agent should do with the new messages. Empty tells it to read each one, save the attachments it needs and act on its instructions. |
| `max_per_check` | integer | `10` | At most this many messages go into one turn, oldest first, 1 to 50. The rest wait for the next check. |

```json5
gmail_watch: {
  enabled: true,
  query: "from:(@broker.example) has:attachment",
  poll_minutes: 5,
  prompt: "Match each confirmation to its Trade ID and record the execution evidence.",
}
```

How it behaves:

- **Switching it on never replays your inbox.** The first check only records
  the time; mail that arrives after it is delivered. Changing `query` starts
  over the same way.
- **One message wakes the agent once.** The ids already delivered are kept
  in the workspace, so a restart or a busy inbox never hands the same message
  over twice.
- **A failed turn is retried.** If the agent's turn fails, the same messages
  are offered again on the next check, up to three times, after which they are
  skipped so one bad message cannot wake the agent forever.
- **Email is treated as a stranger's words.** The turn starts with the list of
  new messages (sender, subject, received time, ids) and the agent reads them
  with its Gmail tools. Low-risk actions go ahead; anything that needs approval
  waits in the dashboard's Approvals list, whatever the agent's security
  profile says about scheduled jobs. The turn cannot change the agent's own
  settings or use owner-only tools.
- These runs appear in the agent's chat list with its other scheduled runs.

Gmail can also push new mail instantly, but only through a Google Cloud
Pub/Sub topic you would have to create and authorize. The watch avoids that
setup at the cost of a few minutes' delay.

## `webhooks`

See [Webhooks](feature-guides/webhooks.md) for examples of both directions.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | **required** | A label. |
| `direction` | `"outbound"` \| `"inbound"` | `"outbound"` | |
| `enabled` | boolean | `true` | |
| `url` | string | `""` | Outbound: where to POST. Required for outbound, must be `http(s)`. |
| `events` | string[] | `["*"]` | Outbound: which events to send. |
| `secret` | string \| null | `null` | Outbound: HMAC signing secret. Supports `${VAR}`. Never returned by the API. |
| `headers` | object | `{}` | Outbound: extra headers. |
| `timeout_seconds` | number | `10.0` | |
| `max_retries` | integer | `2` | |
| `rate_limit_per_minute` | integer | `60` | `0` = unlimited. |
| `verify_tls` | boolean | `true` | |
| `block_private_addresses` | boolean | `true` | Refuse to deliver to internal addresses. Leave on. |
| `prompt` | string | `""` | Inbound: what the agent should do with each payload. |
| `respond` | boolean | `false` | Inbound: run synchronously and return the agent's reply in the HTTP response. |
| `accumulate_thread` | boolean | `false` | Inbound: one persistent conversation for all payloads, rather than a fresh one each time. |
| `max_body_bytes` | integer | `256000` | Inbound: reject larger payloads. |

Outbound event names: `message.received`, `message.sent`, `agent.started`,
`agent.stopped`, `agent.error`, `task.scheduled`, `task.completed`,
`tool.called`, `approval.requested`, `budget.exceeded`, or `*` for all.

## `memory`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `markdown` | boolean | `true` | Keep `MEMORY.md` and daily logs. |
| `rag` | boolean | `false` | Also index memory for meaning-based search. |
| `rag_collection` | string | `"lionclaw_memory"` | Where indexed memory is stored. The default keeps Olano's previous name, lionclaw, so indexes built by earlier versions stay readable. |
| `self_learning` | boolean | `true` | Distil conversations into durable memory in the background. |
| `self_learning_interval` | integer | `3600` | Seconds between distillation passes. |
| `per_customer` | boolean | `false` | Give every end user their own isolated memory. Turns on multi-user mode (see [Multi-user: two different features](feature-guides/multi-user.md)). |

## `customer_service` — per-end-user isolation

For a shared agent that many people talk to. See
[Multi-user: two different features](feature-guides/multi-user.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | Turn on per-end-user isolation. Mirrors `memory.per_customer`. |
| `prompt_extension` | string | `""` | Extra instructions for handling many users. Blank uses a sensible default. |
| `user_scheduling` | boolean | `false` | Let end users schedule their own reminders. |
| `max_jobs_per_user` | integer | `3` | Cap per user. |
| `min_job_interval_minutes` | integer | `60` | Minimum spacing between a user's jobs. |
| `connections` | boolean | `false` | Let end users connect to each other, with consent. |
| `max_connections_per_user` | integer | `25` | |
| `max_connection_sends_per_day` | integer | `20` | |
| `connection_invite_ttl_hours` | integer | `168` | How long an invite stays valid. |
| `connection_contact_info` | string | `""` | Shown to users in connection flows. |
| `user_credentials` | boolean | `false` | Let end users store their own credentials for the agent to use on their behalf. |
| `customer_credential_names` | string[] | `[]` | The allow-list of credential names they may store. |

`user_scheduling`, `connections`, `user_credentials` and per-customer schedule
fan-out all require multi-user mode; setting one without it makes the config
fail to load rather than silently doing nothing.

## `message_policy` — message length caps and silence rules

For an agent whose chat address is public. Everything here is off unless you
set it, so an existing agent is unaffected. Edit it in **Agents → your agent →
Multi-user → Message policy**.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `max_input_chars` | integer \| null | `null` | Longest inbound message accepted, in characters of the sender's own text. Checked before any model call, so an over-long message costs nothing. `null` = unlimited. |
| `on_oversize` | `"reject"` \| `"truncate"` \| `"ignore"` | `"reject"` | What happens past that limit. `reject` sends a short notice and runs no turn; `truncate` trims to the limit and answers the shortened text; `ignore` drops it with no reply. Only applies while `max_input_chars` is set. |
| `oversize_notice` | string | `""` | Your wording for the `reject` notice. Blank uses the built-in line, which names both the limit and the length actually sent. |
| `ignore_patterns` | string[] | `[]` | Regular expressions. A message matching any of them is dropped before any model call — free and exact. Matching is case-insensitive and unanchored; use `^…$` to require the whole message. An invalid expression makes the config fail to load rather than silently never matching. Keep them simple: a pattern with nested repeats (`(a+)+`) can take seconds per message, and only the first 20,000 characters of a message are scanned. |
| `ignore_rules` | string[] | `[]` | Plain-language conditions the **model** judges, e.g. `"The message is not written in English"`. Added to the system prompt; the agent replies with a silence marker and the runtime sends nothing. Handles what a regex cannot — language, tone, intent — at the cost of one model turn per ignored message. |
| `ignore_notice` | string | `""` | What an ignored sender sees. Blank = true silence (nothing is sent). Text = that one line instead. Applies to patterns, rules, and `on_oversize: "ignore"`. |
| `max_output_tokens` | integer \| null | `null` | Hard ceiling on one reply, translated to whichever parameter your provider uses. `null` = the provider's own maximum. |

Four things worth knowing before you rely on these:

- **Slash commands are never screened.** `/new`, `/help`, `/model` and the rest
  pass through whatever you set, so a tight limit or a broad pattern cannot
  lock you out of your own bot.
- **Only external conversations are screened** — messenger channels, dashboard
  and REST chat, and the OpenAI-compatible API. Scheduled runs, heartbeats and
  background thinking are not, so a capped support desk can still write a long
  nightly report.
- **A capped reply stops mid-sentence and is not continued.** Without a cap,
  Olano automatically asks the model to carry on when a reply is cut short;
  with one, that retry is switched off, because otherwise every capped reply
  would cost several extra turns — the opposite of what a cap is for.
- **On reasoning models the output ceiling counts thinking as well as the
  answer**, so a low value can return an empty reply. Keep it above ~1024
  there, or set `reasoning_effort` to `none`/`low`.

Patterns and rules are the cheap and the flexible halves of the same job.
Anything a regular expression can already decide belongs in `ignore_patterns`,
where it costs nothing; `ignore_rules` is for the judgements only a model can
make. Pairing rules with `max_input_chars` and `max_output_tokens` keeps the
turn they cost small.

```json5
message_policy: {
  max_input_chars: 500,
  on_oversize: "reject",
  ignore_patterns: ["^\\W*$", "\\b(buy|cheap) followers\\b"],
  ignore_rules: [
    "The message is not written in English",
    "The message is nonsense or just testing the bot",
  ],
  ignore_notice: "",          // stay completely silent
  max_output_tokens: 300,
}
```

## `monetization`

See [Subscriptions and monetization](feature-guides/subscriptions-and-monetization.md).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | |
| `providers` | `"stripe"` \| `"telegram_stars"` list | `[]` | How people pay. |
| `trial_days` | integer | `3` | Free trial length. |
| `channels` | string[] | `[]` | Which channels the paywall applies to. |
| `gate_groups` | boolean | `false` | Also require a subscription in group chats. |
| `plan.price_cents` | integer | `500` | Display amount. |
| `plan.currency` | string | `"usd"` | |
| `plan.interval` | `"month"` \| `"year"` | `"month"` | |
| `plan.stripe_price_id` | string | `""` | Empty means Stripe is off. |
| `plan.stars_amount` | integer | `0` | `0` means Telegram Stars is off. |
| `plan.product_name` | string | `"Subscription"` | |
| `plan.offer_copy` | string | see default | The pitch shown to users. |
| `plan.success_url` / `plan.cancel_url` | string | `""` | Where checkout returns to. |

Payment provider secrets go in the vault, never in this block.

## `security`

See [Privacy and security](feature-guides/privacy-and-security.md) for what each control actually does.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `profile` | `"safe"` \| `"developer"` \| `"production"` | `"developer"` | Preset that fills in the unset fields below. |
| `trust_level` | integer 0–4 | `2` | `0` observer, `1` assistant, `2` collaborator, `3` autonomous, `4` developer. |
| `hitl_enabled` | boolean | `false` | Require human approval before risky actions. |
| `hitl_shell` | boolean | `false` | Approve every shell command. |
| `hitl_write_tools` | boolean | `false` | Approve every tool that changes something. |
| `hitl_auto_approve_low_risk` | boolean | `true` | Let clearly-safe actions through without asking. |
| `hitl_timeout_seconds` | integer | `300` | How long a request waits before expiring. |
| `pii_scan_input` | boolean | `true` | Scan what comes in for personal data. |
| `pii_scan_output` | boolean | `true` | Scan what goes out. |
| `pii_scan_peer` | boolean | `false` | Also scan agent-to-agent traffic. |
| `pii_action` | `"warn"` \| `"redact"` \| `"block"` | `"warn"` | What to do on a hit. |
| `input_scan` | boolean | `true` | Detect prompt-injection attempts in incoming text. |
| `output_scan` | boolean | `true` | Screen outgoing text. |
| `web_scan` | boolean | `false` | Also screen fetched web content. |
| `shell_guard` | boolean | `true` | Block dangerous shell patterns. |
| `skill_audit` | boolean | `true` | Audit skills before running them. |
| `skill_audit_level` | `"permissive"` \| `"strict"` \| `"paranoid"` | `"strict"` | |
| `credential_scrub` | boolean | `true` | Strip anything that looks like a secret from output. |
| `ssrf_protection` | boolean | `true` | Block requests to internal network addresses. |
| `ssrf_allowed_domains` | string[] | `[]` | The older spelling of the top-level `egress_allow` (below). Still honoured — the two are combined. |
| `ssrf_blocked_domains` | string[] | `[]` | The older spelling of the top-level `egress_deny`. Still honoured — the two are combined. |
| `rate_limits` | object | see [Privacy and security](feature-guides/privacy-and-security.md) | Per-tool and per-message caps. |
| `tools` | object | `{}` | Fine-grained per-tool permissions. |

Setting `profile` fills in any of `trust_level`, `pii_action`, `input_scan`,
`output_scan`, `hitl_enabled`, `hitl_auto_approve_low_risk` and
`skill_audit_level` you have not set yourself. Anything you set explicitly wins.

### `egress_allow` / `egress_deny` — which internet destinations the agent may reach

Two **top-level** lists in `agent.json5` (not under `security`), edited in the
agent's **Advanced** tab:

```json5
egress_allow: ["api.github.com", "example.com"],   // empty = inherit the deployment's list
egress_deny:  ["pastebin.com"],                    // empty = add nothing
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `egress_allow` | string[] | `[]` | Domains this agent's web and HTTP tools may reach. Empty inherits the deployment's list (**Config → Outbound access**); when that is empty too, every public destination is allowed. A non-empty list here **replaces** the deployment's allow list for this agent. |
| `egress_deny` | string[] | `[]` | Domains this agent's tools must never reach. **Added to** the deployment's deny list — an agent can block more, never less. |

**Format.** One bare domain per entry, like `api.github.com`. A domain covers
its subdomains, so `example.com` also matches `files.example.com`. A leading
`*.`, an `https://` prefix, a port or a path are stripped when the config
loads; anything that is not a domain name or an IPv4 address fails the config
to load, the way a bad `sandbox.memory` does. Deny is checked before allow, so
a denied domain stays blocked even when it is also allowed.

**What it governs.** Everything the agent can reach the internet with:

- its web and HTTP tools, the headless browser, the HTML render tools, and
  calls to remote agents.
  For the browser that means every page a click, a link or a script opens,
  every image, script and request a page loads, and redirects: an allowed page
  that redirects to a refused site is stopped before the browser follows the
  redirect, and a page whose images or scripts come from refused sites loads
  without them;
- every integration toolkit (Stripe, Salesforce, Slack and the rest), whichever
  service they talk to;
- **its sandbox shell** — a `curl`, `pip install`, `npm install` or `git clone`
  inside a shell command.

A blocked destination comes back as a readable error (`Host 'x' is not in the
allowlist.`) rather than a silent failure or a hang, so the agent can tell you
what it could not reach. Internal and private-network addresses are blocked
regardless of these lists (`ssrf_protection`), and your deployment can always
reach its own control plane whatever you put in the lists — otherwise a typo
could take a box offline with no way to tell you.

**When it applies.** The very next tool or shell command after you save — no
restart, no container rebuild. An agent needs every service it actually uses on
its allow list: a support agent that reads email and writes to your CRM needs
both, and a missing entry shows up as that one integration failing. If an agent
suddenly cannot install a package, check whether its allow list covers the
package index it is downloading from.

**One limit worth knowing.** Confining the sandbox shell needs the host's
firewall. On a deployment where that is unavailable — an unusual setup without
the required privileges — the shell is still *pointed* at the policy and
ordinary tools obey it, but a program written to ignore that setting could go
around it. The runtime writes a warning at startup naming this, and the agent's
tools are unaffected either way. Managed deployments always have it.

## `budget`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `daily_usd` / `weekly_usd` / `monthly_usd` | number \| null | `null` | Spending caps. `null` = no cap. |
| `alert_at` | number | `0.8` | Warn at this fraction of the cap. |
| `fallback_model` | string \| null | `null` | Switch to a cheaper model when the cap is hit, instead of stopping. |
| `hard_stop` | boolean | `false` | Stop the agent entirely at the cap. |

## `pulse` — proactive suggestions

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | |
| `in_channels` | boolean | `false` | Push them to messaging channels. |
| `email_digest` | boolean | `false` | Send a daily digest. |
| `digest_time` | string | `"09:00"` | Local time for the digest. |
| `quiet_hours_start` / `quiet_hours_end` | string | `""` | e.g. `"22:00"` / `"08:00"`. Empty = no quiet hours. |
| `max_items_per_session` | integer | `5` | |
| `max_items_per_day` | integer | `20` | |
| `generated_items_per_cycle` | integer | `3` | |
| `pulse_item_ttl_hours` | number | `168.0` | How long a suggestion stays relevant. |
| `min_turns_for_generation` | integer | `4` | Don't generate from very short conversations. |
| `curation_cadence` | `"session"` \| `"daily"` \| `"weekly"` | `"daily"` | |
| `curation_interval_hours` | number | `24.0` | |
| `focus_topics` | string[] | `[]` | Bias suggestions toward these. |
| `sources` | string[] | `["agent","user","approvals","ops"]` | Where suggestions may come from. |

## `cognition` — the Cortex engine, per agent

Every boolean here is three-state: `null` (or omitted) means **inherit the
deployment default**. That includes the master `enabled` switch, so "off for
the fleet, on for one agent" works.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean \| null | `null` (inherit; off) | Master switch for this agent. `null` follows the deployment's `cognition.enabled`, which is off unless someone turned it on, so a new agent runs no Cortex. `true` runs Cortex for this agent even while the fleet is off; `false` keeps it off even while the fleet is on. |
| `fast_model` / `deep_model` | string \| null | `null` (inherit) | Models for light and heavy background work. |
| `autonomous` | boolean \| null | `null` (inherit) | Whether it acts on its own initiative. |
| `auto_approve` | boolean \| null | `null` (inherit) | Whether its own actions skip the approval queue. |
| `intensity` | `"relaxed"` \| `"balanced"` \| `"aggressive"` \| null | `null` (inherit; relaxed) | How much background work it does. `null` follows the deployment's `cognition.intensity`, `"relaxed"` unless changed. |
| `approval_required` | string[] | `["external_send","irreversible","identity_edit"]` | Categories that always need a human, whatever `auto_approve` says. `identity_edit` is any change to SOUL.md, AGENTS.md or IDENTITY.md; `external_send` is anything that sends something outside the deployment (email, calendar, posts, messages to other agents); `irreversible` is moving money or overwriting a stored secret. Applies both to the Fully Autonomous sweep of the approval queue and to tool calls made during a background run — a held item waits in **Pulse → Approvals** with its usual Approve / Reject buttons. Set it to `[]` for an agent whose owner wants everything unattended, or list only the categories to keep; unknown names are ignored. |
| `self_learning`, `brainstorming`, `idea_generation`, `prototyping`, `skills_optimization`, `workspace_optimization`, `memory_optimization`, `pulse` | boolean \| null | `null` (inherit) | Individual capabilities. |
| `deep_self_learning` | boolean | `true` | Use the deep model for distillation. |
| `memory_garden_cron` | string \| null | `null` (inherit) | When to tidy memory (merge duplicate facts in MEMORY.md, roll up old daily logs; snapshotted and undoable from Pulse). Standard 5-field cron, e.g. `"30 4 * * *"`. **Leave it `null` to follow the deployment's `intensity` preset** — once a day around 04:30 at every intensity, with the exact minute spread out per agent so a fleet does not all start at once. An explicit value is used exactly as written and is never spread. |
| `pulse_agent_cron` | string \| null | `null` (inherit) | When to generate Pulse suggestions. Standard 5-field cron. `null` follows the `intensity` preset (daily at 09:00 on `relaxed`, every 12 h on `balanced`, every 6 h on `aggressive`), spread per agent; an explicit value is used as written. |
| `idea_expand_cron` | string \| null | `null` | Reserved for scheduled idea research — currently has no effect. Ideas are expanded when you ask for it or approve a Pulse suggestion; leave this `null`. |
| `workspace_optimize_cron` | string \| null | `null` (inherit) | When the self-optimization pass over the agent's identity and behavior files runs (snapshotted and undoable from Pulse). Standard 5-field cron. `null` follows the `intensity` preset (Sundays at 04:00 on `relaxed`/`balanced`, every third day on `aggressive`), spread per agent; an explicit value is used as written. |
| `skill_librarian_cron` | string \| null | `null` (inherit) | When the learned-skill library is tidied — duplicates merged, survivors polished (snapshotted and undoable). Standard 5-field cron. `null` follows the `intensity` preset (Sundays at 04:30 on `relaxed`/`balanced`, every third day on `aggressive`), spread per agent; an explicit value is used as written. |
| `max_deep_runs_per_day` | integer | `12` | Cost ceiling on heavy background work. |
| `promote_threshold` | integer \| null | `null` (inherit; 8) | Score from 1 to 10 at or above which the Rate & Promote phase moves an item it recommends promoting to the next funnel stage on its own (brainstorm → idea → prototype → project). Only applies while Fully Autonomous (`auto_approve`) is on; otherwise every candidate waits as a card in **Pulse → Approvals**. Whole number 1 to 10; out-of-range values are pulled to the nearest end. `null` follows the deployment's `cognition.promote_threshold`. |
| `max_promotions_per_run` | integer \| null | `null` (inherit; 1) | How many items one Rate & Promote run may promote. Each promotion is a deep run and also counts against the daily cap. Whole number 0 to 5; `0` rates without ever promoting. |
| `max_self_scheduled_tasks` | integer \| null | `null` (inherit; 6) | How many follow-up steps this agent may have pending at once from `cortex_schedule`, the tool it uses to continue a prototype or project after a run (they appear in **Scheduler** named `Cortex: <item>`). Two per item and at most hourly regardless. Whole number 0 to 20; `0` turns self-scheduling off. |
| `auto_discard` | boolean \| null | `null` (inherit; on) | Whether a Fully Autonomous run may discard an item the reviewer rated 3 or lower with a "discard" recommendation. Reversible: the folder stays and **Restore** brings the item back. Has no effect while `auto_approve` is off. |
| `worker_max_iterations` | integer \| null | `null` (inherit) | Step budget for each background reasoning run (Think phases, self-learning, the optimizers). Counted in internal steps — roughly 4 per round of thinking plus tool use, so the shipped default of 100 allows about 25 rounds per run. A run that exceeds it stops with a "hit its step limit" error and its work is discarded; raise this if that error keeps appearing, lower it to cap the cost of any single run. Accepts a whole number from 10 to 400; a value outside that range is pulled to the nearest end rather than rejected. **Leave it unset (or `null`) to inherit the deployment-wide value** set in Cortex → Autonomy under "All agents" — that is what most agents should do. Caps one run — how *many* runs happen per day is governed by `intensity` and the agent's `budget` — and a run also stops at `worker_timeout_seconds`, whichever comes first. Editable in the dashboard: **Cortex → Autonomy → Step budget**. |
| `worker_timeout_seconds` | integer \| null | `null` (inherit) | Wall-clock limit for each background reasoning run, in seconds. A run that exceeds it stops with a timeout and its partial work is discarded. Accepts a whole number from 30 to 7200; the shipped default is 1200 (20 minutes). **Leave it unset (or `null`) to inherit the deployment-wide value.** Raise it whenever you raise `worker_max_iterations` — a large step budget behind a small time limit simply fails on time instead, losing the same work under a different name. Editable in the dashboard: **Cortex → Autonomy → Time limit**. |
| `force_tier` | `"fast"` \| `"deep"` \| null | `null` | Pin background work to one tier. |
| `enable_memory_gardener`, `enable_pulse_agent`, `enable_idea_expansion`, `enable_skillsmith`, `enable_workspace_optimizer`, `enable_skill_librarian` | boolean | `true` | Individual background workers. |

## `wallet` — on-chain operations

Off unless you need it.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `false` | |
| `mode` | `"cold"` \| `"hot"` | `"cold"` | `cold` can read but not sign. |
| `chains` | string[] | `["ethereum"]` | |
| `hot_mode_lock_minutes` | integer | `30` | Auto re-lock after this long. |
| `lock_on_session_end` | boolean | `true` | |
| `spending_limits` | object | `{}` | Per-chain caps. |
| `require_approval` | `"always"` \| `"high_value_only"` \| `"never"` | `"always"` | |

## `commands` — slash commands

| Field | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | object | `{}` | Only the deviations from the defaults, e.g. `{ "model": false }`. |
| `custom` | object[] | `[]` | Your own commands. |

Each custom command has `name` (lowercase, `[a-z0-9][a-z0-9_-]*`, must not
collide with a built-in), `prompt` (supports `{args}`), and an optional
`description`. This governs chat surfaces and the dashboard.

What you set here is also **what the messengers advertise**. Each connector's
slash menu — Telegram's `/` autocomplete, Discord's slash-command list — carries
exactly the built-ins this agent exposes plus its custom commands, and `/help`
lists the same set. Edits apply **immediately, without restarting the agent**;
a messenger may cache its own autocomplete for a minute (on Telegram, reopen the
chat to refresh it), but typing a command always follows the current setting.

Two naming caveats for custom commands. Telegram's menu accepts only lowercase
letters, digits and `_`, so a hyphenated `/daily-report` still works when typed
but will not be listed there — name it `daily_report` to have it appear.
Discord accepts hyphens, but skips a name longer than 32 characters.

## `shared_paths` — sharing files between agents

| Field | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | **required** | The path on the host. |
| `target` | string | **required** | Where it appears for the agent. Normalised under `/shared/`. |
| `mode` | `"ro"` \| `"rw"` | `"ro"` | |
| `persistence` | `"session"` \| `"persistent"` | `"persistent"` | |
| `subjects` | object[] | `[{type:"agent",name:"self"}]` | Who gets it: `{type: "agent"\|"subagent"\|"all_subagents", name}`. |

## `backend` — where the agent's tools actually run

| `type` | What it is |
|---|---|
| `local_shell` **(default)** | Real filesystem plus a shell, rooted at the agent's workspace. What you want almost always. |
| `filesystem` | Files without a shell. Add `access_mode: "read"` for a read-only agent. |
| `state` | In-memory only — no real files. Testing. |
| `store` | A namespaced key-value store. |
| `composite` | Route different paths to different backends. |
| `lionclaw_docker` | Run the agent's tools in a container (the value keeps Olano's previous name, lionclaw). Options: `persistent`, `image`, `network` (`"none"` \| `"bridge"` \| `"host"`), `snapshot_on_stop`, `memory`, `cpus`. See the note below before choosing it. |
| `daytona`, `modal`, `runloop`, `agentcore`, `langsmith` | Hosted sandbox providers. |

`local_shell` and `filesystem` take `root_dir` (default `"workspace"`).

**Choosing `lionclaw_docker`.** Most agents should stay on `local_shell`: on a
managed deployment it *already* runs in the isolated container, so naming the
docker backend by hand buys nothing and costs you the shorthand the dashboard's
Environment dropdown understands. Reach for it only when you want to control the
container settings directly.

Whichever you pick, the container is only provided when the deployment has
sandbox execution enabled. Without it the agent is downgraded to files-without-a-shell
rather than being given a shell on the host — so `execute` and the file tools go
away together. If an agent's file or shell tools stop working, check
**sandbox** in the agent's diagnosis: it names the container, or the reason
there isn't one.

On a managed deployment three of the options are pinned and your value is
ignored (a warning says so at startup): `network` cannot be `"host"`, `image` is
the deployment's own sandbox image, and `persistent` is forced on. On your own
self-hosted box all three are honoured as written.

`mounts` is accepted by the config but **does nothing** — what the container
can see is the agent's workspace plus any shared paths you have configured.
`egress_allow` and `egress_deny` on this block are the older home of the agent's
outbound lists: they are still read, and combined with the top-level
`egress_allow` / `egress_deny` described in [`security`](#security), but they
govern the agent's **web tools**, not the sandbox shell — a `curl` inside
`execute` is decided by `network` and your host firewall, not by these lists.
Move them to the top level of `agent.json5`, which is where the dashboard edits
them; a config that still sets them here logs a reminder at startup.

**`sandbox` — this agent's share of the machine.** A top-level block, separate
from `backend`, that applies wherever this agent's shell runs in a sandbox:

```json5
sandbox: { memory: "8g", cpus: 4 }
```

`memory` is a size like `4g` or `2048m` (minimum `6m`); `cpus` is a number of
cores, `0.1`–`64`, where `0.5` means half a core. Set one and the other still
follows the deployment default, so raising an agent's memory does not quietly
pin its CPU. Omit the block entirely and the agent inherits the deployment
answer — normally 25% of the machine's RAM and 50% of its cores (see the
`sandbox` key in [the `config.yaml` reference](config-yaml.md#root-keys)). Use this for
the one agent that runs heavy builds or data jobs; the limits are ceilings, so
an agent that sits idle costs nothing regardless of what you set here.
Changing a value rebuilds that agent's sandbox on its next start: its workspace
survives, anything installed by hand inside the sandbox does not.

## Runtime knobs

| Field | Type | Default | Meaning |
|---|---|---|---|
| `acp_enabled` | boolean | `false` | Expose an HTTP endpoint so external code can drive this agent. Binds to localhost with **no authentication** — reach it over an SSH tunnel, never publish the port. For an authenticated remote surface use an inbound webhook instead. |
| `acp_port` | integer | `8765` | Agents sharing a port share one server. |
| `iteration_budget` | integer | `0` | Cap on reasoning steps per turn. `0` = no cap. |
| `truncation_retry_limit` | integer | `3` | Retries when a reply is cut off. |
| `tool_error_retry_limit` | integer | `3` | Circuit breaker for a repeatedly failing tool: the turn stops once the agent has made the identical failing call (same tool, same arguments) this many times within one turn. Every failure is handed back to the agent with the tool's error output, so it can fix the arguments or the cause and try again; the breaker only stops a call that is repeated unchanged. Identical calls the agent sends together in one step count as one attempt, so the agent always sees an error before the breaker can stop it. One failure before the limit, the failing result itself carries a warning that one more identical failure ends the turn. A successful `edit_file` or `write_file` resets the count: a test command that fails, gets a fix, and fails again is a new attempt, not a repeat. A command's full output is always saved to a log under the workspace (`.olano/artifacts/shell/`) when it is too long to show inline, and the result names the path; for a failing command the inline preview keeps the error and traceback lines rather than the first and last lines. `0` disables the breaker. |

## What makes a config fail to load

Olano refuses to load a broken config rather than booting a half-configured
agent. The common causes:

- **Legacy keys.** `sandbox_backend`, a plain string `backend:`, or
  `security.sandbox_mode` / `shell_use_docker` / `docker_image` /
  `docker_network`. Use the `backend` object instead. `sandbox` is a live key
  again, but only as a block of resource limits — `sandbox: true` or
  `sandbox: "docker"` (the old on/off spelling) is still refused.
- **A utility model as a brain.** `olano:vision` or the image group in `model`,
  `cognition.fast_model` or `cognition.deep_model`.
- **Multi-user features without multi-user mode.** `user_scheduling`,
  `connections`, `user_credentials`, or a schedule entry with
  `for_each_customer`, when `customer_service.enabled` / `memory.per_customer`
  is off.
- **An invalid ignore pattern.** A line in `message_policy.ignore_patterns`
  that is not a valid regular expression — caught on save, so a rule can never
  silently match nothing.
- **A dangling archetype reference.** A `from_archetype` naming something that
  does not resolve.
- **A malformed custom command name**, or one that collides with a built-in.
- **A schedule entry with neither `message` nor `action`**, or `action` without
  `for_each_customer`.
