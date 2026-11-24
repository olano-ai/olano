#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Runs ON the box (DigitalOcean droplet OR AWS EC2). Installs Docker, writes the
# Caddyfile host, and brings up the ALL-IN-ONE stack: the Lionclaw backend AND
# the dashboard_2.0 SPA served from the same origin by Caddy.
#
#   cd ~/olano   (/home/olano/olano on a DO droplet; /root/olano legacy)
#   SSLIP_HOST=acme.olano.run bash deploy/bootstrap-allinone.sh        # production (deploy/dns.sh first)
#   SSLIP_HOST=203-0-113-10.sslip.io bash deploy/bootstrap-allinone.sh  # dev/test only
#
# Runs as the box's service user (`olano` on DO, `ubuntu` on AWS/TC — root only
# on legacy droplets); everything privileged goes through sudo (NOPASSWD).
#
# Differs from bootstrap-remote.sh only in the compose file (all-in-one) and the
# extra "wait for the SPA on /" check. Idempotent — safe to re-run.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SSLIP_HOST="${SSLIP_HOST:?set SSLIP_HOST, e.g. acme.olano.run (customers) or 203-0-113-10.sslip.io (dev)}"
COMPOSE="deploy/docker-compose.allinone.yml"

# ── phase timing ──────────────────────────────────────────────────────────────
# Where did the install actually spend its time? The control plane only ever
# sees `tail -c 6000` of this log, so anything printed as the run goes along is
# cut off long before a human reads it -- a golden-image deploy showed two
# unexplained minutes with nothing left in the tail to explain them. So the
# durations are ACCUMULATED and printed as one compact block at the very end,
# where they survive the truncation, next to the provenance the box reports.
#
# `phase <name>` closes the previous phase and opens a new one (it prints the
# same "▶ name" banner the echoes used to). The EXIT trap closes the last one
# and prints the summary, so a FAILED install is profiled too -- that is when
# the numbers matter most.
_PHASE_NAME=""
_PHASE_START=0
_PHASE_ROWS=""
_RUN_START=$(date +%s)

phase() {
  local now
  now=$(date +%s)
  if [ -n "$_PHASE_NAME" ]; then
    _PHASE_ROWS="${_PHASE_ROWS}$(printf '%5ds  %s' "$((now - _PHASE_START))" "$_PHASE_NAME")
"
  fi
  _PHASE_NAME="${1:-}"
  _PHASE_START="$now"
  [ -n "${1:-}" ] && echo "▶ $1"
  return 0
}

phase_summary() {
  local rc=$?
  phase ""  # close whatever was open
  echo
  echo "── install phase timings ─────────────────────────────────────────────"
  printf '%s' "$_PHASE_ROWS"
  printf '%5ds  TOTAL (bootstrap script)\n' "$(($(date +%s) - _RUN_START))"
  [ "$rc" -ne 0 ] && printf '        exited %s\n' "$rc"
  echo "──────────────────────────────────────────────────────────────────────"
  return 0
}
trap phase_summary EXIT

# ── 1. Docker engine + compose plugin ────────────────────────────────────────
# get.docker.com installs BOTH the engine and the Compose v2 plugin, but only
# runs when docker is absent. DigitalOcean's GPU AI/ML image ships a docker
# engine with NO usable `docker compose` — sometimes the plugin is missing
# outright (`docker: unknown command: docker compose`), sometimes it is present
# but too old for the global `--env-file` flag (`unknown flag: --env-file`).
# Either way the runtime image pull aborts and the box never comes up, while the
# CPU tiers (fresh get.docker.com install) are fine. So: install the engine if
# missing, then GUARANTEE a current Compose plugin by probing the exact call
# shape we use (--env-file, with /dev/null so a not-yet-written .env can't
# mislead the probe) and dropping in the official plugin binary when it fails.
if ! command -v docker >/dev/null 2>&1; then
  phase "Installing Docker…"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "${USER:-$(id -un)}" || true
fi
# Bounded container logs as the DAEMON default (json-file 20m × 3). Docker's
# stock json-file driver is unbounded, and a chatty backend can fill a 25 GB
# Basic droplet with logs alone. The compose file caps its own services, but
# the daemon default is what also covers the per-agent exec-sandbox siblings
# the runtime spawns outside compose. Written only when no daemon.json exists
# (never merge someone's config in bash); restart is safe here — the stack
# isn't up yet at this point of the bootstrap.
if ! sudo test -s /etc/docker/daemon.json; then
  phase "Setting docker log rotation defaults (json-file, 20m × 3)…"
  printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "20m", "max-file": "3" }\n}\n' \
    | sudo tee /etc/docker/daemon.json >/dev/null
  sudo systemctl restart docker 2>/dev/null || true
