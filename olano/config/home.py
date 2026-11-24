"""Central Olano home resolution and per-home configuration."""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Home resolution across the lionclaw → olano rename.
#
# ``OLANO_HOME`` / ``~/.olano`` are what new installs write and read: a hidden
# directory, like every other tool's state under a home directory. The earlier
# names stay honoured indefinitely: they cost one dict lookup and a few
# ``is_dir()`` calls, and they are what already-provisioned instances have on
# disk, a visible ``~/olano`` (installs made while that was the default, late
# 2026) or ``~/.lionclaw`` (the original). Nothing needs to be migrated: an
# existing box keeps using the directory it has forever unless its operator
# moves it deliberately.
_HOME_ENV_VAR = "OLANO_HOME"
_LEGACY_HOME_ENV_VAR = "LIONCLAW_HOME"
_DEFAULT_HOME_NAME = ".olano"
# Newest first: an existing ``~/olano`` outranks an existing ``~/.lionclaw``.
_LEGACY_HOME_NAMES = ("olano", ".lionclaw")
# A visible ``~/olano`` is also exactly where ``git clone`` of this repository
# lands when run from a home directory. A home must never be adopted inside a
# source checkout (the vault, every agent and every session would sit in a git
# work tree), so a ``~/olano`` carrying either marker is never a home
# candidate. An Olano home holds neither marker. The default itself needs no
# such guard: nothing clones into a dot directory.
_SOURCE_CHECKOUT_MARKERS = (".git", "pyproject.toml")
_CONFIG_FILE_NAME = "config.yaml"

# Persistence-backend selection (LangGraph checkpointer + Store). Env overrides
# the per-home YAML so an operator can flip backends without editing the file.
_CHECKPOINT_BACKEND_ENV = "LIONCLAW_CHECKPOINT_BACKEND"
_CHECKPOINT_DB_URL_ENV = "LIONCLAW_CHECKPOINT_DB_URL"
_STORE_BACKEND_ENV = "LIONCLAW_STORE_BACKEND"
_STORE_DB_URL_ENV = "LIONCLAW_STORE_DB_URL"
_VALID_BACKENDS = ("sqlite", "postgres")


