#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# tc-manual-host.sh — install Lionclaw on an EXISTING Tencent Cloud CVM without
# Docker, assuming you already ran ./install.sh on the machine.
#
# Run ON the CVM:
#
#   cd ~/olano
#   ./install.sh --no-docker        # or ./install.sh, if you want Docker too
#   cp .env.example .env            # add provider keys first
#   bash deploy/tc-manual-host.sh
#
# Manage the host-native services after setup:
#
#   bash deploy/tc-manual-host.sh restart
#   bash deploy/tc-manual-host.sh rebuild
#   bash deploy/tc-manual-host.sh server status
#   bash deploy/tc-manual-host.sh server restart
#   bash deploy/tc-manual-host.sh dashboard rebuild
#   bash deploy/tc-manual-host.sh dashboard restart
#
# Repair an existing instance incrementally (no rebuild — only fills in what is
# missing: creates .env from .env.example, strips LIONCLAW_DASHBOARD_TOKEN, pins
# LIONCLAW_PUBLIC_URL for chat/agent-driven MCP OAuth):
#
#   bash deploy/tc-manual-host.sh fix-env
#
# What this does:
#   1. Detects the public host (<ip>.sslip.io unless SSLIP_HOST is provided)
#   2. Creates/repairs .env incrementally (missing file, MCP OAuth public URL)
#   3. Runs olano quick-setup idempotently
#   4. Builds dashboard_2.0 into dist/
#   5. Installs/configures Caddy on the host
#   6. Installs/enables a systemd service for `olano serve`
#
# Optional overrides:
#   PUBLIC_IP=1.2.3.4
#   SSLIP_HOST=acme.olano.run   (production — run deploy/dns.sh ensure first; default is <ip>.sslip.io, dev only)
#   LIONCLAW_HOST_USER=ubuntu
#   OLANO_HOME=/home/ubuntu/olano-data   (default: see the resolution below)
#   LIONCLAW_RUNTIME_PORT=8765
#
# Tencent security group requirements: inbound TCP 80/443 must be reachable for
# Caddy HTTPS. SSH 22 only needs to be reachable from you.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
# Dim, timestamped sub-step line for long-running probes (e.g. the local-API
# wait) so a slow start shows live progress instead of a silent bar.
dbg()  { printf '\033[0;36m  · [%s] %s\033[0m\n' "$(date '+%H:%M:%S')" "$*"; }

