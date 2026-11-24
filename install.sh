#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Lionclaw bootstrap installer
# ──────────────────────────────────────────────────────────────────────────────
# What this script does, in order. Steps marked [default] always run; steps
# marked [opt-in] only run when the matching flag is passed.
#
#   1. [default] Detects OS (macOS / Linux) and the right package manager
#   2. [default] Ensures Python ≥3.12 is available; installs 3.12 if missing
#   3. [default] Installs `uv` (the Python package/venv manager Lionclaw uses)
#   4. [default] Creates the project venv and installs Python deps via uv
#   5. [default] Installs Node.js + npm (~70 MB; required for dashboard +
#                WhatsApp bridge)
#   6. [default] Installs the default agent CLIs (same set as the exec sandbox):
#                git, ripgrep, jq, sqlite3, tmux, ffmpeg, pandoc, poppler-utils,
#                wget, gh, gog, and the npm globals yarn + @openjobs/cli, plus
#                the document toolchain the docx/pptx/xlsx/pdf skills need —
#                tesseract (OCR), qpdf, imagemagick and LibreOffice with
#                metric-compatible fonts. LibreOffice is skipped on macOS unless
#                --office is passed (there it is a GUI cask); --no-office skips
#                it everywhere.
#                (The coding-agent CLIs claude/codex are optional — they are not
#                in the exec sandbox image either; the step prints the one-liner.)
#                Disable with --minimal.
#   6b.[default] Installs `ngrok` (the secure tunnel Lionclaw uses as its public
#                gateway — exposes the local runtime/dashboard/webhooks to the
#                internet). Tiny (~10 MB). Disable with --no-ngrok.
#   7. [default] Downloads Playwright Chromium browser (~150 MB).
#                Firefox + WebKit are NEVER downloaded.  Disable with
#                --no-chromium if you don't need browser tools.
#   8. [default] Installs Docker (Linux Engine ~200 MB; macOS Desktop ~2 GB).
#                Used by Lionclaw's lionclaw_docker backend, shell sandbox,
#                MCP servers, and other containerized services.  Disable with
#                --no-docker.
#   9. [default] Installs pre-commit git hooks (auto-formats / lints on
#                `git commit`). No-op if .pre-commit-config.yaml is missing.
#  10. [default] Installs Node deps for the bundled WhatsApp bridge under
#                whatsapp-bridge/ (so `platform:whatsapp` works out of the box).
#                Skipped automatically if Node was not installed.
#  11. [opt-in]  Pulls IB Gateway (paper trading) image once Docker is ready.
#                Pass --ibkr to enable.  ~700 MB image.  Implies Docker.
#  12. [opt-in]  Installs Google Cloud SDK (~700 MB).  Pass --gcloud.
#  13. [opt-in]  Installs `davey` so Discord *voice* channels work (DAVE
#                end-to-end encryption).  Pass --discord-voice.  Text-only
#                Discord bots do NOT need this; the "davey not installed"
#                warning is harmless if you only do text.
#  14. [opt-in]  Installs a JDK (Java 25+) + the JVM `signal-cli` build + a
#                wrapper for the Signal connector.  Pass --signal.  Required to
#                SEND on Signal: Homebrew's native signal-cli can't (it fails
#                with NETWORK_FAILURE).  Point the connector's signal_cli_path
#                at <data home>/tools/signal-cli-jvm (~/.olano on new
#                installs; ~/olano or ~/.lionclaw on boxes that already
#                have one of those).
#
# Heavy / problematic things that are intentionally NEVER installed:
#   • Anaconda / Miniforge — uv handles Python versions reliably
#   • Full Xcode — only Command Line Tools is needed on macOS
#   • Firefox / WebKit Playwright browsers (~600 MB combined) — Chromium only
#   • Tesseract, ML compilers, GPU drivers
#
# Usage:
#   ./install.sh                   # full default bootstrap (recommended)
#   ./install.sh --ibkr            # also pull IB Gateway image (paper trading)
#   ./install.sh --gcloud          # also install Google Cloud SDK
#   ./install.sh --discord-voice   # also install `davey` for Discord voice E2EE
#   ./install.sh --signal          # also install JDK + JVM signal-cli (Signal sends)
#   ./install.sh --no-docker       # skip Docker install (and IB Gateway)
#   ./install.sh --no-chromium     # skip Chromium browser download
#   ./install.sh --no-whatsapp     # skip npm install for WhatsApp bridge
#   ./install.sh --no-ngrok        # skip ngrok tunnel install
#   ./install.sh --no-postgres     # skip Postgres; use the embedded SQLite backend
#   ./install.sh --sqlite          # alias for --no-postgres
#   ./install.sh --postgres        # force Postgres even on an existing SQLite home
#   ./install.sh --minimal         # only python + uv + deps (skip everything
#                                  #   else, including Postgres)
#   ./install.sh --force-sync      # force a full `uv sync` even if deps are
#                                  #   unchanged (default: skip when pyproject/
#                                  #   uv.lock/flags match the last sync)
#   ./install.sh --hf-embeddings   # also install the legacy sentence-transformers
#                                  #   + torch embedding backend (default is
#                                  #   fastembed, which is torch-free)
#
# Persistence backend: by default the installer provisions a Postgres container
# (via Docker) and makes it this home's checkpoint/store backend — the
# production-scale store. If Docker is unavailable it falls back to the embedded
# SQLite (zero-config). SQLite remains the code default for bare `pip` installs.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Pretty output ─────────────────────────────────────────────────────────────
BOLD=$(printf '\033[1m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RED=$(printf '\033[31m'); BLUE=$(printf '\033[34m'); RESET=$(printf '\033[0m')

step()  { printf "\n${BOLD}${BLUE}━━ %s${RESET}\n" "$*"; }
ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
warn()  { printf "  ${YELLOW}⚠${RESET} %s\n" "$*"; }
err()   { printf "  ${RED}✗${RESET} %s\n" "$*"; }
info()  { printf "    %s\n" "$*"; }

# Fallback ngrok install for Linux distros without a usable package repo.
# Pulls the official static binary for the host arch into /usr/local/bin.
install_ngrok_binary() {
    local ng_arch
    case "$(uname -m)" in
        x86_64|amd64)   ng_arch="amd64" ;;
        aarch64|arm64)  ng_arch="arm64" ;;
        armv7l|armv6l)  ng_arch="arm" ;;
        i386|i686)      ng_arch="386" ;;
        *) warn "Unknown arch $(uname -m) for ngrok — download from https://ngrok.com/download"; return 1 ;;
    esac
    local url="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-${ng_arch}.tgz"
    local tmp="/tmp/ngrok-${ng_arch}.tgz"
    if curl -sSLo "$tmp" "$url" && sudo tar -xzf "$tmp" -C /usr/local/bin ngrok; then
        sudo chmod +x /usr/local/bin/ngrok 2>/dev/null || true
        rm -f "$tmp"
        return 0
    fi
    warn "Direct ngrok binary download failed — download from https://ngrok.com/download"
    return 1
}

