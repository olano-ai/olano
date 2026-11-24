"""Load agent configs from the active Olano home and local project agents/."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from olano.config.agent_config import AgentConfig
from olano.config.home import get_olano_paths
from olano.config.json5_fast import loads as _loads_json5


def _load_json5(text: str) -> dict:
    """Parse an ``agent.json5`` document.

    Thin alias over :mod:`olano.config.json5_fast` — kept because several
    modules import this name. The old inline fallback here stripped ``//``
    with a regex, which silently truncated any string value containing one
    (``"url": "https://…"``); the shared loader is string-literal aware.
    """
    return cast(dict, _loads_json5(text))


_CONFIG_FILENAMES = ("agent.json5",)


def _interpolate_env(value: Any) -> Any:
    """Replace ${VAR} placeholders with environment variable values."""
    if isinstance(value, str):
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


# Keys that identify "the same element" across a save, so a list that grew or
# was reordered still matches its old entries. Ordered by how specific they are.
_IDENTITY_KEYS = ("url", "id", "name", "type")


def _align_list(new: list, old: list) -> list[tuple[Any, Any]]:
    """Pair each new element with the old element it corresponds to (or None).

    Prefers an identity key so appending or reordering keeps the pairing;
    falls back to position for scalar lists and dicts with no usable key.
    """
    for key in _IDENTITY_KEYS:
        old_by_id = {
            item[key]: item
            for item in old
            if isinstance(item, dict) and isinstance(item.get(key), str)
        }
        if old_by_id and all(
            isinstance(item, dict) and isinstance(item.get(key), str) for item in new
        ):
            return [(item, old_by_id.get(item[key])) for item in new]
    return [(item, old[i] if i < len(old) else None) for i, item in enumerate(new)]


def _restore_env_placeholders(new: Any, old: Any) -> Any:
    """Put ``${VAR}`` placeholders back into a config about to be written.

    ``load_agent_config`` interpolates the whole raw dict before validation, so
    a plain load → mutate → save round trip would write the *resolved* value
    back to disk: the file stops referencing the variable, rotating it silently
    stops taking effect, and the live value lands in a file that gets read,
    copied and committed to revision history. Every ``admin.*`` mutation and
    every dashboard save is such a round trip, and the MCP OAuth back-fill made
    one fire automatically from an end user completing a sign-in.

    A value is restored only when it still *equals what the placeholder
    resolves to* — so a caller that deliberately changed the field keeps its
    new value, and a variable that is unset (interpolation leaves the literal)
    round-trips unchanged either way. ``${vault:NAME}`` refs were already safe
    (no env var of that name, so the literal survives); this makes plain
    ``${VAR}`` equally safe.
    """
    if isinstance(old, str) and isinstance(new, str):
        return old if "${" in old and _interpolate_env(old) == new else new
    if isinstance(old, dict) and isinstance(new, dict):
        return {
            k: (_restore_env_placeholders(v, old[k]) if k in old else v) for k, v in new.items()
        }
    if isinstance(old, list) and isinstance(new, list):
        return [
            _restore_env_placeholders(item, match) if match is not None else item
            for item, match in _align_list(new, old)
        ]
    return new


# Also support a local agents/ dir in the project workspace
_LOCAL_AGENTS_DIR = Path("agents")

# ── managed-box gate for the project-local agents/ dir ───────────────────────
# The repo's own agents/ holds internal test fixtures, and the backend image
# used to bake it in (CWD /app ⇒ Path("agents") resolved to /app/agents on
# every customer box). The image no longer ships the dir (.dockerignore), but
# a box that was provisioned as managed must never surface a project-local
# agents dir even from a stale image: on managed evidence — the env markers
# cloud-init writes (LIONCLAW_DEPLOYMENT_MANAGED / a deployment token) or a
# managed /etc/olano/installation.json — the "auto" search path drops the
# local dir entirely. Detection is deliberately cheap (env reads + one small
# file, memoized per process) and fails CLOSED: evidence we cannot interpret
# means "possibly a customer box", not "open". Dev escape hatch:
# LIONCLAW_DISABLE_LOCAL_AGENTS_DIR=0 forces the dir on, =1 forces it off.
_LOCAL_DIR_OVERRIDE_ENV = "LIONCLAW_DISABLE_LOCAL_AGENTS_DIR"
_FALSY = {"0", "false", "no", "off"}
_local_agents_dir_memo: bool | None = None


def _local_agents_dir_enabled() -> bool:
    global _local_agents_dir_memo
    if _local_agents_dir_memo is None:
        _local_agents_dir_memo = _decide_local_agents_dir()
    return _local_agents_dir_memo


def _decide_local_agents_dir() -> bool:
    override = os.environ.get(_LOCAL_DIR_OVERRIDE_ENV, "").strip().lower()
    if override:
        return override in _FALSY
    try:
        # Local import: deployment.sources pulls in httpx/cryptography, which
        # must stay off this module's import path (loader is imported early,
        # everywhere). Paid once thanks to the memo above.
        from olano.deployment import sources

        if sources.managed_markers():
            return False
        manifest = sources.read_manifest()  # ManifestError ⇒ broken managed box
        if manifest is not None and manifest.managed:
            return False
    except Exception:
        return False  # fail closed: unreadable evidence is not "unmanaged"
    return True


def _search_paths(source: str = "auto") -> list[Path]:
    local = _LOCAL_AGENTS_DIR.resolve()
    home = get_olano_paths().agents_dir

    if source == "local":
        return [local]
    if source == "home":
        return [home]
    if not _local_agents_dir_enabled():
        return [home]
    # Project-local agents take precedence over home agents.
    return [local, home]


def load_agent_config(name: str, *, source: str = "auto") -> AgentConfig:
    """Load and parse ``agent.json5`` for the agent."""
    path = find_agent_config_path(name, source=source)
    if path is None:
        searched = [str(p / name / f) for p in _search_paths(source) for f in _CONFIG_FILENAMES]
        raise FileNotFoundError(f"No agent config found for '{name}'. Searched: {searched}")
    text = path.read_text(encoding="utf-8")
    raw = _load_json5(text)
    raw = _interpolate_env(raw)
    raw.setdefault("name", name)
    return AgentConfig.model_validate(raw)


def _read_raw_config(path: Path) -> dict:
    """The uninterpolated on-disk config, or ``{}`` when absent/unreadable."""
    try:
        return _load_json5(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def mutate_agent_config(
    name: str,
    apply: Callable[[AgentConfig], None],
    *,
    source: str = "home",
) -> AgentConfig:
    """Load → mutate → save an agent config as ONE atomic operation.

    The read-modify-write must happen under a single lock hold. Callers used to
    ``load_agent_config`` outside any lock and only enter it inside
    ``save_agent_config``, so a concurrent edit landing in that window was
    clobbered wholesale — the save rewrites the *entire* file from a model that
    never saw the other change. Two dashboard saves, or a dashboard save racing
    the MCP OAuth back-fill, silently lost one of them.

    ``apply`` mutates the freshly-loaded config in place; the saved config is
    returned. The lock is re-entrant, so ``save_agent_config`` taking it again
    inside this block is a no-op acquisition.
    """
    from olano.deployment.locks import deployment_write_lock

    with deployment_write_lock():
        config = load_agent_config(name, source=source)
        apply(config)
        save_agent_config(config)
        return config


def find_agent_config_path(name: str, *, source: str = "auto") -> Path | None:
    """Return the discovered config path for an agent, respecting search precedence."""
    for base in _search_paths(source):
        for fname in _CONFIG_FILENAMES:
            candidate = base / name / fname
            if candidate.exists():
                return candidate
    return None


# Per-file memo for the hidden flag, keyed on mtime so an edit invalidates it.
# list_agents() runs per request on roster/status paths; steady state is one
# stat per agent, a parse only when the file changed.
_HIDDEN_MEMO: dict[str, tuple[int, bool]] = {}


def _config_is_hidden(path: Path) -> bool:
    """True only for a literal, parseable ``hidden: true`` in the config.

    Identity-test semantics on purpose, mirroring runtime/api._agent_is_hidden:
    a missing key, ``false``, any other value, or an unparseable file all read
    as VISIBLE — hiding is opt-in per agent, and a broken config must never
    make a real agent vanish from every listing.
    """
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return False
    key = str(path)
    memo = _HIDDEN_MEMO.get(key)
    if memo is not None and memo[0] == mtime_ns:
        return memo[1]
    try:
        raw = _load_json5(path.read_text(encoding="utf-8"))
        hidden = isinstance(raw, dict) and raw.get("hidden") is True
    except Exception:
        hidden = False
    _HIDDEN_MEMO[key] = (mtime_ns, hidden)
    return hidden


def list_agents(*, source: str = "auto", include_hidden: bool = False) -> list[str]:
    """Return agent names discovered across search paths.

    Agents whose config carries ``hidden: true`` (internal fixtures, or agents
    an operator deliberately hid) are excluded by default, so every enumeration
    surface — dashboard views, admin access tables, tool listings — is safe by
    construction. Callers that genuinely need the complete on-disk set (the
    boot loop, maintenance sweeps, ``olano agents list``, the admin API that
    tags hidden agents) pass ``include_hidden=True``; new opt-ins are guarded
    by the allowlist test in tests/test_hidden_agents_policy.py.
    """
    names: set[str] = set()
    for base in _search_paths(source):
        if base.exists():
            for p in base.iterdir():
                if p.is_dir() and any((p / f).exists() for f in _CONFIG_FILENAMES):
                    names.add(p.name)
    if not include_hidden:
        # Resolve through find_agent_config_path so local-over-home precedence
        # decides which file's `hidden` counts — the file that would load.
        names = {
            name
            for name in names
            if (path := find_agent_config_path(name, source=source)) is None
            or not _config_is_hidden(path)
        }
    return sorted(names)


def write_agent_config_text(path: Path, content: str) -> None:
    """Write ``agent.json5`` atomically — the one way this file may be written.

    ``Path.write_text`` truncates the file and only then fills it, so a reader
    that lands in that window sees **zero bytes**. agent.json5 is read
    constantly and from everywhere — a booting agent, the roster scan, the
    deployment counter, the dashboard, a second process — and the readers that
    swallow a parse error (``except Exception: old_raw = {}``) turn a truncated
    read into a silently wrong answer rather than a crash. Writing to a sibling
    temp file and ``os.replace``-ing it in closes the window: every reader sees
    either the whole old file or the whole new one.

    Route new agent.json5 writers through here rather than calling
    ``write_text`` directly.
    """
    # Local import: `olano.config.loader` is on the earliest import path and
    # `markdown_memory` pulls in langchain_core.tools.
    from olano.memory.markdown_memory import atomic_write_text

    atomic_write_text(path, content)


def save_agent_config(
    config: AgentConfig,
    base: Path | None = None,
    *,
    enforce_deployment_limits: bool = True,
) -> Path:
    """Persist an AgentConfig to disk as JSON5.

    The `workspace` field is excluded — it's derived from the agent's name
    at load time (see AgentConfig._derive_workspace).

    This is the central persistence guard for deployment resource limits:
    the count → decide → write sequence runs under the interprocess
    deployment write lock, so concurrent creates (CLI vs API vs threads)
    cannot jointly exceed a cap. Raises
    :class:`olano.deployment.enforcement.DeploymentLimitExceeded` when the
    write would grow a counted resource beyond its cap. Unmanaged
    installations short-circuit to the plain write.
    """
    base = base or get_olano_paths().agents_dir
    agent_dir = base / config.name
    path = agent_dir / "agent.json5"
    new_raw = config.model_dump(exclude={"workspace"})
    # An MCP OAuth client secret never reaches the file as plain text: this is
    # the one write path every model-based writer shares (mutate_agent_config,
    # admin.update_agent, add_mcp_server, the magic-link credentials form), so
    # vaulting here means none of them can forget. The dashboard's raw-dict
    # path does the same in runtime/api._write_agent_config.
    from olano.config.mcp_secrets import absorb_mcp_client_secrets_quietly

    absorb_mcp_client_secrets_quietly(config.name, new_raw)

    if not enforce_deployment_limits:
        new_raw = _restore_env_placeholders(new_raw, _read_raw_config(path))
        content = json.dumps(new_raw, indent=2, ensure_ascii=False) + "\n"
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_agent_config_text(path, content)
        return path

    from olano.deployment import enforcement
    from olano.deployment.locks import deployment_write_lock

    with deployment_write_lock():
        existing = path if path.exists() else find_agent_config_path(config.name)
        old_raw: dict | None = None
        if existing is not None:
            try:
                old_raw = _load_json5(existing.read_text(encoding="utf-8"))
            except Exception:
                old_raw = {}  # unreadable ⇒ agent exists but counts as empty
        # The on-disk raw is the source of truth for placeholders: restore them
        # before counting, so enforcement sees exactly what will be written.
        new_raw = _restore_env_placeholders(new_raw, old_raw or {})
        content = json.dumps(new_raw, indent=2, ensure_ascii=False) + "\n"
        decision = enforcement.validate_config_write(config.name, old_raw, new_raw)
        if not decision.allowed:
            raise enforcement.DeploymentLimitExceeded(decision)
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_agent_config_text(path, content)
        enforcement.invalidate_counts()
    return path
