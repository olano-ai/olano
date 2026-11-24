#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# build-dashboard.sh — build the dashboard_2.0 frontend (`npm run build`)
# ──────────────────────────────────────────────────────────────────────────────
# Runs the Vite production build for the SPA in olano/dashboard_2.0, emitting
# the static bundle to dashboard_2.0/dist/ (what `vite preview` and the
# deploy serve). Run from anywhere — it resolves paths relative to the repo.
#
# The dashboard image runs `npm run build` itself, so this is for local builds:
# verifying a build is clean, or refreshing the locally-served dist/ after a
# code or asset change.
#
# It also syncs art-source images from the repo-root assets/ into the dashboard's
# public/ (the dir Vite actually serves) BEFORE building, so editing a theme
# image at assets/theme1/*.png reaches the dashboard. Disable with SKIP_ASSET_SYNC=1.
#
#   deploy/build-dashboard.sh                  # sync assets + build
#   SKIP_ASSET_SYNC=1 deploy/build-dashboard.sh  # build only, no asset sync
#   CLEAN_INSTALL=1 deploy/build-dashboard.sh    # `npm ci` instead of conditional install
#
# Env overrides: DASHBOARD_DIR, ASSETS_DIR.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="${DASHBOARD_DIR:-$REPO_ROOT/olano/dashboard_2.0}"
ASSETS_DIR="${ASSETS_DIR:-$REPO_ROOT/assets}"

command -v npm >/dev/null 2>&1 || { echo "✗ npm not found on PATH — install Node.js first." >&2; exit 1; }
[ -f "$DASHBOARD_DIR/package.json" ] || { echo "✗ No package.json at $DASHBOARD_DIR" >&2; exit 1; }

# ── 1. sync art-source assets/ → public/ (so edited images reach the build) ───
if [ "${SKIP_ASSET_SYNC:-0}" != "1" ] && [ -d "$ASSETS_DIR" ]; then
  echo "▶ Syncing assets/ → dashboard public/ …"
  rsync -a "$ASSETS_DIR/" "$DASHBOARD_DIR/public/assets/"
  echo "  synced $ASSETS_DIR → $DASHBOARD_DIR/public/assets"
fi

cd "$DASHBOARD_DIR"

# ── 2. install deps if missing (or always, with CLEAN_INSTALL=1) ──────────────
if [ "${CLEAN_INSTALL:-0}" = "1" ]; then
  echo "▶ npm ci …"
  npm ci
elif [ ! -d node_modules ]; then
  echo "▶ node_modules missing — npm install …"
  npm install
fi

# ── 3. build ──────────────────────────────────────────────────────────────────
echo "▶ npm run build  (in $DASHBOARD_DIR)…"
npm run build

echo "✓ Built → $DASHBOARD_DIR/dist"
