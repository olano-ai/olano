"""How this machine launches its local Chromium, and how an agent's browser
presents itself to the sites it visits.

**Presenting as the browser it is.** Sites decide whether to serve a page to a
browser from what the browser says about itself, and a headless Chromium driven
by Playwright said four things no person's browser says (measured on the
Chromium the runtime ships, 141): ``navigator.webdriver`` was ``true``
(Playwright's ``--enable-automation``); every request carried
``sec-ch-ua: "HeadlessChrome"``, because Playwright overrides the user agent
without the client-hint brands and Chromium then falls back to its headless
ones; the user agent claimed macOS while ``navigator.platform`` and the
platform hint said Linux (a hardcoded Mac string); and the clock was pinned to
New York wherever the machine was. None of that is about pretending to be a
person, it is about not contradicting itself, which is what bot checks look
for first. So an agent browser launches without the automation switch, its
identity is read off the binary once per launch (:func:`probe_identity`) with
only the ``HeadlessChrome`` marker removed, and that identity is applied to
every page (:func:`apply_identity`, client hints included; ``sessions`` arms
each page) and, as a header, to the first request of a popup, which leaves
before a page-level override can land (:func:`context_kwargs`). The live browser is a real headed Chrome in its
own container and keeps its own identity.

**Proxy.** ``BROWSER_PROXY_URL`` (vault, per agent) routes a local agent
browser's traffic through a proxy, so a site sees the proxy's address instead
of this server's. :func:`parse_proxy` is the one reading of it; a value that
cannot be used refuses the browser rather than silently going direct (the
operator asked for the proxy because going direct gets blocked).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

# Patterns that warrant an HTTP/2 retry
HTTP2_ERRORS = ("ERR_HTTP2", "ERR_CONNECTION", "ERR_NETWORK", "ERR_TIMED_OUT", "net::ERR_")


def base_launch_args() -> list[str]:
    """Baseline Chromium launch flags for the current environment.

    Chromium's setuid sandbox refuses to start when the process runs as **root**,
    which is exactly how the Docker image runs (no ``USER`` directive), so add
    ``--no-sandbox`` there, and ``--disable-dev-shm-usage`` so Chromium doesn't
    crash on the tiny 64 MB ``/dev/shm`` containers get by default. Local
    non-root dev keeps the real sandbox (returns ``[]``). Gated on either an
    explicit ``LIONCLAW_CONTAINERIZED=1`` (set by the deploy compose files) or an
    effective uid of 0, so a raw ``docker run`` of the image is covered too.
    """
    containerized = os.environ.get("LIONCLAW_CONTAINERIZED") == "1"
    try:
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        is_root = False
    if containerized or is_root:
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []


# The cloud metadata endpoints, unreachable from an agent browser on every hop.
# The guard refuses them before a request it can see is sent, but a redirect
# hop never reaches the guard; Chromium's own resolver rules stop it before it
# leaves (a pattern matches an IP literal as text, so ``169.254.*`` is the
# whole link-local range; CIDR notation is not understood). Mirrors
# ``guard._ALWAYS_BLOCKED_HOSTS``. Shared with the live browser's launcher
# (``docker/olano-browser/browserd.py``), which keeps its own copy.
METADATA_RESOLVER_RULES = (
    "MAP 169.254.* ~NOTFOUND, MAP 100.100.100.200 ~NOTFOUND, "
    "MAP metadata.google.internal ~NOTFOUND, MAP metadata ~NOTFOUND, "
    "MAP [fd00:ec2::254] ~NOTFOUND, MAP [::ffff:a9fe:a9fe] ~NOTFOUND"
)

# Flags for a browser an agent drives (not for the render tools, which load
# the agent's own HTML): no automation marker, the metadata block above, and
# WebRTC kept on the proxy, without which a page can learn this server's own
# address over UDP even while every request goes through BROWSER_PROXY_URL.
AGENT_BROWSER_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    f"--host-resolver-rules={METADATA_RESOLVER_RULES}",
)


def agent_launch_args() -> list[str]:
    """Launch flags for an agent's headless browser."""
    return base_launch_args() + list(AGENT_BROWSER_ARGS)


def launch_kwargs() -> dict[str, Any]:
    """Extra ``chromium.launch`` kwargs: currently the browser *channel*.

    Playwright ships two Chromium builds: the full browser and
    ``chromium-headless-shell``, a headless-only binary roughly half the size.
    Everything here launches headless, so the runtime image installs the shell
    alone and selects it with ``LIONCLAW_BROWSER_CHANNEL``, worth ~600 MB on a
    25 GB disk, where the image set is the largest thing on disk.

    Env-gated rather than hardcoded because the channel must match what is
    actually installed: a developer who ran ``playwright install chromium``
    has no shell binary, and pinning the channel would fail their launch. Unset
    (the default everywhere but the image) keeps Playwright's own default.
    """
    channel = os.environ.get("LIONCLAW_BROWSER_CHANNEL", "").strip()
    return {"channel": channel} if channel else {}


