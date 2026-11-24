"""Outbound-destination policy: which hosts an agent's tools may reach.

Two lists at two levels, resolved to one policy per agent:

* ``config.yaml → egress_allow / egress_deny`` — the deployment's lists.
* ``agent.json5 → egress_allow / egress_deny`` — one agent's own opinion.

The resolution rule is three lines and deliberately no longer:

* **deny is the union** — a deployment deny can never be undone by an agent;
* **allow is the agent's list when it has one, else the deployment's** — the
  same "the agent wins when it has an opinion" rule the cognition gates use;
* **empty means open** — no allow list anywhere ⇒ every public destination
  is reachable, which is the posture every existing box already has, so
  adding this feature changes nothing until someone writes a list.

Deny is checked before allow (that is :func:`olano.security.ssrf.validate_outbound_url`'s
order, and the two lists ride into it unchanged as ``ssrf_allowlist`` /
``ssrf_blocklist``). The private-network block that module applies is
independent of these lists and stays on regardless.

Two older spellings are folded in rather than retired, because both already
exist in customer configs: ``security.ssrf_allowed_domains`` /
``ssrf_blocked_domains`` (the same per-agent lists under their original name)
and ``backend.egress_allow`` / ``egress_deny`` on the ``lionclaw_docker``
block (accepted for a long time, wired to nothing). All three agent-level
sources are unioned, so a list written under any name counts.

**What the lists govern today**: the in-runtime tools that route through the
SSRF gate — ``web_fetch``, ``http_request``, the media/image fetchers and
outbound A2A. They do **not** yet govern the sandbox shell (``curl``, ``pip``)
or the integration toolkits that build their own HTTP clients; that is the
network-layer half, and it reads the same policy when it lands. The user
guide says exactly this so nobody reads a domain list as a firewall.

Normalisation is one function (:func:`normalize_domain`) with two postures,
mirroring the ``sandbox:`` block's contract: **strict** for an agent config
and a dashboard save (a bad entry is an error the author sees), **lenient**
for the deployment file's hot-path read (a hand-edit must never take the
tool gate down — the entry is dropped with a warning instead).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# A bare host: DNS labels (letters, digits, hyphen, the odd underscore) joined
# by dots. IPv4 literals match too. IPv6 literals do not — the matcher in
# ``ssrf.py`` is a string suffix test, and a bracketed literal would never
# compare equal to what ``urlparse`` hands it.
_HOST_RE = re.compile(
    r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*$"
)

#: The request-context keys the SSRF-gated tools read. Pinned here so the
#: policy module and its consumers can never drift on the spelling.
ALLOW_KEY = "ssrf_allowlist"
DENY_KEY = "ssrf_blocklist"


class EgressRuleError(ValueError):
    """An entry that cannot be read as a domain name or IPv4 address."""


def host_matches(host: str, rules: Iterable[str]) -> bool:
    """Does *host* match any rule? A rule covers itself and its subdomains.

    The one definition of "matches" — :mod:`olano.security.ssrf` and
    :mod:`olano.security.egress_guard` both call it, so the gate that refuses a
    URL and the guard that refuses a socket can never disagree about whether
    ``files.example.com`` is covered by ``example.com``.
    """
    lowered = host.strip().lower().rstrip(".")
    for rule in rules:
        clean = str(rule).strip().lower().rstrip(".")
        if clean and (lowered == clean or lowered.endswith("." + clean)):
            return True
    return False


def normalize_domain(value: Any) -> str | None:
    """One list entry → a bare lowercase host, or ``None`` for a blank entry.

    Forgiving about how people write hosts: ``https://Example.com/path``,
    ``example.com:443``, ``*.example.com`` and ``.example.com`` all become
    ``example.com``. A domain covers its subdomains in the matcher, so the
    wildcard adds nothing and is simply dropped. Raises
    :class:`EgressRuleError` when what is left is not host-shaped.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if "://" in text:
        host = urlsplit(text).hostname or ""
    else:
        host = text.split("/", 1)[0]
        if host.count(":") == 1:  # "example.com:443" — a port, not IPv6
            host = host.split(":", 1)[0]
    # Leading "*." / "." (a wildcard adds nothing — a domain already covers its
    # subdomains) and a trailing "." (an FQDN spelling) are noise, not hosts.
    host = host.strip().lower().strip("*.")
    if not host:
        return None
    if not _HOST_RE.match(host):
        raise EgressRuleError(f"{text!r} is not a domain name or IPv4 address")
    return host


