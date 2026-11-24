#!/usr/bin/env bash
# SessionStart hook: make the live-container test lanes usable immediately.
# Best-effort — never fail the session start, never touch a managed daemon.
#
# Why this exists: in a Claude Code / agent session the docker BINARIES are
# installed (/usr/bin/dockerd) but no daemon is running and no socket exists.
# A single `docker info` therefore reports "Cannot connect to the Docker
# daemon", which reads exactly like "this environment has no docker" — and on
# that reading `make test-sandbox`, tests/integration/* and the whole sandbox
# egress lane get written off as unverifiable. They are not: starting the
# daemon is one command. See CLAUDE.md, "Docker in an agent session".
set -u

cd "$(dirname "$0")/.." || exit 0

start_dockerd() {
  command -v dockerd >/dev/null 2>&1 || return 0
  # Already reachable? Then something else owns it (Docker Desktop, systemd,
  # a mounted socket) and we must not start a second one.
  docker info >/dev/null 2>&1 && return 0
  [ "$(id -u)" = "0" ] || return 0   # rootless setups manage their own daemon

  # A recycled session leaves a dockerd that is gone but a pidfile that is not,
  # and dockerd then refuses to start: "ensure docker is not running or delete
  # /var/run/docker.pid: process with PID N is still running" — where N has
  # since been reused by something unrelated. Clear it ONLY when no dockerd is
  # actually alive, so a genuinely running daemon is never disturbed.
  if ! pgrep -x dockerd >/dev/null 2>&1 && [ -f /var/run/docker.pid ]; then
    echo "[session-start] clearing a stale /var/run/docker.pid…"
    rm -f /var/run/docker.pid
  fi

  echo "[session-start] starting dockerd (live container tests need it)…"
  ( nohup dockerd >/tmp/dockerd.log 2>&1 & ) >/dev/null 2>&1
  for _ in $(seq 1 20); do
    docker info >/dev/null 2>&1 && { echo "[session-start] dockerd ready."; return 0; }
    sleep 1
  done
  echo "[session-start] dockerd did not come up; see /tmp/dockerd.log (non-fatal)."
}

start_dockerd || true
exit 0
