"""Creator monetization: charge an agent's END-USERS for access.

This package adds a **reusable, channel-agnostic** subscription layer on top of
the runtime. It is distinct from the platform billing on cloud.olano.ai:
that bills the agent *owner* (a Clerk org) for hosting; this bills the agent's
*end-users* (the people who DM a Telegram bot) on the owner's behalf.

Design in one paragraph: the existing ``allowed_users`` allowlist answers *"is
this the trusted owner?"*; this layer adds an **entitlement gate** that answers
*"may this untrusted end-user get a model turn?"*. Owners always bypass. A
first-seen end-user gets a free trial; when it lapses they must subscribe via a
pluggable :class:`~olano.monetization.providers.base.PaymentProvider`
(Stripe Checkout and/or Telegram Stars). Per-agent Stripe keys come from the
credential vault, so every creator uses their own account (no platform rake).

Entry points:
- :func:`olano.monetization.gate.evaluate_access` — the pure decision.
- :func:`olano.monetization.service.build_subscription_offer` — assemble an
  offer (Stripe URL + Stars invoice) to present to a blocked user.
- :func:`olano.monetization.service.apply_entitlement_update` — flip stored
  state from a verified payment event.
- :func:`olano.monetization.store.get_entitlement_store` — the SQLite store.
"""

from __future__ import annotations