usage() {
  cat <<USAGE
Usage:
  bash deploy/tc-manual-host.sh
      Install/configure host-native Lionclaw + dashboard on this CVM.

  bash deploy/tc-manual-host.sh start|stop|restart|status
      Manage the full app: Lionclaw backend first, then dashboard/Caddy.

  bash deploy/tc-manual-host.sh rebuild
      Rebuild + publish dashboard, stop server/dashboard, then start both.

  bash deploy/tc-manual-host.sh fix-env
      Incremental .env repair only: create .env from .env.example if missing,
      strip LIONCLAW_DASHBOARD_TOKEN, pin LIONCLAW_PUBLIC_URL (MCP OAuth).
      No rebuild; tells you if a backend restart is needed.

  bash deploy/tc-manual-host.sh server start|stop|restart|status|logs [-f]
      Manage the Lionclaw backend systemd service.

  bash deploy/tc-manual-host.sh dashboard start|stop|restart|reload|rebuild|status|logs [-f]
      Manage the dashboard/Caddy systemd service.

Env overrides:
  SERVICE_NAME=olano
  LIONCLAW_RUNTIME_PORT=8765
  LIONCLAW_WEB_ROOT=/var/www/olano-dashboard
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/olano/dashboard_2.0"
DASHBOARD_DIST="$DASHBOARD_DIR/dist"
WEB_ROOT="${LIONCLAW_WEB_ROOT:-/var/www/olano-dashboard}"
SERVICE_NAME="${SERVICE_NAME:-olano}"
RUNTIME_PORT="${LIONCLAW_RUNTIME_PORT:-8765}"
# $USER can be unset in non-login shells (cron, CI, some sudo setups).
HOST_USER="${LIONCLAW_HOST_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
if command -v getent >/dev/null 2>&1; then
  HOST_HOME="$(getent passwd "$HOST_USER" | cut -d: -f6)"
else
  HOST_HOME="$HOME"
fi
# Mirrors olano/config/home.py:get_olano_home(). Order matters: an
# EXISTING instance keeps its ~/olano or ~/.lionclaw, because defaulting a
# re-run to a new directory would point the unit at an empty one and read, to
# its operator, as though every agent/vault/session had been deleted. Only a
# host with none of the directories gets the default, ~/.olano. This script
# runs from a checkout that normally sits at ~/olano itself (see the usage at
# the top): a ~/olano carrying pyproject.toml or .git is the source, never
# the data dir.
HOST_CHECKOUT=0
if [ -e "$HOST_HOME/olano/pyproject.toml" ] || [ -e "$HOST_HOME/olano/.git" ]; then
  HOST_CHECKOUT=1
fi
if [ -n "${OLANO_HOME:-}" ]; then
  OLANO_HOME="$OLANO_HOME"
elif [ -n "${LIONCLAW_HOME:-}" ]; then
  OLANO_HOME="$LIONCLAW_HOME"
elif [ -d "$HOST_HOME/.olano" ]; then
  OLANO_HOME="$HOST_HOME/.olano"
elif [ -d "$HOST_HOME/olano" ] && [ "$HOST_CHECKOUT" -eq 0 ]; then
  OLANO_HOME="$HOST_HOME/olano"
elif [ -d "$HOST_HOME/.lionclaw" ]; then
  OLANO_HOME="$HOST_HOME/.lionclaw"
else
  OLANO_HOME="$HOST_HOME/.olano"
fi
# Both names carry the same value for the whole script: quick-setup and the
# unit may be run by code from either side of the rename.
LIONCLAW_HOME="$OLANO_HOME"
ENV_FILE="$REPO_ROOT/.env"

cd "$REPO_ROOT"

# install.sh places uv here on Linux, but non-login shells may not have it yet.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$REPO_ROOT/.venv/bin:$PATH"

# Shared idempotent .env repair helpers (create-if-missing, public-url pin, …).
# shellcheck source=deploy/env-lib.sh
. "$SCRIPT_DIR/env-lib.sh"

[ -f install.sh ] || die "Run from a Lionclaw checkout with install.sh."
ensure_env_file "$ENV_FILE" || die "Could not create $ENV_FILE (no .env.example?)."
[ -f "$DASHBOARD_DIR/package.json" ] || die "Dashboard package.json missing at $DASHBOARD_DIR."

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

# fix-env: apply ONLY the incremental .env repairs to an existing instance —
# create .env from the template when missing, drop LIONCLAW_DASHBOARD_TOKEN,
# and pin LIONCLAW_PUBLIC_URL=https://<host> so chat/agent-driven MCP OAuth
# works. No dashboard rebuild, no Caddy/systemd changes. Prints whether a
# backend restart is needed to pick the changes up.
fix_env() {
  ip="$(detect_public_ip | tr -d '[:space:]')"
  [ -n "$ip" ] || die "Could not detect public IP. Re-run with PUBLIC_IP=x.x.x.x."
  host="${SSLIP_HOST:-${ip//./-}.sslip.io}"

  changed=0
  if [ ! -f "$ENV_FILE" ]; then
    ensure_env_file "$ENV_FILE" || die "Could not create $ENV_FILE (no .env.example?)."
    changed=1
  fi
  strip_dashboard_token "$ENV_FILE"
  # Prefer the shared central relay (one registered redirect URI for the whole
  # fleet — required for pre-registration providers like HubSpot) when a relay
  # secret is available; fall back to this box's sslip.io origin otherwise.
  if ensure_relay_wiring "$ENV_FILE"; then
    changed=1
  fi
  if ensure_public_url "$ENV_FILE" "$host"; then
    changed=1
  fi

  if [ "$changed" -eq 1 ]; then
    say "Env updated. Restart the backend to apply:"
    echo "    bash deploy/tc-manual-host.sh server restart"
  else
    ok "Nothing to fix — .env already complete."
  fi
}

SUDO=sudo
[ "$(id -u)" -eq 0 ] && SUDO=""

if [ -t 1 ]; then
  ORIGINAL_STTY="$(stty -g 2>/dev/null || true)"
  cleanup_terminal() {
    [ -n "${ORIGINAL_STTY:-}" ] && stty "$ORIGINAL_STTY" 2>/dev/null || stty sane 2>/dev/null || true
    tput sgr0 2>/dev/null || true
    tput cnorm 2>/dev/null || true
    printf '\n' >&2
  }
  trap cleanup_terminal EXIT
fi

# Translate a curl probe exit code into a human reason, so the log distinguishes
# "port not open yet" (service still booting) from "connected but no reply"
# (runtime wedged, or mid-download before uvicorn binds) — the two things that
# make this wait slow. See knowledge_troubleshooting.md "Waiting for the local
# API..." entries.
curl_probe_reason() {
  case "$1" in
    0)  echo "OK (HTTP 2xx)" ;;
    7)  echo "connection refused — port ${RUNTIME_PORT} not listening yet (service still starting)" ;;
    28) echo "timed out — connected but no HTTP reply (runtime busy/hung, e.g. mid dependency download or MCP OAuth stall)" ;;
    22) echo "HTTP error status — server answered non-2xx (booting or unhealthy)" ;;
    52) echo "empty reply — server accepted then closed the connection (still starting)" ;;
    56) echo "receive failure — connection reset mid-response" ;;
    *)  echo "curl exit $1" ;;
  esac
}