# GitHub CLI (`gh`) — needs its own package repo on apt/dnf. Best-effort: a
# failure warns and returns rather than aborting the install.
install_gh() {
    if command -v gh >/dev/null 2>&1; then
        ok "gh (GitHub CLI) already installed"; return 0
    fi
    case "$OS" in
        macos)
            brew install gh >/dev/null 2>&1 && ok "gh installed" \
                || warn "gh install via brew failed — see https://cli.github.com" ;;
        linux-apt)
            if curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                    | sudo tee /usr/share/keyrings/githubcli-archive-keyring.gpg >/dev/null \
                && sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
                && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
                && sudo apt-get update >/dev/null 2>&1 \
                && sudo apt-get install -y gh >/dev/null 2>&1; then
                ok "gh installed"
            else
                warn "gh install failed — see https://cli.github.com"
            fi ;;
        linux-dnf)
            if (sudo dnf install -y 'dnf-command(config-manager)' >/dev/null 2>&1 || true; \
                sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo >/dev/null 2>&1 \
                && sudo dnf install -y gh >/dev/null 2>&1); then
                ok "gh installed"
            else
                warn "gh install failed — see https://cli.github.com"
            fi ;;
        linux-pacman)
            sudo pacman -S --noconfirm github-cli >/dev/null 2>&1 && ok "gh installed" \
                || warn "gh install failed — see https://cli.github.com" ;;
        *)
            warn "Cannot auto-install gh on $OS — see https://cli.github.com" ;;
    esac
}

# gog (gogcli, https://github.com/steipete/gogcli) ships no prebuilt binaries, so
# build the pinned ref from source WHEN a Go toolchain is already present. We do
# NOT install Go just for this — the managed Docker images build it in a golang
# stage, and on a bare host we print the manual build command instead.
install_gog() {
    if command -v gog >/dev/null 2>&1; then
        ok "gog (gogcli) already installed"; return 0
    fi
    if ! command -v go >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1 \
        || ! command -v git >/dev/null 2>&1; then
        warn "gog (gogcli) skipped — needs Go + make + git. To add it later: git clone --branch ${GOGCLI_REF} https://github.com/steipete/gogcli && cd gogcli && make && sudo install -m755 bin/gog /usr/local/bin/gog"
        return 0
    fi
    local tmp; tmp="$(mktemp -d)"
    if git clone --depth 1 --branch "${GOGCLI_REF}" https://github.com/steipete/gogcli.git "$tmp" >/dev/null 2>&1 \
        && (cd "$tmp" && make >/dev/null 2>&1) \
        && sudo install -Dm755 "$tmp/bin/gog" /usr/local/bin/gog 2>/dev/null; then
        ok "gog installed"
    else
        warn "gog build failed — see https://github.com/steipete/gogcli"
    fi
    rm -rf "$tmp"
}

# Signal connector requirements. Homebrew's `signal-cli` bottle is a GraalVM
# native image that CANNOT send to remote recipients (it lacks reflection
# metadata for IdentityKeyDeserializer → every outbound message fails with
# NETWORK_FAILURE). The fix is the JVM build of signal-cli, which needs a JDK.
# This installs a JDK (Java 25+), the JVM signal-cli, and a wrapper that pins
# JAVA_HOME; point the Signal connector's `signal_cli_path` at the wrapper.
SIGNAL_CLI_VERSION="0.14.4.1"   # JVM build; requires Java 25+

install_signal_jvm() {
    # Mirror config/home.py's resolution: env override first (new name wins),
    # then whichever data dir already exists (~/.olano, then a visible ~/olano
    # from an install made while that was the default, then ~/.lionclaw),
    # defaulting fresh installs to ~/.olano. A ~/olano that is a source
    # checkout (this repository cloned from the home directory: it carries
    # pyproject.toml or .git) is never the data dir.
    local data_home
    local checkout=0
    if [[ -e "$HOME/olano/pyproject.toml" || -e "$HOME/olano/.git" ]]; then checkout=1; fi
    if [[ -n "${OLANO_HOME:-}" ]]; then data_home="$OLANO_HOME"
    elif [[ -n "${LIONCLAW_HOME:-}" ]]; then data_home="$LIONCLAW_HOME"
    elif [[ -d "$HOME/.olano" ]]; then data_home="$HOME/.olano"
    elif [[ -d "$HOME/olano" && "$checkout" -eq 0 ]]; then data_home="$HOME/olano"
    elif [[ -d "$HOME/.lionclaw" ]]; then data_home="$HOME/.lionclaw"
    else data_home="$HOME/.olano"; fi
    local tools_dir="$data_home/tools"
    local dest="$tools_dir/signal-cli-${SIGNAL_CLI_VERSION}"
    local wrapper="$tools_dir/signal-cli-jvm"

    # 1. JDK (Java 25+). Skip if a new-enough java is already available — check
    #    PATH java AND the keg-only Homebrew openjdk path (not on PATH on macOS).
    local java_bin=""
    command -v java >/dev/null 2>&1 && java_bin="java"
    local jc
    for jc in /opt/homebrew/opt/openjdk/bin/java /usr/local/opt/openjdk/bin/java; do
        [[ -x "$jc" ]] && java_bin="$jc" && break
    done
    if [[ -n "$java_bin" ]] \
        && "$java_bin" -version 2>&1 | grep -qE 'version "(2[5-9]|[3-9][0-9])'; then
        ok "Java already present: $("$java_bin" -version 2>&1 | head -1)"
    else
        local java_pkg=""
        case "$OS" in
            macos)        java_pkg="openjdk" ;;                    # brew latest (≥25), keg-only
            linux-apt)    java_pkg="openjdk-25-jdk" ;;
            linux-dnf)    java_pkg="java-latest-openjdk-devel" ;;
            linux-pacman) java_pkg="jdk-openjdk" ;;
        esac
        if [[ -n "$PKG_INSTALL" && -n "$java_pkg" ]]; then
            info "Installing JDK ($java_pkg) for signal-cli…"
            $PKG_INSTALL "$java_pkg" 2>/dev/null \
                || { [[ "$OS" == "linux-apt" ]] && sudo apt-get install -y default-jdk; } \
                || warn "JDK install failed — install Java 25+ manually."
        else
            warn "No package manager for a JDK — install Java 25+ manually."
        fi
    fi

    # Resolve a JAVA_HOME for the wrapper (Homebrew openjdk is keg-only).
    local java_home=""
    local cand
    for cand in /opt/homebrew/opt/openjdk /usr/local/opt/openjdk; do
        [[ -x "$cand/bin/java" ]] && java_home="$cand" && break
    done

    # 2. JVM signal-cli (the generic tarball is the JVM build; bundles libsignal).
    mkdir -p "$tools_dir"
    if [[ ! -x "$dest/bin/signal-cli" ]]; then
        local tarball="signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
        local url="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/${tarball}"
        info "Downloading JVM signal-cli ${SIGNAL_CLI_VERSION}…"
        if curl -fL --retry 2 -o "/tmp/${tarball}" "$url"; then
            tar xzf "/tmp/${tarball}" -C "$tools_dir" && rm -f "/tmp/${tarball}"
        else
            warn "Could not download signal-cli ${SIGNAL_CLI_VERSION}"
            info "Get it from https://github.com/AsamK/signal-cli/releases"
            return 1
        fi
    fi

    # 3. Wrapper: pin JAVA_HOME (else fall back to PATH java on Linux).
    cat > "$wrapper" <<WRAP
