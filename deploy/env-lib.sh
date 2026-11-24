#!/usr/bin/env bash
# env-lib.sh — shared, idempotent .env repair helpers for the deploy scripts.
#
# Source this (don't execute it):   . "$SCRIPT_DIR/env-lib.sh"
#
# Every function is safe to re-run: it does only the missing part and leaves
# existing values alone, so a bootstrap can be applied *incrementally* to a box
# that is already half set up (the common case when retrofitting an existing
# instance). Callers provide `say`/`ok`/`warn` logging functions; minimal
# fallbacks are defined here for standalone use.

command -v say  >/dev/null 2>&1 || say()  { printf '▶ %s\n' "$*"; }
command -v ok   >/dev/null 2>&1 || ok()   { printf '✓ %s\n' "$*"; }
command -v warn >/dev/null 2>&1 || warn() { printf '! %s\n' "$*" >&2; }

# ensure_env_file ENV_FILE [EXAMPLE_FILE]
# Create ENV_FILE from EXAMPLE_FILE (default: .env.example next to it) when
# missing, instead of failing the whole bootstrap. The stack boots fine with an
# unfilled .env — provider keys can be added later via the dashboard/vault —
# but warn loudly so nobody ships insecure defaults by accident.
# Returns 0 if the file existed or was created; 1 if it could not be created.
ensure_env_file() {
  env_file="$1"
  example_file="${2:-$(dirname "$env_file")/.env.example}"
  if [ -f "$env_file" ]; then
    return 0
  fi
  if [ ! -f "$example_file" ]; then
    warn "Cannot create $env_file — no template at $example_file."
    return 1
  fi
  cp "$example_file" "$env_file"
  warn "$env_file was missing — created it from $(basename "$example_file")."
  warn "Add your provider keys (e.g. ANTHROPIC_API_KEY) to $env_file or via the"
  warn "dashboard vault, and OVERRIDE the default admin/vault credentials."
  return 0
}

# pin_compose_project ENV_FILE
# Freeze COMPOSE_PROJECT_NAME to whatever this checkout ALREADY resolves to.
#
# Compose derives the project name from the basename of the directory it is
# invoked in, and prefixes every named volume with it. The Postgres volume
# declared as `<OLANO_PG_NAME>_pgdata` is therefore really
# `<dir>_<name>_pgdata` — on a checkout at ~/tech_devel/olano,
# `olano_lionclaw_pgdata` or `olano_olano_pgdata`.
#
# That volume holds the LangGraph thread checkpoints and the deepagents Store:
# every agent's conversation history. Renaming the checkout directory silently
# changes which volume compose looks for, so the next `up` starts an EMPTY
# Postgres and every agent comes back with no history. Nothing warns.
#
# Writing the CURRENT value is a deliberate no-op today and immunity tomorrow.
# We never write a hardcoded name: the correct value differs per host, and a
# fix that can be wrong per host is worse than the hazard it replaces.
pin_compose_project() {
  env_file="$1"
  [ -f "$env_file" ] || return 0
  if grep -qE '^\s*COMPOSE_PROJECT_NAME=' "$env_file"; then
    ok "COMPOSE_PROJECT_NAME already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  # Exactly what compose would derive right now, so nothing moves.
  current="$(basename "$(pwd)")"
  printf '\n# Pinned so renaming this directory cannot repoint compose at a\n# different (empty) Postgres volume. See deploy/env-lib.sh.\nCOMPOSE_PROJECT_NAME=%s\n' "$current" >> "$env_file"
  ok "Pinned COMPOSE_PROJECT_NAME=$current in $(basename "$env_file")"
  return 0
}

# strip_dashboard_token ENV_FILE
# LIONCLAW_DASHBOARD_TOKEN breaks the cookie-auth frontend — remove it.
strip_dashboard_token() {
  env_file="$1"
  if grep -qE '^\s*LIONCLAW_DASHBOARD_TOKEN=.+' "$env_file"; then
    warn "Removing LIONCLAW_DASHBOARD_TOKEN from $(basename "$env_file") (breaks cookie-auth frontend)."
    sed -i '/^\s*LIONCLAW_DASHBOARD_TOKEN=/d' "$env_file"
  fi
}

