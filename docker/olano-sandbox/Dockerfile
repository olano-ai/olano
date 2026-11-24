# ── gog (gogcli) builder ──────────────────────────────────────────────────────
# gogcli ships no prebuilt release binaries, so build the pinned ref from source
# in a throwaway Go stage and copy just the static binary into the final image.
# Keeps the runtime image Go-toolchain-free.
FROM golang:1.27.4-bookworm AS gog-builder
ARG GOGCLI_REF=v0.12.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch ${GOGCLI_REF} https://github.com/steipete/gogcli.git . \
    && make \
    && install -Dm755 ./bin/gog /out/gog


FROM debian:bookworm-slim

# The document/OCR toolchain. ON by default: the built-in docx/pptx/xlsx/pdf
# skills are dead weight without it — an agent is non-root and cannot apt-get at
# run time, so a tool absent from this image is absent from the box, and the
# skills spent a release telling users so ("LibreOffice is NOT installed") while
# every box pulled this image rather than building it. `--build-arg
# SANDBOX_EXTRAS=0` still builds the lean variant for an operator who wants it.
ARG SANDBOX_EXTRAS=1

# Base toolchain + the CLIs an agent shells out to via execute/safe_shell. Kept
# in one layer. `jq` is on the shell allowlist; `sqlite3` is the CLI agents use
# to inspect/query local .db files (including olano's own sessions DB);
# `poppler-utils` (pdftoppm/pdftotext) and `pandoc` cover the common document
# conversions. `tmux` keeps a long-running command alive across `execute` calls
# (each one is its own short-lived exec) and across dashboard terminal
# reconnects — `tmux new -d -s build …` then `tmux capture-pane -p -t build`.
# `bash-completion` is what makes Tab in the dashboard terminal complete git
# subcommands and the like on top of readline's built-in filename completion
# (the PTY runs bash here — see runtime/terminal.py:_spawn_sandbox_shell).
# Both are ~2 MB together, and unlike a pip/npm package the non-root agent
# cannot add them at run time.
# `python-is-python3` makes the bare `python` resolve: models reach for it by
# reflex, and on a python3-only image every first attempt died with "command
# not found" before the retry. `zstd` is the binary `tar --zstd -xf x.tar.zst`
# execs — modern release tarballs (Ollama's among them) ship that way, and the
# extraction failed with "zstd: Cannot exec" until it was baked in here.
# `vim` + `nano`: the dashboard Terminal lands in THIS container on a managed
# box, and a terminal with no editor cannot open a file — `vi` is what people
# type by reflex, `nano` the one that needs no lesson. The non-root agent
# cannot apt-get either at run time.
# (`python` itself no longer depends on this image: the runtime writes a
# `python` → python3 shim into the shared tool layer, appended last on PATH —
# olano/backends/interpreter_shims.py — so an old image or container still
# resolves it. `python-is-python3` stays so the image is right on its own.)
#
# The everyday shell (the list's tail, from `zip` on). bookworm-slim ships coreutils,
# tar, gzip, grep, sed, find and diff and nothing else a person types by
# reflex, and every package above is installed --no-install-recommends, so
# git's own Recommends (less, patch, openssh-client) never arrive either. A
# zip someone uploads died on `unzip: command not found` in the dashboard
# Terminal, as did `ps`, `free`, `file` and `less`. Grouped by what they buy:
#   archives   zip/unzip (.zip), bzip2 (`tar -xjf`), xz-utils (`tar -xJf`, the
#              format most source tarballs ship in), p7zip-full (`7z`: .7z and
#              the long tail of formats nothing else here opens).
#   files      file (identify an upload), less (the pager git and `man` exec;
#              without it `git log` in the Terminal has no pager), tree, patch,
#              xxd, dos2unix (CRLF files from Windows users), bc, gettext-base
#              (`envsubst`).
#   processes  procps (ps/top/free/pkill/watch: the tmux workflow above is
#              unusable without `ps` and `pkill`), psmisc (killall/pstree),
#              lsof, htop.
#   network    iproute2 (ip/ss), iputils-ping, bind9-dnsutils (dig/nslookup),
#              netcat-openbsd: the first four commands of any "why can't this
#              container reach X" session. `setcap -r` is load-bearing for
#              ping: its postinst gives it cap_net_raw as a FILE capability,
#              and under --cap-drop=ALL the kernel refuses to exec a binary
#              whose file capabilities it cannot grant ("sh: ping: Operation
#              not permitted", measured on a droplet). Stripped, ping uses
#              the unprivileged ICMP socket docker's default
#              ping_group_range already allows. libcap2-bin (setcap) is named
#              so this never rests on someone else's dependency. openssh-client (ssh/scp/sftp, and git
#              over ssh) and rsync. None of these adds a capability: curl,
#              python and node already open arbitrary sockets, and egress is
#              governed by the sandbox firewall, not by which binaries exist.
#   build      make, for the Makefile every other project ships. Not gcc/g++:
#              most pip/npm packages ship prebuilt wheels/binaries, and the
#              compilers are ~200 MB on every box for the few that do not.
# All of it together is a few tens of MB; the non-root agent cannot apt-get any
# of it at run time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    curl \
    wget \
    ca-certificates \
    gnupg \
    jq \
    sqlite3 \
    tmux \
    bash-completion \
    vim \
    nano \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    nodejs \
    npm \
    ffmpeg \
    poppler-utils \
    pandoc \
    zstd \
    zip \
    unzip \
    bzip2 \
    xz-utils \
    p7zip-full \
    file \
    less \
    tree \
    patch \
    xxd \
    dos2unix \
    bc \
    gettext-base \
    procps \
    psmisc \
    lsof \
    htop \
    iproute2 \
    iputils-ping \
    bind9-dnsutils \
    netcat-openbsd \
    openssh-client \
    rsync \
    make \
    libcap2-bin \
    && setcap -r /usr/bin/ping \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (`gh`) from the official apt repo. Present for people who sign it
