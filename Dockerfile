# ── gog (gogcli) builder ──────────────────────────────────────────────────────
# gogcli has no prebuilt release binaries; build the pinned ref from source in a
# throwaway Go stage and copy just the binary below (keeps the runtime image
# Go-toolchain-free). Same pattern as the olano-sandbox image.
FROM golang:1.27.0-bookworm AS gog-builder
ARG GOGCLI_REF=v0.12.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${GOGCLI_REF} https://github.com/steipete/gogcli.git . \
    && make \
    && install -Dm755 ./bin/gog /out/gog


FROM python:3.14-slim

# ── Layer order is the whole point of this file ──────────────────────────────
# Every managed box pulls this image on first boot and on every console update,
# and this image is 2.96 GB (CI's size report). Whether that pull moves 3 GB or
# 50 MB depends entirely on whether the layers BELOW the source copy keep their
# digests from one build to the next. Two things used to defeat that on every
# commit, so every 3-hourly build published ~2.9 GB of brand-new blobs and no
# build step was ever CACHED (verified against run #511's logs):
#
#   1. The build-identity ARG/ENV (OLANO_BUILD_SHA) sat at the top. BuildKit
#      folds every in-scope ARG into each RUN's cache key — `docker history`
#      shows it as the `RUN |2 OLANO_BUILD_SHA=… ` prefix on every layer — so a
#      new commit SHA invalidated apt, Node, gh, uv and Playwright alike.
#      It now lives at the very END of the file (see "build identity").
#   2. `COPY olano ./olano` came before `uv pip install`, so any source edit
#      re-ran the 1.32 GB dependency install. Dependencies are now installed
#      from uv.lock BEFORE any source is copied, and the source is installed
#      afterwards with --no-deps.
#
# Keep it that way: nothing that changes per commit may appear above the
# "source" section. (`build-images.yml` uses a GHA layer cache; with stable
# keys the backend build drops from ~4.5 min to ~1 min, and a release's pull
# delta on the fleet from ~2.9 GB to the source layers.)

# ── system deps + default agent CLIs ─────────────────────────────────────────
# The CLIs an agent shells out to (via execute / safe_shell) and that in-process
# tools invoke locally when no Docker sandbox is live (sandboxed_run falls back to
# a host subprocess — e.g. media_tools runs ffmpeg, doc skills run pandoc /
# pdftotext). This mirrors docker/olano-sandbox so the backend container has the
# same toolkit as the exec sandbox. `gh` is added from its official repo below.
# ffmpeg is NOT agent-only tooling and must stay: the Discord and QQ channel
# adapters shell out to it in-process (voice playback, voice-note transcoding
# for STT), and it is the one CLI olano's tool requirements declare
# (tools/requirements.py). pandoc and poppler-utils are the opposite — no
# olano code calls them; they exist for agents running document skills, which
# execute in the sandbox image (which ships both). Dropping them takes this
# layer from 1.02 GB to 808 MB (measured in CI) in an image pulled on every
# update. Less than the ~370 MB their dependency closures suggest, because
# ffmpeg's closure already covers much of the same ground.
# tmux and bash-completion are the exception to that "sandbox-only" split and
# stay here too (~2 MB): on a self-hosted box with no exec sandbox the dashboard
# terminal opens its PTY into THIS container (runtime/terminal.py), so this is
# where a detachable session and Tab-completion have to exist for that surface
# to behave like a normal shell. `zstd` joins them for the same reason and at
# the same cost: `tar --zstd -xf x.tar.zst` execs it, and modern release
# tarballs ship that way. Its sandbox-image counterpart also installs
# `python-is-python3`; that one is deliberately NOT here — the python:*-slim
# base already provides an unversioned `python`, and the Debian shim would only
# add a second symlink further down PATH. `vim-tiny` + `nano` are here for the
# same self-hosted-terminal reason as tmux (a terminal with no editor cannot
# open a file; the sandbox image carries full `vim`, this one the ~1 MB build).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ca-certificates \
    gnupg \
    git \
    build-essential \
    ripgrep \
    jq \
    sqlite3 \
    tmux \
    bash-completion \
    vim-tiny \
    nano \
    zstd \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── GitHub CLI (gh) — official apt repo ──────────────────────────────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# gog (gogcli) — built in the stage above, dropped onto PATH here.
COPY --from=gog-builder /out/gog /usr/local/bin/gog

# ── docker CLI (client only) ──────────────────────────────────────────────────
# On a managed deployment the runtime spawns the source-/secret-free exec sandbox
# as sibling containers via the host docker socket (mounted by
# deploy/docker-compose.allinone.yml). Client only — no daemon; it talks to the
# host daemon over /var/run/docker.sock.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