# ensure_public_url ENV_FILE HOST
# Pin LIONCLAW_PUBLIC_URL=https://HOST so chat/agent-driven MCP OAuth (and
# reconnect links in channel messages) work out of the box: providers redirect
# to <public-url>/api/mcp/oauth/callback, which the web server proxies to the
# backend. Idempotent; an existing non-empty value (custom domain, or relay
# wiring via /opt/olano/olano.env) is left alone.
# Returns 0 if it wrote the value, 1 if an existing value was kept.
ensure_public_url() {
  env_file="$1"
  host="$2"
  if grep -qE '^\s*LIONCLAW_PUBLIC_URL=.+' "$env_file"; then
    ok "LIONCLAW_PUBLIC_URL already set in $(basename "$env_file") (kept)"
    return 1
  fi
  sed -i '/^\s*LIONCLAW_PUBLIC_URL=/d' "$env_file"   # drop any empty stub
  printf 'LIONCLAW_PUBLIC_URL=https://%s\n' "$host" >> "$env_file"
  ok "Set LIONCLAW_PUBLIC_URL=https://${host} in $(basename "$env_file") (MCP OAuth redirect)"
  return 0
}

# ensure_host_home ENV_FILE
# Pin LIONCLAW_HOST_HOME — the HOST-side parent dir of the data dir (its
# basename is pinned separately by ensure_home_dir_name below) — into
# ENV_FILE so compose interpolation is deterministic in every context. The
# invoking shell's $HOME cannot carry it: `sudo docker compose` resets
# HOME=/root (Ubuntu env_reset), and systemd units without User= have no HOME
# at all (which used to interpolate the home mount to /.lionclaw — an empty
# bind). Compose reads the value from the project .env via --env-file .env.
#
# Sticky resolution (first match wins):
#   1. An existing LIONCLAW_HOST_HOME= line in ENV_FILE — idempotent re-runs.
#   2. /root/.lionclaw exists while running as a non-root user → pin /root:
#      the box previously ran as root (or via sudo-HOME) and that data must
#      keep being served — same container paths, checkpoints/sessions continue.
#   3. The invoking user's home (getent fallback for HOME-less shells).
#
# The value also reaches the container env via `env_file: ../.env` — inert
# there (the runtime keys off LIONCLAW_HOME, set separately by the compose
# file from this same variable).
ensure_host_home() {
  env_file="$1"
  existing="$(sed -n 's/^[[:space:]]*LIONCLAW_HOST_HOME=//p' "$env_file" | head -n1)"
  if [ -n "$existing" ]; then
    export LIONCLAW_HOST_HOME="$existing"
    ok "LIONCLAW_HOST_HOME=${existing} already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  home_dir="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
  # /root is mode 700, so a non-root service user needs sudo just to SEE the
  # legacy dir — try sudo first (NOPASSWD on provisioned boxes), else direct.
  if [ "$home_dir" != /root ] \
     && { sudo -n test -d /root/.lionclaw 2>/dev/null || [ -d /root/.lionclaw ]; }; then
    home_dir=/root
    warn "Legacy data found at /root/.lionclaw — pinning LIONCLAW_HOST_HOME=/root (data continuity)."
  fi
  sed -i '/^[[:space:]]*LIONCLAW_HOST_HOME=/d' "$env_file"   # drop any empty stub
  printf 'LIONCLAW_HOST_HOME=%s\n' "$home_dir" >> "$env_file"
  export LIONCLAW_HOST_HOME="$home_dir"
  ok "Pinned LIONCLAW_HOST_HOME=${home_dir} in $(basename "$env_file")"
}

# _dir_present DIR / _is_source_checkout DIR
# Probes that also see through a 700 /root from the service user: sudo -n
# first (NOPASSWD on provisioned boxes), a direct test otherwise, exactly as
# ensure_host_home does. A source checkout is a directory carrying
# pyproject.toml or .git; an Olano home holds neither.
_dir_present() {
  sudo -n test -d "$1" 2>/dev/null || [ -d "$1" ]
}
_is_source_checkout() {
  sudo -n test -e "$1/pyproject.toml" 2>/dev/null || [ -e "$1/pyproject.toml" ] \
    || sudo -n test -e "$1/.git" 2>/dev/null || [ -e "$1/.git" ]
}

