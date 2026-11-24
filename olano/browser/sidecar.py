"""The live browser's container: image, per-agent lifecycle, and the DevTools pipe.

``browser.mode: live`` gives each agent a full Chromium in its own
``olano-browser`` container (``docker/olano-browser/``). Four rules shape this
module.

**The image is pulled or built, never installed.** A managed update replaces
the runtime container, so anything installed inside it is gone on the next
update. The live browser is a separate image the runtime fetches when the mode
is turned on (:func:`start_pull`), from ``OLANO_BROWSER_IMAGE`` (a deployment
pin, default ``ghcr.io/olano-ai/olano-browser:latest``), re-tagged to the local
name the runtime runs, ``LIONCLAW_BROWSER_IMAGE`` (default
``olano-browser:latest``), the same two-name scheme as the sandbox image.
**When the pull fails it is built here instead** from ``docker/olano-browser/``
(:func:`build_context`), which ships in the runtime image and in every source
checkout: the published images are private, and the runtime's ``docker`` client
holds no registry login (a managed box logs in on the host, where only the
update agent can use it; a self-deployed box usually has no login at all). The
build needs only the distro's package mirror. Headless needs no install at
all: its Chromium is already in the runtime image.

**DevTools crosses the boundary over ``docker exec``, never the network.**
Chromium listens on 127.0.0.1 inside the container and nothing is published.
On a managed box the runtime sits on the compose network and this container on
the sandbox lane, which docker isolates from each other, and the runtime
already reaches sandboxes only through ``docker exec``. :class:`ExecPipeRelay`
listens on 127.0.0.1 in the runtime, and for each connection Playwright opens
spawns ``docker exec -i <ctr> browserd.py pipe`` and splices bytes both ways.
A per-relay token, sent by Playwright as a header and checked before the first
byte is forwarded, keeps any other local process from driving the browser.

**One container per agent, hardened like a sandbox**: non-root (the image's
uid 1000), every capability dropped, no new privileges, a pids cap, a memory
and CPU ceiling, and the cloud metadata host pinned to loopback. It holds no
workspace and no credentials: sign-ins live in the runtime's saved session
state (``sessions.state_path``) and are loaded into a fresh context each time.

**The runtime owns every container it started.** Sessions are in-memory, so
any ``olano.browser=1`` container alive at boot belongs to nobody and is
removed (:func:`reap_orphans`), and an idle session's container is removed
with it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_IMAGE = "ghcr.io/olano-ai/olano-browser:latest"
DEFAULT_LOCAL_IMAGE = "olano-browser:latest"
CONTAINER_PREFIX = "olano-browser-"
LABEL = "olano.browser"
LABEL_AGENT = "olano.browser.agent"
LABEL_SIGNATURE = "olano.browser.signature"
BROWSERD = "/opt/olano-browser/browserd.py"
RELAY_HEADER = "x-olano-relay"
# Bump when the run arguments change what an existing container is missing.
SIGNATURE_SCHEMA = 1

_PULL_TIMEOUT_S = 1800
_RUN_TIMEOUT_S = 60
_READY_TIMEOUT_S = 45

_pull_lock = threading.Lock()
_pull_state: dict[str, Any] = {
    "status": "idle",
    "error": "",
    "started_at": None,
    "finished_at": None,
}
_present_memo: dict[str, bool] = {}
_listeners: list[Any] = []


class SidecarUnavailable(RuntimeError):
    """The live browser container cannot be started; the message is user-facing."""


def local_image() -> str:
    return os.environ.get("LIONCLAW_BROWSER_IMAGE", "").strip() or DEFAULT_LOCAL_IMAGE


def source_image() -> str:
    return os.environ.get("OLANO_BROWSER_IMAGE", "").strip() or DEFAULT_SOURCE_IMAGE


def build_context() -> Path | None:
    """The directory the image is built from, or ``None`` when this install
    carries no copy (a wheel install). ``LIONCLAW_BROWSER_BUILD_CONTEXT``
    overrides it."""
    override = os.environ.get("LIONCLAW_BROWSER_BUILD_CONTEXT", "").strip()
    if override:
        candidates = [Path(override)]
    else:
        import olano

        candidates = [Path(olano.__file__).resolve().parent.parent / "docker" / "olano-browser"]
    for candidate in candidates:
        if (candidate / "Dockerfile").is_file() and (candidate / "browserd.py").is_file():
            return candidate
    return None


def container_memory() -> str:
    return os.environ.get("LIONCLAW_BROWSER_MEMORY", "").strip() or "1g"


def container_cpus() -> str:
    return os.environ.get("LIONCLAW_BROWSER_CPUS", "").strip() or "1.0"


def _run(
    cmd: list[str], timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def docker_available() -> bool:
    return shutil.which("docker") is not None


def agent_slug(agent: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (agent or "default").lower()).strip("-")
    if not slug:
        slug = "default"
    if len(slug) > 40 or slug != (agent or "").lower():
        # Keep names unique when sanitising collapsed two agent names together.
        digest = hashlib.sha1((agent or "").encode()).hexdigest()[:8]
        slug = f"{slug[:40]}-{digest}"
    return slug


def container_name(agent: str) -> str:
    return f"{CONTAINER_PREFIX}{agent_slug(agent)}"


def on_status_change(callback: Any) -> None:
    """Register ``callback(status_dict)`` for pull progress (the server emits SSE)."""
    _listeners.append(callback)


def _notify() -> None:
    snapshot = image_status()
    for cb in list(_listeners):
        try:
            cb(snapshot)
        except Exception:  # pragma: no cover - a listener must never break a pull
            logger.debug("browser image listener failed", exc_info=True)


# ── image ────────────────────────────────────────────────────────────────────


def image_present(*, refresh: bool = False) -> bool:
    image = local_image()
    if not refresh and _present_memo.get(image):
        return True
    if not docker_available():
        return False
    try:
        proc = _run(["docker", "image", "inspect", image], timeout=15)
    except Exception:
        return False
    present = proc.returncode == 0
    if present:
        _present_memo[image] = True
    else:
        _present_memo.pop(image, None)
    return present


def image_status() -> dict[str, Any]:
    with _pull_lock:
        pull = dict(_pull_state)
    present = image_present() if pull.get("status") not in ("pulling", "building") else False
    return {
        "image": local_image(),
        "source": source_image(),
        "present": present,
        "docker": docker_available(),
        "pull": pull,
    }


def _set_pull(**kw: Any) -> None:
    with _pull_lock:
        _pull_state.update(kw)


def _last_line(proc: subprocess.CompletedProcess[str], what: str) -> str:
    lines = [ln for ln in (proc.stderr or proc.stdout or "").strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else f"{what} exited {proc.returncode}"


def _pull(source: str, local: str) -> None:
    proc = _run(["docker", "pull", source], timeout=_PULL_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(_last_line(proc, "docker pull"))
    if source != local:
        tag = _run(["docker", "tag", source, local], timeout=60)
        if tag.returncode != 0:
            raise RuntimeError((tag.stderr or "docker tag failed").strip())


_STEP_ECHO_RE = re.compile(r'process "/bin/sh -c .*?" did not complete successfully', re.S)
_APT_ERROR_RE = re.compile(r"(?:^|\s)(E: .+|Err:\d+ .+|Temporary failure resolving .+)$")


def _build_failure(proc: subprocess.CompletedProcess[str]) -> str:
    """The line that says why a build failed. Docker's own last line echoes the
    whole RUN command; the package manager's error above it is the reason."""
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    for line in reversed(text.splitlines()):
        match = _APT_ERROR_RE.search(line.strip())
        if match:
            return match.group(1).strip()[:240]
    last = _last_line(proc, "docker build")
    return _STEP_ECHO_RE.sub("a build step did not complete successfully", last)[:240]