# in THEMSELVES in the dashboard Terminal, at their own risk: Olano never signs
# it in, never writes its store, and a login done here lives on the container's
# writable layer until the sandbox is rebuilt (ADR-002, decision 6). Agents do
# not use it — they reach GitHub through the `github` toolkit and clone/push
# private repos through the `git` toolkit, whose credential never enters this
# container.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# `uv` (fast Python installer, on the shell allowlist) plus the small global npm
# CLIs — `yarn` and `@openjobs/cli` (the `openjobs` binary, used by the OpenJobs
# agent-marketplace tools). --break-system-packages: Debian marks system Python
# PEP-668 managed.
#
# The coding-agent CLIs `@anthropic-ai/claude-code` (`claude`) and
# `@openai/codex` (`codex`) are deliberately NOT here. Measured on linux/amd64
# they install to 616 MB — two platform binaries in an image every box pulls on
# every update, for tools nothing in Olano invokes (they exist purely for an
# agent to shell out to, and the `codex-device` sign-in flow is a pure-HTTP
# implementation that never calls the binary). They are one config line away
# instead: `sandbox_tool_sets: ["coding"]` seeds them once into the SHARED tool
# layer, and an agent can always `npm install -g` them on demand — the old
# justification here ("the non-root agent cannot npm -g at run time") predates
# that layer and is no longer true: the runtime sets npm_config_prefix to
# /opt/agent-tools/npm, which is writable by uid 1000 and on PATH.
#
# The cache clean is NOT cosmetic. `npm install -g` leaves every downloaded
# tarball in /root/.npm/_cacache, and because it happens inside the RUN it is
# baked into the layer forever — a download cache no run-time code path can
# ever read, shipped to every box and re-pulled on every update. pip already
# gets the same treatment via --no-cache-dir; this is npm's equivalent, and it
# must stay in the SAME RUN — a later `rm` only adds a whiteout on top of a
# layer that still carries the bytes. Check the "Image size report" step of the
# build-images workflow after touching this line; that is where the number is
# measured for real.
RUN pip install --break-system-packages --no-cache-dir uv \
    && npm install -g yarn @openjobs/cli --no-fund --no-audit \
    && npm cache clean --force \
    && rm -rf /root/.npm

# gog (gogcli) — built in the stage above, dropped onto PATH here.
COPY --from=gog-builder /out/gog /usr/local/bin/gog

