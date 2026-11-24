#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Runs ON the EC2 box. Installs Docker, writes the Caddyfile with the real host,
# and brings up the headless Lionclaw backend + Caddy TLS proxy.
#
#   cd ~/olano
#   SSLIP_HOST=api-acme.olano.run bash deploy/bootstrap-remote.sh       # production (deploy/dns.sh first)
#   SSLIP_HOST=203-0-113-10.sslip.io bash deploy/bootstrap-remote.sh   # dev/test only
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SSLIP_HOST="${SSLIP_HOST:?set SSLIP_HOST, e.g. api-acme.olano.run (customers) or 203-0-113-10.sslip.io (dev)}"
COMPOSE="deploy/docker-compose.prod.yml"

# ── 1. Docker engine + compose plugin ────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "▶ Installing Docker…"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
# Use sudo for this session (group change needs a new login to take effect).
DC() { sudo docker compose -f "$COMPOSE" "$@"; }

# ── 2. .env sanity ───────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  echo "✗ .env missing — copy your keys to ~/olano/.env (needs ANTHROPIC_API_KEY)." >&2
  exit 1
fi
if grep -qE '^\s*LIONCLAW_DASHBOARD_TOKEN=.+' .env; then
  echo "! Removing LIONCLAW_DASHBOARD_TOKEN from .env (breaks cookie-auth frontend)."
  sed -i '/^\s*LIONCLAW_DASHBOARD_TOKEN=/d' .env
fi

# ── 2b. MCP OAuth relay (shared, fleet-wide redirect URI) ─────────────────────
# When a relay secret is supplied (LIONCLAW_MCP_RELAY_SECRET — e.g. forwarded by
# the deploy orchestrator, or already in .env), wire the central relay so
# pre-registration providers like HubSpot use the one registered
# https://<relay>/mcp/callback shared across the fleet. No secret → no-op (this
# headless box keeps whatever redirect target .env already defines).
# shellcheck source=deploy/env-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/env-lib.sh"
ensure_relay_wiring .env || true

# ── 3. Caddy host config (rsync-excluded; survives re-ships + re-ups) ─────────
echo "▶ Configuring Caddy for https://${SSLIP_HOST}"
echo "LIONCLAW_HTTPS_HOST=${SSLIP_HOST}" > deploy/caddy.env
# Optional ACME contact override (the Caddyfile has a default). An email is
# what activates Caddy's ZeroSSL fallback when Let's Encrypt is rate-limited.
if [ -n "${LIONCLAW_ACME_EMAIL:-}" ]; then
  echo "LIONCLAW_ACME_EMAIL=${LIONCLAW_ACME_EMAIL}" >> deploy/caddy.env
fi

# ── 4. build + up ────────────────────────────────────────────────────────────
echo "▶ Building + starting (first build pulls many providers — be patient)…"
DC up -d --build

echo "▶ Waiting for the API to answer locally…"
for i in $(seq 1 60); do
  if sudo docker exec olano curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
    echo "✓ Runtime API is up"; break
  fi
  sleep 5
done

# ── 5. Quick-setup: seed home + workspace + agent + admin + vault ─────────────
# Idempotent, safe to re-run. With no credentials in .env it generates a
# one-time admin password (printed once, kept in the olano home under
# run/initial-admin-password until changed) and a vault passphrase (kept in
# the vault's boot-key file). Set LIONCLAW_ADMIN_EMAIL / LIONCLAW_ADMIN_PASSWORD
# / LIONCLAW_VAULT_PASSWORD in .env to choose them yourself.
# When .env carries LIONCLAW_ADMIN_PASSWORD the runtime already ran the same
# provisioning at its own startup, before its API answered — this pass then
# only retries a failed step and refreshes the packs. A non-zero exit names
# the step that failed; the admin step itself never aborts the run.
echo "▶ Running quick-setup inside the container (idempotent)…"
# --skip-catalogs: the server is already up (we blocked on its API above) and
# populates these itself — built-in skills via run_startup_upgrades, the cloud
# skills + archetype catalogs via CatalogSyncService ~20s after boot. Repeating
# them here re-downloaded the same ~8 MB in the foreground on every deploy.
sudo docker exec olano olano quick-setup --yes --skip-catalogs || \
  echo "! quick-setup returned non-zero (may already be set up) — continuing."

echo
DC ps
cat <<DONE

────────────────────────────────────────────────────────────────────────────
  Up. Verify TLS from your laptop (Caddy may need ~30s to issue the cert):
    curl -sS https://${SSLIP_HOST}/api/status

  quick-setup already created the admin account and a working 'merli' agent.
DONE
# Never a password: this banner lands in the install log (see env-lib.sh).
print_login_hint .env
echo "────────────────────────────────────────────────────────────────────────────"