wait_local_api() {
  url="http://127.0.0.1:${RUNTIME_PORT}/api/status"
  max_attempts=60
  interval=3
  start_epoch="$(date +%s)"
  prev_state=""
  say "Waiting for the local API at ${url} (probing every ${interval}s, up to $((max_attempts * interval))s)..."
  dbg "started; each line below = timestamp · attempt · elapsed · systemd state · probe result"

  for attempt in $(seq 1 "$max_attempts"); do
    elapsed=$(( $(date +%s) - start_epoch ))
    state="$($SUDO systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || echo unknown)"

    # --max-time is essential here: a wedged runtime still ACCEPTs connections
    # (the listen backlog is kernel-side) but never sends a response, and an
    # unbounded curl would then block this loop — and the whole script — forever.
    # `|| rc=$?` keeps a failed probe from tripping `set -e` while capturing the
    # exit code so we can explain WHY it failed.
    rc=0
    curl -fsS --connect-timeout 2 --max-time 5 "$url" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
      ok "Runtime API is up — answered at $(date '+%H:%M:%S') after ${elapsed}s (attempt ${attempt}/${max_attempts})"
      return 0
    fi

    dbg "attempt ${attempt}/${max_attempts} · elapsed ${elapsed}s · service=${state} · probe: $(curl_probe_reason "$rc")"

    # Surface the log tail the moment the unit leaves "active" (a crash /
    # restart-loop) instead of making the user wait out the full timeout.
    if [ "$state" != "active" ] && [ "$state" != "$prev_state" ]; then
      warn "Service is now '${state}' (was '${prev_state:-unknown}') — recent backend logs:"
      $SUDO journalctl -u "${SERVICE_NAME}.service" -n 8 --no-pager >&2 || true
    # While it IS active but not answering, show the latest backend log line
    # every ~30s so a slow-but-progressing start (dependency download, MCP
    # connect) is visible rather than looking hung.
    elif [ "$state" = "active" ] && [ $(( attempt % 10 )) -eq 0 ]; then
      last_log="$($SUDO journalctl -u "${SERVICE_NAME}.service" -n 1 --no-pager -o cat 2>/dev/null || true)"
      [ -n "$last_log" ] && dbg "latest backend log: ${last_log}"
    fi

    prev_state="$state"
    sleep "$interval"
  done

  elapsed=$(( $(date +%s) - start_epoch ))
  warn "Runtime API did not answer on 127.0.0.1:${RUNTIME_PORT} after ${elapsed}s (~$((max_attempts * interval / 60)) minutes)."
  warn "Service state: $($SUDO systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || echo unknown). Recent backend logs:"
  $SUDO journalctl -u "${SERVICE_NAME}.service" -n 25 --no-pager >&2 || true
  warn "If the service is active but the port never answers, the runtime is hung — restart it cleanly:"
  warn "    bash deploy/tc-manual-host.sh server restart"
  return 1
}

stop_unit_now() {
  unit="$1"
  if command -v timeout >/dev/null 2>&1; then
    $SUDO timeout 15s systemctl stop "$unit" >/dev/null 2>&1 || true
  else
    $SUDO systemctl stop "$unit" >/dev/null 2>&1 || true
  fi
  $SUDO systemctl kill --kill-who=all "$unit" >/dev/null 2>&1 || true
}

