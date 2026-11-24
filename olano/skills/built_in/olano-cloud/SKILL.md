---
name: olano-cloud
category: platform
tier: light
description: "Olano Cloud (cloud.olano.ai) and how a deployment is paid for: what the managed service does (deploy, scale, updates, snapshots, disk, organizations, billing), the plan ladder and how to move up it, Olano credits versus your own provider keys, the olano:* model tiers, which services run on credits, what happens when credits run out and how to buy more, where usage and spend are read (Overview, Insights, the cloud usage page, budgets), and disk encryption on a managed instance (standard custody, the owner's recovery key from Config > Disk encryption, what the card's states mean). Use when someone asks about plans, upgrading, credits, billing, costs, usage, 'which tier should I use', 'why did this stop working' after a credit warning, snapshots, disk space, cloud versus self-hosted, whether their data is encrypted, the recovery key, 'sealed' or 'custody'. Never quote plan names, prices, machine sizes, agent ranges or credit rates: send people to cloud.olano.ai, where the current figures live."
trigger: A question about plans, credits, billing, usage, cost, upgrading, snapshots, disk, disk encryption, the recovery key, 'is my data encrypted', 'sealed', 'custody', or Olano Cloud itself.
---

# Olano Cloud, credits and usage

`cloud.olano.ai` is the managed service: instead of installing and maintaining
Olano, the owner deploys an instance there and Olano runs it. Everything in
the platform works identically — same dashboard, same files, same features —
so this skill is only about what the cloud adds and how it is paid for.

**Numbers change; this skill does not carry them.** Plan names, prices,
machine sizes, recommended agent ranges, credit allowances and per-service
rates all live on cloud.olano.ai and in the deployment's own cloud card.
Point there. Never quote a figure from memory, and never say which provider
or model sits behind a managed tier — routing is deliberately opaque so it can
improve without anyone's config changing.

## What the owner can do on cloud.olano.ai

- **Deploy** a dedicated instance in minutes, with its own dashboard at its
  own address, and more than one if the organization needs them.
- **Scale up** an instance as the workload grows; **add disk** when it fills.
- **Stay current** — new releases are made available and the owner decides
  when to take an update, from the dashboard.
- **Snapshots** — a restore point before a risky change; some are kept
  automatically, more can be enabled.
- **Credits** — see the balance, buy credit packs on top of what the plan
  includes.
- **Usage** — what the agents consumed, by service and over time.
- **Billing** — plan, payment method, invoices; each deployment is its own
  subscription.
- **Organizations** — deployments belong to an organization so a team shares
  access.

The box side of this is **Admin → Deployment** in the dashboard: the plan,
what the plan entitles (some tabs and features are hidden on plans that do
not include them), resource limits versus current use, and the credits card
with a link to the deployment on cloud.olano.ai.

## The plan ladder, without the numbers

Plans form a ladder in three families:

1. **Shared instances** — the entry family. Each step up gives more CPU,
   memory and disk, includes more credits, and comfortably runs more agents.
   Moving up within this family is an in-place resize: the deployment keeps
   its files and settings.
2. **Dedicated instances** — reserved hardware for larger fleets and heavier
   scheduled work, with an included storage volume. Reachable in place only
   from the smallest shared plans; from elsewhere it is a new deployment with
   the old one's data restored.
3. **Private AI** — GPU instances for running models on the owner's own
   hardware. Chosen at deploy time and tied to a region; not an upgrade
   target.

Rules the cloud enforces: plan changes are upgrades (a smaller plan means a
fresh deployment), never across families, and never onto a smaller disk than
the deployment already has. When a change is blocked the Plan panel says why.

How to think about sizing, in words: one shared instance of the recommended
entry size is right for a first agent or two doing real work. What fills an
instance is how much runs at the same moment — conversations at once, agents
working in parallel, heavy scheduled jobs — not how many agents exist, so a
roster of mostly-idle agents fits a much smaller plan than the same roster
kept busy all day. Size for the busiest hour, not the average one. Signs a
deployment has outgrown its plan are slow boots, memory pressure on the
Overview page, the disk card turning amber, and scheduled jobs queuing behind
each other. Each plan's recommended agent range is on the pricing page, and it
is a sizing guide, not a cap: any plan can hold as many agents as the owner
wants to create.

## Two ways to pay for models — two books, never mixed

- **Olano credits** price Olano-managed models and services and nothing
  else. A managed deployment ships with a credit balance, needs no provider
  account, and every managed call is charged from it.
