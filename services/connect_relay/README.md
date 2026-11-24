# Olano Connect Relay (reference)

The domain-side half of Olano's **official connector** flow — OAuth for the
built-in toolkits (Microsoft 365, Salesforce, …) using OAuth apps *you* own,
so a box clicks *Connect* without its user creating a developer app. Sibling
of the MCP relay (`services/mcp_oauth_relay`), deliberately not part of it:

| | MCP relay (`/mcp/*`) | Connect relay (`/connect/*`, here) |
|---|---|---|
| Shuttles | the authorization `code` | the **exchanged token bundle** |
| Code exchange | on the box | **here** (the client secret never reaches a box) |
| Refresh | on the box | **here** (stateless `POST /connect/refresh`) |
| Secrets held | none | the OAuth client secrets you configure |

Both flows share one box identity and the same frozen `"\n".join(parts)`
signing contract under different action names (`connect_register` /
`connect_pending` / `connect_refresh` — a signature for one flow can never
authorize the other). Refresh has no OAuth state, so its state slot carries
`sha256_hex(refresh_token)`, binding the signature to that exact token. The
golden vector is `tests/fixtures/relay/connect_signing_golden.json`, committed
identically to `olano-ai/cloud`.

## Deploy

```bash
pip install fastapi uvicorn httpx
export LIONCLAW_MCP_RELAY_SECRET="$(openssl rand -hex 32)"   # same secret as the MCP relay
export CONNECT_RELAY_PROVIDERS='{
  "microsoft365": {
    "client_id": "…", "client_secret": "…",
    "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    "client_auth": "post", "pkce": true, "params": {"tenant": "common"}
  }
}'
uvicorn app:app --host 127.0.0.1 --port 8901   # run from this directory
```

Put it behind the same HTTPS front as the MCP relay (one Caddy, two upstream
path prefixes) — boxes default their connect-relay URL to the MCP relay host
(`LIONCLAW_MCP_RELAY_URL`), overridable with `LIONCLAW_CONNECT_RELAY_URL` for
a split deployment. Register `https://<relay-host>/connect/callback` as the
redirect URI on every OAuth app you configure.

On the box side, the official client id for each provider comes from the
`connectors` catalog; a self-hoster without the cloud catalog can keep using
bring-your-own clients (the dashboard's classic connect form) — this service
is only for running your own *official-app* fleet.

## Provider entries

`CONNECT_RELAY_PROVIDERS` is a JSON object keyed by the olano provider id.
Fields per entry:

| Field | Meaning |
|---|---|
| `client_id` / `client_secret` | your OAuth app |
| `token_url` | the provider's token endpoint; may contain `{param}` placeholders |
| `client_auth` | `post` (secret in the body, default) or `basic` (HTTP basic) |
| `pkce` | mint/require a PKCE verifier (default false) |
| `token_headers` | extra exchange/refresh headers (e.g. Reddit's `User-Agent`) |
| `params` | allowed template params with defaults, e.g. `{"tenant": "common"}` |

Security posture: the token URL comes **only** from this config — a box can
never supply one — and params are charset-checked before ever touching the
template, so the secret is only POSTed where you configured. Unknown params
are refused, not dropped. States are readable only by the `instance_id` that
registered them; bundles are delivered exactly once and expire in
`STATE_TTL` (15 min).

This reference keeps everything in memory with one shared secret. The
multi-tenant production edition — Postgres-backed registry + state store,
per-tenant Ed25519 keys, AES-256-GCM-sealed secrets and bundles, admin <!-- gitleaks:allow -->
console — is `olano-ai/cloud` `services/mcp_oauth_relay/connect_*.py`,
deployed at `https://auth.olano.ai`.
