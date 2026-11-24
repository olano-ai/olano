# ──────────────────────────────────────────────────────────────────────────────
# dashboard_2.0 → static SPA served by Caddy (all-in-one deploys)
# ──────────────────────────────────────────────────────────────────────────────
# Two stages:
#   1. node:22  — `npm ci && npm run build` the Vite SPA → /app/dist
#   2. caddy:2  — bake dist into /srv; the Caddyfile (mounted by the compose
#                 file) serves it on / and reverse-proxies /api → olano:8765,
#                 so the dashboard and the backend share ONE origin (cookies +
#                 the terminal WebSocket work — a cross-site rewrite cannot).
#
# Build context is the REPO ROOT (so we can also pull in repo-root assets/):
#   docker build -f docker/dashboard/Dockerfile -t olano-web .
# ──────────────────────────────────────────────────────────────────────────────
FROM node:26-slim AS build
WORKDIR /app

# Install deps first (cached unless the lockfile changes).
COPY olano/dashboard_2.0/package.json olano/dashboard_2.0/package-lock.json ./
RUN npm ci

# App source + art-source assets (mirrors deploy/build-dashboard.sh's asset sync
# so theme images at assets/theme1/*.png reach the build via public/assets).
COPY olano/dashboard_2.0/ ./
COPY assets/ /assets/
RUN mkdir -p public/assets && cp -r /assets/. public/assets/ 2>/dev/null || true

# tsc --noEmit + vite build → dist/ (static, no runtime needed).
RUN npm run build

FROM caddy:2
COPY --from=build /app/dist /srv