fi
if ! sudo docker compose --env-file /dev/null version >/dev/null 2>&1; then
  phase "Installing Docker Compose v2 plugin (image ships none, or one too old for --env-file)…"
  case "$(uname -m)" in
    aarch64|arm64) cmarch=aarch64 ;;
    *)             cmarch=x86_64 ;;
  esac
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo curl -fsSL \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${cmarch}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  sudo docker compose version >/dev/null 2>&1 \
    || { echo "! could not install a working docker compose plugin — aborting"; exit 1; }
fi
# Use sudo for this session (group change needs a new login to take effect).
# --env-file .env is REQUIRED: it feeds LIONCLAW_HOST_HOME to compose
# interpolation (sudo resets HOME=/root, so the shell env can't carry it). Safe
# now that the block above guarantees a current plugin that accepts the flag.
DC() { sudo docker compose --env-file .env -f "$COMPOSE" "$@"; }

# Last-resort host protection: kill the largest offender before total memory
# pressure livelocks the VM (complements the container mem_limit pinned below).
# Best-effort — the deploy proceeds fine without it.
if command -v apt-get >/dev/null 2>&1 && ! command -v earlyoom >/dev/null 2>&1; then
  phase "Installing earlyoom…"
  if sudo apt-get install -y earlyoom || { sudo apt-get update && sudo apt-get install -y earlyoom; }; then
    sudo tee /etc/default/earlyoom >/dev/null <<'EOOM'
EARLYOOM_ARGS="-r 3600 --avoid '(^|/)(init|systemd.*|sshd|dockerd|containerd.*|caddy|postgres.*|multipathd)$'"
EOOM
    sudo systemctl enable --now earlyoom || true
    sudo systemctl restart earlyoom || true
  else
    echo "⚠ earlyoom install failed — continuing without it."
  fi
fi

# ── 2. .env repair (incremental — creates/patches only what is missing) ───────
# shellcheck source=deploy/env-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/env-lib.sh"
ensure_env_file .env || exit 1
strip_dashboard_token .env
# Freeze the compose project name BEFORE any `up`, so a future directory
# rename cannot repoint the Postgres volume at an empty one.
pin_compose_project .env
# Pin the host-side data dir for compose interpolation (sticky: keeps /root on
# legacy boxes — see env-lib.sh), then pin its basename (sticky: whichever of
# .olano / olano / .lionclaw already holds data, else .olano on a fresh box;
# a ~/olano that is this source checkout is never the data dir —
# ensure_home_dir_name must run AFTER ensure_host_home since the probes use
# LIONCLAW_HOST_HOME).
# Pre-create the dir owned by the invoking user so dockerd doesn't create it
# root-owned on first `up` (sudo fallback covers the legacy /root pin, where
# the dir already exists behind a 700 /root).
ensure_host_home .env
ensure_home_dir_name .env
mkdir -p "${LIONCLAW_HOST_HOME}/${OLANO_HOME_DIR:-olano}" 2>/dev/null \
  || sudo mkdir -p "${LIONCLAW_HOST_HOME}/${OLANO_HOME_DIR:-olano}"
# Cap the olano container at 75% of host RAM (sticky — see env-lib.sh) so a
# runaway agent shell command OOMs the container instead of freezing the VM.
ensure_mem_limit .env
# Give the Postgres sidecar a random password (sticky — see env-lib.sh). MUST
# precede the first `up`: Postgres reads POSTGRES_PASSWORD only when it
# initialises the data dir, and every compose file falls back to the literal
# `lionclaw` when the variable is unset. The role/database/volume name is
# pinned the same way (olano on a fresh box, the name an existing data dir
# was initialised with otherwise), for the same reason.
ensure_pg_password .env
ensure_pg_name .env