def _normalize_home(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


@dataclass
class OlanoHomePaths:
    """All runtime paths derived from a single Olano home root."""

    root: Path

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def vault_dir(self) -> Path:
        return self.root / "vault"

    @property
    def vault_db_path(self) -> Path:
        return self.vault_dir / "vault.db"

    @property
    def legacy_vault_db_path(self) -> Path:
        return self.root / "vault.db"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def thread_db_path(self) -> Path:
        return self.sessions_dir / "threads.db"

    @property
    def store_db_path(self) -> Path:
        """SQLite database backing the deepagents StoreBackend (Virtual File System)."""
        return self.sessions_dir / "store.db"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def analytics_dir(self) -> Path:
        """Cross-agent analytics state — the usage-event ledger lives here."""
        return self.root / "analytics"

    @property
    def usage_ledger_path(self) -> Path:
        """Append-only JSONL of per-call LLM usage events (agent/model/tokens/usd)."""
        return self.analytics_dir / "usage.jsonl"

    @property
    def service_ledger_path(self) -> Path:
        """Append-only JSONL of per-call NON-model service usage.

        The sibling of ``usage_ledger_path``: image generation, TTS/STT and
        managed web search/fetch spend olano credits without ever producing a
        token count, so they cannot ride the LLM ledger's schema.
        """
        return self.analytics_dir / "services.jsonl"

    @property
    def threadcost_db_path(self) -> Path:
        """SQLite cache of per-thread token/cost aggregates mined from threads.db.

        Derived/regenerable data — safe to delete (it rebuilds lazily). Powers the
        Insights drill-down ranking so agent/thread cost queries never scan the
        multi-GB checkpoint store on every request.
        """
        return self.analytics_dir / "threadcost.db"

    @property
    def thread_summaries_db_path(self) -> Path:
        """SQLite store of per-transcript thread-list summaries (``runtime/thread_summaries``).

        Derived data, safe to delete: it is rebuilt from the transcripts, by
        the boot warm or the next list request.
        """
        return self.analytics_dir / "thread_summaries.db"

    @property
    def journal_dir(self) -> Path:
        """Daily work-journal entries (one ``YYYY-MM-DD.json`` per day)."""
        return self.root / "journal"

    @property
    def reports_dir(self) -> Path:
        """Rolled-up weekly/monthly highlight reports (``journal/reports/<period>/``)."""
        return self.journal_dir / "reports"

    @property
    def run_dir(self) -> Path:
        """Directory holding PID files and runtime sockets for daemon mode."""
        return self.root / "run"

    @property
    def runtime_pid_path(self) -> Path:
        return self.run_dir / "olano.pid"

    @property
    def runtime_log_path(self) -> Path:
        return self.logs_dir / "runtime.log"

    @property
    def scheduler_dir(self) -> Path:
        return self.root / "scheduler"

    @property
    def scheduler_db_path(self) -> Path:
        return self.scheduler_dir / "scheduler.db"

    @property
    def chat_directory_db_path(self) -> Path:
        """Cross-platform chat/user directory (``platforms/chat_directory.py``)."""
        return self.root / "directory" / "chats.db"

    @property
    def webhooks_db_path(self) -> Path:
        return self.root / "webhooks.db"

    @property
    def issues_db_path(self) -> Path:
        """Agent-to-agent issue tracker (``olano/issues/store.py``)."""
        return self.root / "issues.db"

    @property
    def revisions_repo_path(self) -> Path:
        """Bare git repo holding home/workspace revision history.

        Deliberately bare and *outside* any work tree: the revisions engine
        commits allowlisted snapshots only (``olano/revisions/``), so a
        ``.git`` never appears at the home root and ``git add -A`` against
        the live home is structurally impossible.
        """
        return self.root / "revisions.git"

    @property
    def pages_dir(self) -> Path:
        """Agent-published page hosting state: slug registry + viewer credential."""
        return self.root / "pages"

    @property
    def pages_registry_path(self) -> Path:
        """Host-wide slug registry for agent-published pages: <home>/pages/registry.json."""
        return self.pages_dir / "registry.json"

    @property
    def pages_auth_path(self) -> Path:
        """Owner-set HTTP Basic-Auth viewer credential (username + PBKDF2 hash)."""
        return self.pages_dir / "auth.json"

    @property
    def magic_links_dir(self) -> Path:
        """Magic-link state (browser-reachable setup pages opened from chat).

        The directory name stays ``handshake`` — it is the live location on
        every existing deployment and never surfaces to a user, so moving it
        would risk racing a concurrent writer of ``links.json`` for nothing.
        """
        return self.root / "handshake"

    @property
    def magic_links_store_path(self) -> Path:
        """Short-lived magic links: <home>/handshake/links.json (0600).

        Holds only hashes of the link token and PIN — never either value, and
        never a secret a form flow collected.
        """
        return self.magic_links_dir / "links.json"

    @property
    def magic_links_bindings_path(self) -> Path:
        """Chat-identity → dashboard-user bindings for the owner-login flow:
        <home>/handshake/owner_bindings.json (0600). Maps ``<channel>:<user_id>``
        to a dashboard user id/email; carries no password or session material.
        """
        return self.magic_links_dir / "owner_bindings.json"

    @property
    def users_dir(self) -> Path:
        return self.root / "users"

    @property
    def users_db_path(self) -> Path:
        return self.users_dir / "users.db"

    @property
    def managed_skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def config_path(self) -> Path:
        return self.root / _CONFIG_FILE_NAME

    @property
    def org_path(self) -> Path:
        """Central org-chart + talk_to_agent governance: <home>/organization.yaml."""
        return self.root / "organization.yaml"

    @property
    def approval_rules_path(self) -> Path:
        return self.root / "approval_rules.yaml"

    @property
    def chat_history_path(self) -> Path:
        return self.logs_dir / "chat_history"

    @property
    def google_token_path(self) -> Path:
        return self.root / "google_token.json"

    @property
    def connectors_dir(self) -> Path:
        """System-scope OAuth connector tokens: <home>/connectors/."""
        return self.root / "connectors"

    def connector_token_path(self, service: str) -> Path:
        """System-scope token for a generic OAuth connector.

        Google predates the generic broker and keeps its legacy
        ``google_token.json`` paths (see :meth:`google_token_path`).
        """
        return self.connectors_dir / f"{service}_token.json"

    def agent_connector_token_path(self, name: str, service: str) -> Path:
        """Per-agent token for a generic OAuth connector."""
        return self.agents_dir / name / "connectors" / f"{service}_token.json"

    @property
    def legacy_workspace_root(self) -> Path:
        return self.root / "workspace"

    def agent_workspace_dir(self, name: str) -> Path:
        """Canonical per-agent workspace: <home>/agents/<name>/workspace/."""
        return self.agents_dir / name / "workspace"

    def agent_google_token_path(self, name: str) -> Path:
        """Per-agent Google OAuth token: <home>/agents/<name>/google_token.json."""
        return self.agents_dir / name / "google_token.json"

    def agent_connections_meta_path(self, name: str) -> Path:
        """Per-agent connection metadata (sharing flags, etc.): <home>/agents/<name>/connections.json."""
        return self.agents_dir / name / "connections.json"

    def agent_runtime_state_path(self, name: str) -> Path:
        """Per-agent runtime state (paused flag, etc.): <home>/agents/<name>/runtime_state.json."""
        return self.agents_dir / name / "runtime_state.json"

    @property
    def external_archetypes_dir(self) -> Path:
        """Where externally-fetched archetype packs live (e.g. cloned `olano-agents`).

        Walked recursively by the archetype loader. Populated by cloning the
        canonical archetype repo via `olano archetypes update` (also run as
        part of `quick-setup` / `setup`).
        """
        return self.root / "archetypes"

    @property
    def skill_packs_dir(self) -> Path:
        """Where externally-fetched skill packs are cloned (e.g. `olano-ai/skills`).

        The clone is a cache; its skills are copied into ``managed_skills_dir``
        (the common skills dir, ``<home>/skills/``) by
        ``olano skills update`` (also run as part of ``quick-setup`` /
        ``setup``). Mirrors ``external_archetypes_dir`` for agent packs.
        """
        return self.root / "skill-packs"

    @property
    def deployment_dir(self) -> Path:
        """Deployment-settings state (signed cloud cache, revision high-water mark).

        See :mod:`olano.deployment` — the vendor-controlled entitlements layer.
        """
        return self.root / "deployment"

    @property
    def catalog_dir(self) -> Path:
        """Synced cloud-catalog cache (models.json, mcp.json, state.json).

        See :mod:`olano.catalog` — payloads fetched from the cloud catalog
        API, kept fresh by the background :class:`CatalogSyncService`.
        """
        return self.root / "catalog"

    @property
    def deployment_license_path(self) -> Path:
        """The signed local license file: <home>/deployment.lic."""
        return self.root / "deployment.lic"


@dataclass
class HomeConfig:
    """Config persisted once per Olano home."""

    default_agent: str = "merli"
    preferred_model: str | None = None
    runtime_api_port: int = 8765
    dashboard_port: int = 3200
    # When True, the runtime requires per-human login (session cookie) and
    # enforces per-agent access control. When False, the platform behaves as a
    # single-owner install and skips identity resolution. None (the scaffold
    # default) means "unset": runtime/auth.multi_user_enabled() resolves it by
    # deployment posture — True on a managed (control-plane-provisioned) box,
    # False on a manual install. An explicit value always wins.
    multi_user_enabled: bool | None = None
    # Which dashboard shell a SINGLE-USER install shows: "advanced" (the full
    # operator console, the default and what every install had before this
    # existed) or "simple" (the bot chat app). Multi-user installs ignore this
    # and read the per-user `ui_mode` column instead — see
    # runtime/auth.resolve_ui_mode(). Presentation only; never a permission.
    ui_mode: str = "advanced"
    # When True, the daily / weekly / monthly work-highlights digests are shown
    # in full (every agent) to every human — team-transparency mode, so a whole
    # team can see what each other's agents are doing. When False (default), a
    # non-admin member's digest is composed from ONLY the agents they can
    # access. Admins always see the full fleet digest regardless.
    highlights_all_agents: bool = False
    extra_bind_roots: list[str] = field(default_factory=list)
    # Extra directories to scan for archetype JSON5/YAML files. Each dir is
    # walked recursively. Paths may use ~ which expands at lookup time. The
    # bundled `olano/archetypes/templates/` dir is always searched first,
    # then `<home>/archetypes/` (for `olano-agents` clone), then these.
    archetype_dirs: list[str] = field(default_factory=list)
    # Deployment-wide outbound-destination lists for the agents' SSRF-gated
    # tools (web_fetch, http_request, media/image fetch, outbound A2A). Bare
    # hosts, a domain covering its subdomains. Both empty (the default) means
    # every public destination is reachable — the posture every box had before
    # the lists existed. An agent's own `egress_allow` REPLACES this allow list
    # when non-empty; its `egress_deny` is ADDED to this deny list, so a
    # deployment block can never be undone per agent. Resolution lives in
    # security/egress.py; the private-network block is separate and always on.
    egress_allow: list[str] = field(default_factory=list)
    egress_deny: list[str] = field(default_factory=list)
    # Named tool sets seeded into the shared sandbox tool layer (/opt/agent-tools)
    # on first sandbox start — pip SDKs + CLIs, installed once and shared across
    # every agent (see docker/olano-sandbox/seed-tools.sh). Only official-API
    # packages — no ToS-violating / ban-risk automation libs.
    #
    # **Empty by default.** "chinese" used to be on for everyone, which put
    # ~740 MB (tencentcloud-sdk-python dominates) into every deployment's home
    # whether or not any agent ever called a Chinese cloud API — on a 25 GB
    # disk that is the single largest thing in the part of the disk the owner
    # can actually see. Opt in per deployment instead:
    #   chinese → Alibaba/Tencent/Qiniu/Baidu cloud + storage, WeChat official
    #             APIs, Bilibili, yt-dlp
    #   coding  → the `claude` and `codex` coding-agent CLIs
    # Seeding is one-way: turning a set back off leaves what it already
    # installed in <home>/sandbox/tools (deleting a customer's installed tools
    # behind their back would be worse than the disk it costs).
    sandbox_tool_sets: list[str] = field(default_factory=list)
    # Persistence backend for LangGraph thread checkpoints. "postgres" (default)
    # uses a shared connection pool against ``checkpoint_db_url`` for true
    # concurrent writes at scale and REQUIRES a URL (no URL raises an actionable
    # error — there is no silent fallback). "sqlite" is the zero-config opt-out
    # (on-disk threads.db). See olano/core/checkpoints.py.
    checkpoint_backend: str = "postgres"
    checkpoint_db_url: str | None = None
    # Persistence backend for the deepagents Store (Virtual File System). Mirrors
    # the checkpoint backend; falls back to the checkpoint DSN when set to
    # "postgres" without an explicit store URL (one server, distinct tables).
    store_backend: str = "postgres"
    store_db_url: str | None = None
    # Central MCP OAuth relay (see olano/core/mcp_relay.py). When set, chat-
    # driven MCP OAuth builds its redirect against ``<mcp_relay_url>/mcp/callback``
    # so a domain-less deployment (e.g. a bare cloud instance with only a public IP) can still
    # complete the browser handshake. Env override: LIONCLAW_MCP_RELAY_URL. The
    # shared HMAC secret is resolved separately (env/vault), never stored here.
    mcp_relay_url: str | None = None
    # Stable per-deployment identifier the relay uses to label pending flows.
    # Auto-generated on first use when blank (env override: LIONCLAW_INSTANCE_ID).
    instance_id: str | None = None


def _resolve_backend(
    yaml_backend: str | None,
    yaml_url: str | None,
    backend_env: str,
    url_env: str,
) -> tuple[str, str | None]:
    """Resolve a (backend, url) pair: env > YAML > default ("postgres", None).

    Raises ``ValueError`` when ``postgres`` is selected without a connection URL.
    """
    backend = (os.environ.get(backend_env) or yaml_backend or "postgres").strip().lower()
    url = (os.environ.get(url_env) or yaml_url or "").strip() or None
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown persistence backend {backend!r} (expected one of {_VALID_BACKENDS}). "
            f"Check {backend_env} or config.yaml."
        )
    if backend == "postgres" and not url:
        raise ValueError(
            f"{backend_env}=postgres requires a connection URL — set {url_env} "
            "or the matching config.yaml key (e.g. "
            "postgresql://olano:<password>@localhost:5432/olano)."
        )
    return backend, url


