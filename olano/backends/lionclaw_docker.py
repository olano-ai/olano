"""OlanoDockerBackend — Olano's custom Docker sandbox backend.

Subclasses deepagents' ``BaseSandbox``, so it IS a ``SandboxBackendProtocol``:
deepagents' filesystem/skills middleware discovers it with
``isinstance(backend, SandboxBackendProtocol)`` and implements every file
operation (``ls``/``read``/``write``/``edit``/``glob``/``grep``/``delete`` plus
their ``a*`` async twins) on top of the three primitives we provide here —
``execute``, ``upload_files``, ``download_files`` — each a ``docker exec`` into
the live container.

Do NOT hand-roll the file operations: deepagents' base class runs carefully
built server-side ``python3`` scripts and parses their JSON into the
``LsResult``/``ReadResult``/``EditResult``/… dataclasses the middleware expects.
A bare class with ad-hoc ``read``/``ls_info``/``grep_raw`` methods (what this
backend used to be) satisfies none of that contract — every turn died in
``SkillsMiddleware.abefore_agent`` with ``'OlanoDockerBackend' object has no
attribute 'als'``, before the model was ever called.

Lifecycle modes:
  persistent=False  Thread-scoped: new container per conversation thread, auto-removed on stop.
  persistent=True   Assistant-scoped: one named container per agent, reused across threads.
                    snapshot_on_stop=True commits the container image on persistent stop.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from olano.backends.interpreter_shims import (
    CONTAINER_SHIM_PATH,
    CONTAINER_TOOL_LAYER_DIR,
    CONTAINER_TOOL_LAYER_ENV,
    CONTAINER_TOOL_LAYER_PATH,
    shell_path_export,
)

if TYPE_CHECKING:
    from olano.config.backend_config import OlanoDockerBackendConfig

logger = logging.getLogger(__name__)

_SHELL_ENV_DEFAULTS = {
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "GH_SPINNER_DISABLED": "1",
    "PIP_PROGRESS_BAR": "off",
    "npm_config_progress": "false",
    "NPM_CONFIG_PROGRESS": "false",
}

# Shared tool layer. A single host directory (``<home>/sandbox/tools``) is
# bind-mounted into EVERY agent's sandbox at this path, and pip/npm are pointed
# at it. Effect: a package installed by one agent lands in one shared place that
# every other agent's sandbox sees — install once, shared across agents, and it
# survives container restarts AND image rebuilds (it lives on the host, not in
# the container or a throwaway snapshot). The builder supplies the bind-mount via
# ``shared_mounts``; these env vars make the installers target it. When the mount
# is absent the same paths resolve to the image's own (root-created) dir, so a
# lone sandbox still works — it just isn't shared.
#
# The strings live in ``interpreter_shims`` (single source: the Terminal rc file
# is rendered from the same values) and keep their historical names here.
# ``_TOOL_LAYER_PATH`` is PREPENDED to PATH inside the shell so shared
# console-scripts (pip --user bin, npm -g bin) and seeded CLIs (Go binaries in
# bin/) are found; ``_TOOL_LAYER_SHIM_PATH`` is APPENDED so the runtime-written
# `python`/`pip` shims only ever fill a name the image lacks. Both are set at
# exec time because $PATH must expand in-shell.
_TOOL_LAYER_DIR = CONTAINER_TOOL_LAYER_DIR
_TOOL_LAYER_ENV = CONTAINER_TOOL_LAYER_ENV
_TOOL_LAYER_PATH = CONTAINER_TOOL_LAYER_PATH
_TOOL_LAYER_SHIM_PATH = CONTAINER_SHIM_PATH

# The caps every sandbox ran at before limits became configurable. They remain
# the answer when nothing else decides one (a directly-constructed backend, or
# a host whose size could not be detected), so behaviour without a resolver is
# exactly what it always was.
_DEFAULT_MEMORY = "512m"
_DEFAULT_CPUS = 0.5

_LIFECYCLE_CMD = "trap 'exit 0' TERM INT; while :; do sleep 3600; done"
# Docker label carried by sandbox containers that are ALREADY RUNNING on
# deployed hosts. Renaming makes the runtime fail to recognise its own
# containers and recreate them. Never rename.
_LABEL_KEY = "lionclaw.signature"

# Default wall-clock budget for one `docker exec`. deepagents' file operations
# call `execute()` without a timeout, so this bounds them too.
_DEFAULT_EXEC_TIMEOUT = 60
# Exit codes reported when the command never ran / never finished. 124 and 127
# match the shell conventions for "timed out" (GNU timeout) and "command not
# found", so a model reading the transcript sees a familiar signal.
_EXIT_TIMEOUT = 124
_EXIT_UNAVAILABLE = 127

# One-file transfer scripts run inside the container. Only base64 is
# interpolated (never a raw path), so no shell/Python quoting hazard: the whole
# `python3 -c "…"` is passed through `sh -lc`, and the script body itself must
# therefore avoid `$`, backticks and double quotes. Both write a single-token
# status line to stdout so the caller can map it onto a FileOperationError.
_UPLOAD_TEMPLATE = """python3 -c "
import base64, os, sys

path = base64.b64decode('{path_b64}').decode('utf-8')
try:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = base64.b64decode(sys.stdin.read())
    with open(path, 'wb') as fh:
        fh.write(data)
    sys.stdout.write('OK')
except IsADirectoryError:
    sys.stdout.write('E:is_directory')
except FileNotFoundError:
    sys.stdout.write('E:file_not_found')
except PermissionError:
    sys.stdout.write('E:permission_denied')
except (NotADirectoryError, ValueError):
    sys.stdout.write('E:invalid_path')
except OSError as exc:
    sys.stdout.write('E:' + str(exc))
" 2>/dev/null"""

_DOWNLOAD_TEMPLATE = """python3 -c "
import base64, os, sys

path = base64.b64decode('{path_b64}').decode('utf-8')
try:
    if os.path.isdir(path):
        sys.stdout.write('E:is_directory')
    else:
        with open(path, 'rb') as fh:
            sys.stdout.write('D:' + base64.b64encode(fh.read()).decode('ascii'))
except IsADirectoryError:
    sys.stdout.write('E:is_directory')
except FileNotFoundError:
    sys.stdout.write('E:file_not_found')
except PermissionError:
    sys.stdout.write('E:permission_denied')
except (NotADirectoryError, ValueError):
    sys.stdout.write('E:invalid_path')
except OSError as exc:
    sys.stdout.write('E:' + str(exc))
" 2>/dev/null"""

# Error tokens the transfer scripts emit → deepagents FileOperationError codes.
_TRANSFER_ERRORS = {
    "is_directory": IS_DIRECTORY,
    "file_not_found": FILE_NOT_FOUND,
    "permission_denied": PERMISSION_DENIED,
    "invalid_path": INVALID_PATH,
}


class SandboxRun(NamedTuple):
    """Split-stream result of one command, for Olano's own callers.

    deepagents' ``execute()`` returns a single combined ``output`` string; the
    security middleware and ``tools/_sandbox_exec`` need stdout and stderr apart
    (separate previews, `subprocess.CompletedProcess`-shaped results), so they
    call :meth:`OlanoDockerBackend.run` instead.
    """

    stdout: str
    stderr: str
    returncode: int


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker is not installed or not on PATH. "
            "Install Docker to use the lionclaw_docker backend."
        )


def _agent_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip().lower()).strip("-.") or "agent"


def _workspace_hash(workspace_root: Path) -> str:
    return hashlib.sha1(str(workspace_root.resolve()).encode()).hexdigest()[:10]


def _persistent_container_name(agent_name: str, workspace_root: Path) -> str:
    # Names containers that are ALREADY RUNNING on deployed hosts. _start_persistent
    # finds them with `docker inspect <name>` and reuses them, so a renamed prefix
    # orphans the live container and builds a duplicate beside it. Never rename.
    return f"lionclaw-{_agent_slug(agent_name)}-{_workspace_hash(workspace_root)}"