#!/bin/sh
# JVM signal-cli launcher (generated by install.sh). Fixes the Homebrew
# native-build send bug. Point the Signal connector's signal_cli_path here.
if [ -z "\$JAVA_HOME" ]; then
  for c in "${java_home}" /opt/homebrew/opt/openjdk /usr/local/opt/openjdk; do
    [ -n "\$c" ] && [ -x "\$c/bin/java" ] && JAVA_HOME="\$c" && break
  done
fi
[ -n "\$JAVA_HOME" ] && export JAVA_HOME
exec "${dest}/bin/signal-cli" "\$@"
WRAP
    chmod +x "$wrapper"

    # 4. Verify.
    if "$wrapper" --version >/dev/null 2>&1; then
        ok "JVM signal-cli ready: $("$wrapper" --version 2>&1 | head -1)"
        info "Set the Signal connector's signal_cli_path to: $wrapper"
    else
        warn "signal-cli wrapper did not run — ensure Java 25+ is installed."
        info "Current java: $(java -version 2>&1 | head -1 || echo 'not found')"
        return 1
    fi
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ── Args ──────────────────────────────────────────────────────────────────────
# Defaults: Node + system CLIs + Chromium + Docker + git hooks all ON.
#           IB Gateway image and gcloud are OFF (opt-in).
WANT_NODE=1
WANT_CHROMIUM=1
WANT_DOCKER=1
WANT_PRECOMMIT=1
WANT_SYSTEM_CLIS=1
WANT_NGROK=1
# gogcli ref built from source by install_gog (and the Docker images' gog-builder).
GOGCLI_REF="${GOGCLI_REF:-v0.12.0}"
WANT_WHATSAPP_BRIDGE=1
WANT_DISCORD_VOICE=0
WANT_IBKR=0
WANT_GCLOUD=0
WANT_SIGNAL=0
# Postgres checkpoint backend: default-on (provisioned via Docker) — the
# production-scale store. Falls back to SQLite automatically if Docker is
# unavailable. Skip with --no-postgres / --sqlite. FORCE means an explicit
# --postgres was passed (switch an existing SQLite home, not just fresh ones).
WANT_POSTGRES=1
FORCE_POSTGRES=0
MINIMAL=0
# Dependency sync is skipped when pyproject.toml/uv.lock (and the sync flags)
# are unchanged since the last successful sync — the common re-run/redeploy is
# then a no-op instead of a multi-minute `uv sync` audit. --force-sync (or
# LIONCLAW_FORCE_SYNC=1) forces a full reinstall.
FORCE_SYNC="${LIONCLAW_FORCE_SYNC:-0}"
# Legacy sentence-transformers/torch embedding backend. Default OFF — the default
# embedding backend is fastembed (ONNX, torch-free). Opt in with --hf-embeddings
# to also install the `hf-embeddings` dependency-group (adds ~180 MB CPU torch).
WANT_HF_EMBEDDINGS=0
# LibreOffice, the one thing that converts between office formats and
# recalculates spreadsheet formulas. Default ON with a Linux package manager,
# where it is a headless ~500 MB install the document skills need. On macOS the
# only formula is a GUI cask (~700 MB), so it waits for an explicit --office;
# --no-office skips it on every OS. Everything else in the document toolchain
# (tesseract, imagemagick, qpdf, the fonts) is small and installs either way.
WANT_OFFICE=1
OFFICE_EXPLICIT=0
for arg in "$@"; do
    case "$arg" in
        --force-sync|--force) FORCE_SYNC=1 ;;
        --hf-embeddings)  WANT_HF_EMBEDDINGS=1 ;;
        --office)         WANT_OFFICE=1; OFFICE_EXPLICIT=1 ;;
        --no-office)      WANT_OFFICE=0 ;;
        --ibkr)           WANT_IBKR=1 ;;
        --gcloud)         WANT_GCLOUD=1 ;;
        --discord-voice)  WANT_DISCORD_VOICE=1 ;;
        --signal)         WANT_SIGNAL=1 ;;
        --no-docker)      WANT_DOCKER=0 ;;
        --no-chromium)    WANT_CHROMIUM=0 ;;
        --no-whatsapp)    WANT_WHATSAPP_BRIDGE=0 ;;
        --no-ngrok)       WANT_NGROK=0 ;;
        --postgres)       WANT_POSTGRES=1; FORCE_POSTGRES=1 ;;
        --no-postgres|--sqlite) WANT_POSTGRES=0 ;;
        --minimal)
            MINIMAL=1
            WANT_NODE=0
            WANT_CHROMIUM=0
            WANT_DOCKER=0
            WANT_PRECOMMIT=0
            WANT_SYSTEM_CLIS=0
            WANT_NGROK=0
            WANT_WHATSAPP_BRIDGE=0
            WANT_DISCORD_VOICE=0
            WANT_IBKR=0
            WANT_GCLOUD=0
            WANT_SIGNAL=0
            WANT_POSTGRES=0
            WANT_OFFICE=0
            ;;
        -h|--help)
            # Print only the top-of-file header block (between the first and
            # third "# ──…" decorator lines).
            awk '/^# ─/{c++; if (c==3) exit; next}
                 c>=1 { sub(/^# ?/, ""); print }' "$0"
            exit 0
            ;;
        *) warn "Unknown arg: $arg (ignored)" ;;
    esac
done

# --ibkr implies --docker (we need Docker to pull/run the gateway image)
if [[ "$WANT_IBKR" -eq 1 ]]; then
    WANT_DOCKER=1
fi

# Postgres is provisioned as a Docker container, so it needs Docker. If Docker
# was disabled (--no-docker / --minimal), silently fall back to the SQLite
# default unless the user explicitly forced it (then warn).
if [[ "$WANT_POSTGRES" -eq 1 && "$WANT_DOCKER" -eq 0 ]]; then
    if [[ "$FORCE_POSTGRES" -eq 1 ]]; then
        warn "--postgres needs Docker, but Docker is disabled — keeping SQLite. Re-run without --no-docker."
    fi
    WANT_POSTGRES=0
fi

# ── 1. OS detection ───────────────────────────────────────────────────────────
step "Detecting OS"
OS=""
case "$(uname -s)" in
    Darwin)  OS="macos" ;;
    Linux)
        if command -v apt-get >/dev/null 2>&1; then OS="linux-apt"
        elif command -v dnf >/dev/null 2>&1;     then OS="linux-dnf"
        elif command -v pacman >/dev/null 2>&1;  then OS="linux-pacman"
        else OS="linux-generic"; fi
        ;;
    *) err "Unsupported OS: $(uname -s)"; exit 1 ;;
esac
ok "Detected $OS"

PKG_INSTALL=""
PKG_UPDATE=""
case "$OS" in
    macos)
        if ! command -v brew >/dev/null 2>&1; then
            warn "Homebrew not found — installing"
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        PKG_INSTALL="brew install"
        PKG_UPDATE="brew update"
        ;;
    linux-apt)
        PKG_INSTALL="sudo apt-get install -y"
        PKG_UPDATE="sudo apt-get update"
        $PKG_UPDATE
        ;;
    linux-dnf)
        PKG_INSTALL="sudo dnf install -y"
        PKG_UPDATE="sudo dnf check-update || true"
        ;;
    linux-pacman)
        PKG_INSTALL="sudo pacman -S --noconfirm"
        PKG_UPDATE="sudo pacman -Sy"
        $PKG_UPDATE
        ;;
    *)
        warn "Unrecognized Linux distro — skipping system-package install steps"
        PKG_INSTALL=""
        ;;
