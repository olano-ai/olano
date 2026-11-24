#!/usr/bin/env bash
# Olano self-hosted: first-run setup for deploy/self-host/docker-compose.yml.
#
#   bash setup.sh                           # picks the address for you (see below)
#   bash setup.sh --host olano.example.com  # https, Let's Encrypt (DNS must point here)
#   bash setup.sh --host auto               # https on <your-ip-with-dashes>.sslip.io
#   bash setup.sh --local                   # http://localhost:3200 only (same as --host local)
#   bash setup.sh --sandbox                 # each agent's shell in its own container
#   bash setup.sh --no-sandbox              # back to running shells in the runtime container
#   bash setup.sh --olano-key sk-olano-...  # run on Olano credits from the first boot
#   bash setup.sh --no-start                # write .env only
#
# With no address option on the first run, setup.sh asks a what-is-my-ip
# service for this machine's public IPv4: when that address sits on one of the
# machine's own network interfaces (a VPS with a public IP), it uses
# --host auto; otherwise (a laptop, a home server, a cloud VM behind NAT) it
# stays on http://localhost:3200. Later runs keep whatever address .env holds.
#
# Idempotent: it writes .env from .env.example when missing, pins only what is
# not pinned yet (a random Postgres password, a one-time setup code, the
# compose project name, the memory cap) and leaves every existing value alone,
# except the ones an option passed now asks it to change. It then pulls the
# images and starts the stack. Re-run it after editing .env.
#
# What it never does: write a deployment token, an installation manifest or
# anything else that belongs to a cloud.olano.ai-managed deployment. Those are
# written by the control plane on the boxes it provisions; on a self-hosted box
# they would quarantine the runtime.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/.env"
HOST=""
OLANO_KEY=""
START=1
SANDBOX=""   # "", "on" or "off"

usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:?--host needs a hostname, auto or local}"; shift 2 ;;
    --host=*) HOST="${1#--host=}"; shift ;;
    --local) HOST="local"; shift ;;
    --sandbox) SANDBOX="on"; shift ;;
    --no-sandbox) SANDBOX="off"; shift ;;
    --olano-key) OLANO_KEY="${2:?--olano-key needs a key}"; shift 2 ;;
    --olano-key=*) OLANO_KEY="${1#--olano-key=}"; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see bash setup.sh --help)" >&2; exit 2 ;;
  esac
done

case "$HOST" in
  http://*|https://*) echo "--host takes a bare hostname (olano.example.com), auto or local" >&2; exit 2 ;;
esac