def _container_signature(
    *,
    image: str,
    network: str,
    workspace_root: Path,
    extra_mounts: list[tuple[str, str, str]],
    memory: str,
    cpus: float,
) -> str:
    payload = {
        # Bump when _base_run_args changes what a container gets beyond these
        # inputs (mounts, env, hardening), OR when the image's baked content
        # must reach containers that already exist. The signature otherwise
        # only hashes the *inputs*, so an existing persistent container created
        # by older code would be happily reattached to without the new wiring —
        # e.g. the path-identical workspace mount (schema 2) would silently be
        # missing until someone deleted the container by hand. `image` is a tag
        # string ("olano-sandbox:latest"), never a digest, so re-pulling a
        # rebuilt image does NOT move this hash on its own — that is what
        # schema 3 is for (python-is-python3 + zstd baked into the image) and
        # schema 4 (the document toolchain: LibreOffice, tesseract, qpdf,
        # ImageMagick and the fonts LibreOffice substitutes by name) and
        # schema 5 (the everyday shell: zip/unzip, xz, 7z, file, less, procps,
        # iproute2, dig, ssh, rsync, make and friends).
        # The runtime-written interpreter shims (interpreter_shims.py) needed
        # NO bump: same mount, and PATH is set per exec, so an existing
        # container picks them up on its next command — and the skill fonts the
        # runtime seeds beside them (ensure_skill_fonts) ride that same mount,
        # so they are not what moved this to 4 either. Don't bump "to be safe"
        # — a bump recreates every agent's container on the next restart.
        "schema": 5,
        "image": image,
        "network": network,
        "workspace_root": str(workspace_root.resolve()),
        "mounts": extra_mounts,
        # Resource ceilings ride in the payload rather than needing a bump:
        # docker cannot retune a running container's caps, so a changed limit
        # has to recreate it, and hashing the values makes that automatic.
        "memory": memory,
        "cpus": cpus,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── sandbox networking: a long-lived container can outlive its bridge ─────────
#
# A persistent sandbox is reused by name+signature for as long as the signature
# holds, so one container can survive many runtime restarts — and a docker
# daemon restart (an engine upgrade, a `ufw` reload re-installing the netfilter
# rules, live-restore) can leave a RUNNING container's veth no longer plumbed
# into the bridge. The container keeps its eth0, its /etc/resolv.conf and its
# default route, and reaches nothing at all: ARP for the gateway never
# completes, so every connect ends EHOSTUNREACH or times out and the resolver
# is the first casualty. On a healthy managed box the same probe answers in 0 ms.
#
# That is what shipped as "npx dies EAI_AGAIN on one deployment": npm's retry
# ladder sitting on DNS queries to servers the container could no longer reach,
# 144 s per `npx`, while the compose stack on its OWN bridge was healthy and the
# box looked fine from every other angle. Nothing recreated the container,
# because nothing had changed its signature — the whole point of the signature.
#
# So the reuse path now PROVES the container can still reach its gateway and
# rebuilds it when it cannot. A rebuild re-plumbs the veth, which is the actual
# repair; if a fresh container on the same bridge is still stranded, the bridge
# itself is broken and the escalation moves the sandbox lane onto a dedicated
# docker network (a new bridge, created with its own forward/NAT rules and its
# own subnet — which also clears a docker0 subnet that collides with the host's
# own routes). That choice is sticky, so the next boot computes its signature
# with the rescue network and reuses the container instead of rebuilding it.
_RESCUE_NETWORK = "olano-sandbox"

# ── sandbox isolation: no container may open a connection to a sandbox ────────
#
# On a network with inter-container communication on (docker0 always has it),
# every container can reach every other container's IP directly. A server an
# agent runs in its sandbox and binds to all interfaces (Express's
# `app.listen(PORT)`, Python's `http.server` with no host, which is most of
# them) was therefore reachable from every other agent's sandbox on the box,
# around whatever password the runtime put in front of it, and around nothing at
# all for a server that was never meant to be published.
#
# The fix has to work where the runtime is itself a container (every managed
# box): host iptables written from inside the runtime land in the runtime's own
# network namespace, which is why the egress firewall degrades there. What the
# runtime always has is the docker socket, and docker will enforce this itself:
# a bridge created with `com.docker.network.bridge.enable_icc=false` drops
# container-to-container traffic on it in the daemon's own netfilter rules.
# Containers still reach their gateway (the egress proxy, the embedded DNS) and
# the world through NAT; the runtime still reaches them through `docker exec`,
# which is not network traffic. Nothing Olano runs needs a container-to-sandbox
# connection: the page keeper, the browser relay and the stdio MCP sessions all
# cross over exec pipes.
#
# So the default lane is `_ISOLATED_NETWORK`, created on demand with ICC off.
# The rescue network is created with ICC off too while isolation is on, so a
# repair escalation keeps the guarantee. `LIONCLAW_SANDBOX_ISOLATION=0` (or an
# operator pin of `LIONCLAW_SANDBOX_NETWORK`) restores the shared bridge. A box
# where the isolated network cannot be created falls back to the shared bridge,
# says so once, and reports `isolated: false` in the sandbox status.
#
# Moving lanes changes a persistent sandbox's signature, so each sandbox is
# rebuilt once on the upgrade that turns this on. What an agent installed with
# pip/npm lives in the shared tool layer mount and survives; only files written
# elsewhere in the container's own filesystem do not.
_ISOLATED_NETWORK = "olano-sandboxes"
SANDBOX_ISOLATION_ENV = "LIONCLAW_SANDBOX_ISOLATION"
_ICC_OPTION = "com.docker.network.bridge.enable_icc"
#: A failed create is retried at most this often, so a box that cannot create
#: networks does not pay a docker round trip on every sandbox start.
_NETWORK_RETRY_SECONDS = 60.0
#: Operator override: a docker network name to put every sandbox on. `bridge`
#: pins the historical default bridge and disables the escalation.
SANDBOX_NETWORK_ENV = "LIONCLAW_SANDBOX_NETWORK"
#: `0` skips the reachability probe entirely (and with it the self-repair).
SANDBOX_NETCHECK_ENV = "LIONCLAW_SANDBOX_NETCHECK"
#: Where a successful escalation is remembered, so it survives a restart.
_STICKY_NETWORK_FILE = ("run", "sandbox-network")

_NET_OK = "ok"
_NET_STRANDED = "stranded"
_NET_UNKNOWN = "unknown"

# Runs inside the container. Two signals, in order of authority:
#   1. a reply from the bridge gateway — a connect that opens OR is refused
#      proves the packet made the round trip;
#   2. the gateway's ARP entry — proof of L2 even when a host firewall drops
#      container→host traffic on purpose.
# Only when both say no is the container stranded. Everything else prints
# `unknown` and is never acted on: a false "stranded" costs a rebuild.
#
# The second signal needs time, and that is why this is a polling loop rather
# than one blocking connect. /proc/net/arp shows flag 0x2 for EVERY NUD_VALID
# state — REACHABLE, but also STALE, DELAY and PROBE — so a container stranded
# while idle still carries its gateway's old MAC as STALE. Sending through a
# STALE entry puts it in DELAY for ~5 s before the kernel unicast-probes it
# (3 × 1 s) and only then marks it FAILED (flag 0x0, then gone). A single 1.5 s
# connect reads 0x2 the whole time and calls the container healthy. So:
#   - anything that answers → `ok` immediately (a healthy box: milliseconds);
#   - entry absent/incomplete after a 1.5 s grace → `stranded` (ARP on a live
#     bridge resolves in <10 ms, so 1.5 s of INCOMPLETE is a real failure —
#     this is the FAILED/absent case a busy stranded container is in);
#   - still VALID at the 9.5 s cap → `ok`: a STALE entry that survived a full
#     DELAY+PROBE cycle was re-confirmed by the gateway, so L2 works and only
#     TCP is being dropped. That cap is the boot-time cost, per agent, on a
#     box whose firewall drops container→host TCP — and only there.
# The window this cannot see: a gateway entry still REACHABLE (confirmed <~45 s
# ago) on a container stranded seconds earlier. Stranding happens on a daemon
# restart and is met on the NEXT runtime start, so that window is theoretical.
_NETCHECK_SCRIPT = """
import errno, select, socket, struct, time

GRACE, CAP, TICK = 1.5, 9.5, 0.25

gw = ''
try:
    with open('/proc/net/route') as fh:
        for line in fh.read().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == '00000000':
                gw = socket.inet_ntoa(struct.pack('<L', int(f[2], 16)))
                break
except Exception as exc:
    print('unknown route-unreadable ' + str(exc))
    raise SystemExit(0)
if not gw:
    print('unknown no-default-route')
    raise SystemExit(0)

def arp_state():
    try:
        with open('/proc/net/arp') as fh:
            rows = fh.read().splitlines()[1:]
    except Exception as exc:
        return 'unreadable ' + str(exc)
    for line in rows:
        f = line.split()
        if len(f) > 2 and f[0] == gw:
            return 'valid' if int(f[2], 16) & 0x2 else 'incomplete'
    return 'absent'

sock = socket.socket()
sock.setblocking(False)
pending = True
try:
    sock.connect((gw, 80))
    pending = False
    answered = True
except BlockingIOError:
    answered = False
except OSError as exc:
    pending = False
    answered = exc.errno in (errno.ECONNREFUSED, errno.ECONNRESET)

verdict = None
t0 = time.monotonic()
while verdict is None:
    if answered:
        verdict = 'ok'
        break
    if pending:
        _r, w, _x = select.select([], [sock], [], TICK)
        if w:
            pending = False
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err in (0, errno.ECONNREFUSED, errno.ECONNRESET):
                verdict = 'ok'
                break
    else:
        time.sleep(TICK)
    elapsed = time.monotonic() - t0
    state = arp_state()
    if state.startswith('unreadable'):
        verdict = 'unknown arp-' + state
    elif elapsed >= GRACE and state in ('absent', 'incomplete'):
        verdict = 'stranded'
    elif elapsed >= CAP:
        verdict = 'ok' if state == 'valid' else 'stranded'
sock.close()

print(verdict if verdict.startswith('unknown') else verdict + ' ' + gw)
"""


def _sticky_network_path() -> Path | None:
    try:
        from olano.config.home import get_olano_paths

        return get_olano_paths().root.joinpath(*_STICKY_NETWORK_FILE)
    except Exception:
        return None


def _read_sticky_network() -> str | None:
    path = _sticky_network_path()
    if path is None:
        return None
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def _remember_sticky_network(name: str) -> None:
    path = _sticky_network_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n", encoding="utf-8")
    except OSError:
        logger.warning(
            "sandbox network escalation could not be persisted to %s — it will "
            "be re-derived (and the container rebuilt) on the next start",
            path,
        )


def isolation_enabled() -> bool:
    """Whether sandboxes go on a network where containers cannot reach each other."""
    return os.environ.get(SANDBOX_ISOLATION_ENV, "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


_ISOLATION_STATE: dict[str, bool] = {}
_NETWORK_FAILED_AT: dict[str, float] = {}
_ISOLATION_WARNED = False


def network_isolated(name: str, *, run_fn: Any = None) -> bool:
    """Whether docker drops container-to-container traffic on network ``name``.

    Read from the network's own options (``enable_icc=false``), so it is the
    truth about the bridge in use, not about what this process meant to create.
    Memoized per name for the process; ``bridge``, ``host`` and ``none`` are
    never isolated in this sense (``none`` has no network to share).
    """
    if name in ("bridge", "host", "none", "default", ""):
        return False
    if name in _ISOLATION_STATE:
        return _ISOLATION_STATE[name]
    runner = run_fn or _run
    try:
        proc = runner(["docker", "network", "inspect", "-f", "{{json .Options}}", name], timeout=15)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    try:
        options = json.loads((proc.stdout or "").strip() or "{}") or {}
    except ValueError:
        options = {}
    isolated = str(options.get(_ICC_OPTION, "")).strip().lower() == "false"
    _ISOLATION_STATE[name] = isolated
    return isolated


def _ensure_network(name: str, *, isolated: bool, run_fn: Any = None) -> str | None:
    """Create network ``name`` if it is missing; ``None`` when that is impossible."""
    runner = run_fn or _run
    failed_at = _NETWORK_FAILED_AT.get(name)
    if failed_at is not None and time.monotonic() - failed_at < _NETWORK_RETRY_SECONDS:
        return None
    try:
        proc = runner(["docker", "network", "inspect", name], timeout=15)
        if proc.returncode == 0:
            return name
        argv = ["docker", "network", "create", "--driver", "bridge"]
        if isolated:
            argv += ["--opt", f"{_ICC_OPTION}=false"]
        argv += ["--label", "olano.network=sandboxes", name]
        created = runner(argv, timeout=30)
        # A concurrent create (two agents booting) is a success, not a failure.
        if created.returncode == 0 or "already exists" in (created.stderr or "").lower():
            _NETWORK_FAILED_AT.pop(name, None)
            _ISOLATION_STATE.pop(name, None)
            return name
        logger.error(
            "could not create the %r docker network: %s",
            name,
            (created.stderr or created.stdout or "").strip()[:200],
        )
    except Exception:
        logger.exception("could not create the %r docker network", name)
    _NETWORK_FAILED_AT[name] = time.monotonic()
    return None


def ensure_isolated_network(*, run_fn: Any = None) -> str | None:
    """The isolated sandbox network, created if needed; ``None`` if unusable.

    A network that already exists under the name but lets containers talk to
    each other (created by hand, or by something else) is refused rather than
    used: putting sandboxes on it would report a guarantee docker is not
    enforcing.
    """
    name = _ensure_network(_ISOLATED_NETWORK, isolated=True, run_fn=run_fn)
    if name is None:
        return None
    if not network_isolated(name, run_fn=run_fn):
        logger.error(
            "the %r docker network exists but does not isolate containers "
            "(%s is not false); remove it so Olano can re-create it, or set %s=0",
            name,
            _ICC_OPTION,
            SANDBOX_ISOLATION_ENV,
        )
        _NETWORK_FAILED_AT[name] = time.monotonic()
        return None
    return name


def default_lane(*, run_fn: Any = None) -> str:
    """The network a sandbox asking for ``bridge`` joins when nothing overrides it.

    The isolated network while isolation is on and docker can provide it, else
    the shared default bridge (logged once: a box that silently lost the
    isolation it was promised is the failure this exists to prevent).
    """
    global _ISOLATION_WARNED
    if isolation_enabled():
        name = ensure_isolated_network(run_fn=run_fn)
        if name is not None:
            return name
        if not _ISOLATION_WARNED:
            logger.warning(
                "sandbox isolation is unavailable on this box: sandboxes share the "
                "default bridge, so a server one agent runs is reachable from every "
                "other agent's sandbox"
            )
            _ISOLATION_WARNED = True
    return "bridge"


def network_pinned_by_operator() -> bool:
    """``True`` when ``LIONCLAW_SANDBOX_NETWORK`` names the lane — no escalation then."""
    return bool(os.environ.get(SANDBOX_NETWORK_ENV, "").strip())


def resolve_bridge_network() -> str:
    """The docker network a sandbox asking for ``bridge`` actually joins.

    Operator env wins; then a remembered escalation (see the block above); then
    the default lane (:func:`default_lane`: the isolated network, or the shared
    bridge with isolation off). A remembered rescue network is **re-created if
    it has gone** (a `docker network rm`, a daemon reset): the marker must
    never turn into a `docker run` that fails for every agent with nothing to
    fall back to. If it cannot be brought back, the lane reverts to the default
    lane and says so.
    """
    override = os.environ.get(SANDBOX_NETWORK_ENV, "").strip()
    if override:
        return override
    remembered = _read_sticky_network()
    if not remembered or remembered in ("bridge", _ISOLATED_NETWORK):
        return default_lane()
    if remembered == _RESCUE_NETWORK and ensure_rescue_network() is None:
        lane = default_lane()
        logger.warning(
            "the remembered sandbox network %r no longer exists and could not be "
            "re-created, falling back to %r",
            remembered,
            lane,
        )
        return lane
    return remembered


def netcheck_enabled() -> bool:
    return os.environ.get(SANDBOX_NETCHECK_ENV, "").strip() not in {"0", "false", "no"}


# 9.5 s is the probe's own worst case (see _NETCHECK_SCRIPT); the rest is
# python start-up and the docker exec round trip.
_PROBE_EXEC_TIMEOUT = 20


def probe_container_network(container: str, *, run_fn: Any = None) -> tuple[str, str]:
    """``(state, detail)`` for one running container's path off the box.

    ``ok`` / ``stranded`` / ``unknown`` — see ``_NETCHECK_SCRIPT``. Never raises
    and never blocks longer than the exec timeout: a probe that cannot answer
    must cost nothing but a log line.
    """
    runner = run_fn or _run
    try:
        proc = runner(
            ["docker", "exec", container, "python3", "-c", _NETCHECK_SCRIPT],
            timeout=_PROBE_EXEC_TIMEOUT,
        )
    except Exception as exc:  # docker missing, exec timeout, …
        return _NET_UNKNOWN, f"probe failed: {exc}"
    if proc.returncode != 0:
        return _NET_UNKNOWN, (proc.stderr or proc.stdout or "").strip()[:200]
    head, _, detail = (proc.stdout or "").strip().partition(" ")
    if head in {_NET_OK, _NET_STRANDED}:
        return head, detail.strip()
    return _NET_UNKNOWN, (proc.stdout or "").strip()[:200]


_RESCUE_NETWORK_READY = False


def ensure_rescue_network(*, run_fn: Any = None) -> str | None:
    """Create (idempotently) the dedicated sandbox bridge, or None if we can't.

    A fresh docker network is a fresh bridge: docker installs its forward and
    masquerade rules at creation and picks a subnet that does not collide with
    the host's existing routes, which is exactly what a broken ``docker0``
    cannot offer any longer. Success is memoized for the process — this runs on
    every sandbox and stdio-MCP start once a box has escalated, and a network
    removed under a running runtime surfaces as the `docker run` failure it is.
    """
    global _RESCUE_NETWORK_READY
    if _RESCUE_NETWORK_READY:
        return _RESCUE_NETWORK
    runner = run_fn or _run
    try:
        proc = runner(["docker", "network", "inspect", _RESCUE_NETWORK], timeout=15)
        if proc.returncode == 0:
            _RESCUE_NETWORK_READY = True
            return _RESCUE_NETWORK
        argv = ["docker", "network", "create", "--driver", "bridge"]
        if isolation_enabled():
            # A repair must not cost the isolation the default lane gives.
            argv += ["--opt", f"{_ICC_OPTION}=false"]
        argv.append(_RESCUE_NETWORK)
        created = runner(argv, timeout=30)
        # A concurrent create (two agents booting) is a success, not a failure.
        if created.returncode == 0 or "already exists" in (created.stderr or "").lower():
            _RESCUE_NETWORK_READY = True
            return _RESCUE_NETWORK
        logger.error(
            "could not create the %r docker network: %s",
            _RESCUE_NETWORK,
            (created.stderr or created.stdout or "").strip()[:200],
        )
    except Exception:
        logger.exception("could not create the %r docker network", _RESCUE_NETWORK)
    return None


# A `docker run` that fails for one of these reasons failed at the NETWORK, so
# the other lane is the answer — exactly as it is for a container that starts
# and cannot reach its gateway. Anything else (a missing image, a bad mount, a
# name clash) must keep failing loudly: escalating past it would paper over an
# unrelated defect with a container on a different bridge.
#
# `exchange full` is the one worth knowing by name. A Linux bridge holds at
# most `BR_MAX_PORTS` = 1024 interfaces, and `br_add_if` returns `-EXFULL`
# ("Exchange full") when that table is exhausted — so
# `failed to create endpoint … adding interface vethX to bridge docker0
# failed: exchange full` means docker0 has 1024 ports in use, almost always
# leaked veths from containers whose cleanup never ran. Every later container
# then fails to get networking, and a container that raced the boundary can end
# up running with an eth0 whose host side is not a bridge port at all: an
# address and a default route, and no L2 — which is exactly the "stranded"
# shape the probe catches from the other side.
_NETWORK_CREATE_FAILURE_MARKERS = (
    "failed to create endpoint",
    "exchange full",
    "adding interface",
    "failed to set up container networking",
    # IPAM, not the port table: a /16 whose addresses or pools are used up.
    "no available ipv4 addresses",
    "non-overlapping ipv4 address pool",
    "address pools have been fully subnetted",
    # A stale endpoint docker still believes in; a different lane has none.
    "endpoint with name",
)

#: A remembered network that vanished between resolve and run. Docker puts the
#: name in the middle (`network olano-sandbox not found`), so this one cannot be
#: a substring — and a bare "not found" would swallow `No such image`.
_NETWORK_MISSING_RE = re.compile(r"network\s+\S+\s+not found", re.IGNORECASE)


def is_network_creation_failure(text: str) -> bool:
    """Did this ``docker run`` fail because of the container's network?"""
    low = (text or "").lower()
    if any(marker in low for marker in _NETWORK_CREATE_FAILURE_MARKERS):
        return True
    return bool(_NETWORK_MISSING_RE.search(low))


def _fallback_network_for(network: str) -> str | None:
    """The one other lane to try when *network* proves unusable, or None.

    The default lane (the isolated network, or ``bridge``) ⇄ the rescue
    network, in both directions: a box that escalated once and later lost the
    rescue bridge instead must be able to come back. Never when the operator
    pinned the lane, and never for ``none``/``host``.
    """
    if network_pinned_by_operator():
        return None
    if network in ("bridge", _ISOLATED_NETWORK):
        return ensure_rescue_network()
    if network == _RESCUE_NETWORK:
        return default_lane()
    return None


def hardened_run_args(
    *,
    network: str,
    memory: str,
    cpus: float,
    workspace_root: Path,
    extra_mounts: list[tuple[str, str, str]],
) -> list[str]:
    """The one definition of a hardened Olano sandbox `docker run` argv prefix.

    Shared by the agent backend (:meth:`OlanoDockerBackend._base_run_args`) and
    the ephemeral stdio-MCP containers (:func:`mcp_stdio_run_args`) so "hardened
    container" cannot mean two different things. Flags only — the caller appends
    lifecycle flags, the image, and the command.
    """
    args = [
        "docker",
        "run",
        f"--network={network}",
        f"--memory={memory}",
        f"--cpus={cpus}",
        # Hardening: non-root user (image ships a uid-1000 `sandbox` user),
        # drop all Linux capabilities, forbid privilege escalation, and cap
        # process count against fork bombs. The rootfs stays writable so the
        # customer can pip/npm install; the container holds only the
        # workspace — no product source, no host filesystem, no secrets.
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        # Cut the sandbox off from the cloud metadata service (a credential
        # theft vector on every major cloud). Host-internal services are
        # already unreachable except via published ports, which the sandbox
        # has none of. A stricter per-deployment egress firewall is a host-level
        # policy (documented in deploy/), not expressible in run flags.
        "--add-host=metadata.google.internal:127.0.0.1",
        "-v",
        f"{workspace_root.resolve()}:/workspace:rw",
        "-w",
        "/workspace",
    ]
    # ONE path namespace: the workspace is ALSO mounted at its host path, so
    # the paths the model actually receives — the prompt's "Workspace root:
    # <host path>", the security middleware's normalization of every file-tool
    # arg to a host-absolute path, deepagents' skills scanner being handed
    # host dirs — resolve inside the container exactly like they do in the
    # runtime. /workspace stays as the working dir and a stable alias (both
    # mounts are the same host directory, so writes cross instantly).
    # Without this, a sandboxed agent's every prompt-derived path is a
    # "path_not_found": the prompt says the host path, the container only
    # has /workspace.
    ws = str(workspace_root.resolve())
    taken = {"/workspace", *(container for _h, container, _m in extra_mounts)}
    if ws not in taken:
        args += ["-v", f"{ws}:{ws}:rw"]
    for key, val in _SHELL_ENV_DEFAULTS.items():
        args += ["-e", f"{key}={val}"]
    for key, val in _TOOL_LAYER_ENV.items():
        args += ["-e", f"{key}={val}"]
    for host_path, container_path, mode in extra_mounts:
        args += ["-v", f"{host_path}:{container_path}:{mode}"]
    return args


# Labels stamped on ephemeral containers so `docker ps` output is attributable
# and an orphan sweep has something to key on. The stdio-MCP family keeps the
# key it has always had (`olano.mcp=1` + `olano.mcp.agent=<slug>`); connected
# jobs (a `git clone`/`push` holding a credential the agent's own sandbox never
# sees — see ``core/job_runner.py``) carry `olano.job=<label>` +
# `olano.job.agent=<slug>`.
_MCP_CONTAINER_LABEL = "olano.mcp"
_JOB_CONTAINER_LABEL = "olano.job"
_MCP_LABEL_FAMILY = "mcp"


def ephemeral_run_args(
    *,
    label: str,
    agent_name: str,
    workspace_root: Path,
    secret_names: Sequence[str],
    image: str | None = None,
    interactive: bool,
    extra_mounts_ro: bool = False,
    container_name: str | None = None,
) -> list[str]:
    """`docker run` argv for one throwaway hardened container, minus the command.

    Returns ``["docker", "run", …flags…, <image>]``; the caller appends
    ``["sh", "-c", <launch line>]`` and spawns it. The one definition shared by
    the stdio-MCP sandbox (:func:`mcp_stdio_run_args`, ``label="mcp"``,
    ``interactive=True``) and connected jobs (``core/job_runner.py``, any other
    label, ``interactive=False``), so "ephemeral container" cannot mean two
    different things. Three properties are load-bearing:

    - ``--rm`` + ``--init`` (+ ``-i`` when *interactive*): an interactive
      container's stdio IS the caller's pipe (MCP JSON-RPC) and stdin-EOF ends
      it; a job just runs to completion. ``--init`` reaps/forwards signals, and
      the daemon removes the container on exit. Non-tty ``docker run`` also
      sig-proxies SIGTERM, so terminating the CLI child terminates the process.
    - Secrets ride as **name-only** ``-e NAME`` flags — the docker CLI resolves
      each from its own process env and ships the value inside the API body
      over the socket. Values never appear on a host argv.
    - The shared tool layer is mounted (``rw`` for MCP so npx/uvx/pip caches
      stay warm; ``ro`` for a job, which must never write it) at the same
      mount every agent sandbox uses.

    *container_name* (jobs only) gives the caller a handle to ``docker rm -f``
    when its own wait times out — a SIGKILLed CLI child cannot sig-proxy.
    Resource caps default to the historical per-sandbox floor and are
    deliberately per-container, not per-agent: every one starts from a clean
    image, so a misbehaving process cannot persist anything into a long-lived
    container.
    """
    from olano.config.backend_config import _default_sandbox_image
    from olano.config.home import get_olano_paths

    memory = os.environ.get("LIONCLAW_MCP_SANDBOX_MEMORY", "").strip() or _DEFAULT_MEMORY
    cpus_raw = os.environ.get("LIONCLAW_MCP_SANDBOX_CPUS", "").strip()
    try:
        cpus = float(cpus_raw) if cpus_raw else _DEFAULT_CPUS
    except ValueError:
        cpus = _DEFAULT_CPUS
    tool_dir = get_olano_paths().root / "sandbox" / "tools"
    args = hardened_run_args(
        # Same lane as the agent sandboxes — an ephemeral container is created
        # fresh so it can never be stranded, but if the box escalated off a
        # broken default bridge, every npx-launched stdio MCP server has to
        # follow it there or it resolves nothing.
        network=resolve_bridge_network(),
        memory=memory,
        cpus=cpus,
        workspace_root=workspace_root,
        extra_mounts=[(str(tool_dir), _TOOL_LAYER_DIR, "ro" if extra_mounts_ro else "rw")],
    )
    args.append("--rm")
    if interactive:
        args.append("-i")
    args.append("--init")
    if container_name:
        args += ["--name", container_name]
    slug = _agent_slug(agent_name)
    if label == _MCP_LABEL_FAMILY:
        args += [
            f"--label={_MCP_CONTAINER_LABEL}=1",
            f"--label={_MCP_CONTAINER_LABEL}.agent={slug}",
        ]
    else:
        args += [
            f"--label={_JOB_CONTAINER_LABEL}={_agent_slug(label)}",
            f"--label={_JOB_CONTAINER_LABEL}.agent={slug}",
        ]
    # Name-only: each name is a single argv element, so a hostile name can
    # never become a docker flag — at worst docker rejects it loudly.
    for name in sorted(n for n in secret_names if n):
        args += ["-e", name]
    args.append((image or "").strip() or _default_sandbox_image())
    return args


def mcp_stdio_run_args(
    *,
    agent_name: str,
    workspace_root: Path,
    secret_names: Sequence[str],
    image: str | None = None,
) -> list[str]:
    """`docker run` argv for one ephemeral stdio-MCP session, minus the command.

    A thin wrapper over :func:`ephemeral_run_args` (``label="mcp"``,
    ``interactive=True``) whose argv is pinned byte-for-byte by the stdio
    sandbox tests — the caller appends ``["sh", "-c",
    mcp_stdio_shell_script(...)]`` and spawns it as the MCP stdio child.
    """
    return ephemeral_run_args(
        label=_MCP_LABEL_FAMILY,
        agent_name=agent_name,
        workspace_root=workspace_root,
        secret_names=secret_names,
        image=image,
        interactive=True,
    )


def mcp_stdio_shell_script(command: str, args: Sequence[str]) -> str:
    """The in-container launch line for a sandboxed stdio MCP server.

    ``sh -c``, never ``sh -lc`` — a login shell prints its profile into stdout,
    which here is the MCP JSON-RPC stream. ``exec`` keeps the server as the
    shell's own PID so container/signal lifecycle applies to it directly, and
    the PATH prefix makes the shared tool layer's npx/uvx/pip shims win (and
    the interpreter shims, appended last, cover a `python` the image lacks).
    """
    launch = shlex.join([command, *list(args)])
    return f"{shell_path_export()}; exec {launch}"


# ── ephemeral container reaper ────────────────────────────────────────────────
#
# Every stdio-MCP session and every connected job above is a `docker run --rm`
# whose lifetime is meant to be its caller's: the mcp SDK closes the child's
# stdin, waits two seconds, SIGTERMs the docker CLI (which proxies it into the
# container), and two seconds later SIGKILLs the CLI. A SIGKILLed CLI proxies
# nothing, so on a daemon too busy to deliver the SIGTERM in time — or with a
# server that shuts down slowly, or ignores stdin-EOF — the container keeps
# running with nobody attached and `--rm` never fires. Each such miss is one
# bridge port, forever. The update agent's census on one managed box (2026-09)
# read `docker0=1012/1024 orphans=0 veth=1062 ctrs=1503` on a box with twelve
# agents: docker accounted for every port, i.e. a thousand live containers, and
# the bridge was twelve ports from refusing every new sandbox ("exchange full",
# `BR_MAX_PORTS` = 1024). A persistent sandbox is found by name and reused, so
# an AGENT's cannot leak this way — but a cognition worker's could: each
# background run minted a unique agent name (`<parent>__cog__<label>__<hex>`),
# and the sandbox keyed on it was a new persistent container per run that
# nothing ever stopped. The same box read `top=br-…:959 SATURATED … sbx=960`
# a month later, with the stdio-MCP family at zero. Workers now share their
# parent's container (`builder.sandbox_owner`), so a sandbox whose NAME carries
# a worker marker is one a previous release left behind: the third family.
#
# The reaper is the bound: at boot and on every MCP-health tick, a container
# carrying one of the ephemeral labels and older than any session this process
# could legitimately still hold is removed. Age, never the label alone, so a
# second olano process sharing the daemon (a dev box running a one-shot
# `olano -n` beside `olano serve`) keeps its in-flight session; a container docker created
# but never started is a husk from its first second and gets a short grace.
# Best-effort throughout: it never raises, and a `docker rm -f` of a container
# holds nothing durable (workspace and tool layer are host mounts).
EPHEMERAL_MAX_AGE_ENV = "LIONCLAW_EPHEMERAL_CONTAINER_MAX_AGE"
#: Seconds. Boot connects are bounded at ~35 s and a tool call runs for
#: minutes; three hours is generous to a long scrape and still bounds a leak
#: at a few containers rather than a bridge.
EPHEMERAL_MAX_AGE_DEFAULT = 3 * 3600
#: A `created` container (a `docker run` that failed at the network, or was
#: never started) is only ever legitimate for the instant between create and
#: start of a launch in progress.
_EPHEMERAL_CREATED_GRACE = 600
#: Name fragments only a worker's sandbox carries: `_persistent_container_name`
#: slugs the agent name and both markers survive it (underscores are kept).
#: Mirrors `analytics/threadcost/resolver.py`, which splits on the same marker.
_WORKER_NAME_MARKERS: tuple[str, ...] = ("__cog__", "__sub__")
#: `(family, docker ps filters)`; several filters AND together, and a family may
#: appear more than once (one row per worker marker) — the report counts it once.
_EPHEMERAL_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mcp", (f"label={_MCP_CONTAINER_LABEL}=1",)),
    ("job", (f"label={_JOB_CONTAINER_LABEL}",)),
    *(("worker", (f"label={_LABEL_KEY}", f"name={marker}")) for marker in _WORKER_NAME_MARKERS),
)
_REAP_CHUNK = 50
_REAP_LOCK = threading.Lock()
_LAST_REAP: dict[str, Any] = {}
_CREATED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def ephemeral_max_age() -> int:
    """The age past which an ephemeral container is nobody's (seconds, ≥ 60)."""
    raw = os.environ.get(EPHEMERAL_MAX_AGE_ENV, "").strip()
    try:
        value = int(raw) if raw else EPHEMERAL_MAX_AGE_DEFAULT
    except ValueError:
        return EPHEMERAL_MAX_AGE_DEFAULT
    return max(60, value)


def _parse_created(value: str) -> float | None:
    """`{{.Created}}` (RFC 3339 with nanoseconds, always UTC) → epoch seconds."""
    m = _CREATED_RE.match(value.strip())
    if not m:
        return None
    return (
        datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    )


def last_reap_report() -> dict[str, Any]:
    """What the last completed reap found and did (``{}`` before the first)."""
    return json.loads(json.dumps(_LAST_REAP)) if _LAST_REAP else {}


def reap_ephemeral_containers(
    *, max_age_s: int | None = None, now: float | None = None, reason: str = ""
) -> dict[str, Any]:
    """Remove ephemeral containers no live session can still own.

    Returns a report — ``checked`` (containers carrying an ephemeral label),
    ``stale`` (older than the bound), ``reaped`` (actually removed, by
    ``families``), ``oldest_age_s`` and ``errors`` — and memoizes it for
    :func:`last_reap_report`. A reap already running in another thread is not
    joined and not duplicated: the call returns a report saying so and leaves
    the memo alone.
    """
    max_age = ephemeral_max_age() if max_age_s is None else max(0, int(max_age_s))
    at = time.time() if now is None else now
    report: dict[str, Any] = {
        "at": at,
        "reason": reason,
        "max_age_s": max_age,
        "checked": 0,
        "stale": 0,
        "reaped": 0,
        "families": {family: 0 for family, _ in _EPHEMERAL_FAMILIES},
        "oldest_age_s": None,
        "errors": [],
    }
    if shutil.which("docker") is None:
        report["errors"].append("docker not on PATH")
        return _record_reap(report)
    if not _REAP_LOCK.acquire(blocking=False):
        report["errors"].append("a reap is already running")
        return report
    try:
        return _record_reap(_reap_locked(report, max_age=max_age, now=at))
    finally:
        _REAP_LOCK.release()


def _reap_locked(report: dict[str, Any], *, max_age: int, now: float) -> dict[str, Any]:
    family_of: dict[str, str] = {}
    for family, filters in _EPHEMERAL_FAMILIES:
        cmd = ["docker", "ps", "-aq", "--no-trunc"]
        for flt in filters:
            cmd += ["--filter", flt]
        try:
            listed = _run(cmd, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            report["errors"].append(f"docker ps ({family}): {exc}")
            continue
        if listed.returncode != 0:
            detail = (listed.stderr or listed.stdout).strip()[:200]
            report["errors"].append(f"docker ps ({family}): {detail}")
            continue
        for cid in listed.stdout.split():
            family_of.setdefault(cid, family)
    report["checked"] = len(family_of)
    if not family_of:
        return report

    # One inspect for the lot: `docker ps` formats CreatedAt for humans, while
    # `.Created` is RFC 3339. A container that vanished between the two calls
    # costs an exit status, not the others' rows — parse whatever came back.
    ids = sorted(family_of)
    try:
        inspected = _run(
            ["docker", "inspect", "--format", "{{.Id}}\t{{.State.Status}}\t{{.Created}}", *ids],
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report["errors"].append(f"docker inspect: {exc}")
        return report
    if inspected.returncode != 0 and not inspected.stdout.strip():
        report["errors"].append(
            f"docker inspect: {(inspected.stderr or '').strip()[:200] or 'no output'}"
        )
        return report

    stale: list[str] = []
    oldest = 0.0
    for line in inspected.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        cid, status, created = parts
        created_at = _parse_created(created)
        if created_at is None:
            continue
        age = max(0.0, now - created_at)
        oldest = max(oldest, age)
        limit = _EPHEMERAL_CREATED_GRACE if status == "created" else max_age
        if age > limit:
            stale.append(cid)
    report["oldest_age_s"] = round(oldest)
    report["stale"] = len(stale)

    # `docker rm -f` prints each id it removed and carries on past the ones it
    # could not; small chunks keep one slow container from timing out the rest.
    for start in range(0, len(stale), _REAP_CHUNK):
        chunk = stale[start : start + _REAP_CHUNK]
        try:
            removed = _run(["docker", "rm", "-f", *chunk], timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            report["errors"].append(f"docker rm: {exc}")
            continue
        done = set(removed.stdout.split())
        for cid in chunk:
            if cid in done:
                report["reaped"] += 1
                report["families"][family_of[cid]] += 1
        if removed.returncode != 0:
            report["errors"].append(f"docker rm: {(removed.stderr or '').strip()[:200]}")
    return report


def _record_reap(report: dict[str, Any]) -> dict[str, Any]:
    _LAST_REAP.clear()
    _LAST_REAP.update(json.loads(json.dumps(report)))
    if report["reaped"] or report["errors"]:
        logger.info(
            "ephemeral container reap (%s): %d checked, %d stale, %d removed (%s)%s",
            report["reason"] or "unscheduled",
            report["checked"],
            report["stale"],
            report["reaped"],
            ", ".join(f"{k}={v}" for k, v in report["families"].items()),
            f"; errors: {'; '.join(report['errors'])}" if report["errors"] else "",
        )
    else:
        logger.debug(
            "ephemeral container reap (%s): %d checked, none stale",
            report["reason"] or "unscheduled",
            report["checked"],
        )
    return report


class OlanoDockerBackend(BaseSandbox):
    """Docker sandbox backend for Olano agents.

    A deepagents ``BaseSandbox``: it supplies ``execute``/``upload_files``/
    ``download_files``/``id`` and inherits every file operation the agent's
    tools call (``ls``, ``read``, ``write``, ``edit``, ``glob``, ``grep``,
    ``delete``, and the async variants).
    """

    def __init__(self, config: OlanoDockerBackendConfig) -> None:
        self._config = config
        self._container_id: str | None = None
        self._container_name: str | None = None
        self._agent_name: str | None = None
        self._workspace_root: Path | None = None
        self._extra_mounts: list[tuple[str, str, str]] = []  # (host_path, container_path, mode)
        self._network: str | None = None  # resolved lazily; see _effective_network
        # This agent's own egress lists, read once per backend instance. An
        # edit in the dashboard reloads the agent, which builds a new backend,
        # so a one-shot read cannot go stale; the DEPLOYMENT half is re-read on
        # every proxied connection, so a Config-page save applies immediately.
        self._agent_egress: tuple[list[str], list[str]] | None = None
        #: Whether this container currently carries its netfilter rules. ``None``
        #: = not yet decided; kept in step by ``_sync_egress_confinement``.
        self._egress_confined: bool | None = None
        self._network_report: dict[str, Any] = {
            "network": config.network,
            "health": "unchecked",
            "rescued": False,
            "detail": "",
        }

    @property
    def workspace_root(self) -> Path | None:
        """The host workspace bind-mounted into this container (``None`` before ``start``)."""
        return self._workspace_root

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(
        self,
        *,
        agent_name: str,
        workspace_root: Path,
        shared_mounts: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """Start or attach to the container.

        shared_mounts: list of (host_path, container_path, mode) tuples for shared paths.
        """
        _require_docker()
        self._agent_name = agent_name
        self._workspace_root = workspace_root
        self._extra_mounts = shared_mounts or []

        if self._config.persistent:
            self._start_persistent()
        else:
            self._start_ephemeral()

    def stop(self) -> None:
        """Stop and optionally snapshot the container."""
        if self._container_id is None and self._container_name is None:
            return
        # Drop the netfilter rules first: they are keyed on an address docker
        # reuses, so a rule outliving its container eventually confines somebody
        # else's to a proxy that is not listening for them. `sweep_stale` is the
        # backstop for a runtime that died before reaching this line.
        if self._egress_confined and self._agent_name:
            try:
                from olano.security import egress_firewall
                from olano.security.egress_proxy import stop_proxy

                egress_firewall.clear_for(self._agent_name, self._container_ip())
                stop_proxy(self._agent_name)
            except Exception:
                logger.exception("could not clear egress rules for %r", self._agent_name)
            self._egress_confined = False
        if self._config.persistent:
            self._stop_persistent()
        else:
            self._stop_ephemeral()

    def _effective_memory(self) -> str:
        """The container's RAM ceiling.

        ``None`` means nobody decided — the builder normally resolves a
        host-proportional value first (see ``core.host_resources``), so this
        fallback covers a directly-constructed backend and keeps the historical
        cap for anything that predates the resolver.
        """
        return self._config.memory or _DEFAULT_MEMORY

    def _effective_cpus(self) -> float:
        """The container's CPU ceiling (see :meth:`_effective_memory`)."""
        return _DEFAULT_CPUS if self._config.cpus is None else self._config.cpus

    def _effective_network(self) -> str:
        """The docker network this container joins.

        ``none``/``host`` mean exactly what they say; ``bridge`` is the one
        value that resolves, because it is the lane a stranded or broken bridge
        can be moved off (see ``resolve_bridge_network``). Memoized for the
        life of the backend so the run args and the container signature can
        never be computed against two different answers.
        """
        if self._network is None:
            configured = self._config.network
            self._network = resolve_bridge_network() if configured == "bridge" else configured
        return self._network

    def network_report(self) -> dict[str, Any]:
        """What the last reachability probe found, for the sandbox status entry.

        ``health`` is ``ok``/``stranded``/``unknown``/``unchecked``; ``rescued``
        says this container had to be rebuilt to get a working path off the box,
        which is the bit worth surfacing on a deployment nobody can SSH into.
        ``isolated`` says whether other containers can open connections to this
        sandbox (``False`` means they can, see the isolation block above).
        """
        report = dict(self._network_report)
        if self._network is not None:
            report["isolated"] = network_isolated(self._network)
        return report

    @property
    def isolated(self) -> bool:
        """Whether docker blocks other containers from reaching this sandbox."""
        return network_isolated(self._effective_network())

    def _base_run_args(self) -> list[str]:
        assert self._workspace_root is not None
        return hardened_run_args(
            network=self._effective_network(),
            memory=self._effective_memory(),
            cpus=self._effective_cpus(),
            workspace_root=self._workspace_root,
            extra_mounts=self._extra_mounts,
        )

    def _start_ephemeral(self) -> None:
        args = self._base_run_args()
        args += ["--rm", "-d", self._config.image, "sh", "-lc", _LIFECYCLE_CMD]
        result = _run(args, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start ephemeral Docker container: {result.stderr or result.stdout}"
            )
        self._container_id = result.stdout.strip()

    def _resolve_egress_policy(self) -> Any:
        """This sandbox's effective policy: deployment lists ∪ this agent's.

        Called by the proxy on every connection, so a Config-page save to the
        deployment lists bites without a reload. Never raises — the proxy turns
        a failure into a refusal, which is the right posture there but must be
        its decision, not an exception escaping into the relay loop.
        """
        from olano.security.egress import (
            EgressPolicy,
            agent_egress_lists,
            resolve_egress_policy,
        )

        if self._agent_egress is None:
            lists: tuple[list[str], list[str]] = ([], [])
            if self._agent_name:
                try:
                    from olano.config.loader import load_agent_config

                    lists = agent_egress_lists(load_agent_config(self._agent_name))
                except Exception as exc:
                    logger.debug(
                        "could not read egress lists for agent %r: %s", self._agent_name, exc
                    )
            self._agent_egress = lists

        try:
            # `agent=` hands over the lists we already hold, so the one
            # resolution rule still applies (deny is the union, the agent's
            # allow replaces the deployment's) without re-parsing agent.json5
            # on every shell command. The deployment half IS re-read, which is
            # what makes a Config-page save apply with no reload.
            return resolve_egress_policy(agent=self._agent_egress)
        except Exception:
            logger.exception("could not resolve the egress policy for agent %r", self._agent_name)
            return EgressPolicy()

    def _container_ip(self) -> str | None:
        """This container's address on its network, or ``None``."""
        container = self._container_id or self._container_name
        if not container:
            return None
        result = _run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container,
            ],
            timeout=15,
        )
        ip = (result.stdout or "").strip()
        return ip or None

    def _sync_egress_confinement(self, governed: bool) -> None:
        """Install or remove this container's netfilter rules on a state change.

        Called from the per-exec path but does work only when the answer
        *changed* — the memo is an in-memory bool, so a governed sandbox costs
        one comparison per command rather than two ``iptables -C`` round trips.
        The state can change without a restart because the deployment lists are
        re-read on every proxied connection, so an operator turning the policy
        on (or back off) in Config reaches a running container.
        """
        if self._egress_confined is governed or not self._agent_name:
            return
        from olano.security import egress_firewall

        ip = self._container_ip()
        if not ip:
            return
        if governed:
            egress_firewall.apply_for(self._agent_name, ip)
        else:
            egress_firewall.clear_for(self._agent_name, ip)
        self._egress_confined = governed

    def _egress_proxy_env(self) -> dict[str, str]:
        """Proxy env for one `docker exec`, or ``{}`` when this agent is open.

        Also the point where the container's confinement is kept in step with
        the policy — the two halves belong together: the environment says
        *where* to go, the rules make it the only way out, and a sandbox that
        had one without the other would be either unenforced or cut off.
        """
        if not self._agent_name:
            return {}
        try:
            from olano.security.egress_proxy import proxy_env_for_agent

            env = proxy_env_for_agent(
                self._agent_name, self._effective_network(), self._resolve_egress_policy
            )
            # `env` is empty both when the agent is open and when the proxy
            # could not be started. Confining a container whose proxy is not
            # listening would cut it off entirely, so the rules follow the
            # environment rather than the policy.
            self._sync_egress_confinement(bool(env))
            return env
        except Exception:
            # A shell command must never fail because the proxy could not be
            # set up; the runtime-side guard still governs the tools, and the
            # warning inside proxy_env_for_agent names the gap.
            logger.exception("could not prepare egress proxy env for %r", self._agent_name)
            return {}

    def _signature_for(self, network: str) -> str:
        assert self._workspace_root is not None
        return _container_signature(
            image=self._config.image,
            network=network,
            workspace_root=self._workspace_root,
            extra_mounts=self._extra_mounts,
            memory=self._effective_memory(),
            cpus=self._effective_cpus(),
        )

    def _start_persistent(self) -> None:
        assert self._agent_name is not None and self._workspace_root is not None
        name = _persistent_container_name(self._agent_name, self._workspace_root)
        self._container_name = name
        network = self._effective_network()
        sig = self._signature_for(network)

        # Inspect existing container
        inspect = _run(["docker", "inspect", name], timeout=10)
        if inspect.returncode == 0:
            try:
                payload = json.loads(inspect.stdout)[0]
                existing_sig = (payload.get("Config", {}).get("Labels") or {}).get(_LABEL_KEY)
                is_running = payload.get("State", {}).get("Status") == "running"
                if existing_sig == sig:
                    if not is_running:
                        _run(["docker", "start", name], timeout=15)
                    # A matching signature says the container was built to the
                    # right recipe — not that it can still reach anything. A
                    # daemon restart can strand a RUNNING container's veth, and
                    # reusing it is how a box ends up with every `npx` dying on
                    # DNS while the signature says "nothing changed".
                    if self._network_is_usable(name):
                        return
                    logger.warning(
                        "sandbox container %r matches its signature but cannot reach "
                        "its gateway (%s) — rebuilding it to re-plumb the network",
                        name,
                        self._network_report.get("detail") or "no detail",
                    )
                # Stale container, or a healthy recipe that lost its network.
                _run(["docker", "rm", "-f", name], timeout=20)
            except Exception:
                _run(["docker", "rm", "-f", name], timeout=20)

        # Two ways the lane can be unusable, and BOTH have to reach the
        # escalation below. The container can start and not reach its gateway
        # (a veth no longer plumbed into the bridge), or docker can refuse to
        # give it an endpoint at all — `exchange full` on a docker0 whose 1024
        # port slots are used up. The second one used to raise straight out of
        # here, so a box that had run out of bridge ports failed every agent
        # closed with a repair sitting unused ten lines below.
        create_error = self._create_persistent(name, network, sig)
        if create_error is None:
            if self._network_is_usable(name):
                return
            unusable = f"cannot reach its gateway on {network!r}"
        elif is_network_creation_failure(create_error):
            unusable = f"docker refused an endpoint on {network!r}"
            self._network_report = {
                "network": network,
                "health": _NET_STRANDED,
                "rescued": self._network_report.get("rescued", False),
                "detail": create_error[:200],
            }
        else:
            # Not a networking failure — a missing image, a bad mount, a name
            # clash. Fail closed where it always did rather than retrying it
            # on another bridge and reporting a network problem.
            raise RuntimeError(
                f"Failed to start persistent Docker container '{name}' on network "
                f"'{network}': {create_error}"
            )

        # A container built moments ago cannot have been stranded by a daemon
        # restart, so the bridge itself is the problem. One move to the other
        # lane, then we stop and say so: a repair that loops is worse than one
        # that reports.
        rescue = _fallback_network_for(network)
        if rescue is None:
            logger.error(
                "sandbox container %r %s and no other lane is available — the "
                "agent's shell, npx/pip installs and every stdio MCP server "
                "will fail until the host's docker networking is repaired%s",
                name,
                unusable,
                f": {create_error}" if create_error else "",
            )
            if create_error is not None:
                # Nothing is running: the caller must fail closed to a no-shell
                # filesystem backend rather than register a dead sandbox.
                raise RuntimeError(
                    f"Failed to start persistent Docker container '{name}' on network "
                    f"'{network}': {create_error}"
                )
            return
        logger.warning(
            "sandbox network %r is unusable for %r (%s) — moving this sandbox onto %r",
            network,
            name,
            unusable,
            rescue,
        )
        _run(["docker", "rm", "-f", name], timeout=20)
        self._network = rescue
        rescue_error = self._create_persistent(name, rescue, self._signature_for(rescue))
        if rescue_error is None and self._network_is_usable(name):
            self._network_report["rescued"] = True
            # Sticky: the next boot must compute its signature with this
            # network, or it would rebuild the container on every start.
            _remember_sticky_network(rescue)
            logger.warning(
                "sandbox %r now runs on the %r docker network; set %s to pin a "
                "lane once the host's networking is repaired",
                name,
                rescue,
                SANDBOX_NETWORK_ENV,
            )
            return
        if rescue_error is not None:
            self._network_report = {
                "network": rescue,
                "health": _NET_STRANDED,
                "rescued": self._network_report.get("rescued", False),
                "detail": rescue_error[:200],
            }
            raise RuntimeError(
                f"Failed to start persistent Docker container '{name}' on either "
                f"'{network}' ({create_error or unusable}) or '{rescue}' ({rescue_error}) "
                "— the host's docker networking needs repair"
            )
        logger.error(
            "sandbox container %r cannot reach its gateway on %r either — the "
            "host's docker networking (ip_forward, the FORWARD/DOCKER-USER "
            "chains, NAT, or a bridge whose 1024 port slots are exhausted) "
            "needs repair; agent shell installs and stdio MCP servers will "
            "keep failing to resolve names",
            name,
            rescue,
        )

    def _create_persistent(self, name: str, network: str, sig: str) -> str | None:
        """Create the container. Returns docker's error text, or None on success.

        It returns rather than raises because the caller has a repair for one
        class of failure (see ``is_network_creation_failure``) and must decide;
        every other failure it re-raises unchanged.
        """
        args = self._base_run_args()
        args += [
            "-d",
            "--name",
            name,
            "--label",
            f"{_LABEL_KEY}={sig}",
            self._config.image,
            "sh",
            "-lc",
            _LIFECYCLE_CMD,
        ]
        result = _run(args, timeout=30)
        if result.returncode != 0:
            return (result.stderr or result.stdout or "").strip() or "docker run failed"
        return None

    def _network_is_usable(self, name: str) -> bool:
        """Can this container still reach the world? Inconclusive counts as yes.

        Only a definite ``stranded`` triggers a rebuild — an unreadable
        ``/proc``, a missing ``python3`` or a docker that would not answer must
        never cost an agent its installed packages.
        """
        if not netcheck_enabled():
            self._network_report = {
                "network": self._effective_network(),
                "health": "unchecked",
                "rescued": self._network_report.get("rescued", False),
                "detail": f"{SANDBOX_NETCHECK_ENV} disables the probe",
            }
            return True
        state, detail = probe_container_network(name)
        self._network_report = {
            "network": self._effective_network(),
            "health": state,
            "rescued": self._network_report.get("rescued", False),
            "detail": detail,
        }
        if state == _NET_UNKNOWN:
            logger.info(
                "sandbox container %r network probe was inconclusive (%s) — treating it as healthy",
                name,
                detail or "no detail",
            )
        return state != _NET_STRANDED

    def _stop_ephemeral(self) -> None:
        if self._container_id:
            _run(["docker", "stop", self._container_id], timeout=15)
            self._container_id = None

    def _stop_persistent(self) -> None:
        if self._container_name and self._config.snapshot_on_stop:
            image_tag = f"{self._config.image}-snapshot"
            _run(["docker", "commit", self._container_name, image_tag], timeout=60)

    def container_ref(self) -> str | None:
        """The docker container name/id this backend is bound to, or None.

        Used by the dashboard terminal (``runtime/terminal.py``) and the chat
        ``!`` shell (``runtime/api.py``) to reach the agent's live sandbox via
        ``docker exec`` under a managed posture, instead of a host shell in the
        runtime container.
        """
        return self._container_id or self._container_name

    @property
    def id(self) -> str:
        """Stable identifier for this sandbox (deepagents SandboxBackendProtocol)."""
        return self.container_ref() or "olano-docker:unstarted"

    # ── command execution ──────────────────────────────────────────────────────

    def _shell_env_prefix(self) -> str:
        """The ``export …; PATH=…`` prefix every command in this sandbox runs under."""
        # Outbound access policy for the shell. Injected per exec, not baked
        # into `docker run`, so it stays out of `_container_signature`: a
        # changed policy reaches the next command instead of recreating the
        # container. Empty for an agent with no lists, which is the default and
        # means the sandbox behaves exactly as it always has.
        env_items = {**_SHELL_ENV_DEFAULTS, **_TOOL_LAYER_ENV, **self._egress_proxy_env()}
        env_prefix = "; ".join(f"export {k}={shlex.quote(v)}" for k, v in env_items.items())
        # PATH must expand in-shell: tool layer first, the image's own PATH,
        # then the interpreter shims last (see interpreter_shims.py — they
        # must never shadow a real `python`).
        return f"{env_prefix}; {shell_path_export()}"

    def exec_argv(
        self, command: str, *, interactive: bool = False, container: str | None = None
    ) -> list[str]:
        """The ``docker exec`` argv that runs ``command`` in this sandbox.

        The one definition of "a command in the agent's sandbox": the same
        environment prefix and shell as every agent command, for callers that
        need the process itself rather than its captured output (the page
        supervisor holds a long-lived ``-i`` exec as a page server's keeper and
        streams a build's output into a log). Raises ``RuntimeError`` when the
        sandbox has not been started.
        """
        target = container or self._container_id or self._container_name
        if target is None:
            raise RuntimeError(
                "OlanoDockerBackend.start() has not been called: no sandbox container."
            )
        argv = ["docker", "exec"]
        if interactive:
            argv.append("-i")  # keep stdin open so the payload reaches the child
        # `sh -c`, NOT `sh -lc`. deepagents' file operations are server-side
        # `python3` scripts whose single line of JSON we parse; one echo from
        # /etc/profile, /etc/profile.d/*.sh or ~/.profile prepends itself to that
        # line and silently breaks every read_file / glob / write / edit. A login
        # shell buys nothing here — the export prefix above sets the tool-layer
        # env and PATH deterministically, and `docker exec` already inherits the
        # image's own ENV. (The interactive Terminal still opens `sh -l`: a real
        # TTY should honour the user's profile, and nothing parses its output.)
        argv += [target, "sh", "-c", f"{self._shell_env_prefix()}; {command}"]
        return argv

    def _exec(
        self,
        command: str,
        timeout: int | None = None,
        stdin: str | None = None,
    ) -> SandboxRun:
        """Run a command inside the container.

        Never raises for an unstarted backend, a missing container or a timeout:
        those come back as a non-zero `SandboxRun` so deepagents' file
        operations degrade to a normal error result instead of tearing down the
        agent graph mid-turn.
        """
        container = self._container_id or self._container_name
        if container is None:
            return SandboxRun(
                "",
                "OlanoDockerBackend.start() has not been called — no sandbox container.",
                _EXIT_UNAVAILABLE,
            )

        argv = self.exec_argv(command, interactive=stdin is not None, container=container)

        # None → the backend default; 0 → no timeout (the protocol blesses that
        # spelling for backends that can run unbounded).
        effective_timeout = _DEFAULT_EXEC_TIMEOUT if timeout is None else timeout
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=effective_timeout or None,
                input=stdin,
            )
        except subprocess.TimeoutExpired as exc:
            partial_out = exc.stdout or ""
            partial_err = exc.stderr or ""
            if isinstance(partial_out, bytes):
                partial_out = partial_out.decode("utf-8", errors="replace")
            if isinstance(partial_err, bytes):
                partial_err = partial_err.decode("utf-8", errors="replace")
            return SandboxRun(
                partial_out,
                (partial_err + f"\nCommand timed out after {effective_timeout}s").strip(),
                _EXIT_TIMEOUT,
            )
        except OSError as exc:  # docker CLI missing / not executable
            return SandboxRun("", f"Could not run docker exec: {exc}", _EXIT_UNAVAILABLE)
        return SandboxRun(result.stdout, result.stderr, result.returncode)

    def run(self, command: str, *, timeout: int | None = None) -> SandboxRun:
        """Run ``command`` in the sandbox, keeping stdout and stderr apart.

        Olano-internal counterpart to :meth:`execute` (which must return a
        single combined string to satisfy deepagents). Used by the security
        middleware's shell summariser and by ``tools/_sandbox_exec``.
        """
        return self._exec(command, timeout=timeout)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command in the container (deepagents primitive).

        ``output`` is stdout followed by stderr, per the protocol. deepagents'
        own file-operation templates already fold stderr into stdout
        (``2>&1`` / ``2>/dev/null``), so their JSON stays parseable.
        """
        stdout, stderr, returncode = self._exec(command, timeout=timeout)
        output = stdout + stderr if stderr else stdout
        return ExecuteResponse(output=output, exit_code=returncode)

    # ── file transfer (the other two BaseSandbox primitives) ───────────────────

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write files into the container, one ``docker exec`` per file.

        Content travels as base64 on stdin (not on the command line) so it is
        bounded by nothing but the pipe, and the child runs as the image's
        non-root ``sandbox`` user — unlike ``docker cp``, which would land
        root-owned files in the bind-mounted workspace that the agent could then
        no longer edit.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
            payload = base64.b64encode(content).decode("ascii")
            stdout, stderr, rc = self._exec(
                _UPLOAD_TEMPLATE.format(path_b64=path_b64),
                stdin=payload,
            )
            responses.append(
                FileUploadResponse(path=path, error=_transfer_error(stdout, stderr, rc))
            )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read files out of the container, one ``docker exec`` per file."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
            stdout, stderr, rc = self._exec(_DOWNLOAD_TEMPLATE.format(path_b64=path_b64))
            error = _transfer_error(stdout, stderr, rc, expect="D:")
            if error is not None:
                responses.append(FileDownloadResponse(path=path, error=error))
                continue
            try:
                content = base64.b64decode(stdout.strip()[2:], validate=True)
            except Exception as exc:
                responses.append(
                    FileDownloadResponse(path=path, error=f"Malformed sandbox response: {exc}")
                )
                continue
            responses.append(FileDownloadResponse(path=path, content=content))
        return responses


def _transfer_error(stdout: str, stderr: str, returncode: int, *, expect: str = "OK") -> str | None:
    """Map a transfer script's status token onto a FileOperationError, or None."""
    token = stdout.strip()
    if returncode != 0 and not token.startswith(("E:", expect)):
        return (stderr.strip() or f"sandbox transfer failed with exit code {returncode}")[:500]
    if token.startswith("E:"):
        detail = token[2:]
        return _TRANSFER_ERRORS.get(detail, detail or "unknown transfer error")
    if not token.startswith(expect):
        return (token or stderr.strip() or "empty sandbox response")[:500]
    return None