# ensure_home_dir_name ENV_FILE
# Pin OLANO_HOME_DIR — the BASENAME of the runtime data dir under
# LIONCLAW_HOST_HOME — into ENV_FILE. docker-compose.allinone.yml interpolates
# it into the home env vars, the workspace env, and the bind mount, so the pin
# is what decides whether a box lives at ~/.olano, at a visible ~/olano, or at
# the original ~/.lionclaw. MUST run after ensure_host_home (the probes use
# LIONCLAW_HOST_HOME). Mirrors olano/config/home.py:get_olano_home().
#
# Sticky resolution (first match wins):
#   1. An existing OLANO_HOME_DIR= line in ENV_FILE — idempotent re-runs.
#   2. ${LIONCLAW_HOST_HOME}/.olano exists → pin .olano (the current layout).
#   3. ${LIONCLAW_HOST_HOME}/olano exists and is NOT a source checkout → pin
#      olano (a box provisioned while the visible name was the default).
#      Existing boxes are never migrated here. A ~/olano that IS a checkout
#      is never the data dir: the manual deploys (the fleet tooling's ship.sh, tc-manual-*.sh) put
#      this repository there, and the data must never land in a git work tree.
#   4. ${LIONCLAW_HOST_HOME}/.lionclaw exists → pin .lionclaw: a box with the
#      original layout must keep serving it (sudo probe covers the 700 /root
#      case, same as ensure_host_home).
#   5. Fresh box → pin .olano.
ensure_home_dir_name() {
  env_file="$1"
  existing="$(sed -n 's/^[[:space:]]*OLANO_HOME_DIR=//p' "$env_file" | head -n1)"
  if [ -n "$existing" ]; then
    export OLANO_HOME_DIR="$existing"
    ok "OLANO_HOME_DIR=${existing} already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  host_home="${LIONCLAW_HOST_HOME:-${HOME:-/root}}"
  dir_name=.olano
  if _dir_present "${host_home}/.olano"; then
    dir_name=.olano
  elif _dir_present "${host_home}/olano" && ! _is_source_checkout "${host_home}/olano"; then
    dir_name=olano
    warn "Existing data found at ${host_home}/olano — pinning OLANO_HOME_DIR=olano (data continuity)."
  elif _dir_present "${host_home}/.lionclaw"; then
    dir_name=.lionclaw
    warn "Legacy data found at ${host_home}/.lionclaw — pinning OLANO_HOME_DIR=.lionclaw (data continuity)."
  fi
  sed -i '/^[[:space:]]*OLANO_HOME_DIR=/d' "$env_file"   # drop any empty stub
  printf 'OLANO_HOME_DIR=%s\n' "$dir_name" >> "$env_file"
  export OLANO_HOME_DIR="$dir_name"
  ok "Pinned OLANO_HOME_DIR=${dir_name} in $(basename "$env_file")"
}

# ensure_mem_limit ENV_FILE
# Pin LIONCLAW_MEM_LIMIT (the olano container's mem_limit) to 75% of host
# RAM. Without a cap, one heavy agent shell command (a full test suite, a big
# pip build) can swap-thrash the whole VM into a silent freeze; with it, Docker
# OOM-kills the container and `restart: unless-stopped` brings it back in
# seconds. 75% leaves headroom for the OS, dockerd, the Postgres sidecar, and
# Caddy. Sticky like ensure_host_home: an existing value (including a manual
# override) is kept.
ensure_mem_limit() {
  env_file="$1"
  existing="$(sed -n 's/^[[:space:]]*LIONCLAW_MEM_LIMIT=//p' "$env_file" | head -n1)"
  if [ -n "$existing" ]; then
    export LIONCLAW_MEM_LIMIT="$existing"
    ok "LIONCLAW_MEM_LIMIT=${existing} already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  mem_limit="$(awk '/^MemTotal:/ {printf "%dm", $2 * 0.75 / 1024}' /proc/meminfo 2>/dev/null)"
  if [ -z "$mem_limit" ] || [ "$mem_limit" = "0m" ]; then
    warn "Could not read host RAM from /proc/meminfo — leaving LIONCLAW_MEM_LIMIT unset (no cap)."
    return 0
  fi
  sed -i '/^[[:space:]]*LIONCLAW_MEM_LIMIT=/d' "$env_file"   # drop any empty stub
  printf 'LIONCLAW_MEM_LIMIT=%s\n' "$mem_limit" >> "$env_file"
  export LIONCLAW_MEM_LIMIT="$mem_limit"
  ok "Pinned LIONCLAW_MEM_LIMIT=${mem_limit} (75% of host RAM) in $(basename "$env_file")"
}

