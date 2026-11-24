# Olano Cloud

`cloud.olano.ai` is the managed service: instead of installing and maintaining
Olano yourself, you deploy an instance there and Olano runs it for you.

**What you can do there:**

- **Deploy** a dedicated Olano instance in a few minutes. You choose the size —
  instances come in a range of CPU, memory and disk configurations — and you get
  your own dashboard at your own address.
- **Scale** an instance up as your workload grows, and buy additional disk space
  when you need it.
- **Stay current.** New Olano releases are made available to your instance, and
  you are notified when an update is ready, so you decide when to take it rather
  than discovering a version drift months later.
- **Take snapshots** of an instance, so you have a restore point before a risky
  change.
- **Encrypted at rest.** New instances keep all of their data on an encrypted
  volume that unlocks itself at boot (standard custody, [Privacy and security](feature-guides/privacy-and-security.md)).
  The console's Security tab shows the state of the volume; the recovery key,
  the one part only you hold, is created from the instance's own dashboard
  under **Config → Disk encryption**.
- **Buy credit packs** on top of what your subscription includes, for months
  where your agents do more.
- **Run a self-hosted install on credits.** Create an **API key** and buy
  **account credits**: they belong to your account, never expire, and any
  install you paste the key into spends them (see
  [Models and credits](models-and-credits.md#olano-credits-on-a-self-hosted-install)).
- **Watch usage** — what your agents consumed, by agent and over time.
- **Manage billing and your subscription** — plans, payment method, invoices.
- **Organizations.** Deployments belong to an organization, so a team can share
  access to the same instances.

Everything in this guide applies identically to a cloud deployment: the same
dashboard, the same `agent.json5`, the same features. The differences are that
Olano handles the infrastructure, Olano-managed models are available out of the
box on credits, and your plan's entitlements may hide dashboard sections your
deployment does not include.

Current plans, sizes and pricing are on `cloud.olano.ai` — they change as the
offering evolves, so check there rather than relying on a figure written down
somewhere else.