# ── Node.js (LTS via nodesource) ──────────────────────────────────────────────
# Node itself stays: the runtime runs `npm install` in-process to set up the
# WhatsApp bridge's dependencies on first use (runtime/api.py).
#
# The global agent CLIs (yarn, @openjobs/cli, @anthropic-ai/claude-code,
# @openai/codex) do NOT stay. Nothing in olano invokes them — they are there
# for an agent to shell out to, and an agent's shell is the sandbox image,
# which installs the same set. Keeping a second copy here cost 1.07 GB in the
# image every box pulls on every update (this layer measured 1.3 GB with them
# and 229 MB without, in CI), to serve a container the agent does not execute
# in on a managed deployment. A self-hosted install that runs without the
# sandbox (so the agent's shell falls back to this container) can add them at
# runtime with `npm install -g`.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── uv (fast Python package installer) ───────────────────────────────────────
# Pinned, not :latest: this COPY is a layer, and every layer below it — the
# 1.32 GB dependency install included — keys on its digest. A moving tag would
# quietly republish all of them on uv's release cadence. Bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV UV_SYSTEM_PYTHON=1

# ── interactive-shell defaults ───────────────────────────────────────────────
# Shared with the exec sandbox image (same file, two build contexts): loads
# bash-completion, deepens history, sets a cwd prompt — for interactive bash
# only. Debian ships the completion loader commented out in /etc/bash.bashrc,
# so without this the package installed above would sit there unused and Tab in
# the dashboard terminal would fall back to bare filename completion.
COPY docker/olano-sandbox/profile-interactive.sh /etc/profile.d/olano-interactive.sh
RUN chmod 0644 /etc/profile.d/olano-interactive.sh

# ── app ──────────────────────────────────────────────────────────────────────
WORKDIR /app

# ── dependencies (source-independent, lockfile-pinned) ───────────────────────
# Install the dependency closure from uv.lock BEFORE any source exists in the
# image, so this 1.32 GB layer keys only on pyproject.toml + uv.lock and stays
# byte-identical across source-only commits (see the layer-order note at the
# top). `uv export` needs only the two files it is given — the project version
# is static, so no package metadata build is involved — and `--no-emit-project`
# leaves olano itself out; the source is installed further down.
#
# --locked (not --frozen): a pyproject edit that was not followed by `uv lock`
# fails the build loudly here instead of silently shipping the lock's old pins.
# CI's `uv lock --check` step catches the same drift on the PR first.
# --no-dev: the image ships the base install only, never the dev group (this is
# also what keeps the opt-in hf-embeddings group — torch — out of the image).
# --extra postgres: Docker/cloud deploys default to the Postgres checkpoint
# backend; the extra is a no-op alias today (psycopg ships in the base install)
# and stays named so that stops being true without touching this file.
#
# Install into system Python (UV_SYSTEM_PYTHON=1) so the `olano` console script
# lands on PATH for the ENTRYPOINT. --no-cache: uv otherwise leaves its
# download/build cache in /root/.cache/uv, inside this layer, where nothing can
# ever read it again — ~150 MB of pure ballast in an image every box re-pulls.
# BuildKit caches the layer itself, so builds are not slower in CI.
#
# This pins the image to the SAME versions `make dev` and CI's tests run
# against. The previous form (`uv pip install -e ".[postgres]"` after copying
# the source) resolved fresh from pyproject on every build and could drift from
# the lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv export --locked --no-dev --no-emit-project --no-hashes --extra postgres \
        -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# ── Playwright headless Chromium ──────────────────────────────────────────────
# The install above pulls in the `playwright` *Python package*, but the browser
# itself is a separate download. Bake a headless browser + its system libraries
# into the image so the built-in browser_* tools (browser_navigate,
# browser_screenshot, page_screenshot, …) work out of the box. Firefox / WebKit
# are never downloaded.
#
# `chromium-headless-shell`, NOT `chromium`: Playwright ships two Chromium
# builds and installing the full browser brings BOTH (597 MB + 323 MB measured).
# Every launch in olano is headless (tools/browser.py), so the full browser was
# weight no code path could reach: this layer measured 1.07 GB with both builds
# and 450 MB with the shell alone. LIONCLAW_BROWSER_CHANNEL tells the runtime
# to launch the shell — it is env-gated rather than hardcoded because the
# channel has to match what is installed, and a developer who ran
# `playwright install chromium` has no shell binary (browser._launch_kwargs).
#
# This layer depends only on the playwright version pinned in uv.lock, and it
# sits ABOVE the source copy for exactly that reason: it stays cached across
# every source-only commit. --with-deps runs its own apt-get update, so clean
# the lists afterward. Browsers land in PLAYWRIGHT_BROWSERS_PATH (baked into the
# image, outside the /root/.lionclaw volume); Playwright reads the same env var
# at launch time so the runtime finds them.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    LIONCLAW_BROWSER_CHANNEL=chromium-headless-shell
RUN playwright install --with-deps chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*

