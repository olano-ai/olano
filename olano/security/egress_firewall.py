"""Make the sandbox proxy non-optional, with one netfilter rule per container.

:mod:`olano.security.egress_proxy` points a sandbox at a policy-checking proxy
by setting ``HTTPS_PROXY`` in its shell. Every package manager and HTTP client
honours that — and anything that does not, ignores the policy completely. A
``curl --noproxy '*'``, a Go binary that reads only ``HTTP_PROXY`` when it feels
like it, or a plain socket opened from a Python script would all walk straight
past it. Proxy environment is a convention; this module is the enforcement.

One rule per governed container, in netfilter's ``DOCKER-USER`` chain::

    -s <container ip> -p tcp -j REJECT --reject-with tcp-reset
    -s <container ip> -j DROP

``DOCKER-USER`` hangs off ``FORWARD``, which carries container → *elsewhere*
traffic. Container → the bridge **gateway** is not forwarded — it is ``INPUT``
on the host — so these rules cut off the world while leaving the lane to the
proxy open. That asymmetry is the whole trick, and it is verified live in
``tests/integration/test_sandbox_egress_live.py`` rather than assumed.

**TCP is rejected, not dropped.** A drop makes every blocked connection hang
until the client's timeout: measured at six seconds for a default ``wget``, and
an agent that waits six seconds to learn it may not reach a host will do it
again and again inside one turn. ``tcp-reset`` refuses instantly, so the tool
prints "connection refused" and the model moves on. Non-TCP still drops —
there is nothing useful to answer a blocked UDP datagram with, and the sandbox
does not need outbound DNS: the proxy resolves on its behalf.

Every rule carries ``-m comment --comment "olano-egress:<agent>"``. That is
what makes the rules *ours*: :func:`sweep_stale` can find and remove them
without touching an operator's own firewall, and a rule left behind by a
crashed runtime is recoverable instead of being an invisible DROP that
someone has to diagnose from the inside of a container.

Nothing here raises into a caller. A box with no ``iptables``, an unprivileged
runtime, or a kernel without the comment match loses *this* layer and says so
in the log; the proxy environment still applies, and the runtime-side guard is
untouched. Losing enforcement is a downgrade to report, never a reason to fail
an agent's shell command.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

CHAIN = "DOCKER-USER"
_TAG = "olano-egress"
_TIMEOUT = 15

#: Remembered so a capability failure is logged once, not on every command.
_warned = False


def _tag_for(agent: str) -> str:
    """The comment stamped on this agent's rules. Bounded — netfilter caps a
    comment at 256 bytes and rejects the rule outright past that."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent).strip("-")[:180] or "agent"
    return f"{_TAG}:{slug}"


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["iptables", "-w", *args], capture_output=True, text=True, timeout=_TIMEOUT
        )
    except Exception as exc:
        logger.debug("iptables %s failed: %s", " ".join(args), exc)
        return None


def available() -> bool:
    """Can this box install the rules at all? Logged once when it cannot."""
    global _warned
    if shutil.which("iptables") is None:
        if not _warned:
            logger.warning(
                "iptables is not available — an agent's sandbox shell is pointed at the "
                "egress proxy but cannot be forced through it; a tool that ignores proxy "
                "environment will reach the network directly"
            )
            _warned = True
        return False
    proc = _run(["-L", CHAIN, "-n"])
    if proc is None or proc.returncode != 0:
        if not _warned:
            logger.warning(
                "the %s netfilter chain is not usable (docker not running, or the runtime "
                "lacks NET_ADMIN) — the sandbox egress policy is advisory for this box",
                CHAIN,
            )
            _warned = True
        return False
    return True


def _rule_specs(ip: str, tag: str) -> list[list[str]]:
    """The two rules for one container, in the order they must be inserted.

    Inserted with ``-I`` (top of chain), so the LAST one inserted is matched
    FIRST: the TCP reject has to go in second to end up above the catch-all
    drop, or every TCP connection would hit the drop and hang — the exact
    behaviour the reject exists to avoid.
    """
    common = ["-s", ip, "-m", "comment", "--comment", tag]
    return [
        [*common, "-j", "DROP"],
        [*common, "-p", "tcp", "-j", "REJECT", "--reject-with", "tcp-reset"],
    ]


def _exists(spec: list[str]) -> bool:
    proc = _run(["-C", CHAIN, *spec])
    return proc is not None and proc.returncode == 0