# ensure_pg_password ENV_FILE
# Pin LIONCLAW_PG_PASSWORD — the bundled Postgres sidecar's password — to a
# random value. Every compose file falls back to the literal `lionclaw` when it
# is unset (POSTGRES_PASSWORD and both DB URLs interpolate
# ${LIONCLAW_PG_PASSWORD:-lionclaw}), so a box that never set it keeps every
# agent's thread history behind a password printed in this repo. Postgres reads
# POSTGRES_PASSWORD only when it initialises the data dir, so this MUST run
# before the first `up`. Sticky like ensure_mem_limit: an existing value
# (including a manual override, or one exported in the calling shell) is kept —
# rotating it here would lock the runtime out of a volume initialised with the
# old one. For the same reason a box whose data dir ALREADY EXISTS without a
# pinned value is pinned to the `lionclaw` it was initialised with, never to a
# fresh random one: a retrofit bootstrap must leave an existing deployment
# reachable (rotate by hand with ALTER USER, then edit the pin). Only a fresh
# box gets a random password.
ensure_pg_password() {
  env_file="$1"
  existing="$(sed -n 's/^[[:space:]]*LIONCLAW_PG_PASSWORD=//p' "$env_file" | head -n1)"
  if [ -n "$existing" ]; then
    export LIONCLAW_PG_PASSWORD="$existing"
    ok "LIONCLAW_PG_PASSWORD already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  pg_password="${LIONCLAW_PG_PASSWORD:-}"
  if [ -z "$pg_password" ] && _pg_data_initialised "$env_file"; then
    pg_password=lionclaw
    warn "Existing Postgres data found with no pinned password — pinning the compose default 'lionclaw' it was initialised with (rotate with ALTER USER, then edit LIONCLAW_PG_PASSWORD)."
  fi
  if [ -z "$pg_password" ]; then
    pg_password="$(openssl rand -hex 24 2>/dev/null || true)"
  fi
  if [ -z "$pg_password" ]; then
    warn "Could not generate a Postgres password (openssl rand failed) — leaving LIONCLAW_PG_PASSWORD unset (compose default 'lionclaw')."
    return 0
  fi
  sed -i '/^[[:space:]]*LIONCLAW_PG_PASSWORD=/d' "$env_file"   # drop any empty stub
  printf 'LIONCLAW_PG_PASSWORD=%s\n' "$pg_password" >> "$env_file"
  export LIONCLAW_PG_PASSWORD="$pg_password"
  ok "Pinned LIONCLAW_PG_PASSWORD in $(basename "$env_file")"
}

# _pg_data_initialised ENV_FILE
# True when this box already has an initialised data dir for the sidecar: a
# bind path (LIONCLAW_PGDATA from the shell or ENV_FILE) holding PG_VERSION, or
# a project-prefixed `*_lionclaw_pgdata` / `*_olano_pgdata` named volume.
# Docker may need sudo here (the service user's docker-group membership only
# applies to new logins).
_pg_data_initialised() {
  [ -n "$(_pg_initialised_name "$1")" ]
}