# Document/OCR toolchain (SANDBOX_EXTRAS=1, the default). What each line buys:
#   libreoffice-*  the only thing that converts between office formats — .docx/
#                  .pptx/.xlsx → PDF, legacy .doc → .docx, a deck → PNGs for
#                  visual QA — and the only thing that recalculates spreadsheet
#                  formulas. --no-install-recommends keeps Java out.
#   tesseract      OCR: a scanned PDF has no text layer, so `pdftotext` returns
#                  nothing and the only alternative was one vision call per page.
#                  -eng and -osd are *Recommends*, not Depends, so
#                  --no-install-recommends ships an engine with NO data and
#                  every run dies on "Error opening data file eng.traineddata".
#                  Naming them is the fix, not a nicety. Other languages are an
#                  operator's `tesseract-ocr-<lang>` in the shared tool layer.
#   imagemagick    crop/resize/composite/convert from the shell. It cannot read
#                  PDF/PS/EPS — Debian's policy.xml disables those coders after
#                  CVE-2016-3714 — and that stays disabled: this sandbox handles
#                  untrusted PDFs, and `pdftoppm` rasterises them better anyway.
#   qpdf           PDF structure: merge, split, rotate, encrypt, linearise and —
#                  the part pypdf is weakest at — repair a damaged file. NOT
#                  pdftk, which needs a JRE (~120 MB) for a subset of this.
#   fonts-*        for LibreOffice only, and load-bearing: it substitutes by
#                  name, so without metric-compatible faces a .docx set in Arial
#                  or Times reflows and the PDF is wrong. Liberation 2 covers
#                  those; Noto core stops everything else rendering as tofu.
#                  --no-install-recommends means LibreOffice pulls in no fonts of
#                  its own, so these have to be named.
RUN if [ "$SANDBOX_EXTRAS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd \
        imagemagick \
        qpdf \
        fonts-liberation2 fonts-noto-core \
      && fc-cache -f \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# Let LibreOffice see the fonts a skill ships (canvas-design/canvas-fonts), so a
# PDF it exports can use them. They arrive as a read-only bind mount at their
# HOST path, which is not knowable at build time, so this lists the paths that
# mount can take rather than symlinking into /usr/share/fonts. fontconfig treats
# a <dir> that does not exist as empty, so this is inert when nothing is mounted.
COPY skill-fonts.conf /etc/fonts/conf.d/99-olano-skill-fonts.conf
RUN chmod 0644 /etc/fonts/conf.d/99-olano-skill-fonts.conf

# Non-root user for the sandboxed agent shell (defense-in-depth alongside the
# backend's --cap-drop / no-new-privileges run flags). uid/gid 1000 so the
# bind-mounted workspace (created by the runtime) stays writable.
RUN groupadd -g 1000 sandbox \
    && useradd -m -u 1000 -g 1000 -s /bin/bash sandbox

# Shared tool layer + shared dir mountpoints, owned by the sandbox user. The
# runtime bind-mounts <home>/sandbox/tools over /opt/agent-tools (shared across
# ALL agents) and <home>/shared under /shared; creating them here (owned 1000)
# means a lone sandbox with NO mount still has a writable install target.
#
# Under the mount these subdirectories are SHADOWED, not merged — only the
# mountpoint itself survives, so the host side has to create python/ npm/
# cache/ bin/ too. It does: builder.ensure_tool_layer_layout, from the layout
# interpreter_shims.tool_layer_layout() derives off the same env/PATH that
# name these targets (+ npm's lib/node_modules and bin skeleton, which npm 9
# lstats on every `npx` and never creates). This mkdir only matters for a
# container run with NO mount; the derived layout is the one that counts.
RUN mkdir -p /opt/agent-tools/python /opt/agent-tools/npm /opt/agent-tools/cache /opt/agent-tools/bin /shared \
    && chown -R 1000:1000 /opt/agent-tools /shared

# Warm LibreOffice's per-user profile at build time, as the user that will run
# it. The FIRST `soffice` call builds that profile before it converts anything —
# 15-40s on a 0.5-CPU sandbox — and the shell tools that would invoke it run
# under a 60s exec timeout (lionclaw_docker._DEFAULT_EXEC_TIMEOUT) or the
# middleware's 30s default. Without this the most likely first experience of
# "LibreOffice is installed now" is a timeout, which reads exactly like "not
# installed". `|| true`: a warm-up that fails must not fail the build — and the
# `timeout` is what makes that promise good, since a soffice that hangs here
# would hang the build itself rather than skipping the optimisation. HOME is set
# explicitly rather than trusted to `su`: the profile's whole point is landing in
# the same directory the running container's uid 1000 will look in.
RUN if [ "$SANDBOX_EXTRAS" = "1" ]; then \
      timeout 180 su sandbox -s /bin/sh -c \
        'HOME=/home/sandbox soffice --headless --terminate_after_init' >/dev/null 2>&1 || true; \
      ls -d /home/sandbox/.config/libreoffice >/dev/null 2>&1 \
        && echo "libreoffice profile warmed" || echo "libreoffice profile NOT warmed"; \
    fi

# Tool-layer seed script (installs a named set — e.g. `chinese` — of official
# SDKs/CLIs into the shared tool layer on first sandbox start; see
# home config `sandbox_tool_sets`). Static + tiny; the packages download at run
# time into /opt/agent-tools, not into the image.
COPY seed-tools.sh /usr/local/bin/olano-seed-tools
RUN chmod +x /usr/local/bin/olano-seed-tools

# Interactive-shell defaults (completion, history, prompt) for the dashboard
# Terminal's PTY and any `docker exec -it`. See the file's own header.
COPY profile-interactive.sh /etc/profile.d/olano-interactive.sh
RUN chmod 0644 /etc/profile.d/olano-interactive.sh

# Debian bookworm marks the system Python "externally managed" (PEP 668), which
# blocks `pip install` outright. The sandbox is a throwaway per-agent container,
# so allow user-site installs (they land in the shared tool layer via
# PYTHONUSERBASE, set by the backend at run time).
ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_USER=1

WORKDIR /workspace
USER sandbox
