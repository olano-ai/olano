# Webhooks

Two directions, both configured in the agent's `webhooks` list and manageable
from its **Webhooks** tab.

**Outbound — tell an external system what happened.**

```json5
{
  name: "ops-pager",
  direction: "outbound",
  url: "https://hooks.example.com/olano",
  events: ["agent.error", "budget.exceeded"],
  secret: "${WEBHOOK_SIGNING_SECRET}",
  rate_limit_per_minute: 30,
}
```

Payloads are signed with HMAC-SHA256 so the receiver can verify they came from
you. Delivery retries with backoff, and delivery to internal network addresses
is blocked by default. Webhooks re-register automatically after a server
restart.

**Inbound — let an external system give the agent work.**

```json5
{
  name: "stripe-events",
  direction: "inbound",
  prompt: "This webhook receives Stripe billing events. Summarise what happened, and if a payment failed, draft a dunning email.",
  respond: false,
  accumulate_thread: false,
}
```

Each inbound webhook gets its own unguessable ingress URL. Whatever the sender
POSTs — JSON, form data, plain text — is appended to your `prompt` and handed to
the agent, so you do **not** need to know or control the sender's payload
format. That is the point: you describe what the data means and what to do with
it, and the agent handles the shape.

- `respond: true` runs the agent synchronously and returns its reply in the HTTP
  response. Use for something waiting on an answer.
- `respond: false` (default) acknowledges immediately and works in the
  background.
- `accumulate_thread: true` keeps one running conversation across all payloads
  instead of starting fresh each time.

Both directions keep a delivery history, and the **Test** button sends a sample
(outbound) or simulates an ingest (inbound) so you can check the wiring before
going live.
