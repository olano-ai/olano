"""Machine-level bootstrap environment for provisioned deployments.

Whoever provisions a box writes deployment identity/config **once** to a
well-known env file, and every olano process picks it up automatically —
regardless of how it is launched (CLI, `olano serve`, docker, systemd, cron).
This is what makes cloud-provisioned boxes zero-touch: the cloud
provisioning worker bakes the MCP OAuth relay wiring
(``LIONCLAW_MCP_RELAY_URL`` / ``LIONCLAW_MCP_RELAY_PRIVATE_KEY`` /
``LIONCLAW_INSTANCE_ID`` / ``LIONCLAW_PUBLIC_URL``) into this file via
cloud-init, and the runtime simply has it.

``LIONCLAW_MCP_RELAY_PRIVATE_KEY`` is this instance's Ed25519 relay signing key
(base64 PKCS8 DER); the relay holds only the matching public key. The older
``LIONCLAW_MCP_RELAY_SECRET`` — a symmetric secret shared with the relay — is
still read as a fallback for boxes not yet re-keyed and for the single-secret
reference relay (see :mod:`olano.core.mcp_relay`). On a cloud-provisioned box the
relay-key agent replaces the secret with a private key in place, so expect to see
one or the other here, not both.

Search order (first file that exists wins):

1. ``$LIONCLAW_BOOTSTRAP_ENV`` — explicit override (a file path)
2. ``/opt/olano/olano.env``   — written by olano-cloud cloud-init / installers
3. ``/etc/olano/olano.env``   — conventional alternative for hand-managed boxes

Precedence is **lowest**: a variable already present in the process environment
(or loaded from a ``.env``) is never overwritten. The file holds a per-deployment
secret, so provisioners write it ``0600``; parsing tolerates comments, blank
lines, ``export`` prefixes, and quoted values.
"""

from __future__ import annotations

import os
from pathlib import Path

_BOOTSTRAP_ENV_VAR = "LIONCLAW_BOOTSTRAP_ENV"
_DEFAULT_PATHS = ("/opt/olano/olano.env", "/etc/olano/olano.env")


def bootstrap_env_path() -> Path | None:
    """The bootstrap env file in effect, or ``None`` when no file exists."""
    override = os.environ.get(_BOOTSTRAP_ENV_VAR, "").strip()
    candidates = [override] if override else list(_DEFAULT_PATHS)
    for cand in candidates:
        p = Path(cand)
        if p.is_file():
            return p
    return None


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines (comments / blanks / ``export`` / quotes ok)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_bootstrap_env() -> list[str]:
    """Overlay the bootstrap file onto ``os.environ`` (existing vars win).

    Returns the list of variable names actually applied (for logging/doctor).
    Never raises: a missing or unreadable file is simply a no-op, so this is
    safe to call unconditionally from every entry point.
    """
    path = bootstrap_env_path()
    if path is None:
        return []
    try:
        pairs = parse_env_file(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    applied: list[str] = []
    for key, value in pairs.items():
        if key not in os.environ and value != "":
            os.environ[key] = value
            applied.append(key)
    return applied
