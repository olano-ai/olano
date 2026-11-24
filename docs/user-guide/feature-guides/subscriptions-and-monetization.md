# Subscriptions and monetization

Charge end users for access to an agent. Set it up on the agent's
**Monetization** tab or in the `monetization` block.

Two payment routes, usable together: **Stripe** (set `plan.stripe_price_id`;
empty means Stripe is off) and **Telegram Stars** (set `plan.stars_amount`;
`0` means it is off). `trial_days` (default 3) gives newcomers a free window.
`channels` limits the paywall to specific connectors; `gate_groups` extends it
to group chats.

Payment provider secrets go in the vault, never in the config file. The plan
fields here are what users *see* — your payment provider remains the source of
truth for what is actually charged.

Monetization pairs naturally with multi-user mode ([Multi-user: two different features](multi-user.md)):
you need per-user isolation for per-user subscriptions to mean anything.
