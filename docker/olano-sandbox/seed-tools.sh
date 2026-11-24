#!/bin/sh
# Seed a named tool set into the shared sandbox tool layer (/opt/agent-tools).
#
# Runs INSIDE the sandbox container as the non-root `sandbox` user, once per set
# (sentinel-guarded), backgrounded + best-effort by the caller — a failure just
# means a package isn't pre-seeded and the agent can install it on demand. pip
# installs land in the shared PYTHONUSERBASE (/opt/agent-tools/python) via
# PIP_USER=1; npm globals land in $npm_config_prefix (/opt/agent-tools/npm, on
# PATH); Go binaries go in /opt/agent-tools/bin. Everything here is installed
# ONCE and shared across every agent, and survives restarts/rebuilds because the
# layer is a host bind mount.
#
# NO SET IS ON BY DEFAULT (`sandbox_tool_sets: []`). These are hundreds of MB of
# SDKs and CLIs for workloads most deployments never run, and on a small droplet
# that is a meaningful slice of the disk. Operators turn on what they need.
#
# Only OFFICIAL-API packages are included. No ToS-violating / account-ban-risk
# automation libraries (e.g. personal-WeChat or Xiaohongshu scrapers).
set -u

SET_NAME="${1:-}"
TOOL_DIR="${TOOL_LAYER_DIR:-/opt/agent-tools}"
BIN_DIR="$TOOL_DIR/bin"
SENTINEL="$TOOL_DIR/.seeded-${SET_NAME}-v1"

[ -n "$SET_NAME" ] || { echo "usage: seed-tools.sh <set>"; exit 2; }
[ -f "$SENTINEL" ] && { echo "seed '$SET_NAME' already applied"; exit 0; }
mkdir -p "$BIN_DIR"

log() { echo "[seed:$SET_NAME] $*"; }

pip_install() {
    # Best-effort; PIP_USER=1 + PYTHONUSERBASE target the shared layer.
    python3 -m pip install --disable-pip-version-check --no-input "$@" || \
        log "pip install failed for: $*"
}

npm_install() {
    # Best-effort global install into the SHARED layer. npm_config_prefix is set
    # on the container by the runtime (backends/lionclaw_docker.py
    # _TOOL_LAYER_ENV); defaulted here too so a hand-run outside the runtime
    # still writes to the layer instead of failing on a root-owned /usr/local.
    npm_config_prefix="${npm_config_prefix:-$TOOL_DIR/npm}" \
    npm_config_cache="${npm_config_cache:-$TOOL_DIR/cache/npm}" \
        npm install -g --no-fund --no-audit "$@" || log "npm install failed for: $*"
    # The download cache is pure waste once the install lands — and unlike an
    # image layer this one sits on the customer's disk, in their olano home,
    # where it would show up in the disk breakdown as unexplained growth.
    npm_config_cache="${npm_config_cache:-$TOOL_DIR/cache/npm}" \
        npm cache clean --force >/dev/null 2>&1 || true
}

# Fetch a repo's latest linux binary asset (best-effort, arch-aware).
arch() { case "$(uname -m)" in x86_64) echo amd64;; aarch64|arm64) echo arm64;; *) echo amd64;; esac; }

case "$SET_NAME" in
  chinese)
    log "installing official Chinese cloud/service SDKs + light CLIs"
    # Cloud + storage SDKs (Alibaba, Tencent, Qiniu, Baidu) — all official PyPI.
    pip_install \
        aliyun-python-sdk-core oss2 \
        tccli tencentcloud-sdk-python cos-python-sdk-v5 \
        qiniu bce-python-sdk
    # Messaging / media — official APIs only.
    #   wechatpy  → WeChat Official Account + WeCom (企业微信) + Pay + MiniProgram
    #   bilibili-api-python, yt-dlp → Bilibili / Youku / iQiyi
    pip_install wechatpy bilibili-api-python yt-dlp

    # Alibaba Cloud CLI (`aliyun`) + ossutil — Go binaries, no pip equivalent.
    A="$(arch)"
    aliyun_url="https://github.com/aliyun/aliyun-cli/releases/latest/download/aliyun-cli-linux-latest-${A}.tgz"
    if curl -fsSL "$aliyun_url" -o /tmp/aliyun.tgz 2>/dev/null; then
        tar -xzf /tmp/aliyun.tgz -C "$BIN_DIR" 2>/dev/null && log "aliyun CLI installed" || log "aliyun extract failed"
        rm -f /tmp/aliyun.tgz
    else
        log "aliyun CLI download skipped (offline / URL moved) — tccli + SDKs still available"
    fi
    ;;
  coding)
    log "installing the coding-agent CLIs into the shared tool layer"
    # `claude` (Claude Code) and `codex` (OpenAI Codex). Deliberately NOT baked
    # into the image: 616 MB measured on linux/amd64 for two platform binaries
    # that nothing in Olano invokes — they exist purely for an agent to shell
    # out to, and most deployments never do. Seeded here they cost that space
    # once, on the boxes that asked for them, in the shared layer every agent
    # sees. Each still needs its own sign-in (a Claude subscription/API key, a
    # ChatGPT/OpenAI login) supplied by the user.
    npm_install @anthropic-ai/claude-code @openai/codex
    ;;
  *)
    log "unknown tool set '$SET_NAME' — nothing to do"
    ;;
esac

touch "$SENTINEL"
log "done"
exit 0