esac

# ── 2. Python ≥3.12 ───────────────────────────────────────────────────────────
step "Ensuring Python ≥3.12"

# Find best available Python interpreter (3.12 preferred, then 3.13, then 3.11+)
PYTHON_BIN=""
for candidate in python3.12 python3.13 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 11 ]]; then
            PYTHON_BIN="$candidate"
            ok "Found $candidate (version $ver)"
            break
        fi
    fi
done

# Install Python 3.12 if nothing acceptable
if [[ -z "$PYTHON_BIN" ]]; then
    warn "No Python ≥3.11 found — installing Python 3.12"
    case "$OS" in
        macos)        brew install python@3.12 ;;
        linux-apt)
            sudo apt-get install -y software-properties-common
            sudo add-apt-repository -y ppa:deadsnakes/ppa || true
            sudo apt-get update
            sudo apt-get install -y python3.12 python3.12-venv python3.12-dev
            ;;
        linux-dnf)    sudo dnf install -y python3.12 || sudo dnf install -y python3 ;;
        linux-pacman) sudo pacman -S --noconfirm python ;;
        *)            err "Cannot auto-install Python on $OS — please install Python 3.12 manually"; exit 1 ;;
    esac
    PYTHON_BIN="python3.12"
    ok "Installed $PYTHON_BIN"
fi

# Verify version is acceptable
ACTUAL_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ACTUAL_MINOR=$(echo "$ACTUAL_VER" | cut -d. -f2)
if [[ "$ACTUAL_MINOR" -lt 11 ]]; then
    err "Python $ACTUAL_VER is too old (need ≥3.11). Please install Python 3.12."
    exit 1
fi
info "Using Python $ACTUAL_VER from $(command -v "$PYTHON_BIN")"

# ── 3. uv ─────────────────────────────────────────────────────────────────────
step "Installing uv (Python package manager)"
if command -v uv >/dev/null 2>&1; then
    ok "uv already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Ensure it's on PATH for this session
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    ok "uv installed: $(uv --version)"
fi

# ── 4. Python deps via uv ─────────────────────────────────────────────────────
# Stamp file (lives WITH the venv it describes, so a fresh venv → no stamp →
# sync). Records a hash of the dependency manifests + the exact sync flags of
# the last successful sync.
DEPS_STAMP="$REPO_ROOT/.venv/.lionclaw-deps-stamp"

_sha256() {
    # Portable SHA-256 of stdin (Linux `sha256sum`, macOS `shasum -a 256`).
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1; fi
}

_deps_signature() {
    # Change to pyproject.toml, uv.lock, OR the sync flags invalidates the stamp.
    { cat pyproject.toml uv.lock 2>/dev/null; printf '\037%s' "$*"; } | _sha256
}

deps_sync() {
    # Run `uv sync <args...>` only when dependencies/flags changed since the last
    # successful sync — else skip (the whole point: no reinstall when nothing
    # changed). --force-sync / LIONCLAW_FORCE_SYNC=1 forces it.
    local sig; sig="$(_deps_signature "$@")"
    if [[ "$FORCE_SYNC" -eq 1 ]]; then
        info "Forcing full dependency sync (--force-sync)"
    elif [[ -d .venv && -f "$DEPS_STAMP" && "$(cat "$DEPS_STAMP" 2>/dev/null)" == "$sig" ]]; then
        ok "Dependencies unchanged since last sync — skipping (--force-sync to override)"
        return 0
    fi
    if uv sync "$@"; then
        printf '%s' "$sig" > "$DEPS_STAMP" 2>/dev/null || true
        return 0
    fi
    return 1
}

step "Installing Python dependencies (this can take a few minutes)"
# Opt-in legacy embedding backend: --hf-embeddings adds the `hf-embeddings`
# dependency-group (sentence-transformers + CPU torch). The default (fastembed)
# is torch-free, so the group is NOT pulled by --all-extras.
HF_GROUP=()
if [[ "$WANT_HF_EMBEDDINGS" -eq 1 ]]; then
    info "Including legacy sentence-transformers/torch embedding backend (--hf-embeddings)"
    HF_GROUP=(--group hf-embeddings)
fi
if [[ "$MINIMAL" -eq 1 ]]; then
    info "Minimal mode: dev tools skipped"
    deps_sync --python "$PYTHON_BIN" ${HF_GROUP[@]+"${HF_GROUP[@]}"}
else
    deps_sync --python "$PYTHON_BIN" --all-extras --dev ${HF_GROUP[@]+"${HF_GROUP[@]}"} \
        || deps_sync --python "$PYTHON_BIN" ${HF_GROUP[@]+"${HF_GROUP[@]}"}
fi
ok "Python deps installed"

# ── 5. Node.js + npm (always installed; ~70 MB) ───────────────────────────────
# npx-launched MCP servers (e.g. mcp-remote) need the Node 20+ `File` global;
# on Node 18 they crash at startup and the agent only sees "Connection closed".
NODE_MIN_MAJOR=20
node_major() { node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1; }
if [[ "$WANT_NODE" -eq 1 ]]; then
    step "Installing Node.js + npm"
    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
        && [[ "$(node_major)" -ge "$NODE_MIN_MAJOR" ]]; then
        ok "Node $(node --version), npm $(npm --version) already installed"
    else
        if command -v node >/dev/null 2>&1; then
            warn "Node $(node --version) is older than v${NODE_MIN_MAJOR} — upgrading"
        fi
        case "$OS" in
            macos)        brew install node ;;
            linux-apt)    # distro nodejs is often too old (Ubuntu 24.04 ships v18) — use NodeSource LTS
                          curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
                          sudo apt-get install -y nodejs ;;
            linux-dnf)    sudo dnf install -y nodejs npm ;;
            linux-pacman) sudo pacman -S --noconfirm nodejs npm ;;
            *)            warn "Cannot auto-install Node.js on $OS — install manually" ;;
        esac
        if command -v node >/dev/null 2>&1; then
            if [[ "$(node_major)" -ge "$NODE_MIN_MAJOR" ]]; then
                ok "Node $(node --version), npm $(npm --version)"
            else
                warn "Node $(node --version) is still older than v${NODE_MIN_MAJOR} — npx-launched MCP servers (mcp-remote) will fail to start"
            fi
        fi
    fi
fi

