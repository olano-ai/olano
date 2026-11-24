#!/usr/bin/env bash
# Olano self-hosted: one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/olano-ai/olano/main/deploy/self-host/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --host olano.example.com --sandbox
#
# Everything after `--` goes to setup.sh unchanged (bash setup.sh --help lists
# the options). What this does, in order:
#
#   1. checks for Docker Engine with the Compose plugin. On Linux it offers to
#      install Docker with Docker's own script (https://get.docker.com), only
#      after asking you; with no terminal to ask on, it stops and says how.
#   2. creates the install directory: $OLANO_DIR, or ~/olano by default.
#   3. downloads the self-host files into it (docker-compose.yml, the two
#      override files, Caddyfile, .env.example, setup.sh). Re-running updates
#      them; your .env and data directory are never touched.
#   4. runs setup.sh there with your options.
#
# Environment: OLANO_DIR (install directory), OLANO_REF (git branch or tag to
# download from, default main).
set -euo pipefail

OLANO_DIR="${OLANO_DIR:-$HOME/olano}"
OLANO_REF="${OLANO_REF:-main}"
BASE_URL="${OLANO_INSTALL_BASE_URL:-https://raw.githubusercontent.com/olano-ai/olano/$OLANO_REF/deploy/self-host}"
FILES=(docker-compose.yml docker-compose.https.yml docker-compose.sandbox.yml Caddyfile .env.example setup.sh)

say()  { printf '▶ %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

# ask QUESTION: yes/no from the terminal. stdin is this script when piped from
# curl, so the answer must come from /dev/tty; with no terminal, the answer is no.
ask() {
  local answer=""
  if ! { : </dev/tty; } 2>/dev/null; then
    return 1
  fi
  printf '%s [y/N] ' "$1" >/dev/tty
  read -r answer </dev/tty || return 1
  case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

fetch() {  # fetch URL DEST
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    warn "Neither curl nor wget is installed; install one and re-run."
    exit 1
  fi
}

docker_ready() { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; }

# ── 1. Docker ─────────────────────────────────────────────────────────────────
if ! docker_ready; then
  case "$(uname -s)" in
    Linux)
      if command -v docker >/dev/null 2>&1; then
        warn "Docker is installed but the Compose plugin is not (docker compose version fails)."
      else
        warn "Docker is not installed."
      fi
      if ask "Install Docker Engine and the Compose plugin now with Docker's official script (get.docker.com)?"; then
        tmp_script="$(mktemp)"
        fetch https://get.docker.com "$tmp_script"
        if [ "$(id -u)" -eq 0 ]; then
          sh "$tmp_script"
        else
          sudo sh "$tmp_script"
        fi
        rm -f "$tmp_script"
        if [ "$(id -u)" -ne 0 ] && ! docker info >/dev/null 2>&1; then
          sudo usermod -aG docker "$USER" 2>/dev/null || true
          warn "Docker is installed. Your account was added to the docker group, which takes"
          warn "effect at your next login: log out and back in (or run: newgrp docker), then"
          warn "run this installer again."
          exit 1
        fi
      else
        warn "Not installing Docker. Install Docker Engine with the Compose plugin"
        warn "(https://docs.docker.com/engine/install/) and run this installer again."
        warn "(When piped from curl with no terminal to ask on, the answer is always no.)"
        exit 1
      fi
      ;;
    Darwin)
      warn "Docker is not available. Install Docker Desktop (https://docs.docker.com/desktop/setup/install/mac-install/),"
      warn "start it, and run this installer again."
      exit 1
      ;;
    *)
      warn "Docker with the Compose plugin is required (https://docs.docker.com/get-docker/)."
      exit 1
      ;;
  esac
fi
if ! docker info >/dev/null 2>&1; then
  warn "Docker is installed but this account cannot reach the Docker daemon."
  warn "Start Docker, or add yourself to the docker group (sudo usermod -aG docker \$USER, then"
  warn "log in again), and run this installer again."
  exit 1
fi
ok "Docker and the Compose plugin are available"

# ── 2 + 3. files ──────────────────────────────────────────────────────────────
mkdir -p "$OLANO_DIR"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
say "Downloading the self-host files ($OLANO_REF) into $OLANO_DIR…"
for f in "${FILES[@]}"; do
  fetch "$BASE_URL/$f" "$tmp_dir/$f"
done
# Only after every download succeeded, so a failed fetch never leaves a mix.
for f in "${FILES[@]}"; do
  mv -f "$tmp_dir/$f" "$OLANO_DIR/$f"
done
chmod +x "$OLANO_DIR/setup.sh"
ok "Files are in $OLANO_DIR"

# ── 4. setup ──────────────────────────────────────────────────────────────────
cd "$OLANO_DIR"
exec bash ./setup.sh "$@"
