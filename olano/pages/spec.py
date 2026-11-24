"""What a deployed page runs: one validated, immutable description.

A plain page (``pages_start``) serves a workspace directory as it is. A
*deployed* page (``pages_deploy``) also runs commands, and this spec is the
whole of what it may run:

* ``build``: run once per deploy or restart, in the page directory, before
  anything is served. For a static app (no ``start``) the build's ``output``
  directory is snapshotted into an immutable release and served from there; a
  failed build leaves the previous release live.
* ``start``: a long-running HTTP server. Present ⇒ the page is a *service*:
  the runtime keeps it running and proxies the page URL to it.

Validation lives here, and only here, so the agent tool, the dashboard and the
supervisor cannot disagree about what a well-formed deploy is. Commands are
shell strings (the same shape ``execute`` takes); they are also checked by the
shell guard and gated like a shell tool before they are accepted (see
``tools/pages_tools.py`` and the security middleware).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import PurePosixPath
from typing import Any

KIND_STATIC = "static"
KIND_SERVICE = "service"

MAX_COMMAND_CHARS = 4000
BUILD_TIMEOUT_DEFAULT = 600
BUILD_TIMEOUT_RANGE = (30, 1800)
STARTUP_TIMEOUT_DEFAULT = 60
STARTUP_TIMEOUT_RANGE = (5, 600)
MAX_ENV_VARS = 32

# Variables the keeper sets itself, or that would let a page reach around its
# own process: never taken from a deploy's env.
_RESERVED_ENV = frozenset(
    {
        "PORT",
        "HOST",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class SpecError(ValueError):
    """A deploy request that cannot be accepted, with the reason."""


def _clean_command(name: str, value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_COMMAND_CHARS:
        raise SpecError(f"{name} is longer than {MAX_COMMAND_CHARS} characters")
    if "\x00" in text:
        raise SpecError(f"{name} contains a NUL byte")
    if "\n" in text or "\r" in text:
        raise SpecError(
            f"{name} must be one line: join steps with && (for example 'npm ci && npm run build')"
        )
    return text


def _clean_relpath(name: str, value: str | None) -> str:
    text = (value or "").strip()
    if text.startswith("/"):
        raise SpecError(f"{name} must be a path inside the page directory, without '..'")
    text = text.strip("/")
    if not text or text == ".":
        return ""
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts:
        raise SpecError(f"{name} must be a path inside the page directory, without '..'")
    return str(pure)


def _bounded(name: str, value: int | None, default: int, bounds: tuple[int, int]) -> int:
    if value is None or int(value) == 0:
        return default
    lo, hi = bounds
    number = int(value)
    if number < lo or number > hi:
        raise SpecError(f"{name} must be between {lo} and {hi} seconds")
    return number


@dataclass(frozen=True)
class DeploySpec:
    """The commands and serving rules of one deployed page."""

    build: str = ""
    start: str = ""
    #: Static apps: the build's output directory, relative to the page directory.
    output: str = ""
    #: Services: the port the server listens on; 0 ⇒ the runtime picks one and
    #: passes it as $PORT.
    port: int = 0
    #: Services: the path probed for readiness and health (2xx-4xx counts as up).
    healthcheck: str = "/"
    #: Services: forward ``/pages/<slug>/x`` as ``/x`` (True) or verbatim (False,
    #: for frameworks configured with a base path, such as Next.js ``basePath``).
    strip_prefix: bool = True
    build_timeout: int = BUILD_TIMEOUT_DEFAULT
    startup_timeout: int = STARTUP_TIMEOUT_DEFAULT
    #: Extra non-secret environment for build and start, such as
    #: ``NODE_ENV=production``. Never secrets: the registry stores it in plain
    #: JSON and ``pages_status`` echoes it back.
    env: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return KIND_SERVICE if self.start else KIND_STATIC

    def digest(self) -> str:
        """Stable identity of what runs: a change means rebuild and restart."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["env"] = dict(sorted(self.env.items()))
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DeploySpec | None:
        """Rebuild a stored spec; ``None`` for a record that never deployed.

        Lenient on purpose: a stored record was validated when written, and an
        unknown key from a newer release must not make the page unreadable.
        """
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        env = kwargs.get("env")
        kwargs["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        try:
            return cls(**kwargs)
        except TypeError:
            return None


def build_spec(
    *,
    build: str | None = None,
    start: str | None = None,
    output: str | None = None,
    port: int | None = None,
    healthcheck: str | None = None,
    strip_prefix: bool = True,
    build_timeout: int | None = None,
    startup_timeout: int | None = None,
    env: dict[str, Any] | None = None,
) -> DeploySpec:
    """Validate a deploy request into a :class:`DeploySpec` or raise :class:`SpecError`."""
    build_cmd = _clean_command("build", build)
    start_cmd = _clean_command("start", start)
    if not build_cmd and not start_cmd:
        raise SpecError(
            "give a build command, a start command, or both; a page that runs "
            "nothing is published with pages_start"
        )
    out = _clean_relpath("output", output)
    if start_cmd and out:
        raise SpecError("output applies to static builds only; a service serves its own responses")
    if not start_cmd and not out:
        raise SpecError(
            "a static build needs output: the directory the build writes, such as 'dist' or 'build'"
        )

    port_number = int(port or 0)
    if port_number and not (1024 <= port_number <= 65535):
        raise SpecError("port must be between 1024 and 65535, or 0 to let the runtime choose")
    if port_number and not start_cmd:
        raise SpecError("port applies to services only")

    check = (healthcheck or "/").strip() or "/"
    if not check.startswith("/") or check.startswith("//") or any(c in check for c in " \r\n#"):
        raise SpecError("healthcheck must be a path such as '/' or '/health'")

    clean_env: dict[str, str] = {}
    for key, value in (env or {}).items():
        name = str(key).strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise SpecError(f"env name {name!r} is not a valid variable name")
        if name in _RESERVED_ENV or name.upper().startswith(("OLANO_", "LIONCLAW_")):
            raise SpecError(f"env {name} is set by the runtime and cannot be overridden")
        text = str(value)
        if "\x00" in text or len(text) > 2000:
            raise SpecError(f"env {name} is too long or contains a NUL byte")
        clean_env[name] = text
    if len(clean_env) > MAX_ENV_VARS:
        raise SpecError(f"at most {MAX_ENV_VARS} env variables")

    return DeploySpec(
        build=build_cmd,
        start=start_cmd,
        output=out,
        port=port_number,
        healthcheck=check,
        strip_prefix=bool(strip_prefix),
        build_timeout=_bounded(
            "build_timeout", build_timeout, BUILD_TIMEOUT_DEFAULT, BUILD_TIMEOUT_RANGE
        ),
        startup_timeout=_bounded(
            "startup_timeout", startup_timeout, STARTUP_TIMEOUT_DEFAULT, STARTUP_TIMEOUT_RANGE
        ),
        env=clean_env,
    )


def record_spec(record: dict[str, Any] | None) -> DeploySpec | None:
    """The deploy spec stored on a registry record, or ``None`` for a plain page."""
    if not record:
        return None
    return DeploySpec.from_dict(record.get("deploy"))


def record_kind(record: dict[str, Any] | None) -> str:
    spec = record_spec(record)
    return spec.kind if spec is not None else KIND_STATIC