# ── 6. Default agent CLIs ─────────────────────────────────────────────────────
# The command-line tools agents shell out to (execute / safe_shell) and that
# in-process tools invoke on the host when no Docker sandbox is live. Kept in
# lockstep with the exec-sandbox image (docker/olano-sandbox/Dockerfile) so a
# host install and a managed deployment expose the same toolkit. Every install is
# best-effort — a single failure warns and continues. Skipped by --minimal.
if [[ "$WANT_SYSTEM_CLIS" -eq 1 ]]; then
    step "Installing default agent CLIs (git, ripgrep, jq, sqlite3, tmux, ffmpeg, pandoc, poppler, wget, zstd, tesseract, qpdf, imagemagick, LibreOffice, gh, gog, openjobs)"

    # Tools whose package name is the same across managers. Format "binary:package"
    # (binary differs from package for ripgrep → rg). poppler is handled separately.
    # tmux: a detachable session survives both the 120s execute/safe_shell timeout
    # and a dashboard terminal reconnect, so a long build keeps running.
    # zstd: `tar --zstd -xf x.tar.zst` execs it, and modern release tarballs ship
    # that way. Same package name on apt, dnf, pacman and brew.
    for pair in "git:git" "jq:jq" "ffmpeg:ffmpeg" "wget:wget" "rg:ripgrep" "pandoc:pandoc" "tmux:tmux" "zstd:zstd"; do
        bin="${pair%%:*}"; pkg="${pair##*:}"
        if command -v "$bin" >/dev/null 2>&1; then
            ok "$pkg already installed"
        elif [[ -n "$PKG_INSTALL" ]]; then
            # shellcheck disable=SC2086
            if $PKG_INSTALL "$pkg" >/dev/null 2>&1; then ok "$pkg installed"; else warn "Failed to install $pkg — install manually"; fi
        else
            warn "Cannot install $pkg on $OS — please install manually"
        fi
    done

    # `python` → python3. Agents reach for the bare `python` by reflex, and on a
    # python3-only host every first attempt fails before the retry — the sandbox
    # image fixes that with `python-is-python3`, so a host install matches it.
    # Debian/Ubuntu are the only ones that need the shim: macOS/brew, dnf and
    # pacman either already resolve `python` or manage the name themselves.
    # Never hand-symlink into a system dir — that is the package manager's job.
    # Either way the runtime also writes its own `python` → python3 shim into
    # <home>/sandbox/tools/shims and appends it to every agent shell's PATH
    # (olano/backends/interpreter_shims.py), so agent commands resolve `python`
    # even where this step could not — this only decides the operator's shell.
    if command -v python >/dev/null 2>&1; then
        ok "python already resolves"
    elif [[ "$OS" == "linux-apt" && -n "$PKG_INSTALL" ]]; then
        # shellcheck disable=SC2086
        if $PKG_INSTALL python-is-python3 >/dev/null 2>&1; then
            ok "python → python3 (python-is-python3)"
        else
            warn "Failed to install python-is-python3 — agent shells get the runtime's python shim; your own shell needs python3"
        fi
    else
        info "'python' does not resolve on this host — agent shells get the runtime's python shim; your own shell needs python3"
    fi

    # poppler-utils (pdftotext / pdftoppm) — brew and pacman call the package "poppler".
    if command -v pdftotext >/dev/null 2>&1; then
        ok "poppler already installed"
    elif [[ -n "$PKG_INSTALL" ]]; then
        case "$OS" in
            macos|linux-pacman) poppler_pkg="poppler" ;;
            *)                  poppler_pkg="poppler-utils" ;;
        esac
        # shellcheck disable=SC2086
        if $PKG_INSTALL "$poppler_pkg" >/dev/null 2>&1; then ok "poppler installed"; else warn "Failed to install $poppler_pkg — install manually"; fi
    fi

    # sqlite3 CLI — agents query local .db files with it (including olano's own
    # sessions/threads DB). Debian/Ubuntu ship the shell as "sqlite3"; brew, dnf and
    # pacman call the package "sqlite". macOS already has /usr/bin/sqlite3, so the
    # command check below short-circuits there.
    if command -v sqlite3 >/dev/null 2>&1; then
        ok "sqlite3 already installed"
    elif [[ -n "$PKG_INSTALL" ]]; then
        case "$OS" in
            linux-apt) sqlite_pkg="sqlite3" ;;
            *)         sqlite_pkg="sqlite" ;;
        esac
        # shellcheck disable=SC2086
        if $PKG_INSTALL "$sqlite_pkg" >/dev/null 2>&1; then ok "sqlite3 installed"; else warn "Failed to install $sqlite_pkg — install manually"; fi
    else
        warn "Cannot install sqlite3 on $OS — please install manually"
    fi

    # ── The document toolchain ────────────────────────────────────────────────
    # What the docx/pptx/xlsx/pdf skills reach for. Same lockstep rule as the
    # tools above: these are baked into the exec-sandbox image, so a host
    # install has to offer them too or the same skill behaves differently
    # depending on how olano was installed.
    #
    # tesseract  OCR — a scanned PDF has no text layer, so pdftotext returns
    #            nothing and the only alternative is a vision call per page.
    # imagemagick  crop/resize/composite/convert from the shell.
    # qpdf       merge, split, rotate, encrypt, linearise, and repair a damaged
    #            PDF. Not pdftk: a JRE for a subset of this.
    for pair in "tesseract:tesseract" "qpdf:qpdf"; do
        bin="${pair%%:*}"; pkg="${pair##*:}"
        if command -v "$bin" >/dev/null 2>&1; then
            ok "$pkg already installed"
        elif [[ -n "$PKG_INSTALL" ]]; then
            # Debian names the OCR engine and its data separately; everyone else
            # ships English with the engine or as a same-named langpack.
            pkgs="$pkg"
            if [[ "$bin" == "tesseract" ]]; then
                case "$OS" in
                    linux-apt)     pkgs="tesseract-ocr tesseract-ocr-eng" ;;
                    linux-dnf)     pkgs="tesseract tesseract-langpack-eng" ;;
                    linux-pacman)  pkgs="tesseract tesseract-data-eng" ;;
                esac
            fi
            # shellcheck disable=SC2086
            if $PKG_INSTALL $pkgs >/dev/null 2>&1; then ok "$pkg installed"; else warn "Failed to install $pkgs — install manually"; fi
        else
            warn "Cannot install $pkg on $OS — please install manually"
        fi
    done

    # ImageMagick: the binary is `magick` on v7 and `convert` on v6, and Fedora
    # capitalizes the package.
    if command -v magick >/dev/null 2>&1 || command -v convert >/dev/null 2>&1; then
        ok "imagemagick already installed"
    elif [[ -n "$PKG_INSTALL" ]]; then
        case "$OS" in
            linux-dnf) magick_pkg="ImageMagick" ;;
            *)         magick_pkg="imagemagick" ;;
        esac
        # shellcheck disable=SC2086
        if $PKG_INSTALL "$magick_pkg" >/dev/null 2>&1; then ok "imagemagick installed"; else warn "Failed to install $magick_pkg — install manually"; fi
    fi

    # LibreOffice — see WANT_OFFICE above for why macOS waits to be asked.
    if [[ "$WANT_OFFICE" -eq 1 && "$OS" == "macos" && "$OFFICE_EXPLICIT" -eq 0 ]]; then
        info "Skipping LibreOffice on macOS (a ~700 MB GUI cask) — re-run with --office to install it; document conversion is unavailable without it"
    elif [[ "$WANT_OFFICE" -eq 1 ]]; then
        if command -v soffice >/dev/null 2>&1; then
            ok "LibreOffice already installed"
        elif [[ -n "$PKG_INSTALL" ]]; then
            case "$OS" in
                macos)         office_pkg="--cask libreoffice" ;;
                linux-pacman)  office_pkg="libreoffice-still" ;;
                *)             office_pkg="libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress" ;;
            esac
            step "Installing LibreOffice (document conversion; several hundred MB — skip with --no-office)"
            # shellcheck disable=SC2086
            if $PKG_INSTALL $office_pkg >/dev/null 2>&1; then ok "LibreOffice installed"; else warn "Failed to install LibreOffice — document conversion stays unavailable"; fi
        else
            warn "Cannot install LibreOffice on $OS — please install manually"
        fi
        # Fonts LibreOffice substitutes BY NAME: without metric-compatible faces
        # a .docx set in Arial or Times reflows and the exported PDF is wrong.
        # macOS ships its own metric-compatible set.
        if [[ "$OS" != "macos" && -n "$PKG_INSTALL" ]]; then
            case "$OS" in
                linux-dnf)     font_pkgs="liberation-fonts google-noto-fonts" ;;
                linux-pacman)  font_pkgs="ttf-liberation noto-fonts" ;;
                *)             font_pkgs="fonts-liberation2 fonts-noto-core" ;;
            esac
            # shellcheck disable=SC2086
            if $PKG_INSTALL $font_pkgs >/dev/null 2>&1; then ok "document fonts installed"; else warn "Failed to install $font_pkgs — converted documents may reflow"; fi
        fi
    else
        info "Skipping LibreOffice (--no-office) — document conversion and formula recalculation are unavailable"
    fi

    # GitHub CLI + gogcli (own repos / source build; best-effort helpers above).
    install_gh
    install_gog

    # Global npm CLIs: yarn + @openjobs/cli (the `openjobs` binary). Needs Node/npm
    # from step 5; if npm is missing we skip rather than fail the whole install.
    #
    # The coding-agent CLIs (@anthropic-ai/claude-code, @openai/codex) are NOT
    # installed here. They were, to keep this host toolkit in lockstep with the
    # exec sandbox — but they are no longer in the sandbox image either (616 MB
    # of platform binaries nothing in Olano invokes), so installing them here
    # would reintroduce exactly the divergence the parity rule exists to
    # prevent. Anyone who wants them runs the one-liner printed below.
    if command -v npm >/dev/null 2>&1; then
        for npm_pkg in yarn @openjobs/cli; do
            if npm install -g "$npm_pkg" --no-fund --no-audit >/dev/null 2>&1; then
                ok "npm -g $npm_pkg installed"
            else
                warn "Failed to install npm package $npm_pkg (retry: npm install -g $npm_pkg)"
            fi
        done
        info "Coding-agent CLIs are optional: npm install -g @anthropic-ai/claude-code @openai/codex"
    else
        warn "npm not found — skipping yarn + @openjobs/cli (install Node.js, then re-run)"
    fi