# _pg_initialised_name ENV_FILE
# Print the role/database name an existing sidecar data dir was initialised
# with, or nothing when there is none. A project-prefixed `*_lionclaw_pgdata`
# volume says lionclaw, a `*_olano_pgdata` volume says olano (the older name
# wins if both exist: it is the one with history). A bind path holding
# PG_VERSION cannot say which role it carries, so it reads as lionclaw: every
# box initialised before OLANO_PG_NAME existed is one, and a box initialised
# after it keeps the pin in the .env that lives beside the data.
_pg_initialised_name() {
  pgdata="${LIONCLAW_PGDATA:-$(sed -n 's/^[[:space:]]*LIONCLAW_PGDATA=//p' "$1" | head -n1)}"
  case "$pgdata" in
    /*) if [ -f "${pgdata}/PG_VERSION" ]; then echo lionclaw; return 0; fi ;;
  esac
  volumes="$(docker volume ls -q 2>/dev/null || sudo -n docker volume ls -q 2>/dev/null || true)"
  if printf '%s\n' "$volumes" | grep -q '_lionclaw_pgdata$'; then echo lionclaw; return 0; fi
  if printf '%s\n' "$volumes" | grep -q '_olano_pgdata$'; then echo olano; return 0; fi
  return 0
}

# ensure_pg_name ENV_FILE
# Pin OLANO_PG_NAME — the bundled Postgres sidecar's role, database and
# named-volume stem — into ENV_FILE. Every compose file interpolates
# ${OLANO_PG_NAME:-lionclaw} into POSTGRES_USER, POSTGRES_DB, the healthcheck,
# both DB URLs and the volume key, so the pin decides whether a box runs as
# olano/olano on <project>_olano_pgdata or as the original lionclaw/lionclaw
# on <project>_lionclaw_pgdata. Postgres applies the names only when it
# initialises the data dir, so this MUST run before the first `up`, and it is
# sticky for the same reason ensure_pg_password is: an existing line is kept,
# and a box whose data already exists without a pin is pinned to the name it
# was initialised with (renaming a live role is a by-hand ALTER ROLE / ALTER
# DATABASE, then the pin). Only a fresh box gets olano. The compose fallback
# stays lionclaw so an unpinned legacy box hand-running compose keeps its data.
ensure_pg_name() {
  env_file="$1"
  existing="$(sed -n 's/^[[:space:]]*OLANO_PG_NAME=//p' "$env_file" | head -n1)"
  if [ -n "$existing" ]; then
    export OLANO_PG_NAME="$existing"
    ok "OLANO_PG_NAME=${existing} already pinned in $(basename "$env_file") (kept)"
    return 0
  fi
  pg_name="$(_pg_initialised_name "$env_file")"
  if [ -n "$pg_name" ]; then
    warn "Existing Postgres data found with no pinned name — pinning OLANO_PG_NAME=${pg_name}, the role and database it was initialised with."
  else
    pg_name=olano
  fi
  sed -i '/^[[:space:]]*OLANO_PG_NAME=/d' "$env_file"   # drop any empty stub
  printf 'OLANO_PG_NAME=%s\n' "$pg_name" >> "$env_file"
  export OLANO_PG_NAME="$pg_name"
  ok "Pinned OLANO_PG_NAME=${pg_name} in $(basename "$env_file")"
}

# ensure_relay_wiring ENV_FILE [RELAY_URL] [RELAY_SECRET] [INSTANCE_ID]
# Wire the central MCP OAuth relay so pre-registration providers (e.g. HubSpot)
# and domain-less droplets across the whole fleet share ONE registered redirect
# URI — <relay>/mcp/callback — instead of each box's own https://<ip>.sslip.io
# origin. A provider like HubSpot pins one pre-registered redirect URL and rejects
# every per-box sslip.io host, so the relay is the only redirect that scales past
# a single instance.
#
# ATOMIC by design: the runtime RAISES when LIONCLAW_MCP_RELAY_URL is set without
# a secret, which breaks *all* MCP OAuth (not just HubSpot). So this writes the
# URL only when a secret is also available — passed here (arg/env) or already
# present in ENV_FILE. With no secret anywhere it writes NOTHING and returns 1,
# leaving the caller to fall back to ensure_public_url (origin mode, unchanged).
#
# Idempotent: existing non-empty values (a custom relay, a hand-set secret) are
# kept. Inputs default from the script env; the relay URL defaults to
# https://auth.olano.ai. Returns 0 when the relay is (now or already) wired,
# 1 when left unwired.
ensure_relay_wiring() {
  env_file="$1"
  relay_url="${2:-${LIONCLAW_MCP_RELAY_URL:-https://auth.olano.ai}}"
  relay_secret="${3:-${LIONCLAW_MCP_RELAY_SECRET:-}}"
  instance_id="${4:-${LIONCLAW_INSTANCE_ID:-}}"

  # A secret already in the env file counts as present — don't rewrite it, just
  # make sure the URL (and instance id) are wired alongside it.
  secret_in_file=0
  if grep -qE '^\s*LIONCLAW_MCP_RELAY_SECRET=.+' "$env_file"; then
    secret_in_file=1
  fi
  if [ "$secret_in_file" -eq 0 ] && [ -z "$relay_secret" ]; then
    return 1   # no secret anywhere → leave the relay unwired (origin fallback)
  fi

  if ! grep -qE '^\s*LIONCLAW_MCP_RELAY_URL=.+' "$env_file"; then
    sed -i '/^\s*LIONCLAW_MCP_RELAY_URL=/d' "$env_file"   # drop any empty stub
    printf 'LIONCLAW_MCP_RELAY_URL=%s\n' "$relay_url" >> "$env_file"
  fi
  if [ "$secret_in_file" -eq 0 ] && [ -n "$relay_secret" ]; then
    sed -i '/^\s*LIONCLAW_MCP_RELAY_SECRET=/d' "$env_file"   # drop any empty stub
    printf 'LIONCLAW_MCP_RELAY_SECRET=%s\n' "$relay_secret" >> "$env_file"
  fi
  if [ -n "$instance_id" ] && ! grep -qE '^\s*LIONCLAW_INSTANCE_ID=.+' "$env_file"; then
    sed -i '/^\s*LIONCLAW_INSTANCE_ID=/d' "$env_file"
    printf 'LIONCLAW_INSTANCE_ID=%s\n' "$instance_id" >> "$env_file"
  fi
  ok "Wired MCP OAuth relay → ${relay_url}/mcp/callback in $(basename "$env_file")"
  return 0
}

# print_login_hint ENV_FILE
# The sign-in lines of the bootstrap's closing banner. That banner is not
# private: on an Olano-managed box the tail of the install log goes to the
# control plane and is shown to the customer in the console. So no password is
# ever printed here, only where it is kept: the configured one when ENV_FILE
# sets LIONCLAW_ADMIN_PASSWORD, otherwise the one-time password quick-setup
# generated, which it printed once in its own output and keeps on the box
# (readable by root only) until the account's password is changed. The
# runtime container takes its environment from this file (compose env_file),
# so the file is the whole truth; the calling shell's environment is not.
# Safe under `set -euo pipefail`, with ENV_FILE missing or partial.
print_login_hint() {
  env_file="$1"
  admin_email="$(sed -n 's/^[[:space:]]*LIONCLAW_ADMIN_EMAIL=//p' "$env_file" 2>/dev/null | tail -n1 || true)"
  admin_email="${admin_email%\"}"; admin_email="${admin_email#\"}"
  admin_email="${admin_email%\'}"; admin_email="${admin_email#\'}"
  if grep -qE '^[[:space:]]*LIONCLAW_ADMIN_PASSWORD=.+' "$env_file" 2>/dev/null; then
    printf '  Sign in as %s with the admin password configured for this\n' \
      "${admin_email:-the LIONCLAW_ADMIN_EMAIL in .env}"
    printf '  install (LIONCLAW_ADMIN_PASSWORD in .env). On an Olano-managed instance\n'
    printf "  that is the temporary password on the console's Security tab, also sent\n"
    printf '  in the welcome email. Replace it after signing in.\n'
    return 0
  fi
  # The note lives in the olano home, which the compose file places at
  # ${LIONCLAW_HOST_HOME:-$HOME}/${OLANO_HOME_DIR:-.lionclaw} on the host.
  host_home="$(sed -n 's/^[[:space:]]*LIONCLAW_HOST_HOME=//p' "$env_file" 2>/dev/null | tail -n1 || true)"
  host_home="${host_home%\"}"; host_home="${host_home#\"}"
  home_dir="$(sed -n 's/^[[:space:]]*OLANO_HOME_DIR=//p' "$env_file" 2>/dev/null | tail -n1 || true)"
  home_dir="${home_dir%\"}"; home_dir="${home_dir#\"}"
  note_path="${host_home:-${HOME:-/root}}/${home_dir:-.lionclaw}/run/initial-admin-password"
  login_email="${admin_email:-admin@example.com}"
  cat <<HINT
  Admin login: ${login_email}, with the one-time password quick-setup
  generated. It was printed once in the quick-setup output above and is kept
  on this box, readable by root only, until you change it:
    sudo cat ${note_path}
  Set your own password after signing in (account menu), or reset it:
    sudo docker exec olano olano users reset-password --email ${login_email}

  To choose the credentials yourself, set LIONCLAW_ADMIN_EMAIL /
  LIONCLAW_ADMIN_PASSWORD / LIONCLAW_VAULT_PASSWORD in .env before bootstrap.
HINT
}