# ── 3. Caddy host config (rsync-excluded; survives re-ships + re-ups) ─────────
phase "Configuring Caddy for https://${SSLIP_HOST}"
echo "LIONCLAW_HTTPS_HOST=${SSLIP_HOST}" > deploy/caddy.env
# Optional ACME contact override (the Caddyfile has a default). An email is
# what activates Caddy's ZeroSSL fallback when Let's Encrypt is rate-limited.
if [ -n "${LIONCLAW_ACME_EMAIL:-}" ]; then
  echo "LIONCLAW_ACME_EMAIL=${LIONCLAW_ACME_EMAIL}" >> deploy/caddy.env
fi

# ── 3b. MCP OAuth redirect target ─────────────────────────────────────────────
# Prefer the shared central relay (ONE registered redirect URI for the whole
# fleet — required for pre-registration providers like HubSpot, which pin a
# single redirect URL and reject every per-box sslip.io host) when a relay secret
# is available; otherwise fall back to this box's own sslip.io origin.
ensure_relay_wiring .env || true
ensure_public_url   .env "${SSLIP_HOST}" || true

# ── 4. bring the stack up — pull pre-built images OR build from source ────────
# Managed-cloud deploys set OLANO_PULL_IMAGES=1 so the box pulls the backend +
# dashboard images from GHCR instead of building them (building all provider
# SDKs + the Vite dashboard on a small droplet OOMs / thrashes for tens of
# minutes). Local/manual deploys leave it unset and build from source as before.
if [ "${OLANO_PULL_IMAGES:-0}" = "1" ]; then
  phase "Pulling pre-built images from GHCR + starting…"
  if [ -n "${OLANO_GHCR_TOKEN:-}" ]; then
    printf '%s' "${OLANO_GHCR_TOKEN}" | sudo docker login ghcr.io \
      -u "${OLANO_GHCR_USER:-olano-ai}" --password-stdin \
      || { echo "! GHCR login failed — cannot pull private runtime images"; exit 1; }
    # `sudo docker login` lands in /root/.docker (sudo resets HOME). Mirror the
    # credential for the invoking service user so the managed-update agent —
    # which runs as that user WITHOUT sudo — can pull private images later.
    if [ "$(id -u)" -ne 0 ] && sudo test -f /root/.docker/config.json; then
      sudo install -d -m 700 -o "${USER:-$(id -un)}" -g "${USER:-$(id -un)}" "$HOME/.docker"
      sudo install -m 600 -o "${USER:-$(id -un)}" -g "${USER:-$(id -un)}" \
        /root/.docker/config.json "$HOME/.docker/config.json"
    fi
  fi
  # Retry the pull: the backend image is large, and a single mid-download
  # connection reset from the registry CDN used to abort the whole first-boot
  # install under `set -e` (the deploy then surfaced as "runtime API did not
  # answer after install"). Completed layers are cached between attempts, so
  # each retry only re-fetches what is still missing.
  pulled=""
  for attempt in 1 2 3 4 5; do
    if DC pull; then pulled=1; break; fi
    echo "! image pull attempt ${attempt}/5 failed — retrying in $((attempt * 15))s…"
    sleep $((attempt * 15))
  done
  if [ -z "$pulled" ]; then
    echo "! image pull failed after 5 attempts — aborting install"
    exit 1
  fi
  DC up -d           # images are pre-built; never build on the box

  # The exec sandbox image is NOT a compose service (the runtime spawns it as a
  # sibling container per agent via the host socket), so `DC pull` above doesn't
  # fetch it. Pull it explicitly and tag it to the local name the runtime looks
  # up (olano-sandbox:latest — OlanoDockerBackendConfig.image). Best-effort:
  # a missing sandbox image only disables agent code-exec (the runtime fails
  # closed to a no-shell filesystem backend), so it must not abort the
  # backend/dashboard install that already succeeded above.
  SANDBOX_IMAGE="${OLANO_SANDBOX_IMAGE:-ghcr.io/olano-ai/olano-sandbox:latest}"
  phase "Pulling exec sandbox image ${SANDBOX_IMAGE}…"
  sbx_pulled=""
  for attempt in 1 2 3; do
    if sudo docker pull "$SANDBOX_IMAGE"; then sbx_pulled=1; break; fi
    echo "! sandbox image pull attempt ${attempt}/3 failed — retrying in $((attempt * 10))s…"
    sleep $((attempt * 10))
  done
  if [ -n "$sbx_pulled" ]; then
    sudo docker tag "$SANDBOX_IMAGE" olano-sandbox:latest
    echo "✓ Sandbox image ready (tagged olano-sandbox:latest)"
  else
    echo "! sandbox image pull failed — agent code execution stays disabled (fail-closed) until it is present"
  fi