def normalize_domains(values: Iterable[Any] | None, *, strict: bool) -> list[str]:
    """Normalise a list of entries, deduplicated in first-seen order.

    ``strict=True`` raises on the first bad entry (an agent config, a
    dashboard save). ``strict=False`` drops it with a warning (the deployment
    file read on the tool path, which must never raise).
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        try:
            host = normalize_domain(raw)
        except EgressRuleError:
            if strict:
                raise
            logger.warning("egress list entry %r is not a host — ignored", raw)
            continue
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


@dataclass(frozen=True)
class EgressPolicy:
    """The resolved answer for one agent: what its gated tools may reach."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    #: Which level supplied ``allow``: ``"agent"``, ``"deployment"`` or
    #: ``"open"`` (nobody — everything public is reachable). Diagnostics only.
    allow_source: str = "open"

    @property
    def is_open(self) -> bool:
        return not self.allow and not self.deny

    def as_request_metadata(self) -> dict[str, list[str]]:
        """The ``security`` metadata block the gated tools read per call."""
        return {ALLOW_KEY: list(self.allow), DENY_KEY: list(self.deny)}


def deployment_egress_lists(override: Any = None) -> tuple[list[str], list[str]]:
    """The deployment's two lists from ``config.yaml``, leniently normalised.

    Reads through :func:`olano.config.home.read_home_config_doc` — the parse
    cache — so calling this on every tool call costs one ``stat``. Never
    raises: a missing or unreadable file is an open policy.
    """
    try:
        from olano.config.home import get_olano_paths, read_home_config_doc

        if not get_olano_paths(override).config_path.exists():
            return [], []
        raw = read_home_config_doc(override)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("deployment egress lists unavailable, treating as open: %s", exc)
        return [], []
    return (
        normalize_domains(_as_list(raw.get("egress_allow")), strict=False),
        normalize_domains(_as_list(raw.get("egress_deny")), strict=False),
    )


def agent_egress_lists(config: AgentConfig | None) -> tuple[list[str], list[str]]:
    """One agent's own opinion, all three spellings unioned.

    Top-level ``egress_allow`` / ``egress_deny`` (the fields the dashboard
    edits), the legacy ``security.ssrf_allowed_domains`` /
    ``ssrf_blocked_domains``, and — on a ``lionclaw_docker`` backend block —
    the legacy ``backend.egress_allow`` / ``egress_deny``. Lenient: the
    strict validation already ran when the config loaded, and a list this
    function cannot read must not stop a turn.
    """
    if config is None:
        return [], []
    allow: list[Any] = list(_as_list(getattr(config, "egress_allow", None)))
    deny: list[Any] = list(_as_list(getattr(config, "egress_deny", None)))
    security = getattr(config, "security", None)
    allow += _as_list(getattr(security, "ssrf_allowed_domains", None))
    deny += _as_list(getattr(security, "ssrf_blocked_domains", None))
    backend = getattr(config, "backend", None)
    if getattr(backend, "type", None) == "lionclaw_docker":
        allow += _as_list(getattr(backend, "egress_allow", None))
        deny += _as_list(getattr(backend, "egress_deny", None))
    return normalize_domains(allow, strict=False), normalize_domains(deny, strict=False)


def resolve_egress_policy(
    config: AgentConfig | None = None,
    *,
    deployment: tuple[Iterable[Any], Iterable[Any]] | None = None,
    agent: tuple[Iterable[Any], Iterable[Any]] | None = None,
) -> EgressPolicy:
    """Deployment + agent → one :class:`EgressPolicy`.

    ``deployment`` pins the deployment's ``(allow, deny)`` for callers that
    already hold them (tests, diagnostics); by default they are read from
    ``config.yaml`` on each call so a dashboard save applies to the very next
    tool call with no reload.

    ``agent`` does the same for the agent half, and is how the sandbox backend
    asks: it holds that agent's lists already (read once when the backend was
    built) and must not re-parse ``agent.json5`` on every shell command. Given
    it, ``config`` is not consulted at all.
    """
    if deployment is None:
        dep_allow, dep_deny = deployment_egress_lists()
    else:
        dep_allow = normalize_domains(deployment[0], strict=False)
        dep_deny = normalize_domains(deployment[1], strict=False)
    if agent is None:
        agent_allow, agent_deny = agent_egress_lists(config)
    else:
        agent_allow = normalize_domains(agent[0], strict=False)
        agent_deny = normalize_domains(agent[1], strict=False)

    deny = normalize_domains([*dep_deny, *agent_deny], strict=False)
    if agent_allow:
        allow, source = agent_allow, "agent"
    elif dep_allow:
        allow, source = dep_allow, "deployment"
    else:
        allow, source = [], "open"
    return EgressPolicy(allow=tuple(allow), deny=tuple(deny), allow_source=source)


def security_metadata(config: AgentConfig | None) -> dict[str, list[str]]:
    """The per-call ``security`` metadata the tool gate pushes — one helper so
    the pipeline wrapper and the deepagents middleware cannot disagree.

    Also the install point for :mod:`olano.security.egress_guard`: this runs on
    every governed tool call and nowhere else, so the HTTP chokepoints are
    guaranteed to be patched before any policy can be in force, with no
    import-time side effect and no boot-order requirement. ``install()`` is
    idempotent — after the first call it is one boolean check.
    """
    from olano.security.egress_guard import install as install_guard

    install_guard()
    return resolve_egress_policy(config).as_request_metadata()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set | frozenset):
        return list(value)
    return []