fi

# ── 6b. ngrok (default-on; Lionclaw's public gateway / secure tunnel) ─────────
# ngrok exposes the local Lionclaw runtime (dashboard, webhooks, channel
# callbacks) to the internet over a secure tunnel. It's the gateway, so it's
# installed by default. Tiny (~10 MB). Disable with --no-ngrok.
if [[ "$WANT_NGROK" -eq 1 ]]; then
    step "Installing ngrok (public gateway / secure tunnel)"
    if command -v ngrok >/dev/null 2>&1; then
        ok "ngrok already installed: $(ngrok --version 2>/dev/null | head -n1)"
    else
        case "$OS" in
            macos)
                brew install ngrok || warn "ngrok install via brew failed — see https://ngrok.com/download"
                ;;
            linux-apt)
                # Official ngrok apt repo
                if curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
                        | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
                    && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
                        | sudo tee /etc/apt/sources.list.d/ngrok.list >/dev/null \
                    && sudo apt-get update \
                    && sudo apt-get install -y ngrok; then
                    : # installed via repo
                else
                    warn "ngrok apt repo failed — falling back to direct binary"
                    install_ngrok_binary
                fi
                ;;
            linux-dnf)
                if sudo dnf install -y https://ngrok-agent.s3.amazonaws.com/ngrok.rpm; then
                    : # installed via rpm
                else
                    warn "ngrok rpm failed — falling back to direct binary"
                    install_ngrok_binary
                fi
                ;;
            linux-pacman|linux-generic)
                install_ngrok_binary
                ;;
            *)
                warn "Cannot auto-install ngrok on $OS — download from https://ngrok.com/download"
                ;;
        esac
        if command -v ngrok >/dev/null 2>&1; then
            ok "ngrok installed: $(ngrok --version 2>/dev/null | head -n1)"
            info "Add your authtoken once: ngrok config add-authtoken <token>  (https://dashboard.ngrok.com)"
        fi
    fi
fi

# ── 7. Docker (default-on; used by lionclaw_docker backend + sandbox) ─────────
if [[ "$WANT_DOCKER" -eq 1 ]]; then
    step "Installing Docker (used by lionclaw_docker backend, sandbox, MCP servers)"
    if command -v docker >/dev/null 2>&1; then
        ok "Docker already installed: $(docker --version)"
    else
        case "$OS" in
            macos)
                info "Installing Docker Desktop via Homebrew Cask (~1.5 GB on disk)…"
                if brew install --cask docker; then
                    ok "Docker Desktop installed"
                    warn "Open Docker Desktop once to accept terms and start the engine."
                else
                    warn "Docker install failed — install manually from https://docker.com/"
                fi
                ;;
            linux-apt)
                if sudo apt-get install -y docker.io; then
                    sudo systemctl enable --now docker 2>/dev/null || true
                    sudo usermod -aG docker "$USER" 2>/dev/null || true
                    ok "Docker engine installed"
                    info "Log out / back in (or run \`newgrp docker\`) so your user is in the docker group."
                else
                    warn "Docker install failed"
                fi
                ;;
            linux-dnf)
                sudo dnf install -y docker \
                    && sudo systemctl enable --now docker 2>/dev/null || true
                sudo usermod -aG docker "$USER" 2>/dev/null || true
                ok "Docker engine installed"
                ;;
            linux-pacman)
                sudo pacman -S --noconfirm docker \
                    && sudo systemctl enable --now docker 2>/dev/null || true
                sudo usermod -aG docker "$USER" 2>/dev/null || true
                ok "Docker engine installed"
                ;;
            *)
                warn "Cannot auto-install Docker on $OS — install from https://docker.com/"
                ;;
        esac
    fi
fi

# ── 8. IB Gateway image (opt-in via --ibkr; requires Docker) ──────────────────
if [[ "$WANT_IBKR" -eq 1 ]]; then
    step "Pulling IB Gateway image (--ibkr)"
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        info "Pulling ghcr.io/gnzsnz/ib-gateway:stable (~700 MB)…"
        if docker pull ghcr.io/gnzsnz/ib-gateway:stable; then
            ok "IB Gateway image ready"
            cat <<'EOF'

  Start IB Gateway (paper) once you have an IBKR account:
    docker run -d --name ibgw \
      -e TWS_USERID=$IBKR_USERNAME \
      -e TWS_PASSWORD=$IBKR_PASSWORD \
      -e TRADING_MODE=paper \
      -p 4002:4002 \
      ghcr.io/gnzsnz/ib-gateway:stable

  Then point olano IBKR tools at it:
    export IBKR_HOST=127.0.0.1
    export IBKR_PAPER_PORT=4002    # IB Gateway paper port
    export IBKR_LIVE_PORT=4001     # IB Gateway live port

  Lionclaw IBKR tools always default to mode='paper'.

