# olano-browser: the live agent browser (config.yaml → browser.mode: live).
#
# One container per agent, started by the runtime on demand (olano/browser/
# sidecar.py) and pulled when an operator turns the live browser on, never
# installed into the runtime container (a managed update replaces that
# container and would lose it). It holds a full Chromium on a virtual display
# and nothing else: no workspace, no credentials, no product source.
#
# Chromium's DevTools endpoint stays on 127.0.0.1 inside the container. The
# runtime reaches it only through `docker exec -i <ctr> browserd.py pipe`, so
# nothing is published and no network path from the runtime is needed.
#
# BASE / INSTALL_PACKAGES exist for building where the distro mirror is out of
# reach: a base that already carries Chromium, Xvfb and python3 (the Playwright
# image does) builds with INSTALL_PACKAGES=0. The published image is the
# default: Debian's maintained Chromium, security-updated with the distro.
ARG BASE=debian:bookworm-slim
FROM ${BASE}

ARG INSTALL_PACKAGES=1

# chromium: the browser. xvfb + xauth: the virtual display it draws on, which
# makes it a real headed browser (sites treat headless Chrome as a bot far more
# often). python3: browserd.py, the supervisor and DevTools pipe (stdlib
# only, but the full stdlib: python3-minimal ships no json or urllib, so the
# daemon died on import and the container stopped at once). Fonts: Liberation for metric-compatible Arial/Times/Courier,
# Noto for most scripts and emoji, so pages a person watches render as they
# would on a desktop.
RUN if [ "$INSTALL_PACKAGES" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends \
            chromium \
            xvfb \
            xauth \
            python3 \
            ca-certificates \
            fonts-liberation2 \
            fonts-noto-core \
            fonts-noto-cjk \
            fonts-noto-color-emoji \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Non-root, uid 1000, like the sandbox. Reuse an existing uid-1000 account when
# the base already has one (Ubuntu images ship `ubuntu`).
RUN if ! getent passwd 1000 >/dev/null; then \
        groupadd -g 1000 browser && useradd -m -u 1000 -g 1000 -s /usr/sbin/nologin browser; \
    fi \
    && mkdir -p /opt/olano-browser /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix

COPY browserd.py /opt/olano-browser/browserd.py

ENV OLANO_BROWSER_PROFILE=/tmp/olano-browser-profile \
    OLANO_BROWSER_WIDTH=1280 \
    OLANO_BROWSER_HEIGHT=800

USER 1000
HEALTHCHECK --interval=30s --timeout=25s --start-period=20s \
    CMD ["python3", "/opt/olano-browser/browserd.py", "version"]
ENTRYPOINT ["python3", "/opt/olano-browser/browserd.py"]