- **Your own keys (BYOK)** — the owner's own provider account bills those
  calls. Olano only *estimates* what they cost, in the owner's currency, and
  never charges credits for them.

A deployment can use both at once: some agents on `olano:*` tiers, others on
a provider key. The dashboard shows each figure in exactly one book — credits
for managed usage, currency for BYOK, and never converts between them.

A self-hosted install can run on credits too, with an **Olano API key**: the
owner creates one on cloud.olano.ai (API keys), buys account credits there
(they never expire), and pastes the key on **Providers → Olano** or in the
first-run wizard. From then on the box behaves like a managed one for billing:
the `olano:*` tiers, image generation and editing, vision, transcription, web
search and page fetching all run on that account's credits, agents on the
default tiers move to the managed models at their next start, and Overview
and Insights show the credit book. Removing the key moves them back. Without
a key, provider keys are the only route. The account's balance and keys live
on cloud.olano.ai (Billing → Account credits, API keys), not on any
deployment card; `magic_link_open_dashboard("providers")` is the route to the
paste field. An agent never asks for the key in chat and never reads it.

## The managed tiers

Agents on a managed deployment pick a tier, not a model:

| Tier | Use it for |
|---|---|
| `olano:turbo` | the default: fast, inexpensive, right for most conversational and tool-driven work |
| `olano:pro` | reasoning-heavy work — analysis, planning, code, multi-step research; costs more per call |
| `olano:max` | the strongest tier, for the hardest problems; reserve it and say why |
| `olano:auto` | let the platform pick per request |

`default:turbo` / `default:pro` / `default:max` are aliases the deployment
maps to its own defaults (Providers tab). Behind each tier the platform routes
to whatever serves it best at the time — do not name or guess the model. A
bigger tier spends credits faster; when switching a model with
`self_set_model`, say so.

Other services that run on credits on a managed deployment, with no key to
configure: seeing images (vision), generating and editing images,
transcription, web search and page fetching, and — once an operator turns it
on in Config → Speech — voice replies. An owner who prefers their own key for
any of these switches it deliberately; a key set for something else is never
used to bill their account.