def resolve_checkpoint_backend(config: HomeConfig | None = None) -> tuple[str, str | None]:
    """Return the active ``(checkpoint_backend, url)`` for the current process."""
    cfg = config if config is not None else load_home_config()
    return _resolve_backend(
        cfg.checkpoint_backend,
        cfg.checkpoint_db_url,
        _CHECKPOINT_BACKEND_ENV,
        _CHECKPOINT_DB_URL_ENV,
    )


def resolve_store_backend(config: HomeConfig | None = None) -> tuple[str, str | None]:
    """Return the active ``(store_backend, url)`` for the deepagents Store.

    When ``postgres`` is selected without an explicit store URL, inherit the
    checkpoint DSN so a single Postgres server can back both (the savers create
    non-colliding table names).
    """
    cfg = config if config is not None else load_home_config()
    backend = (
        (os.environ.get(_STORE_BACKEND_ENV) or cfg.store_backend or "postgres").strip().lower()
    )
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown store backend {backend!r} (expected one of {_VALID_BACKENDS}). "
            f"Check {_STORE_BACKEND_ENV} or config.yaml."
        )
    url = (os.environ.get(_STORE_DB_URL_ENV) or cfg.store_db_url or "").strip() or None
    if backend == "postgres" and not url:
        # Inherit the checkpoint DSN (env or YAML) before giving up.
        _, ckpt_url = resolve_checkpoint_backend(cfg)
        url = ckpt_url
    if backend == "postgres" and not url:
        raise ValueError(
            f"{_STORE_BACKEND_ENV}=postgres requires a connection URL — set "
            f"{_STORE_DB_URL_ENV} (or {_CHECKPOINT_DB_URL_ENV} to share one server)."
        )
    return backend, url