say()  { printf '▶ %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

# read_value NAME: the value of NAME= in .env, with surrounding quotes and
# whitespace stripped (a hand-edited `OLANO_DATA_DIR="/srv/olano"` is the
# same directory as the unquoted spelling; compose strips the quotes too).
read_value() {
  sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | head -n1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

has_value() { grep -qE "^[[:space:]]*$1=" "$ENV_FILE"; }

# pin NAME VALUE: append NAME=VALUE unless NAME is already set in .env.
pin() {
  if has_value "$1"; then
    ok "$1 already set in .env (kept)"
    return 0
  fi
  printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  ok "Set $1 in .env"
}

# set_value NAME VALUE: replace or append NAME=VALUE (for options passed now).
set_value() {
  if has_value "$1"; then
    if [ "$(read_value "$1")" = "$2" ]; then
      return 0
    fi
    sed -i.bak "s|^[[:space:]]*$1=.*|$1=$2|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
  ok "Set $1 in .env"
}

# A one-time code in three groups of four, from an alphabet without 0/O/1/I/L
# so it can be copied by hand from a terminal.
new_setup_code() {
  local raw
  raw="$(LC_ALL=C tr -dc 'ABCDEFGHJKMNPQRSTUVWXYZ23456789' </dev/urandom 2>/dev/null | head -c 12 || true)"
  printf '%s-%s-%s' "${raw:0:4}" "${raw:4:4}" "${raw:8:4}"
}

is_ipv4() {
  [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

# public_ipv4: this machine's public IPv4 as the internet sees it, or "".
# Two independent services, so one being down does not change the answer.
public_ipv4() {
  local url ip
  for url in https://api.ipify.org https://ipv4.icanhazip.com; do
    ip="$(curl -fsS -4 --connect-timeout 4 --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if is_ipv4 "$ip"; then
      printf '%s' "$ip"
      return 0
    fi
  done
  return 0
}

# ip_is_local IP: whether IP is assigned to one of this machine's interfaces
# (a VPS with its public address attached), as opposed to a NAT gateway's.
ip_is_local() {
  # Captured first, never `cmd | grep -q`: grep -q exits at the first match,
  # and under pipefail the writer's SIGPIPE would turn a match into a failure.
  local ip="$1" listing=""
  if command -v ip >/dev/null 2>&1; then
    listing="$(ip -4 -o addr show 2>/dev/null || true)"
  elif command -v ifconfig >/dev/null 2>&1; then
    listing="$(ifconfig 2>/dev/null | sed -n 's/.*inet \(addr:\)\{0,1\}\([0-9.]*\).*/ \2\//p' || true)"
  fi
  case "$listing" in *" $ip/"*) return 0 ;; esac
  return 1
}

sslip_name() { printf '%s.sslip.io' "${1//./-}"; }

# ── 1. .env ───────────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  if [ ! -f "$HERE/.env.example" ]; then
    warn ".env.example is missing next to setup.sh; download it with the other files and re-run."
    exit 1
  fi
  cp "$HERE/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "Created .env from .env.example"
fi
FIRST_RUN=0
has_value OLANO_SITE_ADDRESS || FIRST_RUN=1

# The compose project name prefixes every volume. Compose derives it from the
# directory name ("self-host"), and a directory rename would silently point
# the stack at an empty Postgres volume: freeze it now.
pin COMPOSE_PROJECT_NAME olano

# Postgres reads its password only when it initialises the volume, so this has
# to exist before the first start and never change afterwards. The compose
# file refuses to start without it.
if ! has_value LIONCLAW_PG_PASSWORD; then
  if command -v docker >/dev/null 2>&1 && docker volume inspect olano_pgdata >/dev/null 2>&1; then
    warn "A Postgres volume (olano_pgdata) exists but .env has no LIONCLAW_PG_PASSWORD."
    warn "Put the password that volume was created with into .env, then re-run."
    exit 1
  fi
  pw="$(openssl rand -hex 24 2>/dev/null || head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  pin LIONCLAW_PG_PASSWORD "$pw"
fi
pin OLANO_PG_NAME olano

# The one-time code the dashboard asks for before it creates the first admin
# account, so nobody who reaches the address before you can claim the box.
# It stops working the moment that account exists.
pin OLANO_SETUP_CODE "$(new_setup_code)"

# 75% of the host's RAM for the runtime container, so a runaway agent command
# is OOM-killed and restarted instead of freezing the machine.
if ! has_value OLANO_MEM_LIMIT; then
  mem="$(awk '/^MemTotal:/ {printf "%dm", $2 * 0.75 / 1024}' /proc/meminfo 2>/dev/null || true)"
  if [ -n "$mem" ] && [ "$mem" != "0m" ]; then
    pin OLANO_MEM_LIMIT "$mem"
  fi
fi

# ── 2. the address ────────────────────────────────────────────────────────────
case "$HOST" in
  auto)
    say "Looking up this machine's public IPv4…"
    ip="$(public_ipv4)"
    if [ -z "$ip" ]; then
      warn "Could not determine this machine's public IPv4 (api.ipify.org and icanhazip.com"
      warn "did not answer). Pass a hostname instead: bash setup.sh --host olano.example.com"
      exit 2
    fi
    HOST="$(sslip_name "$ip")"
    ok "Public IPv4 $ip: using $HOST"
    ;;
  "")
    if [ "$FIRST_RUN" -eq 1 ]; then
      say "Choosing an address: looking up this machine's public IPv4…"
      ip="$(public_ipv4)"
      if [ -n "$ip" ] && ip_is_local "$ip"; then
        HOST="$(sslip_name "$ip")"
        ok "Public IPv4 $ip is on this machine: serving https on $HOST"
      else
        HOST="local"
        if [ -n "$ip" ]; then
          ok "Public IPv4 $ip is not on this machine (NAT): staying on localhost"
        else
          ok "No public IPv4 found: staying on localhost"
        fi
      fi
    fi
    ;;
esac

port="$(read_value OLANO_HTTP_PORT)"
case "$HOST" in
  "") ;;  # a re-run with no address option: keep what .env holds
  local)
    set_value OLANO_SITE_ADDRESS ":80"
    # An older https setup published the plain port as 80; it is loopback-only now.
    if [ "$port" = "80" ]; then set_value OLANO_HTTP_PORT 3200; port=3200; fi
    set_value LIONCLAW_PUBLIC_URL "http://localhost:${port:-3200}"
    ;;
  *)
    set_value OLANO_SITE_ADDRESS "$HOST"
    set_value LIONCLAW_PUBLIC_URL "https://$HOST"
    ;;
