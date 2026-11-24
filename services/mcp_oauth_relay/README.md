# Olano MCP OAuth Relay

> **Reference service.** This is the canonical, standalone definition of the
> relay HTTP + HMAC contract, kept here alongside the droplet client
> (`olano/core/mcp_relay.py`) so the two stay testable in lockstep. The
> **production, multi-tenant relay** — durable Postgres store + per-tenant
> secrets, run once on `auth.olano.ai` — lives in
> [`olano-ai/cloud`](https://github.com/olano-ai/cloud) under
> `services/mcp_oauth_relay/`. It speaks the identical contract, so this client
> needs no changes. If you change the signing scheme, change it in **both**.

A tiny always-on service that lets **domain-less** olano deployments (a bare
cloud droplet with only a public IP) complete browser-based MCP OAuth. It owns
the single HTTPS redirect URI every provider needs and shuttles the short-lived
authorization `code` back to the droplet that started the flow.

## Why it exists

OAuth providers reject redirect URIs that aren't HTTPS with a trusted cert, and
you can't get a CA cert for a raw IP. Rather than give every customer droplet a
domain, you run **one** relay on a domain you own; all droplets point
`LIONCLAW_MCP_RELAY_URL` at it.

**Privacy:** the relay only ever sees the opaque OAuth `state` and `code`. The
token exchange and storage happen on each droplet, keyed per `(agent, server-url)` —
so customer accounts and tokens never touch this service, and two agents (or two
customers) authorizing the same MCP stay fully isolated.

## HTTP contract

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/mcp/register` | HMAC | Droplet announces a pending `state` |
| `GET` | `/mcp/callback` | public | Provider redirect target (the one URI you register) |
| `GET` | `/mcp/pending` | HMAC | Droplet polls; gets the `code` once |
| `GET` | `/healthz` | — | Liveness |

HMAC = `HMAC-SHA256(secret, "\n".join(parts))` over `(action, instance_id, state, ts)`,
with a fresh unix `ts` (replay window 300s). This mirrors
`olano/core/mcp_relay.py` exactly.

## Deploy (DigitalOcean droplet + Caddy, ~10 min)

1. Point a DNS record at the droplet, e.g. `auth.olano.ai → <droplet-ip>`.
2. Install deps and run the app:
   ```bash
   pip install fastapi uvicorn
   export LIONCLAW_MCP_RELAY_SECRET="$(openssl rand -hex 32)"   # keep this
   uvicorn app:app --host 127.0.0.1 --port 8900
   ```
   (Use a systemd unit or `pm2`/`supervisor` to keep it running.)
3. Put Caddy in front for automatic HTTPS (edit the hostname in `Caddyfile`):
   ```bash
   caddy run --config Caddyfile
   ```
4. Register `https://auth.olano.ai/mcp/callback` as the redirect URI in any
   provider that requires pre-registration (HubSpot, etc.). DCR providers
   (Notion, Linear, Sentry, …) pick it up automatically.

## Point a olano deployment at it

On each droplet, set the **same** secret and the relay URL:

```bash
export LIONCLAW_MCP_RELAY_URL="https://auth.olano.ai"
export LIONCLAW_MCP_RELAY_SECRET="<the same secret from step 2>"
```

(Or store the secret in the vault as `LIONCLAW_MCP_RELAY_SECRET` and set
`mcp_relay_url` in `~/.olano/config.yaml`.) Then an agent with the
`olano_authorize_mcp_server` tool (AgentFather has it by default) can hand the
user a magic link over any channel and finish the connection hands-free.

## Scaling / hardening notes

- The store is in-memory (fine for one process). For HA, back it with Redis
  (`SETEX` keyed by `state`) behind the same endpoints.
- v1 uses one shared secret across droplets; `state` is high-entropy so
  cross-tenant polling requires knowing another droplet's secret **and** its
  state. For stronger isolation, issue a per-deployment key and have the relay
  check a set of allowed keys.