def is_postgres_available() -> bool:
    """True when the optional Postgres backend dependencies are importable.

    Mirrors :func:`olano.models.is_provider_available` — checks without
    importing (psycopg + the langgraph postgres saver). Used to raise an
    actionable install hint when ``postgres`` is selected but the extra is
    missing (``uv add "olano[postgres]"``).
    """
    import importlib.util

    return (
        importlib.util.find_spec("psycopg_pool") is not None
        and importlib.util.find_spec("langgraph.checkpoint.postgres") is not None
    )


_AMBIGUOUS_HOME_WARNED = False


def is_source_checkout(path: str | Path) -> bool:
    """True when ``path`` is a source checkout rather than an Olano home.

    A ``.git`` entry or a ``pyproject.toml`` marks a repository (this one, when
    someone ran ``git clone`` from their home directory, or any other project
    that happens to be called ``olano``). A home holds neither, so the check
    never mistakes a real home for a checkout.
    """
    candidate = Path(path)
    return any((candidate / marker).exists() for marker in _SOURCE_CHECKOUT_MARKERS)


def _home_candidates() -> list[Path]:
    """The homes Olano would pick on its own, in precedence order.

    ``~/.olano``, then ``~/olano`` unless a source checkout occupies it, then
    ``~/.lionclaw``.
    """
    home = Path.home()
    candidates: list[Path] = [home / _DEFAULT_HOME_NAME]
    for name in _LEGACY_HOME_NAMES:
        candidate = home / name
        if not is_source_checkout(candidate):
            candidates.append(candidate)
    return candidates