esac
site="$(read_value OLANO_SITE_ADDRESS)"
HTTPS=0
case "$site" in ""|":80"|:*) ;; *) HTTPS=1 ;; esac
# In https mode ports 80 and 443 come from docker-compose.https.yml. A setup
# from before that file put 80 in OLANO_HTTP_PORT, which would now collide.
if [ "$HTTPS" -eq 1 ] && [ "$(read_value OLANO_HTTP_PORT)" = "80" ]; then
  set_value OLANO_HTTP_PORT 3200
fi

# ── 3. Olano credits ──────────────────────────────────────────────────────────
if [ -n "$OLANO_KEY" ]; then
  case "$OLANO_KEY" in
    sk-olano-*) set_value OLANO_CREDITS_KEY "$OLANO_KEY" ;;
    *) echo "an Olano API key starts with sk-olano- (cloud.olano.ai → API keys)" >&2; exit 2 ;;
  esac
fi

# ── 4. the data directory ─────────────────────────────────────────────────────
# A relative OLANO_DATA_DIR is relative to the compose file, exactly as compose
# resolves it.
data_dir="$(read_value OLANO_DATA_DIR)"
data_dir="${data_dir:-./data}"
case "$data_dir" in /*) ;; *) data_dir="$HERE/${data_dir#./}" ;; esac

# ── 5. sandbox mode ───────────────────────────────────────────────────────────
if [ -z "$SANDBOX" ]; then
  case "$(read_value OLANO_SANDBOX_EXEC)" in 1|true|yes|on) SANDBOX="on" ;; *) SANDBOX="off" ;; esac
elif [ "$SANDBOX" = "off" ] && has_value OLANO_SANDBOX_EXEC; then
  set_value OLANO_SANDBOX_EXEC 0
fi
if [ "$SANDBOX" = "on" ]; then
  # The sandboxes are started by the host's Docker daemon, which mounts each
  # workspace by the path the runtime sees: that path has to be the host path.
  set_value OLANO_DATA_DIR "$data_dir"
  set_value OLANO_SANDBOX_EXEC 1
fi

# ── 6. which compose files ────────────────────────────────────────────────────
compose_files="docker-compose.yml"
[ "$HTTPS" -eq 1 ] && compose_files="$compose_files:docker-compose.https.yml"
[ "$SANDBOX" = "on" ] && compose_files="$compose_files:docker-compose.sandbox.yml"
set_value COMPOSE_FILE "$compose_files"

# Pre-create the data directory owned by the invoking user, so Docker does not
# create it root-owned on the first start.
mkdir -p "$data_dir"

port="$(read_value OLANO_HTTP_PORT)"
if [ "$HTTPS" -eq 1 ]; then
  url="https://$site"
else
  url="http://localhost:${port:-3200}"
fi
code="$(read_value OLANO_SETUP_CODE)"

print_hints() {
  if [ "$HTTPS" -eq 1 ]; then
    echo "  Ports 80 and 443 must be reachable from the internet (open 22, 80 and 443 in"
    echo "  your cloud firewall, nothing else). Caddy gets the certificate on the first"
    echo "  request; until then, or if it fails, the dashboard is still reachable through"
    echo "  an SSH tunnel: ssh -L ${port:-3200}:localhost:${port:-3200} you@this-server, then http://localhost:${port:-3200}"
    case "$site" in
      *.sslip.io) echo "  sslip.io shares Let's Encrypt's rate limit with every other user; a domain of"
                  echo "  your own (bash setup.sh --host olano.example.com) is the reliable route." ;;
    esac
  else
    echo "  This address only answers on this machine. To reach the dashboard from the internet:"
    echo "    bash setup.sh --host olano.example.com   # a domain whose DNS points here (https)"
    echo "    bash setup.sh --host auto                # https on <your-ip>.sslip.io; for a cloud VM"
    echo "                                             # behind NAT (AWS EC2 and similar) whose ports"
    echo "                                             # 80 and 443 are forwarded to this machine"
  fi
}

print_code() {
  echo
  echo "  ================================================================"
  echo "    Setup code:  $code"
  echo "    The dashboard asks for it once, to create the first admin"
  echo "    account. It is also in .env (OLANO_SETUP_CODE) and in the log"
  echo "    (docker compose logs olano)."
  echo "  ================================================================"
  echo
}

if [ "$START" -eq 0 ]; then
  ok ".env is ready; start with: docker compose up -d"
  echo "  Address: $url"
  [ "$SANDBOX" = "on" ] && echo "  Sandbox mode: pull the image first (re-run without --no-start, or see the README)."
  print_code
  print_hints
  exit 0
fi

# ── 7. start ──────────────────────────────────────────────────────────────────
if ! docker compose version >/dev/null 2>&1; then
  warn "docker compose is not available. Install Docker Engine with the Compose plugin (https://docs.docker.com/engine/install/) and re-run."
  exit 1
fi
cd "$HERE"

if [ "$SANDBOX" = "on" ]; then
  if [ ! -S /var/run/docker.sock ]; then
    warn "Sandbox mode needs the Docker socket at /var/run/docker.sock and it is not there."
    warn "Run Docker Engine (or Docker Desktop) on this machine, or re-run with --no-sandbox."
    exit 1
  fi
  sandbox_ref="$(read_value OLANO_SANDBOX_IMAGE)"
  sandbox_ref="${sandbox_ref:-ghcr.io/olano-ai/olano-sandbox:latest}"
  say "Pulling the sandbox image ($sandbox_ref)…"
  if docker pull "$sandbox_ref"; then
    # The runtime starts the local tag olano-sandbox:latest.
    docker tag "$sandbox_ref" olano-sandbox:latest
    ok "Tagged $sandbox_ref as olano-sandbox:latest"
  elif docker image inspect olano-sandbox:latest >/dev/null 2>&1; then
    warn "The sandbox image pull failed; using the olano-sandbox:latest already on this machine."
  else
    warn "The sandbox image pull failed and no olano-sandbox:latest exists here, so agents"
    warn "would have no shell at all. The image is public, so this is usually the network:"
    warn "check that this machine reaches ghcr.io and re-run, or re-run with --no-sandbox."
    exit 1
  fi
fi

# Every image the compose files name is already on this machine: a failed pull
# (no network, a registry hiccup, Docker Hub's anonymous rate limit) can then
# fall back to what is here instead of stopping, as the sandbox pull above does.
all_images_local() {
  local image found=0
  while IFS= read -r image; do
    [ -z "$image" ] && continue
    found=1
    docker image inspect "$image" >/dev/null 2>&1 || return 1
  done < <(docker compose config --images 2>/dev/null)
  [ "$found" -eq 1 ]
}

say "Pulling the images…"
if ! docker compose pull; then
  if all_images_local; then
    warn "The image pull failed; starting with the images already on this machine."
    warn "Re-run setup.sh later to update them."
  else
    warn "The image pull failed. The Olano images (ghcr.io) and Postgres (Docker Hub) are"
    warn "public, so this is usually the network: re-run, and completed layers are kept."
    warn "'429 Too Many Requests' from registry-1.docker.io is Docker Hub's limit on"
    warn "anonymous pulls from your address; 'docker login' with a free Docker account"
    warn "raises it."
    exit 1
  fi
fi
say "Starting…"
docker compose up -d --remove-orphans

say "Waiting for the runtime…"
for _ in $(seq 1 60); do
  if docker exec olano curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
    ok "Olano is up: $url"
    status="$(docker exec olano curl -fsS --max-time 5 http://127.0.0.1:8765/api/auth/setup-status 2>/dev/null || true)"
    case "$status" in
      *'"setup_code_required":false'*)
        echo "  Already set up: sign in with your account." ;;
      *)
        echo "  Open it in a browser; the setup wizard runs on the first visit."
        print_code ;;
    esac
    print_hints
    exit 0
  fi
  sleep 5
done
warn "The runtime has not answered yet. Watch it with: docker compose logs -f olano"
print_code
print_hints