**LinkedIn outreach** (the `linkedin_outreach` toolkit) is a paid add-on, not
a credit charge: on a managed deployment the owner switches it on in the Olano
Cloud console (the instance's page, Plan tab); on a self-hosted box running on
an Olano API key the owner buys it for the account on cloud.olano.ai (Billing,
LinkedIn outreach for your API keys). One seat per LinkedIn account to connect,
either way. On top of that each visit, follow, connection request, message,
comment or reaction costs a small amount of credits; reading the inbox and
posts is free. When a LinkedIn tool says the add-on is off or every seat is
taken, relay exactly that and the place it names; do not retry. Report the
`Credits charged` line each action returns, and offer
`linkedin_disconnect_account` when the owner stops using outreach on an agent
(it frees the seat). An owner with their own Unipile workspace needs neither the
add-on nor credits: once `UNIPILE_API_KEY` and `UNIPILE_DSN` are in the Vault
(collect each with `magic_link_request_secret`), the same tools run on that
workspace and say so.

## Running out of credits

When the balance is spent, managed calls stop with a message saying so and a
**Buy credits** action that opens the deployment's card on cloud.olano.ai (on
a self-hosted box running on an Olano API key, the account's Billing page).
Agents on the owner's own keys keep working meanwhile. Purchased packs never
expire; the credits a plan includes reset with the billing period. A managed
service that is *temporarily unavailable* is a different message: the
platform retries on its own and the call resumes, nothing to buy. A
self-hosted box whose Olano API key was *refused* is a third message: the key
was revoked or mistyped, and the owner pastes a valid one on Providers →
Olano; nothing retries it.

If you are the agent hitting this, relay the message as it is, name the Buy
credits route, and stop. Do not retry in a loop and do not suggest pasting a
provider key into the chat.

## Where usage and spend are read

| Question | Where |
|---|---|
| How much is left, what did today cost | **Overview** — Credits card and Services usage card |
| Which agent, which thread, which tool spent it; trend over time | **Insights** — usage and cost analytics, drill-down per thread and call, daily journal, reports |
| Usage the cloud has billed, per service | cloud.olano.ai → the deployment → Usage |
| Ceilings and warnings | Config → **Usage budgets** (`usage_budgets:` in `config.yaml`, per agent with `budget` in `agent.json5`) |
| Disk | **Overview** Disk card, Config → Disk monitor |

Insights is computed on the box from its own records; the cloud is the
authority for the billed balance. For managed calls the customer-facing
figure is credits and call counts — never tokens or the model behind the
tier.

No agent tool reads these numbers today. When someone asks you for a usage
report, send them to Insights (or `magic_link_open_dashboard("overview")` from
a phone) rather than estimating.

## Cloud versus self-hosted

Same product. The cloud adds the infrastructure, updates, snapshots, the
plan's included credits and entitlements; a self-hosted install is installed
and maintained by its owner, has no cloud card and no plan gating, and pays
for models either with provider keys or with an Olano API key spending the
owner's account credits (above). A cloud deployment can still use the owner's
own keys, and a self-hosted one can be migrated to the cloud by deploying
there and restoring the home directory.

The same Olano API key also lets a self-hosted install finish **MCP server
sign-ins through Olano's sign-in service**, with no domain or certificate of its
own: the box registers itself a few seconds after the key is pasted, and a
revoked key stops it. It covers MCP servers only. **Connect with Olano's app**
on the Connections tab (Google, Microsoft 365 and the rest) stays a cloud
feature; on a self-hosted install those services connect with the owner's own
OAuth app. If an MCP sign-in on a self-hosted box says there is nowhere to
return to, the fix is that key (or a public https address for the box).

## Disk encryption and the recovery key

A managed instance keeps everything it stores (agents, memory, files,
conversations, the vault, the database) on an encrypted volume that unlocks
itself when the instance boots. This is standard custody: the data is
protected against disclosure from stolen disks, snapshots, backups and
offline storage access; the instance decrypts at boot using Olano-operated
key infrastructure, and Olano's production control plane retains the
technical capability to authorize key release. A self-hosted install has no
such volume, and the card below never appears there.

The one thing the owner holds alone is the **recovery key**: a second key
that opens the volume without Olano's key service, for example to read the
data from a disk image if the instance or Olano is gone. It is created from
the instance's own dashboard, **Config → Disk encryption**, never from the
cloud site and never by you: the instance generates it, shows it exactly
once, and asks the owner to type it back before it counts. Nothing can show
a key again afterwards, Olano included.

What the card shows and does:

- **Custody** (the claim above) and **Encrypted volume** (an identifier, not
  a secret).
- **Recovery key**: *No recovery key yet* with **Create recovery key**;
  *Pending confirmation* (a key was shown and not yet typed back; it expires
  after fifteen minutes; **Confirm the shown key** or **Cancel pending key**;
  no other key can be created meanwhile); *Active* with **Replace recovery
  key**, which makes a new key that takes over only once it is confirmed, so
  a lost key is replaced while the instance runs and stops working at that
  moment.
- **Header backup**: whether a copy of the volume's encryption header (the
  keyslots, never the data) is held off the instance, and which generation;
  it is what lets the recovery key open the volume again if the header on
  the disk is damaged.
- **Encryption service**: the version that answers these actions, or
  *Unavailable*: the data stays encrypted and the instance keeps working,
  only the recovery-key actions pause; **Refresh** after a minute, a restart
  of the instance from the cloud console if it persists, support after that.

How to help:

1. Someone asks whether their data is encrypted, or what "sealed" or
   "custody" means: answer with the claim above, in those words, and point
   at **Config → Disk encryption** (`magic_link_open_dashboard("config")`
   from a phone) and at the cloud console's Security tab, which shows the
   same state per instance.
2. Someone wants a recovery key, or lost theirs: send them to the card. You
   cannot create, read or replace a key on their behalf, and you must never
   ask for one or accept one in chat. A lost key is replaced from the card;
   until then the volume opens through Olano's key service alone.
3. The card is missing: the instance is self-hosted or predates encrypted
   volumes; encryption is decided when an instance is created and cannot be
   switched on afterwards.
4. Something on the card reads *Unavailable*, or a key shows *Pending
   confirmation* for longer than fifteen minutes: the steps above; the
   instance's data is never at risk in either state.

## Answering a cloud question

1. Say what the feature does and where it is (the cloud site or the
   Admin → Deployment card).
2. For anything with a number — price, size, credits, agent ranges, rates —
   point at cloud.olano.ai and the deployment card; do not estimate.
3. Offer the route the person is on: from a phone, a
   `magic_link_open_dashboard("overview")` link to the dashboard; from the
   dashboard, the tab.
