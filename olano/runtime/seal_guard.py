"""Refuse to boot a fresh home beside an unmounted sealed data volume.

Managed boxes provisioned with a LUKS2 "sealed data volume" keep the olano home
(``/home/olano/olano``, or the earlier ``.olano`` / ``.lionclaw``) and the Postgres data dir on
an encrypted volume that is unlocked and mounted at boot. After an unclean
reboot that unlock can lose the race with Docker: the runtime container comes up
while the mountpoint is still an empty directory on the unencrypted root disk,
``get_olano_home()`` creates the home there without complaint, and
``RuntimeServer._provision_on_first_boot`` seeds a brand-new admin, agent and
vault into it, on the wrong disk, in the clear, with the real home sitting
unopened next to it. Nothing on that path can tell "first boot" from "the disk
is not here yet"; this module can.

The provisioner leaves ``/etc/olano/seal.json`` on the root disk (mounted
read-only into the runtime container): the seal *state*, ``mode`` and the
``luks_uuid`` of the sealed volume. When the state names a UUID, the guard
proves that the home directory's own mount is backed by the sealed volume's
device-mapper node (``/dev/mapper/olano-sealed``), read from
``/proc/self/mountinfo``; otherwise :func:`assert_home_on_sealed_volume` raises
:class:`SealGuardError` and ``RuntimeServer.start`` exits non-zero so
``restart: unless-stopped`` retries once the volume is up.

Why the mount table and not a marker file: the runtime runs in a container
whose only view of the box's home is the one bind mount of the home directory
itself (``deploy/docker-compose.allinone.yml``). The sealer also writes an
``.olano-sealed-id`` marker at the root of the sealed mount and beside the home
directory, and the first sealed boxes read that marker from *inside* the
container, where the home's parent directory is the container's own filesystem:
the marker was never visible, the guard refused every boot, and the runtime
sat in a restart loop with exit code 3 on a box whose seal was fine. The mount
source is the one fact the container can see that says which disk the home is
on. The marker stays a host-side check (the sealer's own ``assert-mounted`` and
the live check read it).

A box with no seal state, every deployment that predates sealed volumes, passes
at the cost of one ``stat``. ``/api/status`` reports the custody mode from the
same file. Stdlib only and no olano imports: it runs before anything else on
every boot.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Seal state written by the provisioner on the ROOT disk (it must be readable
# while the sealed volume is still locked). Tests point it elsewhere.
SEAL_STATE_PATH = "/etc/olano/seal.json"
SEAL_STATE_ENV = "LIONCLAW_SEAL_STATE"
# The device-mapper node the sealer opens the volume as. A mount whose
# source is this node is on the sealed
# volume; the name is fixed across every sealed box. Tests point it elsewhere.
SEAL_MAPPER = "/dev/mapper/olano-sealed"
SEAL_MAPPER_ENV = "LIONCLAW_SEAL_MAPPER"
# The mount table of this process (the container's, inside Docker). Tests
# point it at a file of their own.
MOUNTINFO_PATH = "/proc/self/mountinfo"
MOUNTINFO_ENV = "LIONCLAW_MOUNTINFO"
# Host-side marker the sealer writes at the root of the sealed mount and beside
# the home directory. The guard does not read it (see the module docstring);
# the name is kept here because the sealer and the /api/status docs cite it.
SEALED_ID_FILENAME = ".olano-sealed-id"

# proc(5): a mountinfo field escapes space, tab, newline and backslash as
# three-digit octal (``\040`` and friends).
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


class SealGuardError(RuntimeError):
    """The home is not on the sealed volume this box was provisioned with."""


def seal_state_path() -> Path:
    return Path(os.environ.get(SEAL_STATE_ENV) or SEAL_STATE_PATH)


def sealed_mapper() -> str:
    return os.environ.get(SEAL_MAPPER_ENV) or SEAL_MAPPER


def mountinfo_path() -> Path:
    return Path(os.environ.get(MOUNTINFO_ENV) or MOUNTINFO_PATH)


def read_seal_state() -> dict[str, Any] | None:
    """The seal state, or ``None`` when the file is absent or unparseable.

    Unparseable is logged and treated as absent rather than raised: the guard
    must never turn a corrupt state file on an ordinary box into a boot loop.
    """
    path = seal_state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("seal state %s is unreadable (%s) - treating it as absent", path, exc)
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        logger.warning("seal state %s is not valid JSON (%s) - treating it as absent", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("seal state %s is not a JSON object - treating it as absent", path)
        return None
    return data


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def holding_mount(path: Path) -> tuple[str, str] | None:
    """``(mount point, mount source)`` of the mount that holds ``path``.

    The longest mount point that is ``path`` itself or one of its ancestors
    wins, which is how the kernel resolves the path too. ``None`` when the
    mount table cannot be read. Each mountinfo line is ``ID PARENT MAJ:MIN ROOT
    MOUNTPOINT OPTIONS [optional fields] - FSTYPE SOURCE SUPEROPTIONS``
    (proc(5)); the separator is what makes the optional fields skippable.
    """
    resolved = os.path.realpath(path)
    try:
        lines = mountinfo_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    best: tuple[str, str] | None = None
    for line in lines:
        head, sep, tail = line.partition(" - ")
        if not sep:
            continue
        head_fields = head.split()
        tail_fields = tail.split()
        if len(head_fields) < 5 or len(tail_fields) < 2:
            continue
        point = _unescape(head_fields[4])
        source = _unescape(tail_fields[1])
        holds = resolved == point or resolved.startswith(point.rstrip("/") + "/")
        if holds and (best is None or len(point) > len(best[0])):
            best = (point, source)
    return best


def _as_int(value: Any) -> int | None:
    """An integer field of the seal state, or ``None`` for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def custody_status(home: Path) -> dict[str, Any]:
    """Data-custody summary for ``/api/status``.

    ``{"mode": "none"}`` on every box without seal state; otherwise the mode
    and the volume's LUKS UUID (public identifiers, never key material).
    ``sealed`` is ``False`` because a runtime that is answering has the volume
    open in front of it.

    The rest mirrors what the sealer writes into ``seal.json`` after every
    ceremony transition (sealer 12 and later): ``sealer_version``; ``recovery``
    as ``{status, at}`` where status is ``none`` (no recovery key yet),
    ``pending`` (a key was shown and not yet confirmed) or ``acknowledged``
    (the slot the recovery keyslot occupies is deliberately not surfaced);
    ``header_generation``, the number of the LUKS header backup the control
    plane holds; and ``ceremony_api``, whether the state names the sealer's
    ceremony socket. A state written by an older sealer lacks those keys and
    reads as ``none`` / ``None`` / ``False``, so the dashboard's card renders
    with "no recovery key" rather than not at all. Only ``/api/status`` reads
    this, and it is public: nothing here is a fingerprint or a key.
    """
    state = read_seal_state()
    if not state:
        return {"mode": "none"}
    recovery_raw = state.get("recovery")
    recovery = recovery_raw if isinstance(recovery_raw, dict) else {}
    version = state.get("sealer_version")
    return {
        "mode": str(state.get("mode") or "unknown"),
        "sealed": False,
        "luks_uuid": state.get("luks_uuid"),
        "sealer_version": str(version) if version not in (None, "") else None,
        "recovery": {
            "status": str(recovery.get("status") or "none"),
            "at": recovery.get("at") if isinstance(recovery.get("at"), str) else None,
        },
        "header_generation": _as_int(state.get("header_generation")),
        "ceremony_api": bool(state.get("serve_socket")),
    }


def assert_home_on_sealed_volume(home: Path) -> None:
    """Raise :class:`SealGuardError` unless ``home`` sits on the sealed volume.

    No seal state, or state without a ``luks_uuid``, means there is nothing to
    check and the call returns at once. Otherwise the mount holding ``home``
    must be sourced from the sealed device-mapper node.
    """
    state = read_seal_state()
    if not state:
        return
    expected = str(state.get("luks_uuid") or "").strip()
    if not expected:
        return
    mapper = sealed_mapper()
    found = holding_mount(Path(home))
    if found is None:
        raise SealGuardError(
            f"cannot read {mountinfo_path()} to prove the home {home} is on the sealed volume "
            f"{expected} named by {seal_state_path()}. Refusing to provision a fresh home on the "
            "unencrypted disk."
        )
    point, source = found
    if source != mapper:
        raise SealGuardError(
            f"the home {home} is on {source} (mounted at {point}), not on {mapper}: "
            f"{seal_state_path()} says this box keeps its data on LUKS volume {expected}, so the "
            "home is not on the sealed volume (is it mounted?). Refusing to provision a fresh home "
            "on the unencrypted disk."
        )
