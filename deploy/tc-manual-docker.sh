#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# tc-manual-docker.sh — install Lionclaw on an EXISTING Tencent Cloud CVM
# using the Docker all-in-one stack.
#
# Run ON the CVM after you manually created it, SSH'd in, cloned this repo, and
# copied/edited .env:
#
#   cd ~/olano
#   cp .env.example .env   # add provider keys first
#   bash deploy/tc-manual-docker.sh
#
# Optional overrides:
#   PUBLIC_IP=1.2.3.4
#   SSLIP_HOST=acme.olano.run   (production — run deploy/dns.sh ensure first; default is <ip>.sslip.io, dev only)
#
# Tencent security group requirements: inbound TCP 80/443 must be reachable for
# Caddy HTTPS. SSH 22 only needs to be reachable from you.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

[ -f deploy/bootstrap-allinone.sh ] || die "Run from a Lionclaw checkout with deploy/bootstrap-allinone.sh."
# Incremental .env repair (create from .env.example when missing; the delegated
# bootstrap-allinone.sh handles the LIONCLAW_PUBLIC_URL pin + token strip).
# shellcheck source=deploy/env-lib.sh
. "$SCRIPT_DIR/env-lib.sh"
ensure_env_file .env || die "Could not create .env (no .env.example?)."

SUDO=sudo
[ "$(id -u)" -eq 0 ] && SUDO=""

if ! command -v curl >/dev/null 2>&1; then
  say "Installing curl..."
  $SUDO apt-get update
  $SUDO apt-get install -y curl ca-certificates
fi

detect_public_ip() {
  if [ -n "${PUBLIC_IP:-}" ]; then
    printf '%s\n' "$PUBLIC_IP"
    return 0
  fi

  curl -fsS --connect-timeout 2 \
    http://metadata.tencentyun.com/latest/meta-data/public-ipv4 2>/dev/null \
    || curl -fsS --connect-timeout 5 https://checkip.amazonaws.com 2>/dev/null \
    || curl -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null
}

PUBLIC_IP="$(detect_public_ip | tr -d '[:space:]')"
[ -n "$PUBLIC_IP" ] || die "Could not detect public IP. Re-run with PUBLIC_IP=x.x.x.x."

SSLIP_HOST="${SSLIP_HOST:-${PUBLIC_IP//./-}.sslip.io}"

if command -v docker >/dev/null 2>&1 && ! $SUDO docker compose version >/dev/null 2>&1; then
  warn "Docker exists but the compose plugin is missing or broken."
  say "Repairing Docker/Compose with get.docker.com..."
  curl -fsSL https://get.docker.com | $SUDO sh
fi

say "Installing Lionclaw Docker all-in-one at https://${SSLIP_HOST}"
# The delegated bootstrap wires the MCP OAuth redirect target (central relay when
# a secret is available, else the sslip.io origin). A relay secret set here flows
# through to it — exported LIONCLAW_MCP_RELAY_SECRET is inherited by this child,
# or it's picked up from .env — so no explicit forwarding is needed for a local run.
SSLIP_HOST="$SSLIP_HOST" bash deploy/bootstrap-allinone.sh

cat <<DONE

────────────────────────────────────────────────────────────────────────────
  Lionclaw is installed on this Tencent Cloud CVM.
────────────────────────────────────────────────────────────────────────────
  Dashboard : https://${SSLIP_HOST}/
  API       : https://${SSLIP_HOST}/api/status

  Manage:
    sudo docker compose -f deploy/docker-compose.allinone.yml ps
    sudo docker compose -f deploy/docker-compose.allinone.yml logs -f olano
    sudo docker compose -f deploy/docker-compose.allinone.yml up -d --build
────────────────────────────────────────────────────────────────────────────
DONE