def get_olano_home(override: str | Path | None = None) -> Path:
    """
    Resolve the active Olano home.

    Precedence:
    1. explicit override (``--home``)
    2. ``OLANO_HOME``
    3. ``LIONCLAW_HOME``           (legacy; the first deployed instances set this)
    4. ``~/.olano``    if it exists
    5. ``~/olano``     if it exists and is not a source checkout (an install
       made while the visible name was the default)
    6. ``~/.lionclaw`` if it exists (the original name, left in place)
    7. ``~/.olano``    (created fresh)

    Env beats on-disk probing so an operator who pins a home always wins, and
    the legacy env var is honoured before any directory probe: a box whose
    unit file says ``LIONCLAW_HOME=/home/x/.lionclaw`` must keep using exactly
    that even if an empty ``~/.olano`` appears next to it.
    """
    if override:
        return _normalize_home(override)
    for var in (_HOME_ENV_VAR, _LEGACY_HOME_ENV_VAR):
        env_home = os.environ.get(var)
        if env_home:
            return _normalize_home(env_home)

    existing = [candidate for candidate in _home_candidates() if candidate.is_dir()]
    if existing:
        # More than one present is ambiguous, and silently picking one can look
        # like the agents/vault/sessions vanished. Say so once, loudly, then
        # continue with the newest layout.
        if len(existing) > 1:
            _warn_ambiguous_home(existing[0], existing[1:])
        return _normalize_home(existing[0])
    return get_default_olano_home()