stop_stray_lionclaw() {
  # Clean up old/manual foreground runs so only the systemd-managed runtime remains.
  pkill -u "$HOST_USER" -f "olano serve" >/dev/null 2>&1 || true
  pkill -u "$HOST_USER" -f "python -m olano serve" >/dev/null 2>&1 || true
  sleep 1
  pkill -9 -u "$HOST_USER" -f "olano serve" >/dev/null 2>&1 || true
  pkill -9 -u "$HOST_USER" -f "python -m olano serve" >/dev/null 2>&1 || true
}

stop_stray_caddy() {
  # Clear manually launched Caddy processes that can hold ports 80/443.
  $SUDO pkill -f "caddy run" >/dev/null 2>&1 || true
  $SUDO pkill -f "caddy start" >/dev/null 2>&1 || true
  $SUDO pkill -x caddy >/dev/null 2>&1 || true
  sleep 1
  $SUDO pkill -9 -f "caddy run" >/dev/null 2>&1 || true
  $SUDO pkill -9 -f "caddy start" >/dev/null 2>&1 || true
  $SUDO pkill -9 -x caddy >/dev/null 2>&1 || true
}

require_npm() {
  if ! command -v npm >/dev/null 2>&1; then
    die "npm not found. Run ./install.sh on this machine first."
  fi
  # npx-launched MCP servers (mcp-remote) crash on Node < 20 ("File is not
  # defined"), which agents surface as an opaque "McpError: Connection closed".
  node_major="$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)"
  if [ -z "$node_major" ] || [ "$node_major" -lt 20 ] 2>/dev/null; then
    die "Node >= 20 required (found: $(node --version 2>/dev/null || echo none)). Re-run ./install.sh to upgrade via NodeSource."
  fi
}

rebuild_dashboard() {
  require_npm
  say "Building dashboard_2.0..."
  bash deploy/build-dashboard.sh
  [ -d "$DASHBOARD_DIST" ] || die "Dashboard build did not create $DASHBOARD_DIST."

  say "Publishing dashboard build to $WEB_ROOT..."
  $SUDO mkdir -p "$WEB_ROOT"
  if command -v rsync >/dev/null 2>&1; then
    $SUDO rsync -a --delete "$DASHBOARD_DIST/" "$WEB_ROOT/"
  else
    $SUDO find "$WEB_ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    $SUDO cp -a "$DASHBOARD_DIST/." "$WEB_ROOT/"
  fi
  if id caddy >/dev/null 2>&1; then
    $SUDO chown -R caddy:caddy "$WEB_ROOT"
  fi
  $SUDO chmod -R a+rX "$WEB_ROOT"
}

manage_server() {
  action="${1:-}"; [ "$#" -gt 0 ] && shift || true
  case "$action" in
    start)
      say "Starting ${SERVICE_NAME}.service cleanly..."
      stop_unit_now "${SERVICE_NAME}.service"
      stop_stray_lionclaw
      $SUDO systemctl start "${SERVICE_NAME}.service"
      wait_local_api || true
      ;;
    stop)
      say "Stopping ${SERVICE_NAME}.service and stray olano serve processes..."
      stop_unit_now "${SERVICE_NAME}.service"
      stop_stray_lionclaw
      ok "Stopped"
      ;;
    restart)
      say "Restarting ${SERVICE_NAME}.service cleanly..."
      stop_unit_now "${SERVICE_NAME}.service"
      stop_stray_lionclaw
      $SUDO systemctl start "${SERVICE_NAME}.service"
      wait_local_api || true
      ;;
    status)
      $SUDO systemctl status "${SERVICE_NAME}.service" --no-pager || true
      curl -fsS --connect-timeout 2 --max-time 8 "http://127.0.0.1:${RUNTIME_PORT}/api/status" 2>/dev/null \
        && echo || warn "API not reachable at http://127.0.0.1:${RUNTIME_PORT}/api/status"
      ;;
    logs)
      follow=""
      case "${1:-}" in -f|--follow) follow="-f";; "" ) ;; *) die "unknown server logs arg '$1'";; esac
      $SUDO journalctl -u "${SERVICE_NAME}.service" -n "${TAIL:-200}" $follow
      ;;
    ""|-h|--help|help) usage;;
    *) die "unknown server command '$action' (use: start | stop | restart | status | logs)";;
  esac
}