EOF
        else
            warn "Failed to pull IB Gateway image. After starting Docker, run:"
            info "  docker pull ghcr.io/gnzsnz/ib-gateway:stable"
        fi
    else
        warn "Docker daemon not reachable. Start Docker and re-run: ./install.sh --ibkr"
    fi
fi

# ── 8b. Postgres checkpoint backend (default-on; --no-postgres / --sqlite to skip) ──
# Brings up the bundled Postgres container and makes it the default backend for
# this home. We write the choice into the home config.yaml (NOT .env) on purpose:
# env vars override config.yaml, so a Docker deploy's compose env (postgres:5432)
# still wins over this host-native localhost setting — no cross-context clash.
if [[ "$WANT_POSTGRES" -eq 1 ]]; then
    step "Setting up Postgres checkpoint backend (default; --no-postgres to skip)"
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        # The sidecar's password, and its role/database/volume name.
        # docker-compose.yml interpolates ${LIONCLAW_PG_PASSWORD:-lionclaw} into
        # POSTGRES_PASSWORD and ${OLANO_PG_NAME:-lionclaw} into POSTGRES_USER,
        # POSTGRES_DB and the volume key, all of which Postgres reads only when
        # it initialises the data dir — so both are decided and exported BEFORE
        # the compose call. Sticky in the home's .env (the dotenv the runtime
        # loads at start — config/home.py env_path) so re-runs reuse what the
        # volume was initialised with; an explicit value in the environment
        # wins (the dev default 'lionclaw' still works when set). The name is
        # olano on a fresh machine and lionclaw wherever a
        # <project>_lionclaw_pgdata volume already exists; the password is
        # generated only when nothing else provides one.
        HOME_ENV_FILE="$(uv run python -c 'from olano.config.home import get_olano_paths; print(get_olano_paths().env_path)' 2>/dev/null || true)"
        if [[ -z "${LIONCLAW_PG_PASSWORD:-}" && -n "$HOME_ENV_FILE" && -f "$HOME_ENV_FILE" ]]; then
            LIONCLAW_PG_PASSWORD="$(sed -n 's/^[[:space:]]*LIONCLAW_PG_PASSWORD=//p' "$HOME_ENV_FILE" | head -n1)"
        fi
        if [[ -z "${LIONCLAW_PG_PASSWORD:-}" ]]; then
            LIONCLAW_PG_PASSWORD="$(openssl rand -hex 24)"
        fi
        export LIONCLAW_PG_PASSWORD
        if [[ -z "${OLANO_PG_NAME:-}" && -n "$HOME_ENV_FILE" && -f "$HOME_ENV_FILE" ]]; then
            OLANO_PG_NAME="$(sed -n 's/^[[:space:]]*OLANO_PG_NAME=//p' "$HOME_ENV_FILE" | head -n1)"
        fi
        if [[ -z "${OLANO_PG_NAME:-}" ]]; then
            if docker volume ls -q 2>/dev/null | grep -q '_lionclaw_pgdata$'; then
                OLANO_PG_NAME=lionclaw
            else
                OLANO_PG_NAME=olano
            fi
        fi
        export OLANO_PG_NAME
        info "Starting bundled Postgres (docker compose --profile postgres up -d; role and database '${OLANO_PG_NAME}')…"
        if docker compose --profile postgres up -d postgres; then
            # Wait (best-effort) for the container to accept connections.
            for _ in $(seq 1 30); do
                if docker exec olano_postgres pg_isready -U "$OLANO_PG_NAME" -d "$OLANO_PG_NAME" >/dev/null 2>&1; then
                    break
                fi
                sleep 1
            done
            # Point the home at it — but don't clobber an explicit existing choice
            # unless --postgres was passed (FORCE). The password goes into the URL
            # URL-encoded and is persisted in the home's .env, with the name, only
            # when the URL is actually written, so the three can never disagree.
            if PG_CFG_RESULT="$(FORCE_POSTGRES="$FORCE_POSTGRES" uv run python - <<'PY'
import os, urllib.parse, yaml
from olano.config.home import get_olano_paths, update_home_config
force = os.environ.get("FORCE_POSTGRES") == "1"
password = os.environ["LIONCLAW_PG_PASSWORD"]
name = os.environ["OLANO_PG_NAME"]
paths = get_olano_paths()
cfg_path = paths.config_path
raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
if force or "checkpoint_backend" not in (raw or {}):
    quoted = urllib.parse.quote(password, safe="")
    update_home_config({
        "checkpoint_backend": "postgres",
        "checkpoint_db_url": f"postgresql://{name}:{quoted}@localhost:5432/{name}",
        "store_backend": "postgres",
    })
    env_path = paths.env_path
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [
        ln for ln in lines
        if not ln.strip().startswith(("LIONCLAW_PG_PASSWORD=", "OLANO_PG_NAME="))
    ]
    lines.append(f"LIONCLAW_PG_PASSWORD={password}")
    lines.append(f"OLANO_PG_NAME={name}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.touch(mode=0o600, exist_ok=True)  # mode applies on creation only
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("SET")
else:
    print("KEPT:" + str((raw or {}).get("checkpoint_backend")))
PY
)"; then
                if [[ "$PG_CFG_RESULT" == "SET" ]]; then
                    # POSTGRES_PASSWORD applies only when the data dir is
                    # initialised, so a volume from an older install (literal
                    # 'lionclaw') would refuse the URL just written. Align the live
                    # role with it over the container's trusted local socket — a
                    # no-op on a freshly initialised volume.
                    if ! docker exec olano_postgres psql -U "$OLANO_PG_NAME" -d "$OLANO_PG_NAME" -q -v ON_ERROR_STOP=1 \
                        -v pw="$LIONCLAW_PG_PASSWORD" -c "ALTER USER ${OLANO_PG_NAME} PASSWORD :'pw'" >/dev/null; then
                        warn "Could not set the Postgres role password to the one in $HOME_ENV_FILE — if the runtime cannot connect, run:"
                        warn "  docker exec olano_postgres psql -U ${OLANO_PG_NAME} -d ${OLANO_PG_NAME} -c \"ALTER USER ${OLANO_PG_NAME} PASSWORD '<LIONCLAW_PG_PASSWORD>'\""
                    fi
                fi
                ok "Postgres ready on localhost:5432 and set as the default backend"
            else
                warn "Postgres container is up but configuring the home failed."
                warn "Set it manually: LIONCLAW_CHECKPOINT_BACKEND=postgres + LIONCLAW_CHECKPOINT_DB_URL=postgresql://${OLANO_PG_NAME}:<LIONCLAW_PG_PASSWORD, URL-encoded>@localhost:5432/${OLANO_PG_NAME}"
            fi
            # Best-effort: if this home now targets Postgres and a legacy SQLite
            # threads.db exists, carry the thread history over so existing
            # conversations stay resumable. Never fatal — the legacy threads.db
            # remains readable via LIONCLAW_CHECKPOINT_BACKEND=sqlite.
            MIGRATE_SRC="$(uv run python -c 'import pathlib