def _warn_ambiguous_home(chosen: Path, others: list[Path]) -> None:
    global _AMBIGUOUS_HOME_WARNED
    if _AMBIGUOUS_HOME_WARNED:
        return
    _AMBIGUOUS_HOME_WARNED = True
    import logging

    logging.getLogger(__name__).warning(
        "Several Olano homes exist (%s); using %s. The others are untouched; set "
        "OLANO_HOME (or pass --home) to pin the one you want.",
        ", ".join(str(path) for path in [chosen, *others]),
        chosen,
    )


def get_default_olano_home() -> Path:
    """The built-in default home, ignoring overrides, env and existing legacy homes: ``~/.olano``."""
    return (Path.home() / _DEFAULT_HOME_NAME).resolve()


def is_default_home(path: str | Path) -> bool:
    """True when ``path`` is a home Olano would have picked on its own.

    Every default layout counts, the current one and both earlier ones: an
    install sitting at ``~/olano`` or ``~/.lionclaw`` is still running "the
    default home", and telling its operator to pass ``--home`` for it would be
    wrong.
    """
    resolved = _normalize_home(path)
    home = Path.home()
    return resolved in {
        (home / name).resolve() for name in (_DEFAULT_HOME_NAME, *_LEGACY_HOME_NAMES)
    }


def set_olano_home(path: str | Path) -> Path:
    """Set the active Olano home for the current process and future lookups.

    Writes *both* env var names. Child processes are the reason: during the
    rename a subprocess may be running code that only knows ``LIONCLAW_HOME``,
    and leaving a stale value there while setting ``OLANO_HOME`` would point
    parent and child at two different homes.
    """
    resolved = _normalize_home(path)
    os.environ[_HOME_ENV_VAR] = str(resolved)
    os.environ[_LEGACY_HOME_ENV_VAR] = str(resolved)
    return resolved


def get_olano_paths(override: str | Path | None = None) -> OlanoHomePaths:
    """Return the active Olano home path bundle."""
    return OlanoHomePaths(root=get_olano_home(override))


def ensure_olano_home(override: str | Path | None = None) -> OlanoHomePaths:
    """Create the directory scaffold and default config for the active Olano home."""
    paths = get_olano_paths(override)
    for directory in [
        paths.root,
        paths.agents_dir,
        paths.vault_dir,
        paths.sessions_dir,
        paths.logs_dir,
        paths.scheduler_dir,
        paths.users_dir,
        paths.managed_skills_dir,
        paths.shared_dir,
        paths.run_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.config_path.exists():
        save_home_config(HomeConfig(), override=paths.root)

    return paths


# ── The parsed config.yaml document, cached ──────────────────────────────────
# Every settings block in the home config used to re-read and re-parse the WHOLE
# file, and there are ~16 of them (``system_settings.load_*_settings``) plus
# ``load_home_config`` — which ``runtime/auth`` calls four times per HTTP request.
# One GET /api/agents/{name} parsed this file 22 times. With a realistic config
# (every block present, ~2 KB) PyYAML's pure-Python loader costs ~4.9 ms a parse,
# so that one request spent ~110 ms doing nothing but re-reading a file that had
# not changed. Every endpoint in the product paid a version of the same tax.
#
# So: parse once per (file, mtime, size, inode) and serve a copy. Notes on the
# three choices that matter:
#
# * **Revalidate by stat, don't trust the cache blindly.** ``olano config …``
#   runs in a *separate process* from the runtime server, install.sh writes this
#   file at provisioning time, and an operator may edit it by hand. One ``stat``
#   per read keeps every one of those visible on the next call while still
#   skipping the parse. The inode is in the key because
#   ``write_home_config_text`` writes-then-``os.replace``s, which always mints a
#   new one — that catches a rewrite even on a filesystem with coarse mtimes.
# * **Hand out a deep copy.** The read-modify-write writers
#   (``update_home_config``, ``system_settings._write_raw``) mutate the dict they
#   are given. Returning the shared snapshot would let one of them corrupt what
#   every later reader sees, with no write to disk to explain it.
#
# Cached read (stat + copy) measures ~40 µs against the ~4.9 ms parse it skips.
# * **Key by resolved path, not a process singleton.** Tests (and ``--home``)
#   point at a different home mid-process; a singleton would serve one home's
#   settings to another.
_DOC_LOCK = threading.RLock()
_DOC_CACHE: dict[str, tuple[tuple[int, int, int], dict]] = {}

# libyaml when it is available (~8x faster than the pure-Python scanner), the
# stdlib loader when it is not. Both are safe loaders.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def read_home_config_doc(override: str | Path | None = None) -> dict:
    """The parsed ``<home>/config.yaml`` as a plain dict — the one read path.

    Returns ``{}`` for a missing or empty file, exactly as every caller's
    ``yaml.safe_load(...) or {}`` did. A file that exists but cannot be read or
    parsed **raises**, also as before: the settings loaders in
    :mod:`olano.config.system_settings` each catch that and fall back to
    defaults, and ``load_home_config`` deliberately does not. Swallowing it here
    would turn a malformed config into a silent reset of every home-level field
    — a wrong ``runtime_api_port`` or a dropped ``checkpoint_db_url`` — instead
    of a loud failure. Caching a parse must not change what failure means.

    The returned dict is the caller's to mutate.
    """
    path = get_olano_paths(override).config_path
    key = str(path)
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        with _DOC_LOCK:
            _DOC_CACHE.pop(key, None)
        return {}

    with _DOC_LOCK:
        cached = _DOC_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return copy.deepcopy(cached[1])

    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=_YAML_LOADER) or {}
    if not isinstance(doc, dict):
        # A top-level sequence/scalar. Raising matches the AttributeError the
        # old `raw.get(...)` produced on the same file.
        raise TypeError(f"{path} must contain a YAML mapping, got {type(doc).__name__}")

    with _DOC_LOCK:
        _DOC_CACHE[key] = (stamp, doc)
    return copy.deepcopy(doc)