manage_dashboard() {
  action="${1:-}"; [ "$#" -gt 0 ] && shift || true
  case "$action" in
    start)
      say "Starting caddy.service cleanly..."
      stop_unit_now caddy
      stop_stray_caddy
      $SUDO systemctl start caddy
      ;;
    stop)
      say "Stopping caddy.service and stray Caddy processes..."
      stop_unit_now caddy
      stop_stray_caddy
      ok "Stopped"
      ;;
    restart)
      say "Restarting caddy.service cleanly..."
      stop_unit_now caddy
      stop_stray_caddy
      $SUDO systemctl start caddy
      ;;
    rebuild)
      rebuild_dashboard
      manage_dashboard restart
      ;;
    reload)
      say "Reloading caddy.service..."
      $SUDO systemctl reload caddy || $SUDO systemctl restart caddy
      ;;
    status)
      $SUDO systemctl status caddy --no-pager || true
      ;;
    logs)
      follow=""
      case "${1:-}" in -f|--follow) follow="-f";; "" ) ;; *) die "unknown dashboard logs arg '$1'";; esac
      $SUDO journalctl -u caddy -n "${TAIL:-200}" $follow
      ;;
    ""|-h|--help|help) usage;;
    *) die "unknown dashboard command '$action' (use: start | stop | restart | reload | rebuild | status | logs)";;
  esac
}

# Idempotently enable the owner-only /olano command (server update / restart /
# shutdown) on the agentfather agent. Agents scaffolded before /olano existed
# don't have it in commands.enabled, so on an upgrade/redeploy this backfills
# the flag; the server restart that follows picks it up. No-op when agentfather
# isn't installed, /olano is already enabled, or no olano python is reachable
# — always non-fatal so it can't block a deploy.
ensure_olano_command() {
  local py
  if [ -n "${UV_BIN:-}" ]; then
    py=("$UV_BIN" run python)
  elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    py=("$REPO_ROOT/.venv/bin/python")
  elif command -v python3 >/dev/null 2>&1; then
    py=(python3)
  else
    warn "No python found — skipping /olano enablement for agentfather."
    return 0
  fi
  say "Ensuring the /olano command is enabled for agentfather..."
  env LIONCLAW_HOME="$LIONCLAW_HOME" "${py[@]}" - <<'PYEOF' || warn "Could not enable /olano (non-fatal)."
import sys

try:
    from olano.admin.agents import update_agent
    from olano.config.loader import find_agent_config_path, load_agent_config
except Exception as exc:  # olano not importable from this interpreter
    print(f"skip: olano not importable ({exc})")
    sys.exit(0)

NAME = "agentfather"
if find_agent_config_path(NAME, source="home") is None:
    print("skip: agentfather is not installed")
    sys.exit(0)

cfg = load_agent_config(NAME, source="home")
commands = cfg.commands.model_dump()
enabled = dict(commands.get("enabled") or {})
if enabled.get("olano") is True:
    print("ok: /olano already enabled for agentfather")
    sys.exit(0)

enabled["olano"] = True
commands["enabled"] = enabled
update_agent(NAME, {"commands": commands})  # validates + persists (dashboard save path)
print("enabled: /olano for agentfather")
PYEOF
}

case "${1:-}" in
  rebuild)
    rebuild_dashboard
    ensure_olano_command
    manage_server stop
    manage_dashboard stop
    manage_server start
    manage_dashboard start
    exit 0
    ;;
  start|stop|restart|status)
    action="$1"
    manage_server "$action"
    manage_dashboard "$action"
    exit 0
    ;;
  fix-env|env)
    fix_env
    exit 0
    ;;
  server)
    shift
    manage_server "$@"
    exit 0
    ;;
  dashboard|caddy|web)
    shift
    manage_dashboard "$@"
    exit 0
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    die "unknown command '$1' (run --help)"
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  say "Installing curl..."
  $SUDO apt-get update
  $SUDO apt-get install -y curl ca-certificates
fi

require_npm

UV_BIN="$(command -v uv || true)"
LIONCLAW_BIN="$(command -v olano || true)"
if [ -z "$LIONCLAW_BIN" ] && [ -x "$REPO_ROOT/.venv/bin/olano" ]; then
  LIONCLAW_BIN="$REPO_ROOT/.venv/bin/olano"
fi

if [ -n "$UV_BIN" ]; then
  LIONCLAW_RUN=("$UV_BIN" run olano)