def apply_for(agent: str, container_ip: str) -> bool:
    """Confine *container_ip* to the proxy lane. Idempotent; never raises."""
    if not container_ip or not available():
        return False
    tag = _tag_for(agent)
    ok = True
    for spec in _rule_specs(container_ip, tag):
        if _exists(spec):
            continue
        proc = _run(["-I", CHAIN, *spec])
        if proc is None or proc.returncode != 0:
            detail = (proc.stderr.strip() if proc else "") or "unknown error"
            logger.warning(
                "could not confine agent %r (%s) to the egress proxy: %s",
                agent,
                container_ip,
                detail,
            )
            ok = False
    if ok:
        logger.info("agent %r sandbox (%s) confined to the egress proxy", agent, container_ip)
    return ok


def clear_for(agent: str, container_ip: str | None = None) -> int:
    """Remove this agent's rules — all of them, or just one container's.

    Returns how many were removed. Called when a policy goes open again, when
    a container stops, and by the sweep.
    """
    if not available():
        return 0
    tag = _tag_for(agent)
    removed = 0
    for spec in _find_rules(tag, container_ip):
        proc = _run(["-D", CHAIN, *spec])
        if proc is not None and proc.returncode == 0:
            removed += 1
    if removed:
        logger.info("removed %d egress rule(s) for agent %r", removed, agent)
    return removed


def _find_rules(tag: str, container_ip: str | None = None) -> list[list[str]]:
    """Our rules currently in the chain, as ``-D``-able argument lists.

    Parsed from ``iptables -S`` rather than remembered in a dict, so a rule
    left behind by a crashed runtime is still findable by the next one.
    """
    proc = _run(["-S", CHAIN])
    if proc is None or proc.returncode != 0:
        return []
    found: list[list[str]] = []
    for line in proc.stdout.splitlines():
        if tag not in line or not line.startswith(f"-A {CHAIN} "):
            continue
        try:
            parts = _split_rule(line[len(f"-A {CHAIN} ") :])
        except ValueError:
            continue
        if container_ip and f"{container_ip}/32" not in parts and container_ip not in parts:
            continue
        found.append(parts)
    return found


def _split_rule(rest: str) -> list[str]:
    """Split an ``iptables -S`` rule body, honouring the quoted comment."""
    import shlex

    return shlex.split(rest)


def sweep_stale(live_ips: set[str]) -> int:
    """Drop rules for containers that no longer exist.

    A rule keyed on an IP outlives the container that had it, and docker reuses
    addresses — so a stale rule does not merely leak, it eventually confines
    somebody else's container to a proxy that is not listening for them. Run at
    startup, with the addresses of every container that is actually live.
    """
    if not available():
        return 0
    proc = _run(["-S", CHAIN])
    if proc is None or proc.returncode != 0:
        return 0
    removed = 0
    for line in proc.stdout.splitlines():
        if f"{_TAG}:" not in line or not line.startswith(f"-A {CHAIN} "):
            continue
        try:
            parts = _split_rule(line[len(f"-A {CHAIN} ") :])
        except ValueError:
            continue
        ip = _source_of(parts)
        if ip is None or ip in live_ips:
            continue
        drop = _run(["-D", CHAIN, *parts])
        if drop is not None and drop.returncode == 0:
            removed += 1
    if removed:
        logger.info("swept %d stale sandbox egress rule(s)", removed)
    return removed


def _source_of(parts: list[str]) -> str | None:
    if "-s" not in parts:
        return None
    value = parts[parts.index("-s") + 1]
    return value.split("/", 1)[0]


def rules_for(agent: str) -> list[str]:
    """This agent's live rules, as text. Diagnostics and tests."""
    return [" ".join(spec) for spec in _find_rules(_tag_for(agent))]


def sweep_stale_from_docker() -> int:
    """:func:`sweep_stale`, with the live address set read from docker.

    Called once at runtime startup. A runtime that was killed rather than
    stopped leaves its rules behind, and the address they name is one docker
    hands to the next container that asks — so the first thing a new process
    should do is forget rules whose container is gone.
    """
    if shutil.which("docker") is None or not available():
        return 0
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--no-trunc",
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        live: set[str] = set()
        if ids:
            inspect = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                    *ids,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
            for line in (inspect.stdout or "").splitlines():
                live.update(part for part in line.split() if part)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("could not enumerate live containers for the egress sweep: %s", exc)
        return 0
    return sweep_stale(live)
