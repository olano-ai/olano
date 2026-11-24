#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# setup-host-user.sh — create the dedicated service user for olano deployments.
# ──────────────────────────────────────────────────────────────────────────────
# Idempotent; run AS ROOT on the box (or pipe it over SSH):
#
#   ssh root@<ip> 'bash -s' < deploy/setup-host-user.sh
#   ssh root@<ip> 'bash -s -- --no-root-keys' < deploy/setup-host-user.sh
#
# Creates the `olano` user (override: OLANO_HOST_USER=<name>) with:
#   • docker group membership (group pre-created; Docker's installer reuses it)
#   • full passwordless sudo (NOPASSWD:SETENV: ALL) — the user must be able to
#     install software (apt, get.docker.com, systemctl, …) non-interactively,
#     and SETENV lets installers pass environment through sudo
#   • root's authorized_keys merged in (DO injects operator SSH keys into root)
#     — skipped with --no-root-keys, for boxes whose operator keys arrive by
#     another path (a managed box that must not inherit the provisioner's key)
#
# Root SSH login is intentionally left enabled as the recovery path.
# The fleet tooling's do-deploy.sh runs this before shipping; the cloud control
# plane's first-boot script mirrors it (olano-ai/cloud
# lionclaw_cloud/cloud_init.py:_render_host_user_block — keep the two in sync).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

U="${OLANO_HOST_USER:-olano}"
NO_ROOT_KEYS=0
for arg in "$@"; do
  case "$arg" in
    --no-root-keys) NO_ROOT_KEYS=1 ;;
    *) echo "✗ setup-host-user.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "✗ setup-host-user.sh must run as root" >&2; exit 1; }

# Docker may not be installed yet (bootstrap installs it later); pre-create the
# group so the membership below sticks — get.docker.com reuses an existing one.
groupadd -f docker
id -u "$U" >/dev/null 2>&1 || useradd -m -s /bin/bash "$U"
usermod -aG docker "$U"

H="$(getent passwd "$U" | cut -d: -f6)"
# `useradd -m` does not chown a home that already exists (e.g. a data volume
# bind-mounted onto it before this runs, as on the cloud Scale tiers), so the
# user could not write to its own home. Chown it explicitly. Non-recursive: an
# existing home with data keeps its contents' ownership.
chown "$U:$U" "$H"
install -d -m 700 -o "$U" -g "$U" "$H/.ssh"
if [ "$NO_ROOT_KEYS" -eq 0 ] && [ -f /root/.ssh/authorized_keys ]; then
  touch "$H/.ssh/authorized_keys"
  sort -u /root/.ssh/authorized_keys "$H/.ssh/authorized_keys" -o "$H/.ssh/authorized_keys"
  chown "$U:$U" "$H/.ssh/authorized_keys"
  chmod 600 "$H/.ssh/authorized_keys"
fi

printf '%s ALL=(ALL) NOPASSWD:SETENV: ALL\n' "$U" > "/etc/sudoers.d/$U"
chmod 440 "/etc/sudoers.d/$U"
visudo -cf "/etc/sudoers.d/$U" >/dev/null

echo "✓ user '$U' ready (docker group + passwordless sudo; home $H$([ "$NO_ROOT_KEYS" -eq 1 ] && printf '; root keys not merged'))"