def agent_launch_kwargs() -> dict[str, Any]:
    """``chromium.launch`` kwargs for an agent's headless browser: the channel,
    minus Playwright's ``--enable-automation`` (it sets ``navigator.webdriver``)."""
    return {**launch_kwargs(), "ignore_default_args": ["--enable-automation"]}


def is_http2_error(msg: str) -> bool:
    return any(k in msg for k in HTTP2_ERRORS)


VIEWPORT = {"width": 1280, "height": 800}
LOCALE = "en-US"
# Chromium adds the q-weights itself: this goes out as "en-US,en;q=0.9".
ACCEPT_LANGUAGE = "en-US,en"


# ── Identity ────────────────────────────────────────────────────────────────

_HEADLESS_BRAND = "HeadlessChrome"
# ``navigator.userAgentData`` exists only in a secure context, so the probe
# reads it on an https page the probe context serves itself (no network).
_PROBE_URL = "https://identity.probe.invalid/"
_PROBE_JS = """async () => {
  const d = navigator.userAgentData;
  const hi = d ? await d.getHighEntropyValues(
    ["architecture", "bitness", "fullVersionList", "platformVersion", "uaFullVersion"]) : {};
  return {
    ua: navigator.userAgent,
    platform: navigator.platform,
    brands: d ? d.brands : [],
    uaPlatform: d ? d.platform : "",
    mobile: d ? d.mobile : false,
    architecture: hi.architecture || "",
    bitness: hi.bitness || "",
    fullVersionList: hi.fullVersionList || [],
    platformVersion: hi.platformVersion || "",
    fullVersion: hi.uaFullVersion || "",
  };
}"""


@dataclass(frozen=True)
class Identity:
    """What an agent's headless browser says it is: its own values, with the
    headless marker removed and nothing else changed."""

    user_agent: str
    platform: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sec_ch_ua(self) -> str:
        return ", ".join(
            f'"{b["brand"]}";v="{b["version"]}"' for b in self.metadata.get("brands", [])
        )

    def override(self) -> dict[str, Any]:
        """``Emulation.setUserAgentOverride`` parameters."""
        return {
            "userAgent": self.user_agent,
            "acceptLanguage": ACCEPT_LANGUAGE,
            "platform": self.platform,
            "userAgentMetadata": self.metadata,
        }


def _without_headless(brands: list[dict[str, Any]], chromium_version: str) -> list[dict[str, str]]:
    out = [
        {"brand": str(b.get("brand", "")), "version": str(b.get("version", ""))}
        for b in brands
        if str(b.get("brand", "")) != _HEADLESS_BRAND
    ]
    if not any(b["brand"] == "Chromium" for b in out):
        out.append({"brand": "Chromium", "version": chromium_version})
    return out


_PLATFORMS = (("Win", "Windows"), ("Mac", "macOS"), ("Linux", "Linux"), ("CrOS", "Chrome OS"))


def identity_from_probe(probe: dict[str, Any]) -> Identity:
    """The identity a probe of the real binary describes, headless marker removed.

    The user agent is put in Chrome's reduced form (``Chrome/141.0.0.0``), which
    every Chrome has sent since 113; the headless shell still sends the full
    build number, one more thing no person's browser says.
    """
    ua = str(probe.get("ua") or "")
    full_version = str(probe.get("fullVersion") or "")
    match = re.search(r"(?:Headless)?Chrome/(\d+)[\d.]*", ua)
    major = full_version.split(".", 1)[0] if full_version else (match.group(1) if match else "")
    if match and major:
        ua = ua[: match.start()] + f"Chrome/{major}.0.0.0" + ua[match.end() :]
    nav_platform = str(probe.get("platform") or "")
    ua_platform = str(probe.get("uaPlatform") or "") or next(
        (name for prefix, name in _PLATFORMS if nav_platform.startswith(prefix)), ""
    )
    full_list = _without_headless(list(probe.get("fullVersionList") or []), full_version or major)
    metadata = {
        "brands": _without_headless(list(probe.get("brands") or []), major),
        "fullVersionList": full_list,
        "fullVersion": full_version,
        "platform": ua_platform,
        "platformVersion": str(probe.get("platformVersion") or ""),
        "architecture": str(probe.get("architecture") or ""),
        "model": "",
        "mobile": bool(probe.get("mobile")),
        "bitness": str(probe.get("bitness") or ""),
        "wow64": False,
    }
    return Identity(user_agent=ua, platform=nav_platform, metadata=metadata)