from olano.config.home import get_olano_paths, resolve_checkpoint_backend, load_home_config
b, _ = resolve_checkpoint_backend(load_home_config())
p = get_olano_paths().thread_db_path
print(p if (b == "postgres" and pathlib.Path(p).exists()) else "")' 2>/dev/null || true)"
            if [[ -n "$MIGRATE_SRC" ]]; then
                info "Migrating existing SQLite thread history → Postgres…"
                if uv run olano migrate-checkpoints; then
                    ok "Thread history migrated to Postgres"
                else
                    warn "Checkpoint migration reported issues — legacy history stays readable via LIONCLAW_CHECKPOINT_BACKEND=sqlite. Re-run: olano migrate-checkpoints"
                fi
            fi
        else
            warn "Could not start the Postgres container — keeping the SQLite default."
            warn "Start it later with: make postgres-up"
        fi
    else
        warn "Docker not reachable — keeping the SQLite default (zero-config)."
        warn "To switch later: start Docker, then 'make postgres-up' and set LIONCLAW_CHECKPOINT_BACKEND=postgres."
    fi
fi

# ── 9. Google Cloud SDK (opt-in via --gcloud, ~700 MB) ────────────────────────
if [[ "$WANT_GCLOUD" -eq 1 ]]; then
    step "Installing Google Cloud SDK"
    if command -v gcloud >/dev/null 2>&1; then
        ok "gcloud already installed: $(gcloud --version | head -n1)"
    else
        case "$OS" in
            macos)
                brew install --cask google-cloud-sdk
                ;;
            linux-apt)
                curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
                    | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
                echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
                    | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
                sudo apt-get update && sudo apt-get install -y google-cloud-cli
                ;;
            *) warn "gcloud auto-install not implemented for $OS — install manually" ;;
        esac
        info "Run 'gcloud auth application-default login' to authorize"
    fi
fi

# ── 10. Playwright Chromium browser binary (~150 MB) ──────────────────────────
# The Playwright *Python package* is already in pyproject.toml and was installed
# in step 4. This step only downloads the actual browser binary. We always
# install Chromium only — Firefox / WebKit binaries are skipped (~600 MB saved).
if [[ "$WANT_CHROMIUM" -eq 1 ]]; then
    step "Downloading Chromium browser for Playwright (~150 MB; Chromium only)"
    if uv run python -c "import playwright" >/dev/null 2>&1; then
        uv run playwright install chromium --with-deps 2>/dev/null \
            || uv run playwright install chromium \
            || warn "Chromium install failed"
        ok "Chromium ready"
    else
        warn "playwright Python package not found in venv — skipping browser install"
    fi
fi

# ── 10b. WhatsApp bridge (default-on; tiny ~30 MB of npm deps) ────────────────
# The `platform:whatsapp` adapter relies on a small Node bridge under
# whatsapp-bridge/.  Pre-install its node_modules here so the first agent
# session doesn't pay the npm-install latency, and so the warning
# "Bridge script not found" goes away.
if [[ "$WANT_WHATSAPP_BRIDGE" -eq 1 ]] && command -v npm >/dev/null 2>&1; then
    if [[ -f whatsapp-bridge/package.json ]]; then
        step "Installing WhatsApp bridge npm deps (whatsapp-bridge/)"
        if [[ -d whatsapp-bridge/node_modules ]]; then
            ok "whatsapp-bridge/node_modules already present"
        else
            (cd whatsapp-bridge && npm install --no-audit --no-fund --silent) \
                && ok "WhatsApp bridge ready (run platform:whatsapp from the dashboard)" \
                || warn "npm install in whatsapp-bridge/ failed — re-run later or set Bridge script path manually"
        fi
    fi
fi

# ── 10c. Discord voice E2EE (opt-in via --discord-voice) ──────────────────────
# Installs `davey` so discord.py can decrypt DAVE-protocol voice streams.
# Text-only Discord bots do NOT need this; the warning
# "davey is not installed, voice will NOT be supported"
# is purely informational and can be ignored unless you actually join voice.
if [[ "$WANT_DISCORD_VOICE" -eq 1 ]]; then
    step "Installing davey (Discord voice E2EE) — opt-in via --discord-voice"
    if uv sync --python "$PYTHON_BIN" --extra discord-voice >/dev/null 2>&1; then
        ok "davey installed — Discord voice E2EE ready"
    else
        warn "Failed to install davey. It only ships wheels for some platforms;"
        info "see https://pypi.org/project/davey/ — text-only Discord bots are unaffected."
    fi
fi

# ── 14. Signal connector (JVM signal-cli + JDK) ───────────────────────────────
# Opt-in via --signal. Installs a JDK + the JVM signal-cli build because the
# Homebrew native signal-cli cannot send to remote recipients.
if [[ "$WANT_SIGNAL" -eq 1 ]]; then
    step "Installing JVM signal-cli + JDK (Signal connector) — opt-in via --signal"
    install_signal_jvm || warn "Signal setup incomplete — see messages above."
fi

# ── 11. pre-commit git hooks ──────────────────────────────────────────────────
# Installs the hooks defined in .pre-commit-config.yaml so `git commit`
# auto-runs ruff/mypy/etc. before each commit.
if [[ "$WANT_PRECOMMIT" -eq 1 ]]; then
    step "Installing pre-commit git hooks"
    if [[ -f .pre-commit-config.yaml ]]; then
        if uv run pre-commit --version >/dev/null 2>&1; then
            uv run pre-commit install || warn "pre-commit hook install failed"
            ok "pre-commit hooks installed (run on every git commit)"
        else
            warn "pre-commit not in venv — re-run after \`uv sync --dev\`"
        fi
    else
        info "No .pre-commit-config.yaml in repo — skipping"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
step "Done"
cat <<EOF

  ${BOLD}Lionclaw is ready.${RESET}

  Activate the venv:
    source .venv/bin/activate

  Or run commands directly:
    uv run olano --help
    uv run pytest -q
    make check

  ${BOLD}Optional add-ons (re-run with these flags):${RESET}
    ./install.sh --ibkr           # Pull IB Gateway image (~700 MB, needs Docker)
    ./install.sh --gcloud         # Google Cloud SDK (~700 MB)
    ./install.sh --discord-voice  # Install davey for Discord voice E2EE
    ./install.sh --signal         # Install JDK + JVM signal-cli (Signal connector)
    ./install.sh --no-docker      # Skip Docker entirely
    ./install.sh --no-chromium    # Skip Playwright Chromium download
    ./install.sh --no-whatsapp    # Skip npm install for WhatsApp bridge
    ./install.sh --no-ngrok       # Skip ngrok tunnel install
    ./install.sh --no-postgres    # Use embedded SQLite instead of the Postgres backend
    ./install.sh --postgres       # Force Postgres on an existing SQLite home
    ./install.sh --minimal        # Just python + uv + deps

  ${BOLD}Trading-tool defaults are PAPER:${RESET}
    Alpaca   → mode='paper'    (set ALPACA_PAPER_API_KEY + _SECRET)
    Binance  → mode='testnet'  (set BINANCE_TESTNET_API_KEY + _SECRET)
    Bybit    → mode='testnet'  (set BYBIT_TESTNET_API_KEY  + _SECRET)
    OKX      → mode='demo'     (set OKX_DEMO_API_KEY + _SECRET + _PASSPHRASE)
    Kraken   → dry_run=True    (validate-only, no live orders by default)
    IBKR     → mode='paper'    (port 7497 TWS / 4002 Gateway)
    Coinbase → ⚠ NO SANDBOX — every order hits production. Use small sizes.

  Pass mode='live' / dry_run=False explicitly to use real funds.

EOF