elif [ -n "$LIONCLAW_BIN" ]; then
  LIONCLAW_RUN=("$LIONCLAW_BIN")
else
  die "Lionclaw command not found. Run ./install.sh on this machine first."
fi

# The systemd ExecStart must NOT be a bare `uv run olano serve`: `uv run`
# re-checks and re-syncs the project environment against uv.lock on EVERY start,
# and the default resolution pulls sentence-transformers → torch → the multi-GB
# NVIDIA CUDA wheels. On a fresh/ephemeral box — or after a `git pull` that
# drifted a dependency — that turns each service start into a multi-gigabyte
# download BEFORE uvicorn can bind, and the `Waiting for the local API...` probe
# below then sits through the entire download (the #1 cause of a slow/stuck
# wait). The environment is already synced authoritatively elsewhere (install.sh
# runs `uv sync --all-extras --dev`; the /olano self-update flow runs `uv sync`),
# so the service only needs to *exec* the pre-synced interpreter. Prefer the
# venv binary directly; otherwise fall back to `uv run --no-sync` (runs from the
# existing env, skips the sync) so a service start can never trigger a download.
if [ -x "$REPO_ROOT/.venv/bin/olano" ]; then
  SERVICE_EXEC="$REPO_ROOT/.venv/bin/olano serve"
elif [ -n "$UV_BIN" ]; then
  SERVICE_EXEC="${UV_BIN} run --no-sync olano serve"
else
  SERVICE_EXEC="${LIONCLAW_BIN} serve"
fi

PUBLIC_IP="$(detect_public_ip | tr -d '[:space:]')"
[ -n "$PUBLIC_IP" ] || die "Could not detect public IP. Re-run with PUBLIC_IP=x.x.x.x."

SSLIP_HOST="${SSLIP_HOST:-${PUBLIC_IP//./-}.sslip.io}"

strip_dashboard_token "$ENV_FILE"
# MCP OAuth redirect target: prefer the shared central relay (one registered
# redirect URI for the whole fleet — required for pre-registration providers like
# HubSpot) when a relay secret is available; otherwise pin this box's own public
# origin (providers redirect to <public-url>/api/mcp/oauth/callback → Caddy →
# backend).
ensure_relay_wiring "$ENV_FILE" || true
ensure_public_url "$ENV_FILE" "$SSLIP_HOST" || true

env_value() {
  awk -v key="$1" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      gsub(/^"|"$/, "")
      gsub(/^'\''|'\''$/, "")
      print
      exit
    }
  ' "$ENV_FILE"
}

QUICK_SETUP_ENV=()
for key in LIONCLAW_ADMIN_EMAIL LIONCLAW_ADMIN_PASSWORD LIONCLAW_ADMIN_NAME LIONCLAW_VAULT_PASSWORD LIONCLAW_VAULT_KEY; do
  value="$(env_value "$key")"
  [ -n "$value" ] && QUICK_SETUP_ENV+=("${key}=${value}")
done

vault_key="$(env_value LIONCLAW_VAULT_KEY)"
vault_password="$(env_value LIONCLAW_VAULT_PASSWORD)"
if [ -n "$vault_key" ] && [ -z "$vault_password" ]; then
  QUICK_SETUP_ENV+=("LIONCLAW_VAULT_PASSWORD=${vault_key}")
fi

# --skip-catalogs: the runtime is already answering (waited on above) and
# populates these itself — built-in skills via run_startup_upgrades on boot, the
# cloud skills + archetype catalogs via CatalogSyncService ~20s later, which is
# also what keeps them current afterwards. Running them here as well fetched the
# same ~8 MB a second time in the foreground on every rebuild.
say "Running quick-setup with OLANO_HOME=$OLANO_HOME"
env "${QUICK_SETUP_ENV[@]}" LIONCLAW_HOME="$LIONCLAW_HOME" "${LIONCLAW_RUN[@]}" quick-setup --yes --skip-catalogs || \
  warn "quick-setup returned non-zero (may already be set up) — continuing."

# Backfill the owner-only /olano command on an existing agentfather (new agents
# get it from the archetype; the restart below makes it live).
ensure_olano_command

rebuild_dashboard

install_caddy() {
  if command -v caddy >/dev/null 2>&1; then
    ok "Caddy already installed: $(caddy version 2>/dev/null | head -1)"
    return 0
  fi

  say "Installing Caddy..."
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update
    $SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | $SUDO gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | $SUDO tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    $SUDO apt-get update
    $SUDO apt-get install -y caddy
  else
    die "No supported package manager found for Caddy install. Install caddy, then re-run."
  fi
}