async def probe_identity(browser: Any) -> Identity | None:
    """Read the identity off *browser* (one throwaway page, once per launch).

    ``None`` when it cannot be read; the browser then keeps Playwright's
    defaults, which is how it ran before this existed.
    """
    context = None
    try:
        context = await browser.new_context()
        await context.route(
            _PROBE_URL + "**",
            lambda route: route.fulfill(status=200, content_type="text/html", body="<p>probe</p>"),
        )
        page = await context.new_page()
        await page.goto(_PROBE_URL, timeout=10_000)
        probe = await page.evaluate(_PROBE_JS)
        return identity_from_probe(probe)
    except Exception as exc:
        logger.warning("browser: could not read the browser's identity: %s", exc)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def apply_identity(context: Any, page: Any, identity: Identity | None) -> None:
    """Make *page* present *identity*, client hints and ``navigator`` included.

    Never raises: a page that closed first simply needs nothing.
    """
    if identity is None:
        return
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Emulation.setUserAgentOverride", identity.override())
    except Exception as exc:
        logger.debug("browser: identity override skipped: %s", exc)


# ── Proxy ───────────────────────────────────────────────────────────────────

PROXY_CREDENTIAL = "BROWSER_PROXY_URL"
_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})


class ProxyError(ValueError):
    """``BROWSER_PROXY_URL`` is set to something the browser cannot use."""


def parse_proxy(raw: str | None) -> dict[str, str] | None:
    """Playwright's ``proxy`` option for *raw*, or ``None`` when unset.

    Accepts ``scheme://[user:password@]host:port`` with scheme ``http``,
    ``https``, ``socks5`` or ``socks4``. Raises :class:`ProxyError` (the message
    is user-facing and never repeats the password) for anything else.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ProxyError(f"{PROXY_CREDENTIAL} is not a valid proxy address ({exc}).") from None
    scheme = parts.scheme.lower()
    if scheme not in _PROXY_SCHEMES:
        raise ProxyError(
            f"{PROXY_CREDENTIAL} uses '{scheme}:', which the browser cannot use as a proxy. "
            "Use http://, https://, socks5:// or socks4://."
        )
    host = parts.hostname or ""
    if not host:
        raise ProxyError(f"{PROXY_CREDENTIAL} has no host (expected scheme://host:port).")
    from olano.browser.guard import metadata_refusal

    if metadata_refusal(host):
        raise ProxyError(f"{PROXY_CREDENTIAL} points at a cloud metadata address.")
    if port is None:
        raise ProxyError(f"{PROXY_CREDENTIAL} has no port (expected scheme://host:port).")
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if username and scheme.startswith("socks"):
        raise ProxyError(
            f"{PROXY_CREDENTIAL}: Chromium cannot sign in to a SOCKS proxy. Use the "
            "provider's http:// endpoint with the same user name and password, or a "
            "SOCKS proxy that allows this server's address without a password."
        )
    shown_host = f"[{host}]" if ":" in host else host
    proxy = {"server": f"{scheme}://{shown_host}:{port}"}
    if username:
        proxy["username"] = username
        proxy["password"] = password
    return proxy


def proxy_fingerprint(proxy: dict[str, str] | None) -> str:
    """A short, password-free key for *proxy* (``""`` for none): tells whether
    an open session or a remembered site block belongs to the same exit."""
    if not proxy:
        return ""
    material = "\n".join(proxy.get(k, "") for k in ("server", "username", "password"))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# ── Context options ─────────────────────────────────────────────────────────


def context_kwargs(
    *,
    headless: bool,
    identity: Identity | None = None,
    timezone: str = "",
    proxy: dict[str, str] | None = None,
) -> dict[str, Any]:
    """``new_context`` options every agent session shares.

    Service workers are blocked because their fetches bypass the context's
    request interceptor (the egress guard); downloads are refused because a
    download lands on the runtime's disk, outside every workspace.

    *identity* (headless only) sets the user agent and the ``sec-ch-ua`` header,
    which is what a popup's first request carries before
    :func:`apply_identity` reaches it. *timezone* is ``browser.timezone``
    (empty: the browser's own clock). *proxy* is :func:`parse_proxy`'s result.
    """
    kwargs: dict[str, Any] = {
        "viewport": dict(VIEWPORT),
        "locale": LOCALE,
        "color_scheme": "light",
        "device_scale_factor": 1,
        "service_workers": "block",
        "accept_downloads": False,
    }
    if headless and identity is not None:
        kwargs["user_agent"] = identity.user_agent
        if identity.sec_ch_ua:
            kwargs["extra_http_headers"] = {"sec-ch-ua": identity.sec_ch_ua}
    if timezone:
        kwargs["timezone_id"] = timezone
    if proxy:
        kwargs["proxy"] = dict(proxy)
    return kwargs