def _build(context: Path, local: str) -> None:
    # The runtime image carries the docker CLI without the buildx plugin, and
    # a DOCKER_BUILDKIT=1 inherited from the environment would then refuse to
    # build at all; unset, the CLI picks whichever builder it has.
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_BUILDKIT"}
    proc = _run(["docker", "build", "-t", local, str(context)], _PULL_TIMEOUT_S, env=env)
    if proc.returncode != 0:
        raise RuntimeError(_build_failure(proc))


def _pull_worker() -> None:
    source, local = source_image(), local_image()
    try:
        try:
            _pull(source, local)
            via = "pull"
        except Exception as pull_exc:
            context = build_context()
            if context is None:
                raise RuntimeError(
                    f"Could not download {source} ({pull_exc}), and this install carries "
                    "no copy of the image's build files to build it here instead."
                ) from pull_exc
            logger.info(
                "browser: pull of %s failed (%s); building %s here", source, pull_exc, local
            )
            _set_pull(status="building", error="", reason=str(pull_exc)[:300])
            _notify()
            try:
                _build(context, local)
            except Exception as build_exc:
                raise RuntimeError(
                    f"Could not download {source} ({pull_exc}), and building it on this "
                    f"machine failed too ({build_exc}). The build needs the Debian package "
                    "mirror and about 1.5 GB of free disk."
                ) from build_exc
            via = "build"
        _present_memo.pop(local, None)
        if not image_present(refresh=True):
            raise RuntimeError(f"{local} is still missing after the {via}")
        _set_pull(status="ready", error="", via=via, finished_at=time.time())
        logger.info("browser: %s ready (%s)", local, via)
    except Exception as exc:
        msg = str(exc)
        _set_pull(status="failed", error=msg[:600], finished_at=time.time())
        logger.warning("browser: image unavailable: %s", msg)
    finally:
        _notify()