install_caddy

# Last-resort host protection: if total memory pressure still gets critical
# (something outside the olano cgroup, or swap exhaustion), earlyoom kills
# the largest offender before the box livelocks. Complements the unit-level
# MemoryHigh/MemoryMax below — those bound olano; this saves the host.
install_earlyoom() {
  if ! command -v earlyoom >/dev/null 2>&1; then
    say "Installing earlyoom..."
    $SUDO apt-get update
    $SUDO apt-get install -y earlyoom
  else
    ok "earlyoom already installed"
  fi
  # Never pick infrastructure daemons; the biggest RSS elsewhere (an agent's
  # pytest/pip/node child, or the runtime itself) is the right victim.
  $SUDO tee /etc/default/earlyoom >/dev/null <<'EOOM'
EARLYOOM_ARGS="-r 3600 --avoid '(^|/)(init|systemd.*|sshd|dockerd|containerd.*|caddy|postgres.*|multipathd)$'"
EOOM
  $SUDO systemctl enable --now earlyoom
  $SUDO systemctl restart earlyoom   # pick up EARLYOOM_ARGS on re-runs
}

install_earlyoom

say "Writing systemd service ${SERVICE_NAME}.service..."
$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<UNIT
[Unit]
Description=Lionclaw Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${HOST_USER}
WorkingDirectory=${REPO_ROOT}
EnvironmentFile=${ENV_FILE}
Environment=OLANO_HOME=${OLANO_HOME}
Environment=LIONCLAW_HOME=${LIONCLAW_HOME}
Environment=LIONCLAW_RUNTIME_PORT=${RUNTIME_PORT}
# Tell platform-control commands (/olano update|restart|shutdown) that this
# is a source checkout supervised by this exact systemd unit.
Environment=LIONCLAW_INSTALL_MODE=source
Environment=LIONCLAW_SYSTEMD_UNIT=${SERVICE_NAME}.service
# Cap glibc's per-thread malloc arenas. The default is 8 x CPU cores, each able
# to grow to 64 MB and never shared, so a multi-threaded runtime (uvicorn +
# scheduler + per-platform pollers) bloats RSS with per-arena free lists that
# are never handed back. 2 arenas trades a little allocator contention for a
# materially smaller resident set — the right trade on a RAM-capped host.
Environment=MALLOC_ARENA_MAX=2
# Return a free chunk to the OS once it exceeds 64 MB rather than the adaptive
# default, which lets the top of the heap grow unbounded before trimming.
Environment=MALLOC_TRIM_THRESHOLD_=67108864
ExecStart=${SERVICE_EXEC}
Restart=always
RestartSec=5
# Memory guardrails. Agents with a local_shell backend run their commands
# (pytest, pip, builds) inside THIS cgroup, so one heavy command can otherwise
# swap-thrash the whole VM into a silent freeze (no OOM log, hard reset only —
# seen 2026-07-27 on prod). Throttle+reclaim at MemoryHigh, hard-kill at
# MemoryMax and let Restart=always bring the runtime back in seconds instead.
# Percentages scale with the box: on a 7.3G host this is ~4.7G/~5.8G, leaving
# headroom for the OS, Docker, the Postgres sidecar, and Caddy.
MemoryHigh=65%
MemoryMax=80%
# Same ceiling the Docker sandbox uses (--pids-limit=512); bounds fork storms.
TasksMax=512

[Install]
WantedBy=multi-user.target
UNIT

say "Writing Caddyfile for https://${SSLIP_HOST}..."
$SUDO tee /etc/caddy/Caddyfile >/dev/null <<CADDY
# ACME contact email — also enables Caddy's ZeroSSL fallback issuer (needs an
# email) so TLS still issues when Let's Encrypt is down or rate-limited.
{
	email ${LIONCLAW_ACME_EMAIL:-support@olano.ai}
}