def invalidate_home_config_doc(path: str | Path | None = None) -> None:
    """Drop the cached parse for ``path`` (or every home when omitted).

    The stat check already catches external writes; this closes the one gap it
    can't — a write whose stat stamp happens to match the cached one. Called
    from :func:`write_home_config_text`, so every in-process write is covered.
    """
    with _DOC_LOCK:
        if path is None:
            _DOC_CACHE.clear()
        else:
            _DOC_CACHE.pop(str(path), None)


def load_home_config(override: str | Path | None = None) -> HomeConfig:
    """Load per-home config, creating defaults when missing."""
    paths = ensure_olano_home(override)
    if not paths.config_path.exists():
        return HomeConfig()

    raw = read_home_config_doc(paths.root)
    return HomeConfig(
        default_agent=raw.get("default_agent", "merli"),
        preferred_model=raw.get("preferred_model"),
        runtime_api_port=int(raw.get("runtime_api_port", 8765)),
        dashboard_port=int(raw.get("dashboard_port", 3200)),
        # Tri-state: absent/null stays None so the runtime can resolve the
        # posture-dependent default; an explicit true/false is preserved.
        multi_user_enabled=(
            bool(raw["multi_user_enabled"]) if raw.get("multi_user_enabled") is not None else None
        ),
        ui_mode=("simple" if raw.get("ui_mode") == "simple" else "advanced"),
        highlights_all_agents=bool(raw.get("highlights_all_agents", False)),
        extra_bind_roots=list(raw.get("extra_bind_roots", []) or []),
        archetype_dirs=list(raw.get("archetype_dirs", []) or []),
        egress_allow=list(raw.get("egress_allow", []) or []),
        egress_deny=list(raw.get("egress_deny", []) or []),
        sandbox_tool_sets=(
            list(raw["sandbox_tool_sets"]) if raw.get("sandbox_tool_sets") is not None else []
        ),
        checkpoint_backend=str(raw.get("checkpoint_backend", "postgres")),
        checkpoint_db_url=(str(raw["checkpoint_db_url"]) if raw.get("checkpoint_db_url") else None),
        store_backend=str(raw.get("store_backend", "postgres")),
        store_db_url=(str(raw["store_db_url"]) if raw.get("store_db_url") else None),
        mcp_relay_url=(str(raw["mcp_relay_url"]) if raw.get("mcp_relay_url") else None),
        instance_id=(str(raw["instance_id"]) if raw.get("instance_id") else None),
    )