def start_pull(*, force: bool = False) -> dict[str, Any]:
    """Fetch the live browser image in the background: pull it, or build it
    here when the pull fails. Idempotent while running.

    Returns the status snapshot. A present image is not re-fetched unless
    ``force`` (the update agent keeps it current; this is the on-enable fetch).
    """
    if not docker_available():
        _set_pull(status="failed", error="Docker is not available on this machine.")
        return image_status()
    with _pull_lock:
        pulling = _pull_state.get("status") in ("pulling", "building")
    if pulling:
        return image_status()
    if not force and image_present(refresh=True):
        _set_pull(status="ready", error="")
        return image_status()
    _set_pull(
        status="pulling", error="", reason="", via="", started_at=time.time(), finished_at=None
    )
    threading.Thread(target=_pull_worker, name="olano-browser-pull", daemon=True).start()
    _notify()
    return image_status()


# ── containers ───────────────────────────────────────────────────────────────


def _signature(proxied: bool = False) -> str:
    payload = json.dumps(
        {
            "schema": SIGNATURE_SCHEMA,
            "image": local_image(),
            "memory": container_memory(),
            "cpus": container_cpus(),
            # Part of the recipe: the flag is a launch flag of the browser
            # inside, so a proxy coming or going needs a new container.
            "proxied": proxied,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _network() -> str:
    try:
        from olano.backends.lionclaw_docker import resolve_bridge_network

        return resolve_bridge_network()
    except Exception:
        return "bridge"


def run_args(agent: str, *, proxied: bool = False) -> list[str]:
    """``docker run`` for *agent*'s browser. *proxied*: the agent's contexts go
    through ``BROWSER_PROXY_URL``, so the browser keeps WebRTC on the proxy
    (``OLANO_BROWSER_PROXIED``, read by ``browserd.py``); without a proxy that
    flag would only break calls for a person who takes control."""
    name = container_name(agent)
    return [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--init",
        f"--label={LABEL}=1",
        f"--label={LABEL_AGENT}={agent_slug(agent)}",
        f"--label={LABEL_SIGNATURE}={_signature(proxied)}",
        *(["-e", "OLANO_BROWSER_PROXIED=1"] if proxied else []),
        f"--network={_network()}",
        f"--memory={container_memory()}",
        f"--cpus={container_cpus()}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--shm-size=256m",
        "--add-host=metadata.google.internal:127.0.0.1",
        local_image(),
    ]


def _inspect(name: str) -> dict[str, Any] | None:
    try:
        proc = _run(["docker", "inspect", name], timeout=15)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return None
    return data[0] if data else None


def _remove(name: str) -> None:
    try:
        _run(["docker", "rm", "-f", name], timeout=30)
    except Exception:
        logger.debug("browser: removing %s failed", name, exc_info=True)


def _container_log_tail(name: str, chars: int = 600) -> str:
    """The end of the container's own output: when it exited, this is the only
    place the reason is written (browserd and Chromium log to stderr)."""
    try:
        proc = _run(["docker", "logs", "--tail", "20", name], timeout=15)
    except Exception:
        return ""
    return " ".join(((proc.stdout or "") + (proc.stderr or "")).split())[-chars:]


def devtools_version(name: str) -> dict[str, Any]:
    proc = _run(["docker", "exec", name, "python3", BROWSERD, "version"], timeout=_READY_TIMEOUT_S)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()[-300:]
        tail = _container_log_tail(name)
        raise SidecarUnavailable(
            "The live browser started but its DevTools endpoint never answered: "
            + detail
            + (f" Container log: {tail}" if tail else "")
        )
    try:
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SidecarUnavailable(f"Unreadable DevTools answer: {exc}") from exc
    if not isinstance(answer, dict):
        raise SidecarUnavailable("Unreadable DevTools answer: not an object")
    return answer


def ensure_container(agent: str, *, proxied: bool = False) -> tuple[str, dict[str, Any]]:
    """Start (or reuse) *agent*'s browser container. Blocking: run in a thread.

    Returns ``(container_name, devtools_version_json)``. *proxied* is part of
    the container's recipe (:func:`run_args`).
    """
    if not docker_available():
        raise SidecarUnavailable("The live browser needs Docker, and this machine has none.")
    if not image_present():
        status = start_pull()
        state = status.get("pull", {}).get("status")
        raise SidecarUnavailable(
            "The live browser is still being installed on this machine "
            f"(image {local_image()}: {state}). Try again in a few minutes."
        )
    name = container_name(agent)
    info = _inspect(name)
    if info is not None:
        labels = (info.get("Config") or {}).get("Labels") or {}
        running = bool((info.get("State") or {}).get("Running"))
        if running and labels.get(LABEL_SIGNATURE) == _signature(proxied):
            return name, devtools_version(name)
        _remove(name)
    proc = _run(run_args(agent, proxied=proxied), timeout=_RUN_TIMEOUT_S)
    if proc.returncode != 0:
        raise SidecarUnavailable(
            "Could not start the live browser container: "
            + (proc.stderr or proc.stdout or "").strip()[-300:]
        )
    try:
        return name, devtools_version(name)
    except SidecarUnavailable:
        _remove(name)
        raise


def stop_container(agent: str) -> None:
    _remove(container_name(agent))


def list_containers() -> list[dict[str, str]]:
    if not docker_available():
        return []
    try:
        proc = _run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={LABEL}=1",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            timeout=15,
        )
    except Exception:
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            out.append({"name": parts[0], "status": parts[1] if len(parts) > 1 else ""})
    return out


def reap_orphans(keep: set[str] | None = None) -> list[str]:
    """Remove every browser container not named in *keep*. Returns what went."""
    keep = keep or set()
    removed = []
    for row in list_containers():
        if row["name"] not in keep:
            _remove(row["name"])
            removed.append(row["name"])
    if removed:
        logger.info("browser: removed %d orphaned live browser container(s)", len(removed))
    return removed


def pipe_argv(name: str) -> list[str]:
    return ["docker", "exec", "-i", name, "python3", BROWSERD, "pipe"]


# ── the DevTools relay ───────────────────────────────────────────────────────


class ExecPipeRelay:
    """127.0.0.1 listener that turns each connection into a ``docker exec`` pipe.

    Runs on the browser session loop. The first bytes of a connection are the
    HTTP upgrade request Playwright sends; it must carry ``X-Olano-Relay:
    <token>`` or the connection is closed before anything reaches the browser.
    """

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._procs: set[asyncio.subprocess.Process] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    def headers(self) -> dict[str, str]:
        return {RELAY_HEADER: self.token}

    def ws_url(self, devtools_ws_url: str) -> str:
        path = re.sub(r"^wss?://[^/]+", "", devtools_ws_url)
        return f"ws://127.0.0.1:{self.port}{path}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            except Exception:
                pass
            self._server = None
        for proc in list(self._procs):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > 32768:
                break
        return head

    def _authorized(self, head: bytes) -> bool:
        text = head.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
        for line in text.split("\r\n")[1:]:
            key, _, value = line.partition(":")
            if key.strip().lower() == RELAY_HEADER:
                return hmac.compare_digest(value.strip(), self.token)
        return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(self._read_head(reader), timeout=10)
        except Exception:
            writer.close()
            return
        if not self._authorized(head):
            try:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            logger.warning("browser relay: could not start the exec pipe: %s", exc)
            writer.close()
            return
        self._procs.add(proc)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(head)

        async def upstream() -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    proc.stdin.write(data)  # type: ignore[union-attr]
                    await proc.stdin.drain()  # type: ignore[union-attr]
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()  # type: ignore[union-attr]
                except Exception:
                    pass

        async def downstream() -> None:
            try:
                while True:
                    data = await proc.stdout.read(65536)  # type: ignore[union-attr]
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass

        try:
            await asyncio.gather(upstream(), downstream())
        finally:
            self._procs.discard(proc)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                writer.close()
            except Exception:
                pass