# ── source ────────────────────────────────────────────────────────────────────
# Everything from here down changes per commit and is what a release's pull
# delta actually consists of (~50 MB). pyproject declares readme = "README.md"
# and packages = ["olano"], so hatchling's editable build needs both present —
# that is why the README was copied with the lockfile above. --no-deps: the
# closure is already installed; this only registers the package + console
# script. Installing it editable keeps /app/olano the import path, so the
# `COPY . .` below (configs, archetypes, deploy scripts) lands in place.
COPY olano ./olano
RUN uv pip install --system --no-cache --no-deps -e .

# Remaining project files (configs, archetypes already under olano/, etc.)
COPY . .

# ── workspace volume ──────────────────────────────────────────────────────────
# Deliberately still the legacy .lionclaw: for bare `docker run` users the
# VOLUME makes home.py's sticky chain resolve to the persisted dir (the
# mountpoint exists, so the chain picks it), and renaming it would strand
# their existing volumes on image upgrade. Managed boxes override both via
# compose (OLANO_HOME/LIONCLAW_WORKSPACE keyed on OLANO_HOME_DIR — .olano on
# fresh boxes).
VOLUME ["/root/.lionclaw"]
ENV LIONCLAW_WORKSPACE=/root/.lionclaw/workspace

# ── allocator ────────────────────────────────────────────────────────────────
# glibc hands every thread that allocates its own arena (up to 8 per core) and
# keeps freed memory mapped inside each one, so a process that deserializes
# something large on a worker thread and frees it keeps the pages. Measured on
# a managed box under tracemalloc: 270 MB of live Python objects behind 4.6 GB
# of RSS, every byte of the difference freed heap. Two arenas keep that
# retention to what one thread's churn can hold, and the runtime still calls
# malloc_trim after the passes that read whole threads.
ENV MALLOC_ARENA_MAX=2

# ── expose ACP HTTP port ───────────────────────────────────────────────────────
EXPOSE 8765

# ── init: something has to reap orphans ──────────────────────────────────────
# Without an init the runtime is PID 1, and the kernel hands it every process
# whose parent exits first. Python reaps only the children it started itself,
# so those orphans stay zombies until the container restarts: a managed box
# showed twelve, all Chromium helpers (`headless_shell`) left behind by the
# render tools and the headless browser. tini is PID 1 instead, reaps them, and
# forwards signals to the runtime (docker stop's SIGTERM still reaches it, and
# its exit code is the container's). Signals go to the runtime alone, not its
# process group, so a stop still lets it shut its own children down in order.
# Its own layer, after every heavy one, so adding it leaves the big package
# layer above cache-stable for the fleet. The containers the runtime starts
# itself (stdio MCP, jobs, the browser sidecar) already run with `--init`.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# ── build identity ───────────────────────────────────────────────────────────
# Baked so the running container can answer "what am I?" — see
# olano/deployment/version.py. Without this the box reported no version, the
# console rendered NULL as "latest (as deployed)", and a deploy that pulled a
# stale image was indistinguishable from one that worked (2026-08-14).
# Empty on a local build, which is honest: the runtime then reports the package
# version, or nothing at all, rather than inventing one.
#
# LAST on purpose. These values change on every commit, and BuildKit keys every
# RUN that has an ARG in scope on that ARG's value — declared at the top, this
# block invalidated every layer in the image on every build (see the note at
# the head of the file). Only the runtime reads them (an ENV in the final image
# config), so declaring them after the last RUN is behaviour-identical for the
# container and leaves every layer above cache-stable.
ARG OLANO_BUILD_SHA=""
ARG OLANO_BUILD_TIME=""
ENV OLANO_BUILD_SHA=${OLANO_BUILD_SHA} \
    OLANO_BUILD_TIME=${OLANO_BUILD_TIME}

# ── default: start runtime ────────────────────────────────────────────────────
# Under tini (see "init" above). `command:` in the compose files and any
# `docker run <image> <args>` still reach `olano` as its arguments.
ENTRYPOINT ["/usr/bin/tini", "--", "olano"]
CMD ["serve"]