else
  phase "Building + starting backend + dashboard (first build is slow)…"
  DC up -d --build
fi

phase "Waiting for the API to answer locally…"
for _ in $(seq 1 60); do
  if sudo docker exec olano curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
    echo "✓ Runtime API is up"; break
  fi
  sleep 5
done

# ── 4b. Keep the last good image and nothing else ────────────────────────────
# A re-run of this script (re-ship, repair, version bump) pulls or builds a new
# image set and `up -d` never removes the one it replaced; re-tagging
# olano-sandbox:latest orphans the previous sandbox image; and a local build
# leaves its whole buildkit cache behind. On a 25 GB Basic droplet that is the
# difference between a box that can update and one that cannot.
#
# Same rule the runtime's `olano_stale_images` disk action and the managed
# update agent apply, kept deliberately simple here: untag every image in a
# repo WE publish that no container was created from, then collect what that
# untagging (plus any previous update) left dangling. Never forced, never
# another publisher's image, and olano-sandbox:latest is protected because the
# runtime resolves it by name with no container running between agent sessions.
phase "Reclaiming superseded Olano images…"
IN_USE="$(sudo docker ps -aq | xargs -r sudo docker inspect --format '{{.Image}}' 2>/dev/null || true)"
sudo docker images --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' 2>/dev/null \
  | while read -r img_id img_ref; do
      case "$img_ref" in
        ghcr.io/olano-ai/*|olano-backend:*|olano-dashboard:*|olano-sandbox:*|olano-browser:*|lionclaw-sandbox:*) ;;
        *) continue ;;
      esac
      case "$img_ref" in
        olano-sandbox:latest|lionclaw-sandbox:latest|olano-browser:latest) continue ;;
        *:'<none>') continue ;;
      esac
      case "$IN_USE" in *"$img_id"*) continue ;; esac
      sudo docker rmi "$img_ref" >/dev/null 2>&1 && echo "  · dropped $img_ref"
    done
sudo docker image prune -f >/dev/null 2>&1 || true
sudo docker builder prune -f >/dev/null 2>&1 || true

# ── 5. Quick-setup: seed home + workspace + agent + admin + vault ─────────────
# Idempotent. With no credentials in .env it generates a one-time admin
# password (printed once below, kept in the olano home under
# run/initial-admin-password until changed) and a vault passphrase (kept in
# the vault's boot-key file, which the runtime unlocks from). Set
# LIONCLAW_ADMIN_EMAIL / LIONCLAW_ADMIN_PASSWORD / LIONCLAW_VAULT_PASSWORD in
# .env to choose them yourself.
# When .env carries LIONCLAW_ADMIN_PASSWORD the runtime already ran the same
# provisioning at its own startup — BEFORE its API answered above (a runtime
# with zero users serves the account-creation screen to whoever finds the URL
# first). This pass then only retries a step that failed. A non-zero exit means
# a step failed and is named in the output; the admin step itself never aborts
# the run.
#
# --skip-catalogs: the server is ALREADY UP (we blocked on its API above), and a
# running server populates all three of these itself — built-in skills through
# run_startup_upgrades on boot, and the cloud skills + archetype catalogs
# through CatalogSyncService ~20s later, which is also what keeps them fresh
# afterwards. Doing them here as well downloaded the same ~8 MB twice on every
# deploy, in the foreground, delaying the install for work that was about to
# happen anyway. Drop the flag only if this script ever stops waiting for the
# API first: with no server running, quick-setup is the only thing that
# populates them.
phase "Running quick-setup inside the container (idempotent)…"
sudo docker exec olano olano quick-setup --yes --skip-catalogs || \
  echo "! quick-setup returned non-zero (may already be set up) — continuing."

echo
DC ps
cat <<DONE

────────────────────────────────────────────────────────────────────────────
  Up. The dashboard AND the API are both served at (Caddy may need ~30s for
  the first cert):
    https://${SSLIP_HOST}/             ← dashboard_2.0
    https://${SSLIP_HOST}/api/status   ← backend health

  quick-setup created the admin account and the default agent.
DONE
# Never the configured password: this banner lands in the install log, whose
# tail an Olano-managed box ships to the console (see env-lib.sh).
print_login_hint .env
echo "────────────────────────────────────────────────────────────────────────────"