def write_home_config_text(path: Path, content: str) -> None:
    """Write ``<home>/config.yaml`` atomically — the one way this file may be written.

    Same hazard as agent.json5 (see ``config/loader.py:write_agent_config_text``):
    ``Path.write_text`` truncates before it fills, so a reader landing in that
    window gets **zero bytes**, which ``yaml.safe_load`` returns as ``None`` —
    and every reader here coalesces that to ``{}`` (``… or {}``). So a truncated
    read of this file does not raise; it silently reports a home with **no
    settings at all**, and the caller proceeds on defaults. That is live
    behaviour, not just a boot risk: the settings blocks are re-read on a timer
    (the disk monitor re-reads ``disk_monitor:`` every tick, catalog sync reads
    ``catalog_sync:``), so a tick that raced a dashboard save would silently run
    the default thresholds. Worse, the read-modify-write writers here
    (``update_home_config``, ``system_settings._write_raw``) load that empty
    ``{}``, merge their one key into it, and write it back — turning a
    momentary bad read into **permanent loss** of every other key in the file.

    Writing to a sibling temp file and ``os.replace``-ing it in closes the
    window. Route new ``config.yaml`` writers through here.
    """
    # Local import: `config.home` is on the earliest import path and
    # `markdown_memory` pulls in langchain_core.tools.
    from olano.memory.markdown_memory import atomic_write_text

    atomic_write_text(path, content)
    invalidate_home_config_doc(path)


def save_home_config(config: HomeConfig, override: str | Path | None = None) -> Path:
    """Persist per-home config to <home>/config.yaml.

    Serialises a *full* HomeConfig — so it overwrites the top-level document and
    would drop any keys this dataclass doesn't own (e.g. the ``cognition`` block).
    For partial edits that must preserve those keys, use :func:`update_home_config`.
    """
    paths = get_olano_paths(override)
    paths.root.mkdir(parents=True, exist_ok=True)
    write_home_config_text(paths.config_path, yaml.dump(asdict(config), sort_keys=False))
    return paths.config_path


# Recognised HomeConfig fields the dashboard / API may patch, with their coercers.
# update_home_config merges only these into config.yaml, leaving every other
# top-level key (most importantly the ``cognition`` block) untouched.
_HOME_FIELD_COERCERS: dict[str, Callable[[Any], object]] = {
    "default_agent": lambda v: str(v),
    "preferred_model": lambda v: str(v) if v else None,
    "runtime_api_port": lambda v: int(v),
    "dashboard_port": lambda v: int(v),
    "multi_user_enabled": lambda v: bool(v),
    # Anything but the one alternate value reads as the default shell, so a
    # bad write can never leave an operator with no dashboard.
    "ui_mode": lambda v: "simple" if v == "simple" else "advanced",
    "highlights_all_agents": lambda v: bool(v),
    "extra_bind_roots": lambda v: [str(x) for x in (v or [])],
    "archetype_dirs": lambda v: [str(x) for x in (v or [])],
    "egress_allow": lambda v: [str(x) for x in (v or [])],
    "egress_deny": lambda v: [str(x) for x in (v or [])],
    "sandbox_tool_sets": lambda v: [str(x) for x in (v or [])],
    "checkpoint_backend": lambda v: str(v),
    "checkpoint_db_url": lambda v: str(v) if v else None,
    "store_backend": lambda v: str(v),
    "store_db_url": lambda v: str(v) if v else None,
    "mcp_relay_url": lambda v: str(v) if v else None,
    "instance_id": lambda v: str(v) if v else None,
}

#: The root config.yaml keys :func:`update_home_config` will patch — the public
#: face of ``_HOME_FIELD_COERCERS`` for callers (the admin layer) that must
#: report honestly which keys a patch can and cannot reach.
PATCHABLE_HOME_KEYS: frozenset[str] = frozenset(_HOME_FIELD_COERCERS)


def update_home_config(
    updates: dict[str, object],
    override: str | Path | None = None,
) -> HomeConfig:
    """Merge a partial update into <home>/config.yaml, preserving unknown keys.

    Unlike :func:`save_home_config` (which serialises a full HomeConfig via
    ``asdict`` and would clobber the ``cognition`` block and any other top-level
    keys), this reads the raw YAML, overwrites only the recognised HomeConfig
    fields present in ``updates``, and writes the merged document back. Unknown
    keys in ``updates`` are ignored. Returns the reloaded HomeConfig.
    """
    paths = get_olano_paths(override)
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = read_home_config_doc(paths.root)
    for key, value in updates.items():
        coerce = _HOME_FIELD_COERCERS.get(key)
        if coerce is not None:
            raw[key] = coerce(value)
    write_home_config_text(paths.config_path, yaml.dump(raw, sort_keys=False))
    return load_home_config(override)