${SSLIP_HOST} {
	encode zstd gzip

	@api path /api/* /openapi.json
	handle @api {
		reverse_proxy 127.0.0.1:${RUNTIME_PORT}
	}

	# Agent-published pages → backend. The runtime enforces HTTP Basic Auth per
	# request against the owner-set credential (olano pages set-password);
	# nothing under /pages/ is ever served unauthenticated. Without this the
	# catch-all below serves the SPA for /pages/* and the client router bounces
	# viewers to /overview.
	@pages path /pages /pages/*
	handle @pages {
		reverse_proxy 127.0.0.1:${RUNTIME_PORT}
	}

	# /assets/* gets its OWN handle, with no try_files. A rebuild replaces the
	# content-hashed chunks, so a tab opened before it asks for names that are
	# gone; under the SPA fallback below those answer 200 with the HTML shell,
	# the browser refuses to run HTML as a module ("Failed to fetch dynamically
	# imported module"), and because these are cached immutable an intermediary
	# can keep serving that HTML under a .js URL. A plain 404 keeps the failure
	# honest — src/core/chunkRecovery.ts then reloads once and picks up the
	# current index.html. Mirrors deploy/Caddyfile.allinone; pinned by
	# tests/test_dashboard_asset_fallback.py.
	@assets path /assets/*
	handle @assets {
		root * ${WEB_ROOT}

		# Caching. Without an explicit Cache-Control, Caddy's file_server sends
		# none at all, so browsers fall back to heuristic freshness and
		# re-validate every chunk on every navigation — a full round-trip each,
		# times the ~15 chunks a route pulls in. Vite's names change whenever
		# the bytes do, so those are safe to cache permanently.
		@hashed not path /assets/theme1/*
		header @hashed Cache-Control "public, max-age=31536000, immutable"

		# theme1/ is hand-managed and NOT content-hashed — short TTL instead
		# (mirrors the policy in deploy/Caddyfile.allinone).
		@theme path /assets/theme1/*
		header @theme Cache-Control "public, max-age=300, must-revalidate"

		file_server
	}

	handle {
		root * ${WEB_ROOT}

		# The HTML shell — and every SPA route that try_files rewrites to it —
		# must be revalidated, or a deploy's new asset hashes are never seen.
		# /sw.js and /manifest.json ride the same rule, which is what keeps a
		# replacement service worker (above all the kill switch in
		# public/sw.js) one load away instead of a heuristic TTL away.
		header Cache-Control "no-cache"

		try_files {path} /index.html
		file_server
	}
}
CADDY

say "Starting Lionclaw and Caddy..."
$SUDO systemctl daemon-reload
# `enable --now` is a no-op for an already-running unit, so on a re-run it
# would leave the old (possibly hung) runtime alive under the stale unit
# config. Enable for boot persistence, then always do a clean restart so the
# freshly written unit takes effect and a wedged process is actually replaced.
$SUDO systemctl enable "${SERVICE_NAME}.service"
stop_unit_now "${SERVICE_NAME}.service"
stop_stray_lionclaw
$SUDO systemctl start "${SERVICE_NAME}.service"
$SUDO systemctl enable --now caddy
$SUDO systemctl reload caddy || $SUDO systemctl restart caddy

wait_local_api || true

ADMIN_EMAIL_DISPLAY="$(env_value LIONCLAW_ADMIN_EMAIL)"
ADMIN_EMAIL_DISPLAY="${ADMIN_EMAIL_DISPLAY:-admin@example.com}"
# Never the password itself in a banner that lands in a log: say where it is.
if [ -n "$(env_value LIONCLAW_ADMIN_PASSWORD)" ]; then
  ADMIN_PASSWORD_DISPLAY="the LIONCLAW_ADMIN_PASSWORD configured in ${ENV_FILE}"
else
  ADMIN_PASSWORD_DISPLAY="the one-time password quick-setup printed above (kept at ${OLANO_HOME}/run/initial-admin-password until you change it)"
fi

cat <<DONE

────────────────────────────────────────────────────────────────────────────
  Lionclaw is installed on this Tencent Cloud CVM without Docker.
────────────────────────────────────────────────────────────────────────────
  Dashboard : https://${SSLIP_HOST}/
  API       : https://${SSLIP_HOST}/api/status

  Manage:
    sudo systemctl status ${SERVICE_NAME}
    sudo journalctl -u ${SERVICE_NAME} -f
    sudo systemctl restart ${SERVICE_NAME}
    sudo systemctl reload caddy

  Admin login:
    email:    ${ADMIN_EMAIL_DISPLAY}
    password: ${ADMIN_PASSWORD_DISPLAY}
────────────────────────────────────────────────────────────────────────────
DONE
